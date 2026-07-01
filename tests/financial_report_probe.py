"""Experimental probe for financial-report material information.

This file is intentionally kept under tests/ and does not wire anything into
the dashboard production path. It scans MOPS material-information rows,
extracts financial-report metrics, writes a demo cache, and optionally compares
coverage against the public chengwaye realtime-fin JSON page.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_ROOT))

from financial_report_crawler import (  # noqa: E402
    build_financial_report_record,
    dedupe_financial_report_records,
)
from mops_crawler import MopsCrawler  # noqa: E402

REFERENCE_JSON_URL = "https://chengwaye-data.pages.dev/realtime_fin.json"
REFERENCE_HEARTBEAT_URL = "https://chengwaye-data.pages.dev/watcher_heartbeat.json"
DEFAULT_TARGET_QUARTER = "2026Q1"
DEFAULT_ANNOUNCEMENT_DATES = "2026-05-08,2026-05-12,2026-05-07"
DEFAULT_CACHE_OUTPUT = PROJECT_ROOT / "data" / "raw" / "financial_report_demo_cache_2026q1.json"
DEFAULT_REPORT_OUTPUT = PROJECT_ROOT / "data" / "raw" / "financial_report_probe_2026q1_high_volume.json"
TAIWAN_TZ = ZoneInfo("Asia/Taipei")


def now_text() -> str:
    return datetime.now(TAIWAN_TZ).isoformat(timespec="seconds")


def iso_to_roc_date(value: str | date) -> str:
    target = date.fromisoformat(value) if isinstance(value, str) else value
    return f"{target.year - 1911:03d}/{target.month:02d}/{target.day:02d}"


def parse_iso_dates(value: str) -> list[date]:
    return [date.fromisoformat(part.strip()) for part in value.split(",") if part.strip()]


def query_dates_for_announcement_dates(
    announcement_dates: list[date],
    query_extra_days: int,
) -> list[date]:
    query_dates: set[date] = set()
    for announcement_date in announcement_dates:
        for offset in range(query_extra_days + 1):
            query_dates.add(announcement_date + timedelta(days=offset))
    return sorted(query_dates)


def fetch_mops_summaries_with_retries(
    crawler: MopsCrawler,
    target_date: date,
    market: str,
    attempts: int,
    retry_sleep_seconds: float,
) -> tuple[list[dict[str, Any]], list[str]]:
    errors: list[str] = []
    for attempt in range(1, attempts + 1):
        try:
            return crawler.fetch_previous_day_summaries(target_date=target_date, market=market), errors
        except Exception as exc:  # pragma: no cover - network probe resilience
            errors.append(f"{target_date.isoformat()} attempt {attempt}: {type(exc).__name__}: {exc}")
            if attempt < attempts:
                time.sleep(retry_sleep_seconds)
    return [], errors


def compact_record(record: dict[str, Any]) -> dict[str, Any]:
    metrics = record.get("metrics") or {}
    return {
        "company_id": record.get("company_id", ""),
        "company_name": record.get("company_name", ""),
        "spoke_date": record.get("spoke_date", ""),
        "spoke_time": record.get("spoke_time", ""),
        "quarter": record.get("quarter", ""),
        "eps": record.get("eps"),
        "gross_margin_pct": record.get("gross_margin_pct"),
        "operating_margin_pct": record.get("operating_margin_pct"),
        "non_operating_pct": record.get("non_operating_pct"),
        "revenue_k": metrics.get("revenue_k"),
        "gross_profit_k": metrics.get("gross_profit_k"),
        "operating_income_k": metrics.get("operating_income_k"),
        "pretax_income_k": metrics.get("pretax_income_k"),
        "title": record.get("title", ""),
    }


def reference_entry_quarter(entry: dict[str, Any]) -> str:
    parsed = entry.get("parsed") or {}
    if parsed.get("報告期間"):
        return str(parsed.get("報告期間"))
    report_year = parsed.get("報告年")
    report_quarter = parsed.get("報告季度")
    if report_year and report_quarter:
        return f"{report_year}{report_quarter}"
    return ""


def load_reference_payload(skip_reference: bool) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if skip_reference:
        return None, None
    headers = {"User-Agent": "twse-news-dashboard-financial-report-probe/0.1"}
    payload = requests.get(REFERENCE_JSON_URL, timeout=30, headers=headers).json()
    heartbeat = requests.get(REFERENCE_HEARTBEAT_URL, timeout=20, headers=headers).json()
    return payload, heartbeat


def metric_from_reference(parsed: dict[str, Any], target_quarter: str, key: str) -> Any:
    quarter_suffix = target_quarter[-2:]
    candidates = {
        "eps": [f"{quarter_suffix}_EPS", "全年EPS"],
        "gross_margin_pct": [f"{quarter_suffix}_毛利率"],
        "operating_margin_pct": [f"{quarter_suffix}_營益率"],
        "non_operating_pct": [f"{quarter_suffix}_業外佔比"],
    }[key]
    for candidate in candidates:
        if candidate in parsed:
            return parsed.get(candidate)
    return None


def values_match(left: Any, right: Any, tolerance: float = 0.02) -> bool:
    if left is None or right is None:
        return False
    try:
        return abs(float(left) - float(right)) <= tolerance
    except (TypeError, ValueError):
        return False


def build_reference_comparison(
    reference_payload: dict[str, Any] | None,
    heartbeat: dict[str, Any] | None,
    records: list[dict[str, Any]],
    announcement_dates: list[date],
    target_quarter: str,
) -> dict[str, Any]:
    if not reference_payload:
        return {"enabled": False}

    roc_dates = {iso_to_roc_date(value) for value in announcement_dates}
    entries = [
        entry
        for entry in reference_payload.get("entries", [])
        if entry.get("date") in roc_dates and reference_entry_quarter(entry) == target_quarter
    ]
    reference_codes = {str(entry.get("code", "")) for entry in entries}
    mops_codes = {str(record.get("company_id", "")) for record in records}
    reference_by_code = {str(entry.get("code", "")): entry for entry in entries}

    metric_match_counts: dict[str, int] = {}
    metric_comparable_counts: dict[str, int] = {}
    for metric_key in (
        "eps",
        "gross_margin_pct",
        "operating_margin_pct",
        "non_operating_pct",
    ):
        comparable = 0
        matches = 0
        for record in records:
            reference_entry = reference_by_code.get(str(record.get("company_id", "")))
            if not reference_entry:
                continue
            reference_value = metric_from_reference(
                reference_entry.get("parsed") or {},
                target_quarter,
                metric_key,
            )
            if reference_value is None or record.get(metric_key) is None:
                continue
            comparable += 1
            if values_match(record.get(metric_key), reference_value):
                matches += 1
        metric_comparable_counts[metric_key] = comparable
        metric_match_counts[metric_key] = matches

    return {
        "enabled": True,
        "reference_url": REFERENCE_JSON_URL,
        "reference_fetched_at": reference_payload.get("fetched_at"),
        "heartbeat_last_checked": (heartbeat or {}).get("last_checked"),
        "heartbeat_fin_total": (heartbeat or {}).get("fin_total"),
        "reference_count_for_dates": len(entries),
        "reference_unique_codes": len(reference_codes),
        "mops_unique_codes": len(mops_codes),
        "code_overlap_count": len(reference_codes & mops_codes),
        "reference_only_codes_sample": sorted(reference_codes - mops_codes)[:30],
        "mops_only_codes_sample": sorted(mops_codes - reference_codes)[:30],
        "metric_comparable_counts": metric_comparable_counts,
        "metric_match_counts": metric_match_counts,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Probe financial-report MOPS material info")
    parser.add_argument("--target-quarter", default=DEFAULT_TARGET_QUARTER)
    parser.add_argument("--announcement-dates", default=DEFAULT_ANNOUNCEMENT_DATES)
    parser.add_argument("--query-extra-days", type=int, default=1)
    parser.add_argument("--market", default="all")
    parser.add_argument("--request-interval", type=float, default=2.0)
    parser.add_argument("--retry-attempts", type=int, default=3)
    parser.add_argument("--retry-sleep", type=float, default=5.0)
    parser.add_argument("--skip-reference", action="store_true")
    parser.add_argument("--cache-output", type=Path, default=DEFAULT_CACHE_OUTPUT)
    parser.add_argument("--report-output", type=Path, default=DEFAULT_REPORT_OUTPUT)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    announcement_dates = parse_iso_dates(args.announcement_dates)
    announcement_roc_dates = {iso_to_roc_date(value) for value in announcement_dates}
    query_dates = query_dates_for_announcement_dates(announcement_dates, args.query_extra_days)
    detected_at = now_text()

    crawler = MopsCrawler(
        request_interval_seconds=args.request_interval,
        timeout_seconds=30,
    )
    raw_rows: list[dict[str, Any]] = []
    fetch_errors: list[str] = []
    fetch_summaries: list[dict[str, Any]] = []
    for query_date in query_dates:
        rows, errors = fetch_mops_summaries_with_retries(
            crawler,
            query_date,
            args.market,
            args.retry_attempts,
            args.retry_sleep,
        )
        raw_rows.extend(rows)
        fetch_errors.extend(errors)
        fetch_summaries.append(
            {
                "query_date": query_date.isoformat(),
                "row_count": len(rows),
                "spoke_date_counts": dict(Counter(str(row.get("spoke_date_roc", "")) for row in rows)),
                "errors": errors,
            }
        )

    parsed_records: list[dict[str, Any]] = []
    for row in raw_rows:
        if str(row.get("spoke_date_roc", "")) not in announcement_roc_dates:
            continue
        record = build_financial_report_record(
            row,
            target_quarter=args.target_quarter,
            detected_at=detected_at,
        )
        if record is not None:
            parsed_records.append(record)

    parsed_records = dedupe_financial_report_records(parsed_records)
    parsed_records = sorted(
        parsed_records,
        key=lambda record: str(record.get("event_time") or ""),
        reverse=True,
    )
    reference_payload, heartbeat = load_reference_payload(args.skip_reference)
    comparison = build_reference_comparison(
        reference_payload,
        heartbeat,
        parsed_records,
        announcement_dates,
        args.target_quarter,
    )

    cache_payload = {
        "cache_generated_at": detected_at,
        "source": "MOPS t05st02 material information list",
        "target_quarter": args.target_quarter,
        "announcement_dates": [value.isoformat() for value in announcement_dates],
        "query_dates": [value.isoformat() for value in query_dates],
        "records": parsed_records,
    }
    report_payload = {
        "probe_started_at": detected_at,
        "probe_finished_at": now_text(),
        "target_quarter": args.target_quarter,
        "announcement_dates": [value.isoformat() for value in announcement_dates],
        "announcement_roc_dates": sorted(announcement_roc_dates),
        "query_dates": [value.isoformat() for value in query_dates],
        "request_interval_seconds": args.request_interval,
        "raw_row_count": len(raw_rows),
        "parsed_record_count": len(parsed_records),
        "parsed_company_count": len({record.get("company_id", "") for record in parsed_records}),
        "fetch_summaries": fetch_summaries,
        "fetch_errors": fetch_errors,
        "records_sample": [compact_record(record) for record in parsed_records[:30]],
        "reference_comparison": comparison,
        "notes": [
            "MOPS t05st02 query dates can include rows from adjacent announcement dates.",
            "The probe scans each announcement date plus the configured extra following query days, then de-duplicates by company/date/time/quarter/subject.",
            "The demo cache is not wired into the dashboard UI.",
        ],
    }

    args.cache_output.parent.mkdir(parents=True, exist_ok=True)
    args.report_output.parent.mkdir(parents=True, exist_ok=True)
    args.cache_output.write_text(
        json.dumps(cache_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    args.report_output.write_text(
        json.dumps(report_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report_payload, ensure_ascii=False, indent=2))
    print(f"Saved demo cache to {args.cache_output}")
    print(f"Saved probe report to {args.report_output}")


if __name__ == "__main__":
    main()
