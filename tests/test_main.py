import sys
from datetime import date, datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import main as main_module  # noqa: E402
from main import (  # noqa: E402
    CATEGORY_FINANCIAL_SELF_REPORT,
    DEFAULT_OUTPUT_PATH,
    DEFAULT_PREVIOUS_OUTPUT_PATH,
    MODE_RECENT_FINANCIAL,
    TAB_FINANCIAL_REPORT,
    TAB_MATERIAL_INFO,
    TAB_MONTHLY_REVENUE,
    UPDATE_TOKEN_ENV,
    DashboardServer,
    build_handler,
    fetch_finmind_trading_dates,
    last_completed_market_close_date,
    is_update_request_authorized,
    split_records_by_market_reaction,
)
from mops_crawler import save_records  # noqa: E402


class FakeCrawler:
    def fetch_latest_with_details(self, max_items: int = 0) -> list[dict]:
        return [
            {
                "company_id": "3163",
                "company_name": "波若威",
                "spoke_date": "2026-06-26",
                "spoke_time": "16:06:00",
                "subject": "公告本公司115年5月份自結合併損益",
                "detail_payload": {"TYPEK": "otc"},
                "detail": {
                    "fields": {},
                    "description": """
期間              (月)                 (季)              (最近四季累計)
                  115年05月            115年第1季         114年第2季至115年第1季
每股盈餘           0.02      102.33%    3.03    64.76%    5.52
""",
                },
            }
        ]


class FailingCrawler:
    def fetch_latest_with_details(self, max_items: int = 0) -> list[dict]:
        raise AssertionError("monthly revenue tab should not fetch material information")


class FakeMonthlyRevenueCrawler:
    def fetch_monthly_revenue_summary_with_fallbacks(self, **kwargs) -> dict:
        records = [
            {
                "source_type": "mops_monthly_revenue_summary",
                "source_label": "上市月營收彙總",
                "event_type": "monthly_revenue",
                "company_id": "4739",
                "company_name": "康普",
                "title": "4739 康普 115/04 月營收",
                "detected_at": "2026-05-07T16:00:00+08:00",
                "event_time": "2026-05-07T16:00:00+08:00",
                "data_month": "115/04",
                "monthly_revenue": "1026888",
                "previous_month_revenue": "900000",
                "mom_percent": "14.10",
                "yoy_percent": "96.24",
                "ytd_yoy_percent": "115.46",
                "note": "本月營收及累計營收較去年同期增加。",
                "detail": {
                    "fields": {
                        "本月": "1,026,888",
                        "MOM%": "14.10",
                        "YOY%": "96.24",
                        "累計YOY%": "115.46",
                        "備註 / 營收變化原因說明": "本月營收及累計營收較去年同期增加。",
                    },
                    "description": "本月: 1,026,888",
                },
            }
        ]
        return {
            "records": records,
            "market_results": [
                {
                    "market": "sii",
                    "market_label": "上市",
                    "source": "mops_t21sc04_ifrs",
                    "ok": True,
                    "fallback": False,
                    "record_count": len(records),
                }
            ],
        }

    def fetch_dashboard_records(self, **kwargs) -> list[dict]:
        return [
            {
                "source_type": "company_monthly_revenue",
                "source_label": "個股月營收",
                "event_type": "monthly_revenue",
                "company_id": "4739",
                "company_name": "康普",
                "title": "4739 康普 115/04 月營收",
                "detected_at": "2026-05-07T16:00:00+08:00",
                "event_time": "2026-05-07T16:00:00+08:00",
                "data_month": "115/04",
                "monthly_revenue": "1026888",
                "yoy_percent": "96.24",
                "ytd_yoy_percent": "115.46",
                "detail": {
                    "fields": {
                        "本月": "1,026,888",
                        "本月增減百分比": "96.24",
                        "累計增減百分比": "115.46",
                        "備註 / 營收變化原因說明": "本月營收及累計營收較去年同期增加。",
                    },
                    "description": "本月: 1,026,888",
                },
            },
            {
                "source_type": "historical_material_info",
                "source_label": "財務報告",
                "event_type": "financial_report",
                "company_id": "4739",
                "company_name": "康普",
                "title": "本公司董事會通過115年第一季合併財務報告",
                "subject": "本公司董事會通過115年第一季合併財務報告",
                "announced_at": "2026-05-07T15:16:29",
                "event_time": "2026-05-07T15:16:29",
                "metrics": {
                    "operating_revenue": "2370728",
                    "gross_profit": "424988",
                    "operating_income": "313521",
                    "pre_tax_income": "283927",
                    "parent_net_income": "186025",
                    "eps": "1.51",
                },
                "detail": {
                    "description": (
                        "4.1月1日累計至本期止營業收入(仟元):2,370,728\n"
                        "10.1月1日累計至本期止基本每股盈餘(損失) (元):1.51"
                    )
                },
            }
        ]


