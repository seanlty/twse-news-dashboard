"""Crawler and parsers for MOPS realtime material information."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from bs4.element import Tag

MOPS_BASE_URL = "https://mopsov.twse.com.tw"
LATEST_PATH = "/mops/web/t05sr01_1"
AJAX_PATH = "/mops/web/ajax_t05sr01_1"
PREVIOUS_DAY_PATH = "/mops/web/t05st02"
PREVIOUS_DAY_AJAX_PATH = "/mops/web/ajax_t05st02"
DEFAULT_REFRESH_SECONDS = 180
DEFAULT_REQUEST_INTERVAL_SECONDS = 1.0
DEFAULT_DAY_INTERVAL_SECONDS = 5.0
CATEGORY_ALL = "all"
CATEGORY_FINANCIAL_SELF_REPORT = "financial-self-report"
CATEGORY_CHOICES = (CATEGORY_ALL, CATEGORY_FINANCIAL_SELF_REPORT)
EPS_LABEL = "每股盈餘"

FORM_ASSIGNMENT_PATTERN = re.compile(
    r"document\.[A-Za-z0-9_]+\.([A-Za-z0-9_]+)\.value=(['\"])(.*?)\2"
)
NUMBER_TOKEN_PATTERN = re.compile(r"\(?-?\d+(?:\.\d+)?\)?%?")
MONTH_PERIOD_PATTERNS = (
    re.compile(r"\d{3}\s*年\s*0?(\d{1,2})\s*月"),
    re.compile(r"\(\s*\d{3}\s*/\s*0?(\d{1,2})\s*\)"),
)
QUARTER_PERIOD_PATTERN = re.compile(r"\d{3}\s*年?\s*第\s*(\d)\s*季")
FINANCIAL_SELF_REPORT_REQUIRED_KEYWORDS = ("自結",)
FINANCIAL_SELF_REPORT_CONTEXT_KEYWORDS = (
    "財報",
    "財務",
    "損益",
    "稅前",
    "稅後",
    "每股",
    "eps",
    "盈餘",
    "淨利",
    "淨損",
    "虧損",
    "獲利",
    "合併",
)
ATTENTION_TRADING_KEYWORDS = ("注意交易", "公布注意", "公佈注意", "達公布注意", "達公佈注意")
FINANCIAL_BUSINESS_KEYWORDS = ("財務業務資訊", "財務業務", "財務、業務", "相關財務業務")
SELF_PROFIT_KEYWORDS = ("損益", "營業損益", "稅前損益", "稅後損益", "合併損益", "淨利", "淨損")
FINANCIAL_SIGNAL_SELF_REPORT_EPS = "self_report_eps"
FINANCIAL_SIGNAL_ATTENTION_EPS = "attention_financial_eps"
FINANCIAL_SIGNAL_SELF_PROFIT_NO_EPS = "self_profit_without_eps"
PREVIOUS_DAY_HIDDEN_PATTERN = re.compile(r"^h(\d+)([0-8])$")
PREVIOUS_DAY_FIELD_NAMES = {
    "0": "company_name",
    "1": "company_id",
    "2": "spoke_date",
    "3": "spoke_time",
    "4": "subject",
    "5": "sequence_no",
    "6": "clause",
    "7": "fact_date",
    "8": "description",
}


def clean_text(node: Tag | None, preserve_newlines: bool = False) -> str:
    """Normalize text extracted from a BeautifulSoup node."""
    if node is None:
        return ""

    if preserve_newlines:
        text = node.get_text("\n", strip=False)
        lines = [re.sub(r"[ \t\r\f\v]+", " ", line).rstrip() for line in text.splitlines()]
        return "\n".join(line for line in lines).strip()

    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()


def roc_date_to_iso(value: str) -> str:
    """Convert a MOPS ROC date such as 115/06/27 or 20260627 to ISO date."""
    value = value.strip()
    if not value:
        return ""

    if "/" in value:
        year_text, month_text, day_text = value.split("/")[:3]
        year = int(year_text) + 1911 if len(year_text) <= 3 else int(year_text)
        return f"{year:04d}-{int(month_text):02d}-{int(day_text):02d}"

    if len(value) == 8 and value.isdigit():
        return f"{value[:4]}-{value[4:6]}-{value[6:8]}"

    return value


def iso_date_to_roc_parts(value: str | date) -> tuple[str, str, str]:
    """Convert YYYY-MM-DD/date into MOPS ROC year, month, and day strings."""
    if isinstance(value, str):
        target = (
            date(int(value[:4]), int(value[4:6]), int(value[6:8]))
            if len(value) == 8 and value.isdigit()
            else date.fromisoformat(value)
        )
    else:
        target = value
    return str(target.year - 1911), f"{target.month:02d}", f"{target.day:02d}"


def iso_date_to_roc(value: str | date) -> str:
    """Convert YYYY-MM-DD/date into a slash-separated ROC date."""
    year, month, day = iso_date_to_roc_parts(value)
    return f"{int(year):03d}/{month}/{day}"


def default_previous_day() -> date:
    """Return the calendar day used by the MOPS previous-day button."""
    return date.today() - timedelta(days=1)


def default_month_start(today: date | None = None) -> date:
    """Return the first calendar day of the month."""
    current = today or date.today()
    return current.replace(day=1)


def iter_dates(start_date: str | date, end_date: str | date) -> list[date]:
    """Return inclusive dates from start_date to end_date."""
    start = date.fromisoformat(start_date) if isinstance(start_date, str) else start_date
    end = date.fromisoformat(end_date) if isinstance(end_date, str) else end_date
    if start > end:
        raise ValueError("start_date must be earlier than or equal to end_date")

    days: list[date] = []
    current = start
    while current <= end:
        days.append(current)
        current += timedelta(days=1)
    return days


def parse_iso_date(value: str) -> date | None:
    """Parse an ISO date string, returning None for empty/invalid values."""
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def normalize_time(value: str) -> str:
    """Convert compact MOPS time such as 92242 to HH:MM:SS."""
    value = value.strip()
    if not value:
        return ""
    if ":" in value:
        return value
    digits = value.zfill(6)
    return f"{digits[:2]}:{digits[2:4]}:{digits[4:6]}"


def clean_multiline_text(value: str) -> str:
    """Normalize multiline text while keeping line boundaries for table parsing."""
    lines = [re.sub(r"\s+", " ", line).strip() for line in value.splitlines()]
    return "\n".join(line for line in lines if line)


def normalize_number_token(token: str) -> str | None:
    """Normalize a numeric token from MOPS text, skipping percentages."""
    raw = token.strip()
    if not raw or raw.endswith("%"):
        return None

    negative_by_parentheses = raw.startswith("(") and raw.endswith(")")
    cleaned = raw.strip("()").replace(",", "")
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", cleaned):
        return None

    if negative_by_parentheses and not cleaned.startswith("-"):
        cleaned = f"-{cleaned}"
    return cleaned


def numeric_tokens_without_percent(text: str) -> list[str]:
    """Return numeric tokens from text while ignoring percentage values."""
    values: list[str] = []
    for token in NUMBER_TOKEN_PATTERN.findall(text):
        value = normalize_number_token(token)
        if value is not None:
            values.append(value)
    return values


def text_after_eps_label(line: str) -> str:
    """Return the text after the first EPS label in a line."""
    _, _, tail = line.partition(EPS_LABEL)
    return tail


def should_read_next_line_for_eps(line: str) -> bool:
    """Return True when an EPS row label is separated from values."""
    tail = text_after_eps_label(line).strip()
    tail = re.sub(r"^[（(]\s*元\s*[）)]", "", tail).strip()
    tail = tail.strip(":：")
    return tail == ""


def safe_float(value: str | None) -> float | None:
    """Parse a numeric string used by EPS metrics."""
    if value in (None, ""):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def format_decimal(value: float, digits: int = 2) -> str:
    """Format extracted EPS derived values for table display."""
    rounded = round(value, digits)
    if rounded == 0:
        rounded = 0
    return f"{rounded:.{digits}f}"


def find_month_period(text: str) -> str | None:
    """Extract the reported monthly period from MOPS detail text."""
    for pattern in MONTH_PERIOD_PATTERNS:
        match = pattern.search(text)
        if match:
            return f"{int(match.group(1))}月"
    return None


def find_quarter_period(text: str) -> str | None:
    """Extract the reported quarter label from MOPS detail text."""
    match = QUARTER_PERIOD_PATTERN.search(text)
    if match:
        return f"Q{int(match.group(1))}"
    return None


def extract_eps_metrics(record: dict[str, Any]) -> dict[str, Any]:
    """Extract EPS-oriented metrics from a self-reported financial record."""
    detail = record.get("detail") or record.get("detail_preview") or {}
    description = str(detail.get("description") or "")
    text = clean_multiline_text(description)
    lines = text.splitlines()
    eps_rows: list[list[str]] = []

    for index, line in enumerate(lines):
        if EPS_LABEL not in line:
            continue

        values = numeric_tokens_without_percent(text_after_eps_label(line))
        if not values and index + 1 < len(lines) and should_read_next_line_for_eps(line):
            values = numeric_tokens_without_percent(lines[index + 1])

        if values:
            eps_rows.append(values)

    month_eps: str | None = None
    last_year_month_eps: str | None = None
    quarter_eps: str | None = None
    last_year_quarter_eps: str | None = None
    four_quarter_eps: str | None = None

    if len(eps_rows) == 1:
        values = eps_rows[0]
        if values:
            month_eps = values[0]
        if len(values) >= 2:
            quarter_eps = values[1]
        if len(values) >= 3:
            four_quarter_eps = values[2]
    elif eps_rows:
        month_values = eps_rows[0]
        quarter_values = eps_rows[1] if len(eps_rows) > 1 else []
        four_quarter_values = eps_rows[2] if len(eps_rows) > 2 else []

        if month_values:
            month_eps = month_values[0]
        if len(month_values) >= 2:
            last_year_month_eps = month_values[1]

        if quarter_values:
            quarter_eps = quarter_values[0]
        if len(quarter_values) >= 2:
            last_year_quarter_eps = quarter_values[1]

        if four_quarter_values:
            four_quarter_eps = four_quarter_values[0]

    quarter_eps_value = safe_float(quarter_eps)
    quarter_eps_div3 = (
        format_decimal(quarter_eps_value / 3)
        if quarter_eps_value is not None
        else None
    )

    return {
        "period": find_month_period(text),
        "month_eps": month_eps,
        "last_year_month_eps": last_year_month_eps,
        "quarter": find_quarter_period(text),
        "quarter_eps_div3": quarter_eps_div3,
        "quarter_eps": quarter_eps,
        "last_year_quarter_eps": last_year_quarter_eps,
        "four_quarter_eps": four_quarter_eps,
        "has_eps": bool(month_eps or quarter_eps or four_quarter_eps),
    }


def get_or_extract_eps_metrics(record: dict[str, Any]) -> dict[str, Any]:
    """Return cached EPS metrics or extract and attach them to the record."""
    metrics = record.get("eps_metrics")
    if isinstance(metrics, dict):
        return metrics

    metrics = extract_eps_metrics(record)
    record["eps_metrics"] = metrics
    return metrics


def filter_records_with_eps(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only records with extractable self-reported EPS data."""
    return [
        record
        for record in records
        if get_or_extract_eps_metrics(record).get("has_eps")
    ]


