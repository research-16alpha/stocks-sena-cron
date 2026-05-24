"""
bulk_deals_scraper.py
=====================
DAILY cron - scrapes NSE block + bulk deals (last ~7 days).
Runs at 6 PM IST after NSE publishes EOD reports.

Source endpoints (public JSON, cookie warm-up):
  - https://www.nseindia.com/api/historical/equities/bulk-deals?from=...&to=...
  - https://www.nseindia.com/api/historical/equities/block-deals?from=...&to=...

Output: upsert to public.bulk_deals on (date, symbol, client_name, buy_sell).
Powers: Bulk deals widget + StockDetail "Big trades" section.
"""

import os
import sys
import time
from datetime import datetime, timedelta

import requests
from supabase import create_client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
sb = create_client(SUPABASE_URL, SUPABASE_KEY)

NSE_HOME = "https://www.nseindia.com"
BULK_URL = "https://www.nseindia.com/api/historical/equities/bulk-deals"
BLOCK_URL = "https://www.nseindia.com/api/historical/equities/block-deals"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/report-detail/display-bulk-and-block-deals",
    "Connection": "keep-alive",
}


def warm_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    s.get(NSE_HOME, timeout=10)
    time.sleep(1)
    s.get("https://www.nseindia.com/report-detail/display-bulk-and-block-deals", timeout=10)
    time.sleep(1)
    return s


def fetch_deals(session: requests.Session, url: str, label: str, retries: int = 3) -> list:
    # Last 7 days window (NSE format: DD-MM-YYYY)
    today = datetime.now()
    frm = (today - timedelta(days=7)).strftime("%d-%m-%Y")
    to = today.strftime("%d-%m-%Y")
    params = {"from": frm, "to": to}

    for attempt in range(retries):
        try:
            r = session.get(url, params=params, timeout=30)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict) and "data" in data:
                    return data["data"]
                return data or []
            print(f"[{label}] NSE HTTP {r.status_code} attempt {attempt + 1}", file=sys.stderr)
        except Exception as e:
            print(f"[{label}] fetch error: {e}", file=sys.stderr)
        time.sleep(2 + attempt * 2)
    return []


def parse_date(s) -> str | None:
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
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


def to_int(v):
    n = to_num(v)
    return int(n) if n is not None else None


def normalize(rows: list, record_type: str) -> list[dict]:
    out = []
    seen: set[tuple] = set()
    for r in rows:
        if not isinstance(r, dict):
            continue
        symbol = (r.get("symbol") or r.get("BD_SYMBOL") or "").strip()
        if not symbol:
            continue
        date_iso = parse_date(
            r.get("date")
            or r.get("BD_DT_DATE")
            or r.get("BD_TIMESTAMP")
        )
        if not date_iso:
            continue
        client = (
            r.get("clientName")
            or r.get("BD_CLIENT_NAME")
            or ""
        ).strip() or None
        bs_raw = (
            r.get("buySell")
            or r.get("BD_BUY_SELL")
            or ""
        ).strip().upper()
        if bs_raw.startswith("B"):
            bs = "BUY"
        elif bs_raw.startswith("S"):
            bs = "SELL"
        else:
            continue
        qty = to_int(r.get("quantityTraded") or r.get("BD_QTY_TRD"))
        price = to_num(r.get("watp") or r.get("BD_TP_WATP") or r.get("price") or r.get("BD_TP"))
        trade_value_cr = round(qty * price / 1e7, 2) if (qty and price) else None

        key = (date_iso, symbol, client or "", bs)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "date": date_iso,
            "symbol": symbol,
            "security_name": (r.get("securityName") or r.get("BD_SCRIP_NAME") or "").strip() or None,
            "client_name": client,
            "buy_sell": bs,
            "record_type": record_type,
            "quantity": qty,
            "price": price,
            "trade_value_cr": trade_value_cr,
        })
    return out


def main():
    session = warm_session()

    bulk = fetch_deals(session, BULK_URL, "bulk")
    block = fetch_deals(session, BLOCK_URL, "block")
    print(f"[deals] NSE returned bulk={len(bulk)} block={len(block)}")

    normalized = normalize(bulk, "bulk") + normalize(block, "block")
    if not normalized:
        print("[deals] nothing to upsert")
        return

    try:
        sb.table("bulk_deals").upsert(
            normalized, on_conflict="date,symbol,client_name,buy_sell"
        ).execute()
        print(f"[deals] {datetime.now().isoformat()} · upserted {len(normalized)} deals")
    except Exception as e:
        print(f"[deals] upsert failed: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
