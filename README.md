# Promoter Action Scraper · Deploy Guide

Python script that scrapes BSE corporate announcements every 15 min and pushes structured promoter-action records to your Supabase `promoter_actions` table.

## Local test run

```powershell
cd "E:\Stocks sena\StocksSenaApp\cron"
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:SUPABASE_URL = "https://your-project.supabase.co"
$env:SUPABASE_SERVICE_KEY = "your_service_role_key_not_anon"
python promoter_scraper.py
```

You should see something like:
```
[INFO] Fetched 47 announcements
[OK] 2026-05-20T14:23:11 · inserted 3 records
```

Check Supabase Table Editor → `promoter_actions` — 3 new rows.

## Deploy as cron job (free)

### Option A — Render.com (recommended)

1. Push the `cron/` folder to a GitHub repo (private OK).
2. Sign up at **https://render.com** (free).
3. New → **Cron Job**.
4. Connect your GitHub repo.
5. Configure:
   - **Schedule:** `*/15 * * * *` (every 15 min)
   - **Build command:** `pip install -r cron/requirements.txt`
   - **Command:** `python cron/promoter_scraper.py`
6. Environment variables:
   - `SUPABASE_URL` = your project URL
   - `SUPABASE_SERVICE_KEY` = service_role key (from Supabase Settings → API)
   - `MODE` = `once`
7. Save. First run starts within 15 min.

Free tier: 750 cron-job hours/month. This script runs ~5 seconds per execution. Even at 96 runs/day it uses minimal time.

### Option B — Your own laptop (zero cost)

Just run the loop mode on a laptop that stays on:

```powershell
$env:SUPABASE_URL = "https://your-project.supabase.co"
$env:SUPABASE_SERVICE_KEY = "your_service_role_key"
$env:MODE = "loop"
python promoter_scraper.py
```

It will loop every 15 min until you Ctrl+C.

### Option C — DigitalOcean droplet ($5/mo) or any cheap VPS

Same as Option B but on a server. Use `systemd` or `pm2` to keep it running. This is what you'd do once revenue justifies it.

## What gets stored

Each filing matched by the pattern matcher becomes one row in `promoter_actions`:

| column | example |
|---|---|
| `symbol` | `542484` (BSE scrip code) |
| `company_name` | `Adani Green Energy Limited` |
| `action_type` | `sale` |
| `action_description` | `Promoter sold 2,10,000 shares at average ₹1,242` |
| `severity` | `4` |
| `filing_date` | `2026-05-19T11:32:00+05:30` |
| `source_url` | `https://www.bseindia.com/corporates/anndet_new.aspx?scrip=542484` |
| `raw_data` | full BSE JSON for forensics |

The app picks these up in real-time via Supabase Realtime → users see new filings within 30 seconds of the cron run.

## Extending coverage

To add NSE filings (broader coverage), modify `fetch_bse_announcements` to also pull from:

```
https://www.nseindia.com/api/corporate-announcements?index=equities&from_date=...&to_date=...
```

NSE requires browser-like headers and a session cookie warm-up. See `requests.Session` with cookie persistence.

## Adding more pattern types

The `PATTERNS` list in `promoter_scraper.py` defines what gets classified. Add new patterns by appending tuples:

```python
PATTERNS.append(
    (r"buyback.+approved", "buyback_announced", 3, None)
)
```

Severity 1-4 maps to dots shown in the app (4 = red, 3 = red, 2 = gold, 1 = neutral).
