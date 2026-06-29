from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime
from html import escape
from importlib import resources
from pathlib import Path
from typing import Any

import requests
from markdown_it import MarkdownIt

from .core import (
    AgentError,
    AppConfig,
    copy_file,
    format_utc8,
    load_config,
    load_manifest,
    markdown_path_from_manifest,
    now_iso,
    parse_iso8601,
    read_json,
    report_route,
    report_url_from_route,
    resolve_config_path,
    run_name_from_dir,
    status_class,
    write_json,
    write_text,
)

def generation_agent_config(config: AppConfig) -> tuple[str, dict[str, Any]]:
    generation = config.data["generation"]
    agent = generation.get("agent")
    if not isinstance(agent, str) or not agent.strip():
        raise AgentError("generation.agent must be a non-empty string")

    agents = generation.get("agents")
    if not isinstance(agents, dict):
        raise AgentError("generation.agents must be an object")

    agent_config = agents.get(agent)
    if not isinstance(agent_config, dict):
        raise AgentError(f"generation.agents.{agent} must be an object")
    return agent, agent_config


def generation_command_template(config: AppConfig) -> list[str]:
    agent, agent_config = generation_agent_config(config)
    command_template = agent_config.get("command_template")
    if not isinstance(command_template, list) or not all(isinstance(item, str) for item in command_template):
        raise AgentError(f"generation.agents.{agent}.command_template must be a string array")
    return command_template


def agent_string(agent_config: dict[str, Any], key: str) -> str:
    value = agent_config.get(key, "")
    return value if isinstance(value, str) else str(value)


def generation_command_env(agent: str, agent_config: dict[str, Any]) -> dict[str, str]:
    env = os.environ.copy()
    base_url = agent_string(agent_config, "base_url")
    model = agent_string(agent_config, "model")
    api_key = agent_string(agent_config, "api_key")

    if base_url:
        env["DAILY_VULNS_AGENT_BASE_URL"] = base_url
        if agent == "claude":
            env["ANTHROPIC_BASE_URL"] = base_url
        elif agent == "codex":
            env["OPENAI_BASE_URL"] = base_url
    if model:
        env["DAILY_VULNS_AGENT_MODEL"] = model
    if api_key:
        env["DAILY_VULNS_AGENT_API_KEY"] = api_key
        if agent == "claude":
            env["ANTHROPIC_API_KEY"] = api_key
        elif agent == "codex":
            env["OPENAI_API_KEY"] = api_key
    return env


def toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def codex_config_override_args(agent_config: dict[str, Any]) -> list[str]:
    base_url = agent_string(agent_config, "base_url")
    model = agent_string(agent_config, "model")
    api_key = agent_string(agent_config, "api_key")
    if not any((base_url, model, api_key)):
        return []

    args = ["--ignore-user-config"]
    if model:
        args.extend(["--model", model])
    if base_url:
        provider = "daily_vulns_openai"
        args.extend(["-c", f"model_provider={toml_string(provider)}"])
        args.extend(["-c", f"model_providers.{provider}.name={toml_string('Daily Vulns OpenAI')}"])
        args.extend(["-c", f"model_providers.{provider}.base_url={toml_string(base_url)}"])
        args.extend(["-c", f"model_providers.{provider}.env_key={toml_string('OPENAI_API_KEY')}"])
        args.extend(["-c", f"model_providers.{provider}.wire_api={toml_string('responses')}"])
    return args


def apply_codex_config_overrides(command: list[str], prompt: str, agent_config: dict[str, Any]) -> list[str]:
    override_args = codex_config_override_args(agent_config)
    if not override_args:
        return command
    try:
        prompt_index = command.index(prompt)
    except ValueError:
        return [*command, *override_args]
    return [*command[:prompt_index], *override_args, *command[prompt_index:]]


def generation_timeout_seconds(config: AppConfig) -> int:
    value = config.data["generation"].get("timeout_seconds", 3600)
    try:
        timeout = int(value)
    except (TypeError, ValueError) as exc:
        raise AgentError("generation.timeout_seconds must be an integer") from exc
    if timeout <= 0:
        raise AgentError("generation.timeout_seconds must be greater than 0")
    return timeout


def generation_prompt(config: AppConfig) -> str:
    generation = config.data["generation"]
    prompt_file = resolve_config_path(config, generation["prompt_file"])
    return prompt_file.read_text(encoding="utf-8")


