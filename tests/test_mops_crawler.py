from src.mops_crawler import (
    classify_record,
    dedupe_records,
    extract_eps_metrics,
    filter_records_by_company_id,
    filter_records_by_category,
    filter_records_for_recent_financial,
    filter_records_by_recent_days,
    filter_records_with_eps,
    iter_dates,
    parse_detail,
    parse_latest_list,
    parse_previous_day_list,
    roc_date_to_iso,
    sort_records_by_spoke_time,
    taiwan_now_iso,
)


LIST_HTML = """
<form name='fm_t05sr01_1' action='/mops/web/ajax_t05sr01_1' method='post'>
  <input type='hidden' name='TYPEK' value='all'>
  <input type='hidden' name='step' value='1'>
  <input type='hidden' name='skey'>
  <input type='hidden' name='hhc_co_name'>
  <input type='hidden' name='firstin' value='true'>
  <input type='hidden' name='COMPANY_ID'>
  <input type='hidden' name='COMPANY_NAME'>
  <input type='hidden' name='SPOKE_DATE'>
  <input type='hidden' name='SPOKE_TIME'>
  <input type='hidden' name='SEQ_NO'>
  <table class='hasBorder'>
    <tr class='tblHead'>
      <th>公司代號</th><th>公司簡稱</th><th>發言日期</th>
      <th>發言時間</th><th>主旨</th><th>&nbsp;</th>
    </tr>
    <tr class='even'>
      <td>3432</td>
      <td>台端</td>
      <td>115/06/27</td>
      <td>09:22:42</td>
      <td>依臺灣證券交易所股份有限公司臺證上一字第1121801204
號函辦理</td>
      <td><input type='button' value='詳細資料'
        onclick="document.fm_t05sr01_1.SEQ_NO.value='1';document.fm_t05sr01_1.SPOKE_TIME.value='92242';document.fm_t05sr01_1.SPOKE_DATE.value='20260627';document.fm_t05sr01_1.COMPANY_ID.value='3432';document.fm_t05sr01_1.skey.value='3432202606271';openWindow(this.form ,'');">
      </td>
    </tr>
    <tr class='odd'>
      <td>6813</td>
      <td>富動科</td>
      <td>115/06/27</td>
      <td>07:28:33</td>
      <td>公告本公司115年股東常會決議申請停止股票公開發行</td>
      <td><input type='button' value='詳細資料'
        onclick="document.fm_t05sr01_1.SEQ_NO.value='3';document.fm_t05sr01_1.SPOKE_TIME.value='72833';document.fm_t05sr01_1.SPOKE_DATE.value='20260627';document.fm_t05sr01_1.COMPANY_ID.value='C6813';document.fm_t05sr01_1.skey.value='C6813202606273';openWindow(this.form ,'');">
      </td>
    </tr>
  </table>
</form>
"""


DETAIL_HTML = """
<div id="div01">
  <table class='noBorder' align='center'>
    <tr class='compName'><td align='center'><b>本資料由 (上市公司) 3432 台端 公司提供</b></td></tr>
  </table>
  <table class='hasBorder' align='center'>
    <tr>
      <th class='tblHead'>序號</th><td class='odd'>1</td>
      <th class='tblHead'>發言日期</th><td class='odd'>115/06/27</td>
      <th class='tblHead'>發言時間</th><td class='odd'>09:22:42</td>
    </tr>
    <tr>
      <th class='tblHead'>發言人</th><td class='odd'>鄭佩琪</td>
      <th class='tblHead'>發言人職稱</th><td class='odd'>協理</td>
      <th class='tblHead'>發言人電話</th><td class='odd'>32349988</td>
    </tr>
    <tr>
      <th class='tblHead'>主旨</th>
      <td class='odd' colspan='5'>依臺灣證券交易所股份有限公司臺證上一字第1121801204號函辦理</td>
    </tr>
    <tr>
      <th class='tblHead'>符合條款</th>
      <th class='tblHead'>第</th>
      <td class='odd'>51</td>
      <th class='tblHead'>款</th>
      <th class='tblHead'>事實發生日</th>
      <td class='odd'>115/06/27</td>
    </tr>
    <tr>
      <th class='tblHead'>說明</th>
      <td class='odd' colspan='5'><pre>1.事實發生日:115/06/27
2.公司名稱:台端興業股份有限公司
3.發生緣由:測試詳細內容</pre></td>
    </tr>
  </table>
</div>
"""


