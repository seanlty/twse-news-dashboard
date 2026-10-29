import copy
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import main as dashboard_module
import mops_crawler as mops


def list_html(subjects=("Announcement A", "Announcement B", "Announcement C")):
    rows = []
    for index, subject in enumerate(subjects, 1):
        rows.append(f"""<tr><td>{1000 + index}</td><td>Company</td>
        <td>115/10/29</td><td>17:00:0{index}</td><td>{subject}</td><td>
        <input value="詳細資料" onclick="document.fm.SEQ_NO.value='{index}';document.fm.COMPANY_ID.value='{1000 + index}';">
        </td></tr>""")
    return "<form name='fm_t05sr01_1'><input type='hidden' name='step' value='1'><table class='hasBorder'>" + "".join(rows) + "</table></form>"


DETAIL = "<table class='hasBorder'><tr><th>說明</th><td>Current announcement detail</td></tr></table>"


def response(body, status=200):
    result = requests.Response()
    result.status_code = status
    result._content = body.encode()
    return result


class Session:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.headers = {}
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append(kwargs)
        result = next(self.outcomes)
        if isinstance(result, Exception):
            raise result
        return result


def cached_rows(html):
    return [
        {**row, "detail": {"description": "Previously fetched detail", "fields": {}},
         "fetched_at": mops.taiwan_now_iso()}
        for row in mops.parse_latest_list(html)
    ]


def crawler(outcomes):
    return mops.MopsCrawler(session=Session(outcomes), request_interval_seconds=0)


@pytest.fixture(autouse=True)
def no_actual_wait(monkeypatch):
    monkeypatch.setattr(mops.time, "sleep", lambda seconds: None)


def test_repeated_update_reuses_details_and_preserves_original_timestamp():
    html = list_html()
    existing = cached_rows(html)
    before = copy.deepcopy(existing)
    client = crawler([response(html)])
    result = client.fetch_latest_incremental(existing, market="sii")
    assert len(client.session.calls) == 1
    assert result["reused_details"] == 3
    assert result["fetched_details"] == 0
    assert result["errors"] == []
    assert existing == before
    assert result["records"][0]["fetched_at"] == existing[-1]["fetched_at"]


@pytest.mark.parametrize("change", ["title", "sequence", "expired", "missing_detail"])
def test_changed_or_expired_details_are_downloaded_again(change):
    html = list_html(("Announcement A",))
    existing = cached_rows(html)
    if change == "title":
        existing[0]["subject"] = "Old title"
    elif change == "sequence":
        existing[0]["detail_payload"]["SEQ_NO"] = "999"
    elif change == "expired":
        existing[0]["fetched_at"] = (datetime.now(mops.TAIWAN_TZ) - timedelta(hours=2)).isoformat()
    else:
        existing[0]["detail"] = {}
    existing[0]["eps_metrics"] = {"outdated": True}
    client = crawler([response(html), response(DETAIL)])
    result = client.fetch_latest_incremental(existing, market="sii")
    assert result["fetched_details"] == 1
    assert result["records"][0]["detail"]["description"] == "Current announcement detail"
    assert "outdated" not in result["records"][0]["eps_metrics"]


def test_transient_connection_failure_retries_once_with_backoff(monkeypatch):
    waits = []
    monkeypatch.setattr(mops.time, "sleep", waits.append)
    html = list_html()
    client = crawler([requests.ConnectTimeout("secret=do-not-log"), response(html)])
    result = client.fetch_latest_incremental(cached_rows(html), market="sii")
    assert result["summary_ok"] is True
    assert len(client.session.calls) == 2
    assert waits == [2]


@pytest.mark.parametrize("status,attempts", [(403, 1), (429, 2), (503, 2)])
def test_http_retry_is_limited_to_transient_statuses(status, attempts):
    client = crawler([response("unavailable", status)] * 2)
    result = client.fetch_latest_incremental([], market="sii")
    assert result["summary_ok"] is False
    assert len(client.session.calls) == attempts


def test_one_detail_failure_keeps_other_successes_and_sanitizes_errors():
    client = crawler([
        response(list_html()), response(DETAIL),
        requests.ConnectTimeout("token=do-not-log"), requests.ConnectTimeout("token=do-not-log"),
        response(DETAIL),
    ])
    result = client.fetch_latest_incremental([], market="sii")
    assert result["summary_ok"] is True
    assert result["fetched_details"] == 2
    assert result["pending_details"] == 1
    assert len(result["records"]) == 2
    assert "do-not-log" not in json.dumps(result)


def test_failed_refresh_retains_old_detail_without_marking_it_fresh():
    html = list_html(("Announcement A",))
    existing = cached_rows(html)
    existing[0]["fetched_at"] = "2026-01-01T00:00:00+08:00"
    client = crawler([response(html), requests.Timeout(), requests.Timeout()])
    result = client.fetch_latest_incremental(existing, market="sii")
    assert result["records"][0]["detail"] == existing[0]["detail"]
    assert result["records"][0]["fetched_at"] == existing[0]["fetched_at"]
    assert result["pending_details"] == 1


def test_unrecognized_html_is_failure_not_empty_success():
    client = crawler([response("<html>Service unavailable</html>")])
    result = client.fetch_latest_incremental([], market="sii")
    assert result["summary_ok"] is False
    assert result["errors"] == ["sii: list ValueError"]


