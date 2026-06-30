"""CLI and demo page for the TWSE material-information dashboard."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import time
from collections import Counter
from datetime import date, datetime, time as datetime_time, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlencode, urlparse
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

from financial_report_crawler import (
    build_financial_report_record,
    dedupe_financial_report_records,
)
from mops_crawler import (
    CATEGORY_CHOICES,
    CATEGORY_ALL,
    CATEGORY_FINANCIAL_SELF_REPORT,
    DEFAULT_DAY_INTERVAL_SECONDS,
    DEFAULT_REFRESH_SECONDS,
    DEFAULT_REQUEST_INTERVAL_SECONDS,
    MopsCrawler,
    default_month_start,
    default_previous_day,
    dedupe_records,
    enrich_records_for_cache,
    filter_records_by_company_id,
    filter_records_by_category,
    filter_records_for_recent_financial,
    filter_records_by_recent_days,
    get_or_extract_eps_metrics,
    normalize_time,
    save_records,
    sort_records_by_spoke_time,
)
from monthly_revenue_crawler import (
    MonthlyRevenueCrawler,
    append_new_monthly_revenue_records,
    data_month_parts,
    dedupe_event_records,
    filter_monthly_records_by_company_id,
    normalize_data_month,
    normalize_company_ids,
    previous_month_parts,
    sort_event_records,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT_ENV = "TWSE_DASHBOARD_DATA_ROOT"
SEED_CACHE_ON_START_ENV = "TWSE_DASHBOARD_SEED_CACHE_ON_START"
DEFAULT_DATA_ROOT = Path("/data")
BUNDLED_RAW_DATA_DIR = PROJECT_ROOT / "data" / "raw"
DEFAULT_RAW_DATA_DIR = DEFAULT_DATA_ROOT / "raw"
DEFAULT_OUTPUT_PATH = DEFAULT_RAW_DATA_DIR / "latest_material_info.json"
DEFAULT_PREVIOUS_OUTPUT_PATH = DEFAULT_RAW_DATA_DIR / "previous_material_info.json"
DEFAULT_RANGE_OUTPUT_PATH = DEFAULT_RAW_DATA_DIR / "material_info_range.json"
DEFAULT_RANGE_META_PATH = DEFAULT_RAW_DATA_DIR / "material_info_range_meta.json"
DEFAULT_MONTHLY_REVENUE_OUTPUT_PATH = DEFAULT_RAW_DATA_DIR / "monthly_revenue_latest.json"
DEFAULT_MONTHLY_REVENUE_META_PATH = DEFAULT_RAW_DATA_DIR / "monthly_revenue_latest_meta.json"
DEFAULT_FINANCIAL_REPORT_OUTPUT_PATH = DEFAULT_RAW_DATA_DIR / "financial_report_latest.json"
DEFAULT_FINANCIAL_REPORT_META_PATH = DEFAULT_RAW_DATA_DIR / "financial_report_latest_meta.json"
DEFAULT_RECENT_DAYS = 7
DEFAULT_FINANCIAL_REPORT_LOOKBACK_DAYS = 3
MODE_LATEST = "latest"
MODE_PREVIOUS = "previous"
MODE_RECENT_FINANCIAL = "recent-financial"
MODE_CHOICES = (MODE_LATEST, MODE_PREVIOUS, MODE_RECENT_FINANCIAL)
TAB_MATERIAL_INFO = "material-info"
TAB_MONTHLY_REVENUE = "monthly-revenue"
TAB_FINANCIAL_REPORT = "financial-report"
TAB_CHOICES = (TAB_MATERIAL_INFO, TAB_MONTHLY_REVENUE, TAB_FINANCIAL_REPORT)
MONTHLY_REVENUE_SUMMARY_CACHE_KEY = f"{TAB_MONTHLY_REVENUE}:summary"
LISTED_OTC_MARKETS = {"sii", "otc"}
MONTHLY_REVENUE_PRIMARY_SOURCE_TYPES = {"mops_monthly_revenue_summary"}
MONTHLY_REVENUE_FALLBACK_SOURCE_TYPES = {
    "twse_openapi_monthly_revenue",
    "tpex_openapi_monthly_revenue",
}
MARKET_CLOSE_TIME = datetime_time(13, 30)
MARKET_CLOSE_TIME_TEXT = "13:30:00"
UPDATE_API_PATH = "/api/admin/update"
MONTHLY_REVENUE_UPDATE_API_PATH = "/api/admin/update-monthly-revenue"
FINANCIAL_REPORT_UPDATE_API_PATH = "/api/admin/update-financial-report"
UPDATE_TOKEN_ENV = "TWSE_DASHBOARD_UPDATE_TOKEN"
DEV_ALLOW_UNPROTECTED_UPDATE_ENV = "TWSE_DASHBOARD_DEV_ALLOW_UNPROTECTED_UPDATE"
RANGE_CACHE_FILE_ENV = "TWSE_DASHBOARD_RANGE_CACHE_FILE"
RECENT_DAYS_ENV = "TWSE_DASHBOARD_RECENT_DAYS"
UPDATE_MIN_INTERVAL_ENV = "TWSE_DASHBOARD_UPDATE_MIN_INTERVAL"
MONTHLY_REVENUE_COMPANY_IDS_ENV = "TWSE_DASHBOARD_MONTHLY_REVENUE_COMPANY_IDS"
MONTHLY_REVENUE_CACHE_FILE_ENV = "TWSE_DASHBOARD_MONTHLY_REVENUE_CACHE_FILE"
FINANCIAL_REPORT_CACHE_FILE_ENV = "TWSE_DASHBOARD_FINANCIAL_REPORT_CACHE_FILE"
FINANCIAL_REPORT_TARGET_QUARTER_ENV = "TWSE_DASHBOARD_FINANCIAL_REPORT_TARGET_QUARTER"
FINANCIAL_REPORT_LOOKBACK_DAYS_ENV = "TWSE_DASHBOARD_FINANCIAL_REPORT_LOOKBACK_DAYS"
FINMIND_TOKEN_ENV_NAMES = ("FINMIND_TOKEN", "FINMIND_API_TOKEN")
FINMIND_DATA_API_URL = "https://api.finmindtrade.com/api/v4/data"
FINMIND_TRADING_DATE_DATASET = "TaiwanStockTradingDate"
TRADING_DATE_LOOKBACK_DAYS = 21
TRADING_DATE_CACHE_TTL_SECONDS = 3600
TAIWAN_TZ = ZoneInfo("Asia/Taipei")
DEFAULT_UPDATE_MIN_INTERVAL_SECONDS = 300
TRUE_ENV_VALUES = {"1", "true", "yes", "on"}
_TRADING_DATE_CACHE: dict[str, Any] = {"key": "", "fetched_at": 0.0, "dates": []}


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().casefold() in TRUE_ENV_VALUES


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError:
        return default


def env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return None
    return Path(value)


def dashboard_data_root() -> Path:
    return env_path(DATA_ROOT_ENV) or DEFAULT_DATA_ROOT


def dashboard_raw_data_dir() -> Path:
    return dashboard_data_root() / "raw"


def dashboard_cache_path(filename: str) -> Path:
    return dashboard_raw_data_dir() / filename


def default_output_path() -> Path:
    return dashboard_cache_path("latest_material_info.json")


def default_previous_output_path() -> Path:
    return dashboard_cache_path("previous_material_info.json")


def default_range_output_path() -> Path:
    return dashboard_cache_path("material_info_range.json")


def default_range_meta_path() -> Path:
    return dashboard_cache_path("material_info_range_meta.json")


def default_monthly_revenue_output_path() -> Path:
    return dashboard_cache_path("monthly_revenue_latest.json")


def default_monthly_revenue_meta_path() -> Path:
    return dashboard_cache_path("monthly_revenue_latest_meta.json")


def default_financial_report_output_path() -> Path:
    return dashboard_cache_path("financial_report_latest.json")


def default_financial_report_meta_path() -> Path:
    return dashboard_cache_path("financial_report_latest_meta.json")


def env_first(names: tuple[str, ...]) -> str:
    for name in names:
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return ""


def taiwan_now() -> datetime:
    return datetime.now(TAIWAN_TZ)


def is_local_client(client_host: str) -> bool:
    return client_host in {"127.0.0.1", "::1", "localhost"}


def is_update_request_authorized(
    headers: Mapping[str, str],
    query: dict[str, str],
    client_host: str,
    allow_unprotected_local_update: bool = False,
) -> bool:
    expected_token = os.environ.get(UPDATE_TOKEN_ENV, "").strip()
    if not expected_token:
        return allow_unprotected_local_update and is_local_client(client_host)

    auth_header = headers.get("Authorization", "").strip()
    header_token = headers.get("X-Update-Token", "").strip()
    query_token = query.get("token", "").strip()
    bearer_token = ""
    if auth_header.casefold().startswith("bearer "):
        bearer_token = auth_header[7:].strip()

    return expected_token in {bearer_token, header_token, query_token}


def render_empty_state(message: str = "目前沒有抓到重大訊息。") -> str:
    return f"<p class=\"empty\">{html.escape(message)}</p>"


def render_metric(value: Any) -> str:
    text = "" if value is None else str(value)
    return html.escape(text if text else "-")


def metric_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", "").replace("%", "").strip())
    except ValueError:
        return None


def format_number(value: float, digits: int = 1) -> str:
    return f"{value:,.{digits}f}"


def render_money_millions(value: Any) -> str:
    amount = metric_float(value)
    if amount is None:
        return '<span class="muted-value">-</span>'
    return f'<span class="money-value">{html.escape(format_number(amount / 1000, 1))}</span>'


def render_percent_value(value: Any) -> str:
    percent = metric_float(value)
    if percent is None:
        return '<span class="muted-value">-</span>'
    class_name = "finance-up" if percent > 0 else "finance-down" if percent < 0 else "muted-value"
    return f'<span class="{class_name}">{percent:.2f}%</span>'


def sort_value_attr(value: Any) -> str:
    text = "" if value is None else str(value)
    return f' data-sort-value="{html.escape(text, quote=True)}"'


def metric_sort_value(value: Any) -> str:
    number = metric_float(value)
    return "" if number is None else f"{number:.8f}"


def sortable_header(label: str, sort_type: str = "text") -> str:
    return (
        '<th scope="col" aria-sort="none">'
        f'<button class="sort-button" type="button" data-sort-type="{html.escape(sort_type, quote=True)}">'
        f'<span>{html.escape(label)}</span><span class="sort-indicator" aria-hidden="true"></span>'
        "</button>"
        "</th>"
    )


def render_sortable_headers(columns: list[tuple[str, str]]) -> str:
    return "\n".join(sortable_header(label, sort_type) for label, sort_type in columns)


def record_text(record: dict[str, Any]) -> str:
    detail = record.get("detail") or record.get("detail_preview") or {}
    pieces = [
        record.get("event_type", ""),
        record.get("source_label", ""),
        record.get("title", ""),
        record.get("subject", ""),
        detail.get("description", ""),
    ]
    return " ".join(str(piece) for piece in pieces if piece)


def is_financial_report_record(record: dict[str, Any]) -> bool:
    text = record_text(record)
    return (
        str(record.get("event_type", "")) == "financial_report"
        or str(record.get("source_label", "")) in {"財務報告", "財務訊號"}
        or "財務報告" in text
    )


def is_monthly_revenue_record(record: dict[str, Any]) -> bool:
    if is_financial_report_record(record):
        return False
    event_type = str(record.get("event_type", ""))
    if event_type in {"monthly_revenue", "material_revenue"}:
        return True
    return "營收" in record_text(record)


def monthly_revenue_identity_key(record: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(record.get("market", "")),
        str(record.get("company_id", "")),
        normalize_data_month(record.get("data_month", "")),
    )


def filter_monthly_revenue_fallback_duplicates(
    existing_records: list[dict[str, Any]],
    latest_records: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    existing_primary_keys = {
        monthly_revenue_identity_key(record)
        for record in existing_records
        if record.get("source_type") in MONTHLY_REVENUE_PRIMARY_SOURCE_TYPES
    }
    filtered_records: list[dict[str, Any]] = []
    skipped_count = 0
    for record in latest_records:
        if (
            record.get("source_type") in MONTHLY_REVENUE_FALLBACK_SOURCE_TYPES
            and monthly_revenue_identity_key(record) in existing_primary_keys
        ):
            skipped_count += 1
            continue
        filtered_records.append(record)
    return filtered_records, skipped_count


def eps_delta_value(metrics: dict[str, Any]) -> float | None:
    current = metric_float(metrics.get("month_eps"))
    previous = metric_float(metrics.get("last_year_month_eps"))
    if current is None or previous is None:
        return None
    return current - previous


def render_eps_delta(metrics: dict[str, Any]) -> str:
    delta = eps_delta_value(metrics)
    if delta is None:
        return "<span class=\"muted-value\">-</span>"

    class_name = "positive" if delta > 0 else "negative" if delta < 0 else "muted-value"
    return f"<span class=\"{class_name}\">{delta:+.2f}</span>"


def format_table_time(record: dict[str, Any]) -> str:
    date_text = str(record.get("spoke_date", ""))
    time_text = str(record.get("spoke_time", ""))
    date_part = date_text
    if len(date_text) >= 10:
        try:
            date_part = f"{int(date_text[5:7])}/{int(date_text[8:10])}"
        except ValueError:
            date_part = date_text
    return f"{date_part} {time_text[:5]}".strip()


def parse_event_datetime(record: dict[str, Any]) -> datetime | None:
    spoke_date = str(record.get("spoke_date", "")).strip()
    if spoke_date:
        spoke_time = normalize_time(str(record.get("spoke_time", ""))) or "00:00:00"
        try:
            return datetime.fromisoformat(f"{spoke_date}T{spoke_time}")
        except ValueError:
            pass

    for key in ("announced_at", "event_time", "detected_at", "fetched_at"):
        value = str(record.get(key, "")).strip()
        if not value:
            continue
        normalized = value.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(normalized)
        except ValueError:
            continue
    return None


def format_event_table_time(record: dict[str, Any]) -> str:
    event_at = parse_event_datetime(record)
    if event_at is not None:
        return f"{event_at.month:02d}-{event_at.day:02d} {event_at.hour:02d}:{event_at.minute:02d}"
    return display_event_time(record)


def format_event_table_time_with_seconds(record: dict[str, Any]) -> str:
    event_at = parse_event_datetime(record)
    if event_at is not None:
        return (
            f"{event_at.month:02d}-{event_at.day:02d} "
            f"{event_at.hour:02d}:{event_at.minute:02d}:{event_at.second:02d}"
        )
    return display_event_time(record)


def event_sort_value(record: dict[str, Any]) -> str:
    event_at = parse_event_datetime(record)
    if event_at is None:
        return ""
    if event_at.tzinfo is not None:
        event_at = event_at.astimezone(TAIWAN_TZ)
    return event_at.strftime("%Y%m%d%H%M%S")


def latest_event_record(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    candidates = [
        (sort_value, record)
        for record in records
        if (sort_value := event_sort_value(record))
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def taiwan_now_iso() -> str:
    return datetime.now(TAIWAN_TZ).isoformat(timespec="seconds")


def event_time_iso(record: dict[str, Any]) -> str:
    event_at = parse_event_datetime(record)
    if event_at is None:
        return ""
    if event_at.tzinfo is None:
        event_at = event_at.replace(tzinfo=TAIWAN_TZ)
    else:
        event_at = event_at.astimezone(TAIWAN_TZ)
    return event_at.isoformat(timespec="seconds")


def newest_event_time_iso(records: list[dict[str, Any]]) -> str:
    latest_record = latest_event_record(records)
    return event_time_iso(latest_record) if latest_record is not None else ""


def format_meta_time(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(TAIWAN_TZ)
    return parsed.strftime("%m-%d %H:%M:%S")


def monthly_revenue_period_key(record: dict[str, Any]) -> tuple[int, int] | None:
    year, month = data_month_parts(record.get("data_month"))
    if year is None or month is None:
        return None
    return year, month


def format_monthly_revenue_period(value: Any) -> str:
    year, month = data_month_parts(value)
    if year is None or month is None:
        return "-"
    return f"{year + 1911}/{month:02d}"


def default_financial_report_target_quarter(today: date | None = None) -> str:
    current = today or taiwan_now().date()
    current_quarter = (current.month - 1) // 3 + 1
    year = current.year
    quarter = current_quarter - 1
    if quarter == 0:
        year -= 1
        quarter = 4
    return f"{year}Q{quarter}"


def iso_date_to_roc_label(value: date) -> str:
    return f"{value.year - 1911:03d}/{value.month:02d}/{value.day:02d}"


def financial_report_quarter_key(value: Any) -> tuple[int, int] | None:
    text = str(value or "").strip().upper()
    match = re.fullmatch(r"(\d{4})Q([1-4])", text)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def financial_report_record_quarter_key(record: dict[str, Any]) -> tuple[int, int] | None:
    return financial_report_quarter_key(record.get("quarter", ""))


def filter_latest_monthly_revenue_period(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keyed_records = [
        (period_key, record)
        for record in records
        if (period_key := monthly_revenue_period_key(record)) is not None
    ]
    if not keyed_records:
        return []
    latest_period = max(period_key for period_key, _ in keyed_records)
    return [record for period_key, record in keyed_records if period_key == latest_period]


def dedupe_latest_monthly_revenue_by_company(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for record in sort_event_records(records):
        key = monthly_revenue_identity_key(record)
        if key in seen:
            continue
        seen.add(key)
        selected.append(record)
    return selected


def select_display_monthly_revenue_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest_period_records = filter_latest_monthly_revenue_period(records)
    return dedupe_latest_monthly_revenue_by_company(latest_period_records)


def filter_latest_financial_report_quarter(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keyed_records = [
        (quarter_key, record)
        for record in records
        if (quarter_key := financial_report_record_quarter_key(record)) is not None
    ]
    if not keyed_records:
        return []
    latest_quarter = max(quarter_key for quarter_key, _ in keyed_records)
    return [record for quarter_key, record in keyed_records if quarter_key == latest_quarter]


def filter_display_financial_report_quarter(
    records: list[dict[str, Any]],
    target_quarter: str | None = None,
) -> list[dict[str, Any]]:
    keyed_records = [
        (quarter_key, record)
        for record in records
        if (quarter_key := financial_report_record_quarter_key(record)) is not None
    ]
    if not keyed_records:
        return []

    target_key = financial_report_quarter_key(target_quarter)
    if target_key is not None:
        available_keys = {quarter_key for quarter_key, _ in keyed_records}
        display_key = target_key if target_key in available_keys else None
        if display_key is None:
            fallback_keys = [quarter_key for quarter_key, _ in keyed_records if quarter_key <= target_key]
            display_key = max(fallback_keys) if fallback_keys else max(available_keys)
    else:
        display_key = max(quarter_key for quarter_key, _ in keyed_records)

    return [record for quarter_key, record in keyed_records if quarter_key == display_key]


def select_display_financial_report_records(
    records: list[dict[str, Any]],
    target_quarter: str | None = None,
) -> list[dict[str, Any]]:
    display_quarter_records = filter_display_financial_report_quarter(records, target_quarter)
    return dedupe_financial_report_records(sort_event_records(display_quarter_records))


def financial_report_company_count(records: list[dict[str, Any]]) -> int:
    company_ids = {str(record.get("company_id", "")).strip() for record in records}
    return len([company_id for company_id in company_ids if company_id])


def monthly_revenue_company_count(records: list[dict[str, Any]]) -> int:
    company_ids = {str(record.get("company_id", "")).strip() for record in records}
    return len([company_id for company_id in company_ids if company_id])


def latest_monthly_revenue_detected_record(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    candidates: list[tuple[float, dict[str, Any]]] = []
    for record in records:
        event_at = parse_event_datetime(
            {
                "detected_at": record.get("detected_at", ""),
                "event_time": record.get("event_time", ""),
                "announced_at": record.get("announced_at", ""),
            }
        )
        if event_at is None:
            continue
        candidates.append((event_at.timestamp(), record))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def newest_monthly_revenue_detected_at_iso(records: list[dict[str, Any]]) -> str:
    candidates: list[tuple[float, datetime]] = []
    for record in records:
        detected_at = str(record.get("detected_at", "")).strip()
        if not detected_at:
            continue
        try:
            parsed = datetime.fromisoformat(detected_at.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=TAIWAN_TZ)
        else:
            parsed = parsed.astimezone(TAIWAN_TZ)
        candidates.append((parsed.timestamp(), parsed))
    if not candidates:
        return ""
    return max(candidates, key=lambda item: item[0])[1].isoformat(timespec="seconds")


def newest_detected_at_iso(records: list[dict[str, Any]]) -> str:
    candidates: list[tuple[float, datetime]] = []
    for record in records:
        detected_at = str(record.get("detected_at", "")).strip()
        if not detected_at:
            continue
        try:
            parsed = datetime.fromisoformat(detected_at.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=TAIWAN_TZ)
        else:
            parsed = parsed.astimezone(TAIWAN_TZ)
        candidates.append((parsed.timestamp(), parsed))
    if not candidates:
        return ""
    return max(candidates, key=lambda item: item[0])[1].isoformat(timespec="seconds")


def render_monthly_revenue_panel_subtitle(records: list[dict[str, Any]]) -> str:
    period = format_monthly_revenue_period(records[0].get("data_month")) if records else "-"
    latest_record = latest_monthly_revenue_detected_record(records)
    latest_text = (
        format_event_table_time_with_seconds(latest_record)
        if latest_record is not None
        else "-"
    )
    return f"""
    <div class="panel-subtitle monthly-summary-meta">
      <div>營收期間：{html.escape(period)} | 已申報 {monthly_revenue_company_count(records)} 家</div>
      <div>最新申報：{html.escape(latest_text)} · 偵測中 ✓</div>
    </div>
    """


def render_material_info_panel_subtitle(records: list[dict[str, Any]], recent_days: int) -> str:
    latest_record = latest_event_record(records)
    latest_text = (
        format_event_table_time_with_seconds(latest_record)
        if latest_record is not None
        else "-"
    )
    return f"""
    <div class="panel-subtitle">
      <div>近 {recent_days} 日自結/注意交易財務資訊（{len(records)} 筆）</div>
      <div>最新公告：{html.escape(latest_text)}</div>
    </div>
    """


def format_month_day(value: date) -> str:
    return f"{value.month}/{value.day}"


def record_typek(record: dict[str, Any]) -> str:
    payload = record.get("detail_payload") or {}
    return str(payload.get("TYPEK") or "").casefold()


def filter_records_by_listing_market(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [record for record in records if record_typek(record) in LISTED_OTC_MARKETS]


def parse_record_date(record: dict[str, Any]) -> date | None:
    event_at = parse_event_datetime(record)
    return event_at.date() if event_at is not None else None


def finmind_api_token() -> str:
    return env_first(FINMIND_TOKEN_ENV_NAMES)


def parse_finmind_trading_date_row(row: Any) -> date | None:
    if not isinstance(row, dict):
        return None
    raw_date = row.get("date") or row.get("Date") or row.get("日期")
    if not raw_date:
        return None

    for key in ("is_trading_day", "isTradingDay", "trading_day", "is_open", "isOpen"):
        if key not in row:
            continue
        value = row.get(key)
        if isinstance(value, bool) and not value:
            return None
        if str(value).strip().casefold() in {"0", "false", "no", "n"}:
            return None

    try:
        return date.fromisoformat(str(raw_date)[:10])
    except ValueError:
        return None


def fetch_finmind_trading_dates(
    start_date: date,
    end_date: date,
    token: str | None = None,
) -> list[date]:
    auth_token = token if token is not None else finmind_api_token()
    if not auth_token:
        return []

    response = requests.get(
        FINMIND_DATA_API_URL,
        params={
            "dataset": FINMIND_TRADING_DATE_DATASET,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "token": auth_token,
        },
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json()
    rows = payload.get("data", []) if isinstance(payload, dict) else []
    return sorted(
        {
            parsed
            for row in rows
            if (parsed := parse_finmind_trading_date_row(row)) is not None
        }
    )


def get_recent_trading_dates(now: datetime | None = None) -> list[date]:
    token = finmind_api_token()
    if not token:
        return []

    current = now or taiwan_now()
    end_date = current.date()
    start_date = end_date - timedelta(days=TRADING_DATE_LOOKBACK_DAYS)
    cache_key = f"{start_date.isoformat()}:{end_date.isoformat()}:token"
    current_monotonic = time.monotonic()
    if (
        _TRADING_DATE_CACHE["key"] == cache_key
        and current_monotonic - float(_TRADING_DATE_CACHE["fetched_at"]) < TRADING_DATE_CACHE_TTL_SECONDS
    ):
        return list(_TRADING_DATE_CACHE["dates"])

    try:
        trading_dates = fetch_finmind_trading_dates(start_date, end_date, token=token)
    except Exception:
        trading_dates = []

    _TRADING_DATE_CACHE.update(
        {
            "key": cache_key,
            "fetched_at": current_monotonic,
            "dates": trading_dates,
        }
    )
    return list(trading_dates)


def previous_business_day(value: date) -> date:
    current = value - timedelta(days=1)
    while current.weekday() >= 5:
        current -= timedelta(days=1)
    return current


def last_completed_market_close_date(
    now: datetime | None = None,
    trading_dates: list[date] | None = None,
) -> date:
    current = now or taiwan_now()
    current_date = current.date()

    if trading_dates:
        valid_dates = sorted({trading_date for trading_date in trading_dates if trading_date <= current_date})
        if current_date in valid_dates and current.time() >= MARKET_CLOSE_TIME:
            return current_date
        past_dates = [trading_date for trading_date in valid_dates if trading_date < current_date]
        if past_dates:
            return max(past_dates)

    if current_date.weekday() < 5 and current.time() >= MARKET_CLOSE_TIME:
        return current_date
    return previous_business_day(current_date)


def determine_cutoff_date(
    records: list[dict[str, Any]],
    now: datetime | None = None,
    trading_dates: list[date] | None = None,
) -> date:
    _ = records
    resolved_trading_dates = trading_dates
    if resolved_trading_dates is None:
        resolved_trading_dates = get_recent_trading_dates(now)
    return last_completed_market_close_date(now, trading_dates=resolved_trading_dates)


def is_market_unreacted_record(record: dict[str, Any], cutoff_date: date) -> bool:
    event_at = parse_event_datetime(record)
    if event_at is None:
        return False
    return (
        event_at.date() == cutoff_date
        and event_at.time().replace(tzinfo=None).strftime("%H:%M:%S") > MARKET_CLOSE_TIME_TEXT
    )


def split_records_by_market_reaction(
    records: list[dict[str, Any]],
    now: datetime | None = None,
    trading_dates: list[date] | None = None,
) -> tuple[date, list[dict[str, Any]], list[dict[str, Any]]]:
    cutoff_date = determine_cutoff_date(records, now=now, trading_dates=trading_dates)
    market_unreacted = [
        record
        for record in records
        if is_market_unreacted_record(record, cutoff_date)
    ]
    historical = [
        record
        for record in records
        if not is_market_unreacted_record(record, cutoff_date)
    ]
    return cutoff_date, market_unreacted, historical


def range_cache_meta_path(cache_file: Path | None = None) -> Path:
    cache_path = cache_file or default_range_output_path()
    if cache_path.name == default_range_output_path().name:
        return cache_path.with_name(default_range_meta_path().name)
    return cache_path.with_name(f"{cache_path.stem}_meta.json")


def load_range_cache_meta(cache_file: Path | None = None) -> dict[str, Any]:
    meta_path = range_cache_meta_path(cache_file)
    if not meta_path.exists():
        return {}
    try:
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def save_range_cache_meta(cache_file: Path, meta: Mapping[str, Any]) -> None:
    meta_path = range_cache_meta_path(cache_file)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = meta_path.with_name(f"{meta_path.name}.tmp")
    temp_path.write_text(
        json.dumps(dict(meta), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp_path.replace(meta_path)


def build_range_cache_meta(
    cache_file: Path,
    records: list[dict[str, Any]],
    **fields: Any,
) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "schema_version": 1,
        "cache_file": str(cache_file),
        "source_label": "MOPS 即時重大訊息 + 持久化快取",
        "record_count": len(records),
        "newest_spoke_at": newest_event_time_iso(records),
    }
    meta.update(fields)
    return meta


def update_range_cache_meta(
    cache_file: Path,
    records: list[dict[str, Any]] | None = None,
    **fields: Any,
) -> dict[str, Any]:
    meta = load_range_cache_meta(cache_file)
    if records is not None:
        meta.update(build_range_cache_meta(cache_file, records))
    else:
        meta.update({"schema_version": 1, "cache_file": str(cache_file)})
    meta.update(fields)
    save_range_cache_meta(cache_file, meta)
    return meta


def format_range_cache_source(cache_file: Path, records: list[dict[str, Any]]) -> str:
    meta = load_range_cache_meta(cache_file)
    source_label = str(meta.get("source_label") or "MOPS 即時重大訊息 + 持久化快取")
    latest_update = format_meta_time(meta.get("last_success_at") or meta.get("seeded_at"))
    newest_spoke_at = format_meta_time(meta.get("newest_spoke_at") or newest_event_time_iso(records))
    parts = [source_label]
    if latest_update:
        parts.append(f"最新更新：{latest_update}")
    if newest_spoke_at:
        parts.append(f"最新公告：{newest_spoke_at}")
    if meta.get("last_error"):
        parts.append("最近更新失敗")
    return " ｜ ".join(parts)


def monthly_revenue_cache_meta_path(cache_file: Path | None = None) -> Path:
    cache_path = cache_file or default_monthly_revenue_output_path()
    if cache_path.name == default_monthly_revenue_output_path().name:
        return cache_path.with_name(default_monthly_revenue_meta_path().name)
    return cache_path.with_name(f"{cache_path.stem}_meta.json")


def load_monthly_revenue_cache_meta(cache_file: Path | None = None) -> dict[str, Any]:
    meta_path = monthly_revenue_cache_meta_path(cache_file)
    if not meta_path.exists():
        return {}
    try:
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def save_monthly_revenue_cache_meta(cache_file: Path, meta: Mapping[str, Any]) -> None:
    meta_path = monthly_revenue_cache_meta_path(cache_file)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = meta_path.with_name(f"{meta_path.name}.tmp")
    temp_path.write_text(
        json.dumps(dict(meta), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp_path.replace(meta_path)


def build_monthly_revenue_cache_meta(
    cache_file: Path,
    records: list[dict[str, Any]],
    **fields: Any,
) -> dict[str, Any]:
    monthly_records = [record for record in records if is_monthly_revenue_record(record)]
    display_records = select_display_monthly_revenue_records(monthly_records)
    display_data_month_roc = (
        normalize_data_month(display_records[0].get("data_month", "")) if display_records else ""
    )
    display_data_month = (
        format_monthly_revenue_period(display_data_month_roc) if display_data_month_roc else ""
    )
    meta: dict[str, Any] = {
        "schema_version": 1,
        "cache_file": str(cache_file),
        "source_label": "MOPS 月營收彙總 + OpenAPI fallback + 持久化快取",
        "record_count": len(records),
        "monthly_revenue_record_count": len(monthly_records),
        "display_record_count": len(display_records),
        "display_data_month": display_data_month,
        "display_data_month_roc": display_data_month_roc,
        "newest_detected_at": newest_monthly_revenue_detected_at_iso(monthly_records),
        "newest_event_at": newest_event_time_iso(monthly_records),
    }
    meta.update(fields)
    return meta


def update_monthly_revenue_cache_meta(
    cache_file: Path,
    records: list[dict[str, Any]] | None = None,
    **fields: Any,
) -> dict[str, Any]:
    meta = load_monthly_revenue_cache_meta(cache_file)
    if records is not None:
        meta.update(build_monthly_revenue_cache_meta(cache_file, records))
    else:
        meta.update({"schema_version": 1, "cache_file": str(cache_file)})
    meta.update(fields)
    save_monthly_revenue_cache_meta(cache_file, meta)
    return meta


def format_monthly_revenue_cache_source(cache_file: Path, records: list[dict[str, Any]]) -> str:
    meta = load_monthly_revenue_cache_meta(cache_file)
    source_label = str(
        meta.get("source_label") or "MOPS 月營收彙總 + OpenAPI fallback + 持久化快取"
    )
    monthly_records = [record for record in records if is_monthly_revenue_record(record)]
    display_records = select_display_monthly_revenue_records(monthly_records)
    display_data_month = str(meta.get("display_data_month") or "").strip()
    if not display_data_month and display_records:
        display_data_month = format_monthly_revenue_period(display_records[0].get("data_month"))
    latest_update = format_meta_time(meta.get("last_success_at") or meta.get("seeded_at"))
    newest_detected_at = format_meta_time(
        meta.get("newest_detected_at") or newest_monthly_revenue_detected_at_iso(monthly_records)
    )
    parts = [source_label]
    if display_data_month:
        parts.append(f"營收月份：{display_data_month}")
    if latest_update:
        parts.append(f"最新更新：{latest_update}")
    if newest_detected_at:
        parts.append(f"最新偵測：{newest_detected_at}")
    if meta.get("market_failure_count"):
        parts.append("部分市場沿用既有 cache")
    if meta.get("last_error"):
        parts.append("最近更新失敗")
    return " ｜ ".join(parts)


def financial_report_cache_meta_path(cache_file: Path | None = None) -> Path:
    cache_path = cache_file or default_financial_report_output_path()
    if cache_path.name == default_financial_report_output_path().name:
        return cache_path.with_name(default_financial_report_meta_path().name)
    return cache_path.with_name(f"{cache_path.stem}_meta.json")


def load_financial_report_cache_meta(cache_file: Path | None = None) -> dict[str, Any]:
    meta_path = financial_report_cache_meta_path(cache_file)
    if not meta_path.exists():
        return {}
    try:
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def save_financial_report_cache_meta(cache_file: Path, meta: Mapping[str, Any]) -> None:
    meta_path = financial_report_cache_meta_path(cache_file)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = meta_path.with_name(f"{meta_path.name}.tmp")
    temp_path.write_text(
        json.dumps(dict(meta), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp_path.replace(meta_path)


def build_financial_report_cache_meta(
    cache_file: Path,
    records: list[dict[str, Any]],
    **fields: Any,
) -> dict[str, Any]:
    financial_records = [record for record in records if is_financial_report_record(record)]
    target_quarter = str(fields.get("target_quarter") or "").strip() or None
    display_records = select_display_financial_report_records(financial_records, target_quarter)
    display_quarter = str(display_records[0].get("quarter", "")) if display_records else ""
    meta: dict[str, Any] = {
        "schema_version": 1,
        "cache_file": str(cache_file),
        "source_label": "MOPS 重大訊息財報 + 持久化快取",
        "record_count": len(records),
        "financial_report_record_count": len(financial_records),
        "display_record_count": len(display_records),
        "display_company_count": financial_report_company_count(display_records),
        "target_quarter": target_quarter or "",
        "display_quarter": display_quarter,
        "newest_announced_at": newest_event_time_iso(financial_records),
        "newest_detected_at": newest_detected_at_iso(financial_records),
    }
    meta.update(fields)
    return meta


def update_financial_report_cache_meta(
    cache_file: Path,
    records: list[dict[str, Any]] | None = None,
    **fields: Any,
) -> dict[str, Any]:
    meta = load_financial_report_cache_meta(cache_file)
    if records is not None:
        meta.update(build_financial_report_cache_meta(cache_file, records, **fields))
    else:
        meta.update({"schema_version": 1, "cache_file": str(cache_file)})
    meta.update(fields)
    save_financial_report_cache_meta(cache_file, meta)
    return meta


def format_financial_report_cache_source(
    cache_file: Path,
    records: list[dict[str, Any]],
    *,
    target_quarter: str | None = None,
) -> str:
    meta = load_financial_report_cache_meta(cache_file)
    source_label = str(meta.get("source_label") or "MOPS 重大訊息財報 + 持久化快取")
    financial_records = [record for record in records if is_financial_report_record(record)]
    display_target_quarter = target_quarter or str(meta.get("target_quarter") or "").strip() or None
    display_records = select_display_financial_report_records(
        financial_records,
        display_target_quarter,
    )
    display_quarter = str(display_records[0].get("quarter", "")) if display_records else ""
    if not display_quarter:
        display_quarter = str(meta.get("display_quarter") or "").strip()
    latest_update = format_meta_time(meta.get("last_success_at") or meta.get("seeded_at"))
    newest_announced_at = format_meta_time(
        meta.get("newest_announced_at") or newest_event_time_iso(financial_records)
    )
    newest_detected_at = format_meta_time(
        meta.get("newest_detected_at") or newest_detected_at_iso(financial_records)
    )
    parts = [source_label]
    if display_quarter:
        parts.append(f"財報季度：{display_quarter}")
    if latest_update:
        parts.append(f"最新更新：{latest_update}")
    if newest_announced_at:
        parts.append(f"最新公告：{newest_announced_at}")
    if newest_detected_at:
        parts.append(f"最新偵測：{newest_detected_at}")
    if meta.get("fetch_error_count"):
        parts.append("部分查詢日更新失敗")
    if meta.get("last_error"):
        parts.append("最近更新失敗")
    return " ｜ ".join(parts)


def load_json_records(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return [record for record in payload if isinstance(record, dict)]
    if isinstance(payload, dict) and isinstance(payload.get("records"), list):
        return [record for record in payload["records"] if isinstance(record, dict)]
    return []


def range_cache_seed_candidates(data_dir: Path) -> list[Path]:
    if not data_dir.exists():
        return []
    return [
        path
        for path in data_dir.glob("material_info_*.json")
        if path.name not in {default_range_output_path().name, default_range_meta_path().name}
        and not path.name.endswith("_meta.json")
    ]


def select_range_cache_seed_file(data_dir: Path) -> Path | None:
    candidates = range_cache_seed_candidates(data_dir)
    if not candidates:
        return None
    preferred = [path for path in candidates if "financial_self_report" in path.name]
    return max(preferred or candidates, key=lambda path: path.stat().st_mtime)


def select_financial_report_seed_file(data_dir: Path) -> Path | None:
    if not data_dir.exists():
        return None
    active_seed = data_dir / default_financial_report_output_path().name
    if active_seed.exists():
        return active_seed
    candidates = list(data_dir.glob("financial_report_demo_cache*.json"))
    return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None


def find_range_cache_file(data_dir: Path | None = None) -> Path | None:
    """Find the active range cache, falling back to legacy seed files."""
    search_dir = data_dir or dashboard_raw_data_dir()
    active_path = search_dir / default_range_output_path().name
    if active_path.exists():
        return active_path
    return select_range_cache_seed_file(search_dir)


def seed_persistent_cache_files(
    target_raw_dir: Path | None = None,
    source_raw_dir: Path = BUNDLED_RAW_DATA_DIR,
) -> list[Path]:
    target_dir = target_raw_dir or dashboard_raw_data_dir()
    if target_dir.resolve() == source_raw_dir.resolve():
        return []

    active_range_cache = target_dir / default_range_output_path().name
    existing_target_seed = select_range_cache_seed_file(target_dir)
    bundled_seed = select_range_cache_seed_file(source_raw_dir) if source_raw_dir.exists() else None
    range_seed_source = existing_target_seed or bundled_seed
    monthly_seed = source_raw_dir / "monthly_revenue_latest.json" if source_raw_dir.exists() else None
    financial_seed = select_financial_report_seed_file(source_raw_dir) if source_raw_dir.exists() else None

    seeded_paths: list[Path] = []
    target_dir.mkdir(parents=True, exist_ok=True)
    if range_seed_source is not None and not active_range_cache.exists():
        shutil.copy2(range_seed_source, active_range_cache)
        records = json.loads(active_range_cache.read_text(encoding="utf-8"))
        meta = build_range_cache_meta(
            active_range_cache,
            records if isinstance(records, list) else [],
            seed_file=str(range_seed_source),
            seeded_at=taiwan_now_iso(),
            last_error=None,
        )
        save_range_cache_meta(active_range_cache, meta)
        seeded_paths.extend([active_range_cache, range_cache_meta_path(active_range_cache)])
    elif active_range_cache.exists() and not range_cache_meta_path(active_range_cache).exists():
        records = json.loads(active_range_cache.read_text(encoding="utf-8"))
        meta = build_range_cache_meta(
            active_range_cache,
            records if isinstance(records, list) else [],
            created_at=taiwan_now_iso(),
            last_error=None,
        )
        save_range_cache_meta(active_range_cache, meta)
        seeded_paths.append(range_cache_meta_path(active_range_cache))

    if monthly_seed is not None and monthly_seed.exists():
        target_monthly = target_dir / monthly_seed.name
        target_monthly_meta = monthly_revenue_cache_meta_path(target_monthly)
        if not target_monthly.exists():
            shutil.copy2(monthly_seed, target_monthly)
            records = json.loads(target_monthly.read_text(encoding="utf-8"))
            meta = build_monthly_revenue_cache_meta(
                target_monthly,
                records if isinstance(records, list) else [],
                seed_file=str(monthly_seed),
                seeded_at=taiwan_now_iso(),
                last_error=None,
            )
            save_monthly_revenue_cache_meta(target_monthly, meta)
            seeded_paths.extend([target_monthly, target_monthly_meta])
        elif not target_monthly_meta.exists():
            records = json.loads(target_monthly.read_text(encoding="utf-8"))
            meta = build_monthly_revenue_cache_meta(
                target_monthly,
                records if isinstance(records, list) else [],
                created_at=taiwan_now_iso(),
                last_error=None,
            )
            save_monthly_revenue_cache_meta(target_monthly, meta)
            seeded_paths.append(target_monthly_meta)

    if financial_seed is not None and financial_seed.exists():
        target_financial = target_dir / default_financial_report_output_path().name
        target_financial_meta = financial_report_cache_meta_path(target_financial)
        if not target_financial.exists():
            records = load_json_records(financial_seed)
            save_records(records, target_financial)
            meta = build_financial_report_cache_meta(
                target_financial,
                records,
                seed_file=str(financial_seed),
                seeded_at=taiwan_now_iso(),
                last_error=None,
            )
            save_financial_report_cache_meta(target_financial, meta)
            seeded_paths.extend([target_financial, target_financial_meta])
        elif not target_financial_meta.exists():
            records = load_json_records(target_financial)
            meta = build_financial_report_cache_meta(
                target_financial,
                records,
                created_at=taiwan_now_iso(),
                last_error=None,
            )
            save_financial_report_cache_meta(target_financial, meta)
            seeded_paths.append(target_financial_meta)
    return seeded_paths


def render_news_cards(records: list[dict[str, Any]]) -> str:
    """Render the default vertical card list for material information."""
    rows = []
    for record in records:
        detail = record.get("detail") or record.get("detail_preview") or {}
        fields = detail.get("fields") or {}
        description = detail.get("description") or ""
        badge = "財報自結" if record.get("is_financial_self_report") else "其他"
        rows.append(
            f"""
            <article class="news-card">
              <header>
                <div class="company">
                  <span class="code">{html.escape(record.get("company_id", ""))}</span>
                  <strong>{html.escape(record.get("company_name", ""))}</strong>
                </div>
                <time>{html.escape(record.get("spoke_date", ""))} {html.escape(record.get("spoke_time", ""))}</time>
              </header>
              <p class="badge">{html.escape(badge)}</p>
              <h2>{html.escape(record.get("subject", ""))}</h2>
              <dl>
                <div><dt>發言人</dt><dd>{html.escape(fields.get("發言人", ""))}</dd></div>
                <div><dt>條款</dt><dd>{html.escape(fields.get("符合條款", ""))}</dd></div>
                <div><dt>事實發生日</dt><dd>{html.escape(fields.get("事實發生日", ""))}</dd></div>
              </dl>
              <details>
                <summary>詳細說明</summary>
                <pre>{html.escape(description)}</pre>
              </details>
            </article>
            """
        )

    return "\n".join(rows) or render_empty_state()


def render_dashboard_tabs(active_tab: str) -> str:
    """Render top-level dashboard tabs without changing data-source mode."""
    tab_items = (
        (TAB_MATERIAL_INFO, "自結"),
        (TAB_MONTHLY_REVENUE, "月營收"),
        (TAB_FINANCIAL_REPORT, "財報"),
    )
    links = []
    for tab, label in tab_items:
        params = {"tab": tab}
        if tab == TAB_MATERIAL_INFO:
            params.update(
                {
                    "mode": MODE_RECENT_FINANCIAL,
                    "category": CATEGORY_FINANCIAL_SELF_REPORT,
                }
            )
        class_name = "tab-link is-active" if tab == active_tab else "tab-link"
        aria_current = ' aria-current="page"' if tab == active_tab else ""
        links.append(
            f'<a class="{class_name}" href="/?{html.escape(urlencode(params))}"'
            f'{aria_current}>{html.escape(label)}</a>'
        )
    return f'<nav class="tab-switcher" aria-label="Dashboard tabs">{"".join(links)}</nav>'


def render_material_info_searchbar(search_query: str) -> str:
    clear_params = urlencode(
        {
            "tab": TAB_MATERIAL_INFO,
            "mode": MODE_RECENT_FINANCIAL,
            "category": CATEGORY_FINANCIAL_SELF_REPORT,
        }
    )
    return f"""
    <form class="searchbar" method="get" action="/">
      <input type="hidden" name="tab" value="{TAB_MATERIAL_INFO}">
      <input type="hidden" name="mode" value="{MODE_RECENT_FINANCIAL}">
      <input type="hidden" name="category" value="{CATEGORY_FINANCIAL_SELF_REPORT}">
      <input type="search" name="q" value="{html.escape(search_query)}" placeholder="輸入股票代號">
      <button type="submit">搜尋</button>
      <a href="/?{html.escape(clear_params)}">清除</a>
    </form>
    """


def render_monthly_revenue_searchbar(search_query: str, active_tab: str = TAB_MONTHLY_REVENUE) -> str:
    active_tab = active_tab if active_tab in {TAB_MONTHLY_REVENUE, TAB_FINANCIAL_REPORT} else TAB_MONTHLY_REVENUE
    clear_params = urlencode({"tab": active_tab})
    return f"""
    <form class="searchbar" method="get" action="/">
      <input type="hidden" name="tab" value="{html.escape(active_tab)}">
      <input type="search" name="q" value="{html.escape(search_query)}" placeholder="輸入股票代號">
      <button type="submit">搜尋</button>
      <a href="/?{html.escape(clear_params)}">清除</a>
    </form>
    """


def display_event_time(record: dict[str, Any]) -> str:
    value = str(record.get("announced_at") or record.get("detected_at") or record.get("event_time") or "")
    if not value:
        return "-"
    normalized = value.replace("T", " ")
    if len(normalized) >= 16:
        return normalized[:16]
    return normalized


def render_monthly_metric(record: dict[str, Any], key: str) -> str:
    value = record.get(key)
    if value in (None, ""):
        metrics = record.get("metrics") or {}
        value = metrics.get(key)
    return render_metric(value)


def render_source_badge(record: dict[str, Any]) -> str:
    source_label = str(record.get("source_label") or record.get("source_type") or "")
    return f'<span class="source-pill">{html.escape(source_label or "-")}</span>'


def record_detail_description(record: dict[str, Any]) -> str:
    detail = record.get("detail") or record.get("detail_preview") or {}
    description = str(detail.get("description") or "")
    fields = detail.get("fields") or record.get("fields") or {}
    if description:
        return description
    if fields:
        return "\n".join(f"{key}: {value}" for key, value in fields.items())
    return str(record.get("title") or record.get("subject") or "")


def record_field_value(record: dict[str, Any], *keys: str) -> str:
    detail = record.get("detail") or {}
    fields = detail.get("fields") or record.get("fields") or {}
    for key in keys:
        value = record.get(key)
        if value not in (None, ""):
            return str(value)
        value = fields.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def monthly_mom_percent(record: dict[str, Any]) -> str:
    existing_value = record_field_value(
        record,
        "mom_percent",
        "MOM%",
        "月增率",
        "較上月增減百分比",
        "上月比較增減百分比",
    )
    if existing_value:
        return existing_value

    current = metric_float(record.get("monthly_revenue"))
    previous = metric_float(record.get("previous_month_revenue"))
    if current is None or previous in (None, 0):
        return ""
    return f"{((current - previous) / abs(previous)) * 100:.2f}"


def monthly_note(record: dict[str, Any]) -> str:
    return record_field_value(
        record,
        "note",
        "備註 / 營收變化原因說明",
        "營收變化原因說明",
        "備註",
        "其他應敘明事項",
    )


def render_monthly_revenue_table(records: list[dict[str, Any]]) -> str:
    if not records:
        return render_empty_state("目前沒有月營收資料。")

    cutoff_date, market_unreacted, historical = split_records_by_market_reaction(records)
    sections = [
        (
            f"市場未反映（{format_month_day(cutoff_date)} 13:30 後公告）（{len(market_unreacted)} 筆）",
            market_unreacted,
        ),
        (
            f"歷史公告（{format_month_day(cutoff_date)} 13:30 前）（{len(historical)} 筆）",
            historical,
        ),
    ]
    rows: list[str] = []
    for section_title, section_records in sections:
        rows.append(
            f"""
            <tr class="eps-group-row">
              <td colspan="8">{html.escape(section_title)}</td>
            </tr>
            """
        )
        if not section_records:
            rows.append(
                """
                <tr class="eps-empty-row">
                  <td colspan="8">目前沒有符合條件的公告。</td>
                </tr>
                """
            )
            continue

        for record in section_records:
            time_note = "公告" if record.get("announced_at") else "偵測"
            note = monthly_note(record)
            revenue_value = record_field_value(record, "monthly_revenue", "本月", "營業收入-當月營收")
            mom_value = monthly_mom_percent(record)
            yoy_value = record_field_value(record, "yoy_percent", "本月增減百分比", "營業收入-去年同月增減(%)")
            ytd_yoy_value = record_field_value(record, "ytd_yoy_percent", "累計增減百分比", "累計營業收入-前期比較增減(%)")
            rows.append(
                f"""
                <tr class="eps-data-row">
                  <td class="code-cell" data-label="代號"{sort_value_attr(record.get("company_id", ""))}>{html.escape(str(record.get("company_id", "")))}</td>
                  <td class="name-cell" data-label="名稱"{sort_value_attr(record.get("company_name", ""))}>{html.escape(str(record.get("company_name", "")))}</td>
                  <td class="time-cell" data-label="偵測時間"{sort_value_attr(event_sort_value(record))}>{html.escape(format_event_table_time(record))}<span class="time-note">{html.escape(time_note)}</span></td>
                  <td class="metric-cell primary-metric" data-label="營收(M)"{sort_value_attr(metric_sort_value(revenue_value))}>{render_money_millions(revenue_value)}</td>
                  <td data-label="MOM%"{sort_value_attr(metric_sort_value(mom_value))}>{render_percent_value(mom_value)}</td>
                  <td data-label="YOY%"{sort_value_attr(metric_sort_value(yoy_value))}>{render_percent_value(yoy_value)}</td>
                  <td data-label="累計YOY%"{sort_value_attr(metric_sort_value(ytd_yoy_value))}>{render_percent_value(ytd_yoy_value)}</td>
                  <td class="note-cell" data-label="備註"{sort_value_attr(note)}>{html.escape(note) if note else '<span class="muted-value">-</span>'}</td>
                </tr>
                """
            )

    headers = render_sortable_headers(
        [
            ("代號", "text"),
            ("名稱", "text"),
            ("偵測時間", "time"),
            ("營收(M)", "number"),
            ("MOM%", "number"),
            ("YOY%", "number"),
            ("累計YOY%", "number"),
            ("備註", "text"),
        ]
    )
    return f"""
    <div class="eps-table-wrap">
      <table class="eps-table monthly-table" data-sortable-table>
        <thead>
          <tr>
            {headers}
          </tr>
        </thead>
        <tbody>
          {"".join(rows)}
        </tbody>
      </table>
    </div>
    """


def financial_metric(record: dict[str, Any], key: str) -> Any:
    metrics = record.get("metrics") or {}
    value = metrics.get(key)
    if value in (None, ""):
        value = record.get(key)
    return value


def render_financial_report_table(records: list[dict[str, Any]]) -> str:
    if not records:
        return render_empty_state("目前沒有財報資料。")

    cutoff_date, market_unreacted, historical = split_records_by_market_reaction(records)
    sections = [
        (
            f"市場未反映（{format_month_day(cutoff_date)} 13:30 後公告）（{len(market_unreacted)} 筆）",
            market_unreacted,
        ),
        (
            f"歷史公告（{format_month_day(cutoff_date)} 13:30 前）（{len(historical)} 筆）",
            historical,
        ),
    ]
    rows: list[str] = []
    detail_index = 0
    for section_title, section_records in sections:
        rows.append(
            f"""
            <tr class="eps-group-row">
              <td colspan="9">{html.escape(section_title)}</td>
            </tr>
            """
        )
        if not section_records:
            rows.append(
                """
                <tr class="eps-empty-row">
                  <td colspan="9">目前沒有符合條件的公告。</td>
                </tr>
                """
            )
            continue

        for record in section_records:
            description = record_detail_description(record)
            detail_id = f"financial-detail-panel-{detail_index}"
            detail_index += 1
            title = str(record.get("title") or record.get("subject") or "")
            source_label = str(record.get("source_label") or "")
            quarter = str(financial_metric(record, "quarter") or record.get("quarter") or "")
            eps = financial_metric(record, "eps")
            gross_margin = financial_metric(record, "gross_margin_pct")
            operating_margin = financial_metric(record, "operating_margin_pct")
            non_operating = financial_metric(record, "non_operating_pct")
            rows.append(
                f"""
                <tr class="eps-data-row" data-detail-target="{detail_id}" tabindex="0" aria-expanded="false">
                  <td class="time-cell" data-label="時間"{sort_value_attr(event_sort_value(record))}>{html.escape(format_event_table_time(record))}</td>
                  <td class="code-cell" data-label="代號"{sort_value_attr(record.get("company_id", ""))}>{html.escape(str(record.get("company_id", "")))}</td>
                  <td class="name-cell" data-label="名稱"{sort_value_attr(record.get("company_name", ""))}>{html.escape(str(record.get("company_name", "")))}</td>
                  <td class="metric-cell" data-label="季度"{sort_value_attr(quarter)}>{html.escape(quarter or "-")}</td>
                  <td class="metric-cell" data-label="EPS"{sort_value_attr(metric_sort_value(eps))}>{render_metric(eps)}</td>
                  <td class="metric-cell" data-label="毛利率"{sort_value_attr(metric_sort_value(gross_margin))}>{render_percent_value(gross_margin)}</td>
                  <td class="metric-cell" data-label="營益率"{sort_value_attr(metric_sort_value(operating_margin))}>{render_percent_value(operating_margin)}</td>
                  <td class="metric-cell" data-label="業外%"{sort_value_attr(metric_sort_value(non_operating))}>{render_percent_value(non_operating)}</td>
                  <td class="detail-cell compact-detail-cell" data-label="原文"{sort_value_attr(title or description)}>
                    <button class="detail-toggle" type="button" aria-controls="{detail_id}" aria-expanded="false">詳細原文</button>
                  </td>
                </tr>
                <tr class="eps-detail-panel-row" id="{detail_id}" hidden>
                  <td colspan="9">
                    <section class="detail-panel" aria-label="財報詳細原文">
                      <div class="detail-subject">{html.escape(title)}</div>
                      <div class="detail-meta-line">{html.escape(source_label or '-')} ｜ 時間：{html.escape(display_event_time(record))}</div>
                      <pre>{html.escape(description)}</pre>
                    </section>
                  </td>
                </tr>
                """
            )

    headers = render_sortable_headers(
        [
            ("時間", "time"),
            ("代號", "text"),
            ("名稱", "text"),
            ("季度", "text"),
            ("EPS", "number"),
            ("毛利率", "number"),
            ("營益率", "number"),
            ("業外%", "number"),
            ("原文", "text"),
        ]
    )
    return f"""
    <div class="eps-table-wrap">
      <table class="eps-table financial-table" data-sortable-table>
        <thead>
          <tr>
            {headers}
          </tr>
        </thead>
        <tbody>
          {"".join(rows)}
        </tbody>
      </table>
    </div>
    """


def render_tabbed_dashboard(
    records: list[dict[str, Any]],
    active_tab: str,
    recent_days: int,
) -> str:
    active_tab = active_tab if active_tab in TAB_CHOICES else TAB_MATERIAL_INFO
    if active_tab == TAB_MONTHLY_REVENUE:
        title = "月營收"
        subtitle_html = render_monthly_revenue_panel_subtitle(records)
        content = render_monthly_revenue_table(records)
    elif active_tab == TAB_FINANCIAL_REPORT:
        title = "財報"
        subtitle_html = f"<p>{html.escape(f'董事會通過財務報告與早期財報訊號（{len(records)} 筆）')}</p>"
        content = render_financial_report_table(records)
    else:
        title = "自結"
        subtitle_html = render_material_info_panel_subtitle(records, recent_days)
        content = render_eps_table(records, recent_days)

    return f"""
    <section class="dashboard-panel">
      <div class="dashboard-panel-header">
        <div class="panel-heading">
          <h2>{html.escape(title)}</h2>
          {subtitle_html}
        </div>
        {render_dashboard_tabs(active_tab)}
      </div>
      {content}
    </section>
    """


def render_eps_table(records: list[dict[str, Any]], recent_days: int) -> str:
    """Render a compact EPS table for recent self-reported financial records."""
    if not records:
        return render_empty_state("目前沒有近一周財報自結資料。")

    cutoff_date, market_unreacted, historical = split_records_by_market_reaction(records)
    sections = [
        (
            f"市場未反映（{format_month_day(cutoff_date)} 13:30 後公告）（{len(market_unreacted)} 筆）",
            market_unreacted,
        ),
        (
            f"歷史公告（{format_month_day(cutoff_date)} 13:30 前）（{len(historical)} 筆）",
            historical,
        ),
    ]

    rows: list[str] = []
    detail_index = 0
    for section_title, section_records in sections:
        rows.append(
            f"""
            <tr class="eps-group-row">
              <td colspan="11">{html.escape(section_title)}</td>
            </tr>
            """
        )
        if not section_records:
            rows.append(
                """
                <tr class="eps-empty-row">
                  <td colspan="11">目前沒有符合條件的公告。</td>
                </tr>
                """
            )
            continue

        for record in section_records:
            metrics = get_or_extract_eps_metrics(record)
            detail = record.get("detail") or record.get("detail_preview") or {}
            description = detail.get("description") or ""
            missing_class = "" if metrics.get("has_eps") else " missing-eps"
            detail_id = f"detail-panel-{detail_index}"
            detail_index += 1
            rows.append(
            f"""
            <tr class="eps-data-row{missing_class}" data-detail-target="{detail_id}" tabindex="0" aria-expanded="false">
              <td class="time-cell" data-label="時間"{sort_value_attr(event_sort_value(record))}>{html.escape(format_table_time(record))}</td>
              <td class="code-cell" data-label="代號"{sort_value_attr(record.get("company_id", ""))}>{html.escape(record.get("company_id", ""))}</td>
              <td class="name-cell" data-label="名稱"{sort_value_attr(record.get("company_name", ""))}>{html.escape(record.get("company_name", ""))}</td>
              <td data-label="EPS年增差"{sort_value_attr(metric_sort_value(eps_delta_value(metrics)))}>{render_eps_delta(metrics)}</td>
              <td data-label="期間"{sort_value_attr(metrics.get("period"))}>{render_metric(metrics.get("period"))}</td>
              <td class="metric-cell primary-metric" data-label="EPS"{sort_value_attr(metric_sort_value(metrics.get("month_eps")))}>{render_metric(metrics.get("month_eps"))}</td>
              <td data-label="去年同期EPS"{sort_value_attr(metric_sort_value(metrics.get("last_year_month_eps")))}>{render_metric(metrics.get("last_year_month_eps"))}</td>
              <td data-label="上季"{sort_value_attr(metrics.get("quarter"))}>{render_metric(metrics.get("quarter"))}</td>
              <td data-label="上季EPS/3"{sort_value_attr(metric_sort_value(metrics.get("quarter_eps_div3")))}>{render_metric(metrics.get("quarter_eps_div3"))}</td>
              <td class="metric-cell" data-label="上季EPS"{sort_value_attr(metric_sort_value(metrics.get("quarter_eps")))}>{render_metric(metrics.get("quarter_eps"))}</td>
              <td class="detail-cell" data-label="原文"{sort_value_attr(record.get("subject") or description)}>
                <button class="detail-toggle" type="button" aria-controls="{detail_id}" aria-expanded="false">詳細原文</button>
              </td>
            </tr>
            <tr class="eps-detail-panel-row" id="{detail_id}" hidden>
              <td colspan="11">
                <section class="detail-panel" aria-label="重大訊息詳細原文">
                  <div class="detail-subject">{html.escape(record.get("subject", ""))}</div>
                  <pre>{html.escape(description)}</pre>
                </section>
              </td>
            </tr>
            """
            )

    headers = render_sortable_headers(
        [
            ("時間", "time"),
            ("代號", "text"),
            ("名稱", "text"),
            ("EPS年增差", "number"),
            ("期間", "text"),
            ("EPS", "number"),
            ("去年同期EPS", "number"),
            ("上季", "text"),
            ("上季EPS/3", "number"),
            ("上季EPS", "number"),
            ("原文", "text"),
        ]
    )
    return f"""
    <div class="eps-table-wrap">
      <table class="eps-table" data-sortable-table>
        <thead>
          <tr>
            {headers}
          </tr>
        </thead>
        <tbody>
          {"".join(rows)}
        </tbody>
      </table>
    </div>
    """


def render_dashboard(
    records: list[dict[str, Any]],
    generated_at: str,
    source: str,
    mode: str,
    category: str,
    search_query: str,
    recent_days: int = DEFAULT_RECENT_DAYS,
    active_tab: str = TAB_MATERIAL_INFO,
) -> str:
    """Render a minimal HTML dashboard."""
    active_tab = active_tab if active_tab in TAB_CHOICES else TAB_MATERIAL_INFO
    body = render_tabbed_dashboard(records, active_tab, recent_days)
    if active_tab == TAB_MATERIAL_INFO:
        searchbar = render_material_info_searchbar(search_query)
    elif active_tab == TAB_MONTHLY_REVENUE:
        searchbar = render_monthly_revenue_searchbar(search_query, active_tab)
    elif active_tab == TAB_FINANCIAL_REPORT:
        searchbar = render_monthly_revenue_searchbar(search_query, active_tab)
    else:
        searchbar = ""
    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>台股重大訊息 Dashboard</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #111421;
      --panel: #111421;
      --panel-2: #171b29;
      --panel-3: #0d101a;
      --ink: #e5e7eb;
      --muted: #7f8a99;
      --line: #252b39;
      --line-strong: #394456;
      --accent: #62b4ff;
    }}
    * {{ box-sizing: border-box; }}
    html {{
      min-height: 100%;
      background: var(--bg);
    }}
    body {{
      margin: 0;
      min-height: 100vh;
      font-family: "Microsoft JhengHei", "Noto Sans TC", Arial, sans-serif;
      background: var(--bg);
      color: var(--ink);
    }}
    main {{
      width: min(1160px, calc(100% - 32px));
      margin: 24px auto 48px;
    }}
    .topbar {{
      padding: 0 0 16px;
      border-bottom: 1px solid var(--line);
    }}
    h1 {{
      margin: 0 0 6px;
      font-size: 28px;
      letter-spacing: 0;
    }}
    .meta {{
      margin: 0;
      color: var(--muted);
      font-size: 14px;
    }}
    .badge {{
      display: inline-block;
      margin: 14px 0 0;
      padding: 3px 7px;
      color: #14532d;
      background: #dcfce7;
      border: 1px solid #86efac;
      border-radius: 4px;
      font-size: 12px;
      font-weight: 700;
    }}
    .searchbar {{
      display: flex;
      align-items: center;
      gap: 8px;
      margin-top: 16px;
      padding-bottom: 16px;
      border-bottom: 1px solid var(--line);
    }}
    .searchbar input[type="search"] {{
      width: min(320px, 100%);
      height: 38px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 8px 10px;
      font: inherit;
      color: var(--ink);
      background: var(--panel-3);
      outline: none;
    }}
    .searchbar input[type="search"]::placeholder {{
      color: #6f7783;
    }}
    .searchbar input[type="search"]:focus {{
      border-color: var(--accent);
      box-shadow: 0 0 0 3px rgba(98, 180, 255, 0.16);
    }}
    .searchbar button,
    .searchbar a {{
      height: 38px;
      border-radius: 6px;
      padding: 8px 12px;
      font: inherit;
      text-decoration: none;
    }}
    .searchbar button {{
      border: 1px solid var(--accent);
      color: #06111f;
      background: #7bb7ff;
      cursor: pointer;
      font-weight: 800;
    }}
    .searchbar a {{
      display: inline-flex;
      align-items: center;
      border: 1px solid #334155;
      color: var(--muted);
      background: var(--panel-2);
    }}
    .dashboard-panel {{
      min-width: 0;
    }}
    .dashboard-panel-header {{
      display: flex;
      align-items: flex-end;
      justify-content: space-between;
      gap: 16px;
      margin-top: 2px;
    }}
    .panel-heading h2 {{
      margin: 0;
      color: #f3f6fb;
      font-size: 18px;
      line-height: 1.3;
      letter-spacing: 0;
    }}
    .panel-heading p {{
      margin: 5px 0 0;
      color: var(--muted);
      font-size: 13px;
    }}
    .panel-subtitle {{
      margin-top: 5px;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.7;
    }}
    .monthly-summary-meta {{
      display: grid;
      gap: 1px;
    }}
    .tab-switcher {{
      display: inline-flex;
      flex: 0 0 auto;
      gap: 2px;
      padding: 3px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel-3);
    }}
    .tab-link {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 32px;
      padding: 6px 12px;
      border: 1px solid transparent;
      border-radius: 4px;
      color: #a6b2c3;
      text-decoration: none;
      font-size: 13px;
      font-weight: 800;
      white-space: nowrap;
    }}
    .tab-link:hover {{
      color: #ffffff;
      border-color: #334155;
      background: #171b29;
    }}
    .tab-link.is-active {{
      color: #06111f;
      border-color: #7bb7ff;
      background: #7bb7ff;
    }}
    .news-list {{
      display: flex;
      flex-direction: column;
      gap: 14px;
      margin-top: 18px;
    }}
    .news-card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
      min-width: 0;
    }}
    .news-card header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      color: var(--muted);
      font-size: 13px;
    }}
    .company {{
      display: flex;
      align-items: center;
      gap: 8px;
      min-width: 0;
    }}
    .code {{
      color: #ffffff;
      background: #475467;
      border-radius: 4px;
      padding: 2px 6px;
      font-size: 12px;
    }}
    .company strong {{
      overflow-wrap: anywhere;
      color: var(--ink);
    }}
    time {{
      white-space: nowrap;
    }}
    h2 {{
      margin: 14px 0;
      font-size: 17px;
      line-height: 1.5;
      letter-spacing: 0;
    }}
    dl {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
      margin: 0 0 12px;
    }}
    dt {{
      color: var(--muted);
      font-size: 12px;
    }}
    dd {{
      margin: 3px 0 0;
      overflow-wrap: anywhere;
      font-size: 14px;
    }}
    details {{
      border-top: 1px solid var(--line);
      padding-top: 12px;
    }}
    summary {{
      cursor: pointer;
      color: var(--accent);
      font-weight: 700;
    }}
    pre {{
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      margin: 12px 0 0;
      font-family: "Microsoft JhengHei", "Noto Sans TC", monospace;
      font-size: 14px;
      line-height: 1.6;
    }}
    .eps-table-wrap {{
      margin-top: 18px;
      overflow-x: auto;
      background: #111421;
      border: 1px solid #263041;
      border-radius: 8px;
      box-shadow: 0 18px 38px rgba(15, 23, 42, 0.12);
    }}
    .eps-table {{
      width: 100%;
      min-width: 980px;
      border-collapse: collapse;
      color: #d8dde7;
      background: #111421;
      font-size: 14px;
    }}
    .eps-table th,
    .eps-table td {{
      padding: 11px 12px;
      border-bottom: 1px solid #252b39;
      text-align: right;
      vertical-align: top;
      white-space: nowrap;
    }}
    .eps-table th {{
      color: #7f8a99;
      background: #171b29;
      font-size: 13px;
      font-weight: 800;
    }}
    .sort-button {{
      display: inline-flex;
      align-items: center;
      justify-content: flex-end;
      gap: 5px;
      width: 100%;
      padding: 0;
      border: 0;
      color: inherit;
      background: transparent;
      font: inherit;
      font-weight: inherit;
      text-align: inherit;
      cursor: pointer;
      white-space: nowrap;
    }}
    .sort-button:hover,
    .sort-button:focus-visible {{
      color: #dbeafe;
    }}
    .sort-button:focus-visible {{
      outline: 2px solid #7bb7ff;
      outline-offset: 3px;
      border-radius: 4px;
    }}
    .sort-indicator {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 10px;
      color: #64748b;
      font-size: 10px;
      line-height: 1;
    }}
    .sort-indicator::before {{
      content: "↕";
    }}
    .eps-table th[aria-sort="ascending"] .sort-indicator::before {{
      content: "▲";
      color: #7bb7ff;
    }}
    .eps-table th[aria-sort="descending"] .sort-indicator::before {{
      content: "▼";
      color: #7bb7ff;
    }}
    .eps-table th:nth-child(1),
    .eps-table th:nth-child(2),
    .eps-table th:nth-child(3),
    .eps-table td:nth-child(1),
    .eps-table td:nth-child(2),
    .eps-table td:nth-child(3) {{
      text-align: left;
    }}
    .eps-table th:nth-child(1) .sort-button,
    .eps-table th:nth-child(2) .sort-button,
    .eps-table th:nth-child(3) .sort-button {{
      justify-content: flex-start;
    }}
    .monthly-table {{
      min-width: 1080px;
    }}
    .financial-table {{
      min-width: 1180px;
    }}
    .monthly-table th:nth-child(8),
    .monthly-table td:nth-child(8),
    .financial-table th:nth-child(10),
    .financial-table td:nth-child(10) {{
      text-align: left;
    }}
    .subject-cell {{
      min-width: 260px;
      max-width: 420px;
      white-space: normal !important;
      overflow-wrap: anywhere;
    }}
    .eps-group-row td {{
      color: #cfd6df;
      background: #1a1e30;
      border-bottom: 1px solid #43503f;
      font-weight: 800;
      text-align: left;
    }}
    .eps-empty-row td {{
      color: #7f8a99;
      background: #111421;
      border-bottom: 1px solid #252b39;
      text-align: left;
    }}
    .eps-data-row:hover td {{
      background: #151a2a;
    }}
    .eps-data-row {{
      cursor: pointer;
    }}
    .eps-data-row:focus {{
      outline: 2px solid #7bb7ff;
      outline-offset: -2px;
    }}
    .eps-data-row.is-expanded td {{
      background: #151a2a;
      border-bottom-color: #394456;
    }}
    .detail-cell {{
      text-align: left;
      white-space: normal;
      min-width: 240px;
    }}
    .compact-detail-cell {{
      min-width: 112px;
    }}
    .detail-toggle {{
      min-height: 30px;
      color: #7bb7ff;
      background: transparent;
      border: 1px solid #334155;
      border-radius: 6px;
      padding: 5px 9px;
      font: inherit;
      font-size: 13px;
      font-weight: 800;
      cursor: pointer;
    }}
    .detail-toggle:hover,
    .eps-data-row.is-expanded .detail-toggle {{
      color: #ffffff;
      border-color: #7bb7ff;
      background: #18324e;
    }}
    .source-pill {{
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      padding: 3px 7px;
      color: #dbeafe;
      background: #17324d;
      border: 1px solid #31577c;
      border-radius: 4px;
      font-size: 12px;
      font-weight: 900;
      white-space: nowrap;
    }}
    .time-note {{
      display: block;
      margin-top: 3px;
      color: #8ca0b7;
      font-size: 11px;
      font-weight: 800;
    }}
    .note-cell {{
      min-width: 260px;
      max-width: 460px;
      color: #b9c2cf;
      line-height: 1.5;
      white-space: normal !important;
      overflow-wrap: anywhere;
    }}
    .eps-detail-panel-row[hidden] {{
      display: none;
    }}
    .eps-detail-panel-row td {{
      padding: 12px 14px 16px;
      background: #0f1320;
      border-bottom: 1px solid #394456;
      text-align: left;
      white-space: normal;
    }}
    .detail-panel {{
      width: 100%;
      max-height: 300px;
      overflow: auto;
      color: #cbd5e1;
      background: #0d101a;
      border: 1px solid #2b3445;
      border-radius: 6px;
      padding: 12px;
      scrollbar-color: #43506a #0d101a;
      scrollbar-width: thin;
    }}
    .detail-panel::-webkit-scrollbar {{
      width: 10px;
      height: 10px;
    }}
    .detail-panel::-webkit-scrollbar-track {{
      background: #0d101a;
      border-radius: 999px;
    }}
    .detail-panel::-webkit-scrollbar-thumb {{
      background: #43506a;
      border: 2px solid #0d101a;
      border-radius: 999px;
    }}
    .detail-panel::-webkit-scrollbar-thumb:hover {{
      background: #5a6885;
    }}
    .detail-panel pre {{
      margin-top: 10px;
      max-height: none;
      overflow: visible;
    }}
    .detail-subject {{
      margin-top: 4px;
      color: #f3f6fb;
      font-weight: 800;
      overflow-wrap: anywhere;
    }}
    .detail-meta-line {{
      margin-top: 8px;
      color: #8ca0b7;
      font-size: 12px;
      font-weight: 800;
    }}
    .time-cell,
    .missing-eps .metric-cell,
    .muted-value {{
      color: #6f7783;
    }}
    .code-cell {{
      color: #62b4ff;
      font-weight: 900;
    }}
    .name-cell {{
      color: #eef3fb;
      font-weight: 900;
    }}
    .metric-cell {{
      color: #ff9b42;
      font-weight: 900;
    }}
    .money-value {{
      color: #eef3fb;
      font-weight: 900;
      font-variant-numeric: tabular-nums;
    }}
    .primary-metric {{
      border-left: 1px solid #394456;
    }}
    .positive {{
      color: #38d46f;
      font-weight: 900;
    }}
    .negative {{
      color: #ff626f;
      font-weight: 900;
    }}
    .finance-up {{
      color: #ff626f;
      font-weight: 900;
      font-variant-numeric: tabular-nums;
    }}
    .finance-down {{
      color: #38d46f;
      font-weight: 900;
      font-variant-numeric: tabular-nums;
    }}
    .empty {{
      padding: 24px;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
    }}
    @media (max-width: 900px) {{
      .eps-table-wrap {{
        overflow-x: visible;
        background: transparent;
        border: 0;
        border-radius: 0;
        box-shadow: none;
      }}
      .eps-table {{
        display: block;
        min-width: 0;
        background: transparent;
      }}
      .eps-table thead {{
        display: none;
      }}
      .eps-table tbody {{
        display: grid;
        gap: 10px;
      }}
      .eps-table tr {{
        display: block;
      }}
      .eps-group-row td {{
        display: block;
        border: 1px solid #293348;
        border-radius: 8px;
        padding: 12px;
      }}
      .eps-data-row {{
        display: grid !important;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        column-gap: 14px;
        padding: 10px 12px;
        background: #111421;
        border: 1px solid #263041;
        border-radius: 8px;
      }}
      .eps-table td {{
        display: flex;
        align-items: baseline;
        justify-content: space-between;
        gap: 10px;
        min-width: 0;
        padding: 8px 0;
        border-bottom: 1px solid #252b39;
        text-align: right;
        white-space: normal;
      }}
      .eps-table td::before {{
        content: attr(data-label);
        flex: 0 0 auto;
        color: #7f8a99;
        font-weight: 800;
        text-align: left;
      }}
      .eps-group-row td::before,
      .eps-detail-panel-row td::before,
      .detail-cell::before {{
        content: none;
      }}
      .primary-metric {{
        border-left: 0;
      }}
      .detail-cell {{
        display: flex !important;
        grid-column: 1 / -1;
        justify-content: flex-start !important;
        min-width: 0;
        padding-top: 10px !important;
        border-bottom: 0 !important;
      }}
      .detail-toggle {{
        width: 100%;
        min-height: 36px;
      }}
      .eps-detail-panel-row {{
        margin-top: -10px;
      }}
      .eps-detail-panel-row td {{
        display: block;
        padding: 0 12px 12px;
        background: #111421;
        border: 1px solid #263041;
        border-top: 0;
        border-radius: 0 0 8px 8px;
      }}
      .detail-panel {{
        max-height: 52vh;
      }}
      .detail-panel pre {{
        width: 100%;
        margin-top: 8px;
      }}
    }}
    @media (max-width: 520px) {{
      .eps-data-row {{
        grid-template-columns: 1fr;
      }}
      .eps-table td {{
        gap: 16px;
      }}
    }}
    @media (max-width: 720px) {{
      main {{
        width: min(100% - 20px, 1160px);
        margin-top: 16px;
      }}
      .topbar {{
        display: block;
      }}
      .searchbar {{
        align-items: stretch;
        flex-direction: column;
      }}
      .searchbar input[type="search"] {{
        width: 100%;
      }}
      .dashboard-panel-header {{
        align-items: stretch;
        flex-direction: column;
      }}
      .tab-switcher {{
        width: 100%;
      }}
      .tab-link {{
        flex: 1 1 0;
      }}
      dl {{
        grid-template-columns: 1fr;
      }}
      .news-card header {{
        display: block;
      }}
      time {{
        display: block;
        margin-top: 8px;
      }}
    }}
  </style>
</head>
<body>
  <main>
    <section class="topbar">
      <div>
        <h1>台股重大訊息 Dashboard</h1>
        <p class="meta">資料來源：{html.escape(source)} ｜ 頁面時間：{html.escape(generated_at)}</p>
      </div>
    </section>
    {searchbar}
    <section class="news-list">{body}</section>
  </main>
  <script>
    (() => {{
      const rows = Array.from(document.querySelectorAll(".eps-data-row[data-detail-target]"));
      const sortableTables = Array.from(document.querySelectorAll("table[data-sortable-table]"));

      const setOpen = (row, open) => {{
        const panel = document.getElementById(row.dataset.detailTarget);
        const button = row.querySelector(".detail-toggle");
        if (!panel || !button) {{
          return;
        }}

        panel.hidden = !open;
        row.classList.toggle("is-expanded", open);
        row.setAttribute("aria-expanded", String(open));
        button.setAttribute("aria-expanded", String(open));
        button.textContent = open ? "收合原文" : "詳細原文";
      }};

      const toggleRow = (row) => {{
        const willOpen = row.getAttribute("aria-expanded") !== "true";
        if (willOpen) {{
          rows.forEach((otherRow) => {{
            if (otherRow !== row) {{
              setOpen(otherRow, false);
            }}
          }});
        }}
        setOpen(row, willOpen);
      }};

      rows.forEach((row) => {{
        const button = row.querySelector(".detail-toggle");
        row.addEventListener("click", (event) => {{
          if (event.target.closest("a, input, select, textarea")) {{
            return;
          }}
          toggleRow(row);
        }});
        row.addEventListener("keydown", (event) => {{
          if (event.target.closest(".detail-toggle")) {{
            return;
          }}
          if (event.key === "Enter" || event.key === " ") {{
            event.preventDefault();
            toggleRow(row);
          }}
        }});
        button?.addEventListener("click", (event) => {{
          event.stopPropagation();
          toggleRow(row);
        }});
      }});

      const normalizeSortText = (value) => (value || "").trim();

      const parseSortNumber = (value) => {{
        const cleaned = normalizeSortText(value).replace(/[,%+]/g, "");
        if (!cleaned || cleaned === "-") {{
          return null;
        }}
        const parsed = Number(cleaned);
        return Number.isFinite(parsed) ? parsed : null;
      }};

      const getCellSortValue = (row, columnIndex, sortType) => {{
        const cell = row.cells[columnIndex];
        if (!cell) {{
          return "";
        }}
        const rawValue = cell.dataset.sortValue ?? cell.textContent;
        if (sortType === "number" || sortType === "time") {{
          const parsed = parseSortNumber(rawValue);
          return parsed === null ? "" : parsed;
        }}
        return normalizeSortText(rawValue).toLocaleLowerCase("zh-Hant");
      }};

      const comparePairs = (columnIndex, sortType, direction) => (left, right) => {{
        const leftValue = getCellSortValue(left.row, columnIndex, sortType);
        const rightValue = getCellSortValue(right.row, columnIndex, sortType);
        const leftEmpty = leftValue === "";
        const rightEmpty = rightValue === "";
        if (leftEmpty || rightEmpty) {{
          if (leftEmpty && rightEmpty) {{
            return left.index - right.index;
          }}
          return leftEmpty ? 1 : -1;
        }}

        let comparison = 0;
        if (typeof leftValue === "number" && typeof rightValue === "number") {{
          comparison = leftValue - rightValue;
        }} else {{
          comparison = String(leftValue).localeCompare(String(rightValue), "zh-Hant", {{
            numeric: true,
            sensitivity: "base",
          }});
        }}
        if (comparison === 0) {{
          comparison = left.index - right.index;
        }}
        return direction === "asc" ? comparison : -comparison;
      }};

      const getSortableGroups = (tbody) => {{
        const groups = [];
        let currentGroup = {{ header: null, pairs: [], extras: [] }};
        groups.push(currentGroup);
        const bodyRows = Array.from(tbody.children);
        for (let index = 0; index < bodyRows.length; index += 1) {{
          const row = bodyRows[index];
          if (row.classList.contains("eps-group-row")) {{
            currentGroup = {{ header: row, pairs: [], extras: [] }};
            groups.push(currentGroup);
            continue;
          }}
          if (row.classList.contains("eps-data-row")) {{
            const nextRow = bodyRows[index + 1];
            const detailRow = nextRow?.classList.contains("eps-detail-panel-row") ? nextRow : null;
            currentGroup.pairs.push({{ row, detailRow, index: currentGroup.pairs.length }});
            if (detailRow) {{
              index += 1;
            }}
            continue;
          }}
          if (row.classList.contains("eps-detail-panel-row")) {{
            continue;
          }}
          currentGroup.extras.push(row);
        }}
        return groups.filter((group) => group.header || group.pairs.length || group.extras.length);
      }};

      const renderSortedTable = (table, columnIndex, sortType, direction) => {{
        const tbody = table.tBodies[0];
        if (!tbody) {{
          return;
        }}
        const fragment = document.createDocumentFragment();
        getSortableGroups(tbody).forEach((group) => {{
          if (group.header) {{
            fragment.appendChild(group.header);
          }}
          const sortedPairs = [...group.pairs].sort(comparePairs(columnIndex, sortType, direction));
          sortedPairs.forEach((pair) => {{
            fragment.appendChild(pair.row);
            if (pair.detailRow) {{
              fragment.appendChild(pair.detailRow);
            }}
          }});
          group.extras.forEach((row) => fragment.appendChild(row));
        }});
        tbody.appendChild(fragment);
      }};

      sortableTables.forEach((table) => {{
        const headerButtons = Array.from(table.querySelectorAll("thead .sort-button"));
        headerButtons.forEach((button, columnIndex) => {{
          button.addEventListener("click", () => {{
            const sortType = button.dataset.sortType || "text";
            const nextDirection = (
              table.dataset.sortColumn === String(columnIndex)
              && table.dataset.sortDirection === "asc"
            ) ? "desc" : "asc";

            table.dataset.sortColumn = String(columnIndex);
            table.dataset.sortDirection = nextDirection;
            headerButtons.forEach((otherButton) => {{
              const th = otherButton.closest("th");
              th?.setAttribute("aria-sort", "none");
            }});
            button.closest("th")?.setAttribute(
              "aria-sort",
              nextDirection === "asc" ? "ascending" : "descending",
            );
            renderSortedTable(table, columnIndex, sortType, nextDirection);
          }});
        }});
      }});
    }})();
  </script>
</body>
</html>"""