class FallbackMonthlyRevenueCrawler:
    def fetch_monthly_revenue_summary_with_fallbacks(self, **kwargs) -> dict:
        records = [
            {
                "source_type": "tpex_openapi_monthly_revenue",
                "source_label": "上櫃月營收OpenAPI",
                "event_type": "monthly_revenue",
                "market": "otc",
                "company_id": "4739",
                "company_name": "康普",
                "title": "4739 康普 115/05 月營收",
                "detected_at": "2026-06-28T20:05:00+08:00",
                "event_time": "2026-06-28T20:05:00+08:00",
                "data_month": "115/05",
                "monthly_revenue": "900000",
            },
            {
                "source_type": "tpex_openapi_monthly_revenue",
                "source_label": "上櫃月營收OpenAPI",
                "event_type": "monthly_revenue",
                "market": "otc",
                "company_id": "9999",
                "company_name": "測試",
                "title": "9999 測試 115/05 月營收",
                "detected_at": "2026-06-28T20:05:00+08:00",
                "event_time": "2026-06-28T20:05:00+08:00",
                "data_month": "115/05",
                "monthly_revenue": "123000",
            },
        ]
        return {
            "records": records,
            "market_results": [
                {
                    "market": "otc",
                    "market_label": "上櫃",
                    "source": "tpex_openapi_mopsfin_t187ap05_O",
                    "ok": True,
                    "fallback": True,
                    "record_count": len(records),
                    "primary_error": "mops otc unavailable",
                }
            ],
        }


class CapturingMonthlyRevenueCrawler:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def fetch_monthly_revenue_summary_with_fallbacks(self, **kwargs) -> dict:
        self.calls.append(kwargs)
        return {"records": [], "market_results": []}


def make_dashboard(
    range_cache_file: Path | None = None,
    crawler: object | None = None,
    monthly_revenue_crawler: object | None = None,
    monthly_revenue_output_path: Path | None = None,
    monthly_revenue_company_ids: list[str] | None = None,
    monthly_revenue_roc_year: int | None = None,
    monthly_revenue_month: int | None = None,
) -> DashboardServer:
    return DashboardServer(
        crawler=crawler or FakeCrawler(),
        max_items=0,
        refresh_seconds=180,
        output_path=DEFAULT_OUTPUT_PATH,
        previous_output_path=DEFAULT_PREVIOUS_OUTPUT_PATH,
        mode=MODE_RECENT_FINANCIAL,
        category=CATEGORY_FINANCIAL_SELF_REPORT,
        range_cache_file=range_cache_file,
        update_min_interval_seconds=300,
        monthly_revenue_crawler=monthly_revenue_crawler,
        monthly_revenue_output_path=monthly_revenue_output_path or DEFAULT_OUTPUT_PATH,
        monthly_revenue_company_ids=monthly_revenue_company_ids,
        monthly_revenue_roc_year=monthly_revenue_roc_year,
        monthly_revenue_month=monthly_revenue_month,
    )


def fetch_dashboard_html(dashboard: DashboardServer, path: str) -> str:
    server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(dashboard))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}{path}"
        with urlopen(url, timeout=5) as response:
            return response.read().decode("utf-8")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def save_renderable_range_cache(cache_file: Path) -> None:
    save_records(
        [
            {
                "company_id": "3163",
                "company_name": "波若威",
                "spoke_date": "2026-06-26",
                "spoke_time": "16:06:00",
                "subject": "公告本公司115年5月份自結合併損益",
                "detail_payload": {"TYPEK": "otc"},
                "detail": {
                    "fields": {},
                    "description": "每股盈餘 0.02 0.01",
                },
                "category": CATEGORY_FINANCIAL_SELF_REPORT,
                "is_financial_self_report": True,
                "eps_metrics": {
                    "period": "5月",
                    "month_eps": "0.02",
                    "last_year_month_eps": "0.01",
                    "quarter": "1Q",
                    "quarter_eps_div3": "1.01",
                    "quarter_eps": "3.03",
                    "has_eps": True,
                },
            }
        ],
        cache_file,
    )


