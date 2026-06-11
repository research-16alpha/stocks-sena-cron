"""
global_cues.py
==============
Global market cues for the market-mood page: US indices + futures, Brent, USD/INR,
US 10Y, Asia. Yahoo chart API (v8, no crumb needed) — acceptable here because these
are non-Indian context quotes with no Kite alternative; source is labelled in the UI.
GIFT Nifty intentionally absent: no reliable free source (NSE IX has no public API).

Writes storage daily/global_cues.json:
  {fetched_at, cues: [{key,label,price,prev,chg_pct,unit}]}
Cron: every ~30 min on weekdays (plus pre-market hours for overnight context).
"""
import datetime
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
STORE_H = {'apikey': SVC, 'Authorization': 'Bearer ' + SVC, 'x-upsert': 'true', 'Content-Type': 'application/json'}
H = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124'}

TICKERS = [
    ('dow',     '^DJI',  'Dow Jones',     'pts'),
    ('nasdaq',  '^IXIC', 'Nasdaq',        'pts'),
    ('sp500',   '^GSPC', 'S&P 500',       'pts'),
    ('spfut',   'ES=F',  'S&P futures',   'pts'),
    ('brent',   'BZ=F',  'Brent crude',   '$/bbl'),
    ('usdinr',  'INR=X', 'USD / INR',     '₹'),
    ('us10y',   '^TNX',  'US 10Y yield',  '%'),
    ('nikkei',  '^N225', 'Nikkei 225',    'pts'),
    ('hangseng', '^HSI', 'Hang Seng',     'pts'),
]


def fetch(symbol):
    d = requests.get(f'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}',
                     params={'range': '1d', 'interval': '1d'}, headers=H, timeout=20).json()
    m = (d.get('chart', {}).get('result') or [{}])[0].get('meta', {})
    price, prev = m.get('regularMarketPrice'), m.get('chartPreviousClose')
    if price is None:
        return None
    return price, prev


def main():
    cues = []
    for key, sym, label, unit in TICKERS:
        try:
            r = fetch(sym)
        except Exception as e:
            print(f'  {key}: ERR {str(e)[:50]}')
            r = None
        if not r:
            continue  # feed missing -> card simply doesn't render (no fake data)
        price, prev = r
        cues.append({'key': key, 'label': label, 'price': round(price, 2),
                     'prev': round(prev, 2) if prev else None,
                     'chg_pct': round((price / prev - 1) * 100, 2) if prev else None,
                     'unit': unit})
        time.sleep(0.4)
    if len(cues) < 4:
        print(f'[cues] only {len(cues)} fetched — NOT overwriting store (Yahoo likely down)')
        return
    doc = {'fetched_at': datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'), 'cues': cues}
    r = requests.put(f'{URL}/storage/v1/object/daily/global_cues.json', headers=STORE_H,
                     data=json.dumps(doc, separators=(',', ':')), timeout=30)
    print(f'[cues] wrote {len(cues)} cues (HTTP {r.status_code})')


if __name__ == '__main__':
    main()