PREVIOUS_DAY_DETAIL_HTML = """
<div id="div01">
  <table class='noBorder'>
    <tr><td class='compName'>鈺寶-創</td></tr>
    <tr><td class='reportName'>公司當日重大訊息之詳細內容</td></tr>
  </table>
  <table class='noBorder'>
    <tr><td>本資料由　(上市公司) 3150 鈺寶-創　公司提供</td></tr>
  </table>
  <table>
    <tr>
      <th>序號</th><td>6</td>
      <th>發言日期</th><td>115/06/25</td>
      <th>發言時間</th><td>17:30:40</td>
    </tr>
    <tr>
      <th>發言人</th><td>黃良駿</td>
      <th>發言人職稱</th><td>總經理</td>
      <th>發言人電話</th><td>03-5169188</td>
    </tr>
    <tr>
      <th>主旨</th><td colspan="5">公告本公司董事會通過委任第七屆薪資報酬委員會委員</td>
    </tr>
    <tr>
      <th>符合條款</th><td colspan="2">第 6款</td>
      <th>事實發生日</th><td colspan="2">115/06/25</td>
    </tr>
    <tr>
      <th>說明</th><td colspan="5"><pre>1.發生變動日期:115/06/25
2.功能性委員會名稱:薪資報酬委員會</pre></td>
    </tr>
  </table>
</div>
"""


PREVIOUS_DAY_HTML = """
<div id="table01">
  <table>
    <tr>
      <th>發言日期</th><th>發言時間</th><th>公司代號</th>
      <th>公司名稱</th><th>主旨</th><th></th>
    </tr>
    <tr class='even'>
      <td align=center>&nbsp;115/06/25</td>
      <td align=center>&nbsp;17:30:40</td>
      <td align=center><pre>&nbsp;3150</pre></td>
      <td align=center>&nbsp;鈺寶-創</td>
      <td style='text-align:left !important;'>&nbsp;公告本公司董事會通過委任第七屆薪資報酬委員會委員</td>
      <td>
        <form action='/mops/web/ajax_t05st02' method='post' name='sii_fm0' id='sii_fm0' target='_blank'>
          <input type='hidden' name='off' value=''>
          <input type='hidden' name='i' value=''>
          <input type='hidden' name='step' value='1'>
          <input type='hidden' name='firstin' value='1'>
          <input type='hidden' name='TYPEK' value=''>
          <input type='hidden' name='newstuff' value='1'>
          <input type='hidden' name='co_id' value=''>
          <input type='hidden' name='pgname' value='t05st02'>
          <input type='hidden' name='h00' value='鈺寶-創'>
          <input type='hidden' name='h01' value='3150'>
          <input type='hidden' name='h02' value='20260625'>
          <input type='hidden' name='h03' value='173040'>
          <input type='hidden' name='h04' value='公告本公司董事會通過委任第七屆薪資報酬委員會委員'>
          <input type='hidden' name='h05' value='6'>
          <input type='hidden' name='h06' value='6'>
          <input type='hidden' name='h07' value='20260625'>
          <input type='hidden' name='h08' value='1.發生變動日期:115/06/25
2.功能性委員會名稱:薪資報酬委員會'>
          <input type='button' value='詳細資料' onclick='document.sii_fm0.TYPEK.value="sii";document.sii_fm0.i.value="0";document.sii_fm0.co_id.value="3150";openWindow(this.form ,"");'>
        </form>
      </td>
    </tr>
  </table>
</div>
"""


def test_roc_date_to_iso() -> None:
    assert roc_date_to_iso("115/06/27") == "2026-06-27"
    assert roc_date_to_iso("20260627") == "2026-06-27"


def test_parse_latest_list_extracts_detail_button_payload() -> None:
    rows = parse_latest_list(LIST_HTML)

    assert len(rows) == 2
    assert rows[0]["company_id"] == "3432"
    assert rows[0]["company_name"] == "台端"
    assert rows[0]["spoke_date"] == "2026-06-27"
    assert rows[0]["subject"] == "依臺灣證券交易所股份有限公司臺證上一字第1121801204 號函辦理"
    assert rows[0]["detail_payload"]["SEQ_NO"] == "1"
    assert rows[0]["detail_payload"]["SPOKE_TIME"] == "92242"
    assert rows[0]["detail_payload"]["SPOKE_DATE"] == "20260627"
    assert rows[0]["detail_payload"]["COMPANY_ID"] == "3432"
    assert rows[0]["detail_payload"]["skey"] == "3432202606271"
    assert rows[1]["detail_payload"]["COMPANY_ID"] == "C6813"


def test_parse_detail_extracts_fields_and_description() -> None:
    detail = parse_detail(DETAIL_HTML)

    assert detail["company_info"] == "本資料由 (上市公司) 3432 台端 公司提供"
    assert detail["fields"]["序號"] == "1"
    assert detail["fields"]["發言人"] == "鄭佩琪"
    assert detail["fields"]["發言人職稱"] == "協理"
    assert detail["fields"]["符合條款"] == "51"
    assert detail["fields"]["事實發生日"] == "115/06/27"
    assert "2.公司名稱:台端興業股份有限公司" in detail["description"]


