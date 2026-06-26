#!/usr/bin/env python3
# Usage:
#   uv run skills/daily-vulns/scripts/ghsa.py --start 2026-05-10 --end 2026-05-12
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

API_URL = "https://api.github.com/advisories"
PER_PAGE = 100
USER_AGENT = "vuln-watcher/0.1"
API_VERSION = "2022-11-28"
ADVISORY_TYPES = ("unreviewed", "malware", "reviewed")


def parse_bound(value: str, *, end_of_day: bool) -> datetime:
    raw = value.strip()
    if not raw:
        raise ValueError("date value is empty")
    date_only = "T" not in raw and " " not in raw
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"invalid date: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    if date_only:
        if end_of_day:
            parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999000)
        else:
            parsed = parsed.replace(hour=0, minute=0, second=0, microsecond=0)
    return parsed


def format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def cve_from_advisory(advisory: dict[str, Any]) -> str:
    direct = str(advisory.get("cve_id") or "").strip()
    if direct:
        return direct
    for identifier in advisory.get("identifiers") or []:
        if not isinstance(identifier, dict):
            continue
        value = str(identifier.get("value") or "").strip()
        if value.startswith("CVE-"):
            return value
    return ""


def aliases_from_advisory(advisory: dict[str, Any]) -> list[str]:
    aliases: list[str] = []
    seen: set[str] = set()
    for identifier in advisory.get("identifiers") or []:
        if not isinstance(identifier, dict):
            continue
        value = str(identifier.get("value") or "").strip()
        if value and value not in seen:
            seen.add(value)
            aliases.append(value)
    return aliases


def references_from_advisory(advisory: dict[str, Any]) -> list[str]:
    references: list[str] = []
    seen: set[str] = set()
    for candidate in [advisory.get("html_url"), *list(advisory.get("references") or [])]:
        if isinstance(candidate, dict):
            url = str(candidate.get("url") or "").strip()
        else:
            url = str(candidate or "").strip()
        if url and url not in seen:
            seen.add(url)
            references.append(url)
    return references


def describe_http_error(error: HTTPError) -> str:
    try:
        body = error.read().decode("utf-8")
    except Exception:
        body = ""
    if error.code == 403:
        # GitHub reports rate-limit exhaustion as HTTP 403; make that failure explicit.
        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            payload = {}
        message = str(payload.get("message") or error.reason or "forbidden").strip()
        remaining = str(error.headers.get("X-RateLimit-Remaining") or "").strip()
        reset = str(error.headers.get("X-RateLimit-Reset") or "").strip()
        if remaining == "0":
            if reset.isdigit():
                reset_time = datetime.fromtimestamp(int(reset), tz=timezone.utc)
                return (
                    "GitHub anonymous API rate limit exceeded"
                    f" (reset at {format_timestamp(reset_time)})"
                )
            return "GitHub anonymous API rate limit exceeded"
        return f"HTTP 403: {message}"
    return f"HTTP {error.code}: {error.reason}"


def affected_packages_from_advisory(advisory: dict[str, Any]) -> list[dict[str, str]]:
    packages: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in advisory.get("vulnerabilities") or []:
        if not isinstance(item, dict):
            continue
        package = item.get("package") or {}
        if not isinstance(package, dict):
            continue
        ecosystem = str(package.get("ecosystem") or "").strip()
        name = str(package.get("name") or "").strip()
        key = (ecosystem, name)
        if not ecosystem and not name:
            continue
        if key in seen:
            continue
        seen.add(key)
        packages.append({"ecosystem": ecosystem, "name": name})
    return packages


def tags_from_advisory(advisory: dict[str, Any]) -> list[str]:
    tags: list[str] = []
    seen: set[str] = set()
    for cwe in advisory.get("cwes") or []:
        if not isinstance(cwe, dict):
            continue
        cwe_id = str(cwe.get("cwe_id") or "").strip()
        if cwe_id and cwe_id not in seen:
            seen.add(cwe_id)
            tags.append(cwe_id)
    for package in affected_packages_from_advisory(advisory):
        ecosystem = package["ecosystem"]
        name = package["name"]
        if ecosystem and ecosystem not in seen:
            seen.add(ecosystem)
            tags.append(ecosystem)
        if name and name not in seen:
            seen.add(name)
            tags.append(name)
    return tags


