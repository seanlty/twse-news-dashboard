# Initial Launch Tracker

This tracker records what is still needed before the first Zeabur deployment can run with automatic updates.

## Current Target

- Platform: Zeabur
- Docker entrypoint: `python /app/src/main.py serve`
- Persistent cache mount: Zeabur Volume mounted at `/data`
- Runtime cache folder: `/data/raw`
- Scheduler: GitHub Actions calling protected update endpoints

## Required Production Environment

Set these on Zeabur:

```text
HOST=0.0.0.0
PORT=<platform-port>
TWSE_DASHBOARD_DATA_ROOT=/data
TWSE_DASHBOARD_RANGE_CACHE_FILE=/data/raw/material_info_2026-06-01_2026-06-27_financial_self_report.json
TWSE_DASHBOARD_MONTHLY_REVENUE_CACHE_FILE=/data/raw/monthly_revenue_latest.json
TWSE_DASHBOARD_FINANCIAL_REPORT_CACHE_FILE=/data/raw/financial_report_latest.json
TWSE_DASHBOARD_FINANCIAL_REPORT_LOOKBACK_DAYS=3
TWSE_DASHBOARD_UPDATE_MIN_INTERVAL=300
TWSE_DASHBOARD_UPDATE_TOKEN=<secret-token>
TWSE_DASHBOARD_RECENT_DAYS=7
FINMIND_TOKEN=<secret-token>
```

Set these on GitHub Actions secrets:

```text
TWSE_DASHBOARD_BASE_URL=https://<zeabur-domain>
TWSE_DASHBOARD_UPDATE_TOKEN=<same-secret-token-as-zeabur>
```

## Progress Checklist

| Item | Status | Implementation |
| --- | --- | --- |
| Zeabur Dockerfile | Done | `Dockerfile` uses `WORKDIR /app` and explicitly starts `/app/src/main.py`, avoiding Zeabur's default `/app/main.py` lookup. |
| GitHub Actions workflow | Done | `.github/workflows/dashboard-update.yml` calls `/api/admin/update`, `/api/admin/update-monthly-revenue`, and `/api/admin/update-financial-report`. |
| Zeabur persistent cache path | Done | Production defaults now write to `/data/raw/...`; `TWSE_DASHBOARD_DATA_ROOT` can override the root. |
| Zeabur Volume mount | Manual | Mount a Zeabur Volume at `/data` before enabling the scheduler. |
| Initial cache seed | Done | Startup seed copies missing bundled `data/raw/material_info_*.json`, `data/raw/monthly_revenue_latest.json`, and optional `data/raw/financial_report_latest.json` into `/data/raw`. |
| Seed cache tracked by Git | Done | `.gitignore` allows the self-report seed JSON, monthly revenue seed JSON, and optional financial report seed JSON into Git/Docker image. |
| Seed overwrite protection | Done | Startup seed does not overwrite existing `/data/raw` cache files. |
| GitHub secrets | Manual | Add `TWSE_DASHBOARD_BASE_URL` and `TWSE_DASHBOARD_UPDATE_TOKEN`. |
| First live update verification | Todo | Manually run the workflow and confirm all three update endpoints return `ok: true` or cooldown `skipped: true`. |
| Health check | Todo | Confirm `GET /health` returns `ok` on the deployed domain. |
| Data check | Todo | Confirm `/api/news?tab=monthly-revenue` and `/api/news?tab=material-info` return records after deploy. |

## GitHub Actions Behavior

The workflow is scheduled in UTC because GitHub Actions cron uses UTC, but the intended monitoring window is explicitly `07:00-23:00 Asia/Taipei`.

Current schedule:

- `*/5 23 * * *`: `07:00-07:55 Asia/Taipei`
- `*/5 0-14 * * *`: `08:00-22:55 Asia/Taipei`
- `0 15 * * *`: `23:00 Asia/Taipei`

The job also checks `Asia/Taipei` local time inside the workflow as a safety gate. It proceeds only during `07:00-23:00 Asia/Taipei`, or when manually triggered with `workflow_dispatch`.

Endpoints called:

```text
POST /api/admin/update
POST /api/admin/update-monthly-revenue
POST /api/admin/update-financial-report
```

Both use:

```text
Authorization: Bearer <TWSE_DASHBOARD_UPDATE_TOKEN>
```

## Cache Seed Contract

Bundled repo seed files:

- `data/raw/material_info_2026-06-01_2026-06-27_financial_self_report.json`
- `data/raw/monthly_revenue_latest.json`
- optional `data/raw/financial_report_latest.json`

Runtime target files:

- `/data/raw/material_info_2026-06-01_2026-06-27_financial_self_report.json`
- `/data/raw/monthly_revenue_latest.json`
- `/data/raw/financial_report_latest.json`

The seed is intentionally one-way and non-destructive:

1. If `/data/raw` is empty, seed files are copied in on startup.
2. If a target file already exists, the app leaves it untouched.
3. Scheduler updates then append/merge into the persistent `/data/raw` cache files.

Disable startup seeding only if needed:

```text
TWSE_DASHBOARD_SEED_CACHE_ON_START=0
```

## Remaining Before First Public Use

1. Commit and push the Dockerfile, workflow, and cache-path changes to the default branch.
2. Configure Zeabur env vars and mount the `/data` Volume.
3. Configure GitHub Actions secrets.
4. Deploy Zeabur and confirm `/health`.
5. Run `dashboard-update.yml` manually once.
6. Check `/api/news?tab=material-info`, `/api/news?tab=monthly-revenue`, and `/api/news?tab=financial-report`.
7. Let the scheduled workflow run at least once inside `07:00-23:00 Asia/Taipei` and inspect logs.
