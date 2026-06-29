# Dashboard Data Flow and Update API Tracker

This note records the current dashboard data path, update endpoints, cache files, and open follow-up items.

## Page Entry Points

- `GET /` and `GET /index.html`: render the HTML dashboard.
- `GET /api/news?tab=<tab>`: return the current tab records as JSON.
- Supported tab values:
  - `material-info`: self-reported and attention-trading financial tab, displayed as `自結`.
  - `monthly-revenue`: monthly revenue tab, displayed as `月營收`.
  - `financial-report`: financial report tab, displayed as `財報`.

## Self-Reported / Attention Financial Tab

Current display flow:

1. `DashboardServer.get_records()` routes `MODE_RECENT_FINANCIAL` to `_get_recent_financial_records()`.
2. `_get_recent_financial_records()` loads the active range cache from `TWSE_DASHBOARD_RANGE_CACHE_FILE`, or `/data/raw/material_info_range.json`.
3. Records are filtered to recent days, listed/OTC markets, and recent-financial candidates: self-reported EPS, attention-trading financial/EPS disclosures, or self-reported profit/loss disclosures without EPS.
4. Rows are split into `市場未反映` and `歷史公告` by the latest completed market close date.
5. The tab title area shows latest announcement time from the newest record in the rendered dataset.

Update API:

- `POST /api/admin/update`
- Auth: `Authorization: Bearer <TWSE_DASHBOARD_UPDATE_TOKEN>`
- Cooldown: `TWSE_DASHBOARD_UPDATE_MIN_INTERVAL`, default `300` seconds.
- Behavior: fetch realtime MOPS rows, enrich category/EPS/signal fields, merge into the active persistent range cache, write lifecycle metadata, then invalidate the recent-financial memory cache.

Primary cache:

- `TWSE_DASHBOARD_RANGE_CACHE_FILE`
- Default active cache: `/data/raw/material_info_range.json`.
- Metadata: `/data/raw/material_info_range_meta.json`, with `seeded_at`, `last_success_at`, `last_error`, `record_count`, and `newest_spoke_at`.

## Monthly Revenue Tab

Current display flow:

1. `DashboardServer._get_monthly_revenue_records()` loads `/data/raw/monthly_revenue_latest.json` or `TWSE_DASHBOARD_MONTHLY_REVENUE_CACHE_FILE`.
2. Source/update text is derived from `/data/raw/monthly_revenue_latest_meta.json` when present; if metadata is missing, the page falls back to values computed from the JSON cache.
3. The page keeps only monthly revenue records and selects the newest available revenue period.
4. Before a new period appears, the newest complete previous period remains visible.
5. After any newer-period row appears, only that newer period is displayed.
6. Company rows are de-duplicated by market, company code, and revenue period.
7. Rows are sorted by crawler/event time and split into `市場未反映` and `歷史公告`.
8. `detected_at` remains the data-observed timestamp. Official source dates are kept as source fields only.

Update API:

- `POST /api/admin/update-monthly-revenue`
- Auth: same `Authorization: Bearer <TWSE_DASHBOARD_UPDATE_TOKEN>` contract.
- Behavior: dynamically target the previous calendar month, fetch listed and OTC monthly revenue summary, append new or changed rows to the monthly revenue cache.

Source priority:

1. MOPS `t21sc04_ifrs` for listed plus OTC monthly revenue summary.
2. TWSE OpenAPI `https://openapi.twse.com.tw/v1/opendata/t187ap05_L` as listed-company fallback.
3. TPEX OpenAPI `https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O` as OTC-company fallback.
4. FinMind `TaiwanStockTradingDate` only for latest completed trading-day close classification.

Primary cache:

- `TWSE_DASHBOARD_MONTHLY_REVENUE_CACHE_FILE`
- Default: `/data/raw/monthly_revenue_latest.json`
- Metadata: `/data/raw/monthly_revenue_latest_meta.json`
- The metadata records `target_data_month`, `target_display_data_month`, `display_data_month`, `last_success_at`, `last_failed_at`, `last_error`, `market_results`, `market_failure_count`, `record_count`, `display_record_count`, and `newest_detected_at`.
- `target_display_data_month` is the month the scheduler is trying to fetch. `display_data_month` is the month currently shown by the page, so it can remain on the previous month until newer-period rows are detected.

## Financial Report Tab

Current display flow:

1. `DashboardServer._get_financial_report_records()` reads from the monthly signal cache path.
2. Records are filtered with `is_financial_report_record()`.
3. Rows are split into `市場未反映` and `歷史公告` by the same market-close classifier.
4. Original announcement text stays available through the same expandable-row pattern as the self-reported EPS tab.
5. This tab is still a shared/derived flow. Do not treat monthly revenue metadata as independent financial-report freshness until a dedicated financial-report cache and update endpoint are added.

Current limitation:

- There is no dedicated financial-report update endpoint yet.
- The tab currently depends on the monthly signal cache refresh path, not a separate persistent financial-report cache.

## Shared Table Behavior

- All tab tables are client-sortable through header buttons.
- Sorting is local to the browser and does not call update APIs.
- Group headers stay in place; rows are sorted within each `市場未反映` / `歷史公告` group.
- Expandable detail rows move together with their parent data row.

## Environment Variables

- `TWSE_DASHBOARD_UPDATE_TOKEN`: required for update endpoints in production.
- `TWSE_DASHBOARD_DATA_ROOT`: persistent cache root. Default is `/data`.
- `TWSE_DASHBOARD_RANGE_CACHE_FILE`: active self-reported/attention financial range cache path.
- `TWSE_DASHBOARD_MONTHLY_REVENUE_CACHE_FILE`: monthly revenue cache path.
- `TWSE_DASHBOARD_UPDATE_MIN_INTERVAL`: update cooldown seconds.
- `TWSE_DASHBOARD_RECENT_DAYS`: self-reported EPS lookback window.
- `TWSE_DASHBOARD_SEED_CACHE_ON_START`: set `0` to disable startup seed from bundled repo cache files.
- `FINMIND_TOKEN` or `FINMIND_API_TOKEN`: FinMind trading calendar token for market-close classification.

## Update Progress

| Area | Status | Notes |
| --- | --- | --- |
| Self-reported/attention financial update API | Done | `/api/admin/update` merges realtime MOPS rows into active cache and updates metadata. |
| Monthly revenue update API | Done | `/api/admin/update-monthly-revenue` updates newest target month with TWSE/TPEX fallbacks. |
| Monthly revenue newest-period display | Done | Page shows only the newest available revenue period. |
| Trading-day market reaction split | Done | Uses FinMind trading calendar when available, weekday fallback otherwise. |
| Sortable tables | Done | All three tab tables support local grouped sorting. |
| Zeabur persistent cache defaults | Done | Production defaults write cache files under `/data/raw`. |
| Initial launch cache seed | Done | Startup seed copies missing bundled range/monthly caches into `/data/raw`. |
| Financial report dedicated update/cache | Todo | Needs a separate endpoint/cache if the financial report tab must update independently. |
| Deployment scheduler workflow | Done | `.github/workflows/dashboard-update.yml` calls both update endpoints. |
| Production scheduler activation | Todo | Add GitHub secrets and confirm workflow runs on the default branch. |
