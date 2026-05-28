"""
bulk_deals_scraper.py
=====================
DAILY cron - fetches NSE bulk + block deals from NSE archives.
Runs at 6 PM IST after NSE publishes EOD reports.

Sources (static CSVs, no bot protection):
  - https://nsearchives.nseindia.com/content/equities/bulk.csv   (today's bulk)
  - https://nsearchives.nseindia.com/content/equities/block.csv  (today's block)

Was previously using NSE's API endpoints which got blocked by Akamai WAF
(403 from GitHub Actions IPs). The archive CSVs are static files served from
NSE's CDN — no rate limiting, no cookies needed.

Output: upsert to public.bulk_deals on (date, symbol, client_name, buy_sell).
Powers: Bulk deals widget + StockDetail "Big trades" section.
"""

import csv
import io
import os
import sys
from datetime import datetime

import requests
from supabase import create_client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
sb = create_client(SUPABASE_URL, SUPABASE_KEY)

BULK_URL = "https://nsearchives.nseindia.com/content/equities/bulk.csv"
BLOCK_URL = "https://nsearchives.nseindia.com/content/equities/block.csv"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}


def fetch_csv(url: str, label: str) -> list[dict]:
    """Fetch and parse the NSE archive CSV. Returns list of dicts keyed by CSV header."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        if r.status_code != 200:
            print(f"[{label}] HTTP {r.status_code}", file=sys.stderr)
            return []
        text = r.text
        # First line is header; NO RECORDS sentinel possible
        if "NO RECORDS" in text:
            return []
        reader = csv.DictReader(io.StringIO(text))
        return list(reader)
    except Exception as e:
        print(f"[{label}] fetch error: {e}", file=sys.stderr)
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


def normalize(rows: list[dict], record_type: str) -> list[dict]:
    """NSE archive CSV columns:
       Date,Symbol,Security Name,Client Name,Buy/Sell,
       Quantity Traded,Trade Price / Wght. Avg. Price,Remarks
    """
    out = []
    seen: set[tuple] = set()
    for r in rows:
        if not isinstance(r, dict):
            continue
        symbol = (r.get("Symbol") or "").strip()
        if not symbol or symbol == "NO RECORDS":
            continue
        date_iso = parse_date(r.get("Date"))
        if not date_iso:
            continue
        client = (r.get("Client Name") or "").strip() or None
        bs_raw = (r.get("Buy/Sell") or "").strip().upper()
        if bs_raw.startswith("B"):
            bs = "BUY"
        elif bs_raw.startswith("S"):
            bs = "SELL"
        else:
            continue
        qty = to_int(r.get("Quantity Traded"))
        price_field = r.get("Trade Price / Wght. Avg. Price") or r.get("Trade Price/Wght. Avg. Price")
        price = to_num(price_field)
        trade_value_cr = round(qty * price / 1e7, 2) if (qty and price) else None

        key = (date_iso, symbol, client or "", bs)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "date": date_iso,
            "symbol": symbol,
            "security_name": (r.get("Security Name") or "").strip() or None,
            "client_name": client,
            "buy_sell": bs,
            "record_type": record_type,
            "quantity": qty,
            "price": price,
            "trade_value_cr": trade_value_cr,
        })
    return out


def main():
    bulk = fetch_csv(BULK_URL, "bulk")
    block = fetch_csv(BLOCK_URL, "block")
    print(f"[deals] NSE archive returned bulk={len(bulk)} block={len(block)}")

    normalized = normalize(bulk, "bulk") + normalize(block, "block")
    if not normalized:
        print("[deals] nothing to upsert")
        return

    # Final dedup on the EXACT on_conflict key across both lists. normalize()
    # dedups bulk and block separately, so a row present in both (or NULL-client
    # rows) can still collide within one upsert command -> Postgres 21000
    # "ON CONFLICT DO UPDATE command cannot affect row a second time".
    by_key: dict[tuple, dict] = {}
    for row in normalized:
        k = (row["date"], row["symbol"], row.get("client_name") or "", row["buy_sell"])
        by_key[k] = row  # last write wins
    deduped = list(by_key.values())
    if len(deduped) != len(normalized):
        print(f"[deals] deduped {len(normalized)} -> {len(deduped)} on conflict key")

    try:
        sb.table("bulk_deals").upsert(
            deduped, on_conflict="date,symbol,client_name,buy_sell"
        ).execute()
        print(f"[deals] {datetime.now().isoformat()} · upserted {len(deduped)} deals")
    except Exception as e:
        print(f"[deals] upsert failed: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
