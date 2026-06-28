"""Monthly revenue and early financial-signal crawlers for MOPS."""

from __future__ import annotations

import csv
import io
import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from bs4.element import Tag

from mops_crawler import (
    DEFAULT_REQUEST_INTERVAL_SECONDS,
    MOPS_BASE_URL,
    MopsCrawler,
    clean_text,
    iso_date_to_roc_parts,
    iter_dates,
    normalize_time,
    parse_detail,
    parse_detail_assignments,
    roc_date_to_iso,
)

HISTORICAL_MATERIAL_AJAX_PATH = "/mops/web/ajax_t05st01"
MOPS_MONTHLY_REVENUE_AJAX_PATH = "/mops/web/ajax_t21sc04_ifrs"
MOPS_DOWNLOAD_PATH = "/server-java/FileDownLoad"
COMPANY_MONTHLY_REVENUE_AJAX_PATH = "/mops/web/ajax_t05st10_ifrs"
SELF_PROFIT_MONTHLY_AJAX_PATH = "/mops/web/ajax_t138sb02_q1"
SELF_PROFIT_QUARTERLY_AJAX_PATH = "/mops/web/ajax_t138sb02_q2"
MOPS_MONTHLY_REVENUE_PAGE = "/mops/web/t21sc04_ifrs"
COMPANY_MONTHLY_REVENUE_PAGE = "/mops/web/t05st10_ifrs"
SELF_PROFIT_MONTHLY_PAGE = "/mops/web/t138sb02_q1"
SELF_PROFIT_QUARTERLY_PAGE = "/mops/web/t138sb02_q2"
TWSE_LISTED_MONTHLY_REVENUE_OPENAPI_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap05_L"
TPEX_OTC_MONTHLY_REVENUE_OPENAPI_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O"

SIGNAL_KEYWORDS = (
    "營收",
    "合併營收",
    "自結",
    "財務報告",
    "董事會通過",
    "每股盈餘",
    "EPS",
)
MONTHLY_REVENUE_KEYWORDS = ("營收", "合併營收", "月營收")
FINANCIAL_REPORT_KEYWORDS = ("財務報告", "董事會通過", "每股盈餘", "EPS")
NOISE_SUBJECT_KEYWORDS = ("股東常會", "除權息", "取得", "處分", "背書保證")
MARKET_LABELS = {
    "sii": "上市",
    "otc": "上櫃",
    "rotc": "興櫃",
    "pub": "公開發行",
    "all": "全部",
}

NUMBER_PATTERN = re.compile(r"-?\d[\d,]*(?:\.\d+)?")
REVENUE_PERIOD_PATTERN = re.compile(r"民國\s*(\d{2,3})\s*年\s*(\d{1,2})\s*月")
COMPANY_INFO_PATTERN = re.compile(r"\(([^)]+)\)\s*([0-9A-Z]+)?\s*([^\s　]+)")
DETAIL_FIELD_LABELS = {
    "序號",
    "發言日期",
    "發言時間",
    "發言人",
    "發言人職稱",
    "發言人電話",
    "主旨",
    "符合條款",
    "事實發生日",
    "說明",
}