def record_classification_text(record: dict[str, Any]) -> str:
    """Build searchable text from a MOPS summary/detail record."""
    detail = record.get("detail") or record.get("detail_preview") or {}
    fields = detail.get("fields") or {}
    return "\n".join(
        [
            str(record.get("subject", "")),
            str(detail.get("description", "")),
            *[str(value) for value in fields.values()],
        ]
    )


def has_any_keyword(text: str, keywords: tuple[str, ...]) -> bool:
    normalized = text.casefold()
    return any(keyword.casefold() in normalized for keyword in keywords)


def is_financial_self_report_text(text: str) -> bool:
    """Return True when text looks like self-reported financial results."""
    normalized = text.casefold()
    return all(keyword in normalized for keyword in FINANCIAL_SELF_REPORT_REQUIRED_KEYWORDS) and any(
        keyword in normalized for keyword in FINANCIAL_SELF_REPORT_CONTEXT_KEYWORDS
    )


def is_attention_financial_business_text(text: str) -> bool:
    """Return True for attention-trading disclosures with financial business tables."""
    return has_any_keyword(text, ATTENTION_TRADING_KEYWORDS) and has_any_keyword(text, FINANCIAL_BUSINESS_KEYWORDS)


def is_self_profit_text(text: str) -> bool:
    """Return True for self-reported profit/loss disclosures."""
    return "自結" in text and has_any_keyword(text, SELF_PROFIT_KEYWORDS)


