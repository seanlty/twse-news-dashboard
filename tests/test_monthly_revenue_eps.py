import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from monthly_revenue_eps import (  # noqa: E402
    FinlabMonthlyRevenueInputs,
    enrich_latest_monthly_revenue_eps,
)


def test_enrich_latest_monthly_revenue_eps_uses_crawler_latest_month() -> None:
    inputs = FinlabMonthlyRevenueInputs(
        monthly_revenue_billion=pd.DataFrame(
            {
                "2330": [1.0, 2.0, None],
            },
            index=pd.to_datetime(["2026-05-10", "2026-06-10", "2026-07-10"]),
        ),
        previous_eps=pd.DataFrame({"2330": [0.6]}, index=["2026-Q1"]),
        capital_billion=pd.DataFrame({"2330": [10.0]}, index=["2026-Q1"]),
        net_margin_percent=pd.DataFrame({"2330": [20.0]}, index=["2026-Q1"]),
        par_value=pd.DataFrame({"普通股每股面額": [10.0]}, index=["2330"]),
        fetched_at="2026-07-03T00:00:00+00:00",
    )
    records = [
        {
            "event_type": "monthly_revenue",
            "company_id": "2330",
            "company_name": "台積電",
            "data_month": "115/06",
            "monthly_revenue": "300000",
        }
    ]

    result = enrich_latest_monthly_revenue_eps(records, inputs)

    assert result["enriched_count"] == 1
    assert records[0]["estimated_eps"] == "1.200"
    assert records[0]["previous_quarter_eps"] == "0.600"
    assert records[0]["estimated_eps_qoq_percent"] == "100.00"
    assert records[0]["eps_estimate_meta"]["target_quarter"] == "2026Q2"
    assert records[0]["eps_estimate_meta"]["previous_quarter"] == "2026-Q1"
    assert records[0]["eps_estimate_meta"]["ratio"] == "1.000"
    assert records[0]["eps_estimate_meta"]["par_value"] == "10.0000"


def test_enrich_latest_monthly_revenue_eps_partial_quarter_ratio() -> None:
    inputs = FinlabMonthlyRevenueInputs(
        monthly_revenue_billion=pd.DataFrame(
            {
                "2330": [1.0, None],
            },
            index=pd.to_datetime(["2026-05-10", "2026-06-10"]),
        ),
        previous_eps=pd.DataFrame({"2330": [0.6]}, index=["2026-Q1"]),
        capital_billion=pd.DataFrame({"2330": [10.0]}, index=["2026-Q1"]),
        net_margin_percent=pd.DataFrame({"2330": [20.0]}, index=["2026-Q1"]),
        par_value=pd.DataFrame({"普通股每股面額": [10.0]}, index=["2330"]),
        fetched_at="2026-07-03T00:00:00+00:00",
    )
    records = [
        {
            "event_type": "monthly_revenue",
            "company_id": "2330",
            "company_name": "台積電",
            "data_month": "115/05",
            "monthly_revenue": "300000",
        }
    ]

    result = enrich_latest_monthly_revenue_eps(records, inputs)

    assert result["enriched_count"] == 1
    assert records[0]["estimated_eps"] == "1.200"
    assert records[0]["eps_estimate_meta"]["target_quarter"] == "2026Q2"
    assert records[0]["eps_estimate_meta"]["ratio"] == "1.500"


def test_enrich_latest_monthly_revenue_eps_uses_company_par_value() -> None:
    inputs = FinlabMonthlyRevenueInputs(
        monthly_revenue_billion=pd.DataFrame(
            {
                "8070": [22.32043],
            },
            index=pd.to_datetime(["2026-08-10"]),
        ),
        previous_eps=pd.DataFrame({"8070": [0.78]}, index=["2026-Q2"]),
        capital_billion=pd.DataFrame({"8070": [7.25648]}, index=["2026-Q2"]),
        net_margin_percent=pd.DataFrame({"8070": [13.629873851667416]}, index=["2026-Q2"]),
        par_value=pd.DataFrame({"普通股每股面額": [1.0]}, index=["8070"]),
        fetched_at="2026-08-13T00:00:00+00:00",
    )
    records = [
        {
            "event_type": "monthly_revenue",
            "company_id": "8070",
            "company_name": "長華",
            "data_month": "115/07",
            "monthly_revenue": "2232043",
        }
    ]

    result = enrich_latest_monthly_revenue_eps(records, inputs)

    assert result["enriched_count"] == 1
    assert records[0]["estimated_eps"] == "1.258"
    assert records[0]["estimated_eps_qoq_percent"] == "61.25"
    assert records[0]["eps_estimate_meta"]["par_value"] == "1.0000"
    assert records[0]["eps_estimate_meta"]["par_value_source"] == (
        "company_basic_info:普通股每股面額"
    )
