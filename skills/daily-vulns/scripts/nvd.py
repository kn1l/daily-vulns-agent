#!/usr/bin/env python3
# Usage:
#   uv run skills/daily-vulns/scripts/nvd.py --start 2026-05-10 --end 2026-05-12
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API_URL = "https://services.nvd.nist.gov/rest/json/cves/2.0"
RESULTS_PER_PAGE = 2000
USER_AGENT = "vuln-watcher/0.1"


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


def format_nvd_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def first_english(items: list[dict[str, Any]], key: str) -> str:
    for item in items:
        if item.get("lang") == "en" and item.get(key):
            return str(item[key]).strip()
    for item in items:
        if item.get(key):
            return str(item[key]).strip()
    return ""


def references_from_cve(cve: dict[str, Any]) -> list[str]:
    refs = cve.get("references") or []
    urls: list[str] = []
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        url = str(ref.get("url") or "").strip()
        if url:
            urls.append(url)
    return urls


def tags_from_cve(cve: dict[str, Any]) -> list[str]:
    tags: list[str] = []
    seen: set[str] = set()
    for weakness in cve.get("weaknesses") or []:
        if not isinstance(weakness, dict):
            continue
        value = first_english(weakness.get("description") or [], "value")
        if value and value not in seen:
            seen.add(value)
            tags.append(value)
    return tags


def metrics_from_cve(cve: dict[str, Any]) -> tuple[str, float | None]:
    metrics = cve.get("metrics") or {}
    # Prefer the newest CVSS block available; NVD entries may only carry older metrics.
    for key in ("cvssMetricV40", "cvssMetricV31", "cvssMetricV30", "cvssMetricV2"):
        values = metrics.get(key) or []
        if not values:
            continue
        item = values[0]
        if not isinstance(item, dict):
            continue
        data = item.get("cvssData") or {}
        severity = str(item.get("baseSeverity") or data.get("baseSeverity") or "").strip().lower()
        score = data.get("baseScore")
        try:
            numeric = float(score)
        except (TypeError, ValueError):
            numeric = None
        return severity, numeric
    return "", None


def normalize_vulnerability(item: dict[str, Any]) -> dict[str, Any]:
    cve = item.get("cve") or {}
    severity, cvss = metrics_from_cve(cve)
    descriptions = cve.get("descriptions") or []
    published = str(cve.get("published") or item.get("published") or "").strip()
    cve_id = str(cve.get("id") or "").strip()
    url = f"https://nvd.nist.gov/vuln/detail/{cve_id}" if cve_id else ""
    return {
        "cve": cve_id,
        "title": cve_id or first_english(descriptions, "value") or "NVD entry",
        "source": "nvd",
        "url": url,
        "severity": severity,
        "cvss": cvss,
        "description": first_english(descriptions, "value"),
        "references": references_from_cve(cve),
        "tags": tags_from_cve(cve),
        "time": published,
        "nvd": item,
    }


def fetch_page(start: str, end: str, start_index: int, *, timeout: int) -> dict[str, Any]:
    query = urlencode(
        {
            "pubStartDate": start,
            "pubEndDate": end,
            "startIndex": start_index,
            "resultsPerPage": RESULTS_PER_PAGE,
        }
    )
    request = Request(f"{API_URL}?{query}", headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def collect(start: datetime, end: datetime, *, timeout: int) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    start_index = 0
    start_text = format_nvd_timestamp(start)
    end_text = format_nvd_timestamp(end)
    # NVD uses offset pagination and reports the total result count on each page.
    while True:
        payload = fetch_page(start_text, end_text, start_index, timeout=timeout)
        vulnerabilities = payload.get("vulnerabilities") or []
        for item in vulnerabilities:
            if isinstance(item, dict):
                records.append(normalize_vulnerability(item))
        total_results = int(payload.get("totalResults") or 0)
        results_per_page = int(payload.get("resultsPerPage") or len(vulnerabilities) or 0)
        if results_per_page == 0:
            return records
        start_index += results_per_page
        if start_index >= total_results:
            return records


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect vulnerabilities from NVD in a time window.")
    parser.add_argument("--start", required=True, help="Inclusive start date in YYYY-MM-DD or ISO format.")
    parser.add_argument("--end", required=True, help="Inclusive end date in YYYY-MM-DD or ISO format.")
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
        payload = collect(start, end, timeout=30)
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        print(f"failed to fetch NVD vulnerabilities: {exc}", file=sys.stderr)
        return 1
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
