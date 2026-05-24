"""
pcr_cron.py
===========
Runs every 10 min during market hours (9:15 AM - 3:30 PM IST, weekdays).
Fetches NIFTY option chain from NSE, computes Put/Call OI ratio for the
NEAREST WEEKLY EXPIRY, writes one row to nifty_pcr_history.

Source: https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY
  - Public JSON endpoint
  - Requires User-Agent + cookie warm-up (visit main page first to get cookies)
  - No auth, no API key

Why nearest weekly: it's where the bulk of speculative positioning sits,
making PCR most reflective of short-term sentiment.

Stored in nifty_pcr_history (one row per run). Consumed by the app via
useMarketMood hook (latest row).
"""

import os
import sys
import time
from datetime import datetime

import requests
from supabase import create_client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
sb = create_client(SUPABASE_URL, SUPABASE_KEY)

NSE_HOME = "https://www.nseindia.com"
NSE_OC_URL = "https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.nseindia.com/option-chain",
    "Connection": "keep-alive",
}


def warm_session() -> requests.Session:
    """NSE blocks bare requests. Visit homepage to populate cookies first."""
    s = requests.Session()
    s.headers.update(HEADERS)
    # Two warm-up visits — homepage, then option-chain page
    s.get(NSE_HOME, timeout=10)
    time.sleep(1)
    s.get("https://www.nseindia.com/option-chain", timeout=10)
    time.sleep(1)
    return s


def fetch_option_chain(session: requests.Session, retries: int = 3) -> dict | None:
    for attempt in range(retries):
        try:
            r = session.get(NSE_OC_URL, timeout=20)
            if r.status_code == 200:
                return r.json()
            print(f"[WARN] NSE returned {r.status_code} on attempt {attempt + 1}", file=sys.stderr)
        except Exception as e:
            print(f"[WARN] NSE fetch error: {e}", file=sys.stderr)
        time.sleep(2 + attempt * 2)
    return None


def compute_pcr_for_nearest_expiry(payload: dict) -> dict | None:
    """
    payload['records']['data'] -> list of strike rows, each with optional CE/PE legs.
    payload['records']['expiryDates'] -> sorted list of expiry strings (DD-MMM-YYYY).
    Returns dict ready for insert, or None.
    """
    records = payload.get("records") or {}
    data = records.get("data") or []
    expiries = records.get("expiryDates") or []
    if not data or not expiries:
        return None

    nearest = expiries[0]   # already sorted ascending
    spot = records.get("underlyingValue")

    total_call_oi = 0
    total_put_oi = 0
    for row in data:
        if row.get("expiryDate") != nearest:
            continue
        ce = row.get("CE") or {}
        pe = row.get("PE") or {}
        total_call_oi += int(ce.get("openInterest") or 0)
        total_put_oi += int(pe.get("openInterest") or 0)

    if total_call_oi == 0:
        return None

    pcr = round(total_put_oi / total_call_oi, 4)

    # Convert NSE expiry "DD-MMM-YYYY" to ISO YYYY-MM-DD
    try:
        expiry_iso = datetime.strptime(nearest, "%d-%b-%Y").date().isoformat()
    except ValueError:
        expiry_iso = None

    return {
        "expiry": expiry_iso,
        "spot": float(spot) if spot is not None else None,
        "total_call_oi": total_call_oi,
        "total_put_oi": total_put_oi,
        "pcr": pcr,
    }


def is_market_open() -> bool:
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    mins = now.hour * 60 + now.minute
    return 555 <= mins <= 930


def main():
    if os.environ.get("SKIP_MARKET_CHECK") != "1" and not is_market_open():
        print("[INFO] Market closed, skipping PCR fetch.")
        return

    session = warm_session()
    payload = fetch_option_chain(session)
    if not payload:
        print("[FAIL] Could not fetch option chain after retries.")
        sys.exit(1)

    row = compute_pcr_for_nearest_expiry(payload)
    if not row:
        print("[FAIL] Could not compute PCR from payload.")
        sys.exit(1)

    print(f"[OK] PCR: {row}")
    sb.table("nifty_pcr_history").insert(row).execute()
    print(f"[OK] Wrote nifty_pcr_history row at {datetime.now().isoformat()}")


if __name__ == "__main__":
    main()
