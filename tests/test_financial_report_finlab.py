import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from financial_report_finlab import (  # noqa: E402
    FinlabFinancialReportInputs,
    enrich_financial_report_records,
    prior_finlab_quarter_labels,
    previous_quarter_label,
)


def sample_inputs() -> FinlabFinancialReportInputs:
    index = ["2026-Q1", "2026-Q2"]
    return FinlabFinancialReportInputs(
        eps=pd.DataFrame({"2330": [1.0, 2.5]}, index=index),
        revenue_k=pd.DataFrame({"2330": [100.0, 200.0]}, index=index),
        gross_profit_k=pd.DataFrame({"2330": [40.0, 110.0]}, index=index),
        operating_income_k=pd.DataFrame({"2330": [20.0, 60.0]}, index=index),
        pretax_income_k=pd.DataFrame({"2330": [25.0, 75.0]}, index=index),
        fetched_at="2026-07-27T00:00:00+00:00",
    )


def test_financial_report_single_quarter_enrichment_for_q2() -> None:
    records = [
        {
            "event_type": "financial_report",
            "company_id": "2330",
            "quarter": "2026Q2",
            "eps": 3.5,
            "metrics": {
                "revenue_k": 300.0,
                "gross_profit_k": 150.0,
                "operating_income_k": 80.0,
                "pretax_income_k": 100.0,
            },
        }
    ]

    result = enrich_financial_report_records(records, sample_inputs())

    assert result["enriched_count"] == 1
    assert records[0]["previous_quarter"] == "2026Q1"
    assert records[0]["previous_finlab_quarter"] == "2026-Q1"
    assert records[0]["prior_ytd_finlab_quarters"] == ["2026-Q1"]
    assert records[0]["previous_quarter_eps"] == 1.0
    assert records[0]["single_quarter_eps"] == 2.5
    assert records[0]["previous_quarter_gross_margin_pct"] == 40.0
    assert records[0]["single_quarter_gross_margin_pct"] == 55.0
    assert records[0]["gross_margin_growth_pct"] == 15.0
    assert records[0]["single_quarter_operating_margin_pct"] == 30.0
    assert records[0]["single_quarter_non_operating_pct"] == 20.0


def test_financial_report_single_quarter_enrichment_for_q3_rolls_prior_ytd() -> None:
    records = [
        {
            "event_type": "financial_report",
            "company_id": "2330",
            "quarter": "2026Q3",
            "eps": 7.0,
            "metrics": {
                "revenue_k": 700.0,
                "gross_profit_k": 350.0,
                "operating_income_k": 210.0,
                "pretax_income_k": 280.0,
            },
        }
    ]

    result = enrich_financial_report_records(records, sample_inputs())

    assert result["enriched_count"] == 1
    assert previous_quarter_label("2026Q3") == "2026Q2"
    assert prior_finlab_quarter_labels("2026Q3") == ["2026-Q1", "2026-Q2"]
    assert records[0]["previous_finlab_quarter"] == "2026-Q2"
    assert records[0]["prior_ytd_finlab_quarters"] == ["2026-Q1", "2026-Q2"]
    assert records[0]["previous_quarter_eps"] == 2.5
    assert records[0]["single_quarter_eps"] == 3.5
    assert records[0]["previous_quarter_gross_margin_pct"] == 55.0
    assert records[0]["single_quarter_gross_margin_pct"] == 50.0
    assert records[0]["gross_margin_growth_pct"] == -5.0
    assert records[0]["single_quarter_operating_margin_pct"] == 32.5
    assert records[0]["single_quarter_non_operating_pct"] == 27.78
