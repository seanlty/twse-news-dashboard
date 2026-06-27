import sys
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from main import (  # noqa: E402
    CATEGORY_FINANCIAL_SELF_REPORT,
    DEFAULT_OUTPUT_PATH,
    DEFAULT_PREVIOUS_OUTPUT_PATH,
    MODE_RECENT_FINANCIAL,
    TAB_MATERIAL_INFO,
    TAB_MONTHLY_REVENUE,
    UPDATE_TOKEN_ENV,
    DashboardServer,
    build_handler,
    is_update_request_authorized,
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


def make_dashboard(
    range_cache_file: Path | None = None,
    crawler: object | None = None,
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
    assert 'aria-current="page">重大訊息</a>' in html
    assert "EPS年增差" in html
    assert "3163" in html
    assert "月營收資料抓取功能待補" not in html


def test_monthly_revenue_tab_renders_placeholder(tmp_path: Path) -> None:
    dashboard = make_dashboard(
        range_cache_file=tmp_path / "missing-cache.json",
        crawler=FailingCrawler(),
    )

    html = fetch_dashboard_html(dashboard, f"/?tab={TAB_MONTHLY_REVENUE}")

    assert 'aria-current="page">月營收</a>' in html
    assert "月營收資料抓取功能待補" in html
    assert "EPS年增差" not in html
    assert 'name="q"' not in html


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