def test_parse_detail_supports_previous_day_detail_table_without_class() -> None:
    detail = parse_detail(PREVIOUS_DAY_DETAIL_HTML)

    assert detail["company_info"] == "鈺寶-創"
    assert detail["fields"]["序號"] == "6"
    assert detail["fields"]["發言人"] == "黃良駿"
    assert detail["fields"]["發言人電話"] == "03-5169188"
    assert detail["fields"]["符合條款"] == "第 6款"
    assert detail["fields"]["事實發生日"] == "115/06/25"
    assert "薪資報酬委員會" in detail["description"]


def test_parse_previous_day_list_extracts_embedded_detail_payload() -> None:
    rows = parse_previous_day_list(PREVIOUS_DAY_HTML, query_date="2026-06-26")

    assert len(rows) == 1
    assert rows[0]["company_id"] == "3150"
    assert rows[0]["company_name"] == "鈺寶-創"
    assert rows[0]["spoke_date"] == "2026-06-25"
    assert rows[0]["spoke_time"] == "17:30:40"
    assert rows[0]["subject"] == "公告本公司董事會通過委任第七屆薪資報酬委員會委員"
    assert rows[0]["query_date"] == "2026-06-26"
    assert rows[0]["detail_payload"]["TYPEK"] == "sii"
    assert rows[0]["detail_payload"]["i"] == "0"
    assert rows[0]["detail_payload"]["co_id"] == "3150"
    assert rows[0]["detail_preview"]["fields"]["序號"] == "6"
    assert rows[0]["detail_preview"]["fields"]["符合條款"] == "6"
    assert "薪資報酬委員會" in rows[0]["detail_preview"]["description"]


def test_classify_record_marks_financial_self_report() -> None:
    record = {
        "subject": "公告本公司115年5月份自結合併損益",
        "detail": {"fields": {}, "description": "本公司自結稅後淨利及每股盈餘如下"},
    }

    classified = classify_record(record)

    assert classified["category"] == "financial-self-report"
    assert classified["is_financial_self_report"] is True


def test_extract_eps_metrics_from_single_line_table() -> None:
    record = {
        "detail": {
            "description": """
期間              (月)                 (季)              (最近四季累計)
                  115年05月            115年第1季         114年第2季至115年第1季
每股盈餘           0.87      102.33%    1.73    64.76%    5.52
"""
        }
    }

    metrics = extract_eps_metrics(record)

    assert metrics["period"] == "5月"
    assert metrics["month_eps"] == "0.87"
    assert metrics["quarter"] == "Q1"
    assert metrics["quarter_eps_div3"] == "0.58"
    assert metrics["quarter_eps"] == "1.73"
    assert metrics["four_quarter_eps"] == "5.52"
    assert metrics["has_eps"] is True


def test_extract_eps_metrics_from_sectioned_table_and_parenthesized_loss() -> None:
    record = {
        "detail": {
            "description": """
(1)單月 (115/05) (114/05)
每股盈餘(元) -0.15 0.00 由盈轉虧
(2)單季 (115第1季) (114第1季)
每股盈餘(元) (2.37) 0.06 由盈轉虧
(3)最近四季累計
每股盈餘(元) 5.53
"""
        }
    }

    metrics = extract_eps_metrics(record)

    assert metrics["period"] == "5月"
    assert metrics["month_eps"] == "-0.15"
    assert metrics["last_year_month_eps"] == "0.00"
    assert metrics["quarter"] == "Q1"
    assert metrics["quarter_eps"] == "-2.37"
    assert metrics["quarter_eps_div3"] == "-0.79"
    assert metrics["last_year_quarter_eps"] == "0.06"
    assert metrics["four_quarter_eps"] == "5.53"


def test_filter_records_with_eps_keeps_only_extractable_eps_data() -> None:
    records = [
        {
            "company_id": "3163",
            "detail": {
                "description": """
期間              (月)                 (季)              (最近四季累計)
                  115年05月            115年第1季         114年第2季至115年第1季
每股盈餘           0.02      102.33%    3.03    64.76%    5.52
"""
            },
        },
        {
            "company_id": "4609",
            "detail": {
                "description": "公告本公司115年5月份自結合併營收及相關比率，未揭露每股盈餘。",
            },
        },
    ]

    filtered = filter_records_with_eps(records)

    assert [record["company_id"] for record in filtered] == ["3163"]
    assert records[0]["eps_metrics"]["has_eps"] is True
    assert records[1]["eps_metrics"]["has_eps"] is False


