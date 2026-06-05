"""
kite_daily_update.py
===================
Layer 4: the EVERYDAY refresh. Kite access tokens expire daily, so this is run
manually after pasting a fresh token into .kite-credentials (it can't be an
unattended GitHub cron). Kite is the ONLY source — no Yahoo / bhavcopy / BSE-API.

Does, for every stock with a kite_token:
  1. LTP   -> latest_price + price_change_pct        (batched, 500/call, ~seconds)
  2. daily -> fetch last ~12 sessions, MERGE the tail into daily/{sym}.json
              (extends history, no 20y re-download), recompute 52w / returns
  3. intraday -> rewrite intraday/{sym}.json with today's 1-minute bars

Run:  python kite_daily_update.py                 # full daily refresh
      python kite_daily_update.py --no-intraday   # skip the minute pass (faster)
      python kite_daily_update.py --prices-only    # just LTP (fastest)
"""
import argparse
import datetime
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from kiteconnect import KiteConnect
from supabase import create_client

from returns_calc import compute_returns  # shared date-based return/52w math

SVC = os.environ.get('SUPABASE_SERVICE_KEY') or open(r'e:/Stocks sena/.supabase-service-key').read().strip()
URL = os.environ.get('SUPABASE_URL', 'https://tbeadvvkqyrhtendttrg.supabase.co')
sb = create_client(URL, SVC)
STORE_H = {'apikey': SVC, 'Authorization': 'Bearer ' + SVC, 'x-upsert': 'true', 'Content-Type': 'application/json'}
PUB = f'{URL}/storage/v1/object/public'

def _kite_creds():
    """Daily Kite token lives in app_config (the single source the website + this
    cron share; the user updates it each morning with no redeploy). Fallbacks:
    env vars (KITE_API_KEY / KITE_ACCESS_TOKEN), then the local .kite-credentials."""
    ak = tk = None
    try:
        rows = sb.table('app_config').select('key,value').in_(
            'key', ['kite_api_key', 'kite_access_token']).execute().data or []
        cfg = {r['key']: (r.get('value') or '') for r in rows}
        ak, tk = cfg.get('kite_api_key') or None, cfg.get('kite_access_token') or None
    except Exception as e:
        print('  [warn] app_config read failed:', str(e)[:80], file=sys.stderr)
    ak = ak or os.environ.get('KITE_API_KEY')
    tk = tk or os.environ.get('KITE_ACCESS_TOKEN')
    if not (ak and tk):
        try:
            c = json.load(open(r'e:/Stocks sena/.kite-credentials'))
            ak = ak or c.get('api_key'); tk = tk or c.get('access_token')
        except Exception:
            pass
    if not (ak and tk):
        print('[FATAL] no Kite credentials found (app_config / env / file). '
              'Set the daily token in app_config.kite_access_token.', file=sys.stderr)
        sys.exit(1)
    return ak, tk


_AK, _TK = _kite_creds()
kite = KiteConnect(api_key=_AK)
kite.set_access_token(_TK)
_lock = threading.Lock(); _retries = {'n': 0}


def load_stocks():
    stocks, off = [], 0
    while True:
        d = sb.table('stock_master').select('symbol,kite_token,kite_tradingsymbol,kite_exchange').eq('is_active', True) \
            .not_.is_('kite_token', 'null').range(off, off + 999).execute().data or []
        stocks += d
        if len(d) < 1000:
            break
        off += 1000
    return stocks


def refresh_prices(stocks):
    """LTP -> latest_price + price_change_pct. Kite ids by exchange:tradingsymbol."""
    id_for = {f"{s['kite_exchange']}:{s['kite_tradingsymbol']}": s['symbol'] for s in stocks}
    ids = list(id_for)
    quotes = {}
    for i in range(0, len(ids), 500):
        for attempt in range(4):
            try:
                quotes.update(kite.quote(ids[i:i + 500])); break
            except Exception as e:
                if 'Too many' in str(e) or '429' in str(e):
                    time.sleep(0.6 * (attempt + 1)); continue
                print('  quote chunk err', str(e)[:50], file=sys.stderr); break
    n = 0
    for kid, q in quotes.items():
        sym = id_for.get(kid)
        lp = q.get('last_price')
        prev = (q.get('ohlc') or {}).get('close')
        if sym and lp and 0 < lp < 1e7:
            patch = {'latest_price': round(lp, 2)}
            if prev:
                patch['price_change_pct'] = round((lp / prev - 1) * 100, 2)
            try:
                sb.table('stock_master').update(patch).eq('symbol', sym).execute(); n += 1
            except Exception:
                pass
    print(f'[OK] prices: {n}/{len(stocks)} updated from Kite LTP', flush=True)


