"""
freshness_monitor.py
====================
Frequent, lightweight data-freshness monitor (complements freshness_assert.py, the
daily deep audit). Measures DATA AGE per feed - so a schedule that silently never
ran still shows up (the failure mode GitHub's own notifications can't see).

Writes one row per feed to public.feed_status (public-read -> the website /status
page) and, on an OK->STALE transition, sends an in-product notification to the
admin account (website bell + app). A recovery notice is sent when a stale feed
comes back. No notification spam: only transitions notify, never repeats.

Run:  python freshness_monitor.py            # check + write feed_status (default)
      python freshness_monitor.py --dry-run  # print only, no writes/notifications
"""
import datetime as dt
import time
import json
import os
import sys

import requests

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

URL = os.environ.get('SUPABASE_URL', 'https://tbeadvvkqyrhtendttrg.supabase.co')
SVC = os.environ.get('SUPABASE_SERVICE_KEY') or open(r'e:/Stocks sena/.supabase-service-key').read().strip()
H = {'apikey': SVC, 'Authorization': 'Bearer ' + SVC, 'Content-Type': 'application/json'}
DRY = '--dry-run' in sys.argv
# Krishna's account - bell notifications for stale feeds land here.
ADMIN_USER_ID = os.environ.get('ADMIN_USER_ID', '10658747-b0d3-4d8d-a2da-a1775afb9c87')

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
NOW = dt.datetime.now(dt.timezone.utc)
NOW_IST = NOW.astimezone(IST)


def market_open():
    return NOW_IST.weekday() < 5 and (9 * 60 + 15) <= (NOW_IST.hour * 60 + NOW_IST.minute) <= (15 * 60 + 32)


def last_trading_day(d=None):
    d = d or NOW_IST.date()
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def get(path, t=25):
    r = requests.get(f'{URL}/rest/v1/{path}', headers=H, timeout=t)
    r.raise_for_status()
    return r.json()


def parse_ts(s):
    if not s:
        return None
    s = str(s)
    try:
        if len(s) == 10:  # bare date = end of that IST day for age purposes
            return dt.datetime.fromisoformat(s).replace(tzinfo=IST)
        return dt.datetime.fromisoformat(s.replace('Z', '+00:00'))
    except Exception:
        return None


def age_min(ts):
    return None if ts is None else (NOW - ts.astimezone(dt.timezone.utc)).total_seconds() / 60


def latest(table, col, extra=''):
    # A FAILED probe is not evidence of a stale feed: under DB load (the universe
    # metrics recompute) this query can time out, and silently returning None here
    # fired a false "no data found" bell. Retry with backoff; if every attempt
    # errors, raise so the caller reports a probe failure instead of staleness.
    last_exc = None
    for attempt in range(3):
        try:
            rows = get(f'{table}?select={col}{extra}&order={col}.desc.nullslast&limit=1')
            return parse_ts(rows[0].get(col)) if rows else None
        except Exception as e:
            last_exc = e
            time.sleep(4 * (attempt + 1))
    raise RuntimeError(f'probe failed: {str(last_exc)[:50]}')


def storage_json(path):
    try:
        r = requests.get(f'{URL}/storage/v1/object/public/{path}', headers=H, timeout=25)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


# ---- feed definitions -------------------------------------------------------
# Each check returns (last_data_at: datetime|None, ok_minutes: float, note: str).
# ok_minutes = the freshness window; status = ok within it, warn within 1.5x, else stale.

def chk_prices():
    rows = get("app_config?select=value&key=eq.hb_prices_at")
    ts = parse_ts(rows[0]['value']) if rows else None
    if market_open():
        return ts, 30, 'live loop heartbeat (15-min ticks)'
    # off-hours: heartbeat from the last session is fine (loop stops at close)
    close = dt.datetime.combine(last_trading_day(), dt.time(15, 35), IST)
    return ts, max(30.0, (NOW - close.astimezone(dt.timezone.utc)).total_seconds() / 60 + 90), 'market closed - last session heartbeat'


def chk_pcr():
    ts = latest('nifty_pcr_history', 'captured_at')
    if market_open():
        return ts, 60, 'PCR cron (10-min slots, GH-delayed)'
    close = dt.datetime.combine(last_trading_day(), dt.time(15, 35), IST)
    return ts, max(60.0, (NOW - close.astimezone(dt.timezone.utc)).total_seconds() / 60 + 120), 'market closed'


