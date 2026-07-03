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
