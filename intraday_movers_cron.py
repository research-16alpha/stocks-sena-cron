"""
intraday_movers_cron.py
=======================
LIVE movers refresh during market hours. Every `--interval` seconds it pulls one
batched Kite quote for the universe and writes to stock_master:
  latest_price, price_change_pct, traded_value_cr, rvol_1d / rvol_1w / rvol_1m
so the website's Gainers / Losers / Most Active / 52W tabs are intraday-live, not EOD.

The Kite quote already carries today's running cumulative volume, so RVOL =
today's live volume / a per-stock baseline (prior trading days' average), which is
computed once per session from the daily/{sym}.json volume bars.

Driven by ONE GitHub Actions trigger near the open (the proven --loop pattern, since
GitHub silently drops most high-frequency schedules); the loop self-exits at close.

Reuses the Kite client + Supabase handle from kite_daily_update so there is a single
source of truth for credentials (daily token in app_config vault).

Run:  py -3.11 intraday_movers_cron.py --once          # single refresh, verify
      py -3.11 intraday_movers_cron.py --loop           # 5-min loop until close
      py -3.11 intraday_movers_cron.py --loop --interval 300 --min-mcap 50
"""
import argparse, time, datetime
from concurrent.futures import ThreadPoolExecutor

import requests
from kite_daily_update import kite, sb, PUB  # shared Kite client + Supabase + storage base

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
WORKERS = 24


def market_open(now=None):
    now = now or datetime.datetime.now(IST)
    if now.weekday() >= 5:
        return False
    m = now.hour * 60 + now.minute
    return 555 <= m <= 930  # 09:15 - 15:30 IST


def load_stocks(min_mcap):
    """Liquid-enough universe to refresh live (micro-caps refresh EOD only, to keep
    per-tick write volume sane). min_mcap in ₹ Cr; 0 = everything with a kite_token."""
    out, off = [], 0
    while True:
        q = sb.table('stock_master').select(
            'symbol,kite_tradingsymbol,kite_exchange,market_cap_cr'
        ).eq('is_active', True).not_.is_('kite_token', 'null')
        if min_mcap > 0:
            q = q.gte('market_cap_cr', min_mcap)
        d = q.range(off, off + 999).execute().data or []
        out += d
        if len(d) < 1000:
            break
        off += 1000
    return out


def _median(xs):
    s = sorted(xs)
    n = len(s)
    if not n:
        return None
    m = n // 2
    return s[m] if n % 2 else (s[m - 1] + s[m]) / 2.0


def load_baselines(stocks):
    """Per-symbol volume baselines from daily/{sym}.json, EXCLUDING any bar dated
    today (today comes from the live quote):
      med21 = MEDIAN of the last 21 days  -> the 'typical day' baseline for rvol_1d.
              Median (not a single prior day) so one freak low-volume day can't
              blow RVOL up (e.g. KAMAHOLD's 94-share 04-Jun day did before).
      avg5  = mean of last 5 days  -> rvol_1w (recent-week pace).
      avg21 = mean of last 21 days -> rvol_1m (month pace)."""
    today = datetime.datetime.now(IST).date()
    base = {}

    def one(s):
        sym = s['symbol']
        try:
            r = requests.get(f"{PUB}/daily/{sym}.json", timeout=20)
            bars = (r.json() or {}).get('bars') if r.status_code == 200 else None
        except Exception:
            bars = None
        if not bars:
            return
        vols, turns = [], []
        for b in bars:
            try:
                d = datetime.datetime.strptime(str(b[0])[:10], '%Y-%m-%d').date()
                if d >= today:
                    continue  # exclude today's (partial) bar; live volume comes from the quote
                v = float(b[5]); c = float(b[4])
            except Exception:
                continue
            vols.append(v); turns.append(v * c)
        if not vols:
            return
        med21 = _median(vols[-21:])
        avg5 = sum(vols[-5:]) / len(vols[-5:])
        avg21 = sum(vols[-21:]) / len(vols[-21:])
        base[sym] = (med21, avg5, avg21)

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        list(ex.map(one, stocks))
    return base


def _rvol(vol, den):
    if not den or den <= 0 or not vol or vol <= 0:
        return None
    return round(vol / den, 2)  # NO cap: show the real relative volume


def tick(stocks, baselines):
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
                break

    payloads = []
    for kid, q in quotes.items():
        sym = id_for.get(kid)
        lp = q.get('last_price')
        prev_close = (q.get('ohlc') or {}).get('close')
        vol = q.get('volume')
        if not (sym and lp and 0 < lp < 1e7):
            continue
        patch = {'latest_price': round(lp, 2)}
        if prev_close:
            patch['price_change_pct'] = round((lp / prev_close - 1) * 100, 2)
        if vol is not None:
            patch['traded_value_cr'] = round(vol * lp / 1e7, 2)
            b = baselines.get(sym)
            if b:
                med, a5, a21 = b
                for col, den in (('rvol_1d', med), ('rvol_1w', a5), ('rvol_1m', a21)):
                    val = _rvol(vol, den)
                    if val is not None:
                        patch[col] = val
        payloads.append((sym, patch))

    def write(p):
        sym, patch = p
        try:
            sb.table('stock_master').update(patch).eq('symbol', sym).execute(); return True
        except Exception:
            return False

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        return sum(1 for ok in ex.map(write, payloads) if ok)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--once', action='store_true', help='single refresh then exit')
    ap.add_argument('--loop', action='store_true', help='refresh every --interval until market close')
    ap.add_argument('--interval', type=int, default=300, help='seconds between refreshes (default 300=5min)')
    ap.add_argument('--max-minutes', dest='max_minutes', type=int, default=210, help='safety cap per run')
    ap.add_argument('--min-mcap', dest='min_mcap', type=float, default=50, help='only refresh stocks >= this mcap (₹ Cr); 0=all')
    args = ap.parse_args()

    stocks = load_stocks(args.min_mcap)
    print(f'[intraday] {len(stocks)} stocks (min_mcap={args.min_mcap}); computing baselines...', flush=True)
    baselines = load_baselines(stocks)
    print(f'[intraday] baselines for {len(baselines)} stocks', flush=True)

    if args.once or not args.loop:
        n = tick(stocks, baselines)
        print(f'[intraday] one refresh: {n} stocks updated @ {datetime.datetime.now(IST):%H:%M:%S IST}', flush=True)
        return

    deadline = time.time() + args.max_minutes * 60
    ticks = 0
    while time.time() < deadline:
        if not market_open():
            print(f'[intraday] market closed @ {datetime.datetime.now(IST):%H:%M IST} - exit after {ticks} ticks', flush=True)
            break
        n = tick(stocks, baselines); ticks += 1
        print(f'[intraday] tick {ticks}: {n} updated @ {datetime.datetime.now(IST):%H:%M:%S IST}', flush=True)
        time.sleep(min(args.interval, max(0, deadline - time.time())))
    print(f'[intraday] done after {ticks} ticks', flush=True)


if __name__ == '__main__':
    main()
