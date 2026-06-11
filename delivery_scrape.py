"""
delivery_scrape.py
==================
Daily delivery-% feed from NSE's security-wise full bhavcopy
(sec_bhavdata_full_DDMMYYYY.csv, published ~18:00 IST; DELIV_QTY / DELIV_PER cols).

Writes:
  1. delivery_data rows (symbol, date, traded_qty, delivered_qty, delivery_pct)
     for EQ/BE/BZ series symbols that exist in stock_master (NSE-listed only —
     BSE-only names have no NSE delivery data; their stat just stays empty).
  2. stock_master.delivery_pct (latest session) + delivery_pct_avg (20-session mean)
     so the screener can sort/filter without a join.

Run:  py -3.11 delivery_scrape.py            # today (or last trading day)
      py -3.11 delivery_scrape.py --days 30  # backfill last N calendar days
"""
import argparse
import datetime as dt
import io
import json
import os
import sys
import time

import requests

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

URL = os.environ.get('SUPABASE_URL', 'https://tbeadvvkqyrhtendttrg.supabase.co')
SVC = os.environ.get('SUPABASE_SERVICE_KEY') or open(r'e:/Stocks sena/.supabase-service-key').read().strip()
H = {'apikey': SVC, 'Authorization': 'Bearer ' + SVC, 'Content-Type': 'application/json'}
NSE_H = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124', 'Referer': 'https://www.nseindia.com/'}
SERIES = {'EQ', 'BE', 'BZ', 'SM', 'ST'}  # SM/ST = NSE Emerge SME


def fetch_day(d):
    """[(symbol, date_iso, traded, delivered, pct)] or None if no file (holiday)."""
    u = f"https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{d.strftime('%d%m%Y')}.csv"
    r = requests.get(u, headers=NSE_H, timeout=60)
    if r.status_code != 200 or len(r.content) < 1000:
        return None
    out = []
    rd = io.StringIO(r.text)
    header = [c.strip() for c in rd.readline().split(',')]
    idx = {c: i for i, c in enumerate(header)}
    need = ['SYMBOL', 'SERIES', 'TTL_TRD_QNTY', 'DELIV_QTY', 'DELIV_PER']
    if any(c not in idx for c in need):
        print(f'  [warn] unexpected header for {d}: {header[:6]}')
        return None
    for line in rd:
        c = [x.strip() for x in line.split(',')]
        if len(c) < len(header) or c[idx['SERIES']] not in SERIES:
            continue
        try:
            traded = int(c[idx['TTL_TRD_QNTY']])
            dlv = c[idx['DELIV_QTY']]
            per = c[idx['DELIV_PER']]
            if dlv in ('-', '') or per in ('-', ''):
                continue
            out.append((c[idx['SYMBOL']], d.isoformat(), traded, int(dlv), float(per)))
        except Exception:
            continue
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=4)  # small lookback heals missed runs
    args = ap.parse_args()

    known = set()
    off = 0
    while True:
        d = requests.get(f'{URL}/rest/v1/stock_master?select=symbol&is_active=eq.true&offset={off}&limit=1000',
                         headers=H, timeout=30).json()
        known.update(x['symbol'] for x in d)
        if len(d) < 1000:
            break
        off += 1000
    print(f'[deliv] {len(known)} active symbols')

    today = dt.date.today()
    total = 0
    for back in range(args.days, -1, -1):
        d = today - dt.timedelta(days=back)
        if d.weekday() >= 5:
            continue
        rows = fetch_day(d)
        if rows is None:
            print(f'  {d} no file (holiday/not yet published)')
            continue
        payload = [{'symbol': s, 'date': dt_, 'traded_qty': t, 'delivered_qty': q, 'delivery_pct': p}
                   for s, dt_, t, q, p in rows if s in known]
        for i in range(0, len(payload), 2000):
            r = requests.post(f'{URL}/rest/v1/delivery_data?on_conflict=symbol,date',
                              headers={**H, 'Prefer': 'resolution=merge-duplicates,return=minimal'},
                              data=json.dumps(payload[i:i + 2000]), timeout=60)
            if r.status_code not in (200, 201, 204):
                print(f'  [err] upsert {d}: {r.status_code} {r.text[:120]}')
                break
        total += len(payload)
        print(f'  {d}: {len(payload)} rows')
        time.sleep(0.5)

    # latest + 20-session average -> stock_master (screener reads these directly)
    since = (today - dt.timedelta(days=45)).isoformat()
    print('[deliv] computing latest + 20d averages...')
    agg = {}
    off = 0
    while True:
        # NOTE: PostgREST caps each response at 1000 rows regardless of a larger
        # limit param — page by 1000 or the loop exits after the first page.
        d = requests.get(f'{URL}/rest/v1/delivery_data?select=symbol,date,delivery_pct&date=gte.{since}'
                         f'&order=symbol,date&offset={off}&limit=1000', headers=H, timeout=60).json()
        for x in d:
            agg.setdefault(x['symbol'], []).append((x['date'], x['delivery_pct']))
        if len(d) < 1000:
            break
        off += 1000
    n = 0
    for sym, arr in agg.items():
        arr.sort()
        last20 = [p for _, p in arr[-20:] if p is not None]
        if not last20:
            continue
        patch = {'delivery_pct': arr[-1][1], 'delivery_pct_avg': round(sum(last20) / len(last20), 2)}
        r = requests.patch(f'{URL}/rest/v1/stock_master?symbol=eq.{sym}',
                           headers={**H, 'Prefer': 'return=minimal'}, data=json.dumps(patch), timeout=30)
        if r.status_code in (200, 204):
            n += 1
    print(f'[deliv] done: {total} day-rows upserted, {n} stock_master rows patched')


if __name__ == '__main__':
    main()
