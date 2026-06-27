# TWSE News Dashboard

台股重大訊息更新 dashboard 專案。

## Project Structure

```text
twse-news-dashboard/
├─ src/
│  └─ main.py
├─ data/
│  ├─ raw/
│  └─ processed/
├─ notebooks/
├─ docs/
├─ tests/
├─ README.md
├─ requirements.txt
├─ .env
└─ .gitignore
```

## Local Setup

1. Put API tokens and local secrets in `.env`.
2. Install dependencies after `requirements.txt` is filled:

```powershell
pip install -r requirements.txt
```

3. Run the app entry point:

```powershell
python src/main.py crawl
python src/main.py crawl-previous --max-items 10
python src/main.py crawl-range --start-date 2026-06-01 --end-date 2026-06-27
python src/main.py serve --max-items 10
```

## MOPS Realtime Material Information Demo

第一個功能會抓取公開資訊觀測站「即時重大訊息」清單，並逐筆 POST 詳細資料按鈕使用的參數到同一個 AJAX endpoint，取得詳細視窗中的發言人、條款、事實發生日與完整說明。

公開頁面本身每 180 秒自動更新一次，因此 demo server 預設也會快取 180 秒；逐筆詳細資料請求預設間隔 1 秒，避免過度頻繁抓取。

```powershell
# 抓取即時清單全部資料並儲存到 data/raw/latest_material_info.json
python src/main.py crawl

# 只保留財報自結類別
python src/main.py crawl --category financial-self-report

# 抓取前一日資料並儲存到 data/raw/previous_material_info.json
python src/main.py crawl-previous --max-items 10

# 指定日期抓取前一日/當日重大訊息頁的資料
python src/main.py crawl-previous --date 2026-06-26 --max-items 10

# 抓取日期區間資料，預設只抓每日清單與內嵌完整說明
python src/main.py crawl-range --start-date 2026-06-01 --end-date 2026-06-27 --output data/raw/material_info_2026-06-01_2026-06-27.json

# 保守節流：每個 HTTP request 間隔 1 秒，每個日期清單之間額外等 5 秒
python src/main.py crawl-range --start-date 2026-06-01 --end-date 2026-06-27 --request-interval 1 --day-interval 5

# 若需要逐筆打開詳細頁，再加 include-details；這會產生很多請求，請保守使用
python src/main.py crawl-range --start-date 2026-06-01 --end-date 2026-06-27 --include-details --request-interval 2 --day-interval 5

# 啟動即時 demo 頁
python src/main.py serve --host 127.0.0.1 --port 8000

# 啟動前一日資料 demo 頁
python src/main.py serve --host 127.0.0.1 --port 8000 --mode previous --max-items 10

# 使用已抓下來的 JSON 啟動離線 demo 頁
python src/main.py serve --host 127.0.0.1 --port 8000 --offline-file data/raw/latest_material_info.json
```

Demo page:

- HTML: `http://127.0.0.1:8000/`
- Previous day HTML: `http://127.0.0.1:8000/?mode=previous`
- Recent financial self-report HTML: `http://127.0.0.1:8000/?mode=recent-financial`
- JSON: `http://127.0.0.1:8000/api/news`
- Previous day JSON: `http://127.0.0.1:8000/api/news?mode=previous`
- Recent financial self-report JSON: `http://127.0.0.1:8000/api/news?mode=recent-financial`
- Update cache API: `POST http://127.0.0.1:8000/api/admin/update`

`mode=recent-financial` 會讀取本機 `crawl-range` 產生的區間 cache，取近 7 日並依公告日期時間倒序顯示，不會在點擊時重新爬站。可用 `--range-cache-file` 指定 cache 檔案，或用 `--recent-days` 調整天數。

## Cache enrichment

舊的 demo cache 若還沒有 `eps_metrics`，可以先把 EPS 解析結果寫回 JSON，讓部署時不需要等第一次視窗開啟才計算：

```powershell
python src/main.py enrich-cache data/raw/material_info_2026-06-01_2026-06-27.json --recompute-eps
```

## Update path

Deployment reminder: see `docs/deployment.md` before production setup.

排程本身先不放在專案內。未來可以用主機 Cron、GitHub Actions、或外部 cron service 在 07:00-23:00 每 5 分鐘打一次：

```powershell
$env:TWSE_DASHBOARD_UPDATE_TOKEN="dev-token"
python src/main.py serve --host 127.0.0.1 --port 8000
curl -X POST http://127.0.0.1:8000/api/admin/update -H "Authorization: Bearer dev-token"
```

部署時建議在 `.env` 或平台環境變數設定 `TWSE_DASHBOARD_UPDATE_TOKEN`，並用 Bearer token 呼叫：

```powershell
curl -X POST https://your-domain.example/api/admin/update -H "Authorization: Bearer <token>"
```

這個路徑會抓取公開資訊觀測站即時重大訊息、合併到 range cache、寫回 `eps_metrics`，並有 300 秒 cooldown 避免短時間重複爬取。可用 `--update-min-interval` 調整 cooldown 秒數。

若只是本機開發測試、且刻意不設定 token，可加 `--allow-unprotected-local-update`；production 不要使用這個選項。

## Tests

```powershell
python -m pytest
```
