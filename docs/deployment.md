# Deployment Notes

## Update API protection

Production deployments should set `TWSE_DASHBOARD_UPDATE_TOKEN` in the platform environment variables or `.env`.
Do not commit the token to Git.

The cache update endpoint is:

```text
POST /api/admin/update
```

Call it with a Bearer token:

```powershell
curl -X POST https://your-domain.example/api/admin/update -H "Authorization: Bearer <token>"
```

The same token can also be sent with `X-Update-Token`, but `Authorization: Bearer <token>` is the preferred deployment contract.

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
