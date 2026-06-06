"""
eod_price_settle.py
===================
End-of-day price finalization. After the close, the live movers loop's last tick is
a mid-session snapshot (e.g. ~15:34), not the day's settled close, so volatile movers
can be off by a percent or two. This runs ONCE post-close and overwrites, for every
active ticker with a kite_token, the *absolute* close values from one batched Kite
quote:
  latest_price, price_change_pct, traded_value_cr
It deliberately does NOT touch the RVOL ratios (rvol_1d/1w/1m): those need the live
loop's trading-day baseline and would be wrong if recomputed off-session.

Broker prices only (Kite). Reuses the Kite client + Supabase handle from
kite_daily_update (single source of truth for the daily token in app_config).

Run:  py -3.11 eod_price_settle.py
"""
import time, datetime
from concurrent.futures import ThreadPoolExecutor

from kite_daily_update import kite, sb  # shared Kite client + Supabase (app_config token)

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
WORKERS = 10


def market_traded_today():
    """Data-driven trading-day guard. Returns True ONLY if the broker's last trade for
    a liquid benchmark is dated today (IST). This needs no holiday calendar and handles
    every case correctly: normal weekends, mid-week holidays (the cron still fires but
    we skip), and rare special Saturday/Sunday sessions (we act because trades happened).
    On any non-trading day last_trade_time is a prior session, so we skip and never
    overwrite the last real close."""
    today = datetime.datetime.now(IST).date()
    try:
        q = kite.quote(['NSE:RELIANCE', 'NSE:HDFCBANK', 'NSE:INFY'])
    except Exception:
        return False
    for v in q.values():
        lt = v.get('last_trade_time')
        if isinstance(lt, str):
            try:
                lt = datetime.datetime.strptime(lt[:19], '%Y-%m-%d %H:%M:%S')
            except Exception:
                lt = None
        if lt and lt.date() == today:
            return True
    return False


def load_stocks():
    out, off = [], 0
    while True:
        d = (sb.table('stock_master')
             .select('symbol,kite_tradingsymbol,kite_exchange')
             .eq('is_active', True).not_.is_('kite_token', 'null')
             .range(off, off + 999).execute().data) or []
        out += d
        if len(d) < 1000:
            break
        off += 1000
    return out


def main():
    if not market_traded_today():
        print(f'[eod-settle] market did not trade today ({datetime.datetime.now(IST):%Y-%m-%d}); skipping (no holiday calendar needed).', flush=True)
        return
    stocks = load_stocks()
    print(f'[eod-settle] {len(stocks)} active tickers with a kite_token', flush=True)
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
                print(f'  quote batch {i} err: {str(e)[:60]}', flush=True); break
    print(f'[eod-settle] quotes for {len(quotes)} instruments', flush=True)

    payloads = []
    for kid, q in quotes.items():
        sym = id_for.get(kid)
        lp = q.get('last_price')
        prev = (q.get('ohlc') or {}).get('close')
        vol = q.get('volume')
        if not (sym and lp and 0 < lp < 1e7):
            continue
        patch = {'latest_price': round(lp, 2)}
        if prev:
            patch['price_change_pct'] = round((lp / prev - 1) * 100, 2)
        if vol:  # truthy only: a 0/None volume (off-session quote) must never zero turnover
            patch['traded_value_cr'] = round(vol * lp / 1e7, 2)
        payloads.append((sym, patch))

    def write(p):
        sym, patch = p
        for attempt in range(4):
            try:
                sb.table('stock_master').update(patch).eq('symbol', sym).execute(); return True
            except Exception:
                time.sleep(0.3 * (attempt + 1))
        return False

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        ok = sum(1 for r in ex.map(write, payloads) if r)
    print(f'[eod-settle] {ok}/{len(payloads)} prices settled to broker close @ {datetime.datetime.now(IST):%Y-%m-%d %H:%M:%S IST}', flush=True)


if __name__ == '__main__':
    main()
