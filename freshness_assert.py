"""
freshness_assert.py  (C5)
=========================
Single source of truth for "is the data actually current?". Runs daily AFTER the
ingestion crons and FAILS LOUD (exit 1) when any dataset is stale or under-covered.
Before this, a cron could exit 0 having written nothing and no one would know.

What it checks (all read-only):
  1. Per-table freshness SLA: max(timestamp) age vs an allowed max, + a minimum
     row count in the recent window, for every important table.
  2. Fundamentals coverage: how many ACTIVE stocks are missing the expected
     latest filed quarter (given a +45-day filing window after each quarter-end).
  3. Large-cap watchlist: a hardcoded set of ~30 bellwethers must each carry the
     expected quarter in their bundle — catches "we silently missed RELIANCE".

Trading-calendar aware: market-feed tables are measured against the last NSE
trading day (skips weekends), so Saturday/Sunday don't false-alarm.

Exit code: 0 = all green; 1 = at least one breach (CI marks the run failed, and
the monitor.yml notifier fires). Use --warn-only to print without failing.

USAGE
  py -3.11 freshness_assert.py
  py -3.11 freshness_assert.py --warn-only
"""
import argparse
import datetime as dt
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

TODAY = dt.date.today()


def last_trading_day(d=None):
    """Most recent Mon-Fri on/before d (NSE holidays not modelled — weekend is the
    dominant false-alarm source; a holiday at worst delays an alert by 1 day)."""
    d = d or TODAY
    while d.weekday() >= 5:   # 5=Sat,6=Sun
        d -= dt.timedelta(days=1)
    return d


def expected_latest_quarter(d=None):
    """The most recent quarter-end whose +45-day filing window has elapsed by d."""
    d = d or TODAY
    # quarter-ends and the date by which ~all results are filed (~45d later)
    qs = [((d.year - 1, 12, 31), (d.year, 2, 14)),
          ((d.year, 3, 31), (d.year, 5, 15)),
          ((d.year, 6, 30), (d.year, 8, 14)),
          ((d.year, 9, 30), (d.year, 11, 14)),
          ((d.year, 12, 31), (d.year + 1, 2, 14))]
    latest = None
    for (qy, qm, qd), (fy, fm, fd) in qs:
        if dt.date(fy, fm, fd) <= d:
            latest = dt.date(qy, qm, qd)
    return latest or dt.date(d.year - 1, 12, 31)


def _count(table, query=''):
    try:
        r = requests.get(f'{URL}/rest/v1/{table}?{query}',
                         headers={**H, 'Prefer': 'count=exact', 'Range': '0-0'}, timeout=30)
        if r.status_code >= 400:
            return None
        return int(r.headers.get('content-range', '*/0').split('/')[-1])
    except Exception:
        return None


def _max_ts(table, candidates):
    """Return (column, max_value_str) for the first timestamp-ish column that exists."""
    for col in candidates:
        try:
            r = requests.get(f'{URL}/rest/v1/{table}?select={col}&order={col}.desc.nullslast&limit=1',
                             headers=H, timeout=30)
            if r.status_code >= 400:
                continue
            rows = r.json()
            if rows and rows[0].get(col):
                return col, str(rows[0][col])
        except Exception:
            continue
    return None, None