def test_default_page_renders_material_info_tab(tmp_path: Path) -> None:
    cache_file = tmp_path / "range-cache.json"
    save_renderable_range_cache(cache_file)
    dashboard = make_dashboard(range_cache_file=cache_file)

    html = fetch_dashboard_html(dashboard, "/")

    assert f'tab={TAB_MATERIAL_INFO}' in html
    assert 'aria-current="page">自結</a>' in html
    assert "EPS年增差" in html
    assert "3163" in html
    assert "最新公告：06-26 16:06:00" in html
    assert "data-sortable-table" in html
    assert 'class="sort-button"' in html
    assert "月營收資料抓取功能待補" not in html


def test_monthly_revenue_tab_renders_records(tmp_path: Path) -> None:
    monthly_cache = tmp_path / "monthly-cache.json"
    dashboard = make_dashboard(
        range_cache_file=tmp_path / "missing-cache.json",
        crawler=FailingCrawler(),
        monthly_revenue_crawler=FakeMonthlyRevenueCrawler(),
        monthly_revenue_output_path=monthly_cache,
        monthly_revenue_company_ids=["4739"],
    )

    html = fetch_dashboard_html(dashboard, f"/?tab={TAB_MONTHLY_REVENUE}")

    assert 'aria-current="page">月營收</a>' in html
    assert "4739" in html
    assert "康普" in html
    assert "1,026.9" in html
    assert "96.24%" in html
    assert "115.46%" in html
    assert "本月營收及累計營收較去年同期增加。" in html
    assert "營收期間：2026/04 | 已申報 1 家" in html
    assert "最新申報：05-07 16:00:00" in html
    assert "data-sortable-table" in html
    assert 'data-sort-type="number"' in html
    assert 'class="detail-toggle"' not in html
    assert "monthly-detail-panel" not in html
    assert "本公司董事會通過115年第一季合併財務報告" not in html
    assert "1.51" not in html
    assert "EPS年增差" not in html
    assert 'name="q"' in html


def test_monthly_revenue_tab_only_displays_latest_data_month_from_cache(tmp_path: Path) -> None:
    monthly_cache = tmp_path / "monthly-cache.json"
    save_records(
        [
            {
                "source_type": "mops_monthly_revenue_summary",
                "source_label": "上市月營收彙總",
                "event_type": "monthly_revenue",
                "market": "sii",
                "company_id": "1111",
                "company_name": "舊資料",
                "title": "1111 舊資料 115/05 月營收",
                "detected_at": "2026-06-12T19:04:29+08:00",
                "event_time": "2026-06-12T19:04:29+08:00",
                "data_month": "115/05",
                "monthly_revenue": "100000",
            },
            {
                "source_type": "mops_monthly_revenue_summary",
                "source_label": "上市月營收彙總",
                "event_type": "monthly_revenue",
                "market": "sii",
                "company_id": "2222",
                "company_name": "新資料",
                "title": "2222 新資料 115/06 月營收",
                "detected_at": "2026-07-03T09:05:06+08:00",
                "event_time": "2026-07-03T09:05:06+08:00",
                "data_month": "115/06",
                "monthly_revenue": "200000",
            },
        ],
        monthly_cache,
    )
    dashboard = make_dashboard(
        range_cache_file=tmp_path / "missing-cache.json",
        crawler=FailingCrawler(),
        monthly_revenue_output_path=monthly_cache,
    )

    html = fetch_dashboard_html(dashboard, f"/?tab={TAB_MONTHLY_REVENUE}")

    assert "新資料" in html
    assert "舊資料" not in html
    assert "營收期間：2026/06 | 已申報 1 家" in html
    assert "最新申報：07-03 09:05:06" in html


def test_monthly_revenue_market_reaction_uses_latest_completed_trading_day() -> None:
    records = [
        {
            "company_id": "7839",
            "company_name": "達人網",
            "event_type": "monthly_revenue",
            "detected_at": "2026-06-12T19:04:29+08:00",
            "event_time": "2026-06-12T19:04:29+08:00",
            "data_month": "115/05",
        }
    ]

    cutoff_date, market_unreacted, historical = split_records_by_market_reaction(
        records,
        now=datetime(2026, 6, 28, 10, 0),
        trading_dates=[date(2026, 6, 26)],
    )

    assert cutoff_date == date(2026, 6, 26)
    assert market_unreacted == []
    assert historical == records


def test_last_completed_market_close_date_uses_trading_calendar() -> None:
    trading_dates = [date(2026, 6, 26), date(2026, 6, 29)]

    assert (
        last_completed_market_close_date(
            datetime(2026, 6, 29, 12, 0),
            trading_dates=trading_dates,
        )
        == date(2026, 6, 26)
    )
    assert (
        last_completed_market_close_date(
            datetime(2026, 6, 29, 14, 0),
            trading_dates=trading_dates,
        )
        == date(2026, 6, 29)
    )


