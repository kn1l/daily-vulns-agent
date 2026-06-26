from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .core import AgentError, AppConfig, load_config, now_iso, read_json, resolve_config_path, write_json

ADMIN_PREFIX = "/admin"
CONFIG_ENV = "DAILY_VULNS_CONFIG"
CONFIG_PATH = Path(os.environ.get(CONFIG_ENV, "config.yaml")).expanduser().resolve()
TEMPLATE_DIR = Path(__file__).parent / "templates"


def create_app() -> FastAPI:
    config = load_config(CONFIG_PATH)
    app = FastAPI()
    secret = str(config.data.get("web", {}).get("admin", {}).get("session_secret", "change-me-session-secret"))
    app.add_middleware(SessionMiddleware, secret_key=secret, max_age=7 * 24 * 60 * 60, same_site="lax", https_only=False)
    state = WebState(config_path=CONFIG_PATH)
    app.state.web_state = state

    @app.on_event("startup")
    def startup() -> None:
        state.reload_config()
        state.init_runtime_paths()
        state.reload_scheduler()

    @app.on_event("shutdown")
    def shutdown() -> None:
        state.shutdown()

    register_routes(app, state)
    return app


class WebState:
    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path
        self.config = load_config(config_path)
        self.templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
        self.scheduler = BackgroundScheduler()
        self.scheduler.start(paused=True)

    @property
    def state_dir(self) -> Path:
        return self.config.root / "state"

    @property
    def scheduler_state_path(self) -> Path:
        return self.state_dir / "scheduler_state.json"

    @property
    def scheduler_runs_dir(self) -> Path:
        return self.state_dir / "scheduler_runs"

    def reload_config(self) -> None:
        self.config = load_config(self.config_path)

    def init_runtime_paths(self) -> None:
        self.config.runs_dir.mkdir(parents=True, exist_ok=True)
        self.scheduler_runs_dir.mkdir(parents=True, exist_ok=True)
        (self.config.public_dir / "reports").mkdir(parents=True, exist_ok=True)
        (self.config.public_dir / "assets").mkdir(parents=True, exist_ok=True)

    def shutdown(self) -> None:
        self.scheduler.shutdown(wait=False)

    def reload_scheduler(self) -> None:
        self.scheduler.remove_all_jobs()
        schedule = self.schedule_config()
        if schedule["enabled"]:
            timezone = ZoneInfo(schedule["timezone"])
            for run_time in schedule["times"]:
                hour, minute = parse_hhmm(run_time)
                self.scheduler.add_job(
                    self.run_scheduled_command,
                    trigger=CronTrigger(hour=hour, minute=minute, timezone=timezone),
                    id=f"daily-{run_time}",
                    replace_existing=True,
                    max_instances=5,
                )
        self.scheduler.resume()
        self.write_scheduler_summary()

    def schedule_config(self) -> dict[str, Any]:
        schedule = self.config.data.get("schedule", {})
        times = schedule.get("times", [])
        if not isinstance(times, list):
            times = []
        return {
            "enabled": bool(schedule.get("enabled", False)),
            "timezone": str(schedule.get("timezone", "Asia/Shanghai")),
            "times": [str(item) for item in times],
        }

    def scheduler_state(self) -> dict[str, Any]:
        try:
            state = read_json(self.scheduler_state_path)
        except AgentError:
            state = {}
        state.setdefault("enabled", self.schedule_config()["enabled"])
        state.setdefault("next_runs", [])
        state.setdefault("last_run", None)
        state.setdefault("last_success", None)
        state.setdefault("last_failure", None)
        return state

    def write_scheduler_summary(self, last_run: dict[str, Any] | None = None) -> None:
        existing = self.scheduler_state()
        if last_run is not None:
            existing["last_run"] = last_run
            if last_run.get("status") == "success":
                existing["last_success"] = last_run
            else:
                existing["last_failure"] = last_run
        existing["enabled"] = self.schedule_config()["enabled"]
        existing["next_runs"] = [job.next_run_time.isoformat() for job in self.scheduler.get_jobs() if job.next_run_time]
        write_json(self.scheduler_state_path, existing)

    def run_scheduled_command(self) -> None:
        self.reload_config()
        started_at = now_iso()
        log_name = datetime.now().astimezone().strftime("%Y-%m-%d_%H%M%S") + ".log"
        log_path = self.scheduler_runs_dir / log_name
        command_template = self.config.data.get("web", {}).get("run_command", [])
        if not isinstance(command_template, list) or not all(isinstance(item, str) for item in command_template):
            summary = {
                "status": "failed",
                "started_at": started_at,
                "finished_at": now_iso(),
                "log_path": str(log_path),
                "error": "web.run_command must be a string array",
            }
            self.write_scheduler_summary(summary)
            return
        command = [item.format(config_path=str(self.config.path)) for item in command_template]
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("w", encoding="utf-8") as log:
            log.write(f"$ {' '.join(command)}\n\n")
            result = subprocess.run(command, cwd=self.config.root, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            log.write(result.stdout or "")
            log.write(f"\nexit_code={result.returncode}\n")
        status = "success" if result.returncode == 0 else "failed"
        summary = {
            "status": status,
            "started_at": started_at,
            "finished_at": now_iso(),
            "log_path": str(log_path),
            "exit_code": result.returncode,
        }
        self.write_scheduler_summary(summary)
        self.cleanup_scheduler_logs()

    def cleanup_scheduler_logs(self, keep: int = 30) -> None:
        if not self.scheduler_runs_dir.exists():
            return
        logs = sorted(self.scheduler_runs_dir.glob("*.log"), key=lambda path: path.stat().st_mtime, reverse=True)
        for log_path in logs[keep:]:
            log_path.unlink(missing_ok=True)


def parse_hhmm(value: str) -> tuple[int, int]:
    parts = value.split(":")
    if len(parts) != 2:
        raise ValueError(f"invalid HH:MM time: {value}")
    hour = int(parts[0])
    minute = int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"invalid HH:MM time: {value}")
    return hour, minute


