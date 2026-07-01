import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from src.monthly_revenue_crawler import (
    MonthlyRevenueCrawler,
    append_new_monthly_revenue_records,
    current_detected_at,
    filter_monthly_revenue_records_by_data_month,
    filter_monthly_records_by_company_id,
    looks_like_monthly_or_financial_signal,
    parse_openapi_monthly_revenue_rows,
    parse_mops_monthly_revenue_csv,
    parse_company_monthly_revenue,
    parse_financial_metrics,
    parse_historical_material_list,
    parse_material_detail_with_fallback,
    sort_event_records,
)


HISTORICAL_MATERIAL_HTML = """
<form action="/mops/web/ajax_t05st01" id="t05st01_fm" method="post" name="t05st01_fm">
  <input name="firstin" type="hidden" value="true"/>
  <input name="b_date" type="hidden" value="05"/>
  <input name="e_date" type="hidden" value="15"/>
  <input name="TYPEK" type="hidden"/>
  <input name="year" type="hidden" value="115"/>
  <input name="month" type="hidden" value="05"/>
  <input name="co_id" type="hidden"/>
  <input name="spoke_date" type="hidden"/>
  <input name="spoke_time" type="hidden"/>
  <input name="seq_no" type="hidden"/>
  <input name="step" type="hidden" value="2"/>
  <input name="off" type="hidden" value="1"/>
  <table class="hasBorder">
    <tr><th>公司代號</th><th>公司名稱</th><th>發言日期</th><th>發言時間</th><th>主旨</th><th></th></tr>
    <tr>
      <td>4739</td>
      <td>康普</td>
      <td>115/05/07</td>
      <td>15:16:29</td>
      <td>本公司董事會通過115年第一季合併財務報告</td>
      <td><input type="button" value="詳細資料"
        onclick="document.t05st01_fm.action='ajax_t05st01';document.t05st01_fm.seq_no.value='1';document.t05st01_fm.spoke_time.value='151629';document.t05st01_fm.spoke_date.value='20260507';document.t05st01_fm.co_id.value='4739';document.t05st01_fm.TYPEK.value='sii';openWindow(this.form ,'');"></td>
    </tr>
  </table>
</form>
"""


COMPANY_MONTHLY_REVENUE_HTML = """
<html>
  <body>
    <table><tr><td>本資料由　(上市公司)康普　公司提供</td></tr></table>
    <center>民國115年04月</center>
    <table class="hasBorder">
      <tr><th>項目</th><th>營業收入淨額</th></tr>
      <tr><td>本月</td><td>1,026,888</td></tr>
      <tr><td>去年同期</td><td>523,295</td></tr>
      <tr><td>增減金額</td><td>503,593</td></tr>
      <tr><td>增減百分比</td><td>96.24</td></tr>
      <tr><td>本年累計</td><td>3,397,616</td></tr>
      <tr><td>去年累計</td><td>1,576,943</td></tr>
      <tr><td>備註 / 營收變化原因說明</td><td>本月營收及累計營收較去年同期增加。</td></tr>
    </table>
  </body>
</html>
"""


MATERIAL_DETAIL_HTML = """
<table><tr><td>本資料由　(上市公司) 4739 康普　公司提供</td></tr></table>
<table>
  <tr><td>序號</td><td>1</td><td>發言日期</td><td>115/05/07</td><td>發言時間</td><td>15:16:29</td></tr>
  <tr><td>發言人</td><td>陳均恒</td><td>發言人職稱</td><td>副總經理</td><td>發言人電話</td><td>(03)598-3101</td></tr>
  <tr><td>主旨</td><td colspan="5">本公司董事會通過115年第一季合併財務報告</td></tr>
  <tr><td>符合條款</td><td>第 31 款</td><td>事實發生日</td><td>115/05/07</td></tr>
  <tr><td>說明</td><td colspan="5"><pre>4.1月1日累計至本期止營業收入(仟元):2,370,728
10.1月1日累計至本期止基本每股盈餘(損失) (元):1.51</pre></td></tr>
</table>
"""


def test_parse_historical_material_list_extracts_announcement_time_and_payload() -> None:
    records = parse_historical_material_list(HISTORICAL_MATERIAL_HTML)

    assert len(records) == 1
    assert records[0]["company_id"] == "4739"
    assert records[0]["company_name"] == "康普"
    assert records[0]["source_label"] == "財務報告"
    assert records[0]["announced_at"] == "2026-05-07T15:16:29"
    assert records[0]["detail_payload"]["seq_no"] == "1"
    assert records[0]["detail_payload"]["TYPEK"] == "sii"