class DashboardServer:
    """Tiny HTTP server with either live crawling or an offline JSON file."""

    def __init__(
        self,
        crawler: MopsCrawler,
        max_items: int,
        refresh_seconds: int,
        output_path: Path,
        previous_output_path: Path,
        mode: str,
        category: str,
        range_cache_file: Path | None = None,
        recent_days: int = DEFAULT_RECENT_DAYS,
        target_date: str | None = None,
        offline_file: Path | None = None,
        update_min_interval_seconds: int = DEFAULT_UPDATE_MIN_INTERVAL_SECONDS,
        allow_unprotected_local_update: bool = False,
        monthly_revenue_crawler: MonthlyRevenueCrawler | None = None,
        monthly_revenue_output_path: Path = DEFAULT_MONTHLY_REVENUE_OUTPUT_PATH,
        monthly_revenue_company_ids: list[str] | None = None,
        monthly_revenue_market: str = "all",
        monthly_revenue_roc_year: int | None = None,
        monthly_revenue_month: int | None = None,
        financial_report_output_path: Path = DEFAULT_FINANCIAL_REPORT_OUTPUT_PATH,
        financial_report_target_quarter: str | None = None,
        financial_report_lookback_days: int = DEFAULT_FINANCIAL_REPORT_LOOKBACK_DAYS,
    ) -> None:
        self.crawler = crawler
        self.monthly_revenue_crawler = monthly_revenue_crawler or MonthlyRevenueCrawler(
            request_interval_seconds=getattr(crawler, "request_interval_seconds", DEFAULT_REQUEST_INTERVAL_SECONDS)
        )
        self.max_items = max_items
        self.refresh_seconds = refresh_seconds
        self.output_path = output_path
        self.previous_output_path = previous_output_path
        self.monthly_revenue_output_path = monthly_revenue_output_path
        self.financial_report_output_path = financial_report_output_path
        self.mode = mode
        self.category = category
        self.range_cache_file = range_cache_file
        self.recent_days = recent_days
        self.target_date = target_date
        self.offline_file = offline_file
        self.update_min_interval_seconds = update_min_interval_seconds
        self.allow_unprotected_local_update = allow_unprotected_local_update
        self.last_update_at = 0.0
        self.last_update_result: dict[str, Any] | None = None
        self.cache_records: dict[str, list[dict[str, Any]]] = {}
        self.cache_at: dict[str, float] = {}
        self.monthly_revenue_company_ids = monthly_revenue_company_ids or []
        self.monthly_revenue_market = monthly_revenue_market
        self.monthly_revenue_roc_year = monthly_revenue_roc_year
        self.monthly_revenue_month = monthly_revenue_month
        self.financial_report_target_quarter = financial_report_target_quarter
        self.financial_report_lookback_days = financial_report_lookback_days

    def get_records(
        self,
        mode: str | None = None,
        category: str | None = None,
        search_query: str = "",
    ) -> tuple[list[dict[str, Any]], str]:
        selected_mode = mode or self.mode
        selected_category = category or self.category
        if self.offline_file is not None:
            records = self._load_offline_records(self.offline_file)
            records = sort_records_by_spoke_time(records)
            records = filter_records_by_category(records, selected_category)
            records = filter_records_by_company_id(records, search_query)
            return records, f"offline file: {self.offline_file}"

        if selected_mode == MODE_RECENT_FINANCIAL:
            records, source = self._get_recent_financial_records()
            records = filter_records_by_company_id(records, search_query)
            return records, source

        now = time.monotonic()
        if (
            selected_mode not in self.cache_records
            or now - self.cache_at.get(selected_mode, 0.0) >= self.refresh_seconds
        ):
            if selected_mode == MODE_PREVIOUS:
                records = self.crawler.fetch_previous_day_with_details(
                    target_date=self.target_date,
                    max_items=self.max_items,
                )
                save_records(records, self.previous_output_path)
                source = "MOPS previous-day endpoint"
            else:
                records = self.crawler.fetch_latest_with_details(max_items=self.max_items)
                save_records(records, self.output_path)
                source = "MOPS realtime endpoint"

            self.cache_records[selected_mode] = records
            self.cache_at[selected_mode] = now
            self.cache_records[f"{selected_mode}:source"] = [{"source": source}]

        source_record = self.cache_records.get(f"{selected_mode}:source", [{"source": ""}])[0]
        records = sort_records_by_spoke_time(self.cache_records[selected_mode])
        records = filter_records_by_category(records, selected_category)
        records = filter_records_by_company_id(records, search_query)
        return records, source_record["source"]

    def _load_offline_records(self, path: Path) -> list[dict[str, Any]]:
        return load_json_records(path)

    def _get_recent_financial_records(self) -> tuple[list[dict[str, Any]], str]:
        cache_file = self.range_cache_file or find_range_cache_file()
        if cache_file is None or not cache_file.exists():
            return [], "range cache not found"

        records = self._load_offline_records(cache_file)
        records = filter_records_by_recent_days(records, days=self.recent_days)
        records = filter_records_by_listing_market(records)
        records = filter_records_for_recent_financial(records)
        records = sort_records_by_spoke_time(records)
        return records, format_range_cache_source(cache_file, records)

    def _get_monthly_signal_records(
        self,
    ) -> tuple[list[dict[str, Any]], str]:
        now = time.monotonic()
        cache_key = TAB_MONTHLY_REVENUE
        if (
            cache_key not in self.cache_records
            or now - self.cache_at.get(cache_key, 0.0) >= self.refresh_seconds
        ):
            result = self.update_monthly_revenue_cache()
            self.cache_records[f"{cache_key}:source"] = [
                {"source": result.get("source", "MOPS monthly revenue endpoints")}
            ]

        records = sort_event_records(self.cache_records.get(cache_key, []))
        source_record = self.cache_records.get(
            f"{cache_key}:source",
            [{"source": "MOPS monthly revenue endpoints"}],
        )[0]
        return records, source_record["source"]

    def _get_monthly_revenue_records(
        self,
        search_query: str = "",
    ) -> tuple[list[dict[str, Any]], str]:
        now = time.monotonic()
        cache_key = MONTHLY_REVENUE_SUMMARY_CACHE_KEY
        if (
            cache_key not in self.cache_records
            or now - self.cache_at.get(cache_key, 0.0) >= self.refresh_seconds
        ):
            if self.monthly_revenue_output_path.exists():
                cached_records = self._load_offline_records(self.monthly_revenue_output_path)
                self.cache_records[cache_key] = sort_event_records(cached_records)
                self.cache_at[cache_key] = now
                self.cache_records[f"{cache_key}:source"] = [
                    {
                        "source": format_monthly_revenue_cache_source(
                            self.monthly_revenue_output_path,
                            cached_records,
                        )
                    }
                ]
            else:
                result = self.update_monthly_revenue_summary_cache()
                self.cache_records[f"{cache_key}:source"] = [
                    {"source": result.get("source", "MOPS t21sc04_ifrs monthly revenue summary")}
                ]
        records = sort_event_records(self.cache_records.get(cache_key, []))
        source_record = self.cache_records.get(
            f"{cache_key}:source",
            [{"source": "MOPS t21sc04_ifrs monthly revenue summary"}],
        )[0]
        source = source_record["source"]
        records = [record for record in records if is_monthly_revenue_record(record)]
        records = select_display_monthly_revenue_records(records)
        records = filter_monthly_records_by_company_id(records, search_query)
        return records, source

    def _get_financial_report_records(
        self,
        search_query: str = "",
    ) -> tuple[list[dict[str, Any]], str]:
        now = time.monotonic()
        cache_key = TAB_FINANCIAL_REPORT
        target_quarter = self._current_financial_report_target_quarter()
        if (
            cache_key not in self.cache_records
            or now - self.cache_at.get(cache_key, 0.0) >= self.refresh_seconds
        ):
            if self.financial_report_output_path.exists():
                cached_records = self._load_offline_records(self.financial_report_output_path)
                self.cache_records[cache_key] = sort_event_records(cached_records)
                self.cache_records[f"{cache_key}:source"] = [
                    {
                        "source": format_financial_report_cache_source(
                            self.financial_report_output_path,
                            cached_records,
                            target_quarter=target_quarter,
                        )
                    }
                ]
            else:
                self.cache_records[cache_key] = []
                self.cache_records[f"{cache_key}:source"] = [
                    {"source": f"financial report cache not found: {self.financial_report_output_path}"}
                ]
            self.cache_at[cache_key] = now

        records = [record for record in self.cache_records.get(cache_key, []) if is_financial_report_record(record)]
        records = select_display_financial_report_records(records, target_quarter)
        records = filter_monthly_records_by_company_id(records, search_query)
        source_record = self.cache_records.get(
            f"{cache_key}:source",
            [{"source": "MOPS financial report cache"}],
        )[0]
        return records, source_record["source"]

    def _monthly_revenue_summary_markets(self) -> list[str]:
        if self.monthly_revenue_market == "all":
            return ["sii", "otc"]
        return [self.monthly_revenue_market]

    def _current_monthly_revenue_target(self) -> tuple[int, int]:
        default_roc_year, default_month = previous_month_parts()
        return (
            self.monthly_revenue_roc_year or default_roc_year,
            self.monthly_revenue_month or default_month,
        )

    def _current_financial_report_target_quarter(self) -> str:
        return self.financial_report_target_quarter or default_financial_report_target_quarter()

    def _financial_report_update_dates(
        self,
        lookback_days: int | None = None,
        reference_date: date | None = None,
    ) -> list[date]:
        days = max(1, lookback_days or self.financial_report_lookback_days)
        end_date = reference_date or taiwan_now().date()
        start_date = end_date - timedelta(days=days - 1)
        return [start_date + timedelta(days=offset) for offset in range(days)]

    def update_financial_report_cache(
        self,
        target_quarter: str | None = None,
        lookback_days: int | None = None,
        reference_date: date | None = None,
    ) -> dict[str, Any]:
        cache_file = self.financial_report_output_path
        update_started_at = taiwan_now_iso()
        selected_target_quarter = target_quarter or self._current_financial_report_target_quarter()
        query_dates = self._financial_report_update_dates(lookback_days, reference_date)
        announcement_roc_dates = {iso_date_to_roc_label(value) for value in query_dates}
        fetch_summaries: list[dict[str, Any]] = []
        fetch_errors: list[str] = []
        latest_records: list[dict[str, Any]] = []

        try:
            existing_records = self._load_offline_records(cache_file) if cache_file.exists() else []
            before_count = len(dedupe_financial_report_records(existing_records))
            for query_date in query_dates:
                try:
                    summaries = self.crawler.fetch_previous_day_summaries(
                        target_date=query_date,
                        market="all",
                    )
                except Exception as exc:
                    error_text = f"{query_date.isoformat()}: {type(exc).__name__}: {exc}"
                    fetch_errors.append(error_text)
                    fetch_summaries.append(
                        {
                            "query_date": query_date.isoformat(),
                            "row_count": 0,
                            "spoke_date_counts": {},
                            "error": error_text,
                        }
                    )
                    continue

                fetch_summaries.append(
                    {
                        "query_date": query_date.isoformat(),
                        "row_count": len(summaries),
                        "spoke_date_counts": dict(
                            Counter(str(row.get("spoke_date_roc", "")) for row in summaries)
                        ),
                    }
                )
                for summary in summaries:
                    if str(summary.get("spoke_date_roc", "")) not in announcement_roc_dates:
                        continue
                    record = build_financial_report_record(
                        summary,
                        target_quarter=selected_target_quarter,
                        detected_at=update_started_at,
                    )
                    if record is not None:
                        latest_records.append(record)

            if fetch_errors and len(fetch_errors) == len(query_dates):
                raise RuntimeError("; ".join(fetch_errors))

            merged_records = dedupe_financial_report_records([*latest_records, *existing_records])
            merged_records = sort_event_records(merged_records)
            save_records(merged_records, cache_file)
            meta = update_financial_report_cache_meta(
                cache_file,
                merged_records,
                target_quarter=selected_target_quarter,
                query_dates=[value.isoformat() for value in query_dates],
                announcement_roc_dates=sorted(announcement_roc_dates),
                fetch_summaries=fetch_summaries,
                fetch_errors=fetch_errors,
                fetch_error_count=len(fetch_errors),
                degraded=bool(fetch_errors),
                last_update_started_at=update_started_at,
                last_success_at=taiwan_now_iso(),
                last_error=None,
                fetched_count=len(latest_records),
                before_count=before_count,
                after_count=len(merged_records),
                new_count=max(len(merged_records) - before_count, 0),
            )
            self.cache_records[TAB_FINANCIAL_REPORT] = merged_records
            self.cache_records[f"{TAB_FINANCIAL_REPORT}:source"] = [
                {
                    "source": format_financial_report_cache_source(
                        cache_file,
                        merged_records,
                        target_quarter=selected_target_quarter,
                    )
                }
            ]
            self.cache_at[TAB_FINANCIAL_REPORT] = time.monotonic()
            return {
                "ok": not fetch_errors,
                "degraded": bool(fetch_errors),
                "source": format_financial_report_cache_source(
                    cache_file,
                    merged_records,
                    target_quarter=selected_target_quarter,
                ),
                "cache_file": str(cache_file),
                "meta_file": str(financial_report_cache_meta_path(cache_file)),
                "target_quarter": selected_target_quarter,
                "display_quarter": meta.get("display_quarter", ""),
                "query_dates": [value.isoformat() for value in query_dates],
                "fetched_count": len(latest_records),
                "before_count": before_count,
                "after_count": len(merged_records),
                "new_count": max(len(merged_records) - before_count, 0),
                "fetch_error_count": len(fetch_errors),
                "fetch_errors": fetch_errors,
                "updated_at": meta.get("last_success_at", ""),
                "newest_announced_at": meta.get("newest_announced_at", ""),
            }
        except Exception as exc:
            existing_records = self._load_offline_records(cache_file) if cache_file.exists() else []
            update_financial_report_cache_meta(
                cache_file,
                existing_records,
                target_quarter=selected_target_quarter,
                query_dates=[value.isoformat() for value in query_dates],
                announcement_roc_dates=sorted(announcement_roc_dates),
                fetch_summaries=fetch_summaries,
                fetch_errors=fetch_errors,
                fetch_error_count=len(fetch_errors),
                degraded=True,
                last_update_started_at=update_started_at,
                last_failed_at=taiwan_now_iso(),
                last_error=f"{type(exc).__name__}: {exc}",
            )
            raise

    def update_monthly_revenue_summary_cache(self) -> dict[str, Any]:
        cache_file = self.monthly_revenue_output_path
        revenue_roc_year, revenue_month = self._current_monthly_revenue_target()
        target_data_month = f"{revenue_roc_year:03d}/{revenue_month:02d}"
        target_display_data_month = f"{revenue_roc_year + 1911}/{revenue_month:02d}"
        markets = self._monthly_revenue_summary_markets()

        try:
            existing_records = self._load_offline_records(cache_file) if cache_file.exists() else []
            before_count = len(existing_records)
            fetch_result = self.monthly_revenue_crawler.fetch_monthly_revenue_summary_with_fallbacks(
                roc_year=revenue_roc_year,
                month=revenue_month,
                markets=markets,
            )
            latest_records = list(fetch_result.get("records", []))
            market_results = list(fetch_result.get("market_results", []))
            latest_records, fallback_skipped_existing_primary_count = (
                filter_monthly_revenue_fallback_duplicates(existing_records, latest_records)
            )
            merged_records, new_records = append_new_monthly_revenue_records(
                existing_records,
                latest_records,
            )
            save_records(merged_records, cache_file)

            market_failure_count = sum(1 for result in market_results if not result.get("ok"))
            meta = update_monthly_revenue_cache_meta(
                cache_file,
                merged_records,
                target_data_month=target_data_month,
                target_display_data_month=target_display_data_month,
                markets=markets,
                market_results=market_results,
                market_failure_count=market_failure_count,
                degraded=market_failure_count > 0,
                fallback_skipped_existing_primary_count=fallback_skipped_existing_primary_count,
                last_success_at=taiwan_now_iso(),
                last_error=None,
            )
            self.cache_records[MONTHLY_REVENUE_SUMMARY_CACHE_KEY] = merged_records
            self.cache_at[MONTHLY_REVENUE_SUMMARY_CACHE_KEY] = time.monotonic()
            source = format_monthly_revenue_cache_source(cache_file, merged_records)
            return {
                "ok": market_failure_count == 0,
                "degraded": market_failure_count > 0,
                "source": source,
                "cache_file": str(cache_file),
                "meta_file": str(monthly_revenue_cache_meta_path(cache_file)),
                "fetched_count": len(latest_records),
                "before_count": before_count,
                "after_count": len(merged_records),
                "new_count": len(new_records),
                "markets": markets,
                "market_results": market_results,
                "market_failure_count": market_failure_count,
                "fallback_skipped_existing_primary_count": fallback_skipped_existing_primary_count,
                "data_month": f"{revenue_roc_year}/{revenue_month:02d}",
                "target_display_data_month": target_display_data_month,
                "display_data_month": meta.get("display_data_month", ""),
                "updated_at": meta.get("last_success_at", ""),
            }
        except Exception as exc:
            existing_records = self._load_offline_records(cache_file) if cache_file.exists() else []
            update_monthly_revenue_cache_meta(
                cache_file,
                existing_records,
                target_data_month=target_data_month,
                target_display_data_month=target_display_data_month,
                markets=markets,
                degraded=True,
                last_failed_at=taiwan_now_iso(),
                last_error=f"{type(exc).__name__}: {exc}",
            )
            raise

    def update_monthly_revenue_cache(self) -> dict[str, Any]:
        cache_file = self.monthly_revenue_output_path
        existing_records = self._load_offline_records(cache_file) if cache_file.exists() else []
        before_count = len(dedupe_event_records(existing_records))
        revenue_roc_year, revenue_month = self._current_monthly_revenue_target()

        latest_records = self.monthly_revenue_crawler.fetch_dashboard_records(
            company_ids=self.monthly_revenue_company_ids,
            revenue_roc_year=revenue_roc_year,
            revenue_month=revenue_month,
            market=self.monthly_revenue_market,
            include_realtime=True,
            include_historical_material=False,
            include_company_revenue=bool(self.monthly_revenue_company_ids),
            include_self_profit=bool(self.monthly_revenue_company_ids),
            max_realtime_items=self.max_items,
        )
        merged_records = sort_event_records(
            dedupe_event_records([*latest_records, *existing_records])
        )
        save_records(merged_records, cache_file)

        self.cache_records[TAB_MONTHLY_REVENUE] = merged_records
        self.cache_at[TAB_MONTHLY_REVENUE] = time.monotonic()
        return {
            "ok": True,
            "source": "MOPS realtime material info + company monthly revenue",
            "cache_file": str(cache_file),
            "fetched_count": len(latest_records),
            "before_count": before_count,
            "after_count": len(merged_records),
            "new_count": max(len(merged_records) - before_count, 0),
            "company_ids": self.monthly_revenue_company_ids,
            "data_month": f"{revenue_roc_year}/{revenue_month:02d}",
            "display_data_month": f"{revenue_roc_year + 1911}/{revenue_month:02d}",
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    def _fetch_latest_listed_otc_records(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for market in ("sii", "otc"):
            records.extend(
                self.crawler.fetch_latest_with_details(
                    max_items=self.max_items,
                    market=market,
                )
            )
        return sort_records_by_spoke_time(dedupe_records(records))

    def update_latest_cache(self) -> dict[str, Any]:
        """Fetch realtime rows and merge them into the persistent range cache."""
        now = time.monotonic()
        if (
            self.last_update_at > 0
            and now - self.last_update_at < self.update_min_interval_seconds
            and self.last_update_result is not None
        ):
            return {
                **self.last_update_result,
                "ok": True,
                "skipped": True,
                "reason": "cooldown",
                "retry_after_seconds": int(self.update_min_interval_seconds - (now - self.last_update_at)),
            }

        cache_file = self.range_cache_file or find_range_cache_file() or default_range_output_path()
        update_started_at = taiwan_now_iso()
        try:
            existing_records = self._load_offline_records(cache_file) if cache_file.exists() else []
            existing_records = enrich_records_for_cache(existing_records)
            before_count = len(dedupe_records(existing_records))

            latest_records = self._fetch_latest_listed_otc_records()
            latest_records = enrich_records_for_cache(latest_records)
            merged_records = sort_records_by_spoke_time(
                dedupe_records([*latest_records, *existing_records])
            )
            save_records(merged_records, cache_file)
        except Exception as exc:
            update_range_cache_meta(
                cache_file,
                last_update_started_at=update_started_at,
                last_update_failed_at=taiwan_now_iso(),
                last_error=str(exc),
            )
            raise

        self.range_cache_file = cache_file
        self.last_update_at = now
        self.cache_records.pop(MODE_RECENT_FINANCIAL, None)
        self.cache_at.pop(MODE_RECENT_FINANCIAL, None)
        meta = update_range_cache_meta(
            cache_file,
            merged_records,
            last_update_started_at=update_started_at,
            last_success_at=taiwan_now_iso(),
            last_error=None,
            fetched_count=len(latest_records),
            before_count=before_count,
            after_count=len(merged_records),
            new_count=max(len(merged_records) - before_count, 0),
            update_source="MOPS realtime endpoint (sii+otc)",
        )

        result = {
            "ok": True,
            "skipped": False,
            "source": "MOPS realtime endpoint (sii+otc)",
            "cache_file": str(cache_file),
            "meta_file": str(range_cache_meta_path(cache_file)),
            "fetched_count": len(latest_records),
            "before_count": before_count,
            "after_count": len(merged_records),
            "new_count": max(len(merged_records) - before_count, 0),
            "updated_at": meta.get("last_success_at", ""),
            "newest_spoke_at": meta.get("newest_spoke_at", ""),
            "next_allowed_after_seconds": self.update_min_interval_seconds,
        }
        if self.monthly_revenue_company_ids:
            result["monthly_revenue"] = self.update_monthly_revenue_cache()
        self.last_update_result = result
        return result


def build_handler(dashboard: DashboardServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self._handle_request()

        def do_POST(self) -> None:
            self._handle_request()

        def _handle_request(self) -> None:
            parsed_url = urlparse(self.path)
            path = parsed_url.path
            query_values = parse_qs(parsed_url.query)
            query = {key: values[0] for key, values in query_values.items() if values}
            active_tab = query.get("tab", TAB_MATERIAL_INFO)
            if active_tab not in TAB_CHOICES:
                active_tab = TAB_MATERIAL_INFO
            mode = query.get("mode", dashboard.mode)
            if mode not in MODE_CHOICES:
                mode = dashboard.mode
            if mode == MODE_RECENT_FINANCIAL:
                category = CATEGORY_FINANCIAL_SELF_REPORT
            category = query.get("category", dashboard.category)
            if mode == MODE_RECENT_FINANCIAL:
                category = CATEGORY_FINANCIAL_SELF_REPORT
            if category not in CATEGORY_CHOICES:
                category = dashboard.category
            search_query = query.get("q", "").strip()
            if path == "/health":
                self._send_text("ok")
                return

            if path == UPDATE_API_PATH:
                if not self._is_update_authorized(query):
                    self._send_json(
                        {
                            "ok": False,
                            "error": "unauthorized",
                            "message": f"Set {UPDATE_TOKEN_ENV} and send it with Authorization: Bearer <token>.",
                        },
                        status=401,
                    )
                    return
                try:
                    self._send_json(dashboard.update_latest_cache())
                except Exception as exc:  # pragma: no cover - live endpoint.
                    self._send_json(
                        {
                            "ok": False,
                            "error": "update_failed",
                            "message": str(exc),
                        },
                        status=502,
                    )
                return

            if path == MONTHLY_REVENUE_UPDATE_API_PATH:
                if not self._is_update_authorized(query):
                    self._send_json(
                        {
                            "ok": False,
                            "error": "unauthorized",
                            "message": f"Set {UPDATE_TOKEN_ENV} and send it with Authorization: Bearer <token>.",
                        },
                        status=401,
                    )
                    return
                try:
                    self._send_json(dashboard.update_monthly_revenue_summary_cache())
                except Exception as exc:  # pragma: no cover - live endpoint.
                    self._send_json(
                        {
                            "ok": False,
                            "error": "monthly_revenue_update_failed",
                            "message": str(exc),
                        },
                        status=502,
                    )
                return

            if path == FINANCIAL_REPORT_UPDATE_API_PATH:
                if not self._is_update_authorized(query):
                    self._send_json(
                        {
                            "ok": False,
                            "error": "unauthorized",
                            "message": f"Set {UPDATE_TOKEN_ENV} and send it with Authorization: Bearer <token>.",
                        },
                        status=401,
                    )
                    return
                try:
                    lookback_days = None
                    if query.get("lookback_days"):
                        lookback_days = max(1, int(query["lookback_days"]))
                    self._send_json(
                        dashboard.update_financial_report_cache(
                            target_quarter=query.get("target_quarter") or None,
                            lookback_days=lookback_days,
                        )
                    )
                except Exception as exc:  # pragma: no cover - live endpoint.
                    self._send_json(
                        {
                            "ok": False,
                            "error": "financial_report_update_failed",
                            "message": str(exc),
                        },
                        status=502,
                    )
                return

            if path in {"/", "/index.html"}:
                mode = MODE_RECENT_FINANCIAL
                category = CATEGORY_FINANCIAL_SELF_REPORT

            try:
                if path in {"/", "/index.html", "/api/news"} and active_tab == TAB_MONTHLY_REVENUE:
                    records, source = dashboard._get_monthly_revenue_records(
                        search_query=search_query,
                    )
                elif path in {"/", "/index.html", "/api/news"} and active_tab == TAB_FINANCIAL_REPORT:
                    records, source = dashboard._get_financial_report_records(
                        search_query=search_query,
                    )
                else:
                    records, source = dashboard.get_records(
                        mode=mode,
                        category=category,
                        search_query=search_query,
                    )
            except Exception as exc:  # pragma: no cover - exercised manually.
                self.send_response(502)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(f"Failed to load dashboard data: {exc}".encode("utf-8"))
                return

            if path == "/api/news":
                self._send_json(records)
                return

            if path in {"/", "/index.html"}:
                generated_at = time.strftime("%Y-%m-%d %H:%M:%S")
                self._send_html(
                    render_dashboard(
                        records,
                        generated_at,
                        source,
                        mode=mode,
                        category=category,
                        search_query=search_query,
                        recent_days=dashboard.recent_days,
                        active_tab=active_tab,
                    )
                )
                return

            self.send_error(404)

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _is_update_authorized(self, query: dict[str, str]) -> bool:
            client_host = self.client_address[0] if self.client_address else ""
            return is_update_request_authorized(
                headers=self.headers,
                query=query,
                client_host=client_host,
                allow_unprotected_local_update=dashboard.allow_unprotected_local_update,
            )

        def _send_html(self, content: str) -> None:
            payload = content.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _send_json(self, data: Any, status: int = 200) -> None:
            payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _send_text(self, content: str) -> None:
            payload = content.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return Handler


def crawl_command(args: argparse.Namespace) -> None:
    crawler = MopsCrawler(request_interval_seconds=args.request_interval)
    records = crawler.fetch_latest_with_details(max_items=args.max_items, market=args.market)
    records = filter_records_by_category(records, args.category)
    save_records(records, args.output)
    print(f"Saved {len(records)} records to {args.output}")


def crawl_previous_command(args: argparse.Namespace) -> None:
    crawler = MopsCrawler(request_interval_seconds=args.request_interval)
    target_date = args.date or default_previous_day().isoformat()
    records = crawler.fetch_previous_day_with_details(
        target_date=target_date,
        max_items=args.max_items,
        market=args.market,
    )
    records = filter_records_by_category(records, args.category)
    save_records(records, args.output)
    print(f"Saved {len(records)} records for {target_date} to {args.output}")


def crawl_range_command(args: argparse.Namespace) -> None:
    crawler = MopsCrawler(request_interval_seconds=args.request_interval)
    start_date = args.start_date or default_month_start().isoformat()
    end_date = args.end_date or date.today().isoformat()
    records = crawler.fetch_date_range_records(
        start_date=start_date,
        end_date=end_date,
        max_items_per_day=args.max_items_per_day,
        market=args.market,
        include_details=args.include_details,
        day_interval_seconds=args.day_interval,
    )
    records = filter_records_by_category(records, args.category)
    records = filter_records_by_company_id(records, args.query)
    save_records(records, args.output)
    detail_mode = "with detail pages" if args.include_details else "list pages only"
    print(f"Saved {len(records)} records for {start_date} to {end_date} ({detail_mode}) to {args.output}")


def crawl_monthly_revenue_command(args: argparse.Namespace) -> None:
    crawler = MonthlyRevenueCrawler(request_interval_seconds=args.request_interval)
    start_date = args.start_date or default_month_start().isoformat()
    end_date = args.end_date or date.today().isoformat()
    reference_date = date.fromisoformat(end_date)
    default_roc_year, default_month = previous_month_parts(reference_date)
    revenue_roc_year = args.revenue_roc_year or default_roc_year
    revenue_month = args.revenue_month or default_month
    company_ids = normalize_company_ids(args.company_ids)

    records = crawler.fetch_dashboard_records(
        company_ids=company_ids,
        revenue_roc_year=revenue_roc_year,
        revenue_month=revenue_month,
        material_start_date=start_date,
        material_end_date=end_date,
        market=args.market,
        include_realtime=args.include_realtime,
        include_historical_material=True,
        include_company_revenue=not args.skip_company_revenue,
        include_self_profit=not args.skip_self_profit,
        max_realtime_items=args.max_realtime_items,
    )
    records = filter_monthly_records_by_company_id(records, args.query)
    save_records(records, args.output)
    print(
        f"Saved {len(records)} monthly revenue/financial records "
        f"for {','.join(company_ids) or 'no-company'} "
        f"({start_date} to {end_date}, data_month={revenue_roc_year}/{revenue_month:02d}) "
        f"to {args.output}"
    )


def enrich_cache_command(args: argparse.Namespace) -> None:
    records = json.loads(args.path.read_text(encoding="utf-8"))
    records = enrich_records_for_cache(records, recompute_eps=args.recompute_eps)
    output_path = args.output or args.path
    save_records(records, output_path)

    financial_count = sum(1 for record in records if record.get("is_financial_self_report"))
    eps_count = sum(
        1
        for record in records
        if isinstance(record.get("eps_metrics"), dict)
        and record["eps_metrics"].get("has_eps")
    )
    print(
        f"Saved {len(records)} enriched records to {output_path} "
        f"(financial_self_report={financial_count}, has_eps={eps_count})"
    )


def serve_command(args: argparse.Namespace) -> None:
    if env_bool(SEED_CACHE_ON_START_ENV, True):
        seeded_paths = seed_persistent_cache_files(dashboard_raw_data_dir())
        if seeded_paths:
            print(
                "Seeded persistent cache files: "
                + ", ".join(str(path) for path in seeded_paths)
            )

    crawler = MopsCrawler(request_interval_seconds=args.request_interval)
    monthly_crawler = MonthlyRevenueCrawler(request_interval_seconds=args.request_interval)
    dashboard = DashboardServer(
        crawler=crawler,
        max_items=args.max_items,
        refresh_seconds=args.refresh_seconds,
        output_path=args.output,
        previous_output_path=args.previous_output,
        mode=args.mode,
        category=args.category,
        range_cache_file=args.range_cache_file,
        recent_days=args.recent_days,
        target_date=args.date,
        offline_file=args.offline_file,
        update_min_interval_seconds=args.update_min_interval,
        allow_unprotected_local_update=args.allow_unprotected_local_update,
        monthly_revenue_crawler=monthly_crawler,
        monthly_revenue_output_path=args.monthly_revenue_cache_file,
        monthly_revenue_company_ids=normalize_company_ids(args.monthly_revenue_company_ids),
        monthly_revenue_market=args.monthly_revenue_market,
        monthly_revenue_roc_year=args.monthly_revenue_year,
        monthly_revenue_month=args.monthly_revenue_month,
        financial_report_output_path=args.financial_report_cache_file,
        financial_report_target_quarter=args.financial_report_target_quarter,
        financial_report_lookback_days=args.financial_report_lookback_days,
    )
    server = ThreadingHTTPServer((args.host, args.port), build_handler(dashboard))
    print(f"Serving dashboard at http://{args.host}:{args.port}")
    server.serve_forever()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TWSE/MOPS material-information dashboard")
    parser.add_argument("--env-file", default=PROJECT_ROOT / ".env", type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)

    crawl = subparsers.add_parser("crawl", help="Fetch latest MOPS rows with detail content")
    crawl.add_argument("--max-items", type=int, default=0)
    crawl.add_argument("--market", default="all", choices=["all", "sii", "otc", "rotc", "pub"])
    crawl.add_argument("--category", default=CATEGORY_ALL, choices=CATEGORY_CHOICES)
    crawl.add_argument("--request-interval", type=float, default=DEFAULT_REQUEST_INTERVAL_SECONDS)
    crawl.add_argument("--output", type=Path, default=default_output_path())
    crawl.set_defaults(func=crawl_command)

    crawl_previous = subparsers.add_parser(
        "crawl-previous",
        help="Fetch previous-day MOPS rows with detail content",
    )
    crawl_previous.add_argument("--date", help="Target MOPS date, e.g. 2026-06-26")
    crawl_previous.add_argument("--max-items", type=int, default=10)
    crawl_previous.add_argument("--market", default="all", choices=["all", "sii", "otc", "rotc", "pub"])
    crawl_previous.add_argument("--category", default=CATEGORY_ALL, choices=CATEGORY_CHOICES)
    crawl_previous.add_argument("--request-interval", type=float, default=DEFAULT_REQUEST_INTERVAL_SECONDS)
    crawl_previous.add_argument("--output", type=Path, default=default_previous_output_path())
    crawl_previous.set_defaults(func=crawl_previous_command)

    crawl_range = subparsers.add_parser(
        "crawl-range",
        help="Fetch date-based MOPS rows for an inclusive date range",
    )
    crawl_range.add_argument("--start-date", help="Start date, e.g. 2026-06-01")
    crawl_range.add_argument("--end-date", help="End date, e.g. 2026-06-27")
    crawl_range.add_argument("--max-items-per-day", type=int, default=0)
    crawl_range.add_argument("--market", default="all", choices=["all", "sii", "otc", "rotc", "pub"])
    crawl_range.add_argument("--category", default=CATEGORY_ALL, choices=CATEGORY_CHOICES)
    crawl_range.add_argument("--query", default="", help="Optional stock/company code filter")
    crawl_range.add_argument("--request-interval", type=float, default=DEFAULT_REQUEST_INTERVAL_SECONDS)
    crawl_range.add_argument("--day-interval", type=float, default=DEFAULT_DAY_INTERVAL_SECONDS)
    crawl_range.add_argument(
        "--include-details",
        action="store_true",
        help="Also POST each row's detail page; slower and much more request-heavy",
    )
    crawl_range.add_argument("--output", type=Path, default=default_range_output_path())
    crawl_range.set_defaults(func=crawl_range_command)

    crawl_monthly = subparsers.add_parser(
        "crawl-monthly-revenue",
        help="Fetch early monthly revenue and financial-report signals",
    )
    crawl_monthly.add_argument("--company-ids", default="4739", help="Comma-separated stock codes")
    crawl_monthly.add_argument("--start-date", help="Material-info start date, e.g. 2026-05-05")
    crawl_monthly.add_argument("--end-date", help="Material-info end date, e.g. 2026-05-15")
    crawl_monthly.add_argument("--revenue-roc-year", type=int, help="Revenue ROC year, e.g. 115")
    crawl_monthly.add_argument("--revenue-month", type=int, help="Revenue data month, e.g. 4")
    crawl_monthly.add_argument("--market", default="all", choices=["all", "sii", "otc", "rotc", "pub"])
    crawl_monthly.add_argument("--query", default="", help="Optional stock/company code filter")
    crawl_monthly.add_argument("--request-interval", type=float, default=DEFAULT_REQUEST_INTERVAL_SECONDS)
    crawl_monthly.add_argument("--max-realtime-items", type=int, default=0)
    crawl_monthly.add_argument(
        "--include-realtime",
        action="store_true",
        help="Also check current realtime material-info rows",
    )
    crawl_monthly.add_argument(
        "--skip-company-revenue",
        action="store_true",
        help="Skip t05st10_ifrs single-company monthly revenue",
    )
    crawl_monthly.add_argument(
        "--skip-self-profit",
        action="store_true",
        help="Skip t138sb02 monthly/quarterly self-profit pages",
    )
    crawl_monthly.add_argument("--output", type=Path, default=default_monthly_revenue_output_path())
    crawl_monthly.set_defaults(func=crawl_monthly_revenue_command)

    enrich_cache = subparsers.add_parser(
        "enrich-cache",
        help="Write derived category and EPS metrics back to an existing JSON cache",
    )
    enrich_cache.add_argument("path", type=Path)
    enrich_cache.add_argument("--output", type=Path)
    enrich_cache.add_argument(
        "--recompute-eps",
        action="store_true",
        help="Ignore existing eps_metrics and parse EPS from source text again",
    )
    enrich_cache.set_defaults(func=enrich_cache_command)

    serve = subparsers.add_parser("serve", help="Run the local demo dashboard")
    serve.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    serve.add_argument("--port", type=int, default=env_int("PORT", 8000))
    serve.add_argument("--mode", default=MODE_RECENT_FINANCIAL, choices=MODE_CHOICES)
    serve.add_argument("--category", default=CATEGORY_FINANCIAL_SELF_REPORT, choices=CATEGORY_CHOICES)
    serve.add_argument("--recent-days", type=int, default=env_int(RECENT_DAYS_ENV, DEFAULT_RECENT_DAYS))
    serve.add_argument("--range-cache-file", type=Path, default=env_path(RANGE_CACHE_FILE_ENV))
    serve.add_argument("--date", help="Target date for previous mode, e.g. 2026-06-26")
    serve.add_argument("--max-items", type=int, default=0)
    serve.add_argument("--refresh-seconds", type=int, default=DEFAULT_REFRESH_SECONDS)
    serve.add_argument("--update-min-interval", type=int, default=env_int(UPDATE_MIN_INTERVAL_ENV, DEFAULT_UPDATE_MIN_INTERVAL_SECONDS))
    serve.add_argument(
        "--monthly-revenue-company-ids",
        default=os.environ.get(MONTHLY_REVENUE_COMPANY_IDS_ENV, ""),
        help="Comma-separated watchlist for t05st10_ifrs single-company monthly revenue",
    )
    serve.add_argument(
        "--monthly-revenue-cache-file",
        type=Path,
        default=env_path(MONTHLY_REVENUE_CACHE_FILE_ENV) or default_monthly_revenue_output_path(),
    )
    serve.add_argument("--monthly-revenue-market", default="all", choices=["all", "sii", "otc", "rotc", "pub"])
    serve.add_argument("--monthly-revenue-year", type=int, help="ROC year for the watched monthly revenue data")
    serve.add_argument("--monthly-revenue-month", type=int, help="Month for the watched monthly revenue data")
    serve.add_argument(
        "--financial-report-cache-file",
        type=Path,
        default=env_path(FINANCIAL_REPORT_CACHE_FILE_ENV) or default_financial_report_output_path(),
    )
    serve.add_argument(
        "--financial-report-target-quarter",
        default=os.environ.get(FINANCIAL_REPORT_TARGET_QUARTER_ENV, "").strip() or None,
        help="Target quarter for financial-report updates, e.g. 2026Q1",
    )
    serve.add_argument(
        "--financial-report-lookback-days",
        type=int,
        default=env_int(FINANCIAL_REPORT_LOOKBACK_DAYS_ENV, DEFAULT_FINANCIAL_REPORT_LOOKBACK_DAYS),
        help="Number of recent MOPS query dates scanned by financial-report updates",
    )
    serve.add_argument(
        "--allow-unprotected-local-update",
        action="store_true",
        default=env_bool(DEV_ALLOW_UNPROTECTED_UPDATE_ENV, False),
        help="Development only: allow localhost update calls when TWSE_DASHBOARD_UPDATE_TOKEN is unset",
    )
    serve.add_argument("--request-interval", type=float, default=DEFAULT_REQUEST_INTERVAL_SECONDS)
    serve.add_argument("--output", type=Path, default=default_output_path())
    serve.add_argument("--previous-output", type=Path, default=default_previous_output_path())
    serve.add_argument("--offline-file", type=Path)
    serve.set_defaults(func=serve_command)

    return parser


def main() -> None:
    bootstrap_parser = argparse.ArgumentParser(add_help=False)
    bootstrap_parser.add_argument("--env-file", default=PROJECT_ROOT / ".env", type=Path)
    bootstrap_args, _ = bootstrap_parser.parse_known_args()
    load_dotenv(bootstrap_args.env_file, override=False)

    parser = build_parser()
    args = parser.parse_args()
    load_dotenv(args.env_file, override=False)
    args.func(args)


if __name__ == "__main__":
    main()
