"""CLI and demo page for the TWSE material-information dashboard."""

from __future__ import annotations

import argparse
import html
import json
import os
import time
from datetime import date, datetime, time as datetime_time, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlencode, urlparse

from dotenv import load_dotenv

from mops_crawler import (
    CATEGORY_CHOICES,
    CATEGORY_ALL,
    CATEGORY_FINANCIAL_SELF_REPORT,
    DEFAULT_DAY_INTERVAL_SECONDS,
    DEFAULT_REFRESH_SECONDS,
    DEFAULT_REQUEST_INTERVAL_SECONDS,
    MopsCrawler,
    default_month_start,
    default_previous_day,
    dedupe_records,
    enrich_records_for_cache,
    filter_records_by_company_id,
    filter_records_by_category,
    filter_records_by_recent_days,
    filter_records_with_eps,
    get_or_extract_eps_metrics,
    normalize_time,
    save_records,
    sort_records_by_spoke_time,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "data" / "raw" / "latest_material_info.json"
DEFAULT_PREVIOUS_OUTPUT_PATH = PROJECT_ROOT / "data" / "raw" / "previous_material_info.json"
DEFAULT_RANGE_OUTPUT_PATH = PROJECT_ROOT / "data" / "raw" / "material_info_range.json"
DEFAULT_RECENT_DAYS = 7
MODE_LATEST = "latest"
MODE_PREVIOUS = "previous"
MODE_RECENT_FINANCIAL = "recent-financial"
MODE_CHOICES = (MODE_LATEST, MODE_PREVIOUS, MODE_RECENT_FINANCIAL)
TAB_MATERIAL_INFO = "material-info"
TAB_MONTHLY_REVENUE = "monthly-revenue"
TAB_CHOICES = (TAB_MATERIAL_INFO, TAB_MONTHLY_REVENUE)
LISTED_OTC_MARKETS = {"sii", "otc"}
MARKET_CLOSE_TIME = datetime_time(13, 30)
MARKET_CLOSE_TIME_TEXT = "13:30:00"
UPDATE_API_PATH = "/api/admin/update"
UPDATE_TOKEN_ENV = "TWSE_DASHBOARD_UPDATE_TOKEN"
DEV_ALLOW_UNPROTECTED_UPDATE_ENV = "TWSE_DASHBOARD_DEV_ALLOW_UNPROTECTED_UPDATE"
RANGE_CACHE_FILE_ENV = "TWSE_DASHBOARD_RANGE_CACHE_FILE"
RECENT_DAYS_ENV = "TWSE_DASHBOARD_RECENT_DAYS"
UPDATE_MIN_INTERVAL_ENV = "TWSE_DASHBOARD_UPDATE_MIN_INTERVAL"
DEFAULT_UPDATE_MIN_INTERVAL_SECONDS = 300
TRUE_ENV_VALUES = {"1", "true", "yes", "on"}


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().casefold() in TRUE_ENV_VALUES


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError:
        return default


def env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return None
    return Path(value)


def is_local_client(client_host: str) -> bool:
    return client_host in {"127.0.0.1", "::1", "localhost"}


def is_update_request_authorized(
    headers: Mapping[str, str],
    query: dict[str, str],
    client_host: str,
    allow_unprotected_local_update: bool = False,
) -> bool:
    expected_token = os.environ.get(UPDATE_TOKEN_ENV, "").strip()
    if not expected_token:
        return allow_unprotected_local_update and is_local_client(client_host)

    auth_header = headers.get("Authorization", "").strip()
    header_token = headers.get("X-Update-Token", "").strip()
    query_token = query.get("token", "").strip()
    bearer_token = ""
    if auth_header.casefold().startswith("bearer "):
        bearer_token = auth_header[7:].strip()

    return expected_token in {bearer_token, header_token, query_token}


def render_empty_state(message: str = "目前沒有抓到重大訊息。") -> str:
    return f"<p class=\"empty\">{html.escape(message)}</p>"


def render_metric(value: Any) -> str:
    text = "" if value is None else str(value)
    return html.escape(text if text else "-")


def metric_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value))
    except ValueError:
        return None


def render_eps_delta(metrics: dict[str, Any]) -> str:
    current = metric_float(metrics.get("month_eps"))
    previous = metric_float(metrics.get("last_year_month_eps"))
    if current is None or previous is None:
        return "<span class=\"muted-value\">-</span>"

    delta = current - previous
    class_name = "positive" if delta > 0 else "negative" if delta < 0 else "muted-value"
    return f"<span class=\"{class_name}\">{delta:+.2f}</span>"


def format_table_time(record: dict[str, Any]) -> str:
    date_text = str(record.get("spoke_date", ""))
    time_text = str(record.get("spoke_time", ""))
    date_part = date_text
    if len(date_text) >= 10:
        try:
            date_part = f"{int(date_text[5:7])}/{int(date_text[8:10])}"
        except ValueError:
            date_part = date_text
    return f"{date_part} {time_text[:5]}".strip()


def format_month_day(value: date) -> str:
    return f"{value.month}/{value.day}"


def record_typek(record: dict[str, Any]) -> str:
    payload = record.get("detail_payload") or {}
    return str(payload.get("TYPEK") or "").casefold()


