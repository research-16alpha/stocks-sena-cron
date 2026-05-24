"""
earnings_scraper.py
===================
DAILY cron - scrapes NSE board meeting calendar for upcoming earnings results.
Runs every 6 hours so we catch new disclosures within the day.

Source:
  - https://www.nseindia.com/api/event-calendar (public JSON, no auth)
  - Returns next ~30 days of corporate board meetings
  - Same cookie warm-up pattern as pcr_cron.py

Stores into public.corporate_calendar with event_type:
  - 'quarterly_result' when purpose mentions quarterly/Q1/Q2/Q3/Q4/quarter
  - 'annual_result'    when purpose mentions audited/annual/yearly
  - 'board_meeting'    for other board meetings (capex, fundraise, etc.)

Idempotent: upsert on (symbol, event_type, event_date).

Used by: EarningsCalendarScreen, "EARNINGS in 3d" badge on watchlist (later).
"""

import os
import re
import sys
import time
from datetime import datetime

import requests
from supabase import create_client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
sb = create_client(SUPABASE_URL, SUPABASE_KEY)

NSE_HOME = "https://www.nseindia.com"
NSE_CAL_URL = "https://www.nseindia.com/api/event-calendar"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-event-calendar",
    "Connection": "keep-alive",
}


def warm_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    s.get(NSE_HOME, timeout=10)
    time.sleep(1)
    s.get("https://www.nseindia.com/companies-listing/corporate-filings-event-calendar", timeout=10)
    time.sleep(1)
    return s


def fetch_calendar(session: requests.Session, retries: int = 3) -> list | None:
    for attempt in range(retries):
        try:
            r = session.get(NSE_CAL_URL, timeout=20)
            if r.status_code == 200:
                return r.json() or []
            print(f"[earnings] NSE HTTP {r.status_code} attempt {attempt + 1}", file=sys.stderr)
        except Exception as e:
            print(f"[earnings] fetch error: {e}", file=sys.stderr)
        time.sleep(2 + attempt * 2)
    return None


# Heuristic classification of NSE purpose text.
QUARTERLY_RE = re.compile(r"\b(quarter|quarterly|q[1-4]|unaudited)\b", re.I)
ANNUAL_RE = re.compile(r"\b(annual|audited|yearly|year ended)\b", re.I)
RESULT_RE = re.compile(r"\b(result|financial\s+result)s?\b", re.I)


def classify(purpose: str) -> str:
    """Map NSE purpose text to one of our event_type values."""
    p = purpose or ""
    if RESULT_RE.search(p):
        if ANNUAL_RE.search(p):
            return "annual_result"
        return "quarterly_result"   # default to quarterly when ambiguous
    return "board_meeting"


def parse_date(date_str: str) -> str | None:
    """NSE date format: '24-May-2026' -> '2026-05-24' (ISO)."""
    if not date_str:
        return None
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(date_str.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def normalize(rows: list) -> list[dict]:
    """Convert NSE rows into our corporate_calendar shape."""
    out = []
    seen: set[tuple] = set()
    for r in rows:
        symbol = (r.get("symbol") or r.get("Symbol") or "").strip()
        purpose = (r.get("purpose") or r.get("Purpose") or "").strip()
        date_raw = r.get("date") or r.get("Date") or r.get("bm_date") or ""
        iso_date = parse_date(date_raw)
        if not symbol or not iso_date:
            continue
        event_type = classify(purpose)
        key = (symbol, event_type, iso_date)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "symbol": symbol,
            "event_type": event_type,
            "event_date": iso_date,
            "description": purpose[:240] if purpose else None,
            "is_confirmed": True,
        })
    return out


def main():
    session = warm_session()
    rows = fetch_calendar(session)
    if rows is None:
        print("[earnings] could not fetch NSE event calendar")
        sys.exit(1)

    normalized = normalize(rows)
    if not normalized:
        print("[earnings] no events to insert (NSE returned 0 usable rows)")
        return

    # Bulk upsert. on_conflict needs the constraint we added on
    # (symbol, event_type, event_date).
    try:
        sb.table("corporate_calendar").upsert(
            normalized, on_conflict="symbol,event_type,event_date"
        ).execute()
        by_type: dict[str, int] = {}
        for r in normalized:
            by_type[r["event_type"]] = by_type.get(r["event_type"], 0) + 1
        print(f"[earnings] {datetime.now().isoformat()} · upserted {len(normalized)} events")
        for k, v in sorted(by_type.items()):
            print(f"  {k}: {v}")
    except Exception as e:
        print(f"[earnings] upsert failed: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
