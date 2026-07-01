"""Experimental probes for monthly-revenue data sources.

This file is intentionally kept under tests/ and does not wire anything into
the dashboard production path.  It checks three sources:

1. Existing MOPS material-information path, filtered by revenue keywords.
2. TWSE listed-company monthly-revenue OpenAPI.
3. MOPS t21sc04_ifrs monthly-revenue summary for listed and OTC companies.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import time
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_ROOT))

from mops_crawler import (  # noqa: E402
    MopsCrawler,
    dedupe_records,
    sort_records_by_spoke_time,
)

MOPS_BASE_URL = "https://mopsov.twse.com.tw"
MOPS_MONTHLY_REVENUE_AJAX_PATH = "/mops/web/ajax_t21sc04_ifrs"
MOPS_DOWNLOAD_PATH = "/server-java/FileDownLoad"
TWSE_LISTED_MONTHLY_REVENUE_OPENAPI_URL = (
    "https://openapi.twse.com.tw/v1/opendata/t187ap05_L"
)
REQUEST_USER_AGENT = "twse-news-dashboard-monthly-revenue-probe/0.1"
DEFAULT_OUTPUT_PATH = (
    PROJECT_ROOT
    / "data"
    / "raw"
    / "monthly_revenue_probe_2026-06-01_2026-06-10.json"
)
DEFAULT_MATERIAL_INFO_CACHE = (
    PROJECT_ROOT / "data" / "raw" / "material_info_2026-06-01_2026-06-27.json"
)
MARKET_LABELS = {
    "sii": "上市",
    "otc": "上櫃",
    "rotc": "興櫃",
}
REVENUE_SUBJECT_EXCLUDE_KEYWORDS = (
    "注意交易資訊",
    "澄清",
    "更正",
    "會計師調整",
)
TAIWAN_TZ = ZoneInfo("Asia/Taipei")


class ThrottledSession:
    """Small requests wrapper that sleeps between HTTP calls."""

    def __init__(self, request_interval_seconds: float, timeout_seconds: int = 25) -> None:
        self.request_interval_seconds = request_interval_seconds
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": REQUEST_USER_AGENT,
                "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
            }
        )
        self._last_request_at = 0.0

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        elapsed = time.monotonic() - self._last_request_at
        wait_seconds = self.request_interval_seconds - elapsed
        if self._last_request_at and wait_seconds > 0:
            time.sleep(wait_seconds)

        started_at = time.monotonic()
        response = self.session.request(
            method,
            url,
            timeout=self.timeout_seconds,
            **kwargs,
        )
        response.elapsed_seconds_for_probe = time.monotonic() - started_at  # type: ignore[attr-defined]
        self._last_request_at = time.monotonic()
        response.raise_for_status()
        return response


def now_text() -> str:
    return datetime.now(TAIWAN_TZ).isoformat(timespec="seconds")


def roc_year_for(value: date) -> int:
    return value.year - 1911


def previous_month_parts(reference_date: date) -> tuple[int, int]:
    year = reference_date.year
    month = reference_date.month - 1
    if month == 0:
        year -= 1
        month = 12
    return roc_year_for(date(year, month, 1)), month


def decode_csv_content(content: bytes) -> str:
    if content.startswith(b"\xef\xbb\xbf"):
        return content.decode("utf-8-sig")
    for encoding in ("utf-8", "big5", "cp950"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def summarize_count_by(records: list[dict[str, Any]], key: str) -> dict[str, int]:
    return dict(sorted(Counter(str(record.get(key, "")) for record in records).items()))


def compact_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "spoke_date": record.get("spoke_date", ""),
        "spoke_time": record.get("spoke_time", ""),
        "company_id": record.get("company_id", ""),
        "company_name": record.get("company_name", ""),
        "subject": record.get("subject", ""),
    }


def material_info_text(record: dict[str, Any]) -> str:
    detail = record.get("detail") or record.get("detail_preview") or {}
    fields = detail.get("fields") or {}
    parts = [
        str(record.get("subject", "")),
        str(detail.get("description", "")),
        *[str(value) for value in fields.values()],
    ]
    return "\n".join(parts)


def subject_has_revenue_keyword(record: dict[str, Any]) -> bool:
    return "營收" in str(record.get("subject", ""))


def subject_has_monthly_revenue_keyword(record: dict[str, Any]) -> bool:
    return "月營收" in str(record.get("subject", ""))


def looks_like_revenue_announcement(record: dict[str, Any]) -> bool:
    subject = str(record.get("subject", ""))
    if "營收" not in subject:
        return False
    return not any(keyword in subject for keyword in REVENUE_SUBJECT_EXCLUDE_KEYWORDS)


def records_in_window(
    records: list[dict[str, Any]],
    start_date: str,
    end_date: str,
) -> list[dict[str, Any]]:
    return [
        record
        for record in records
        if start_date <= str(record.get("spoke_date", "")) <= end_date
    ]


def load_or_fetch_material_info_records(args: argparse.Namespace) -> tuple[list[dict[str, Any]], str]:
    cache_path = Path(args.material_info_cache) if args.material_info_cache else None
    if cache_path and cache_path.exists() and not args.live_material_info:
        return json.loads(cache_path.read_text(encoding="utf-8")), f"cache:{cache_path}"

    crawler = MopsCrawler(request_interval_seconds=args.request_interval)
    records = crawler.fetch_date_range_records(
        start_date=args.start_date,
        end_date=args.end_date,
        max_items_per_day=args.material_info_max_items_per_day,
        market=args.material_info_market,
        include_details=False,
        day_interval_seconds=args.day_interval,
    )
    return records, "live:mops_previous_day_range"


def probe_material_info_keywords(args: argparse.Namespace) -> dict[str, Any]:
    records, source = load_or_fetch_material_info_records(args)
    window_records = records_in_window(records, args.start_date, args.end_date)
    monthly_keyword_records = [
        record for record in window_records if "月營收" in material_info_text(record)
    ]
    revenue_keyword_records = [
        record for record in window_records if "營收" in material_info_text(record)
    ]
    subject_monthly_keyword_records = [
        record for record in window_records if subject_has_monthly_revenue_keyword(record)
    ]
    subject_revenue_keyword_records = [
        record for record in window_records if subject_has_revenue_keyword(record)
    ]
    likely_revenue_records = [
        record for record in window_records if looks_like_revenue_announcement(record)
    ]

    likely_revenue_records = sort_records_by_spoke_time(dedupe_records(likely_revenue_records))
    first_likely = (
        min(likely_revenue_records, key=lambda record: (record.get("spoke_date", ""), record.get("spoke_time", "")))
        if likely_revenue_records
        else None
    )

    return {
        "source": source,
        "window": {"start_date": args.start_date, "end_date": args.end_date},
        "total_window_records": len(window_records),
        "haystack_monthly_revenue_keyword_count": len(monthly_keyword_records),
        "haystack_revenue_keyword_count": len(revenue_keyword_records),
        "subject_monthly_revenue_keyword_count": len(subject_monthly_keyword_records),
        "subject_revenue_keyword_count": len(subject_revenue_keyword_records),
        "likely_revenue_announcement_count": len(likely_revenue_records),
        "likely_unique_company_count": len(
            {str(record.get("company_id", "")) for record in likely_revenue_records}
        ),
        "likely_by_spoke_date": summarize_count_by(likely_revenue_records, "spoke_date"),
        "first_likely_revenue_announcement": compact_record(first_likely) if first_likely else None,
        "subject_monthly_revenue_samples": [
            compact_record(record)
            for record in sort_records_by_spoke_time(subject_monthly_keyword_records)[:20]
        ],
        "likely_revenue_samples": [
            compact_record(record) for record in likely_revenue_records[:50]
        ],
        "notes": [
            "haystack counts search subject/detail/fields and can include attention-trading or clarification records.",
            "likely_revenue_announcement_count keeps subject revenue records but excludes common non-announcement subjects.",
        ],
    }


def normalize_openapi_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "report_date": row.get("出表日期", ""),
        "data_month": row.get("資料年月", ""),
        "company_id": row.get("公司代號", ""),
        "company_name": row.get("公司名稱", ""),
        "industry": row.get("產業別", ""),
        "monthly_revenue": row.get("營業收入-當月營收", ""),
        "previous_month_revenue": row.get("營業收入-上月營收", ""),
        "last_year_month_revenue": row.get("營業收入-去年當月營收", ""),
        "mom_percent": row.get("營業收入-上月比較增減(%)", ""),
        "yoy_percent": row.get("營業收入-去年同月增減(%)", ""),
        "ytd_revenue": row.get("累計營業收入-當月累計營收", ""),
        "last_year_ytd_revenue": row.get("累計營業收入-去年累計營收", ""),
        "ytd_yoy_percent": row.get("累計營業收入-前期比較增減(%)", ""),
        "note": row.get("備註", ""),
    }


def probe_twse_listed_openapi(session: ThrottledSession) -> dict[str, Any]:
    fetched_at = now_text()
    response = session.request("GET", TWSE_LISTED_MONTHLY_REVENUE_OPENAPI_URL)
    rows = response.json()
    normalized_rows = [normalize_openapi_row(row) for row in rows]
    return {
        "url": TWSE_LISTED_MONTHLY_REVENUE_OPENAPI_URL,
        "fetched_at": fetched_at,
        "http_status": response.status_code,
        "elapsed_seconds": round(response.elapsed_seconds_for_probe, 3),  # type: ignore[attr-defined]
        "row_count": len(normalized_rows),
        "unique_company_count": len({row["company_id"] for row in normalized_rows}),
        "report_dates": summarize_count_by(normalized_rows, "report_date"),
        "data_months": summarize_count_by(normalized_rows, "data_month"),
        "sample_rows": normalized_rows[:10],
        "notes": [
            "This TWSE OpenAPI endpoint covers listed companies only.",
            "The user-provided t187ap15_L endpoint is not monthly revenue; t187ap05_L is monthly revenue.",
        ],
    }


def extract_popup_path(html: str) -> str | None:
    match = re.search(r"window\.open\('([^']+)'", html)
    if match:
        return match.group(1)
    match = re.search(r"window\.open\(\"([^\"]+)\"", html)
    if match:
        return match.group(1)
    return None


def parse_mops_monthly_revenue_csv(
    content: bytes,
    market: str,
) -> list[dict[str, Any]]:
    text = decode_csv_content(content)
    reader = csv.DictReader(io.StringIO(text))
    rows: list[dict[str, Any]] = []
    for row in reader:
        rows.append(
            {
                "market": market,
                "market_label": MARKET_LABELS.get(market, market),
                "report_date": row.get("出表日期", ""),
                "data_month": row.get("資料年月", ""),
                "company_id": row.get("公司代號", ""),
                "company_name": row.get("公司名稱", ""),
                "industry": row.get("產業別", ""),
                "monthly_revenue": row.get("營業收入-當月營收", ""),
                "previous_month_revenue": row.get("營業收入-上月營收", ""),
                "last_year_month_revenue": row.get("營業收入-去年當月營收", ""),
                "mom_percent": row.get("營業收入-上月比較增減(%)", ""),
                "yoy_percent": row.get("營業收入-去年同月增減(%)", ""),
                "ytd_revenue": row.get("累計營業收入-當月累計營收", ""),
                "last_year_ytd_revenue": row.get("累計營業收入-去年累計營收", ""),
                "ytd_yoy_percent": row.get("累計營業收入-前期比較增減(%)", ""),
                "note": row.get("備註", ""),
            }
        )
    return rows


def fetch_mops_monthly_revenue_market(
    session: ThrottledSession,
    market: str,
    roc_year: int,
    month: int,
) -> dict[str, Any]:
    fetched_at = now_text()
    ajax_url = urljoin(MOPS_BASE_URL, MOPS_MONTHLY_REVENUE_AJAX_PATH)
    ajax_response = session.request(
        "POST",
        ajax_url,
        data={
            "encodeURIComponent": "1",
            "step": "1",
            "firstin": "1",
            "off": "1",
            "TYPEK": market,
            "year": str(roc_year),
            "month": f"{month:02d}",
        },
    )
    ajax_response.encoding = "utf-8"
    popup_path = extract_popup_path(ajax_response.text)

    file_name = f"t21sc03_{roc_year}_{month}.csv"
    csv_response = session.request(
        "POST",
        urljoin(MOPS_BASE_URL, MOPS_DOWNLOAD_PATH),
        data={
            "step": "9",
            "functionName": "show_file2",
            "filePath": f"/t21/{market}/",
            "fileName": file_name,
        },
    )
    rows = parse_mops_monthly_revenue_csv(csv_response.content, market=market)
    return {
        "market": market,
        "market_label": MARKET_LABELS.get(market, market),
        "query": {"roc_year": roc_year, "month": month},
        "fetched_at": fetched_at,
        "ajax_url": ajax_url,
        "popup_path": popup_path,
        "csv_file_name": file_name,
        "ajax_elapsed_seconds": round(ajax_response.elapsed_seconds_for_probe, 3),  # type: ignore[attr-defined]
        "csv_elapsed_seconds": round(csv_response.elapsed_seconds_for_probe, 3),  # type: ignore[attr-defined]
        "row_count": len(rows),
        "unique_company_count": len({row["company_id"] for row in rows}),
        "report_dates": summarize_count_by(rows, "report_date"),
        "data_months": summarize_count_by(rows, "data_month"),
        "sample_rows": rows[:10],
    }


def probe_mops_monthly_summary(args: argparse.Namespace, session: ThrottledSession) -> dict[str, Any]:
    markets = [market.strip() for market in args.mops_markets.split(",") if market.strip()]
    markets_result = [
        fetch_mops_monthly_revenue_market(
            session=session,
            market=market,
            roc_year=args.roc_year,
            month=args.month,
        )
        for market in markets
    ]
    return {
        "base_url": MOPS_BASE_URL,
        "markets": markets_result,
        "total_row_count": sum(market["row_count"] for market in markets_result),
        "total_unique_company_count": sum(
            market["unique_company_count"] for market in markets_result
        ),
        "notes": [
            "The CSV route is the official download form exposed by the t21sc04_ifrs result page.",
            "This summary source has company revenue values but not company-level announcement timestamps.",
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Probe monthly-revenue data sources")
    parser.add_argument("--start-date", default="2026-06-01")
    parser.add_argument("--end-date", default="2026-06-10")
    parser.add_argument("--roc-year", type=int)
    parser.add_argument("--month", type=int)
    parser.add_argument("--mops-markets", default="sii,otc")
    parser.add_argument("--request-interval", type=float, default=2.0)
    parser.add_argument("--day-interval", type=float, default=5.0)
    parser.add_argument("--material-info-market", default="all")
    parser.add_argument("--material-info-max-items-per-day", type=int, default=0)
    parser.add_argument("--material-info-cache", type=Path, default=DEFAULT_MATERIAL_INFO_CACHE)
    parser.add_argument(
        "--live-material-info",
        action="store_true",
        help="Fetch MOPS material information live instead of reading the local range cache.",
    )
    parser.add_argument("--skip-material-info", action="store_true")
    parser.add_argument("--skip-openapi", action="store_true")
    parser.add_argument("--skip-mops-summary", action="store_true")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.roc_year is None or args.month is None:
        args.roc_year, args.month = previous_month_parts(datetime.now(TAIWAN_TZ).date())

    session = ThrottledSession(request_interval_seconds=args.request_interval)
    result: dict[str, Any] = {
        "probe_started_at": now_text(),
        "request_interval_seconds": args.request_interval,
        "day_interval_seconds": args.day_interval,
        "source_observability": {
            "material_info": "Has company-level spoke_date/spoke_time and is the best source for first-announcement timing.",
            "mops_t21sc04_ifrs": "Can fetch all listed/OTC rows for a data month, but the CSV does not include company-level first-published timestamps.",
            "twse_openapi_t187ap05_L": "Can fetch all listed rows, but not OTC rows and not company-level first-published timestamps.",
        },
    }

    if not args.skip_material_info:
        result["material_info_keyword_probe"] = probe_material_info_keywords(args)
    if not args.skip_openapi:
        result["twse_listed_openapi_probe"] = probe_twse_listed_openapi(session)
    if not args.skip_mops_summary:
        result["mops_monthly_summary_probe"] = probe_mops_monthly_summary(args, session)

    result["probe_finished_at"] = now_text()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"Saved probe report to {args.output}")


if __name__ == "__main__":
    main()