def filter_records_by_listing_market(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [record for record in records if record_typek(record) in LISTED_OTC_MARKETS]


def parse_record_date(record: dict[str, Any]) -> date | None:
    try:
        return date.fromisoformat(str(record.get("spoke_date", "")))
    except ValueError:
        return None


def previous_business_day(value: date) -> date:
    current = value - timedelta(days=1)
    while current.weekday() >= 5:
        current -= timedelta(days=1)
    return current


def last_completed_market_close_date(now: datetime | None = None) -> date:
    current = now or datetime.now()
    if current.date().weekday() < 5 and current.time() >= MARKET_CLOSE_TIME:
        return current.date()
    return previous_business_day(current.date())


def determine_cutoff_date(
    records: list[dict[str, Any]],
    now: datetime | None = None,
) -> date:
    close_date = last_completed_market_close_date(now)
    record_dates = sorted({parsed for record in records if (parsed := parse_record_date(record))})
    if not record_dates:
        return close_date

    eligible_dates = [record_date for record_date in record_dates if record_date <= close_date]
    return max(eligible_dates) if eligible_dates else max(record_dates)


def is_market_unreacted_record(record: dict[str, Any], cutoff_date: date) -> bool:
    record_date = parse_record_date(record)
    record_time = normalize_time(str(record.get("spoke_time", "")))
    return record_date == cutoff_date and record_time > MARKET_CLOSE_TIME_TEXT


def split_records_by_market_reaction(
    records: list[dict[str, Any]],
) -> tuple[date, list[dict[str, Any]], list[dict[str, Any]]]:
    cutoff_date = determine_cutoff_date(records)
    market_unreacted = [
        record
        for record in records
        if is_market_unreacted_record(record, cutoff_date)
    ]
    historical = [
        record
        for record in records
        if not is_market_unreacted_record(record, cutoff_date)
    ]
    return cutoff_date, market_unreacted, historical


def find_range_cache_file(data_dir: Path = PROJECT_ROOT / "data" / "raw") -> Path | None:
    """Find the newest range cache file generated by crawl-range."""
    candidates = [
        path
        for path in data_dir.glob("material_info_*.json")
        if not path.name.endswith("_financial_self_report.json")
    ]
    if DEFAULT_RANGE_OUTPUT_PATH.exists():
        candidates.append(DEFAULT_RANGE_OUTPUT_PATH)
    if not candidates:
        return None
    return max(set(candidates), key=lambda path: path.stat().st_mtime)


def render_news_cards(records: list[dict[str, Any]]) -> str:
    """Render the default vertical card list for material information."""
    rows = []
    for record in records:
        detail = record.get("detail") or record.get("detail_preview") or {}
        fields = detail.get("fields") or {}
        description = detail.get("description") or ""
        badge = "財報自結" if record.get("is_financial_self_report") else "其他"
        rows.append(
            f"""
            <article class="news-card">
              <header>
                <div class="company">
                  <span class="code">{html.escape(record.get("company_id", ""))}</span>
                  <strong>{html.escape(record.get("company_name", ""))}</strong>
                </div>
                <time>{html.escape(record.get("spoke_date", ""))} {html.escape(record.get("spoke_time", ""))}</time>
              </header>
              <p class="badge">{html.escape(badge)}</p>
              <h2>{html.escape(record.get("subject", ""))}</h2>
              <dl>
                <div><dt>發言人</dt><dd>{html.escape(fields.get("發言人", ""))}</dd></div>
                <div><dt>條款</dt><dd>{html.escape(fields.get("符合條款", ""))}</dd></div>
                <div><dt>事實發生日</dt><dd>{html.escape(fields.get("事實發生日", ""))}</dd></div>
              </dl>
              <details>
                <summary>詳細說明</summary>
                <pre>{html.escape(description)}</pre>
              </details>
            </article>
            """
        )

    return "\n".join(rows) or render_empty_state()


def render_dashboard_tabs(active_tab: str) -> str:
    """Render top-level dashboard tabs without changing data-source mode."""
    tab_items = (
        (TAB_MATERIAL_INFO, "重大訊息"),
        (TAB_MONTHLY_REVENUE, "月營收"),
    )
    links = []
    for tab, label in tab_items:
        params = {"tab": tab}
        if tab == TAB_MATERIAL_INFO:
            params.update(
                {
                    "mode": MODE_RECENT_FINANCIAL,
                    "category": CATEGORY_FINANCIAL_SELF_REPORT,
                }
            )
        class_name = "tab-link is-active" if tab == active_tab else "tab-link"
        aria_current = ' aria-current="page"' if tab == active_tab else ""
        links.append(
            f'<a class="{class_name}" href="/?{html.escape(urlencode(params))}"'
            f'{aria_current}>{html.escape(label)}</a>'
        )
    return f'<nav class="tab-switcher" aria-label="Dashboard tabs">{"".join(links)}</nav>'


def render_material_info_searchbar(search_query: str) -> str:
    clear_params = urlencode(
        {
            "tab": TAB_MATERIAL_INFO,
            "mode": MODE_RECENT_FINANCIAL,
            "category": CATEGORY_FINANCIAL_SELF_REPORT,
        }
    )
    return f"""
    <form class="searchbar" method="get" action="/">
      <input type="hidden" name="tab" value="{TAB_MATERIAL_INFO}">
      <input type="hidden" name="mode" value="{MODE_RECENT_FINANCIAL}">
      <input type="hidden" name="category" value="{CATEGORY_FINANCIAL_SELF_REPORT}">
      <input type="search" name="q" value="{html.escape(search_query)}" placeholder="輸入股票代號">
      <button type="submit">搜尋</button>
      <a href="/?{html.escape(clear_params)}">清除</a>
    </form>
    """


def render_monthly_revenue_placeholder() -> str:
    return """
    <section class="monthly-placeholder" aria-label="月營收">
      <p>月營收資料抓取功能待補</p>
    </section>
    """


def render_tabbed_dashboard(
    records: list[dict[str, Any]],
    active_tab: str,
    recent_days: int,
) -> str:
    active_tab = active_tab if active_tab in TAB_CHOICES else TAB_MATERIAL_INFO
    if active_tab == TAB_MONTHLY_REVENUE:
        title = "月營收"
        subtitle = "最新即時月營收公布"
        content = render_monthly_revenue_placeholder()
    else:
        title = "重大訊息"
        subtitle = f"近 {recent_days} 日財報自結 EPS（{len(records)} 筆）"
        content = render_eps_table(records, recent_days)

    return f"""
    <section class="dashboard-panel">
      <div class="dashboard-panel-header">
        <div class="panel-heading">
          <h2>{html.escape(title)}</h2>
          <p>{html.escape(subtitle)}</p>
        </div>
        {render_dashboard_tabs(active_tab)}
      </div>
      {content}
    </section>
    """


def render_eps_table(records: list[dict[str, Any]], recent_days: int) -> str:
    """Render a compact EPS table for recent self-reported financial records."""
    if not records:
        return render_empty_state("目前沒有近一周財報自結資料。")

    cutoff_date, market_unreacted, historical = split_records_by_market_reaction(records)
    sections = [
        (
            f"市場未反映（{format_month_day(cutoff_date)} 13:30 後公告）（{len(market_unreacted)} 筆）",
            market_unreacted,
        ),
        (
            f"歷史公告（{format_month_day(cutoff_date)} 13:30 前）（{len(historical)} 筆）",
            historical,
        ),
    ]

    rows: list[str] = []
    detail_index = 0
    for section_title, section_records in sections:
        rows.append(
            f"""
            <tr class="eps-group-row">
              <td colspan="11">{html.escape(section_title)}</td>
            </tr>
            """
        )
        if not section_records:
            rows.append(
                """
                <tr class="eps-empty-row">
                  <td colspan="11">目前沒有符合條件的公告。</td>
                </tr>
                """
            )
            continue

        for record in section_records:
            metrics = get_or_extract_eps_metrics(record)
            detail = record.get("detail") or record.get("detail_preview") or {}
            description = detail.get("description") or ""
            missing_class = "" if metrics.get("has_eps") else " missing-eps"
            detail_id = f"detail-panel-{detail_index}"
            detail_index += 1
            rows.append(
            f"""
            <tr class="eps-data-row{missing_class}" data-detail-target="{detail_id}" tabindex="0" aria-expanded="false">
              <td class="time-cell" data-label="時間">{html.escape(format_table_time(record))}</td>
              <td class="code-cell" data-label="代號">{html.escape(record.get("company_id", ""))}</td>
              <td class="name-cell" data-label="名稱">{html.escape(record.get("company_name", ""))}</td>
              <td data-label="EPS年增差">{render_eps_delta(metrics)}</td>
              <td data-label="期間">{render_metric(metrics.get("period"))}</td>
              <td class="metric-cell primary-metric" data-label="EPS">{render_metric(metrics.get("month_eps"))}</td>
              <td data-label="去年同期EPS">{render_metric(metrics.get("last_year_month_eps"))}</td>
              <td data-label="上季">{render_metric(metrics.get("quarter"))}</td>
              <td data-label="上季EPS/3">{render_metric(metrics.get("quarter_eps_div3"))}</td>
              <td class="metric-cell" data-label="上季EPS">{render_metric(metrics.get("quarter_eps"))}</td>
              <td class="detail-cell" data-label="原文">
                <button class="detail-toggle" type="button" aria-controls="{detail_id}" aria-expanded="false">詳細原文</button>
              </td>
            </tr>
            <tr class="eps-detail-panel-row" id="{detail_id}" hidden>
              <td colspan="11">
                <section class="detail-panel" aria-label="重大訊息詳細原文">
                  <div class="detail-subject">{html.escape(record.get("subject", ""))}</div>
                  <pre>{html.escape(description)}</pre>
                </section>
              </td>
            </tr>
            """
            )

    return f"""
    <div class="eps-table-wrap">
      <table class="eps-table">
        <thead>
          <tr>
            <th>時間</th>
            <th>代號</th>
            <th>名稱</th>
            <th>EPS年增差</th>
            <th>期間</th>
            <th>EPS</th>
            <th>去年同期EPS</th>
            <th>上季</th>
            <th>上季EPS/3</th>
            <th>上季EPS</th>
            <th>原文</th>
          </tr>
        </thead>
        <tbody>
          {"".join(rows)}
        </tbody>
      </table>
    </div>
    """


def render_dashboard(
    records: list[dict[str, Any]],
    generated_at: str,
    source: str,
    mode: str,
    category: str,
    search_query: str,
    recent_days: int = DEFAULT_RECENT_DAYS,
    active_tab: str = TAB_MATERIAL_INFO,
) -> str:
    """Render a minimal HTML dashboard."""
    active_tab = active_tab if active_tab in TAB_CHOICES else TAB_MATERIAL_INFO
    body = render_tabbed_dashboard(records, active_tab, recent_days)
    searchbar = (
        render_material_info_searchbar(search_query)
        if active_tab == TAB_MATERIAL_INFO
        else ""
    )
    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>台股重大訊息 Dashboard</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #111421;
      --panel: #111421;
      --panel-2: #171b29;
      --panel-3: #0d101a;
      --ink: #e5e7eb;
      --muted: #7f8a99;
      --line: #252b39;
      --line-strong: #394456;
      --accent: #62b4ff;
    }}
    * {{ box-sizing: border-box; }}
    html {{
      min-height: 100%;
      background: var(--bg);
    }}
    body {{
      margin: 0;
      min-height: 100vh;
      font-family: "Microsoft JhengHei", "Noto Sans TC", Arial, sans-serif;
      background: var(--bg);
      color: var(--ink);
    }}
    main {{
      width: min(1160px, calc(100% - 32px));
      margin: 24px auto 48px;
    }}
    .topbar {{
      padding: 0 0 16px;
      border-bottom: 1px solid var(--line);
    }}
    h1 {{
      margin: 0 0 6px;
      font-size: 28px;
      letter-spacing: 0;
    }}
    .meta {{
      margin: 0;
      color: var(--muted);
      font-size: 14px;
    }}
    .badge {{
      display: inline-block;
      margin: 14px 0 0;
      padding: 3px 7px;
      color: #14532d;
      background: #dcfce7;
      border: 1px solid #86efac;
      border-radius: 4px;
      font-size: 12px;
      font-weight: 700;
    }}
    .searchbar {{
      display: flex;
      align-items: center;
      gap: 8px;
      margin-top: 16px;
      padding-bottom: 16px;
      border-bottom: 1px solid var(--line);
    }}
    .searchbar input[type="search"] {{
      width: min(320px, 100%);
      height: 38px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 8px 10px;
      font: inherit;
      color: var(--ink);
      background: var(--panel-3);
      outline: none;
    }}
    .searchbar input[type="search"]::placeholder {{
      color: #6f7783;
    }}
    .searchbar input[type="search"]:focus {{
      border-color: var(--accent);
      box-shadow: 0 0 0 3px rgba(98, 180, 255, 0.16);
    }}
    .searchbar button,
    .searchbar a {{
      height: 38px;
      border-radius: 6px;
      padding: 8px 12px;
      font: inherit;
      text-decoration: none;
    }}
    .searchbar button {{
      border: 1px solid var(--accent);
      color: #06111f;
      background: #7bb7ff;
      cursor: pointer;
      font-weight: 800;
    }}
    .searchbar a {{
      display: inline-flex;
      align-items: center;
      border: 1px solid #334155;
      color: var(--muted);
      background: var(--panel-2);
    }}
    .dashboard-panel {{
      min-width: 0;
    }}
    .dashboard-panel-header {{
      display: flex;
      align-items: flex-end;
      justify-content: space-between;
      gap: 16px;
      margin-top: 2px;
    }}
    .panel-heading h2 {{
      margin: 0;
      color: #f3f6fb;
      font-size: 18px;
      line-height: 1.3;
      letter-spacing: 0;
    }}
    .panel-heading p {{
      margin: 5px 0 0;
      color: var(--muted);
      font-size: 13px;
    }}
    .tab-switcher {{
      display: inline-flex;
      flex: 0 0 auto;
      gap: 2px;
      padding: 3px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel-3);
    }}
    .tab-link {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 32px;
      padding: 6px 12px;
      border: 1px solid transparent;
      border-radius: 4px;
      color: #a6b2c3;
      text-decoration: none;
      font-size: 13px;
      font-weight: 800;
      white-space: nowrap;
    }}
    .tab-link:hover {{
      color: #ffffff;
      border-color: #334155;
      background: #171b29;
    }}
    .tab-link.is-active {{
      color: #06111f;
      border-color: #7bb7ff;
      background: #7bb7ff;
    }}
    .news-list {{
      display: flex;
      flex-direction: column;
      gap: 14px;
      margin-top: 18px;
    }}
    .news-card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
      min-width: 0;
    }}
    .news-card header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      color: var(--muted);
      font-size: 13px;
    }}
    .company {{
      display: flex;
      align-items: center;
      gap: 8px;
      min-width: 0;
    }}
    .code {{
      color: #ffffff;
      background: #475467;
      border-radius: 4px;
      padding: 2px 6px;
      font-size: 12px;
    }}
    .company strong {{
      overflow-wrap: anywhere;
      color: var(--ink);
    }}
    time {{
      white-space: nowrap;
    }}
    h2 {{
      margin: 14px 0;
      font-size: 17px;
      line-height: 1.5;
      letter-spacing: 0;
    }}
    dl {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
      margin: 0 0 12px;
    }}
    dt {{
      color: var(--muted);
      font-size: 12px;
    }}
    dd {{
      margin: 3px 0 0;
      overflow-wrap: anywhere;
      font-size: 14px;
    }}
    details {{
      border-top: 1px solid var(--line);
      padding-top: 12px;
    }}
    summary {{
      cursor: pointer;
      color: var(--accent);
      font-weight: 700;
    }}
    pre {{
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      margin: 12px 0 0;
      font-family: "Microsoft JhengHei", "Noto Sans TC", monospace;
      font-size: 14px;
      line-height: 1.6;
    }}
    .eps-table-wrap {{
      margin-top: 18px;
      overflow-x: auto;
      background: #111421;
      border: 1px solid #263041;
      border-radius: 8px;
      box-shadow: 0 18px 38px rgba(15, 23, 42, 0.12);
    }}
    .eps-table {{
      width: 100%;
      min-width: 980px;
      border-collapse: collapse;
      color: #d8dde7;
      background: #111421;
      font-size: 14px;
    }}
    .eps-table th,
    .eps-table td {{
      padding: 11px 12px;
      border-bottom: 1px solid #252b39;
      text-align: right;
      vertical-align: top;
      white-space: nowrap;
    }}
    .eps-table th {{
      color: #7f8a99;
      background: #171b29;
      font-size: 13px;
      font-weight: 800;
    }}
    .eps-table th:nth-child(1),
    .eps-table th:nth-child(2),
    .eps-table th:nth-child(3),
    .eps-table td:nth-child(1),
    .eps-table td:nth-child(2),
    .eps-table td:nth-child(3) {{
      text-align: left;
    }}
    .eps-group-row td {{
      color: #cfd6df;
      background: #1a1e30;
      border-bottom: 1px solid #43503f;
      font-weight: 800;
      text-align: left;
    }}
    .eps-empty-row td {{
      color: #7f8a99;
      background: #111421;
      border-bottom: 1px solid #252b39;
      text-align: left;
    }}
    .eps-data-row:hover td {{
      background: #151a2a;
    }}
    .eps-data-row {{
      cursor: pointer;
    }}
    .eps-data-row:focus {{
      outline: 2px solid #7bb7ff;
      outline-offset: -2px;
    }}
    .eps-data-row.is-expanded td {{
      background: #151a2a;
      border-bottom-color: #394456;
    }}
    .detail-cell {{
      text-align: left;
      white-space: normal;
      min-width: 240px;
    }}
    .detail-toggle {{
      min-height: 30px;
      color: #7bb7ff;
      background: transparent;
      border: 1px solid #334155;
      border-radius: 6px;
      padding: 5px 9px;
      font: inherit;
      font-size: 13px;
      font-weight: 800;
      cursor: pointer;
    }}
    .detail-toggle:hover,
    .eps-data-row.is-expanded .detail-toggle {{
      color: #ffffff;
      border-color: #7bb7ff;
      background: #18324e;
    }}
    .eps-detail-panel-row[hidden] {{
      display: none;
    }}
    .eps-detail-panel-row td {{
      padding: 12px 14px 16px;
      background: #0f1320;
      border-bottom: 1px solid #394456;
      text-align: left;
      white-space: normal;
    }}
    .detail-panel {{
      width: 100%;
      max-height: 300px;
      overflow: auto;
      color: #cbd5e1;
      background: #0d101a;
      border: 1px solid #2b3445;
      border-radius: 6px;
      padding: 12px;
      scrollbar-color: #43506a #0d101a;
      scrollbar-width: thin;
    }}
    .detail-panel::-webkit-scrollbar {{
      width: 10px;
      height: 10px;
    }}
    .detail-panel::-webkit-scrollbar-track {{
      background: #0d101a;
      border-radius: 999px;
    }}
    .detail-panel::-webkit-scrollbar-thumb {{
      background: #43506a;
      border: 2px solid #0d101a;
      border-radius: 999px;
    }}
    .detail-panel::-webkit-scrollbar-thumb:hover {{
      background: #5a6885;
    }}
    .detail-panel pre {{
      margin-top: 10px;
      max-height: none;
      overflow: visible;
    }}
    .detail-subject {{
      margin-top: 4px;
      color: #f3f6fb;
      font-weight: 800;
      overflow-wrap: anywhere;
    }}
    .time-cell,
    .missing-eps .metric-cell,
    .muted-value {{
      color: #6f7783;
    }}
    .code-cell {{
      color: #62b4ff;
      font-weight: 900;
    }}
    .name-cell {{
      color: #eef3fb;
      font-weight: 900;
    }}
    .metric-cell {{
      color: #ff9b42;
      font-weight: 900;
    }}
    .primary-metric {{
      border-left: 1px solid #394456;
    }}
    .positive {{
      color: #38d46f;
      font-weight: 900;
    }}
    .negative {{
      color: #ff626f;
      font-weight: 900;
    }}
    .empty {{
      padding: 24px;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
    }}
    .monthly-placeholder {{
      margin-top: 18px;
      padding: 24px;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
    }}
    .monthly-placeholder p {{
      margin: 0;
      color: var(--muted);
      font-size: 14px;
    }}
    @media (max-width: 900px) {{
      .eps-table-wrap {{
        overflow-x: visible;
        background: transparent;
        border: 0;
        border-radius: 0;
        box-shadow: none;
      }}
      .eps-table {{
        display: block;
        min-width: 0;
        background: transparent;
      }}
      .eps-table thead {{
        display: none;
      }}
      .eps-table tbody {{
        display: grid;
        gap: 10px;
      }}
      .eps-table tr {{
        display: block;
      }}
      .eps-group-row td {{
        display: block;
        border: 1px solid #293348;
        border-radius: 8px;
        padding: 12px;
      }}
      .eps-data-row {{
        display: grid !important;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        column-gap: 14px;
        padding: 10px 12px;
        background: #111421;
        border: 1px solid #263041;
        border-radius: 8px;
      }}
      .eps-table td {{
        display: flex;
        align-items: baseline;
        justify-content: space-between;
        gap: 10px;
        min-width: 0;
        padding: 8px 0;
        border-bottom: 1px solid #252b39;
        text-align: right;
        white-space: normal;
      }}
      .eps-table td::before {{
        content: attr(data-label);
        flex: 0 0 auto;
        color: #7f8a99;
        font-weight: 800;
        text-align: left;
      }}
      .eps-group-row td::before,
      .eps-detail-panel-row td::before,
      .detail-cell::before {{
        content: none;
      }}
      .primary-metric {{
        border-left: 0;
      }}
      .detail-cell {{
        display: flex !important;
        grid-column: 1 / -1;
        justify-content: flex-start !important;
        min-width: 0;
        padding-top: 10px !important;
        border-bottom: 0 !important;
      }}
      .detail-toggle {{
        width: 100%;
        min-height: 36px;
      }}
      .eps-detail-panel-row {{
        margin-top: -10px;
      }}
      .eps-detail-panel-row td {{
        display: block;
        padding: 0 12px 12px;
        background: #111421;
        border: 1px solid #263041;
        border-top: 0;
        border-radius: 0 0 8px 8px;
      }}
      .detail-panel {{
        max-height: 52vh;
      }}
      .detail-panel pre {{
        width: 100%;
        margin-top: 8px;
      }}
    }}
    @media (max-width: 520px) {{
      .eps-data-row {{
        grid-template-columns: 1fr;
      }}
      .eps-table td {{
        gap: 16px;
      }}
    }}
    @media (max-width: 720px) {{
      main {{
        width: min(100% - 20px, 1160px);
        margin-top: 16px;
      }}
      .topbar {{
        display: block;
      }}
      .searchbar {{
        align-items: stretch;
        flex-direction: column;
      }}
      .searchbar input[type="search"] {{
        width: 100%;
      }}
      .dashboard-panel-header {{
        align-items: stretch;
        flex-direction: column;
      }}
      .tab-switcher {{
        width: 100%;
      }}
      .tab-link {{
        flex: 1 1 0;
      }}
      dl {{
        grid-template-columns: 1fr;
      }}
      .news-card header {{
        display: block;
      }}
      time {{
        display: block;
        margin-top: 8px;
      }}
    }}
  </style>