def current_detected_at() -> str:
    """Return local ISO timestamp for the crawler's first-observed time."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def previous_month_parts(today: date | None = None) -> tuple[int, int]:
    """Return ROC year/month for the previous calendar month."""
    current = today or date.today()
    year = current.year
    month = current.month - 1
    if month == 0:
        year -= 1
        month = 12
    return year - 1911, month


def normalize_company_ids(value: str | list[str] | tuple[str, ...] | None) -> list[str]:
    """Normalize a comma-separated watchlist into stock-code strings."""
    if value is None:
        return []
    if isinstance(value, str):
        parts = re.split(r"[,;\s]+", value)
    else:
        parts = list(value)
    return [part.strip() for part in parts if part and part.strip()]


def clean_number(value: str) -> str:
    return value.replace(",", "").strip()


def first_number(value: str) -> str:
    match = NUMBER_PATTERN.search(value)
    return clean_number(match.group(0)) if match else ""


def clean_monthly_value(value: Any) -> str:
    text = "" if value is None else str(value).strip()
    if text in {"", "-", "--", "N/A", "NA"}:
        return ""
    return clean_number(text)


def decode_csv_content(content: bytes) -> str:
    if content.startswith(b"\xef\xbb\xbf"):
        return content.decode("utf-8-sig")
    for encoding in ("utf-8", "big5", "cp950"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def normalize_roc_date(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    compact = re.sub(r"\D", "", text)
    if len(compact) == 7:
        return f"{compact[:3]}/{compact[3:5]}/{compact[5:7]}"
    if "/" in text:
        parts = [part for part in text.split("/") if part]
        if len(parts) == 3:
            return f"{int(parts[0]):03d}/{int(parts[1]):02d}/{int(parts[2]):02d}"
    return text


def normalize_data_month(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    compact = re.sub(r"\D", "", text)
    if len(compact) >= 5:
        return f"{int(compact[:-2]):03d}/{int(compact[-2:]):02d}"
    if "/" in text:
        parts = [part for part in text.split("/") if part]
        if len(parts) >= 2:
            return f"{int(parts[0]):03d}/{int(parts[1]):02d}"
    match = REVENUE_PERIOD_PATTERN.search(text)
    if match:
        return f"{int(match.group(1)):03d}/{int(match.group(2)):02d}"
    return text


def data_month_parts(value: Any) -> tuple[int | None, int | None]:
    normalized = normalize_data_month(value)
    if "/" not in normalized:
        return None, None
    year_text, month_text = normalized.split("/", 1)
    try:
        return int(year_text), int(month_text)
    except ValueError:
        return None, None


def filter_monthly_revenue_records_by_data_month(
    records: list[dict[str, Any]],
    roc_year: int,
    month: int,
) -> list[dict[str, Any]]:
    expected = f"{int(roc_year):03d}/{int(month):02d}"
    return [
        record
        for record in records
        if normalize_data_month(record.get("data_month")) == expected
    ]


def monthly_revenue_change_key(record: dict[str, Any]) -> tuple[str, ...]:
    return (
        str(record.get("source_type", "")),
        str(record.get("market", "")),
        str(record.get("company_id", "")),
        str(record.get("data_month", "")),
        str(record.get("monthly_revenue", "")),
        str(record.get("previous_month_revenue", "")),
        str(record.get("last_year_month_revenue", "")),
        str(record.get("mom_percent", "")),
        str(record.get("yoy_percent", "")),
        str(record.get("ytd_revenue", "")),
        str(record.get("ytd_yoy_percent", "")),
        str(record.get("note", "")),
    )


def append_new_monthly_revenue_records(
    existing_records: list[dict[str, Any]],
    latest_records: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    seen = {monthly_revenue_change_key(record) for record in existing_records}
    new_records: list[dict[str, Any]] = []
    for record in latest_records:
        key = monthly_revenue_change_key(record)
        if key in seen:
            continue
        seen.add(key)
        new_records.append(record)
    return sort_event_records([*new_records, *existing_records]), new_records


def row_value(row: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def make_monthly_summary_record(
    row: dict[str, Any],
    *,
    source_type: str,
    source_label: str,
    market: str,
    market_label: str,
    detected_at: str,
    source_url: str,
) -> dict[str, Any]:
    data_month = normalize_data_month(row_value(row, "資料年月", "data_month"))
    roc_year, month = data_month_parts(data_month)
    company_id = row_value(row, "公司代號", "company_id")
    company_name = row_value(row, "公司名稱", "company_name")
    note = row_value(row, "備註", "note")
    fields = {
        "出表日期": normalize_roc_date(row_value(row, "出表日期", "report_date")),
        "資料年月": data_month,
        "產業別": row_value(row, "產業別", "industry"),
        "本月": clean_monthly_value(row_value(row, "營業收入-當月營收", "monthly_revenue")),
        "上月": clean_monthly_value(row_value(row, "營業收入-上月營收", "previous_month_revenue")),
        "去年同期": clean_monthly_value(row_value(row, "營業收入-去年當月營收", "last_year_month_revenue")),
        "MOM%": clean_monthly_value(row_value(row, "營業收入-上月比較增減(%)", "mom_percent")),
        "YOY%": clean_monthly_value(row_value(row, "營業收入-去年同月增減(%)", "yoy_percent")),
        "本年累計": clean_monthly_value(row_value(row, "累計營業收入-當月累計營收", "ytd_revenue")),
        "去年累計": clean_monthly_value(row_value(row, "累計營業收入-去年累計營收", "last_year_ytd_revenue")),
        "累計YOY%": clean_monthly_value(row_value(row, "累計營業收入-前期比較增減(%)", "ytd_yoy_percent")),
        "備註 / 營收變化原因說明": note,
    }
    title = f"{company_id} {company_name} {data_month} 月營收".strip()
    description = "\n".join(f"{key}: {value}" for key, value in fields.items() if value)
    return {
        "source_type": source_type,
        "source_label": source_label,
        "event_type": "monthly_revenue",
        "company_id": company_id,
        "company_name": company_name,
        "industry": fields["產業別"],
        "market": market,
        "market_label": market_label,
        "report_date": fields["出表日期"],
        "data_month": data_month,
        "roc_year": roc_year,
        "month": month,
        "title": title,
        "subject": title,
        "detected_at": detected_at,
        "event_time": detected_at,
        "monthly_revenue": fields["本月"],
        "previous_month_revenue": fields["上月"],
        "last_year_month_revenue": fields["去年同期"],
        "mom_percent": fields["MOM%"],
        "yoy_percent": fields["YOY%"],
        "ytd_revenue": fields["本年累計"],
        "last_year_ytd_revenue": fields["去年累計"],
        "ytd_yoy_percent": fields["累計YOY%"],
        "note": note,
        "fields": fields,
        "detail": {"fields": fields, "description": description},
        "source_url": source_url,
    }


def parse_mops_monthly_revenue_csv(
    content: bytes,
    *,
    market: str,
    detected_at: str,
    source_url: str,
) -> list[dict[str, Any]]:
    text = decode_csv_content(content)
    reader = csv.DictReader(io.StringIO(text))
    market_label = MARKET_LABELS.get(market, market)
    return [
        make_monthly_summary_record(
            row,
            source_type="mops_monthly_revenue_summary",
            source_label=f"{market_label}月營收彙總",
            market=market,
            market_label=market_label,
            detected_at=detected_at,
            source_url=source_url,
        )
        for row in reader
        if row.get("公司代號")
    ]


def parse_openapi_monthly_revenue_rows(
    rows: list[Any],
    *,
    source_type: str,
    source_label: str,
    market: str,
    detected_at: str,
    source_url: str,
) -> list[dict[str, Any]]:
    market_label = MARKET_LABELS.get(market, market)
    return sort_event_records(
        [
            make_monthly_summary_record(
                row,
                source_type=source_type,
                source_label=source_label,
                market=market,
                market_label=market_label,
                detected_at=detected_at,
                source_url=source_url,
            )
            for row in rows
            if isinstance(row, dict) and row.get("公司代號")
        ]
    )


def material_text(record: dict[str, Any]) -> str:
    detail = record.get("detail") or record.get("detail_preview") or {}
    fields = detail.get("fields") or {}
    return "\n".join(
        [
            str(record.get("subject", "")),
            str(detail.get("description", "")),
            *[str(value) for value in fields.values()],
        ]
    )


def looks_like_monthly_or_financial_signal(record: dict[str, Any]) -> bool:
    """Return True when a material-info row looks useful for this tab."""
    text = material_text(record)
    subject = str(record.get("subject", ""))
    normalized = text.casefold()

    has_signal = any(keyword.casefold() in normalized for keyword in SIGNAL_KEYWORDS)
    if not has_signal:
        return False

    if "董事會通過" in subject and "財務報告" not in text:
        return False
    if any(keyword in subject for keyword in NOISE_SUBJECT_KEYWORDS) and not any(
        keyword in text for keyword in ("營收", "財務報告", "每股盈餘", "EPS")
    ):
        return False
    return True


def classify_signal_type(record: dict[str, Any]) -> tuple[str, str]:
    text = material_text(record)
    subject = str(record.get("subject", ""))
    if any(keyword in text for keyword in MONTHLY_REVENUE_KEYWORDS):
        return "material_revenue", "重大訊息-營收"
    if "財務報告" in text or "董事會通過" in subject:
        return "financial_report", "財務報告"
    if any(keyword.casefold() in text.casefold() for keyword in FINANCIAL_REPORT_KEYWORDS):
        return "financial_report", "財務訊號"
    if "自結" in text:
        return "self_profit", "自結損益"
    return "material_signal", "重大訊息"


def parse_financial_metrics(text: str) -> dict[str, str]:
    """Extract common early financial metrics from material-info detail text."""
    metric_patterns = {
        "operating_revenue": r"營業收入(?:淨額)?[^:\n]*[:：]\s*([-\d,\.]+)",
        "gross_profit": r"營業毛利(?:\(毛損\))?[^:\n]*[:：]\s*([-\d,\.]+)",
        "operating_income": r"營業利益(?:\(損失\))?[^:\n]*[:：]\s*([-\d,\.]+)",
        "pre_tax_income": r"稅前淨利(?:\(淨損\))?[^:\n]*[:：]\s*([-\d,\.]+)",
        "net_income": r"本期淨利(?:\(淨損\))?[^:\n]*[:：]\s*([-\d,\.]+)",
        "parent_net_income": r"歸屬於母公司業主淨利(?:\(損\))?[^:\n]*[:：]\s*([-\d,\.]+)",
        "eps": r"(?:基本)?每股盈餘(?:\(損失\))?[^:\n]*[:：]\s*([-\d,\.]+)",
    }
    metrics: dict[str, str] = {}
    for key, pattern in metric_patterns.items():
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            metrics[key] = clean_number(match.group(1))
    return metrics


def parse_company_info(text: str) -> tuple[str, str]:
    match = COMPANY_INFO_PATTERN.search(text.replace("\r", " ").replace("\n", " "))
    if not match:
        return "", ""
    company_id = match.group(2) or ""
    company_name = match.group(3).strip("　 ")
    return company_id, company_name


def parse_material_detail_with_fallback(html: str) -> dict[str, Any]:
    """Parse material-info detail pages that use th or td header cells."""
    detail = parse_detail(html)
    if detail.get("fields") and detail.get("description"):
        return detail

    soup = BeautifulSoup(html, "html.parser")
    company_node = soup.select_one("tr.compName td, td.compName")
    company_info = clean_text(company_node)
    if not company_info:
        for table in soup.find_all("table"):
            table_text = clean_text(table)
            if "本資料由" in table_text:
                company_info = table_text
                break

    fields: dict[str, str] = {}
    for row in soup.find_all("tr"):
        cells = [cell for cell in row.find_all(["th", "td"], recursive=False) if isinstance(cell, Tag)]
        index = 0
        while index < len(cells):
            key = clean_text(cells[index])
            if key not in DETAIL_FIELD_LABELS:
                index += 1
                continue
            value = ""
            if index + 1 < len(cells):
                value = clean_text(
                    cells[index + 1],
                    preserve_newlines=(key == "說明"),
                )
            if key and value:
                fields[key] = value
            index += 2

    description = fields.get("說明", "")
    return {
        "company_info": company_info or detail.get("company_info", ""),
        "fields": fields or detail.get("fields", {}),
        "description": description or detail.get("description", ""),
    }


def make_event_time(spoke_date: str, spoke_time: str) -> str:
    normalized_time = normalize_time(spoke_time)
    if not spoke_date:
        return ""
    return f"{spoke_date}T{normalized_time or '00:00:00'}"


def sort_event_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort mixed monthly tab records newest first."""
    return sorted(
        records,
        key=lambda record: str(
            record.get("event_time")
            or record.get("announced_at")
            or record.get("detected_at")
            or ""
        ),
        reverse=True,
    )