def prepare_generation_workdir(config: AppConfig, run_dir: Path) -> Path:
    skills_src = config.root / "skills"
    skill_src = skills_src / "daily-vulns"
    if not (skill_src / "SKILL.md").exists():
        raise AgentError(f"daily-vulns skill not found: {skill_src / 'SKILL.md'}")
    skills_dst = run_dir / "skills"
    skills_dst.symlink_to(skills_src, target_is_directory=True)
    return skills_dst / "daily-vulns"


def cmd_generate(config: AppConfig) -> Path:
    runs_dir = config.runs_dir
    run_name = datetime.now().astimezone().strftime("%Y-%m-%d_%H%M%S")
    run_dir = runs_dir / run_name
    if run_dir.exists():
        raise AgentError(f"run_dir already exists: {run_dir}")
    run_dir.mkdir(parents=True)
    skill_dir = prepare_generation_workdir(config, run_dir)

    generation = config.data["generation"]
    prompt = generation_prompt(config)

    agent, agent_config = generation_agent_config(config)
    command_template = generation_command_template(config)
    template_vars = {
        "prompt": prompt,
        "output_dir": str(run_dir),
        "config_path": str(config.path),
        "base_url": agent_string(agent_config, "base_url"),
        "model": agent_string(agent_config, "model"),
    }
    command = [item.format(**template_vars) for item in command_template]
    if agent == "codex":
        command = apply_codex_config_overrides(command, prompt, agent_config)
    env = generation_command_env(agent, agent_config)
    env["CLAUDE_SKILL_DIR"] = str(skill_dir)

    timeout_seconds = generation_timeout_seconds(config)

    log_path = run_dir / "run.log"
    print(f"generate: running {agent} command in {run_dir}")
    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"$ {' '.join(command)}\n\n")
        try:
            result = subprocess.run(
                command,
                cwd=run_dir,
                env=env,
                text=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            output = exc.stdout or ""
            if isinstance(output, bytes):
                output = output.decode("utf-8", errors="replace")
            log.write(output)
            log.write(f"\ntimeout_seconds={timeout_seconds}\n")
            raise AgentError(f"generate timed out after {timeout_seconds} seconds, see log: {log_path}") from exc
        log.write(result.stdout or "")
        log.write(f"\nexit_code={result.returncode}\n")
    if result.returncode != 0:
        raise AgentError(f"generate failed, see log: {log_path}")

    load_manifest(run_dir)
    print(f"generate: ok {run_dir}")
    return run_dir


def render_report_html(title: str, body_html: str) -> str:
    return f"""<!doctype html>
<html lang=\"zh-CN\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>{escape(title)}</title>
  <link rel=\"stylesheet\" href=\"../../assets/style.css\">
</head>
<body>
  <main>
{body_html}
  </main>
</body>
</html>
"""


def status_label(status: str) -> str:
    return status


def render_reports_index(items: list[dict[str, Any]]) -> str:
    rows = []
    for item in items:
        status = str(item.get("notify_status", "pending"))
        rows.append(
            "<tr>"
            f"<td><a href=\"{escape(item['href'])}\">{escape(item['title'])}</a></td>"
            f"<td>{escape(item['generated_at_display'])}</td>"
            f"<td><span class=\"{escape(status_class(status))}\">{escape(status_label(status))}</span></td>"
            "</tr>"
        )
    rows_html = "\n".join(rows) or "<tr><td colspan=\"3\">暂无报告</td></tr>"
    return f"""<!doctype html>
<html lang=\"zh-CN\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>漏洞日报列表</title>
  <link rel=\"stylesheet\" href=\"../assets/style.css\">
</head>
<body>
  <main>
    <h1>漏洞日报列表</h1>
    <table>
      <thead>
        <tr><th>标题</th><th>生成时间</th><th>推送状态</th></tr>
      </thead>
      <tbody>
{rows_html}
      </tbody>
    </table>
  </main>
</body>
</html>
"""


def sync_assets(config: AppConfig) -> None:
    asset = resources.files("daily_vulns_agent.assets").joinpath("style.css")
    target = config.public_dir / "assets" / "style.css"
    target.parent.mkdir(parents=True, exist_ok=True)
    with resources.as_file(asset) as src:
        copy_file(src, target)


def meta_from_publish(manifest: dict[str, Any], publish_data: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": manifest["title"],
        "generated_at": manifest["generated_at"],
        "route": publish_data["route"],
        "notify_status": publish_data["notify_status"],
    }


def rebuild_reports_index(config: AppConfig) -> None:
    reports_dir = config.public_dir / "reports"
    items: list[dict[str, Any]] = []
    if reports_dir.exists():
        for meta_path in reports_dir.glob("*/meta.json"):
            try:
                meta = read_json(meta_path)
                parse_iso8601(meta.get("generated_at"), f"{meta_path}.generated_at")
                items.append(
                    {
                        "title": str(meta.get("title", "")),
                        "generated_at": str(meta.get("generated_at", "")),
                        "generated_at_display": format_utc8(meta.get("generated_at")),
                        "notify_status": str(meta.get("notify_status", "pending")),
                        "href": f"./{meta_path.parent.name}/",
                    }
                )
            except AgentError:
                continue
    items.sort(key=lambda item: parse_iso8601(item["generated_at"], "generated_at"), reverse=True)
    write_text(reports_dir / "index.html", render_reports_index(items))


def cmd_publish(config: AppConfig, run_dir_arg: str) -> dict[str, Any]:
    run_dir = Path(run_dir_arg).expanduser()
    if not run_dir.is_absolute():
        run_dir = (config.root / run_dir).resolve()
    manifest = load_manifest(run_dir)
    md_path = markdown_path_from_manifest(run_dir, manifest)
    markdown = md_path.read_text(encoding="utf-8")

    md = MarkdownIt("commonmark", {"html": True}).enable("table")
    body_html = md.render(markdown)
    report_html = render_report_html(manifest["title"], body_html)

    run_name = run_name_from_dir(run_dir)
    public_report_dir = config.public_dir / "reports" / run_name
    route = report_route(config, run_name)
    url = report_url_from_route(config, route)
    published_at = now_iso()

    write_text(run_dir / "report.html", report_html)
    public_report_dir.mkdir(parents=True, exist_ok=True)
    copy_file(run_dir / "report.html", public_report_dir / "index.html")
    sync_assets(config)

    publish_data = {
        "run_dir": str(run_dir),
        "public_dir": str(public_report_dir),
        "route": route,
        "published_at": published_at,
        "notify_status": "pending",
    }
    write_json(run_dir / "publish.json", publish_data)
    write_json(public_report_dir / "meta.json", meta_from_publish(manifest, publish_data))
    rebuild_reports_index(config)

    print(f"publish: ok {url}")
    return publish_data


def ding_secret_url(webhook: str, secret: str) -> str:
    timestamp = str(round(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(digest))
    separator = "&" if "?" in webhook else "?"
    return f"{webhook}{separator}timestamp={timestamp}&sign={sign}"


def build_dingtalk_payload(manifest: dict[str, Any], url: str) -> dict[str, Any]:
    highlights = manifest["highlights"]
    if not highlights:
        raise AgentError("manifest.highlights is empty; notify cannot send an empty summary")
    shown = highlights[:5]
    lines = [
        f"# {manifest['title']}",
        "",
        f"生成时间：{format_utc8(manifest['generated_at'])}",
        "",
        f"共 {len(highlights)} 条，以下展示前 {len(shown)} 条：",
        "",
    ]
    lines.extend(f"{index}. {item}" for index, item in enumerate(shown, start=1))
    lines.extend(["", f"[阅读全文]({url})"])
    return {
        "msgtype": "markdown",
        "markdown": {
            "title": manifest["title"],
            "text": "\n".join(lines),
        },
        "at": {"isAtAll": False},
    }


def update_public_meta(config: AppConfig, run_dir: Path, manifest: dict[str, Any], publish_data: dict[str, Any], status: str) -> None:
    publish_data["notify_status"] = status
    write_json(run_dir / "publish.json", publish_data)
    public_report_dir = Path(publish_data["public_dir"])
    write_json(public_report_dir / "meta.json", meta_from_publish(manifest, publish_data))
    rebuild_reports_index(config)


def cleanup_reports(config: AppConfig, keep: int = 100) -> None:
    reports_dir = config.public_dir / "reports"
    if not reports_dir.exists():
        return
    entries: list[tuple[datetime, Path]] = []
    for meta_path in reports_dir.glob("*/meta.json"):
        try:
            meta = read_json(meta_path)
            entries.append((parse_iso8601(meta.get("generated_at"), "generated_at"), meta_path.parent))
        except AgentError:
            continue
    entries.sort(key=lambda item: item[0], reverse=True)
    for _, public_report_dir in entries[keep:]:
        run_dir = config.runs_dir / public_report_dir.name
        shutil.rmtree(public_report_dir, ignore_errors=True)
        shutil.rmtree(run_dir, ignore_errors=True)
    rebuild_reports_index(config)


def cmd_notify(config: AppConfig, run_dir_arg: str, force_notify: bool = False) -> dict[str, Any]:
    run_dir = Path(run_dir_arg).expanduser()
    if not run_dir.is_absolute():
        run_dir = (config.root / run_dir).resolve()
    manifest = load_manifest(run_dir)
    publish_data = read_json(run_dir / "publish.json")
    notify_path = run_dir / "notify.json"
    if notify_path.exists() and not force_notify:
        previous = read_json(notify_path)
        if previous.get("status") == "success":
            raise AgentError("notify already succeeded; use --force-notify to send again")

    report_url_value = report_url_from_route(config, str(publish_data["route"]))
    payload = build_dingtalk_payload(manifest, report_url_value)
    webhook = config.data["dingtalk"].get("webhook")
    if not isinstance(webhook, str) or not webhook.strip():
        raise AgentError("dingtalk.webhook is required")
    secret = config.data["dingtalk"].get("secret") or ""
    url = ding_secret_url(webhook, secret) if secret else webhook

    attempts = 0
    response_summary: dict[str, Any] = {}
    status = "failed"
    for _ in range(2):
        attempts += 1
        try:
            response = requests.post(url, json=payload, timeout=15)
            response_summary = {
                "status_code": response.status_code,
                "text": response.text[:1000],
            }
            if response.ok:
                try:
                    body = response.json()
                except ValueError:
                    body = {}
                if not isinstance(body, dict) or body.get("errcode", 0) == 0:
                    status = "success"
                    break
        except requests.RequestException as exc:
            response_summary = {"error": str(exc)}
        if attempts < 2:
            time.sleep(1)

    notify_data = {
        "run_dir": str(run_dir),
        "url": report_url_value,
        "payload": payload,
        "status": status,
        "response": response_summary,
        "notified_at": now_iso(),
        "attempts": attempts,
    }
    write_json(notify_path, notify_data)
    update_public_meta(config, run_dir, manifest, publish_data, status)

    if status != "success":
        raise AgentError(f"notify failed after {attempts} attempts")
    cleanup_reports(config)
    print(f"notify: ok {report_url_value}")
    return notify_data


def cmd_run(config: AppConfig) -> None:
    run_dir: Path | None = None
    publish_data: dict[str, Any] | None = None
    try:
        run_dir = cmd_generate(config)
        publish_data = cmd_publish(config, str(run_dir))
        notify_data = cmd_notify(config, str(run_dir))
        print(f"run: ok {notify_data['url']}")
    except AgentError as exc:
        print(f"run: failed: {exc}", file=sys.stderr)
        if run_dir is not None:
            print(f"run_dir: {run_dir}", file=sys.stderr)
            print(f"run_log: {run_dir / 'run.log'}", file=sys.stderr)
            if publish_data is not None:
                report_url_value = report_url_from_route(config, str(publish_data["route"]))
                print(f"report_url: {report_url_value}", file=sys.stderr)
                print(f"retry_notify: python -m daily_vulns_agent.cli notify --config {config.path} --run-dir {run_dir}", file=sys.stderr)
        raise


def add_config_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default=argparse.SUPPRESS, help="Path to config.yaml")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="daily-vulns")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate_parser = subparsers.add_parser("generate", help="Generate report artifacts with configured model CLI")
    add_config_argument(generate_parser)

    publish_parser = subparsers.add_parser("publish", help="Render and publish a generated report")
    add_config_argument(publish_parser)
    publish_parser.add_argument("--run-dir", required=True, help="Run directory to publish")

    notify_parser = subparsers.add_parser("notify", help="Send DingTalk notification for a published report")
    add_config_argument(notify_parser)
    notify_parser.add_argument("--run-dir", required=True, help="Run directory to notify")
    notify_parser.add_argument("--force-notify", action="store_true", help="Send again even if notify.json is success")

    run_parser = subparsers.add_parser("run", help="Run generate, publish and notify")
    add_config_argument(run_parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "generate":
            cmd_generate(config)
        elif args.command == "publish":
            cmd_publish(config, args.run_dir)
        elif args.command == "notify":
            cmd_notify(config, args.run_dir, args.force_notify)
        elif args.command == "run":
            cmd_run(config)
        else:
            parser.error(f"unknown command: {args.command}")
        return 0
    except AgentError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
