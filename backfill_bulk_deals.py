"""
backfill_bulk_deals.py
=====================
Historical backfill for bulk_deals (the daily scraper only fetches TODAY's CSV, so
the table had ~4 sporadic days). Uses NSE's historical bulk/block deals API over a
date range and upserts on the same conflict key as the daily cron — so re-running
is duplicate-safe and complements (doesn't fight) the daily scraper.

Source: nseindia.com/api/historicalOR/bulk-block-short-deals?optionType={bulk_deals|block_deals}&from=DD-MM-YYYY&to=DD-MM-YYYY
Conflict key: (date, symbol, client_name, buy_sell).

USAGE
  py -3.11 backfill_bulk_deals.py            # last 180 days
  py -3.11 backfill_bulk_deals.py --days 365
"""
import argparse
import datetime as dt
import json
import os
import sys
import time

import requests

URL = os.environ.get('SUPABASE_URL', 'https://tbeadvvkqyrhtendttrg.supabase.co')
KEY = os.environ.get('SUPABASE_SERVICE_KEY') or open('e:/Stocks sena/.supabase-service-key').read().strip()
H = {'apikey': KEY, 'Authorization': f'Bearer {KEY}'}
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36')
API = 'https://www.nseindia.com/api/historicalOR/bulk-block-short-deals'
_MON = {'JAN': '01', 'FEB': '02', 'MAR': '03', 'APR': '04', 'MAY': '05', 'JUN': '06',
        'JUL': '07', 'AUG': '08', 'SEP': '09', 'OCT': '10', 'NOV': '11', 'DEC': '12'}


def nse_session():
    s = requests.Session()
    s.headers.update({'User-Agent': UA, 'Accept': 'application/json',
                      'Referer': 'https://www.nseindia.com/', 'Accept-Language': 'en-US,en;q=0.9'})
    try:
        s.get('https://www.nseindia.com/', timeout=20)
    except Exception:
        pass
    return s


def to_iso(d):
    # "02-MAR-2026" -> "2026-03-02"
    try:
        day, mon, yr = d.split('-')
        return f'{yr}-{_MON[mon.upper()]}-{int(day):02d}'
    except Exception:
        return None


def fetch(s, opt, frm, to):
    try:
        r = s.get(API, params={'optionType': opt, 'from': frm, 'to': to}, timeout=30)
        if r.status_code != 200:
            return []
        return (r.json() or {}).get('data') or []
    except Exception:
        return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=180)
    args = ap.parse_args()

    s = nse_session()
    end = dt.date.today()
    start = end - dt.timedelta(days=args.days)
    rows = {}
    # 30-day chunks (NSE caps range length)
    cur = start
    while cur < end:
        chunk_end = min(cur + dt.timedelta(days=30), end)
        frm = cur.strftime('%d-%m-%Y')
        to = chunk_end.strftime('%d-%m-%Y')
        for opt, deal_type in (('bulk_deals', 'bulk'), ('block_deals', 'block')):
            data = fetch(s, opt, frm, to)
            for r in data:
                sym = (r.get('BD_SYMBOL') or '').strip()
                d = to_iso(r.get('BD_DT_DATE') or '')
                if not sym or not d:
                    continue
                client = (r.get('BD_CLIENT_NAME') or '').strip()
                bs = (r.get('BD_BUY_SELL') or '').strip().upper()
                qty = r.get('BD_QTY_TRD')
                price = r.get('BD_TP_WATP')
                try:
                    qty = int(qty) if qty is not None else None
                    price = float(price) if price is not None else None
                except (ValueError, TypeError):
                    qty = price = None
                tv = round(qty * price / 1e7, 2) if (qty and price) else None
                key = (d, sym, client, bs)
                rows[key] = {
                    'date': d, 'symbol': sym, 'record_type': deal_type,
                    'security_name': (r.get('BD_SCRIP_NAME') or '').strip() or None,
                    'client_name': client, 'buy_sell': bs,
                    'quantity': qty, 'price': price, 'trade_value_cr': tv,
                    'remarks': (r.get('BD_REMARKS') or '').strip() or None,
                }
            time.sleep(0.4)
        print(f'  {frm}..{to}: cumulative {len(rows)} deals')
        cur = chunk_end + dt.timedelta(days=1)

    deals = list(rows.values())
    print(f'[INFO] {len(deals)} unique historical deals to upsert')
    if not deals:
        print('[WARN] nothing fetched'); return

    written = 0
    for i in range(0, len(deals), 500):
        chunk = deals[i:i + 500]
        r = requests.post(f'{URL}/rest/v1/bulk_deals?on_conflict=date,symbol,client_name,buy_sell',
                          headers={**H, 'Content-Type': 'application/json',
                                   'Prefer': 'resolution=merge-duplicates,return=minimal'},
                          data=json.dumps(chunk, default=str), timeout=40)
        if r.status_code in (200, 201, 204):
            written += len(chunk)
        else:
            print(f'[ERR] chunk {i}: {r.status_code} {r.text[:150]}', file=sys.stderr)
    print(f'[done] upserted {written} historical bulk/block deals')


if __name__ == '__main__':
    main()
