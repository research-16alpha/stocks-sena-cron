"""
capture_deals_today.py
=====================
DAILY going-forward capture of bulk + block deals from NSE's large-deals snapshot
(`/api/snapshot-capital-market-largedeal`) — the ONE deals endpoint that is NOT
Akamai-blocked. NSE's *historical* deals API is blocked, so this is the only way
to build deal history: run it daily after close, it stores that day's deals.

Both bulk and block go into `bulk_deals` (record_type distinguishes them), upserted
on (date, symbol, client_name, buy_sell) so re-runs are idempotent.

Run daily ~6 PM IST:  py capture_deals_today.py
"""
import os, sys, json
import requests
from datetime import date, datetime

URL = os.environ.get("SUPABASE_URL", "https://tbeadvvkqyrhtendttrg.supabase.co").rstrip("/")
KEY = os.environ.get("SUPABASE_SERVICE_KEY") or open(r"e:/Stocks sena/.supabase-service-key").read().strip()
SB_H = {"apikey": KEY, "Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36")
SNAP = "https://www.nseindia.com/api/snapshot-capital-market-largedeal"
REF = "https://www.nseindia.com/market-data/large-deals"


def num(v):
    if v in (None, "-", ""):
        return None
    try:
        return float(str(v).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def pdate(s):
    if not s:
        return date.today().isoformat()
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(str(s).strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return date.today().isoformat()


def norm(rows, rt):
    out = []
    for r in rows or []:
        sym = (r.get("symbol") or "").strip()
        bs = (r.get("buySell") or "").strip().upper()
        bs = "BUY" if bs.startswith("B") else "SELL" if bs.startswith("S") else None
        if not sym or not bs:
            continue
        qty = num(r.get("qty"))
        price = num(r.get("watp"))
        out.append({
            "date": pdate(r.get("date")), "symbol": sym, "security_name": (r.get("name") or "").strip() or None,
            "client_name": (r.get("clientName") or "").strip() or None, "buy_sell": bs, "record_type": rt,
            "quantity": int(qty) if qty else None, "price": price,
            "trade_value_cr": round(qty * price / 1e7, 2) if (qty and price) else None,
            "remarks": (r.get("remarks") or "").strip() or None,
        })
    return out


def main():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9", "Accept-Encoding": "gzip, deflate"})
    s.get("https://www.nseindia.com/", timeout=15); s.get(REF, timeout=12)
    r = s.get(SNAP, headers={"Referer": REF, "X-Requested-With": "XMLHttpRequest", "Accept": "application/json, */*"}, timeout=25)
    if r.status_code != 200 or "json" not in r.headers.get("content-type", ""):
        print(f"[deals] snapshot failed {r.status_code}", file=sys.stderr); sys.exit(1)
    j = r.json()
    rows = norm(j.get("BULK_DEALS_DATA"), "bulk") + norm(j.get("BLOCK_DEALS_DATA"), "block")
    # dedup on the upsert key
    by = {}
    for x in rows:
        by[(x["date"], x["symbol"], x.get("client_name") or "", x["buy_sell"])] = x
    rows = list(by.values())
    print(f"[deals] today: bulk={len(j.get('BULK_DEALS_DATA') or [])} block={len(j.get('BLOCK_DEALS_DATA') or [])} -> {len(rows)} rows")
    if not rows:
        return
    resp = requests.post(f"{URL}/rest/v1/bulk_deals?on_conflict=date,symbol,client_name,buy_sell",
                         headers={**SB_H, "Prefer": "resolution=merge-duplicates,return=minimal"},
                         data=json.dumps(rows), timeout=60)
    print(f"[deals] upsert status={resp.status_code} {resp.text[:140] if resp.status_code not in (200,201,204) else 'OK'}")


if __name__ == "__main__":
    main()