def _daily_by(table, col, due_hour_ist, label):
    """EOD feeds: today's (or last trading day's) row must exist by due_hour_ist."""
    ts = latest(table, col)
    ltd = last_trading_day()
    due = dt.datetime.combine(ltd, dt.time(due_hour_ist, 0), IST)
    if ts and ts.astimezone(IST).date() >= ltd:
        return ts, 10 ** 6, label  # current -> always ok
    if NOW_IST < due.astimezone(IST) + dt.timedelta(0):
        # not due yet today: judge against the PREVIOUS trading day instead
        prev = last_trading_day(ltd - dt.timedelta(days=1))
        if ts and ts.astimezone(IST).date() >= prev:
            return ts, 10 ** 6, label + ' (today not due yet)'
    return ts, 0, label + f' (due {due_hour_ist:02d}:00 IST)'


def chk_candles():
    d = storage_json('daily/RELIANCE.json')
    bars = (d or {}).get('bars') or []
    ts = parse_ts(bars[-1][0]) if bars else None
    ltd = last_trading_day()
    if ts and ts.astimezone(IST).date() >= ltd:
        return ts, 10 ** 6, 'RELIANCE last bar current'
    if NOW_IST.hour < 18 and NOW_IST.date() == ltd:  # today's bar lands after close (~17:00)
        prev = last_trading_day(ltd - dt.timedelta(days=1))
        if ts and ts.astimezone(IST).date() >= prev:
            return ts, 10 ** 6, 'RELIANCE last bar = prev session (today due 18:00)'
    return ts, 0, 'daily candles behind (due 18:00 IST)'


def chk_intraday():
    d = storage_json('intraday/RELIANCE.json')
    ts = parse_ts((d or {}).get('date'))
    ltd = last_trading_day()
    if ts and ts.astimezone(IST).date() >= ltd:
        return ts, 10 ** 6, 'intraday bars current'
    if NOW_IST.hour < 19 and NOW_IST.date() == ltd:
        prev = last_trading_day(ltd - dt.timedelta(days=1))
        if ts and ts.astimezone(IST).date() >= prev:
            return ts, 10 ** 6, 'intraday = prev session (today due 19:00)'
    return ts, 0, 'intraday bars behind (due 19:00 IST)'


FEEDS = [
    # feed key, label, category, expected text, check fn
    ('prices', 'Live prices (movers/quotes)', 'market', '15-min during market hours', chk_prices),
    ('pcr', 'NIFTY Put-Call Ratio', 'market', '~10-min during market hours', chk_pcr),
    ('breadth', 'Market breadth', 'market', 'daily by ~18:00 IST', lambda: _daily_by('breadth_daily', 'date', 18, 'breadth cron')),
    ('mood', 'Market mood score', 'market', 'daily by ~18:00 IST', lambda: _daily_by('market_mood_history', 'date', 18, 'mood cron')),
    ('fii_flows', 'FII / DII flows', 'market', 'daily by ~20:00 IST', lambda: _daily_by('fii_dii_flows', 'date', 20, 'FII/DII scraper')),
    ('candles', 'Daily price candles', 'market', 'post-close by ~18:00 IST', chk_candles),
    ('intraday', 'Intraday minute bars', 'market', 'post-close by ~19:00 IST', chk_intraday),
    ('announcements', 'Corporate announcements', 'filings', 'continuous (≤ 36h)', lambda: (latest('corporate_announcements', 'filed_at'), 36 * 60, 'BSE/NSE filings feed')),
    ('news', 'Market news', 'content', 'continuous (≤ 24h)', lambda: (latest('news_items', 'published_at'), 24 * 60, 'news scraper')),
    ('articles', 'Research articles', 'content', 'daily-ish (≤ 48h)', lambda: (latest('articles', 'published_at', '&status=eq.published'), 48 * 60, 'article engine')),
    ('bulk_deals', 'Bulk / block deals', 'filings', 'trading days (≤ 3d)', lambda: (latest('bulk_deals', 'date'), 3 * 1440, 'deals scraper')),
    ('insider', 'Insider trades', 'filings', 'trading days (≤ 4d)', lambda: (latest('insider_trades', 'created_at'), 4 * 1440, 'insider feed')),
    # Was a blind spot: corporate_actions (dividends/splits ex-dates) went stale for days
    # when NSE soft-blocked the cloud runner and nothing watched it. Now monitored on its
    # fetched_at (laptop daily_feeds refresh); a 3-day breach fires the bell + heals.
    ('corp_actions', 'Corporate actions / dividends', 'filings', 'daily, laptop job (≤ 3d)', lambda: (latest('corporate_actions', 'fetched_at'), 3 * 1440, 'corp-actions scraper (laptop)')),
    ('macro', 'Macro indicators', 'market', 'every few days (≤ 5d)', lambda: (latest('macro_indicators', 'date'), 5 * 1440, 'macro cron')),
    ('mf_nav', 'Mutual fund NAVs', 'market', 'daily, laptop job (≤ 4d)', lambda: (latest('mf_nav', 'nav_date'), 4 * 1440, 'AMFI NAV (laptop 22:00)')),
]


