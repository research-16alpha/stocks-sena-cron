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


def _ltt(q):
    """Parsed last_trade_time (datetime) from a Kite quote, or None."""
    lt = q.get('last_trade_time')
    if isinstance(lt, str):
        try:
            return datetime.datetime.strptime(lt[:19], '%Y-%m-%d %H:%M:%S')
        except Exception:
            return None
    return lt


def market_traded_today():
    """Data-driven trading-day guard: True only if a liquid mega-cap's last trade is dated today
    (IST). Handles weekends, mid-week holidays AND rare special Saturday sessions with no calendar."""
    today = datetime.datetime.now(IST).date()
    try:
        q = kite.quote(['NSE:RELIANCE', 'NSE:HDFCBANK', 'NSE:INFY', 'NSE:TCS', 'NSE:ICICIBANK'])
    except Exception:
        return False
    return any(_ltt(v) and _ltt(v).date() == today for v in q.values())


def market_open(now=None):
    # Time window only (09:15-15:30 IST). The trading-DAY decision (holidays + rare special
    # Saturday sessions) is the data guard market_traded_today(), NOT the calendar/weekday.
    now = now or datetime.datetime.now(IST)
    m = now.hour * 60 + now.minute
    return 555 <= m <= 930


def load_stocks(min_mcap):
    """Liquid-enough universe to refresh live (micro-caps refresh EOD only, to keep
    per-tick write volume sane). min_mcap in ₹ Cr; 0 = everything with a kite_token."""
    out, off = [], 0
    while True:
        q = sb.table('stock_master').select(
            'symbol,kite_tradingsymbol,kite_exchange,market_cap_cr,pe_ratio,pb_ratio'
        ).eq('is_active', True).not_.is_('kite_token', 'null')
        if min_mcap > 0:
            q = q.gte('market_cap_cr', min_mcap)
        d = q.range(off, off + 999).execute().data or []
        out += d
        if len(d) < 1000:
            break
        off += 1000
    return out


def load_baselines(stocks):
    """Per-symbol volume baselines from daily/{sym}.json, EXCLUDING any bar dated
    today (today comes from the live quote). RVOL = today's volume / average daily
    volume over the last N trading days:
      d1   = last 1 day  -> rvol_1d (today vs the previous trading day)
      avg5  = mean of last 5 days  -> rvol_1w (today vs the last week)
      avg21 = mean of last 21 days -> rvol_1m (today vs the last month)
    No cap is applied: the real ratio is stored as-is."""
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
        d1 = vols[-1]                                   # last 1 trading day
        avg5 = sum(vols[-5:]) / len(vols[-5:])          # last 5 days (week)
        avg21 = sum(vols[-21:]) / len(vols[-21:])       # last 21 days (month)
        base[sym] = (d1, avg5, avg21)

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        list(ex.map(one, stocks))
    return base


def _rvol(vol, den):
    if not den or den <= 0 or not vol or vol <= 0:
        return None
    return round(vol / den, 2)  # NO cap: show the real relative volume


def tick(stocks, baselines):
    today = datetime.datetime.now(IST).date()
    id_for = {f"{s['kite_exchange']}:{s['kite_tradingsymbol']}": s for s in stocks}
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

    # Market-open guard (the reliable "is it moving?" check): a live session exists only if at
    # least one instrument actually traded today. On a weekend/holiday the cron may still fire,
    # but every quote is from a prior session -> write nothing and never overwrite the real last
    # close. Once the session is confirmed open we refresh EVERY ticker: an untraded small-cap
    # simply shows 0% (last_price == prev close) and updates the instant it trades.
    if not any(_ltt(q) and _ltt(q).date() == today for q in quotes.values()):
        return 0

    payloads = []
    for kid, q in quotes.items():
        s = id_for.get(kid)
        if not s:
            continue
        sym = s['symbol']
        lp = q.get('last_price')
        prev_close = (q.get('ohlc') or {}).get('close')
        vol = q.get('volume')
        if not (sym and lp and 0 < lp < 1e7):
            continue
        patch = {'latest_price': round(lp, 2)}
        if prev_close:
            patch['price_change_pct'] = round((lp / prev_close - 1) * 100, 2)
            # Live valuation: market_cap / pe / pb are LINEAR in price (mcap = shares*price,
            # pe = mcap/profit, pb = price/bvps). compute_metrics computed the stored values at
            # the prev close, so scaling them by lp/prev_close reproduces that exact formula on
            # the live price. Bounded factor guards a bad quote. Assumes ONE live-scaler per
            # session (the daily cloud --loop); compute_metrics re-anchors the base each EOD.
            f = lp / prev_close
            if 0.5 < f < 1.5:
                # Bound to the SAME sane ranges compute_metrics enforces, so the live value
                # never drifts past its EOD cap (mcap < 25 lakh cr, pe/pb in 0..5000). Out of
                # range -> leave the prior value rather than write an out-of-band number.
                if s.get('market_cap_cr'):
                    mc = round(s['market_cap_cr'] * f, 2)
                    if 0 < mc < 2_500_000:
                        patch['market_cap_cr'] = mc
                if s.get('pe_ratio'):
                    pe = round(s['pe_ratio'] * f, 2)
                    if 0 < pe < 5000:
                        patch['pe_ratio'] = pe
                if s.get('pb_ratio'):
                    pb = round(s['pb_ratio'] * f, 2)
                    if 0 < pb < 5000:
                        patch['pb_ratio'] = pb
        if vol is not None:
            patch['traded_value_cr'] = round(vol * lp / 1e7, 2)
            b = baselines.get(sym)
            if b:
                d1, a5, a21 = b
                for col, den in (('rvol_1d', d1), ('rvol_1w', a5), ('rvol_1m', a21)):
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

    # Trading-day gate (loop mode): the cron fires every day, but we only enter the live loop if
    # the market actually traded today. Normal Sat/Sun and mid-week holidays exit here in seconds
    # (no token / no trade -> False); a real session (incl. a rare special Saturday/Sunday) runs.
    if args.loop and not market_traded_today():
        print(f'[intraday] not a trading session today ({datetime.datetime.now(IST):%Y-%m-%d}); exiting without looping.', flush=True)
        return

    # Self-heal frozen Kite identifiers (e.g. NSE T2T names needing a -BE suffix) once at
    # startup, BEFORE loading the universe, so corrected ids are quoted this session and never
    # silently freeze the movers board. Reuses our existing Kite client (no re-auth).
    try:
        from fix_frozen_kite_ids import heal
        heal(kite=kite)
    except Exception as e:
        print('[heal] skipped:', str(e)[:120], flush=True)

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
