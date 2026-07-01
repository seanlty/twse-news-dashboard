import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from financial_report_crawler import (  # noqa: E402
    build_financial_report_record,
    dedupe_financial_report_records,
    parse_financial_report_metrics,
)


OFFICIAL_Q1_DETAIL = """
1.提報董事會或經董事會決議日期:115/05/14
2.審計委員會通過日期:115/05/14
3.財務報告或年度自結財務資訊報導期間
起訖日期(XXX/XX/XX~XXX/XX/XX):115/01/01~115/03/31
4.1月1日累計至本期止營業收入(仟元):568,015
5.1月1日累計至本期止營業毛利(毛損) (仟元):153,080
6.1月1日累計至本期止營業利益(損失) (仟元):126,103
7.1月1日累計至本期止稅前淨利(淨損) (仟元):18,234
8.1月1日累計至本期止本期淨利(淨損) (仟元):11,196
9.1月1日累計至本期止歸屬於母公司業主淨利(損) (仟元):(13,770)
10.1月1日累計至本期止基本每股盈餘(損失) (元):(0.02)
"""


SPEECH_STYLE_Q1_DETAIL = """
本公司今日(8)於法人說明會中公佈115年第1季合併財務報告及115年第2季業績展望。

115年第1季合併財務報告如下：
‧合併營收：新台幣32.25億元，季增12.8%、年增3.4%。
‧毛利率：47.7%，季減1.6百分點，年減2.4百分點。
‧營業淨利：新台幣7.79億元，季增17.9%，年減0.8%。
‧本期淨利(歸屬於母公司)：新台幣7.05億元，季減4.1%，年增28.4%。
‧每股盈餘：新台幣2.46元，季減4.3%，年增28.1%。
"""


def test_parse_financial_report_metrics_from_official_mops_line_items() -> None:
    metrics = parse_financial_report_metrics(
        OFFICIAL_Q1_DETAIL,
        "公告本公司董事會通過115年第一季合併財務報告",
    )

    assert metrics["quarter"] == "2026Q1"
    assert metrics["reporting_period"] == "115/01/01~115/03/31"
    assert metrics["revenue_k"] == 568015
    assert metrics["gross_profit_k"] == 153080
    assert metrics["operating_income_k"] == 126103
    assert metrics["pretax_income_k"] == 18234
    assert metrics["parent_net_income_k"] == -13770
    assert metrics["eps"] == -0.02
    assert metrics["gross_margin_pct"] == 26.95
    assert metrics["operating_margin_pct"] == 22.2
    assert metrics["non_operating_k"] == -107869
    assert metrics["non_operating_pct"] == -591.58
    assert metrics["has_line_item_metrics"] is True


def test_parse_financial_report_metrics_from_speech_style_material_info() -> None:
    metrics = parse_financial_report_metrics(
        SPEECH_STYLE_Q1_DETAIL,
        "本公司115年第1季合併財務報告及115年第2季業績展望",
    )

    assert metrics["quarter"] == "2026Q1"
    assert metrics["revenue_k"] == 3_225_000
    assert metrics["operating_income_k"] == 779_000
    assert metrics["eps"] == 2.46
    assert metrics["gross_margin_pct"] == 47.7
    assert metrics["operating_margin_pct"] == 24.16
    assert metrics["non_operating_pct"] is None


def test_build_financial_report_record_normalizes_mops_row() -> None:
    source_record = {
        "company_id": "2601",
        "company_name": "益航",
        "spoke_date": "2026-05-14",
        "spoke_time": "17:30:33",
        "subject": "公告本公司董事會通過115年第一季合併財務報告",
        "detail_preview": {"description": OFFICIAL_Q1_DETAIL},
    }

    record = build_financial_report_record(
        source_record,
        target_quarter="2026Q1",
        detected_at="2026-06-30T16:00:00+08:00",
    )

    assert record is not None
    assert record["event_type"] == "financial_report"
    assert record["quarter"] == "2026Q1"
    assert record["eps"] == -0.02
    assert record["gross_margin_pct"] == 26.95
    assert record["operating_margin_pct"] == 22.2
    assert record["non_operating_pct"] == -591.58
    assert record["announced_at"] == "2026-05-14T17:30:33"
    assert record["detected_at"] == "2026-06-30T16:00:00+08:00"


def test_build_financial_report_record_skips_corrections_without_line_items() -> None:
    record = build_financial_report_record(
        {
            "company_id": "2457",
            "company_name": "飛宏",
            "spoke_date": "2026-05-14",
            "spoke_time": "17:35:01",
            "subject": "更正115年第1季合併財務報告iXBRL被投資公司名稱",
            "detail_preview": {"description": "更正115年第1季iXBRL被投資公司名稱、所在地區等相關資訊。"},
        },
        target_quarter="2026Q1",
    )

    assert record is None


def test_build_financial_report_record_default_detected_at_uses_taipei_offset() -> None:
    record = build_financial_report_record(
        {
            "company_id": "2601",
            "company_name": "益航",
            "spoke_date": "2026-05-14",
            "spoke_time": "17:30:33",
            "subject": "公告本公司董事會通過115年第一季合併財務報告",
            "detail_preview": {"description": OFFICIAL_Q1_DETAIL},
        },
        target_quarter="2026Q1",
    )

    assert record is not None
    assert record["detected_at"].endswith("+08:00")


def test_dedupe_financial_report_records_uses_company_time_quarter_subject() -> None:
    record = {
        "company_id": "2601",
        "spoke_date": "2026-05-14",
        "spoke_time": "17:30:33",
        "quarter": "2026Q1",
        "title": "公告本公司董事會通過115年第一季合併財務報告",
    }

    assert dedupe_financial_report_records([record, dict(record)]) == [record]