def load_yaml_data(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise AgentError("config root must be a mapping")
    return data


def save_yaml_data(path: Path, data: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)


async def form_data(request: Request) -> dict[str, str]:
    body = (await request.body()).decode("utf-8")
    parsed = parse_qs(body, keep_blank_values=True)
    return {key: values[-1] if values else "" for key, values in parsed.items()}


def parse_command_template(value: str, field: str) -> list[str]:
    command = [line.strip() for line in value.splitlines() if line.strip()]
    if not command:
        raise AgentError(f"{field} must not be empty")
    return command


def generation_form_config(config_data: dict[str, Any]) -> dict[str, str]:
    generation = config_data.get("generation", {})
    if not isinstance(generation, dict):
        generation = {}
    agents = generation.get("agents", {})
    if not isinstance(agents, dict):
        agents = {}

    result = {"agent": str(generation.get("agent", "claude"))}
    for agent in ("claude", "codex"):
        agent_config = agents.get(agent, {})
        if not isinstance(agent_config, dict):
            agent_config = {}
        command_template = agent_config.get("command_template", [])
        api_key = str(agent_config.get("api_key", ""))
        result[f"{agent}_base_url"] = str(agent_config.get("base_url", ""))
        result[f"{agent}_model"] = str(agent_config.get("model", ""))
        result[f"{agent}_has_api_key"] = "true" if api_key else "false"
        result[f"{agent}_api_key_mask"] = "*" * len(api_key)
        result[f"{agent}_command_template"] = "\n".join(str(item) for item in command_template) if isinstance(command_template, list) else ""
    return result


def generation_form_from_post(data: dict[str, str]) -> dict[str, str]:
    result = {"agent": data.get("agent", "claude")}
    for agent in ("claude", "codex"):
        result[f"{agent}_base_url"] = data.get(f"{agent}_base_url", "")
        result[f"{agent}_model"] = data.get(f"{agent}_model", "")
        result[f"{agent}_has_api_key"] = "false"
        result[f"{agent}_command_template"] = data.get(f"{agent}_command_template", "")
    return result


def submitted_secret_value(submitted: str, existing: str) -> str:
    if existing and submitted == "*" * len(existing):
        return existing
    return submitted


def dingtalk_form_config(config_data: dict[str, Any]) -> dict[str, str]:
    dingtalk = config_data.get("dingtalk", {})
    if not isinstance(dingtalk, dict):
        dingtalk = {}
    secret = str(dingtalk.get("secret", ""))
    return {
        "webhook": str(dingtalk.get("webhook", "")),
        "has_secret": "true" if secret else "false",
        "secret_mask": "*" * len(secret),
    }


def dingtalk_form_from_post(data: dict[str, str]) -> dict[str, str]:
    return {
        "webhook": data.get("webhook", ""),
        "has_secret": "false",
        "secret_mask": "",
    }


def is_authenticated(request: Request) -> bool:
    return bool(request.session.get("authenticated"))


def redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


def template_context(request: Request, state: WebState, **extra: Any) -> dict[str, Any]:
    return {"request": request, "admin_prefix": ADMIN_PREFIX, "error": None, "message": None, **extra}


def render(state: WebState, request: Request, template: str, **context: Any) -> HTMLResponse:
    return state.templates.TemplateResponse(request, template, template_context(request, state, **context))


def require_login(request: Request) -> RedirectResponse | None:
    if is_authenticated(request):
        return None
    return redirect(f"{ADMIN_PREFIX}/login")


def register_routes(app: FastAPI, state: WebState) -> None:
    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return redirect(f"{ADMIN_PREFIX}/")

    @app.get(f"{ADMIN_PREFIX}/", response_class=HTMLResponse)
    def dashboard(request: Request) -> Any:
        if login_redirect := require_login(request):
            return login_redirect
        state.reload_config()
        schedule = state.schedule_config()
        return render(state, request, "admin/dashboard.html", title="仪表盘", schedule=schedule, scheduler_state=state.scheduler_state())

    @app.get(f"{ADMIN_PREFIX}/login", response_class=HTMLResponse)
    def login_page(request: Request) -> HTMLResponse:
        return render(state, request, "admin/login.html", title="登录")

    @app.post(f"{ADMIN_PREFIX}/login")
    async def login(request: Request) -> Any:
        data = await form_data(request)
        state.reload_config()
        admin = state.config.data.get("web", {}).get("admin", {})
        if data.get("username") == admin.get("username") and data.get("password") == admin.get("password"):
            request.session["authenticated"] = True
            return redirect(f"{ADMIN_PREFIX}/")
        return render(state, request, "admin/login.html", title="登录", error="用户名或密码错误")

    @app.get(f"{ADMIN_PREFIX}/logout")
    def logout(request: Request) -> RedirectResponse:
        request.session.clear()
        return redirect(f"{ADMIN_PREFIX}/login")

    @app.get(f"{ADMIN_PREFIX}/schedule", response_class=HTMLResponse)
    def schedule_page(request: Request) -> Any:
        if login_redirect := require_login(request):
            return login_redirect
        state.reload_config()
        return render(state, request, "admin/schedule.html", title="调度配置", schedule=state.schedule_config())

    @app.post(f"{ADMIN_PREFIX}/schedule")
    async def save_schedule(request: Request) -> Any:
        if login_redirect := require_login(request):
            return login_redirect
        data = await form_data(request)
        times = [line.strip() for line in data.get("times", "").splitlines() if line.strip()]
        try:
            for item in times:
                parse_hhmm(item)
            ZoneInfo(data.get("timezone", "Asia/Shanghai"))
        except (ValueError, ZoneInfoNotFoundError) as exc:
            state.reload_config()
            return render(state, request, "admin/schedule.html", title="调度配置", schedule=state.schedule_config(), error=str(exc))
        config_data = load_yaml_data(state.config_path)
        schedule = config_data.setdefault("schedule", {})
        schedule["enabled"] = data.get("enabled") == "1"
        schedule["timezone"] = data.get("timezone", "Asia/Shanghai")
        schedule["times"] = times
        save_yaml_data(state.config_path, config_data)
        state.reload_config()
        state.reload_scheduler()
        return render(state, request, "admin/schedule.html", title="调度配置", schedule=state.schedule_config(), message="调度配置已保存并重载")

    @app.get(f"{ADMIN_PREFIX}/settings")
    def settings_page() -> RedirectResponse:
        return redirect(f"{ADMIN_PREFIX}/settings/site")

    @app.get(f"{ADMIN_PREFIX}/settings/site", response_class=HTMLResponse)
    def site_settings_page(request: Request) -> Any:
        if login_redirect := require_login(request):
            return login_redirect
        state.reload_config()
        return render(
            state,
            request,
            "admin/settings_site.html",
            title="站点配置",
            publish=state.config.data.get("publish", {}),
        )

    @app.post(f"{ADMIN_PREFIX}/settings/site")
    async def save_site_settings(request: Request) -> Any:
        if login_redirect := require_login(request):
            return login_redirect
        data = await form_data(request)
        config_data = load_yaml_data(state.config_path)
        publish = config_data.setdefault("publish", {})
        publish["site_base_url"] = data.get("site_base_url", "").rstrip("/")
        save_yaml_data(state.config_path, config_data)
        state.reload_config()
        return render(
            state,
            request,
            "admin/settings_site.html",
            title="站点配置",
            publish=state.config.data.get("publish", {}),
            message="站点配置已保存",
        )

    @app.get(f"{ADMIN_PREFIX}/settings/dingtalk", response_class=HTMLResponse)
    def dingtalk_settings_page(request: Request) -> Any:
        if login_redirect := require_login(request):
            return login_redirect
        state.reload_config()
        return render(
            state,
            request,
            "admin/settings_dingtalk.html",
            title="钉钉配置",
            dingtalk=dingtalk_form_config(state.config.data),
        )

    @app.post(f"{ADMIN_PREFIX}/settings/dingtalk")
    async def save_dingtalk_settings(request: Request) -> Any:
        if login_redirect := require_login(request):
            return login_redirect
        data = await form_data(request)
        config_data = load_yaml_data(state.config_path)
        dingtalk = config_data.setdefault("dingtalk", {})
        dingtalk["webhook"] = data.get("webhook", "")
        dingtalk["secret"] = submitted_secret_value(data.get("secret", ""), str(dingtalk.get("secret", "")))
        save_yaml_data(state.config_path, config_data)
        state.reload_config()
        return render(
            state,
            request,
            "admin/settings_dingtalk.html",
            title="钉钉配置",
            dingtalk=dingtalk_form_config(state.config.data),
            message="钉钉配置已保存",
        )

    @app.get(f"{ADMIN_PREFIX}/settings/agents", response_class=HTMLResponse)
    def agent_settings_page(request: Request) -> Any:
        if login_redirect := require_login(request):
            return login_redirect
        state.reload_config()
        return render(
            state,
            request,
            "admin/settings_agents.html",
            title="Agent 配置",
            generation=generation_form_config(state.config.data),
        )

    @app.post(f"{ADMIN_PREFIX}/settings/agents")
    async def save_agent_settings(request: Request) -> Any:
        if login_redirect := require_login(request):
            return login_redirect
        data = await form_data(request)
        config_data = load_yaml_data(state.config_path)
        generation = config_data.setdefault("generation", {})
        agent = data.get("agent", "claude")
        agents = generation.setdefault("agents", {})
        if not isinstance(agents, dict):
            agents = {}
            generation["agents"] = agents
        agent_configs: dict[str, dict[str, Any]] = {}
        try:
            if agent not in {"claude", "codex"}:
                raise AgentError("generation.agent must be claude or codex")
            for agent_name in ("claude", "codex"):
                existing_agent_config = agents.get(agent_name, {})
                if not isinstance(existing_agent_config, dict):
                    existing_agent_config = {}
                api_key = submitted_secret_value(data.get(f"{agent_name}_api_key", ""), str(existing_agent_config.get("api_key", "")))
                agent_configs[agent_name] = {
                    "base_url": data.get(f"{agent_name}_base_url", ""),
                    "api_key": api_key,
                    "model": data.get(f"{agent_name}_model", ""),
                    "command_template": parse_command_template(data.get(f"{agent_name}_command_template", ""), f"{agent_name} command_template"),
                }
        except AgentError as exc:
            state.reload_config()
            return render(
                state,
                request,
                "admin/settings_agents.html",
                title="Agent 配置",
                generation=generation_form_from_post(data),
                error=str(exc),
            )
        generation["agent"] = agent
        agents["claude"] = agent_configs["claude"]
        agents["codex"] = agent_configs["codex"]
        save_yaml_data(state.config_path, config_data)
        state.reload_config()
        return render(
            state,
            request,
            "admin/settings_agents.html",
            title="Agent 配置",
            generation=generation_form_config(state.config.data),
            message="Agent 配置已保存",
        )


app = create_app()