def test_filter_records_by_category_keeps_only_financial_self_report() -> None:
    records = [
        classify_record({"subject": "公告自結合併損益", "detail": {"description": "稅後淨利"}}),
        classify_record({"subject": "公告董事會推舉董事長", "detail": {"description": "董事長異動"}}),
    ]

    filtered = filter_records_by_category(records, "financial-self-report")

    assert len(filtered) == 1
    assert filtered[0]["is_financial_self_report"] is True


def test_filter_records_for_recent_financial_includes_attention_eps_and_self_profit_without_eps() -> None:
    records = [
        classify_record(
            {
                "company_id": "2017",
                "subject": "公告本公司115年5月自結合併損益",
                "detail": {"description": "每股盈餘 -0.05 -0.19"},
            }
        ),
        classify_record(
            {
                "company_id": "4716",
                "subject": "公司有價證券近期多次達公布注意交易資訊標準，故公告相關訊息",
                "detail": {
                    "description": """
3.財務業務資訊:
單月 (115/05) (114/05)
每股盈餘(元) -0.03 -0.37
"""
                },
            }
        ),
        classify_record(
            {
                "company_id": "1529",
                "subject": "公告本公司115年05月自結合併損益",
                "detail": {
                    "description": "合併營業損益 25,022 48,966\n合併稅前損益 50,579 91,808",
                },
            }
        ),
        classify_record(
            {
                "company_id": "8444",
                "subject": "公告本公司115年5月自結合併財務報告之負債比率、流動比率及速動比率",
                "detail": {
                    "description": "自結流動比率:9.01%\n自結速動比率:5.20%\n自結負債比率:99.98%",
                },
            }
        ),
    ]

    filtered = filter_records_for_recent_financial(records)

    assert [record["company_id"] for record in filtered] == ["2017", "4716", "1529"]
    assert records[0]["financial_signal_kind"] == "self_report_eps"
    assert records[1]["financial_signal_kind"] == "attention_financial_eps"
    assert records[1]["is_attention_financial_eps"] is True
    assert records[2]["financial_signal_kind"] == "self_profit_without_eps"
    assert records[2]["is_self_profit_without_eps"] is True
    assert records[3]["financial_signal_kind"] == ""


def test_filter_records_by_company_id_matches_stock_code() -> None:
    records = [
        {"company_id": "3432", "company_name": "台端"},
        {"company_id": "6813", "company_name": "富動科"},
    ]

    assert filter_records_by_company_id(records, "3432") == [records[0]]
    assert filter_records_by_company_id(records, "68") == [records[1]]
    assert filter_records_by_company_id(records, "") == records


def test_sort_records_by_spoke_time_newest_first() -> None:
    records = [
        {"company_id": "1", "spoke_date": "2026-06-27", "spoke_time": "07:28:33"},
        {"company_id": "2", "spoke_date": "2026-06-27", "spoke_time": "09:22:42"},
        {"company_id": "3", "spoke_date": "2026-06-26", "spoke_time": "23:59:59"},
    ]

    sorted_records = sort_records_by_spoke_time(records)

    assert [record["company_id"] for record in sorted_records] == ["2", "1", "3"]


def test_taiwan_now_iso_uses_taipei_offset() -> None:
    assert taiwan_now_iso().endswith("+08:00")


def test_dedupe_records_uses_mops_row_identity() -> None:
    records = [
        {"company_id": "2760", "spoke_date": "2026-06-26", "spoke_time": "21:36:58", "subject": "公告自結財報"},
        {"company_id": "2760", "spoke_date": "2026-06-26", "spoke_time": "213658", "subject": "公告自結財報"},
        {"company_id": "2760", "spoke_date": "2026-06-26", "spoke_time": "21:37:00", "subject": "公告自結財報"},
    ]

    unique_records = dedupe_records(records)

    assert len(unique_records) == 2


def test_iter_dates_returns_inclusive_range() -> None:
    dates = iter_dates("2026-06-01", "2026-06-03")

    assert [day.isoformat() for day in dates] == [
        "2026-06-01",
        "2026-06-02",
        "2026-06-03",
    ]


def test_filter_records_by_recent_days_uses_spoke_date_window() -> None:
    records = [
        {"company_id": "1", "spoke_date": "2026-06-21"},
        {"company_id": "2", "spoke_date": "2026-06-20"},
        {"company_id": "3", "spoke_date": "2026-06-27"},
    ]

    filtered = filter_records_by_recent_days(records, end_date="2026-06-27", days=7)

    assert [record["company_id"] for record in filtered] == ["1", "3"]