def test_valid_empty_list_is_success():
    result = crawler([response(list_html(()))]).fetch_latest_incremental([], market="sii")
    assert result["summary_ok"] is True
    assert result["records"] == []
    assert result["errors"] == []


def test_two_consecutive_detail_failures_defer_remaining_requests():
    client = crawler([response(list_html())] + [requests.Timeout()] * 4)
    result = client.fetch_latest_incremental([], market="sii")
    assert len(client.session.calls) == 5
    assert result["pending_details"] == 3
    assert result["records"] == []


def test_expired_budget_starts_no_network_request():
    client = crawler([])
    result = client.fetch_latest_incremental([], market="sii", budget_seconds=0)
    assert client.session.calls == []
    assert result["summary_ok"] is False


def test_budget_expiring_after_list_defers_details_and_preserves_cache(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(mops.time, "monotonic", lambda: clock[0])
    html = list_html(("Announcement A",))
    client = crawler([response(html)])
    original_request = client.session.request

    def slow_request(*args, **kwargs):
        clock[0] += 10
        return original_request(*args, **kwargs)

    client.session.request = slow_request
    result = client.fetch_latest_incremental([], market="sii", budget_seconds=10)
    assert len(client.session.calls) == 1
    assert result["summary_ok"] is True
    assert result["pending_details"] == 1
    assert result["errors"]


def test_next_update_downloads_only_list_after_successful_first_fetch():
    html = list_html(("Announcement A", "Announcement B"))
    client = crawler([response(html), response(DETAIL), response(DETAIL), response(html)])
    first = client.fetch_latest_incremental([], market="sii")
    second = client.fetch_latest_incremental(first["records"], market="sii")
    assert first["fetched_details"] == 2
    assert second["reused_details"] == 2
    assert second["fetched_details"] == 0
    assert len(client.session.calls) == 4


def market_result(market, *, ok=True, records=None, partial=False):
    return {
        "market": market, "summary_ok": ok, "records": records or [],
        "errors": [f"{market}: list ConnectTimeout"] if not ok else [f"{market}: detail Timeout"] if partial else [],
        "fetched_details": len(records or []), "reused_details": 0,
        "pending_details": int(partial),
    }


class MarketCrawler:
    def __init__(self, results):
        self.results = results
        self.calls = []

    def fetch_latest_incremental(self, existing_records, *, market, max_items):
        self.calls.append(market)
        return self.results[market]


def server(tmp_path, results):
    cache = tmp_path / "range.json"
    mops.save_records(cached_rows(list_html(("Existing",))), cache)
    dashboard_module.update_range_cache_meta(cache, last_success_at="2026-10-29T13:25:40+08:00")
    client = MarketCrawler(results)
    app = dashboard_module.DashboardServer(
        crawler=client, max_items=0, refresh_seconds=180,
        output_path=tmp_path / "latest.json", previous_output_path=tmp_path / "previous.json",
        mode="recent-financial", category="all", range_cache_file=cache,
        update_min_interval_seconds=0,
    )
    return app, cache, client


def test_failed_market_does_not_discard_other_market_or_advance_complete_timestamp(tmp_path):
    new_rows = cached_rows(list_html(("New announcement",)))
    app, cache, client = server(tmp_path, {
        "sii": market_result("sii", ok=False),
        "otc": market_result("otc", records=new_rows),
    })
    result = app.update_latest_cache()
    assert client.calls == ["sii", "otc"]
    assert result["ok"] is True and result["degraded"] is True
    assert len(json.loads(cache.read_text())) == 2
    meta = dashboard_module.load_range_cache_meta(cache)
    assert meta["last_success_at"] == "2026-10-29T13:25:40+08:00"
    assert meta["last_partial_success_at"] == result["updated_at"]
    assert "部分更新" in dashboard_module.format_range_cache_source(cache, [])
    client.results["sii"] = market_result("sii")
    recovered = app.update_latest_cache()
    meta = dashboard_module.load_range_cache_meta(cache)
    assert recovered["degraded"] is False
    assert meta["last_error"] is None
    assert meta["last_success_at"] == recovered["updated_at"]
    assert "部分更新" not in dashboard_module.format_range_cache_source(cache, [])


def test_all_markets_fail_preserves_cache_bytes_and_success_time(tmp_path):
    app, cache, client = server(tmp_path, {market: market_result(market, ok=False) for market in ("sii", "otc")})
    before = cache.read_bytes()
    with pytest.raises(RuntimeError, match="previous cache retained"):
        app.update_latest_cache()
    assert client.calls == ["sii", "otc"]
    assert cache.read_bytes() == before
    meta = dashboard_module.load_range_cache_meta(cache)
    assert meta["last_success_at"] == "2026-10-29T13:25:40+08:00"
    assert meta["update_status"] == "failed"


def test_detail_failure_marks_partial_even_when_both_lists_succeed(tmp_path):
    app, cache, client = server(tmp_path, {
        "sii": market_result("sii", partial=True), "otc": market_result("otc"),
    })
    result = app.update_latest_cache()
    assert result["degraded"] is True
    assert result["pending_details"] == 1
    assert dashboard_module.load_range_cache_meta(cache)["update_status"] == "partial"
