from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import yaml


class AgentError(Exception):
    pass


@dataclass(frozen=True)
class AppConfig:
    path: Path
    root: Path
    data: dict[str, Any]

    @property
    def runs_dir(self) -> Path:
        return resolve_config_path(self, self.data["publish"].get("runs_dir", "runs"))

    @property
    def public_dir(self) -> Path:
        return resolve_config_path(self, self.data["publish"].get("public_dir", "public"))

    @property
    def reports_path(self) -> str:
        path = self.data["publish"].get("reports_path", "/reports")
        return "/" + str(path).strip("/")

    @property
    def site_base_url(self) -> str:
        return str(self.data["publish"]["site_base_url"]).rstrip("/")


def load_config(config_path: str | Path) -> AppConfig:
    path = Path(config_path).expanduser().resolve()
    if not path.exists():
        raise AgentError(f"config file not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    for section in ("generation", "publish", "dingtalk"):
        if section not in data or not isinstance(data[section], dict):
            raise AgentError(f"missing config section: {section}")
    return AppConfig(path=path, root=path.parent, data=data)


def resolve_config_path(config: AppConfig, value: str | Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return (config.root / path).resolve()


def read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError as exc:
        raise AgentError(f"json file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise AgentError(f"invalid json file: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise AgentError(f"json root must be an object: {path}")
    return data


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def parse_iso8601(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise AgentError(f"{field} must be a non-empty ISO 8601 string")
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise AgentError(f"{field} must be ISO 8601: {value}") from exc


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def run_name_from_dir(run_dir: Path) -> str:
    return run_dir.resolve().name


def report_route(config: AppConfig, run_name: str) -> str:
    return f"{config.reports_path}/{run_name}/"


def report_url_from_route(config: AppConfig, route: str) -> str:
    return urljoin(config.site_base_url + "/", route.lstrip("/"))


def report_url(config: AppConfig, run_name: str) -> str:
    return report_url_from_route(config, report_route(config, run_name))


def ensure_inside(base: Path, candidate: Path) -> Path:
    base_resolved = base.resolve()
    candidate_resolved = candidate.resolve()
    try:
        candidate_resolved.relative_to(base_resolved)
    except ValueError as exc:
        raise AgentError(f"path escapes run_dir: {candidate}") from exc
    return candidate_resolved


def load_manifest(run_dir: Path) -> dict[str, Any]:
    manifest_path = run_dir / "manifest.json"
    manifest = read_json(manifest_path)

    title = manifest.get("title")
    if not isinstance(title, str) or not title.strip():
        raise AgentError("manifest.title is required")

    parse_iso8601(manifest.get("generated_at"), "manifest.generated_at")

    md_path = manifest.get("md_path")
    if not isinstance(md_path, str) or not md_path.strip():
        raise AgentError("manifest.md_path is required")
    md_candidate = Path(md_path)
    if md_candidate.is_absolute():
        raise AgentError("manifest.md_path must be relative")
    ensure_inside(run_dir, run_dir / md_candidate)

    highlights = manifest.get("highlights")
    if not isinstance(highlights, list):
        raise AgentError("manifest.highlights must be a string array")
    for index, item in enumerate(highlights):
        if not isinstance(item, str) or not item.strip():
            raise AgentError(f"manifest.highlights[{index}] must be a non-empty string")

    return manifest


def markdown_path_from_manifest(run_dir: Path, manifest: dict[str, Any]) -> Path:
    md_path = ensure_inside(run_dir, run_dir / manifest["md_path"])
    if not md_path.exists():
        raise AgentError(f"markdown file not found: {md_path}")
    if not md_path.read_text(encoding="utf-8").strip():
        raise AgentError(f"markdown file is empty: {md_path}")
    return md_path


def status_class(status: str) -> str:
    if status in {"pending", "success", "failed"}:
        return f"status-{status}"
    return ""