def numeric_score(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def cvss_version_from_vector(value: Any, *, fallback: str) -> str:
    vector = str(value or "").strip()
    if vector.startswith("CVSS:"):
        return vector.split("/", 1)[0].removeprefix("CVSS:")
    return fallback


def cvss_from_advisory(advisory: dict[str, Any]) -> tuple[float | None, str, dict[str, dict[str, Any]]]:
    severities = advisory.get("cvss_severities") or {}
    if not isinstance(severities, dict):
        severities = {}
    scores: dict[str, dict[str, Any]] = {}
    for key in ("cvss_v4", "cvss_v3"):
        value = severities.get(key) or {}
        if not isinstance(value, dict):
            continue
        score = numeric_score(value.get("score"))
        if score is None:
            continue
        label = "v4" if key == "cvss_v4" else "v3"
        scores[label] = {
            "score": score,
            "vector": str(value.get("vector_string") or "").strip(),
        }
    if "v4" in scores:
        return scores["v4"]["score"], cvss_version_from_vector(scores["v4"]["vector"], fallback="4.0"), scores
    if "v3" in scores:
        return scores["v3"]["score"], cvss_version_from_vector(scores["v3"]["vector"], fallback="3.x"), scores
    cvss = advisory.get("cvss") or {}
    if not isinstance(cvss, dict):
        cvss = {}
    score = numeric_score(cvss.get("score"))
    version = cvss_version_from_vector(cvss.get("vector_string"), fallback="") if score is not None else ""
    if score is not None:
        scores[version or "default"] = {
            "score": score,
            "vector": str(cvss.get("vector_string") or "").strip(),
        }
    return score, version, scores


def normalize_advisory(advisory: dict[str, Any]) -> dict[str, Any]:
    numeric_cvss, cvss_version, cvss_scores = cvss_from_advisory(advisory)
    updated = str(advisory.get("updated_at") or "").strip()
    published = str(advisory.get("published_at") or "").strip()
    aliases = aliases_from_advisory(advisory)
    packages = affected_packages_from_advisory(advisory)
    return {
        "cve": cve_from_advisory(advisory),
        "title": str(advisory.get("summary") or advisory.get("ghsa_id") or "GitHub advisory").strip(),
        "source": "ghsa",
        "url": str(advisory.get("html_url") or "").strip(),
        "severity": str(advisory.get("severity") or "").strip().lower(),
        "cvss": numeric_cvss,
        "cvss_version": cvss_version,
        "cvss_scores": cvss_scores,
        "description": str(advisory.get("description") or advisory.get("summary") or "").strip(),
        "references": references_from_advisory(advisory),
        "tags": tags_from_advisory(advisory),
        "time": updated or published,
        "ghsa_id": str(advisory.get("ghsa_id") or "").strip(),
        "advisory_type": str(advisory.get("type") or "").strip().lower(),
        "aliases": aliases,
        "affected_packages": packages,
        "cwes": advisory.get("cwes") or [],
        "ghsa": advisory,
    }


def next_cursor_from_link_header(value: str) -> str | None:
    # Global advisories use cursor pagination; the next cursor is only in the Link header.
    for part in value.split(","):
        section = part.strip()
        if 'rel="next"' not in section:
            continue
        start = section.find("<")
        end = section.find(">", start + 1)
        if start == -1 or end == -1:
            continue
        parsed = urlparse(section[start + 1 : end])
        cursor = parse_qs(parsed.query).get("after")
        if cursor:
            return cursor[0]
    return None


def fetch_page(
    start: str,
    end: str,
    after: str | None,
    *,
    advisory_type: str,
    token: str,
    timeout: int,
) -> tuple[list[dict[str, Any]], str | None]:
    query: dict[str, str | int] = {
        "sort": "updated",
        "direction": "asc",
        "updated": f"{start}..{end}",
        "per_page": PER_PAGE,
        "type": advisory_type,
    }
    if after:
        query["after"] = after
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": USER_AGENT,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(f"{API_URL}?{urlencode(query)}", headers=headers)
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
            next_cursor = next_cursor_from_link_header(response.headers.get("Link") or "")
    except HTTPError as exc:
        raise RuntimeError(describe_http_error(exc)) from exc
    if not isinstance(payload, list):
        raise ValueError("GitHub advisories response was not a JSON array")
    return [item for item in payload if isinstance(item, dict)], next_cursor


def collect(start: datetime, end: datetime, *, token: str, timeout: int) -> list[dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    start_text = format_timestamp(start)
    end_text = format_timestamp(end)
    for advisory_type in ADVISORY_TYPES:
        after: str | None = None
        # Stop when GitHub stops advertising a rel="next" cursor.
        while True:
            payload, after = fetch_page(
                start_text,
                end_text,
                after,
                advisory_type=advisory_type,
                token=token,
                timeout=timeout,
            )
            for item in payload:
                record = normalize_advisory(item)
                key = record.get("ghsa_id") or record.get("url") or record.get("title")
                records[str(key)] = record
            if after is None:
                break
    return sorted(records.values(), key=lambda record: str(record.get("time") or ""))


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect GitHub security advisories in a time window.")
    parser.add_argument("--start", required=True, help="Inclusive start date in YYYY-MM-DD or ISO format.")
    parser.add_argument("--end", required=True, help="Inclusive end date in YYYY-MM-DD or ISO format.")
    parser.add_argument("--token", default="", help="Optional GitHub token for advisory API requests.")
    parser.add_argument(
        "--timeout",
        type=positive_int,
        default=30,
        help="HTTP request timeout in seconds.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        start = parse_bound(args.start, end_of_day=False)
        end = parse_bound(args.end, end_of_day=True)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if end < start:
        print("--end must be on or after --start", file=sys.stderr)
        return 2
    try:
        payload = collect(start, end, token=args.token.strip(), timeout=args.timeout)
    except Exception as exc:
        print(f"failed to fetch GitHub advisories: {exc}", file=sys.stderr)
        return 1
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
