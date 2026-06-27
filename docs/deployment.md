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
TWSE_DASHBOARD_RANGE_CACHE_FILE=data/raw/material_info_2026-06-01_2026-06-27.json
TWSE_DASHBOARD_RECENT_DAYS=7
TWSE_DASHBOARD_UPDATE_MIN_INTERVAL=300
TWSE_DASHBOARD_UPDATE_TOKEN=<secret-token>
```

The `serve` command reads these environment variables as defaults:

```powershell
python src/main.py serve
```

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

## Scheduler reminder

When moving to production, run the update request every 5 minutes during the active monitoring window:

```text
07:00-23:00 Asia/Taipei
```

The scheduler can be a host cron, GitHub Actions, or an external cron service. The dashboard server already has a 300 second cooldown on `/api/admin/update`, so accidental repeated calls should not immediately hammer MOPS.

## Cache before first deploy

Before deploying, make sure the demo/range cache has derived EPS fields written to disk:

```powershell
python src/main.py enrich-cache data/raw/material_info_2026-06-01_2026-06-27.json --recompute-eps
```

This keeps the page from opening empty while waiting for the first live update.