</head>
<body>
  <main>
    <section class="topbar">
      <div>
        <h1>台股重大訊息 Dashboard</h1>
        <p class="meta">資料來源：{html.escape(source)} ｜ 產生時間：{html.escape(generated_at)}</p>
      </div>
    </section>
    {searchbar}
    <section class="news-list">{body}</section>
  </main>
  <script>
    (() => {{
      const rows = Array.from(document.querySelectorAll(".eps-data-row[data-detail-target]"));

      const setOpen = (row, open) => {{
        const panel = document.getElementById(row.dataset.detailTarget);
        const button = row.querySelector(".detail-toggle");
        if (!panel || !button) {{
          return;
        }}

        panel.hidden = !open;
        row.classList.toggle("is-expanded", open);
        row.setAttribute("aria-expanded", String(open));
        button.setAttribute("aria-expanded", String(open));
        button.textContent = open ? "收合原文" : "詳細原文";
      }};

      const toggleRow = (row) => {{
        const willOpen = row.getAttribute("aria-expanded") !== "true";
        if (willOpen) {{
          rows.forEach((otherRow) => {{
            if (otherRow !== row) {{
              setOpen(otherRow, false);
            }}
          }});
        }}
        setOpen(row, willOpen);
      }};

      rows.forEach((row) => {{
        const button = row.querySelector(".detail-toggle");
        row.addEventListener("click", (event) => {{
          if (event.target.closest("a, input, select, textarea")) {{
            return;
          }}
          toggleRow(row);
        }});
        row.addEventListener("keydown", (event) => {{
          if (event.target.closest(".detail-toggle")) {{
            return;
          }}
          if (event.key === "Enter" || event.key === " ") {{
            event.preventDefault();
            toggleRow(row);
          }}
        }});
        button?.addEventListener("click", (event) => {{
          event.stopPropagation();
          toggleRow(row);
        }});
      }});
    }})();
  </script>