def test_fetch_finmind_trading_dates_uses_taiwan_stock_trading_date(monkeypatch) -> None:
    captured = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "data": [
                    {"date": "2026-06-25", "is_trading_day": False},
                    {"date": "2026-06-26"},
                    {"date": "invalid"},
                ]
            }

    def fake_get(url: str, params: dict, timeout: int) -> FakeResponse:
        captured["url"] = url
        captured["params"] = params
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(main_module.requests, "get", fake_get)

    result = fetch_finmind_trading_dates(
        date(2026, 6, 20),
        date(2026, 6, 28),
        token="test-token",
    )

    assert result == [date(2026, 6, 26)]
    assert captured["url"] == main_module.FINMIND_DATA_API_URL
    assert captured["params"]["dataset"] == "TaiwanStockTradingDate"
    assert captured["params"]["start_date"] == "2026-06-20"
    assert captured["params"]["end_date"] == "2026-06-28"
    assert captured["params"]["token"] == "test-token"
    assert captured["timeout"] == 10


def test_dashboard_cache_defaults_use_persistent_data_root(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv(main_module.DATA_ROOT_ENV, raising=False)

    assert main_module.DEFAULT_OUTPUT_PATH.as_posix() == "/data/raw/latest_material_info.json"
    assert main_module.DEFAULT_RANGE_OUTPUT_PATH.as_posix() == "/data/raw/material_info_range.json"
    assert (
        main_module.DEFAULT_MONTHLY_REVENUE_OUTPUT_PATH.as_posix()
        == "/data/raw/monthly_revenue_latest.json"
    )

    custom_root = tmp_path / "volume"
    monkeypatch.setenv(main_module.DATA_ROOT_ENV, str(custom_root))

    assert main_module.dashboard_raw_data_dir() == custom_root / "raw"
    assert main_module.default_range_output_path() == custom_root / "raw" / "material_info_range.json"
    assert (
        main_module.default_monthly_revenue_output_path()
        == custom_root / "raw" / "monthly_revenue_latest.json"
    )


def test_seed_persistent_cache_files_copies_only_missing_launch_caches(tmp_path: Path) -> None:
    source_raw = tmp_path / "repo" / "data" / "raw"
    target_raw = tmp_path / "volume" / "raw"
    source_raw.mkdir(parents=True)
    save_records([{"company_id": "1111"}], source_raw / "material_info_2026-06-01_2026-06-27.json")
    save_records([{"company_id": "2222"}], source_raw / "monthly_revenue_latest.json")
    save_records(
        [{"company_id": "3333"}],
        source_raw / "material_info_2026-06-01_2026-06-27_financial_self_report.json",
    )
    target_raw.mkdir(parents=True)
    existing_monthly = target_raw / "monthly_revenue_latest.json"
    save_records([{"company_id": "existing"}], existing_monthly)

    seeded_paths = main_module.seed_persistent_cache_files(target_raw, source_raw)

    assert target_raw / "material_info_2026-06-01_2026-06-27.json" in seeded_paths
    assert target_raw / "material_info_2026-06-01_2026-06-27_financial_self_report.json" in seeded_paths
    assert existing_monthly not in seeded_paths
    assert (target_raw / "material_info_2026-06-01_2026-06-27.json").exists()
    assert (target_raw / "material_info_2026-06-01_2026-06-27_financial_self_report.json").exists()
    assert main_module.json.loads(existing_monthly.read_text(encoding="utf-8")) == [
        {"company_id": "existing"}
    ]


def test_find_range_cache_file_accepts_self_report_seed(tmp_path: Path) -> None:
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    seed_file = raw_dir / "material_info_2026-06-01_2026-06-27_financial_self_report.json"
    save_records([{"company_id": "3163"}], seed_file)

    assert main_module.find_range_cache_file(raw_dir) == seed_file


def test_update_monthly_revenue_summary_uses_dynamic_previous_month(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monthly_cache = tmp_path / "monthly-cache.json"
    capturing_crawler = CapturingMonthlyRevenueCrawler()
    dashboard = make_dashboard(
        monthly_revenue_crawler=capturing_crawler,
        monthly_revenue_output_path=monthly_cache,
    )
    monkeypatch.setattr(main_module, "previous_month_parts", lambda: (115, 6))

    result = dashboard.update_monthly_revenue_summary_cache()

    assert capturing_crawler.calls[0]["roc_year"] == 115
    assert capturing_crawler.calls[0]["month"] == 6
    assert result["data_month"] == "115/06"
    assert result["display_data_month"] == "2026/06"


def test_update_monthly_revenue_summary_skips_fallback_when_mops_primary_exists(tmp_path: Path) -> None:
    monthly_cache = tmp_path / "monthly-cache.json"
    save_records(
        [
            {
                "source_type": "mops_monthly_revenue_summary",
                "source_label": "上櫃月營收彙總",
                "event_type": "monthly_revenue",
                "market": "otc",
                "company_id": "4739",
                "company_name": "康普",
                "title": "4739 康普 115/05 月營收",
                "detected_at": "2026-06-28T20:00:00+08:00",
                "event_time": "2026-06-28T20:00:00+08:00",
                "data_month": "115/5",
                "monthly_revenue": "1000000",
            }
        ],
        monthly_cache,
    )
    dashboard = make_dashboard(
        monthly_revenue_crawler=FallbackMonthlyRevenueCrawler(),
        monthly_revenue_output_path=monthly_cache,
        monthly_revenue_roc_year=115,
        monthly_revenue_month=5,
    )

    result = dashboard.update_monthly_revenue_summary_cache()
    records = dashboard._load_offline_records(monthly_cache)

    assert result["ok"] is True
    assert result["fallback_skipped_existing_primary_count"] == 1
    assert result["new_count"] == 1
    assert {record["company_id"] for record in records} == {"4739", "9999"}
    assert [
        record
        for record in records
        if record["company_id"] == "4739" and record["source_type"] == "tpex_openapi_monthly_revenue"
    ] == []


def test_financial_report_tab_renders_financial_records(tmp_path: Path) -> None:
    monthly_cache = tmp_path / "monthly-cache.json"
    dashboard = make_dashboard(
        range_cache_file=tmp_path / "missing-cache.json",
        crawler=FailingCrawler(),
        monthly_revenue_crawler=FakeMonthlyRevenueCrawler(),
        monthly_revenue_output_path=monthly_cache,
        monthly_revenue_company_ids=["4739"],
    )

    html = fetch_dashboard_html(dashboard, f"/?tab={TAB_FINANCIAL_REPORT}")

    assert 'aria-current="page">財報</a>' in html
    assert "4739" in html
    assert "康普" in html
    assert "本公司董事會通過115年第一季合併財務報告" in html
    assert "2,370.7" in html
    assert "1.51" in html
    assert "data-sortable-table" in html
    assert 'data-sort-type="time"' in html
    assert "1,026.9" not in html
    assert 'name="q"' in html


def test_update_latest_cache_merges_and_persists_eps_metrics(tmp_path: Path) -> None:
    cache_file = tmp_path / "range-cache.json"
    save_records(
        [
            {
                "company_id": "1435",
                "company_name": "中福",
                "spoke_date": "2026-06-26",
                "spoke_time": "15:02:00",
                "subject": "公告本公司115年5月份自結合併損益",
                "detail_payload": {"TYPEK": "sii"},
                "detail": {
                    "fields": {},
                    "description": """
期間              (月)                 (季)
                  115年05月            115年第1季
每股盈餘           -0.15               -0.08
""",
                },
            }
        ],
        cache_file,
    )
    dashboard = DashboardServer(
        crawler=FakeCrawler(),
        max_items=0,
        refresh_seconds=180,
        output_path=DEFAULT_OUTPUT_PATH,
        previous_output_path=DEFAULT_PREVIOUS_OUTPUT_PATH,
        mode=MODE_RECENT_FINANCIAL,
        category=CATEGORY_FINANCIAL_SELF_REPORT,
        range_cache_file=cache_file,
        update_min_interval_seconds=300,
    )

    result = dashboard.update_latest_cache()
    records, _ = dashboard._get_recent_financial_records()

    assert result["ok"] is True
    assert result["new_count"] == 1
    assert {record["company_id"] for record in records} == {"1435", "3163"}
    assert all(record["eps_metrics"]["has_eps"] for record in records)


def test_update_request_requires_token_by_default(monkeypatch) -> None:
    monkeypatch.delenv(UPDATE_TOKEN_ENV, raising=False)

    assert is_update_request_authorized({}, {}, "127.0.0.1") is False
    assert (
        is_update_request_authorized(
            {},
            {},
            "127.0.0.1",
            allow_unprotected_local_update=True,
        )
        is True
    )


def test_update_request_accepts_bearer_token(monkeypatch) -> None:
    monkeypatch.setenv(UPDATE_TOKEN_ENV, "secret-token")

    assert (
        is_update_request_authorized(
            {"Authorization": "Bearer secret-token"},
            {},
            "203.0.113.1",
        )
        is True
    )
    assert (
        is_update_request_authorized(
            {"Authorization": "Bearer wrong-token"},
            {},
            "203.0.113.1",
        )
        is False
    )