# self-heal: when a feed goes stale because GitHub dropped a schedule (recurring
# disease - 4th incident 2026-06-11), dispatch the owning workflow directly. Uses
# the runner's GITHUB_TOKEN (needs `permissions: actions: write` in the yml).
HEAL_WORKFLOW = {
    'candles': 'kite-daily-update.yml',
    'intraday': 'kite-daily-update.yml',
    'prices': 'intraday-movers.yml',
    'breadth': 'breadth-cron.yml',
    'mood': 'mood-history-cron.yml',
    'pcr': 'pcr-cron.yml',
    'fii_flows': 'fii-dii-cron.yml',
}

def heal(feed_key):
    tok = os.environ.get('GITHUB_TOKEN')
    wf = HEAL_WORKFLOW.get(feed_key)
    if not tok or not wf:
        return False
    try:
        r = requests.post(
            f'https://api.github.com/repos/research-16alpha/stocks-sena-cron/actions/workflows/{wf}/dispatches',
            headers={'Authorization': 'Bearer ' + tok, 'Accept': 'application/vnd.github+json',
                     'X-GitHub-Api-Version': '2022-11-28'},
            json={'ref': 'main'}, timeout=20)
        print(f'  [heal] dispatched {wf}: HTTP {r.status_code}')
        return r.status_code == 204
    except Exception as e:
        print(f'  [heal] dispatch failed: {str(e)[:60]}')
        return False


def notify_admin(title, body):
    if DRY:
        print(f'  WOULD NOTIFY ADMIN: {title} | {body}')
        return
    requests.post(f'{URL}/rest/v1/user_notifications', headers={**H, 'Prefer': 'return=minimal'},
                  data=json.dumps({'user_id': ADMIN_USER_ID, 'title': title, 'body': body,
                                   'link': '/status'}), timeout=25).raise_for_status()


def main():
    prev = {r['feed']: r for r in get('feed_status?select=feed,status,stale_since')} if not DRY else {}
    out, stale_now = [], []
    print(f'=== freshness monitor @ {NOW_IST:%Y-%m-%d %H:%M IST} (market {"OPEN" if market_open() else "closed"}) ===')
    for key, label, cat, expected, fn in FEEDS:
        probe_err = False
        try:
            ts, ok_min, note = fn()
        except Exception as e:
            ts, ok_min, note, probe_err = None, 0, f'probe error: {str(e)[:60]}', True
        a = age_min(ts)
        if probe_err:
            # a failed probe says nothing about the feed - report, never bell/heal
            status, detail = 'warn', note
        elif ts is None:
            status, detail = 'stale', f'no data found ({note})'
        elif ok_min >= 10 ** 6:
            status, detail = 'ok', note
        elif a <= ok_min:
            status, detail = 'ok', f'{a:.0f}m old ({note})'
        elif a <= ok_min * 1.5:
            status, detail = 'warn', f'{a:.0f}m old, window {ok_min:.0f}m ({note})'
        else:
            status, detail = 'stale', f'{a/60:.1f}h old, window {ok_min/60:.1f}h ({note})'
        was = (prev.get(key) or {}).get('status', 'ok')
        stale_since = (prev.get(key) or {}).get('stale_since')
        if status == 'stale' and was != 'stale':
            stale_since = NOW.strftime('%Y-%m-%dT%H:%M:%SZ')
            healed = heal(key)
            notify_admin(f'⚠️ Data feed stale: {label}',
                         f'{detail}. Expected: {expected}.'
                         + (' Auto-heal: owning cron re-dispatched.' if healed
                            else ' Check /status and the cron logs.'))
            stale_now.append(key)
        elif status != 'stale':
            if was == 'stale':
                notify_admin(f'✅ Feed recovered: {label}', f'Back to {status}: {detail}')
            stale_since = None
        out.append({'feed': key, 'label': label, 'category': cat, 'expected': expected,
                    'last_data_at': ts.astimezone(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ') if ts else None,
                    'status': status, 'detail': detail, 'stale_since': stale_since,
                    'checked_at': NOW.strftime('%Y-%m-%dT%H:%M:%SZ')})
        print(f"  {status.upper():5} {label:28} {detail[:70]}")
    if not DRY:
        r = requests.post(f'{URL}/rest/v1/feed_status', headers={**H, 'Prefer': 'resolution=merge-duplicates,return=minimal'},
                          data=json.dumps(out), timeout=30)
        r.raise_for_status()
        print(f'[monitor] wrote {len(out)} feed rows' + (f' · notified stale: {stale_now}' if stale_now else ''))


if __name__ == '__main__':
    main()