</body>
</html>"""


class DashboardServer:
    """Tiny HTTP server with either live crawling or an offline JSON file."""

    def __init__(
        self,
        crawler: MopsCrawler,
        max_items: int,
        refresh_seconds: int,
        output_path: Path,
        previous_output_path: Path,
        mode: str,
        category: str,
        range_cache_file: Path | None = None,
        recent_days: int = DEFAULT_RECENT_DAYS,
        target_date: str | None = None,
        offline_file: Path | None = None,
        update_min_interval_seconds: int = DEFAULT_UPDATE_MIN_INTERVAL_SECONDS,
        allow_unprotected_local_update: bool = False,
    ) -> None:
        self.crawler = crawler
        self.max_items = max_items
        self.refresh_seconds = refresh_seconds
        self.output_path = output_path
        self.previous_output_path = previous_output_path
        self.mode = mode
        self.category = category
        self.range_cache_file = range_cache_file
        self.recent_days = recent_days
        self.target_date = target_date
        self.offline_file = offline_file
        self.update_min_interval_seconds = update_min_interval_seconds
        self.allow_unprotected_local_update = allow_unprotected_local_update
        self.last_update_at = 0.0
        self.last_update_result: dict[str, Any] | None = None
        self.cache_records: dict[str, list[dict[str, Any]]] = {}
        self.cache_at: dict[str, float] = {}

    def get_records(
        self,
        mode: str | None = None,
        category: str | None = None,
        search_query: str = "",
    ) -> tuple[list[dict[str, Any]], str]:
        selected_mode = mode or self.mode
        selected_category = category or self.category
        if self.offline_file is not None:
            records = self._load_offline_records(self.offline_file)
            records = sort_records_by_spoke_time(records)
            records = filter_records_by_category(records, selected_category)
            records = filter_records_by_company_id(records, search_query)
            return records, f"offline file: {self.offline_file}"

        if selected_mode == MODE_RECENT_FINANCIAL:
            records, source = self._get_recent_financial_records()
            records = filter_records_by_company_id(records, search_query)
            return records, source

        now = time.monotonic()
        if (
            selected_mode not in self.cache_records
            or now - self.cache_at.get(selected_mode, 0.0) >= self.refresh_seconds
        ):
            if selected_mode == MODE_PREVIOUS:
                records = self.crawler.fetch_previous_day_with_details(
                    target_date=self.target_date,
                    max_items=self.max_items,
                )
                save_records(records, self.previous_output_path)
                source = "MOPS previous-day endpoint"
            else:
                records = self.crawler.fetch_latest_with_details(max_items=self.max_items)
                save_records(records, self.output_path)
                source = "MOPS realtime endpoint"

            self.cache_records[selected_mode] = records
            self.cache_at[selected_mode] = now
            self.cache_records[f"{selected_mode}:source"] = [{"source": source}]

        source_record = self.cache_records.get(f"{selected_mode}:source", [{"source": ""}])[0]
        records = sort_records_by_spoke_time(self.cache_records[selected_mode])
        records = filter_records_by_category(records, selected_category)
        records = filter_records_by_company_id(records, search_query)
        return records, source_record["source"]

    def _load_offline_records(self, path: Path) -> list[dict[str, Any]]:
        return json.loads(path.read_text(encoding="utf-8"))

    def _get_recent_financial_records(self) -> tuple[list[dict[str, Any]], str]:
        cache_file = self.range_cache_file or find_range_cache_file()
        if cache_file is None or not cache_file.exists():
            return [], "range cache not found"

        records = self._load_offline_records(cache_file)
        records = filter_records_by_recent_days(records, days=self.recent_days)
        records = filter_records_by_category(records, CATEGORY_FINANCIAL_SELF_REPORT)
        records = filter_records_by_listing_market(records)
        records = filter_records_with_eps(records)
        records = sort_records_by_spoke_time(records)
        return records, f"range cache: {cache_file}"

    def update_latest_cache(self) -> dict[str, Any]:
        """Fetch realtime rows and merge them into the persistent range cache."""
        now = time.monotonic()
        if (
            self.last_update_at > 0
            and now - self.last_update_at < self.update_min_interval_seconds
            and self.last_update_result is not None
        ):
            return {
                **self.last_update_result,
                "ok": True,
                "skipped": True,
                "reason": "cooldown",
                "retry_after_seconds": int(self.update_min_interval_seconds - (now - self.last_update_at)),
            }

        cache_file = self.range_cache_file or find_range_cache_file() or DEFAULT_RANGE_OUTPUT_PATH
        existing_records = self._load_offline_records(cache_file) if cache_file.exists() else []
        existing_records = enrich_records_for_cache(existing_records)
        before_count = len(dedupe_records(existing_records))

        latest_records = self.crawler.fetch_latest_with_details(max_items=self.max_items)
        latest_records = enrich_records_for_cache(latest_records)
        merged_records = sort_records_by_spoke_time(
            dedupe_records([*latest_records, *existing_records])
        )
        save_records(merged_records, cache_file)

        self.range_cache_file = cache_file
        self.last_update_at = now
        self.cache_records.pop(MODE_RECENT_FINANCIAL, None)
        self.cache_at.pop(MODE_RECENT_FINANCIAL, None)

        result = {
            "ok": True,
            "skipped": False,
            "source": "MOPS realtime endpoint",
            "cache_file": str(cache_file),
            "fetched_count": len(latest_records),
            "before_count": before_count,
            "after_count": len(merged_records),
            "new_count": max(len(merged_records) - before_count, 0),
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "next_allowed_after_seconds": self.update_min_interval_seconds,
        }
        self.last_update_result = result
        return result


def build_handler(dashboard: DashboardServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self._handle_request()

        def do_POST(self) -> None:
            self._handle_request()

        def _handle_request(self) -> None:
            parsed_url = urlparse(self.path)
            path = parsed_url.path
            query_values = parse_qs(parsed_url.query)
            query = {key: values[0] for key, values in query_values.items() if values}
            active_tab = query.get("tab", TAB_MATERIAL_INFO)
            if active_tab not in TAB_CHOICES:
                active_tab = TAB_MATERIAL_INFO
            mode = query.get("mode", dashboard.mode)
            if mode not in MODE_CHOICES:
                mode = dashboard.mode
            if mode == MODE_RECENT_FINANCIAL:
                category = CATEGORY_FINANCIAL_SELF_REPORT
            category = query.get("category", dashboard.category)
            if mode == MODE_RECENT_FINANCIAL:
                category = CATEGORY_FINANCIAL_SELF_REPORT
            if category not in CATEGORY_CHOICES:
                category = dashboard.category
            search_query = query.get("q", "").strip()
            if path == "/health":
                self._send_text("ok")
                return

            if path == UPDATE_API_PATH:
                if not self._is_update_authorized(query):
                    self._send_json(
                        {
                            "ok": False,
                            "error": "unauthorized",
                            "message": f"Set {UPDATE_TOKEN_ENV} and send it with Authorization: Bearer <token>.",
                        },
                        status=401,
                    )
                    return
                try:
                    self._send_json(dashboard.update_latest_cache())
                except Exception as exc:  # pragma: no cover - live endpoint.
                    self._send_json(
                        {
                            "ok": False,
                            "error": "update_failed",
                            "message": str(exc),
                        },
                        status=502,
                    )
                return

            if path in {"/", "/index.html"}:
                mode = MODE_RECENT_FINANCIAL
                category = CATEGORY_FINANCIAL_SELF_REPORT

            if path in {"/", "/index.html"} and active_tab == TAB_MONTHLY_REVENUE:
                records = []
                source = "月營收資料尚未接入"
            else:
                try:
                    records, source = dashboard.get_records(
                        mode=mode,
                        category=category,
                        search_query=search_query,
                    )
                except Exception as exc:  # pragma: no cover - exercised manually.
                    self.send_response(502)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(f"Failed to load dashboard data: {exc}".encode("utf-8"))
                    return

            if path == "/api/news":
                self._send_json(records)
                return

            if path in {"/", "/index.html"}:
                generated_at = time.strftime("%Y-%m-%d %H:%M:%S")
                self._send_html(
                    render_dashboard(
                        records,
                        generated_at,
                        source,
                        mode=mode,
                        category=category,
                        search_query=search_query,
                        recent_days=dashboard.recent_days,
                        active_tab=active_tab,
                    )
                )
                return

            self.send_error(404)

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _is_update_authorized(self, query: dict[str, str]) -> bool:
            client_host = self.client_address[0] if self.client_address else ""
            return is_update_request_authorized(
                headers=self.headers,
                query=query,
                client_host=client_host,
                allow_unprotected_local_update=dashboard.allow_unprotected_local_update,
            )

        def _send_html(self, content: str) -> None:
            payload = content.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _send_json(self, data: Any, status: int = 200) -> None:
            payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _send_text(self, content: str) -> None:
            payload = content.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return Handler


def crawl_command(args: argparse.Namespace) -> None:
    crawler = MopsCrawler(request_interval_seconds=args.request_interval)
    records = crawler.fetch_latest_with_details(max_items=args.max_items, market=args.market)
    records = filter_records_by_category(records, args.category)
    save_records(records, args.output)
    print(f"Saved {len(records)} records to {args.output}")


def crawl_previous_command(args: argparse.Namespace) -> None:
    crawler = MopsCrawler(request_interval_seconds=args.request_interval)
    target_date = args.date or default_previous_day().isoformat()
    records = crawler.fetch_previous_day_with_details(
        target_date=target_date,
        max_items=args.max_items,
        market=args.market,
    )
    records = filter_records_by_category(records, args.category)
    save_records(records, args.output)
    print(f"Saved {len(records)} records for {target_date} to {args.output}")


def crawl_range_command(args: argparse.Namespace) -> None:
    crawler = MopsCrawler(request_interval_seconds=args.request_interval)
    start_date = args.start_date or default_month_start().isoformat()
    end_date = args.end_date or date.today().isoformat()
    records = crawler.fetch_date_range_records(
        start_date=start_date,
        end_date=end_date,
        max_items_per_day=args.max_items_per_day,
        market=args.market,
        include_details=args.include_details,
        day_interval_seconds=args.day_interval,
    )
    records = filter_records_by_category(records, args.category)
    records = filter_records_by_company_id(records, args.query)
    save_records(records, args.output)
    detail_mode = "with detail pages" if args.include_details else "list pages only"
    print(f"Saved {len(records)} records for {start_date} to {end_date} ({detail_mode}) to {args.output}")


def enrich_cache_command(args: argparse.Namespace) -> None:
    records = json.loads(args.path.read_text(encoding="utf-8"))
    records = enrich_records_for_cache(records, recompute_eps=args.recompute_eps)
    output_path = args.output or args.path
    save_records(records, output_path)

    financial_count = sum(1 for record in records if record.get("is_financial_self_report"))
    eps_count = sum(
        1
        for record in records
        if isinstance(record.get("eps_metrics"), dict)
        and record["eps_metrics"].get("has_eps")
    )
    print(
        f"Saved {len(records)} enriched records to {output_path} "
        f"(financial_self_report={financial_count}, has_eps={eps_count})"
    )


def serve_command(args: argparse.Namespace) -> None:
    crawler = MopsCrawler(request_interval_seconds=args.request_interval)
    dashboard = DashboardServer(
        crawler=crawler,
        max_items=args.max_items,
        refresh_seconds=args.refresh_seconds,
        output_path=args.output,
        previous_output_path=args.previous_output,
        mode=args.mode,
        category=args.category,
        range_cache_file=args.range_cache_file,
        recent_days=args.recent_days,
        target_date=args.date,
        offline_file=args.offline_file,
        update_min_interval_seconds=args.update_min_interval,
        allow_unprotected_local_update=args.allow_unprotected_local_update,
    )
    server = ThreadingHTTPServer((args.host, args.port), build_handler(dashboard))
    print(f"Serving dashboard at http://{args.host}:{args.port}")
    server.serve_forever()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TWSE/MOPS material-information dashboard")
    parser.add_argument("--env-file", default=PROJECT_ROOT / ".env", type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)

    crawl = subparsers.add_parser("crawl", help="Fetch latest MOPS rows with detail content")
    crawl.add_argument("--max-items", type=int, default=0)
    crawl.add_argument("--market", default="all", choices=["all", "sii", "otc", "rotc", "pub"])
    crawl.add_argument("--category", default=CATEGORY_ALL, choices=CATEGORY_CHOICES)
    crawl.add_argument("--request-interval", type=float, default=DEFAULT_REQUEST_INTERVAL_SECONDS)
    crawl.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    crawl.set_defaults(func=crawl_command)

    crawl_previous = subparsers.add_parser(
        "crawl-previous",
        help="Fetch previous-day MOPS rows with detail content",
    )
    crawl_previous.add_argument("--date", help="Target MOPS date, e.g. 2026-06-26")
    crawl_previous.add_argument("--max-items", type=int, default=10)
    crawl_previous.add_argument("--market", default="all", choices=["all", "sii", "otc", "rotc", "pub"])
    crawl_previous.add_argument("--category", default=CATEGORY_ALL, choices=CATEGORY_CHOICES)
    crawl_previous.add_argument("--request-interval", type=float, default=DEFAULT_REQUEST_INTERVAL_SECONDS)
    crawl_previous.add_argument("--output", type=Path, default=DEFAULT_PREVIOUS_OUTPUT_PATH)
    crawl_previous.set_defaults(func=crawl_previous_command)

    crawl_range = subparsers.add_parser(
        "crawl-range",
        help="Fetch date-based MOPS rows for an inclusive date range",
    )
    crawl_range.add_argument("--start-date", help="Start date, e.g. 2026-06-01")
    crawl_range.add_argument("--end-date", help="End date, e.g. 2026-06-27")
    crawl_range.add_argument("--max-items-per-day", type=int, default=0)
    crawl_range.add_argument("--market", default="all", choices=["all", "sii", "otc", "rotc", "pub"])
    crawl_range.add_argument("--category", default=CATEGORY_ALL, choices=CATEGORY_CHOICES)
    crawl_range.add_argument("--query", default="", help="Optional stock/company code filter")
    crawl_range.add_argument("--request-interval", type=float, default=DEFAULT_REQUEST_INTERVAL_SECONDS)
    crawl_range.add_argument("--day-interval", type=float, default=DEFAULT_DAY_INTERVAL_SECONDS)
    crawl_range.add_argument(
        "--include-details",
        action="store_true",
        help="Also POST each row's detail page; slower and much more request-heavy",
    )
    crawl_range.add_argument("--output", type=Path, default=DEFAULT_RANGE_OUTPUT_PATH)
    crawl_range.set_defaults(func=crawl_range_command)

    enrich_cache = subparsers.add_parser(
        "enrich-cache",
        help="Write derived category and EPS metrics back to an existing JSON cache",
    )
    enrich_cache.add_argument("path", type=Path)
    enrich_cache.add_argument("--output", type=Path)
    enrich_cache.add_argument(
        "--recompute-eps",
        action="store_true",
        help="Ignore existing eps_metrics and parse EPS from source text again",
    )
    enrich_cache.set_defaults(func=enrich_cache_command)

    serve = subparsers.add_parser("serve", help="Run the local demo dashboard")
    serve.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    serve.add_argument("--port", type=int, default=env_int("PORT", 8000))
    serve.add_argument("--mode", default=MODE_RECENT_FINANCIAL, choices=MODE_CHOICES)
    serve.add_argument("--category", default=CATEGORY_FINANCIAL_SELF_REPORT, choices=CATEGORY_CHOICES)
    serve.add_argument("--recent-days", type=int, default=env_int(RECENT_DAYS_ENV, DEFAULT_RECENT_DAYS))
    serve.add_argument("--range-cache-file", type=Path, default=env_path(RANGE_CACHE_FILE_ENV))
    serve.add_argument("--date", help="Target date for previous mode, e.g. 2026-06-26")
    serve.add_argument("--max-items", type=int, default=0)
    serve.add_argument("--refresh-seconds", type=int, default=DEFAULT_REFRESH_SECONDS)
    serve.add_argument("--update-min-interval", type=int, default=env_int(UPDATE_MIN_INTERVAL_ENV, DEFAULT_UPDATE_MIN_INTERVAL_SECONDS))
    serve.add_argument(
        "--allow-unprotected-local-update",
        action="store_true",
        default=env_bool(DEV_ALLOW_UNPROTECTED_UPDATE_ENV, False),
        help="Development only: allow localhost update calls when TWSE_DASHBOARD_UPDATE_TOKEN is unset",
    )
    serve.add_argument("--request-interval", type=float, default=DEFAULT_REQUEST_INTERVAL_SECONDS)
    serve.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    serve.add_argument("--previous-output", type=Path, default=DEFAULT_PREVIOUS_OUTPUT_PATH)
    serve.add_argument("--offline-file", type=Path)
    serve.set_defaults(func=serve_command)

    return parser


def main() -> None:
    bootstrap_parser = argparse.ArgumentParser(add_help=False)
    bootstrap_parser.add_argument("--env-file", default=PROJECT_ROOT / ".env", type=Path)
    bootstrap_args, _ = bootstrap_parser.parse_known_args()
    load_dotenv(bootstrap_args.env_file, override=False)

    parser = build_parser()
    args = parser.parse_args()
    load_dotenv(args.env_file, override=False)
    args.func(args)


if __name__ == "__main__":
    main()
