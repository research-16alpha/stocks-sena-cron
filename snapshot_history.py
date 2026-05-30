"""
snapshot_history.py  (H11)
==========================
Point-in-time store. Each run appends ONE dated row per active stock to
`stock_metrics_history`, capturing the metrics that otherwise get overwritten in
place on stock_master. This is the foundation for any "as-of" / backtest / screen-
as-of-date feature, and is the only way to avoid survivorship + look-ahead bias
(stock_master alone only ever knows the present).

Runs daily AFTER the snapshot sync + compute_metrics, so the row reflects that
day's closing prices and freshly-computed ratios. Idempotent on PK
(symbol, as_of_date) — re-running the same day overwrites that day's row, never
duplicates.

USAGE
  py -3.11 snapshot_history.py
  py -3.11 snapshot_history.py --dry-run
"""
import argparse
import datetime as dt
import json
import os
import sys

import requests

URL = os.environ.get('SUPABASE_URL', 'https://tbeadvvkqyrhtendttrg.supabase.co')
KEY = os.environ.get('SUPABASE_SERVICE_KEY')
if not KEY:
    try:
        with open('e:/Stocks sena/.supabase-service-key') as f:
            KEY = f.read().strip()
    except FileNotFoundError:
        pass
H = {'apikey': KEY, 'Authorization': f'Bearer {KEY}'}

COLS = ['symbol', 'latest_price', 'market_cap_cr', 'pe_ratio', 'pb_ratio',
        'roe_pct', 'roce_pct', 'debt_equity', 'promoter_pct', 'piotroski_score',
        'latest_quarter_period']


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()
    if not KEY:
        print('[ERR] no Supabase key', file=sys.stderr)
        sys.exit(2)

    today = dt.date.today().isoformat()
    rows = []
    off = 0
    while True:
        r = requests.get(f'{URL}/rest/v1/stock_master?select={",".join(COLS)}'
                         f'&is_active=eq.true&limit=1000&offset={off}', headers=H, timeout=30)
        b = r.json()
        if not b:
            break
        rows += b
        off += 1000
        if len(b) < 1000:
            break
    print(f'[history] {len(rows)} active stocks -> stock_metrics_history @ {today}')

    hist = []
    for r in rows:
        rec = {k: r.get(k) for k in COLS}
        rec['as_of_date'] = today
        hist.append(rec)

    if args.dry_run:
        print(f'[dry-run] would upsert {len(hist)} rows (sample: {json.dumps(hist[0])[:200] if hist else "none"})')
        return

    written = 0
    for i in range(0, len(hist), 500):
        chunk = hist[i:i + 500]
        rr = requests.post(f'{URL}/rest/v1/stock_metrics_history',
                           headers={**H, 'Content-Type': 'application/json',
                                    'Prefer': 'resolution=merge-duplicates,return=minimal'},
                           data=json.dumps(chunk, default=str), timeout=40)
        if rr.status_code in (200, 201, 204):
            written += len(chunk)
        else:
            print(f'[ERR] chunk {i}: HTTP {rr.status_code} {rr.text[:150]}', file=sys.stderr)
            sys.exit(1)
    print(f'[done] wrote {written} point-in-time rows for {today}')


if __name__ == '__main__':
    main()