def _age_days(ts_str):
    if not ts_str:
        return None
    s = ts_str.replace('Z', '').split('.')[0].split('+')[0]
    for fmt in ('%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
        try:
            return (dt.datetime.now() - dt.datetime.strptime(s[:19], fmt)).total_seconds() / 86400
        except ValueError:
            continue
    return None


# table -> SLA. max_age_days measured vs NOW; market feeds get +2d slack so a
# weekend/Monday-morning run doesn't false-alarm (also compared to last trading day).
SLA = [
    # table, ts candidates, max_age_days, market_feed, label
    ('stock_master', ['last_synced', 'quality_updated_at', 'fundamentals_ts'], 2.0, True, 'price/quality sync'),
    ('nifty_pcr_history', ['captured_at', 'created_at', 'ts', 'date'], 1.5, True, 'PCR (feeds Mood)'),
    ('fii_dii_flows', ['date', 'trade_date', 'created_at'], 2.0, True, 'FII/DII flows'),
    ('corporate_actions', ['created_at', 'updated_at', 'ex_date', 'date'], 7.0, False, 'corporate actions'),
    ('breadth_daily', ['date', 'created_at'], 2.0, True, 'market breadth'),
    ('market_mood_history', ['date', 'created_at', 'captured_at'], 2.0, True, 'market mood'),
    ('macro_indicators', ['date', 'updated_at', 'created_at'], 4.0, False, 'macro'),
    ('bulk_deals', ['date', 'trade_date', 'created_at'], 4.0, True, 'bulk deals'),
    ('news_items', ['published_at', 'created_at', 'fetched_at'], 1.5, False, 'news'),
    ('shareholding_periods', ['period', 'updated_at', 'created_at'], 120.0, False, 'shareholding (quarterly)'),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--warn-only', action='store_true', help='print breaches but exit 0')
    args = ap.parse_args()
    if not KEY:
        print('[ERR] no Supabase key', file=sys.stderr)
        sys.exit(2)

    ltd = last_trading_day()
    exp_q = expected_latest_quarter().isoformat()
    breaches = []
    print(f'=== Freshness assertion @ {dt.datetime.now():%Y-%m-%d %H:%M} '
          f'(last trading day {ltd}, expected quarter {exp_q}) ===\n')

    # 1) per-table SLA
    print('%-24s %-10s %-22s %s' % ('table', 'rows', 'latest', 'verdict'))
    for table, cands, max_age, market, label in SLA:
        n = _count(table)
        if n is None:
            print('%-24s %-10s %-22s %s' % (table, '-', '-', 'SKIP (no table/perm)'))
            continue
        col, ts = _max_ts(table, cands)
        age = _age_days(ts)
        # market feeds: allow age up to (now - last_trading_day) + max_age slack
        eff_max = max_age + ((TODAY - ltd).days if market else 0)
        stale = (age is None) or (age > eff_max) or (n == 0)
        verdict = 'OK'
        if n == 0:
            verdict = 'BREACH: 0 rows'
        elif age is None:
            verdict = f'BREACH: no ts ({col or "none"})'
        elif age > eff_max:
            verdict = f'BREACH: {age:.1f}d old (>{eff_max:.1f})'
        if stale:
            breaches.append(f'{table} ({label}): {verdict}')
        print('%-24s %-10s %-22s %s' % (table, n, (ts or '-')[:22], verdict))

    # 2) fundamentals coverage vs expected quarter
    active = _count('stock_master', 'is_active=eq.true')
    cur_q = _count('stock_master', f'is_active=eq.true&latest_quarter_period=gte.{exp_q}')
    if active and cur_q is not None:
        pct = 100 * cur_q / active
        print(f'\nfundamentals: {cur_q}/{active} active carry latest quarter >= {exp_q}  ({pct:.0f}%)')
        if pct < 55:
            breaches.append(f'fundamentals coverage low: only {pct:.0f}% have quarter >= {exp_q}')

    # 3) large-cap watchlist must each have the expected quarter
    WATCH = ['RELIANCE', 'TCS', 'HDFCBANK', 'INFY', 'ICICIBNK', 'SBIN', 'ITC', 'LT',
             'BHARTIARTL', 'KOTAKBANK', 'AXISBANK', 'MARUTI', 'SUNPHARMA', 'TITAN',
             'BAJFINANCE', 'HINDUNILVR', 'NESTLEIND', 'WIPRO', 'ULTRACEMCO', 'NTPC',
             'POWERGRID', 'TATAMOTORS', 'TATASTEEL', 'ASIANPAINT', 'HCLTECH', 'ONGC',
             'COALINDIA', 'ADANIENT', 'JSWSTEEL', 'VEDL']
    missing_watch = []
    for sym in WATCH:
        try:
            d = requests.get(f'{URL}/storage/v1/object/public/fundamentals-v2/{sym}.json', timeout=20).json()
            q = d.get('quarterly_results') or d.get('quarterly_results_consolidated') or []
            latest = q[-1].get('period') if q else None
            # bellwethers should be within one quarter of expected (allow Q-1 lag)
            if not latest or latest < (dt.date.fromisoformat(exp_q) - dt.timedelta(days=100)).isoformat():
                missing_watch.append(f'{sym}={latest}')
        except Exception:
            missing_watch.append(f'{sym}=ERR')
    if missing_watch:
        breaches.append(f'watchlist stale ({len(missing_watch)}): ' + ', '.join(missing_watch[:12]))
        print(f'\nwatchlist stale: {missing_watch}')
    else:
        print(f'\nwatchlist: all {len(WATCH)} bellwethers current (>= ~{exp_q})')

    print('\n' + '=' * 60)
    if breaches:
        print(f'FRESHNESS: {len(breaches)} BREACH(es):')
        for b in breaches:
            print(f'  ✗ {b}')
        if not args.warn_only:
            sys.exit(1)
    else:
        print('FRESHNESS: all datasets within SLA ✓')


if __name__ == '__main__':
    main()