def test_parse_company_monthly_revenue_extracts_revenue_fields() -> None:
    record = parse_company_monthly_revenue(
        COMPANY_MONTHLY_REVENUE_HTML,
        company_id="4739",
        market="sii",
        detected_at="2026-05-07T16:00:00+08:00",
    )

    assert record is not None
    assert record["company_id"] == "4739"
    assert record["company_name"] == "康普"
    assert record["source_label"] == "個股月營收"
    assert record["data_month"] == "115/04"
    assert record["monthly_revenue"] == "1026888"
    assert record["last_year_month_revenue"] == "523295"
    assert record["yoy_percent"] == "96.24"
    assert record["ytd_yoy_percent"] == ""
    assert record["detected_at"] == "2026-05-07T16:00:00+08:00"


def test_parse_material_detail_with_fallback_supports_td_headers() -> None:
    detail = parse_material_detail_with_fallback(MATERIAL_DETAIL_HTML)

    assert detail["company_info"] == "本資料由 (上市公司) 4739 康普 公司提供"
    assert detail["fields"]["發言人"] == "陳均恒"
    assert detail["fields"]["主旨"] == "本公司董事會通過115年第一季合併財務報告"
    assert "基本每股盈餘" in detail["description"]


def test_financial_signal_detection_and_metrics() -> None:
    record = {
        "subject": "本公司董事會通過115年第一季合併財務報告",
        "detail": {
            "description": """
4.1月1日累計至本期止營業收入(仟元):2,370,728
7.1月1日累計至本期止稅前淨利(淨損) (仟元):283,927
10.1月1日累計至本期止基本每股盈餘(損失) (元):1.51
"""
        },
    }

    assert looks_like_monthly_or_financial_signal(record) is True
    metrics = parse_financial_metrics(record["detail"]["description"])
    assert metrics["operating_revenue"] == "2370728"
    assert metrics["pre_tax_income"] == "283927"
    assert metrics["eps"] == "1.51"


def test_sort_and_filter_monthly_records_by_event_time_and_company_id() -> None:
    records = [
        {"company_id": "2330", "event_time": "2026-05-08T13:00:00"},
        {"company_id": "4739", "event_time": "2026-05-07T15:16:29"},
    ]

    assert [record["company_id"] for record in sort_event_records(records)] == ["2330", "4739"]
    assert filter_monthly_records_by_company_id(records, "4739") == [records[1]]


def test_parse_mops_monthly_revenue_csv_normalizes_summary_rows() -> None:
    csv_text = (
        "出表日期,資料年月,公司代號,公司名稱,產業別,營業收入-當月營收,"
        "營業收入-上月營收,營業收入-去年當月營收,營業收入-上月比較增減(%),"
        "營業收入-去年同月增減(%),累計營業收入-當月累計營收,"
        "累計營業收入-去年累計營收,累計營業收入-前期比較增減(%),備註\n"
        "115/06/28,115/5,4739,康普,化學工業,1026888,900000,523295,"
        "14.098666,96.24,3397616,1576943,115.46,本月營收增加\n"
    )

    records = parse_mops_monthly_revenue_csv(
        csv_text.encode("utf-8"),
        market="sii",
        detected_at="2026-06-28T20:00:00+08:00",
        source_url="https://mopsov.twse.com.tw/mops/web/t21sc04_ifrs",
    )

    assert len(records) == 1
    assert records[0]["source_type"] == "mops_monthly_revenue_summary"
    assert records[0]["source_label"] == "上市月營收彙總"
    assert records[0]["event_type"] == "monthly_revenue"
    assert records[0]["data_month"] == "115/05"
    assert records[0]["monthly_revenue"] == "1026888"
    assert records[0]["mom_percent"] == "14.098666"
    assert records[0]["yoy_percent"] == "96.24"
    assert records[0]["ytd_yoy_percent"] == "115.46"
    assert records[0]["detected_at"] == "2026-06-28T20:00:00+08:00"


