"""
Promoter Action Scraper · Stocks Sena
======================================
Pulls corporate filings from BSE every 15 minutes and pushes structured
records to Supabase `promoter_actions` table.

Sources (all free):
  - BSE Corporate Announcements:
    https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w
  - NSE: requires headers, slower — start with BSE only

Detected event types:
  - Promoter pledge increase/decrease
  - Promoter sale / purchase
  - Auditor change
  - Related-party transaction (RPT)

Deploy as scheduled job on Render (free tier) or a cheap VPS.
"""

import os
import time
import re
from datetime import datetime, timedelta
import requests
from supabase import create_client, Client

# ============================================================
# CONFIG
# ============================================================

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")  # service_role, not anon

if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    raise SystemExit("Missing SUPABASE_URL or SUPABASE_SERVICE_KEY env vars")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

BSE_ANNOUNCEMENTS_URL = (
    "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json",
    "Referer": "https://www.bseindia.com/",
}

# Keyword patterns for classification + severity
PATTERNS = [
    # (regex, action_type, severity, label_extract_fn)
    (r"pledg[ed]|pledge\s+of\s+shares.+increas", "pledge_increase", 3, None),
    (r"pledg[ed]|pledge.+decreas|release\s+of\s+pledge", "pledge_decrease", 2, None),
    (r"sale\s+of\s+(equity\s+)?shares\s+by\s+(promoter|insider)", "sale", 4, None),
    (r"acquisition\s+of\s+shares|purchase\s+of\s+shares\s+by\s+promoter", "purchase", 2, None),
    (r"appointment.+auditor|change.+auditor|resignation.+auditor", "auditor_change", 3, None),
    (r"related[\s-]party\s+transaction", "rpt_disclosed", 2, None),
]


def fetch_bse_announcements(from_date: str, to_date: str):
    """Pull last N hours of BSE corporate announcements."""
    params = {
        "strCat": "-1",
        "strPrevDate": from_date,
        "strToDate": to_date,
        "strScrip": "",
        "strSearch": "P",
        "strType": "C",
        "pageno": "1",
    }
    try:
        r = requests.get(BSE_ANNOUNCEMENTS_URL, params=params, headers=HEADERS, timeout=30)
        r.raise_for_status()
        return r.json().get("Table", [])
    except Exception as e:
        print(f"[WARN] BSE fetch failed: {e}")
        return []


def classify(text: str):
    """Match announcement text against patterns. Returns (action_type, severity) or None."""
    text_lower = text.lower()
    for regex, action_type, severity, _ in PATTERNS:
        if re.search(regex, text_lower):
            return action_type, severity
    return None


def deduplicate(records):
    """Avoid inserting same filing twice (BSE shows duplicates across days)."""
    existing = (
        supabase.table("promoter_actions")
        .select("symbol, action_description")
        .gte("filing_date", (datetime.now() - timedelta(days=14)).isoformat())
        .execute()
    )
    seen = {(r["symbol"], r["action_description"]) for r in (existing.data or [])}
    return [r for r in records if (r["symbol"], r["action_description"]) not in seen]


def process_announcements(items):
    new_records = []
    for item in items:
        symbol = item.get("SCRIP_CD") or item.get("scrip_code") or ""
        company = item.get("SLONGNAME") or item.get("company_name") or "Unknown"
        headline = item.get("HEADLINE") or item.get("NEWSSUB") or ""
        body = item.get("NEWSSUB") or item.get("MORE") or ""

        full_text = f"{headline} {body}"
        classification = classify(full_text)
        if not classification:
            continue

        action_type, severity = classification
        new_records.append(
            {
                "symbol": symbol,
                "company_name": company,
                "action_type": action_type,
                "action_description": headline[:500] if headline else body[:500],
                "severity": severity,
                "filing_date": item.get("NEWS_DT") or datetime.now().isoformat(),
                "source_url": f"https://www.bseindia.com/corporates/anndet_new.aspx?scrip={symbol}",
                "raw_data": item,
            }
        )

    if not new_records:
        print(f"[INFO] {datetime.now().isoformat()} · 0 new records")
        return

    deduped = deduplicate(new_records)
    if not deduped:
        print(f"[INFO] {datetime.now().isoformat()} · all duplicates, skipping")
        return

    result = supabase.table("promoter_actions").insert(deduped).execute()
    print(f"[OK] {datetime.now().isoformat()} · inserted {len(deduped)} records")


def main_once():
    """Single run — for testing or cron schedule."""
    today = datetime.now().strftime("%Y%m%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
    items = fetch_bse_announcements(yesterday, today)
    print(f"[INFO] Fetched {len(items)} announcements")
    process_announcements(items)


def main_loop():
    """Long-running loop — for VPS deployment."""
    while True:
        try:
            main_once()
        except Exception as e:
            print(f"[ERROR] {datetime.now().isoformat()} · {e}")
        # Sleep 15 minutes
        time.sleep(15 * 60)


if __name__ == "__main__":
    mode = os.environ.get("MODE", "once")
    if mode == "loop":
        main_loop()
    else:
        main_once()
