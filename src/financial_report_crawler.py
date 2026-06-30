"""Parsers for MOPS financial-report material-information rows."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from mops_crawler import normalize_time, roc_date_to_iso

NUMBER_PATTERN = re.compile(r"\(?-?\d[\d,]*(?:\.\d+)?\)?")
ROC_PERIOD_PATTERN = re.compile(
    r"(\d{3})\s*/\s*(\d{1,2})\s*/\s*(\d{1,2})\s*[~～]\s*"
    r"(\d{3})\s*/\s*(\d{1,2})\s*/\s*(\d{1,2})"
)
QUARTER_TEXT_PATTERN = re.compile(r"(\d{3})\s*年?\s*第\s*([1-4一二三四])\s*季")
QUARTER_WORDS = {"一": 1, "二": 2, "三": 3, "四": 4}
FINANCIAL_REPORT_KEYWORDS = ("財務報告", "財報")


def parse_number_token(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    match = NUMBER_PATTERN.search(text)
    if not match:
        return None
    token = match.group(0).strip()
    negative_by_parentheses = token.startswith("(") and token.endswith(")")
    cleaned = token.strip("()").replace(",", "")
    try:
        parsed = float(cleaned)
    except ValueError:
        return None
    return -abs(parsed) if negative_by_parentheses else parsed


def parse_amount_to_thousand(value: Any, default_unit: str = "thousand") -> float | None:
    text = str(value or "")
    amount = parse_number_token(text)
    if amount is None:
        return None

    compact = re.sub(r"\s+", "", text)
    if "億元" in compact:
        return amount * 100_000
    if "百萬元" in compact:
        return amount * 1_000
    if "萬元" in compact:
        return amount * 10
    if "元" in compact and "仟元" not in compact and "千元" not in compact:
        return amount / 1_000
    if default_unit == "yuan":
        return amount / 1_000
    return amount


def parse_percent_token(value: Any) -> float | None:
    text = str(value or "")
    match = re.search(r"\(?-?\d[\d,]*(?:\.\d+)?\)?\s*%", text)
    if not match:
        return None
    return parse_number_token(match.group(0))


def round_ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    return round(numerator / denominator * 100, 2)


def value_after_label(text: str, labels: tuple[str, ...]) -> str:
    for line in text.splitlines():
        if not any(label in line for label in labels):
            continue
        if ":" in line:
            return line.rsplit(":", 1)[-1]
        if "：" in line:
            return line.rsplit("：", 1)[-1]
        return line
    return ""


def report_quarter_from_text(text: str) -> tuple[str, str, int | None, int | None]:
    match = ROC_PERIOD_PATTERN.search(text)
    if match:
        roc_year = int(match.group(4))
        month = int(match.group(5))
        quarter = (month - 1) // 3 + 1
        year = roc_year + 1911
        period = (
            f"{int(match.group(1)):03d}/{int(match.group(2)):02d}/{int(match.group(3)):02d}"
            f"~{roc_year:03d}/{month:02d}/{int(match.group(6)):02d}"
        )
        return f"{year}Q{quarter}", period, year, quarter

    match = QUARTER_TEXT_PATTERN.search(text)
    if not match:
        return "", "", None, None
    quarter_text = match.group(2)
    quarter = QUARTER_WORDS.get(quarter_text, int(quarter_text) if quarter_text.isdigit() else 0)
    if not quarter:
        return "", "", None, None
    year = int(match.group(1)) + 1911
    return f"{year}Q{quarter}", "", year, quarter


def parse_financial_report_metrics(description: str, subject: str = "") -> dict[str, Any]:
    text = description or ""
    full_text = f"{subject}\n{text}"
    quarter, reporting_period, report_year, report_quarter = report_quarter_from_text(full_text)
    revenue_k = parse_amount_to_thousand(
        value_after_label(text, ("營業收入", "營收", "合併營收"))
    )
    gross_profit_k = parse_amount_to_thousand(value_after_label(text, ("營業毛利",)))
    operating_income_k = parse_amount_to_thousand(
        value_after_label(text, ("營業利益", "營業淨利"))
    )
    pretax_income_k = parse_amount_to_thousand(value_after_label(text, ("稅前淨利",)))
    net_income_k = parse_amount_to_thousand(value_after_label(text, ("本期淨利",)))
    parent_net_income_k = parse_amount_to_thousand(value_after_label(text, ("歸屬於母公司業主淨利",)))
    eps = parse_number_token(value_after_label(text, ("基本每股盈餘", "每股盈餘", "EPS")))

    gross_margin_pct = round_ratio(gross_profit_k, revenue_k)
    if gross_margin_pct is None:
        gross_margin_pct = parse_percent_token(value_after_label(text, ("毛利率",)))
    operating_margin_pct = round_ratio(operating_income_k, revenue_k)
    if operating_margin_pct is None:
        operating_margin_pct = parse_percent_token(value_after_label(text, ("營業利益率", "營益率")))

    non_operating_k = (
        pretax_income_k - operating_income_k
        if pretax_income_k is not None and operating_income_k is not None
        else None
    )
    non_operating_pct = round_ratio(non_operating_k, pretax_income_k)

    return {
        "quarter": quarter,
        "reporting_period": reporting_period,
        "report_year": report_year,
        "report_quarter": report_quarter,
        "revenue_k": revenue_k,
        "gross_profit_k": gross_profit_k,
        "operating_income_k": operating_income_k,
        "pretax_income_k": pretax_income_k,
        "net_income_k": net_income_k,
        "parent_net_income_k": parent_net_income_k,
        "eps": eps,
        "gross_margin_pct": gross_margin_pct,
        "operating_margin_pct": operating_margin_pct,
        "non_operating_k": non_operating_k,
        "non_operating_pct": non_operating_pct,
        "has_line_item_metrics": bool(revenue_k is not None and eps is not None),
    }


def is_financial_report_announcement(record: dict[str, Any]) -> bool:
    detail = record.get("detail") or record.get("detail_preview") or {}
    text = "\n".join(
        [
            str(record.get("subject", "")),
            str(detail.get("description", "")),
        ]
    )
    return any(keyword in text for keyword in FINANCIAL_REPORT_KEYWORDS)


def financial_report_identity_key(record: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        str(record.get("company_id", "")),
        str(record.get("spoke_date", "")),
        str(record.get("spoke_time", "")),
        str(record.get("quarter", "")),
        str(record.get("title") or record.get("subject") or ""),
    )


def build_financial_report_record(
    record: dict[str, Any],
    *,
    target_quarter: str = "",
    detected_at: str | None = None,
) -> dict[str, Any] | None:
    if not is_financial_report_announcement(record):
        return None

    detail = record.get("detail") or record.get("detail_preview") or {}
    description = str(detail.get("description") or "")
    subject = str(record.get("subject") or "")
    metrics = parse_financial_report_metrics(description, subject)
    if target_quarter and metrics.get("quarter") != target_quarter:
        return None
    if not metrics.get("quarter") or not metrics.get("has_line_item_metrics"):
        return None

    spoke_date = str(record.get("spoke_date") or "")
    if not spoke_date and record.get("spoke_date_roc"):
        spoke_date = roc_date_to_iso(str(record.get("spoke_date_roc")))
    spoke_time = normalize_time(str(record.get("spoke_time") or ""))
    announced_at = f"{spoke_date}T{spoke_time or '00:00:00'}" if spoke_date else ""

    return {
        **record,
        "source_type": "mops_material_financial_report",
        "source_label": "重大訊息-財報",
        "event_type": "financial_report",
        "title": subject,
        "quarter": metrics.get("quarter", ""),
        "eps": metrics.get("eps"),
        "gross_margin_pct": metrics.get("gross_margin_pct"),
        "operating_margin_pct": metrics.get("operating_margin_pct"),
        "non_operating_pct": metrics.get("non_operating_pct"),
        "announced_at": announced_at,
        "event_time": announced_at,
        "detected_at": detected_at or datetime.now().astimezone().isoformat(timespec="seconds"),
        "metrics": metrics,
        "detail": detail,
    }


def dedupe_financial_report_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str, str, str]] = set()
    unique: list[dict[str, Any]] = []
    for record in records:
        key = financial_report_identity_key(record)
        if key in seen:
            continue
        seen.add(key)
        unique.append(record)
    return unique
