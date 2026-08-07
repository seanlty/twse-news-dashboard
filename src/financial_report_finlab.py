"""FinLab-backed single-quarter metrics for financial-report records."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import math
import os
import pickle
import time
from pathlib import Path
from typing import Any

import pandas as pd


FINLAB_TOKEN_ENV = "FINLAB_TOKEN"
DEFAULT_FINLAB_CACHE_TTL_SECONDS = 86400
DEFAULT_EPS_DATASET = "financial_statement:每股盈餘"
DEFAULT_REVENUE_DATASET = "financial_statement:營業收入淨額"
DEFAULT_GROSS_PROFIT_DATASET = "financial_statement:營業毛利"
DEFAULT_OPERATING_INCOME_DATASET = "financial_statement:營業利益"
DEFAULT_PRETAX_INCOME_DATASET = "financial_statement:稅前淨利"
FINANCIAL_REPORT_ENRICHMENT_FIELD_NAMES = (
    "previous_quarter",
    "previous_finlab_quarter",
    "prior_ytd_finlab_quarters",
    "previous_quarter_eps",
    "previous_quarter_gross_margin_pct",
    "previous_quarter_operating_margin_pct",
    "previous_quarter_non_operating_pct",
    "single_quarter_eps",
    "eps_growth_pct",
    "single_quarter_gross_margin_pct",
    "single_quarter_operating_margin_pct",
    "single_quarter_non_operating_pct",
    "gross_margin_growth_pct",
    "financial_report_finlab_meta",
)


@dataclass
class FinlabFinancialReportInputs:
    eps: pd.DataFrame
    revenue_k: pd.DataFrame
    gross_profit_k: pd.DataFrame
    operating_income_k: pd.DataFrame
    pretax_income_k: pd.DataFrame
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


def rounded(value: float | None, digits: int = 2) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(value, digits)


def ratio_pct(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    return numerator / denominator * 100


def growth_rate_pct(current: float | None, previous: float | None) -> float | None:
    if current is None or previous in (None, 0):
        return None
    return (current - previous) / abs(previous) * 100


def normalize_quarter(value: Any) -> str:
    text = str(value or "").strip().upper().replace("-", "")
    if len(text) != 6 or not text[:4].isdigit() or text[4] != "Q" or text[5] not in "1234":
        return ""
    return text


def quarter_key(value: Any) -> tuple[int, int] | None:
    quarter = normalize_quarter(value)
    if not quarter:
        return None
    return int(quarter[:4]), int(quarter[-1])


def quarter_label(year: int, quarter: int) -> str:
    return f"{year}Q{quarter}"


def previous_quarter_label(quarter: str) -> str:
    key = quarter_key(quarter)
    if key is None:
        return ""
    year, q = key
    if q == 1:
        return f"{year - 1}Q4"
    return f"{year}Q{q - 1}"


def finlab_quarter_label(quarter: str) -> str:
    normalized = normalize_quarter(quarter)
    if not normalized:
        return ""
    return f"{normalized[:4]}-Q{normalized[-1]}"


def prior_finlab_quarter_labels(quarter: str) -> list[str]:
    key = quarter_key(quarter)
    if key is None:
        return []
    year, q = key
    return [f"{year}-Q{previous_q}" for previous_q in range(1, q)]


def plain_dataframe(frame: Any) -> pd.DataFrame:
    result = pd.DataFrame(frame).copy()
    result.columns = [str(column).strip() for column in result.columns]
    return result


def write_pickle_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.tmp")
    with temp_path.open("wb") as output:
        pickle.dump(payload, output, protocol=pickle.HIGHEST_PROTOCOL)
    temp_path.replace(path)


def cache_age_seconds(cache_file: Path) -> float | None:
    try:
        return time.time() - cache_file.stat().st_mtime
    except OSError:
        return None


def read_cached_finlab_inputs(path: Path) -> FinlabFinancialReportInputs | None:
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
        return FinlabFinancialReportInputs(
            eps=payload["eps"],
            revenue_k=payload["revenue_k"],
            gross_profit_k=payload["gross_profit_k"],
            operating_income_k=payload["operating_income_k"],
            pretax_income_k=payload["pretax_income_k"],
            fetched_at=str(payload["fetched_at"]),
        )
    except KeyError:
        return None


def fetch_finlab_inputs(token: str) -> FinlabFinancialReportInputs:
    from finlab import data, login

    login(token)
    return FinlabFinancialReportInputs(
        eps=plain_dataframe(data.get(DEFAULT_EPS_DATASET)),
        revenue_k=plain_dataframe(data.get(DEFAULT_REVENUE_DATASET)),
        gross_profit_k=plain_dataframe(data.get(DEFAULT_GROSS_PROFIT_DATASET)),
        operating_income_k=plain_dataframe(data.get(DEFAULT_OPERATING_INCOME_DATASET)),
        pretax_income_k=plain_dataframe(data.get(DEFAULT_PRETAX_INCOME_DATASET)),
        fetched_at=current_utc_iso(),
    )


def load_finlab_inputs_with_cache(
    *,
    cache_file: Path,
    token: str,
    ttl_seconds: int = DEFAULT_FINLAB_CACHE_TTL_SECONDS,
) -> tuple[FinlabFinancialReportInputs | None, dict[str, Any]]:
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
                "eps": fresh_inputs.eps,
                "revenue_k": fresh_inputs.revenue_k,
                "gross_profit_k": fresh_inputs.gross_profit_k,
                "operating_income_k": fresh_inputs.operating_income_k,
                "pretax_income_k": fresh_inputs.pretax_income_k,
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


def frame_value(frame: pd.DataFrame, quarter: str, code: str) -> float | None:
    if quarter not in frame.index or code not in frame.columns:
        return None
    return parse_float(frame.at[quarter, code])


def frame_sum(frame: pd.DataFrame, quarters: list[str], code: str) -> float | None:
    total = 0.0
    for quarter in quarters:
        value = frame_value(frame, quarter, code)
        if value is None:
            return None
        total += value
    return total


def record_metric(record: dict[str, Any], key: str) -> float | None:
    metrics = record.get("metrics") or {}
    value = metrics.get(key)
    if value in (None, ""):
        value = record.get(key)
    return parse_float(value)


def clear_financial_report_enrichment(record: dict[str, Any]) -> None:
    for field_name in FINANCIAL_REPORT_ENRICHMENT_FIELD_NAMES:
        record.pop(field_name, None)


def calculate_record_enrichment(
    record: dict[str, Any],
    inputs: FinlabFinancialReportInputs,
) -> tuple[dict[str, Any] | None, str]:
    code = str(record.get("company_id") or "").strip()
    quarter = normalize_quarter(record.get("quarter"))
    if not code:
        return None, "missing_company_id"
    if not quarter:
        return None, "missing_quarter"

    previous_quarter = previous_quarter_label(quarter)
    previous_finlab_quarter = finlab_quarter_label(previous_quarter)
    prior_ytd_quarters = prior_finlab_quarter_labels(quarter)

    required_frames = [
        inputs.eps,
        inputs.revenue_k,
        inputs.operating_income_k,
        inputs.pretax_income_k,
    ]
    if any(code not in frame.columns for frame in required_frames):
        return None, "missing_financial_statement_column"

    prior_ytd_eps = frame_sum(inputs.eps, prior_ytd_quarters, code)
    prior_ytd_revenue = frame_sum(inputs.revenue_k, prior_ytd_quarters, code)
    prior_ytd_operating = frame_sum(inputs.operating_income_k, prior_ytd_quarters, code)
    prior_ytd_pretax = frame_sum(inputs.pretax_income_k, prior_ytd_quarters, code)
    prior_ytd_gross = (
        frame_sum(inputs.gross_profit_k, prior_ytd_quarters, code)
        if code in inputs.gross_profit_k.columns
        else None
    )

    ytd_eps = record_metric(record, "eps")
    ytd_revenue = record_metric(record, "revenue_k")
    ytd_gross = record_metric(record, "gross_profit_k")
    ytd_operating = record_metric(record, "operating_income_k")
    ytd_pretax = record_metric(record, "pretax_income_k")

    single_eps = None if ytd_eps is None or prior_ytd_eps is None else ytd_eps - prior_ytd_eps
    single_revenue = (
        None
        if ytd_revenue is None or prior_ytd_revenue is None
        else ytd_revenue - prior_ytd_revenue
    )
    single_gross = None if ytd_gross is None or prior_ytd_gross is None else ytd_gross - prior_ytd_gross
    single_operating = (
        None
        if ytd_operating is None or prior_ytd_operating is None
        else ytd_operating - prior_ytd_operating
    )
    single_pretax = (
        None
        if ytd_pretax is None or prior_ytd_pretax is None
        else ytd_pretax - prior_ytd_pretax
    )
    single_non_operating = (
        None
        if single_pretax is None or single_operating is None
        else single_pretax - single_operating
    )

    previous_eps = frame_value(inputs.eps, previous_finlab_quarter, code)
    previous_revenue = frame_value(inputs.revenue_k, previous_finlab_quarter, code)
    previous_gross = (
        frame_value(inputs.gross_profit_k, previous_finlab_quarter, code)
        if code in inputs.gross_profit_k.columns
        else None
    )
    previous_operating = frame_value(inputs.operating_income_k, previous_finlab_quarter, code)
    previous_pretax = frame_value(inputs.pretax_income_k, previous_finlab_quarter, code)
    previous_non_operating = (
        None
        if previous_pretax is None or previous_operating is None
        else previous_pretax - previous_operating
    )

    single_gross_margin = ratio_pct(single_gross, single_revenue)
    single_operating_margin = ratio_pct(single_operating, single_revenue)
    single_non_operating_pct = ratio_pct(single_non_operating, single_pretax)
    previous_gross_margin = ratio_pct(previous_gross, previous_revenue)
    previous_operating_margin = ratio_pct(previous_operating, previous_revenue)
    previous_non_operating_pct = ratio_pct(previous_non_operating, previous_pretax)
    eps_growth = growth_rate_pct(single_eps, previous_eps)
    gross_margin_growth = growth_rate_pct(single_gross_margin, previous_gross_margin)

    enrichment = {
        "previous_quarter": previous_quarter,
        "previous_finlab_quarter": previous_finlab_quarter,
        "prior_ytd_finlab_quarters": prior_ytd_quarters,
        "previous_quarter_eps": rounded(previous_eps, 3),
        "previous_quarter_gross_margin_pct": rounded(previous_gross_margin, 2),
        "previous_quarter_operating_margin_pct": rounded(previous_operating_margin, 2),
        "previous_quarter_non_operating_pct": rounded(previous_non_operating_pct, 2),
        "single_quarter_eps": rounded(single_eps, 3),
        "eps_growth_pct": rounded(eps_growth, 4),
        "single_quarter_gross_margin_pct": rounded(single_gross_margin, 2),
        "single_quarter_operating_margin_pct": rounded(single_operating_margin, 2),
        "single_quarter_non_operating_pct": rounded(single_non_operating_pct, 2),
        "gross_margin_growth_pct": rounded(gross_margin_growth, 4),
        "financial_report_finlab_meta": {
            "source": "MOPS financial report + FinLab financial_statement",
            "target_quarter": quarter,
            "previous_quarter": previous_quarter,
            "previous_finlab_quarter": previous_finlab_quarter,
            "prior_ytd_finlab_quarters": prior_ytd_quarters,
            "finlab_fetched_at": inputs.fetched_at,
        },
    }
    if not any(
        enrichment.get(field_name) is not None
        for field_name in (
            "single_quarter_eps",
            "single_quarter_gross_margin_pct",
            "single_quarter_operating_margin_pct",
            "single_quarter_non_operating_pct",
        )
    ):
        return None, "missing_single_quarter_metrics"
    return enrichment, "ok"


def enrich_financial_report_records(
    records: list[dict[str, Any]],
    inputs: FinlabFinancialReportInputs,
) -> dict[str, Any]:
    enriched_count = 0
    skipped_reasons: Counter[str] = Counter()
    for record in records:
        if record.get("event_type") != "financial_report":
            continue
        clear_financial_report_enrichment(record)
        enrichment, reason = calculate_record_enrichment(record, inputs)
        if enrichment is None:
            skipped_reasons[reason] += 1
            continue
        record.update(enrichment)
        enriched_count += 1

    return {
        "ok": True,
        "enabled": True,
        "source": "MOPS financial report + FinLab financial_statement",
        "enriched_count": enriched_count,
        "skipped_count": sum(skipped_reasons.values()),
        "skipped_reasons": dict(sorted(skipped_reasons.items())),
    }


def enrich_records_with_cached_finlab_financials(
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
    enrichment = enrich_financial_report_records(records, inputs)
    enrichment["finlab"] = finlab_status
    return enrichment


def env_finlab_token() -> str:
    return os.environ.get(FINLAB_TOKEN_ENV, "").strip()