def dedupe_event_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str, str, str]] = set()
    unique: list[dict[str, Any]] = []
    for record in records:
        key = (
            str(record.get("source_type", "")),
            str(record.get("company_id", "")),
            str(record.get("data_month", "")),
            str(record.get("announced_at", "")),
            str(record.get("title") or record.get("subject") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(record)
    return unique


def filter_monthly_records_by_company_id(
    records: list[dict[str, Any]],
    query: str = "",
) -> list[dict[str, Any]]:
    normalized_query = re.sub(r"\s+", "", query)
    if not normalized_query:
        return records
    return [
        record
        for record in records
        if normalized_query in re.sub(r"\s+", "", str(record.get("company_id", "")))
    ]


def parse_historical_material_list(html: str) -> list[dict[str, Any]]:
    """Parse company/date historical material-info rows from t05st01."""
    soup = BeautifulSoup(html, "html.parser")
    form = soup.find("form", attrs={"id": "t05st01_fm"})
    if not isinstance(form, Tag):
        return []

    hidden_payload: dict[str, str] = {}
    for input_node in form.find_all("input"):
        name = input_node.get("name")
        if name:
            hidden_payload[name] = input_node.get("value", "")

    table = form.find("table", class_="hasBorder")
    if not isinstance(table, Tag):
        return []

    records: list[dict[str, Any]] = []
    for row in table.find_all("tr"):
        cells = row.find_all("td", recursive=False)
        if len(cells) < 5:
            continue

        company_id = clean_text(cells[0])
        company_name = clean_text(cells[1])
        spoke_date_roc = clean_text(cells[2])
        spoke_time = clean_text(cells[3])
        subject = clean_text(cells[4])
        button = row.find("input", attrs={"value": "詳細資料"})
        onclick = button.get("onclick", "") if isinstance(button, Tag) else ""
        detail_payload = hidden_payload | parse_detail_assignments(onclick)
        detail_payload.setdefault("step", "2")
        detail_payload.setdefault("firstin", "true")
        detail_payload.setdefault("co_id", company_id)

        spoke_date = roc_date_to_iso(spoke_date_roc)
        event_type, source_label = classify_signal_type(
            {"subject": subject, "detail": {"description": ""}}
        )
        records.append(
            {
                "source_type": "historical_material_info",
                "source_label": source_label,
                "event_type": event_type,
                "company_id": company_id,
                "company_name": company_name,
                "spoke_date_roc": spoke_date_roc,
                "spoke_date": spoke_date,
                "spoke_time": normalize_time(spoke_time),
                "announced_at": make_event_time(spoke_date, spoke_time),
                "event_time": make_event_time(spoke_date, spoke_time),
                "subject": subject,
                "title": subject,
                "detail_payload": detail_payload,
                "source_url": urljoin(MOPS_BASE_URL, "/mops/web/t05st01"),
            }
        )
    return records


def parse_company_monthly_revenue(
    html: str,
    company_id: str = "",
    market: str = "",
    detected_at: str | None = None,
) -> dict[str, Any] | None:
    """Parse the single-company monthly revenue table from t05st10_ifrs."""
    soup = BeautifulSoup(html, "html.parser")
    page_text = clean_text(soup)
    if "查無資料" in page_text or "無符合條件之資料" in page_text:
        return None

    period_match = REVENUE_PERIOD_PATTERN.search(page_text)
    if not period_match:
        return None
    roc_year = int(period_match.group(1))
    month = int(period_match.group(2))

    parsed_company_id = company_id
    company_name = ""
    provider_text = ""
    for table in soup.find_all("table"):
        table_text = clean_text(table)
        if "本資料由" not in table_text:
            continue
        provider_text = table_text
        parsed_company_id, company_name = parse_company_info(table_text)
        if company_id and not parsed_company_id:
            parsed_company_id = company_id
        break

    fields: dict[str, str] = {}
    cumulative_section = False
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        for row in rows:
            cells = row.find_all(["th", "td"], recursive=False)
            if len(cells) < 2:
                continue
            key = clean_text(cells[0])
            value = clean_text(cells[1])
            if key and value and key != "項目":
                if key == "本年累計":
                    cumulative_section = True
                normalized_key = key
                if key in {"增減金額", "增減百分比"}:
                    normalized_key = f"{'累計' if cumulative_section else '本月'}{key}"
                fields[normalized_key] = value

    if not fields:
        return None

    detected = detected_at or current_detected_at()
    monthly_revenue = first_number(fields.get("本月", ""))
    yoy_percent = first_number(fields.get("本月增減百分比", fields.get("增減百分比", "")))
    data_month = f"{roc_year}/{month:02d}"
    title_company = " ".join(part for part in (parsed_company_id, company_name) if part)
    title = f"{title_company} {data_month} 月營收".strip()
    description = "\n".join(f"{key}: {value}" for key, value in fields.items())
    if provider_text:
        description = f"{provider_text}\n{description}"

    return {
        "source_type": "company_monthly_revenue",
        "source_label": "個股月營收",
        "event_type": "monthly_revenue",
        "company_id": parsed_company_id,
        "company_name": company_name,
        "market": market,
        "market_label": MARKET_LABELS.get(market, market),
        "data_month": data_month,
        "roc_year": roc_year,
        "month": month,
        "title": title,
        "subject": title,
        "detected_at": detected,
        "event_time": detected,
        "monthly_revenue": monthly_revenue,
        "previous_month_revenue": first_number(fields.get("上月", "")),
        "last_year_month_revenue": first_number(fields.get("去年同期", "")),
        "yoy_percent": yoy_percent,
        "ytd_revenue": first_number(fields.get("本年累計", "")),
        "last_year_ytd_revenue": first_number(fields.get("去年累計", "")),
        "ytd_yoy_percent": first_number(fields.get("累計增減百分比", "")),
        "fields": fields,
        "detail": {"fields": fields, "description": description},
        "source_url": urljoin(MOPS_BASE_URL, COMPANY_MONTHLY_REVENUE_PAGE),
    }


def parse_self_profit_table(html: str, source_label: str, source_url: str) -> list[dict[str, Any]]:
    """Parse generic self-profit rows when the MOPS page returns tabular data."""
    soup = BeautifulSoup(html, "html.parser")
    records: list[dict[str, Any]] = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        headers = [clean_text(cell) for cell in rows[0].find_all(["th", "td"])]
        if not any("公司" in header or "代號" in header for header in headers):
            continue

        for row in rows[1:]:
            cells = [clean_text(cell) for cell in row.find_all(["th", "td"])]
            if len(cells) < 2:
                continue
            values = dict(zip(headers, cells))
            company_id = values.get("公司代號") or values.get("代號") or ""
            company_name = values.get("公司名稱") or values.get("簡稱") or ""
            title = values.get("主旨") or f"{company_id} {company_name} {source_label}".strip()
            detected = current_detected_at()
            records.append(
                {
                    "source_type": "self_profit",
                    "source_label": source_label,
                    "event_type": "self_profit",
                    "company_id": company_id,
                    "company_name": company_name,
                    "title": title,
                    "subject": title,
                    "detected_at": detected,
                    "event_time": detected,
                    "fields": values,
                    "detail": {
                        "fields": values,
                        "description": "\n".join(f"{key}: {value}" for key, value in values.items()),
                    },
                    "source_url": source_url,
                }
            )
    return records


@dataclass
class MonthlyRevenueCrawler:
    """Crawler for the monthly-revenue tab's early signals."""

    base_url: str = MOPS_BASE_URL
    request_interval_seconds: float = DEFAULT_REQUEST_INTERVAL_SECONDS
    timeout_seconds: int = 20
    session: requests.Session | None = None

    def __post_init__(self) -> None:
        if self.session is None:
            self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (compatible; twse-news-dashboard/0.1; "
                    "+https://mopsov.twse.com.tw)"
                ),
                "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
            }
        )
        self._last_request_at = 0.0
        self.material_crawler = MopsCrawler(
            base_url=self.base_url,
            request_interval_seconds=self.request_interval_seconds,
            timeout_seconds=self.timeout_seconds,
            session=self.session,
        )

    def _request_response(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        elapsed = time.monotonic() - self._last_request_at
        wait_seconds = self.request_interval_seconds - elapsed
        if self._last_request_at and wait_seconds > 0:
            time.sleep(wait_seconds)

        response = self.session.request(
            method,
            urljoin(self.base_url, path),
            timeout=self.timeout_seconds,
            **kwargs,
        )
        self._last_request_at = time.monotonic()
        response.raise_for_status()
        response.encoding = "utf-8"
        return response

    def _request(self, method: str, path: str, **kwargs: Any) -> str:
        response = self._request_response(method, path, **kwargs)
        return response.text

    def fetch_twse_listed_monthly_revenue_openapi(self) -> list[dict[str, Any]]:
        detected_at = current_detected_at()
        response = self._request_response("GET", TWSE_LISTED_MONTHLY_REVENUE_OPENAPI_URL)
        rows = response.json()
        if not isinstance(rows, list):
            raise ValueError("TWSE monthly revenue OpenAPI returned a non-list payload")
        return parse_openapi_monthly_revenue_rows(
            rows,
            source_type="twse_openapi_monthly_revenue",
            source_label="上市月營收OpenAPI",
            market="sii",
            detected_at=detected_at,
            source_url=TWSE_LISTED_MONTHLY_REVENUE_OPENAPI_URL,
        )

    def fetch_tpex_otc_monthly_revenue_openapi(self) -> list[dict[str, Any]]:
        detected_at = current_detected_at()
        response = self._request_response("GET", TPEX_OTC_MONTHLY_REVENUE_OPENAPI_URL)
        rows = response.json()
        if not isinstance(rows, list):
            raise ValueError("TPEX OTC monthly revenue OpenAPI returned a non-list payload")
        return parse_openapi_monthly_revenue_rows(
            rows,
            source_type="tpex_openapi_monthly_revenue",
            source_label="上櫃月營收OpenAPI",
            market="otc",
            detected_at=detected_at,
            source_url=TPEX_OTC_MONTHLY_REVENUE_OPENAPI_URL,
        )

    def fetch_mops_monthly_revenue_summary_market(
        self,
        market: str,
        roc_year: int,
        month: int,
    ) -> list[dict[str, Any]]:
        detected_at = current_detected_at()
        self._request(
            "POST",
            MOPS_MONTHLY_REVENUE_AJAX_PATH,
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
        csv_response = self._request_response(
            "POST",
            MOPS_DOWNLOAD_PATH,
            data={
                "step": "9",
                "functionName": "show_file2",
                "filePath": f"/t21/{market}/",
                "fileName": f"t21sc03_{roc_year}_{month}.csv",
            },
        )
        return parse_mops_monthly_revenue_csv(
            csv_response.content,
            market=market,
            detected_at=detected_at,
            source_url=urljoin(self.base_url, MOPS_MONTHLY_REVENUE_PAGE),
        )

    def fetch_mops_monthly_revenue_summary(
        self,
        roc_year: int,
        month: int,
        markets: list[str] | tuple[str, ...] = ("sii", "otc"),
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for market in markets:
            records.extend(
                self.fetch_mops_monthly_revenue_summary_market(
                    market=market,
                    roc_year=roc_year,
                    month=month,
                )
            )
        return sort_event_records(records)

    def fetch_monthly_revenue_summary_with_fallbacks(
        self,
        roc_year: int,
        month: int,
        markets: list[str] | tuple[str, ...] = ("sii", "otc"),
    ) -> dict[str, Any]:
        records: list[dict[str, Any]] = []
        market_results: list[dict[str, Any]] = []
        requested_month = f"{int(roc_year):03d}/{int(month):02d}"

        for market in markets:
            market_label = MARKET_LABELS.get(market, market)
            try:
                market_records = self.fetch_mops_monthly_revenue_summary_market(
                    market=market,
                    roc_year=roc_year,
                    month=month,
                )
                records.extend(market_records)
                market_results.append(
                    {
                        "market": market,
                        "market_label": market_label,
                        "source": "mops_t21sc04_ifrs",
                        "ok": True,
                        "fallback": False,
                        "record_count": len(market_records),
                    }
                )
                continue
            except Exception as primary_exc:
                primary_error = str(primary_exc)

            fallback_source = ""
            fallback_records: list[dict[str, Any]] = []
            try:
                if market == "sii":
                    fallback_source = "twse_openapi_t187ap05_L"
                    fallback_records = self.fetch_twse_listed_monthly_revenue_openapi()
                elif market == "otc":
                    fallback_source = "tpex_openapi_mopsfin_t187ap05_O"
                    fallback_records = self.fetch_tpex_otc_monthly_revenue_openapi()
                else:
                    raise RuntimeError(f"No monthly revenue OpenAPI fallback for market={market}")

                fallback_records = filter_monthly_revenue_records_by_data_month(
                    fallback_records,
                    roc_year=roc_year,
                    month=month,
                )
                if not fallback_records:
                    raise RuntimeError(
                        f"{fallback_source} did not return requested data_month={requested_month}"
                    )

                records.extend(fallback_records)
                market_results.append(
                    {
                        "market": market,
                        "market_label": market_label,
                        "source": fallback_source,
                        "ok": True,
                        "fallback": True,
                        "record_count": len(fallback_records),
                        "primary_error": primary_error,
                    }
                )
            except Exception as fallback_exc:
                market_results.append(
                    {
                        "market": market,
                        "market_label": market_label,
                        "source": fallback_source or "none",
                        "ok": False,
                        "fallback": bool(fallback_source),
                        "record_count": 0,
                        "primary_error": primary_error,
                        "fallback_error": str(fallback_exc),
                    }
                )

        return {
            "records": sort_event_records(records),
            "market_results": market_results,
        }

    def fetch_historical_material_records(
        self,
        company_id: str,
        start_date: str | date,
        end_date: str | date,
        market: str = "all",
        include_details: bool = True,
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        grouped_dates: dict[tuple[str, str], list[date]] = {}
        for target_date in iter_dates(start_date, end_date):
            year, month, _ = iso_date_to_roc_parts(target_date)
            grouped_dates.setdefault((year, month), []).append(target_date)

        for (year, month), dates in grouped_dates.items():
            html = self._request(
                "POST",
                HISTORICAL_MATERIAL_AJAX_PATH,
                data={
                    "step": "1",
                    "firstin": "ture",
                    "off": "1",
                    "keyword4": "",
                    "code1": "",
                    "TYPEK2": "",
                    "checkbtn": "",
                    "queryName": "co_id",
                    "inpuType": "co_id",
                    "TYPEK": market,
                    "co_id": company_id,
                    "year": year,
                    "month": month,
                    "b_date": f"{min(day.day for day in dates):02d}",
                    "e_date": f"{max(day.day for day in dates):02d}",
                },
            )
            month_records = parse_historical_material_list(html)
            candidate_records = [
                record
                for record in month_records
                if looks_like_monthly_or_financial_signal(record)
            ]
            if include_details:
                for record in candidate_records:
                    detail = self.fetch_historical_material_detail(record["detail_payload"])
                    record["detail"] = detail
                    record["metrics"] = parse_financial_metrics(detail.get("description", ""))
                    event_type, source_label = classify_signal_type(record)
                    record["event_type"] = event_type
                    record["source_label"] = source_label
            records.extend(
                record
                for record in candidate_records
                if looks_like_monthly_or_financial_signal(record)
            )

        return sort_event_records(dedupe_event_records(records))

    def fetch_historical_material_detail(self, detail_payload: dict[str, str]) -> dict[str, Any]:
        html = self._request("POST", HISTORICAL_MATERIAL_AJAX_PATH, data=detail_payload)
        return parse_material_detail_with_fallback(html)

    def fetch_realtime_material_signals(
        self,
        max_items: int = 0,
        market: str = "all",
    ) -> list[dict[str, Any]]:
        self.material_crawler._last_request_at = self._last_request_at
        summaries = self.material_crawler.fetch_latest_summaries(market=market)
        self._last_request_at = self.material_crawler._last_request_at
        signals: list[dict[str, Any]] = []

        candidates = [
            summary
            for summary in summaries
            if looks_like_monthly_or_financial_signal(
                {**summary, "detail": {"description": ""}}
            )
        ]
        selected = candidates[:max_items] if max_items > 0 else candidates
        fetched_at = current_detected_at()

        for summary in selected:
            self.material_crawler._last_request_at = self._last_request_at
            detail = self.material_crawler.fetch_detail(summary["detail_payload"])
            self._last_request_at = self.material_crawler._last_request_at
            record = {**summary, "fetched_at": fetched_at, "detail": detail}
            if not looks_like_monthly_or_financial_signal(record):
                continue
            event_type, source_label = classify_signal_type(record)
            spoke_date = str(record.get("spoke_date", ""))
            spoke_time = str(record.get("spoke_time", ""))
            detail = record.get("detail") or record.get("detail_preview") or {}
            signals.append(
                {
                    **record,
                    "source_type": "realtime_material_info",
                    "source_label": source_label,
                    "event_type": event_type,
                    "title": record.get("subject", ""),
                    "announced_at": make_event_time(spoke_date, spoke_time),
                    "event_time": make_event_time(spoke_date, spoke_time),
                    "metrics": parse_financial_metrics(detail.get("description", "")),
                    "source_url": urljoin(self.base_url, "/mops/web/t05sr01_1"),
                }
            )
        return sort_event_records(dedupe_event_records(signals))

    def fetch_company_monthly_revenue(
        self,
        company_id: str,
        roc_year: int,
        month: int,
        market: str = "all",
    ) -> dict[str, Any] | None:
        detected_at = current_detected_at()
        html = self._request(
            "POST",
            COMPANY_MONTHLY_REVENUE_AJAX_PATH,
            data={
                "step": "1",
                "firstin": "ture",
                "off": "1",
                "keyword4": "",
                "code1": "",
                "TYPEK2": "",
                "checkbtn": "",
                "queryName": "co_id",
                "inpuType": "co_id",
                "TYPEK": market,
                "isnew": "false",
                "co_id": company_id,
                "year": str(roc_year),
                "month": f"{month:02d}",
            },
        )
        return parse_company_monthly_revenue(
            html,
            company_id=company_id,
            market=market,
            detected_at=detected_at,
        )

    def fetch_self_profit_records(
        self,
        company_id: str,
        roc_year: int,
        market: str = "all",
        quarterly: bool = False,
    ) -> list[dict[str, Any]]:
        path = SELF_PROFIT_QUARTERLY_AJAX_PATH if quarterly else SELF_PROFIT_MONTHLY_AJAX_PATH
        source_label = "自結損益-季申報" if quarterly else "自結損益-月申報"
        page = SELF_PROFIT_QUARTERLY_PAGE if quarterly else SELF_PROFIT_MONTHLY_PAGE
        html = self._request(
            "POST",
            path,
            data={
                "step": "1",
                "firstin": "ture",
                "off": "1",
                "TYPEK": market,
                "keyword4": "",
                "code1": "",
                "TYPEK2": "",
                "checkbtn": "",
                "queryName": "co_id",
                "inpuType": "co_id",
                "co_id": company_id,
                "year": str(roc_year),
            },
        )
        return parse_self_profit_table(html, source_label, urljoin(self.base_url, page))

    def fetch_dashboard_records(
        self,
        company_ids: list[str],
        revenue_roc_year: int,
        revenue_month: int,
        material_start_date: str | date | None = None,
        material_end_date: str | date | None = None,
        market: str = "all",
        include_realtime: bool = True,
        include_historical_material: bool = False,
        include_company_revenue: bool = True,
        include_self_profit: bool = True,
        max_realtime_items: int = 0,
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        if include_realtime:
            records.extend(
                self.fetch_realtime_material_signals(
                    max_items=max_realtime_items,
                    market=market,
                )
            )

        for company_id in company_ids:
            if include_historical_material and material_start_date and material_end_date:
                records.extend(
                    self.fetch_historical_material_records(
                        company_id=company_id,
                        start_date=material_start_date,
                        end_date=material_end_date,
                        market=market,
                        include_details=True,
                    )
                )
            if include_company_revenue:
                revenue_record = self.fetch_company_monthly_revenue(
                    company_id=company_id,
                    roc_year=revenue_roc_year,
                    month=revenue_month,
                    market=market,
                )
                if revenue_record:
                    records.append(revenue_record)
            if include_self_profit:
                records.extend(
                    self.fetch_self_profit_records(
                        company_id=company_id,
                        roc_year=revenue_roc_year,
                        market=market,
                        quarterly=False,
                    )
                )
                records.extend(
                    self.fetch_self_profit_records(
                        company_id=company_id,
                        roc_year=revenue_roc_year,
                        market=market,
                        quarterly=True,
                    )
                )

        return sort_event_records(dedupe_event_records(records))