def merge_daily(stock):
    """Fetch last ~12 sessions, merge tail into bucket, recompute 52w/returns."""
    sym, tok, tsym = stock['symbol'], stock['kite_token'], stock.get('kite_tradingsymbol') or stock['symbol']
    to = datetime.date.today(); frm = to - datetime.timedelta(days=18)
    for attempt in range(4):
        try:
            raw = kite.historical_data(tok, frm, to, 'day'); break
        except Exception as e:
            if 'Too many' in str(e) or '429' in str(e):
                with _lock:
                    _retries['n'] += 1
                time.sleep(0.4 * (attempt + 1)); continue
            return (sym, 'err')
    else:
        return (sym, 'err')
    if not raw:
        return (sym, 'empty')
    new = [[b['date'].strftime('%Y-%m-%d'), round(b['open'], 4), round(b['high'], 4),
            round(b['low'], 4), round(b['close'], 4), int(b['volume'])] for b in raw]
    # Kite's historical 'day' bar for the CURRENT session is PARTIAL until close, so
    # writing it would freeze a mid-session volume into history (the 04-Jun-2026 bug,
    # where the after-close fix also failed on an expired token). During market hours
    # drop today's bar; the after-close run writes the complete one.
    _ist = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5, minutes=30)))
    if (_ist.hour * 60 + _ist.minute) < 935:  # before 15:35 IST (close + buffer)
        _today = _ist.date().isoformat()
        new = [b for b in new if b[0] < _today]
    if not new:
        return (sym, 'empty')
    try:
        cur = requests.get(f'{PUB}/daily/{sym}.json', headers=STORE_H, timeout=20)
        doc = cur.json() if cur.status_code == 200 else {'interval': '1d'}
        old = [b for b in doc.get('bars', []) if b[0] < new[0][0]]      # keep history before the new window
        bars = old + new
    except Exception:
        bars = new
    doc = {'interval': '1d', 'from': bars[0][0], 'to': bars[-1][0], 'bars': bars,
           '_ticker_used': f'KITE:{tsym}', 'symbol': sym}
    requests.put(f'{URL}/storage/v1/object/daily/{sym}.json', headers=STORE_H,
                 data=json.dumps(doc, separators=(',', ':')), timeout=30)
    # Date-based returns + 52w (shared math). latest_price stays whatever the LTP
    # path set — we take only the returns/52w keys here.
    m = compute_returns(bars) or {}
    d = {k: m.get(k) for k in ('high_52w', 'low_52w', 'return_1m_pct', 'return_3m_pct',
                               'return_6m_pct', 'return_1y_pct', 'return_5y_pct')}
    try:
        sb.table('stock_master').update(d).eq('symbol', sym).execute()
    except Exception:
        pass
    return (sym, 'ok')


def refresh_intraday(stock):
    sym, tok = stock['symbol'], stock['kite_token']
    to = datetime.datetime.now(); frm = to - datetime.timedelta(days=6)
    for attempt in range(4):
        try:
            raw = kite.historical_data(tok, frm, to, 'minute'); break
        except Exception as e:
            if 'Too many' in str(e) or '429' in str(e):
                time.sleep(0.4 * (attempt + 1)); continue
            return (sym, 'err')
    else:
        return (sym, 'err')
    if not raw:
        return (sym, 'empty')
    day = raw[-1]['date'].date()
    bars = [[b['date'].strftime('%H:%M'), round(b['close'], 4), int(b['volume'])] for b in raw if b['date'].date() == day]
    doc = {'symbol': sym, 'date': day.isoformat(), 'bars': bars}
    requests.put(f'{URL}/storage/v1/object/intraday/{sym}.json', headers=STORE_H,
                 data=json.dumps(doc, separators=(',', ':')), timeout=30)
    return (sym, 'ok')


def run_pool(label, fn, stocks, workers):
    t0 = time.time(); stat = {'ok': 0, 'empty': 0, 'err': 0}; n = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for sym, res in ex.map(fn, stocks):
            n += 1; stat[res if res in stat else 'err'] += 1
            if n % 400 == 0:
                print(f'  {label} {n}/{len(stocks)} · {stat} · {n/(time.time()-t0):.1f}/s', flush=True)
    print(f'[OK] {label}: {stat} · {time.time()-t0:.0f}s', flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--no-intraday', action='store_true')
    ap.add_argument('--prices-only', action='store_true')
    ap.add_argument('--workers', type=int, default=5)
    args = ap.parse_args()
    # Fail loud if the daily token is missing/expired so monitor.yml alerts and the
    # user knows to refresh app_config.kite_access_token (instead of silently writing 0).
    try:
        kite.quote(['NSE:INFY'])
    except Exception as e:
        print('[FATAL] Kite token invalid/expired — refresh app_config.kite_access_token. '
              'Detail:', str(e)[:140], file=sys.stderr)
        sys.exit(1)
    stocks = load_stocks()
    print(f'[INFO] daily update for {len(stocks)} stocks', flush=True)
    refresh_prices(stocks)
    if args.prices_only:
        return
    run_pool('daily-merge', merge_daily, stocks, args.workers)
    if not args.no_intraday:
        run_pool('intraday', refresh_intraday, stocks, args.workers)
    print('[DONE] kite daily update complete', flush=True)


if __name__ == '__main__':
    main()