def test_parse_tpex_openapi_monthly_revenue_rows_normalizes_otc_rows() -> None:
    rows = [
        {
            "出表日期": "1150617",
            "資料年月": "11505",
            "公司代號": "4739",
            "公司名稱": "康普",
            "產業別": "化學工業",
            "營業收入-當月營收": "1026888",
            "營業收入-上月營收": "900000",
            "營業收入-去年當月營收": "523295",
            "營業收入-上月比較增減(%)": "14.098666",
            "營業收入-去年同月增減(%)": "96.24",
            "累計營業收入-當月累計營收": "3397616",
            "累計營業收入-去年累計營收": "1576943",
            "累計營業收入-前期比較增減(%)": "115.46",
            "備註": "本月營收增加",
        }
    ]

    records = parse_openapi_monthly_revenue_rows(
        rows,
        source_type="tpex_openapi_monthly_revenue",
        source_label="上櫃月營收OpenAPI",
        market="otc",
        detected_at="2026-06-28T20:00:00+08:00",
        source_url="https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O",
    )

    assert len(records) == 1
    assert records[0]["source_type"] == "tpex_openapi_monthly_revenue"
    assert records[0]["source_label"] == "上櫃月營收OpenAPI"
    assert records[0]["market"] == "otc"
    assert records[0]["market_label"] == "上櫃"
    assert records[0]["report_date"] == "115/06/17"
    assert records[0]["data_month"] == "115/05"
    assert records[0]["monthly_revenue"] == "1026888"


def test_fetch_monthly_revenue_summary_with_fallbacks_uses_tpex_for_otc_failure() -> None:
    crawler = MonthlyRevenueCrawler(request_interval_seconds=0)

    def fake_mops_monthly_revenue_summary_market(
        *,
        market: str,
        roc_year: int,
        month: int,
    ) -> list[dict]:
        raise RuntimeError(f"MOPS {market} unavailable")

    def fake_tpex_otc_monthly_revenue_openapi() -> list[dict]:
        return [
            {
                "source_type": "tpex_openapi_monthly_revenue",
                "market": "otc",
                "company_id": "1111",
                "data_month": "115/04",
                "event_time": "2026-06-28T20:00:00+08:00",
            },
            {
                "source_type": "tpex_openapi_monthly_revenue",
                "market": "otc",
                "company_id": "4739",
                "data_month": "115/05",
                "event_time": "2026-06-28T20:05:00+08:00",
            },
        ]

    crawler.fetch_mops_monthly_revenue_summary_market = fake_mops_monthly_revenue_summary_market
    crawler.fetch_tpex_otc_monthly_revenue_openapi = fake_tpex_otc_monthly_revenue_openapi

    result = crawler.fetch_monthly_revenue_summary_with_fallbacks(
        roc_year=115,
        month=5,
        markets=["otc"],
    )

    assert [record["company_id"] for record in result["records"]] == ["4739"]
    assert result["market_results"][0]["market"] == "otc"
    assert result["market_results"][0]["source"] == "tpex_openapi_mopsfin_t187ap05_O"
    assert result["market_results"][0]["fallback"] is True
    assert result["market_results"][0]["ok"] is True
    assert result["market_results"][0]["record_count"] == 1
    assert "primary_error" in result["market_results"][0]


def test_filter_monthly_revenue_records_by_data_month() -> None:
    records = [
        {"company_id": "1111", "data_month": "115/04"},
        {"company_id": "4739", "data_month": "115/05"},
    ]

    filtered = filter_monthly_revenue_records_by_data_month(records, roc_year=115, month=5)

    assert filtered == [records[1]]


def test_append_new_monthly_revenue_records_keeps_first_seen_data_time() -> None:
    existing = [
        {
            "source_type": "mops_monthly_revenue_summary",
            "market": "sii",
            "company_id": "4739",
            "data_month": "115/05",
            "monthly_revenue": "1026888",
            "detected_at": "2026-06-28T20:00:00+08:00",
            "event_time": "2026-06-28T20:00:00+08:00",
        }
    ]
    unchanged = {**existing[0], "detected_at": "2026-06-28T20:05:00+08:00"}
    changed = {
        **existing[0],
        "monthly_revenue": "1027000",
        "detected_at": "2026-06-28T20:10:00+08:00",
        "event_time": "2026-06-28T20:10:00+08:00",
    }

    merged, new_records = append_new_monthly_revenue_records(existing, [unchanged, changed])

    assert new_records == [changed]
    assert [record["monthly_revenue"] for record in merged] == ["1027000", "1026888"]


def test_current_detected_at_uses_taipei_offset() -> None:
    assert current_detected_at().endswith("+08:00")


def test_sort_event_records_orders_mixed_timezones_by_actual_time() -> None:
    records = [
        {
            "company_id": "old",
            "event_time": "2026-07-01T18:30:00+08:00",
        },
        {
            "company_id": "new",
            "event_time": "2026-07-01T10:56:15+0000",
        },
    ]

    sorted_records = sort_event_records(records)

    assert [record["company_id"] for record in sorted_records] == ["new", "old"]
