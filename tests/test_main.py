import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from main import (  # noqa: E402
    CATEGORY_FINANCIAL_SELF_REPORT,
    DEFAULT_OUTPUT_PATH,
    DEFAULT_PREVIOUS_OUTPUT_PATH,
    MODE_RECENT_FINANCIAL,
    UPDATE_TOKEN_ENV,
    DashboardServer,
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
