"""FinLab-backed EPS estimates for monthly revenue records."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import math
import os
import pickle
import re
import time
from pathlib import Path
from typing import Any

import pandas as pd


FINLAB_TOKEN_ENV = "FINLAB_TOKEN"
FINLAB_CACHE_TTL_SECONDS_ENV = "TWSE_DASHBOARD_FINLAB_CACHE_TTL_SECONDS"
FINLAB_CACHE_FILE_ENV = "TWSE_DASHBOARD_FINLAB_CACHE_FILE"
FINLAB_EPS_ENABLED_ENV = "TWSE_DASHBOARD_FINLAB_EPS_ENABLED"
DEFAULT_FINLAB_CACHE_TTL_SECONDS = 86400
DEFAULT_MONTHLY_REVENUE_DATASET = "monthly_revenue:當月營收"
DEFAULT_EPS_DATASET = "financial_statement:每股盈餘"
DEFAULT_CAPITAL_DATASET = "financial_statement:股本"
DEFAULT_NET_MARGIN_DATASET = "fundamental_features:稅後淨利率"
DEFAULT_PAR_VALUE_DATASET = "company_basic_info"
PAR_VALUE_COLUMN = "普通股每股面額"
EPS_ESTIMATE_FIELD_NAMES = (
    "estimated_eps",
    "previous_quarter_eps",
    "estimated_eps_qoq_percent",
)


@dataclass
class FinlabMonthlyRevenueInputs:
    monthly_revenue_billion: pd.DataFrame
    previous_eps: pd.DataFrame
    capital_billion: pd.DataFrame
    net_margin_percent: pd.DataFrame
    par_value: pd.DataFrame
    fetched_at: str


def current_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(str(value).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def extract_par_value(value: Any) -> float | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if str(value).strip() == "":
        return None
    text = str(value)
    match = re.search(r"\d+\.\d{2}", text)
    if match:
        return parse_float(match.group(0))
    match = re.search(r"\d+(?:\.\d+)?", text)
    return parse_float(match.group(0)) if match else None


def format_number(value: float | None, digits: int) -> str:
    if value is None or not math.isfinite(value):
        return ""
    return f"{value:.{digits}f}"


def data_month_parts(value: Any) -> tuple[int | None, int | None]:
    text = str(value or "").strip()
    if "/" not in text:
        return None, None
    year_text, month_text = text.split("/", 1)
    try:
        year = int(year_text)
        month = int(month_text)
    except ValueError:
        return None, None
    if month < 1 or month > 12:
        return None, None
    if year < 1911:
        year += 1911
    return year, month


def data_month_key(value: Any) -> tuple[int, int] | None:
    year, month = data_month_parts(value)
    if year is None or month is None:
        return None
    return year, month


def quarter_months_through(year: int, month: int) -> list[tuple[int, int]]:
    quarter_start_month = ((month - 1) // 3) * 3 + 1
    return [(year, current_month) for current_month in range(quarter_start_month, month + 1)]


def target_quarter_label(year: int, month: int) -> str:
    quarter = (month - 1) // 3 + 1
    return f"{year}Q{quarter}"


def previous_quarter_label(year: int, month: int) -> str:
    quarter = (month - 1) // 3 + 1
    if quarter == 1:
        return f"{year - 1}-Q4"
    return f"{year}-Q{quarter - 1}"


def nominal_finlab_revenue_date(year: int, month: int) -> pd.Timestamp:
    if month == 12:
        return pd.Timestamp(year + 1, 1, 10)
    return pd.Timestamp(year, month + 1, 10)


def finlab_revenue_index_for_month(
    index: pd.Index,
    year: int,
    month: int,
) -> pd.Timestamp:
    nominal = nominal_finlab_revenue_date(year, month)
    datetime_index = pd.to_datetime(index)
    matches = [
        value
        for value in datetime_index
        if value.year == nominal.year and value.month == nominal.month
    ]
    if matches:
        return max(matches)
    return nominal


def plain_dataframe(frame: Any) -> pd.DataFrame:
    result = pd.DataFrame(frame).copy()
    result.columns = [str(column).strip() for column in result.columns]
    return result


def company_par_value_frame(frame: Any) -> pd.DataFrame:
    company_info = pd.DataFrame(frame).copy()
    if "stock_id" not in company_info.columns or PAR_VALUE_COLUMN not in company_info.columns:
        return pd.DataFrame(columns=[PAR_VALUE_COLUMN])
    company_info["stock_id"] = company_info["stock_id"].astype(str).str.strip()
    result = company_info.set_index("stock_id")[[PAR_VALUE_COLUMN]]
    result[PAR_VALUE_COLUMN] = result[PAR_VALUE_COLUMN].apply(extract_par_value)
    return result.groupby(level=0).last()


def clear_eps_estimate_fields(record: dict[str, Any]) -> None:
    for field_name in EPS_ESTIMATE_FIELD_NAMES:
        record.pop(field_name, None)
    record.pop("eps_estimate_meta", None)


def write_pickle_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.tmp")
    with temp_path.open("wb") as output:
        pickle.dump(payload, output, protocol=pickle.HIGHEST_PROTOCOL)
    temp_path.replace(path)


def read_cached_finlab_inputs(path: Path) -> FinlabMonthlyRevenueInputs | None:
    if not path.exists():
        return None
    try:
        with path.open("rb") as input_file:
            payload = pickle.load(input_file)
    except (OSError, pickle.PickleError, EOFError, AttributeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        return FinlabMonthlyRevenueInputs(
            monthly_revenue_billion=payload["monthly_revenue_billion"],
            previous_eps=payload["previous_eps"],
            capital_billion=payload["capital_billion"],
            net_margin_percent=payload["net_margin_percent"],
            par_value=payload["par_value"],
            fetched_at=str(payload["fetched_at"]),
        )
    except KeyError:
        return None


def cache_age_seconds(cache_file: Path) -> float | None:
    try:
        return time.time() - cache_file.stat().st_mtime
    except OSError:
        return None


def fetch_finlab_inputs(token: str) -> FinlabMonthlyRevenueInputs:
    from finlab import data, login

    login(token)
    monthly_revenue = plain_dataframe(data.get(DEFAULT_MONTHLY_REVENUE_DATASET)) / 100000
    monthly_revenue.index = pd.to_datetime(monthly_revenue.index)
    monthly_revenue = monthly_revenue.sort_index()
    return FinlabMonthlyRevenueInputs(
        monthly_revenue_billion=monthly_revenue,
        previous_eps=plain_dataframe(data.get(DEFAULT_EPS_DATASET)),
        capital_billion=plain_dataframe(data.get(DEFAULT_CAPITAL_DATASET)) / 100000,
        net_margin_percent=plain_dataframe(data.get(DEFAULT_NET_MARGIN_DATASET)),
        par_value=company_par_value_frame(data.get(DEFAULT_PAR_VALUE_DATASET)),
        fetched_at=current_utc_iso(),
    )


def load_finlab_inputs_with_cache(
    *,
    cache_file: Path,
    token: str,
    ttl_seconds: int = DEFAULT_FINLAB_CACHE_TTL_SECONDS,
) -> tuple[FinlabMonthlyRevenueInputs | None, dict[str, Any]]:
    cached_inputs = read_cached_finlab_inputs(cache_file)
    age_seconds = cache_age_seconds(cache_file)
    if cached_inputs is not None and age_seconds is not None and age_seconds < ttl_seconds:
        return cached_inputs, {
            "ok": True,
            "enabled": True,
            "cache_hit": True,
            "stale": False,
            "cache_file": str(cache_file),
            "cache_age_seconds": int(age_seconds),
            "fetched_at": cached_inputs.fetched_at,
        }

    if not token:
        return cached_inputs, {
            "ok": cached_inputs is not None,
            "enabled": False,
            "cache_hit": cached_inputs is not None,
            "stale": cached_inputs is not None,
            "cache_file": str(cache_file),
            "error": f"{FINLAB_TOKEN_ENV} is not set",
            "fetched_at": cached_inputs.fetched_at if cached_inputs else "",
        }

    try:
        fresh_inputs = fetch_finlab_inputs(token)
        write_pickle_atomic(
            cache_file,
            {
                "monthly_revenue_billion": fresh_inputs.monthly_revenue_billion,
                "previous_eps": fresh_inputs.previous_eps,
                "capital_billion": fresh_inputs.capital_billion,
                "net_margin_percent": fresh_inputs.net_margin_percent,
                "par_value": fresh_inputs.par_value,
                "fetched_at": fresh_inputs.fetched_at,
            },
        )
        return fresh_inputs, {
            "ok": True,
            "enabled": True,
            "cache_hit": False,
            "stale": False,
            "cache_file": str(cache_file),
            "fetched_at": fresh_inputs.fetched_at,
        }
    except Exception as exc:  # pragma: no cover - depends on live FinLab service.
        return cached_inputs, {
            "ok": cached_inputs is not None,
            "enabled": True,
            "cache_hit": cached_inputs is not None,
            "stale": cached_inputs is not None,
            "cache_file": str(cache_file),
            "error": f"{type(exc).__name__}: {exc}",
            "fetched_at": cached_inputs.fetched_at if cached_inputs else "",
        }


def latest_monthly_revenue_period(records: list[dict[str, Any]]) -> tuple[int, int] | None:
    keys = [
        key
        for record in records
        if record.get("event_type") == "monthly_revenue"
        if (key := data_month_key(record.get("data_month"))) is not None
    ]
    return max(keys) if keys else None


def selected_latest_revenue_records(
    records: list[dict[str, Any]],
    latest_period: tuple[int, int],
) -> dict[str, dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    for record in records:
        if record.get("event_type") != "monthly_revenue":
            continue
        if data_month_key(record.get("data_month")) != latest_period:
            continue
        company_id = str(record.get("company_id") or "").strip()
        if company_id and company_id not in selected:
            selected[company_id] = record
    return selected


def calculate_estimate_for_company(
    *,
    company_id: str,
    latest_period: tuple[int, int],
    latest_record: dict[str, Any],
    inputs: FinlabMonthlyRevenueInputs,
) -> tuple[dict[str, Any] | None, str]:
    year, month = latest_period
    revenue_frame = inputs.monthly_revenue_billion
    if company_id not in revenue_frame.columns:
        return None, "missing_finlab_monthly_revenue_column"

    current_revenue_date = finlab_revenue_index_for_month(revenue_frame.index, year, month)
    if current_revenue_date not in revenue_frame.index:
        revenue_frame.loc[current_revenue_date] = pd.NA
        revenue_frame.sort_index(inplace=True)

    latest_revenue = parse_float(latest_record.get("monthly_revenue"))
    if latest_revenue is None:
        return None, "missing_latest_monthly_revenue"
    revenue_frame.at[current_revenue_date, company_id] = latest_revenue / 100000

    revenue_values: list[float] = []
    used_revenue_dates: list[str] = []
    for revenue_year, revenue_month in quarter_months_through(year, month):
        revenue_date = finlab_revenue_index_for_month(
            revenue_frame.index,
            revenue_year,
            revenue_month,
        )
        if revenue_date not in revenue_frame.index:
            return None, "missing_quarter_month_revenue"
        revenue_value = parse_float(revenue_frame.at[revenue_date, company_id])
        if revenue_value is None:
            return None, "missing_quarter_month_revenue"
        revenue_values.append(revenue_value)
        used_revenue_dates.append(revenue_date.date().isoformat())

    previous_quarter = previous_quarter_label(year, month)
    if (
        company_id not in inputs.previous_eps.columns
        or company_id not in inputs.capital_billion.columns
        or company_id not in inputs.net_margin_percent.columns
    ):
        return None, "missing_financial_statement_column"
    if PAR_VALUE_COLUMN not in inputs.par_value.columns or company_id not in inputs.par_value.index:
        return None, "missing_par_value"
    if (
        previous_quarter not in inputs.previous_eps.index
        or previous_quarter not in inputs.capital_billion.index
        or previous_quarter not in inputs.net_margin_percent.index
    ):
        return None, "missing_previous_quarter_row"

    previous_eps = parse_float(inputs.previous_eps.at[previous_quarter, company_id])
    capital_billion = parse_float(inputs.capital_billion.at[previous_quarter, company_id])
    net_margin_percent = parse_float(inputs.net_margin_percent.at[previous_quarter, company_id])
    par_value = parse_float(inputs.par_value.at[company_id, PAR_VALUE_COLUMN])
    if previous_eps is None:
        return None, "missing_previous_eps"
    if capital_billion in (None, 0):
        return None, "missing_or_zero_capital"
    if net_margin_percent is None:
        return None, "missing_net_margin"
    if par_value in (None, 0):
        return None, "missing_or_zero_par_value"

    known_month_count = len(revenue_values)
    ratio = 3 / known_month_count
    partial_quarter_revenue_billion = sum(revenue_values)
    estimated_quarter_revenue_billion = partial_quarter_revenue_billion * ratio
    net_margin_ratio = net_margin_percent / 100
    estimated_eps = estimated_quarter_revenue_billion * net_margin_ratio / capital_billion * par_value
    estimated_qoq = None if previous_eps == 0 else (estimated_eps / previous_eps - 1) * 100

    return {
        "estimated_eps": format_number(estimated_eps, 3),
        "previous_quarter_eps": format_number(previous_eps, 3),
        "estimated_eps_qoq_percent": format_number(estimated_qoq, 2),
        "eps_estimate_meta": {
            "source": "FinLab + crawler monthly revenue",
            "target_quarter": target_quarter_label(year, month),
            "previous_quarter": previous_quarter,
            "finlab_revenue_date": current_revenue_date.date().isoformat(),
            "used_revenue_dates": used_revenue_dates,
            "known_month_count": known_month_count,
            "ratio": format_number(ratio, 3),
            "partial_quarter_revenue_billion": format_number(
                partial_quarter_revenue_billion,
                4,
            ),
            "estimated_quarter_revenue_billion": format_number(
                estimated_quarter_revenue_billion,
                4,
            ),
            "previous_quarter_net_margin_percent": format_number(
                net_margin_percent,
                6,
            ),
            "previous_quarter_capital_billion": format_number(capital_billion, 4),
            "par_value": format_number(par_value, 4),
            "par_value_source": f"{DEFAULT_PAR_VALUE_DATASET}:{PAR_VALUE_COLUMN}",
            "eps_denominator_method": "capital_billion_times_par_value",
        },
    }, "ok"


def enrich_latest_monthly_revenue_eps(
    records: list[dict[str, Any]],
    inputs: FinlabMonthlyRevenueInputs,
) -> dict[str, Any]:
    latest_period = latest_monthly_revenue_period(records)
    if latest_period is None:
        return {
            "ok": True,
            "enabled": True,
            "enriched_count": 0,
            "skipped_count": 0,
            "skipped_reasons": {},
        }

    latest_records_by_company = selected_latest_revenue_records(records, latest_period)
    estimates_by_company: dict[str, dict[str, Any]] = {}
    skipped_reasons: Counter[str] = Counter()
    for company_id, latest_record in latest_records_by_company.items():
        estimate, reason = calculate_estimate_for_company(
            company_id=company_id,
            latest_period=latest_period,
            latest_record=latest_record,
            inputs=inputs,
        )
        if estimate is None:
            skipped_reasons[reason] += 1
            continue
        estimates_by_company[company_id] = estimate

    latest_roc = f"{latest_period[0] - 1911:03d}/{latest_period[1]:02d}"
    enriched_count = 0
    for record in records:
        if record.get("event_type") != "monthly_revenue":
            continue
        if data_month_key(record.get("data_month")) != latest_period:
            continue
        company_id = str(record.get("company_id") or "").strip()
        clear_eps_estimate_fields(record)
        estimate = estimates_by_company.get(company_id)
        if not estimate:
            continue
        record.update(estimate)
        enriched_count += 1

    return {
        "ok": True,
        "enabled": True,
        "source": "FinLab + crawler monthly revenue",
        "latest_data_month": latest_roc,
        "target_quarter": target_quarter_label(*latest_period),
        "previous_quarter": previous_quarter_label(*latest_period),
        "enriched_count": enriched_count,
        "skipped_count": sum(skipped_reasons.values()),
        "skipped_reasons": dict(sorted(skipped_reasons.items())),
    }


def enrich_records_with_cached_finlab_eps(
    records: list[dict[str, Any]],
    *,
    cache_file: Path,
    token: str,
    ttl_seconds: int = DEFAULT_FINLAB_CACHE_TTL_SECONDS,
) -> dict[str, Any]:
    inputs, finlab_status = load_finlab_inputs_with_cache(
        cache_file=cache_file,
        token=token,
        ttl_seconds=ttl_seconds,
    )
    if inputs is None:
        return {
            "ok": False,
            "enabled": bool(token),
            "finlab": finlab_status,
            "enriched_count": 0,
            "skipped_count": 0,
            "skipped_reasons": {},
        }
    enrichment = enrich_latest_monthly_revenue_eps(records, inputs)
    enrichment["finlab"] = finlab_status
    return enrichment


def env_finlab_token() -> str:
    return os.environ.get(FINLAB_TOKEN_ENV, "").strip()
