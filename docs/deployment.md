# Deployment Notes

## Update API protection

Production deployments should set `TWSE_DASHBOARD_UPDATE_TOKEN` in the platform environment variables or `.env`.
Do not commit the token to Git.

The update endpoint requires this token by default. For local development only, `--allow-unprotected-local-update` or `TWSE_DASHBOARD_DEV_ALLOW_UNPROTECTED_UPDATE=1` can allow localhost calls without a token. Do not use that setting in production.

The cache update endpoint is:

```text
POST /api/admin/update
```

Call it with a Bearer token:

```powershell
curl -X POST https://your-domain.example/api/admin/update -H "Authorization: Bearer <token>"
```

The same token can also be sent with `X-Update-Token`, but `Authorization: Bearer <token>` is the preferred deployment contract.

## Production server environment

Recommended environment variables:

```text
HOST=0.0.0.0
PORT=<platform-port>
TWSE_DASHBOARD_DATA_ROOT=/data
TWSE_DASHBOARD_RANGE_CACHE_FILE=/data/raw/material_info_2026-06-01_2026-06-27_financial_self_report.json
TWSE_DASHBOARD_MONTHLY_REVENUE_CACHE_FILE=/data/raw/monthly_revenue_latest.json
TWSE_DASHBOARD_RECENT_DAYS=7
TWSE_DASHBOARD_UPDATE_MIN_INTERVAL=300
TWSE_DASHBOARD_UPDATE_TOKEN=<secret-token>
FINMIND_TOKEN=<secret-token>
```

The `serve` command reads these environment variables as defaults:

```powershell
python src/main.py serve
```

## Zeabur Docker entrypoint

The repository includes a `Dockerfile` for Zeabur deployment. It sets:

```text
WORKDIR /app
CMD ["python", "/app/src/main.py", "serve"]
```

This is intentional: Zeabur often assumes an app entry under `/app`, but this repo's server entrypoint is `src/main.py`, not `/app/main.py`.

## Production deployment TODO

The current stable production direction is:

1. Prepare one enriched cache file before first deploy.
2. Ship or mount that cache file with the production instance.
3. Set `TWSE_DASHBOARD_RANGE_CACHE_FILE` to that cache path.
4. Start the server with `python src/main.py serve`.
5. Configure a scheduler to call `/api/admin/update` for incremental updates.
6. Keep `TWSE_DASHBOARD_UPDATE_TOKEN` set and call the update endpoint with `Authorization: Bearer <token>`.
7. Verify the deployed page has data before enabling the scheduler.

This avoids an empty first page while the app is waiting for the first live crawl.

## Zeabur persistent cache

For Zeabur, mount a Volume at:

```text
/data
```

The app defaults to `/data/raw/...` for production cache files. Keep update endpoints writing to this mounted path instead of repo-relative `./data` or `./cache` paths, because the default service filesystem is stateless and can reset on restart or redeploy.

For material-info/self-report data, the active cache is `/data/raw/material_info_range.json` and the lifecycle metadata file is `/data/raw/material_info_range_meta.json`. On first boot, `python src/main.py serve` promotes an existing legacy `/data/raw/material_info_*.json` file into the active cache when present; otherwise it seeds the active cache from the bundled repo `data/raw` folder. This preserves already-updated Zeabur volume data while moving the page away from date-stamped seed filenames.

The metadata file records seed/update lifecycle fields such as `seeded_at`, `last_success_at`, `last_error`, `record_count`, and `newest_spoke_at`. The page header should describe source/update state from metadata, not infer freshness from the cache filename.

Seed behavior can be disabled with:

```text
TWSE_DASHBOARD_SEED_CACHE_ON_START=0
```

## Scheduler reminder

When moving to production, run the update request every 5 minutes during the active monitoring window:

```text
07:00-23:00 Asia/Taipei
```

The scheduler can be a host cron, GitHub Actions, or an external cron service. The dashboard server already has a 300 second cooldown on `/api/admin/update`, so accidental repeated calls should not immediately hammer MOPS.

## Monthly Revenue Scheduler Memo

When enabling the monthly revenue tab in production, keep the update flow separate from material information, self-profit, and financial report crawlers.

The monthly revenue cache update endpoint is `POST /api/admin/update-monthly-revenue` and uses the same `Authorization: Bearer <TWSE_DASHBOARD_UPDATE_TOKEN>` contract.

Monthly revenue scheduler direction:

1. Run a cron, GitHub Actions schedule, or external cron service every 5 minutes during `07:00-23:00 Asia/Taipei`.
2. Each scheduled call dynamically targets the previous calendar month as the revenue month. For example, calls during July 2026 target `2026/06` monthly revenue.
3. First fetch MOPS `t21sc04_ifrs` monthly revenue summary for both `sii` and `otc`.
4. If `sii` fails, use TWSE OpenAPI `https://openapi.twse.com.tw/v1/opendata/t187ap05_L` as the listed-company fallback.
5. If `otc` fails, use TPEX OpenAPI `https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O` as the OTC-company fallback.
6. Append only new or changed rows to the monthly revenue cache. Use crawler `detected_at` as the data-observed timestamp.
7. Do not use official `出表日期` as the row data time. Keep `出表日期` only as a source field.
8. If a fallback also fails, keep the previous cache for that market and record the market failure in the monthly revenue update response.
9. The page reads the multi-period cache but displays only the newest available revenue month. Before the new month appears, it keeps showing the prior month; after any new-month row appears, it switches to that month only.
10. Set `FINMIND_TOKEN` or `FINMIND_API_TOKEN` in the production environment so the page can use FinMind `TaiwanStockTradingDate` for the latest completed market-close date. If FinMind is unavailable, the page falls back to weekday-based close detection.

Source role notes:

- `t21sc04_ifrs`: primary source for listed plus OTC monthly revenue. It supports market/month selection and is better for complete market coverage.
- `t187ap05_L`: fallback or cross-check source for listed companies only. It is fast and JSON-shaped, but it cannot replace `t21sc04_ifrs` for full listed plus OTC coverage by itself.
- `mopsfin_t187ap05_O`: fallback or cross-check source for OTC companies from TPEX OpenAPI. It completes the listed plus OTC fallback plan, but MOPS remains the primary source when available.
- `TaiwanStockTradingDate`: FinMind trading-calendar source used only to classify monthly revenue rows into `市場未反映` versus `歷史公告`. The row timestamp remains crawler `detected_at`.

## Cache before first deploy

Before deploying, make sure the demo/range cache has derived EPS fields written to disk:

```powershell
python src/main.py enrich-cache data/raw/material_info_2026-06-01_2026-06-27.json --recompute-eps
```

This keeps the page from opening empty while waiting for the first live update.

The initial deployment seed should include:

- a self-reported EPS/material-info range cache, for example `data/raw/material_info_2026-06-01_2026-06-27_financial_self_report.json`
- `data/raw/monthly_revenue_latest.json`

At runtime the material-info seed is copied/promoted into `/data/raw/material_info_range.json`; the original dated seed filename is not used as the long-lived active cache identity.

After Zeabur Volume is mounted at `/data`, these seed files should exist under `/data/raw` after first boot.