def recent_financial_signal_kind(record: dict[str, Any], text: str | None = None) -> str:
    """Classify records that should appear in the recent financial tab."""
    haystack = record_classification_text(record) if text is None else text
    is_attention_financial_business = is_attention_financial_business_text(haystack)
    is_self_profit = is_self_profit_text(haystack)
    if not (record.get("is_financial_self_report") or is_attention_financial_business or is_self_profit):
        return ""

    metrics = get_or_extract_eps_metrics(record)
    has_eps = bool(metrics.get("has_eps"))

    if record.get("is_financial_self_report") and has_eps:
        return FINANCIAL_SIGNAL_SELF_REPORT_EPS
    if is_attention_financial_business and has_eps:
        return FINANCIAL_SIGNAL_ATTENTION_EPS
    if is_self_profit and not has_eps:
        return FINANCIAL_SIGNAL_SELF_PROFIT_NO_EPS
    return ""


def is_recent_financial_record(record: dict[str, Any]) -> bool:
    classify_record(record)
    return bool(record.get("financial_signal_kind"))


def filter_records_for_recent_financial(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep records displayed by the recent self-report/attention financial tab."""
    return [record for record in records if is_recent_financial_record(record)]


def classify_record(record: dict[str, Any]) -> dict[str, Any]:
    """Add category fields to a material-information record."""
    haystack = record_classification_text(record)
    is_financial_self_report = is_financial_self_report_text(haystack)
    category = CATEGORY_FINANCIAL_SELF_REPORT if is_financial_self_report else "other"
    record["category"] = category
    record["is_financial_self_report"] = is_financial_self_report
    signal_kind = recent_financial_signal_kind(record, haystack)
    record["financial_signal_kind"] = signal_kind
    record["is_attention_financial_eps"] = signal_kind == FINANCIAL_SIGNAL_ATTENTION_EPS
    record["is_self_profit_without_eps"] = signal_kind == FINANCIAL_SIGNAL_SELF_PROFIT_NO_EPS
    return record


def enrich_records_for_cache(
    records: list[dict[str, Any]],
    recompute_eps: bool = False,
) -> list[dict[str, Any]]:
    """Attach derived category and EPS fields before writing records to cache."""
    enriched: list[dict[str, Any]] = []
    for record in records:
        if recompute_eps:
            record.pop("eps_metrics", None)
        enriched.append(classify_record(record))
    return enriched


def filter_records_by_category(
    records: list[dict[str, Any]],
    category: str = CATEGORY_ALL,
) -> list[dict[str, Any]]:
    """Filter classified records by category."""
    if category == CATEGORY_ALL:
        return records
    if category == CATEGORY_FINANCIAL_SELF_REPORT:
        return [record for record in records if record.get("is_financial_self_report")]
    raise ValueError(f"Unsupported category: {category}")


def filter_records_by_company_id(
    records: list[dict[str, Any]],
    query: str = "",
) -> list[dict[str, Any]]:
    """Filter records by stock/company code."""
    normalized_query = re.sub(r"\s+", "", query)
    if not normalized_query:
        return records

    return [
        record
        for record in records
        if normalized_query in re.sub(r"\s+", "", str(record.get("company_id", "")))
    ]


def filter_records_by_recent_days(
    records: list[dict[str, Any]],
    end_date: str | date | None = None,
    days: int = 7,
) -> list[dict[str, Any]]:
    """Filter records to the inclusive recent N-day window by spoke_date."""
    if days <= 0:
        raise ValueError("days must be positive")

    parsed_dates = [
        parsed
        for parsed in (parse_iso_date(str(record.get("spoke_date", ""))) for record in records)
        if parsed is not None
    ]
    if not parsed_dates:
        return []

    window_end = (
        date.fromisoformat(end_date)
        if isinstance(end_date, str)
        else end_date
        if end_date is not None
        else max(parsed_dates)
    )
    window_start = window_end - timedelta(days=days - 1)

    return [
        record
        for record in records
        if (parsed := parse_iso_date(str(record.get("spoke_date", "")))) is not None
        and window_start <= parsed <= window_end
    ]


def sort_records_by_spoke_time(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort records newest-first by MOPS spoke date and time."""
    return sorted(
        records,
        key=lambda record: (
            str(record.get("spoke_date", "")),
            normalize_time(str(record.get("spoke_time", ""))),
        ),
        reverse=True,
    )


def dedupe_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate records by stable MOPS row identity."""
    seen: set[tuple[str, str, str, str]] = set()
    unique_records: list[dict[str, Any]] = []
    for record in records:
        key = (
            str(record.get("company_id", "")),
            str(record.get("spoke_date", "")),
            normalize_time(str(record.get("spoke_time", ""))),
            str(record.get("subject", "")),
        )
        if key in seen:
            continue
        seen.add(key)
        unique_records.append(record)
    return unique_records


def parse_detail_assignments(onclick: str) -> dict[str, str]:
    """Extract hidden form values from the MOPS detail button JavaScript."""
    return {name: value for name, _, value in FORM_ASSIGNMENT_PATTERN.findall(onclick)}


def parse_latest_list(html: str) -> list[dict[str, Any]]:
    """Parse realtime material-information rows from the MOPS list HTML."""
    soup = BeautifulSoup(html, "html.parser")
    form = soup.find("form", attrs={"name": "fm_t05sr01_1"})
    if not isinstance(form, Tag):
        return []

    hidden_payload: dict[str, str] = {}
    for hidden in form.find_all("input", attrs={"type": "hidden"}):
        name = hidden.get("name")
        if name:
            hidden_payload[name] = hidden.get("value", "")

    table = form.find("table", class_="hasBorder")
    if not isinstance(table, Tag):
        return []

    rows: list[dict[str, Any]] = []
    for tr in table.find_all("tr"):
        cells = tr.find_all("td", recursive=False)
        if len(cells) < 6:
            continue

        button = cells[-1].find("input", attrs={"value": "詳細資料"})
        onclick = button.get("onclick", "") if isinstance(button, Tag) else ""
        detail_payload = hidden_payload | parse_detail_assignments(onclick)
        company_id = clean_text(cells[0])
        company_name = clean_text(cells[1])
        spoke_date_roc = clean_text(cells[2])
        spoke_time_text = clean_text(cells[3])
        subject = clean_text(cells[4])

        detail_payload.setdefault("COMPANY_NAME", company_name)
        detail_payload.setdefault("TYPEK", "all")
        detail_payload.setdefault("step", "1")
        detail_payload.setdefault("firstin", "true")

        rows.append(
            {
                "company_id": company_id,
                "company_name": company_name,
                "spoke_date_roc": spoke_date_roc,
                "spoke_date": roc_date_to_iso(spoke_date_roc),
                "spoke_time": spoke_time_text,
                "subject": subject,
                "detail_payload": detail_payload,
                "source_url": urljoin(MOPS_BASE_URL, LATEST_PATH),
            }
        )

    return rows


def _extract_hidden_payload(form: Tag) -> dict[str, str]:
    payload: dict[str, str] = {}
    for hidden in form.find_all("input"):
        name = hidden.get("name")
        if name:
            payload[name] = hidden.get("value", "")
    return payload


def _previous_day_hidden_fields(payload: dict[str, str]) -> dict[str, str]:
    fields: dict[str, str] = {}
    for name, value in payload.items():
        match = PREVIOUS_DAY_HIDDEN_PATTERN.match(name)
        if not match:
            continue
        field_name = PREVIOUS_DAY_FIELD_NAMES.get(match.group(2))
        if field_name:
            fields[field_name] = value
    return fields


def parse_previous_day_list(html: str, query_date: str | date | None = None) -> list[dict[str, Any]]:
    """Parse previous-day material-information rows from the MOPS t05st02 HTML."""
    soup = BeautifulSoup(html, "html.parser")
    records: list[dict[str, Any]] = []
    query_date_iso = ""
    if query_date:
        query_date_iso = query_date if isinstance(query_date, str) else query_date.isoformat()

    forms = soup.find_all("form", attrs={"action": re.compile(r"/mops/web/ajax_t05st02$")})
    for form in forms:
        if not isinstance(form, Tag):
            continue

        button = form.find("input", attrs={"value": "詳細資料"})
        if not isinstance(button, Tag):
            continue

        row = form.find_parent("tr")
        cells = row.find_all("td", recursive=False) if isinstance(row, Tag) else []
        payload = _extract_hidden_payload(form)
        payload.update(parse_detail_assignments(button.get("onclick", "")))
        hidden_fields = _previous_day_hidden_fields(payload)

        spoke_date_roc = clean_text(cells[0]) if len(cells) > 0 else ""
        spoke_time = clean_text(cells[1]) if len(cells) > 1 else ""
        company_id = clean_text(cells[2]) if len(cells) > 2 else hidden_fields.get("company_id", "")
        company_name = clean_text(cells[3]) if len(cells) > 3 else hidden_fields.get("company_name", "")
        subject = clean_text(cells[4]) if len(cells) > 4 else hidden_fields.get("subject", "")

        if not spoke_date_roc:
            spoke_date_roc = iso_date_to_roc(hidden_fields["spoke_date"]) if hidden_fields.get("spoke_date") else ""
        if not spoke_time and hidden_fields.get("spoke_time"):
            spoke_time = normalize_time(hidden_fields["spoke_time"])

        detail_preview = {
            "fields": {
                "序號": hidden_fields.get("sequence_no", ""),
                "主旨": subject,
                "符合條款": hidden_fields.get("clause", ""),
                "事實發生日": (
                    iso_date_to_roc(hidden_fields["fact_date"])
                    if hidden_fields.get("fact_date")
                    else ""
                ),
                "說明": hidden_fields.get("description", ""),
            },
            "description": hidden_fields.get("description", ""),
        }

        records.append(
            {
                "company_id": company_id,
                "company_name": company_name,
                "spoke_date_roc": spoke_date_roc,
                "spoke_date": roc_date_to_iso(spoke_date_roc),
                "spoke_time": spoke_time,
                "subject": subject,
                "query_date": query_date_iso,
                "detail_payload": payload,
                "detail_preview": detail_preview,
                "source_url": urljoin(MOPS_BASE_URL, PREVIOUS_DAY_PATH),
            }
        )

    return records


def _next_td(elements: list[Tag], index: int, allow_skip_th: bool = False) -> Tag | None:
    for element in elements[index + 1 :]:
        if element.name == "td":
            return element
        if element.name == "th" and not allow_skip_th:
            return None
    return None


def parse_detail(html: str) -> dict[str, Any]:
    """Parse a MOPS detail HTML page into normalized fields."""
    soup = BeautifulSoup(html, "html.parser")
    company_node = soup.select_one("tr.compName td, td.compName")
    detail_table = soup.find("table", class_="hasBorder")
    if not isinstance(detail_table, Tag):
        for table in soup.find_all("table"):
            if not isinstance(table, Tag):
                continue
            headings = {clean_text(th) for th in table.find_all("th")}
            if "說明" in headings and "序號" in headings:
                detail_table = table
                break

    fields: dict[str, str] = {}

    if isinstance(detail_table, Tag):
        elements = [node for node in detail_table.find_all(["th", "td"]) if isinstance(node, Tag)]
        for index, element in enumerate(elements):
            if element.name != "th":
                continue

            key = clean_text(element)
            if not key or key in {"第", "款"}:
                continue

            value_node = _next_td(elements, index, allow_skip_th=(key == "符合條款"))
            if value_node is None:
                continue

            fields[key] = clean_text(value_node, preserve_newlines=(key == "說明"))

    description = fields.get("說明", "")
    return {
        "company_info": clean_text(company_node),
        "fields": fields,
        "description": description,
    }


@dataclass
class MopsCrawler:
    """Small MOPS crawler with request throttling."""

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

    def _request(self, method: str, path: str, **kwargs: Any) -> str:
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
        return response.text

    def fetch_latest_summaries(self, market: str = "all") -> list[dict[str, Any]]:
        """Fetch realtime material-information summaries for a market type."""
        html = self._request(
            "POST",
            AJAX_PATH,
            data={"TYPEK": market, "step": "0"},
        )
        return parse_latest_list(html)

    def fetch_detail(self, detail_payload: dict[str, str]) -> dict[str, Any]:
        """Fetch and parse the detail window content for one summary row."""
        html = self._request("POST", AJAX_PATH, data=detail_payload)
        return parse_detail(html)

    def fetch_previous_day_summaries(
        self,
        target_date: str | date | None = None,
        market: str = "all",
    ) -> list[dict[str, Any]]:
        """Fetch previous-day material-information summaries for a MOPS date."""
        target = default_previous_day() if target_date is None else target_date
        year, month, day = iso_date_to_roc_parts(target)
        html = self._request(
            "POST",
            PREVIOUS_DAY_PATH,
            data={
                "step": "0",
                "newstuff": "1",
                "firstin": "1",
                "TYPEK": market,
                "year": year,
                "month": month,
                "day": day,
            },
        )
        query_date = target if isinstance(target, date) else date.fromisoformat(target)
        return parse_previous_day_list(html, query_date=query_date)

    def fetch_previous_day_detail(self, detail_payload: dict[str, str]) -> dict[str, Any]:
        """Fetch and parse a previous-day detail-window response."""
        html = self._request("POST", PREVIOUS_DAY_AJAX_PATH, data=detail_payload)
        return parse_detail(html)

    def fetch_latest_with_details(
        self,
        max_items: int = 20,
        market: str = "all",
    ) -> list[dict[str, Any]]:
        """Fetch realtime rows and enrich each row with detail-window content."""
        summaries = self.fetch_latest_summaries(market=market)
        selected = summaries[:max_items] if max_items > 0 else summaries

        records: list[dict[str, Any]] = []
        fetched_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        for summary in selected:
            record = dict(summary)
            record["fetched_at"] = fetched_at
            record["detail"] = self.fetch_detail(summary["detail_payload"])
            records.append(classify_record(record))

        return sort_records_by_spoke_time(dedupe_records(records))

    def fetch_previous_day_with_details(
        self,
        target_date: str | date | None = None,
        max_items: int = 20,
        market: str = "all",
    ) -> list[dict[str, Any]]:
        """Fetch previous-day rows and enrich each row with detail-window content."""
        summaries = self.fetch_previous_day_summaries(target_date=target_date, market=market)
        selected = summaries[:max_items] if max_items > 0 else summaries

        records: list[dict[str, Any]] = []
        fetched_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        for summary in selected:
            record = dict(summary)
            record["fetched_at"] = fetched_at
            record["detail"] = self.fetch_previous_day_detail(summary["detail_payload"])
            records.append(classify_record(record))

        return sort_records_by_spoke_time(dedupe_records(records))

    def fetch_date_range_records(
        self,
        start_date: str | date,
        end_date: str | date,
        max_items_per_day: int = 0,
        market: str = "all",
        include_details: bool = False,
        day_interval_seconds: float = DEFAULT_DAY_INTERVAL_SECONDS,
    ) -> list[dict[str, Any]]:
        """Fetch date-based MOPS rows for an inclusive date range.

        By default this only fetches the daily list pages. Those list rows already
        contain the full explanation text in hidden fields, so detail pages are
        optional and should be enabled sparingly.
        """
        records: list[dict[str, Any]] = []
        dates = iter_dates(start_date, end_date)
        fetched_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")

        for index, target_date in enumerate(dates):
            summaries = self.fetch_previous_day_summaries(target_date=target_date, market=market)
            selected = summaries[:max_items_per_day] if max_items_per_day > 0 else summaries

            for summary in selected:
                record = dict(summary)
                record["fetched_at"] = fetched_at
                if include_details:
                    record["detail"] = self.fetch_previous_day_detail(summary["detail_payload"])
                else:
                    record["detail"] = summary.get("detail_preview", {})
                records.append(classify_record(record))

            if index < len(dates) - 1 and day_interval_seconds > 0:
                time.sleep(day_interval_seconds)

        return sort_records_by_spoke_time(dedupe_records(records))


def save_records(records: list[dict[str, Any]], output_path: Path) -> None:
    """Save crawled records as UTF-8 JSON."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(records, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
