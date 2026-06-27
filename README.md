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
python src/main.py
```
