"""
fii_dii_scraper.py
==================
DAILY cron - scrapes NSE's published FII + DII daily cash flows.
Runs once a day at 6 PM IST (after NSE publishes EOD report ~5:30 PM).

Source: https://www.nseindia.com/api/fiidiiTradeReact
  - Public JSON endpoint
  - Same cookie warm-up pattern as pcr_cron / earnings_scraper
  - Returns most recent trading day for FII/FPI + DII (cash segment)

Output: upsert to public.fii_dii_flows on (date, category, segment).
Powers: Home FII/DII widget + Mood Index "Smart Money Flow" signal.

NOTE: replaces the deleted fake `fii_dii_scraper.py` that was using
random.seed(42) to generate flows. ALL DATA HERE IS REAL.
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
NSE_FII_DII_URL = "https://www.nseindia.com/api/fiidiiTradeReact"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/reports/fii-dii",
    "Connection": "keep-alive",
}


def warm_session() -> requests.Session:
    # Warm-up is BEST-EFFORT: NSE throttles cloud IPs intermittently and a timeout
    # here used to crash the whole run before fetch() ever got its retries.
    s = requests.Session()
    s.headers.update(HEADERS)
    for url in (NSE_HOME, "https://www.nseindia.com/reports/fii-dii"):
        try:
            s.get(url, timeout=25)
        except Exception as e:
            print(f"[fiidii] warmup {url} failed (continuing): {e}", file=sys.stderr)
        time.sleep(1.2)
    return s


def fetch(session: requests.Session = None, retries: int = 4) -> list | None:
    for attempt in range(retries):
        try:
            if session is None or attempt > 0:   # fresh cookies per retry
                session = warm_session()
            r = session.get(NSE_FII_DII_URL, timeout=30)
            if r.status_code == 200:
                data = r.json()
                # Endpoint sometimes wraps in {"data": [...]} or returns raw list
                if isinstance(data, dict) and "data" in data:
                    return data["data"]
                return data
            print(f"[fiidii] NSE HTTP {r.status_code} attempt {attempt + 1}", file=sys.stderr)
        except Exception as e:
            print(f"[fiidii] fetch error: {e}", file=sys.stderr)
        time.sleep(2 + attempt * 2)
    return None


# NSE returns category like "FII **", "FII/FPI **", "DII **" - strip noise.
CATEGORY_FII_RE = re.compile(r"\b(fii|fpi)\b", re.I)
CATEGORY_DII_RE = re.compile(r"\bdii\b", re.I)


def normalize_category(raw: str) -> str | None:
    if not raw:
        return None
    r = raw.strip()
    if CATEGORY_FII_RE.search(r):
        return "FII/FPI"
    if CATEGORY_DII_RE.search(r):
        return "DII"
    return None


def parse_date(s: str) -> str | None:
    if not s:
        return None
    s = s.strip()
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def to_num(v) -> float | None:
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def normalize(rows: list) -> list[dict]:
    """NSE row keys vary - tolerate common variants."""
    out = []
    seen: set[tuple] = set()
    for r in rows:
        if not isinstance(r, dict):
            continue
        cat_raw = (
            r.get("category")
            or r.get("Category")
            or r.get("type")
            or ""
        )
        category = normalize_category(cat_raw)
        if not category:
            continue
        date_raw = (
            r.get("date")
            or r.get("reportDate")
            or r.get("trade_date")
            or ""
        )
        iso_date = parse_date(date_raw)
        if not iso_date:
            continue
        buy = to_num(r.get("buyValue") or r.get("buy_value") or r.get("buyAmount"))
        sell = to_num(r.get("sellValue") or r.get("sell_value") or r.get("sellAmount"))
        net = to_num(r.get("netValue") or r.get("net_value") or r.get("netAmount"))
        if net is None and buy is not None and sell is not None:
            net = round(buy - sell, 2)
        # Segment - default to 'Equity' if not specified (most common in NSE feed)
        seg = (
            r.get("segment")
            or r.get("Segment")
            or "Equity"
        ).strip() or "Equity"

        key = (iso_date, category, seg)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "date": iso_date,
            "category": category,
            "segment": seg,
            "buy_value_cr": buy,
            "sell_value_cr": sell,
            "net_value_cr": net,
        })
    return out


def main():
    session = warm_session()
    rows = fetch(session)
    if rows is None:
        print("[fiidii] failed to fetch NSE FII/DII data")
        sys.exit(1)

    print(f"[fiidii] NSE returned {len(rows)} raw rows")
    if len(rows) == 0:
        print("[fiidii] empty payload - nothing to upsert")
        return

    normalized = normalize(rows)
    if not normalized:
        print("[fiidii] no usable rows after normalization. Sample raw:", rows[:2], file=sys.stderr)
        sys.exit(1)

    try:
        sb.table("fii_dii_flows").upsert(
            normalized, on_conflict="date,category,segment"
        ).execute()
        print(f"[fiidii] {datetime.now().isoformat()} · upserted {len(normalized)} rows")
        for r in normalized:
            print(f"  {r['date']} {r['category']:<8} {r['segment']:<8} "
                  f"buy={r['buy_value_cr']} sell={r['sell_value_cr']} net={r['net_value_cr']}")
    except Exception as e:
        print(f"[fiidii] upsert failed: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
