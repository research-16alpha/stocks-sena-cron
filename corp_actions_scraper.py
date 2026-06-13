"""
corp_actions_scraper.py
=======================
DAILY cron - scrapes NSE forthcoming + recent corporate actions
(dividends, splits, bonus, rights, etc.) and appends to corporate_actions.

Source: https://www.nseindia.com/api/corporates-corporateActions?index=equities
  - Public JSON, cookie warm-up required (same pattern as PCR / earnings)
  - Returns ~60 days backward + ~30 days forward window

Output: upsert on (symbol, ex_date, subject).
Powers: StockDetail "Corporate Actions" tab + watchlist "ex-div in 5d" badge.
"""

import os
import re
import sys
import time
from datetime import datetime

import requests
from supabase import create_client

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://tbeadvvkqyrhtendttrg.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
if not SUPABASE_KEY:
    try:
        with open(r"e:/Stocks sena/.supabase-service-key") as _f:
            SUPABASE_KEY = _f.read().strip()
    except FileNotFoundError:
        pass
sb = create_client(SUPABASE_URL, SUPABASE_KEY)

NSE_HOME = "https://www.nseindia.com"
NSE_CA_URL = "https://www.nseindia.com/api/corporates-corporateActions?index=equities"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-actions",
    "Connection": "keep-alive",
}

# Numeric extraction from subject text, e.g. "Dividend - Rs 12.50 Per Share"
RUPEES_RE = re.compile(r"(?:rs\.?|inr|₹)\s*([\d.]+)", re.I)


def warm_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    s.get(NSE_HOME, timeout=10)
    time.sleep(1)
    s.get("https://www.nseindia.com/companies-listing/corporate-filings-actions", timeout=10)
    time.sleep(1)
    return s


def fetch(session: requests.Session, retries: int = 3) -> list | None:
    for attempt in range(retries):
        try:
            r = session.get(NSE_CA_URL, timeout=30)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict) and "data" in data:
                    return data["data"]
                return data
            print(f"[corpact] NSE HTTP {r.status_code} attempt {attempt + 1}", file=sys.stderr)
        except Exception as e:
            print(f"[corpact] fetch error: {e}", file=sys.stderr)
        time.sleep(2 + attempt * 2)
    return None


def parse_date(s) -> str | None:
    if not s or s in ("-", ""):
        return None
    s = str(s).strip()
    if s == "-":
        return None
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def to_num(v):
    if v is None or v in ("-", ""):
        return None
    try:
        return float(str(v).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def normalize(rows: list) -> list[dict]:
    out = []
    seen: set[tuple] = set()
    for r in rows:
        if not isinstance(r, dict):
            continue
        symbol = (r.get("symbol") or r.get("Symbol") or "").strip()
        if not symbol:
            continue
        subject = (r.get("subject") or r.get("Subject") or "").strip()
        ex_date = parse_date(r.get("exDate") or r.get("ex_date") or r.get("Ex-Date"))
        if not ex_date:
            continue
        key = (symbol, ex_date, subject[:120])
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "symbol": symbol,
            "company": (r.get("comp") or r.get("company") or r.get("Company") or "").strip() or None,
            "subject": subject or "Corporate Action",
            "ex_date": ex_date,
            "record_date": parse_date(r.get("recDate") or r.get("Record-Date")),
            "face_value": to_num(r.get("faceVal") or r.get("Face-Value")),
            "isin": (r.get("isin") or r.get("ISIN") or "").strip() or None,
            "series": (r.get("series") or r.get("Series") or "").strip() or None,
            "bc_start": parse_date(r.get("bcStartDate") or r.get("BC-Start-Date")),
            "bc_end": parse_date(r.get("bcEndDate") or r.get("BC-End-Date")),
            "nd_start": parse_date(r.get("ndStartDate") or r.get("ND-Start-Date")),
            "nd_end": parse_date(r.get("ndEndDate") or r.get("ND-End-Date")),
            "broadcast_date": parse_date(r.get("broadcastDate") or r.get("Broadcast-Date")),
            "source": "NSE",
        })
    return out


def main():
    session = warm_session()
    rows = fetch(session)
    if rows is None:
        print("[corpact] failed to fetch NSE corporate actions")
        sys.exit(1)
    print(f"[corpact] NSE returned {len(rows)} raw rows")

    normalized = normalize(rows)
    if not normalized:
        print("[corpact] no usable rows after normalization", file=sys.stderr)
        return

    try:
        sb.table("corporate_actions").upsert(
            normalized, on_conflict="symbol,ex_date,subject"
        ).execute()
        # Count distinct subjects to surface what was scraped
        by_kind: dict[str, int] = {}
        for r in normalized:
            kind = r["subject"].split("-")[0].strip()[:30]
            by_kind[kind] = by_kind.get(kind, 0) + 1
        print(f"[corpact] {datetime.now().isoformat()} · upserted {len(normalized)} rows")
        for k, v in sorted(by_kind.items(), key=lambda kv: -kv[1])[:8]:
            print(f"  {k}: {v}")
    except Exception as e:
        print(f"[corpact] upsert failed: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
