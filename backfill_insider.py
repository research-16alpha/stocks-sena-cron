"""
backfill_insider.py
===================
Backfill insider trades (SEBI PIT disclosures) from NSE into public.insider_trades.

Source: NSE corporates-pit API (whole market, by date window) — confirmed accessible
locally with a requests.Session cookie dance (Akamai). Iterates ~90-day windows over
YEARS years, modest parallelism (NSE blocks aggressive scraping).

Run:
    py backfill_insider.py --dry        # fetch + map + print, NO writes
    py backfill_insider.py --years 3    # full backfill (truncate+insert into empty table)
"""
import os, sys, json, time, argparse
import requests
from datetime import date, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

URL = os.environ.get("SUPABASE_URL", "https://tbeadvvkqyrhtendttrg.supabase.co").rstrip("/")
KEY = os.environ.get("SUPABASE_SERVICE_KEY") or open(r"e:/Stocks sena/.supabase-service-key").read().strip()
SB_H = {"apikey": KEY, "Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36")
PIT_PAGE = "https://www.nseindia.com/companies-listing/corporate-filings-insider-trading"
PIT_API = "https://www.nseindia.com/api/corporates-pit?index=equities&from_date={f}&to_date={t}"


def new_session():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
                      "Accept-Encoding": "gzip, deflate"})  # no brotli (requests can't decode)
    try:
        s.get("https://www.nseindia.com/", timeout=15)
        s.get(PIT_PAGE, timeout=15)
    except Exception:
        pass
    return s


def fetch_window(s, f, t, tries=4):
    url = PIT_API.format(f=f, t=t)
    h = {"Referer": PIT_PAGE, "Accept": "application/json, */*", "X-Requested-With": "XMLHttpRequest"}
    for i in range(tries):
        try:
            r = s.get(url, headers=h, timeout=30)
            if r.status_code == 200 and "json" in r.headers.get("content-type", ""):
                return r.json().get("data") or []
            # 503 / challenge -> re-warm and retry
            s.get(PIT_PAGE, timeout=15)
            time.sleep(1.5 * (i + 1))
        except Exception:
            time.sleep(1.5 * (i + 1))
    return None


def num(v):
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def pdate(s):
    if not s or s == "-":
        return None
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            from datetime import datetime
            return datetime.strptime(str(s).strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def map_row(r):
    sym = (r.get("symbol") or "").strip()
    if not sym:
        return None
    buy_q, buy_v = num(r.get("buyQuantity")), num(r.get("buyValue"))
    sell_q, sell_v = num(r.get("sellquantity")), num(r.get("sellValue"))
    sec_acq, sec_val = num(r.get("secAcq")), num(r.get("secVal"))
    bef, aft = num(r.get("befAcqSharesNo")), num(r.get("afterAcqSharesNo"))
    # trade_type must be lowercase 'buy'/'sell' (DB CHECK constraint).
    if buy_q and buy_q > 0:
        tt, qty, val = "buy", buy_q, (buy_v or sec_val)
    elif sell_q and sell_q > 0:
        tt, qty, val = "sell", sell_q, (sell_v or sec_val)
    elif bef is not None and aft is not None and aft != bef:
        tt = "buy" if aft > bef else "sell"
        qty, val = (abs(sec_acq) if sec_acq else abs(aft - bef)), sec_val
    else:
        return None  # no directional change (pledge/revoke) -> not a buy/sell trade
    value_cr = round(val / 1e7, 2) if val else None
    price = round(val / qty, 2) if (val and qty) else None
    td = pdate(r.get("acqfromDt")) or pdate(r.get("acqtoDt")) or pdate(r.get("date"))
    fd = pdate(r.get("intimDt")) or pdate(r.get("date")) or td  # filed_date is NOT NULL
    return {
        "symbol": sym,
        "insider_name": (r.get("acqName") or "").strip() or None,
        "insider_role": (r.get("personCategory") or "").strip() or None,
        "trade_type": tt,
        "quantity": int(qty) if qty else None,
        "avg_price": price,
        "value_cr": value_cr,
        "trade_date": td,
        "filed_date": fd,
        "filing_url": None,
        "raw_data": {k: r.get(k) for k in ("acqMode", "tdpTransactionType", "secType",
                                           "befAcqSharesNo", "afterAcqSharesNo", "secAcq", "anex", "personCategory")},
    }


def windows(years):
    out = []
    end = date.today()
    start = end - timedelta(days=int(365.25 * years))
    cur = start
    while cur < end:
        nxt = min(cur + timedelta(days=90), end)
        out.append((cur.strftime("%d-%m-%Y"), nxt.strftime("%d-%m-%Y")))
        cur = nxt + timedelta(days=1)
    return out


def post_rows(rows):
    ok = 0
    for i in range(0, len(rows), 500):
        chunk = rows[i:i + 500]
        r = requests.post(f"{URL}/rest/v1/insider_trades", headers={**SB_H, "Prefer": "return=minimal"},
                          data=json.dumps(chunk), timeout=60)
        if r.status_code in (200, 201, 204):
            ok += len(chunk)
        else:
            print(f"[WARN] insert {r.status_code}: {r.text[:200]}")
            sys.stdout.flush()
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=float, default=3)
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--workers", type=int, default=3)
    a = ap.parse_args()

    wins = windows(a.years)
    print(f"[insider] {len(wins)} windows over {a.years}y, {a.workers} workers")
    s = new_session()

    raw = []
    def work(w):
        return fetch_window(s, w[0], w[1])
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(work, w): w for w in wins}
        for fut in as_completed(futs):
            w = futs[fut]
            d = fut.result()
            print(f"  {w[0]}..{w[1]} -> {('FAIL' if d is None else len(d))}")
            if d:
                raw.extend(d)

    # dedup + map
    seen, rows = set(), []
    for r in raw:
        m = map_row(r)
        if not m or not m["trade_date"]:
            continue
        k = (m["symbol"], m["insider_name"], m["trade_date"], m["quantity"], m["trade_type"])
        if k in seen:
            continue
        seen.add(k)
        rows.append(m)
    print(f"[insider] raw={len(raw)} -> mapped/deduped={len(rows)}")

    if a.dry:
        print("=== sample (3) ===")
        for m in rows[:3]:
            print(json.dumps({k: v for k, v in m.items() if k != 'raw_data'}, ensure_ascii=True))
        print("=== top symbols ===")
        from collections import Counter
        for sym, c in Counter(r["symbol"] for r in rows).most_common(8):
            print(f"  {sym}: {c}")
        return

    # full: truncate (empty table) then insert
    requests.delete(f"{URL}/rest/v1/insider_trades?id=not.is.null", headers=SB_H, timeout=60)
    n = post_rows(rows)
    print(f"[insider] DONE inserted={n}")


if __name__ == "__main__":
    main()
