"""
alert_delivery.py
=================
Evaluates user_alerts against live data and delivers IN-PRODUCT notifications
(user_notifications table -> website bell + app feed). No email/SMS in v1.

Alert types (DB CHECK-constraint names, shared by website + app):
  price_above       latest_price >  threshold        | cooldown: 24h while condition holds
  price_below       latest_price <  threshold        | cooldown: 24h
  price_change_pct  |price_change_pct| >= threshold  | once per trading day
  volume_spike      rvol_1d >= 3                     | once per trading day
  insider_trade     new insider_trades row for sym (>= threshold Cr if set) | newer than last fire
  promoter_action   new promoter_actions row for sym | newer than last fire
  news_keyword      news headline contains keyword   | newer than last fire

Run:  python alert_delivery.py            # DRY-RUN: prints what WOULD fire
      python alert_delivery.py --apply    # writes notifications + updates alerts
      python alert_delivery.py --types price   # only market-data types (loop use)
      python alert_delivery.py --types events  # only insider/promoter/news (evening use)

Designed to be cheap: one alerts read + a handful of batched lookups; safe to call
every 15 minutes from the intraday movers loop.
"""
import os
import sys
import json
import datetime

import requests

# Windows consoles default to cp1252 which can't print the rupee sign
try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

URL = os.environ.get('SUPABASE_URL', 'https://tbeadvvkqyrhtendttrg.supabase.co')
SVC = os.environ.get('SUPABASE_SERVICE_KEY') or open(r'e:/Stocks sena/.supabase-service-key').read().strip()
H = {'apikey': SVC, 'Authorization': 'Bearer ' + SVC, 'Content-Type': 'application/json'}
APPLY = '--apply' in sys.argv
TYPES_ARG = sys.argv[sys.argv.index('--types') + 1] if '--types' in sys.argv else 'all'

MARKET_TYPES = ('price_above', 'price_below', 'price_change_pct', 'volume_spike')
EVENT_TYPES = ('insider_trade', 'promoter_action', 'news_keyword')


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc)


def iso(dt):
    # 'Z' suffix, NOT '+00:00': a literal '+' in a URL query decodes as a space and
    # PostgREST then 400s the timestamp filter.
    return dt.strftime('%Y-%m-%dT%H:%M:%SZ')


def get(path):
    r = requests.get(f'{URL}/rest/v1/{path}', headers=H, timeout=30)
    r.raise_for_status()
    return r.json()


def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(str(s).replace('Z', '+00:00'))
    except Exception:
        return None


def fire(alert, title, body, link):
    """Queue/insert one notification + bump the alert counters."""
    if not APPLY:
        print(f"  WOULD NOTIFY [{alert['alert_type']}] {alert.get('symbol') or alert.get('keyword')}: {title} | {body}")
        return
    requests.post(f'{URL}/rest/v1/user_notifications', headers={**H, 'Prefer': 'return=minimal'},
                  data=json.dumps({'user_id': alert['user_id'], 'alert_id': alert['id'],
                                   'symbol': alert.get('symbol'), 'title': title, 'body': body,
                                   'link': link}), timeout=30).raise_for_status()
    requests.patch(f"{URL}/rest/v1/user_alerts?id=eq.{alert['id']}", headers={**H, 'Prefer': 'return=minimal'},
                   data=json.dumps({'fired_count': (alert.get('fired_count') or 0) + 1,
                                    'last_fired': iso(now_utc())}), timeout=30).raise_for_status()
    print(f"  NOTIFIED [{alert['alert_type']}] {alert.get('symbol')}: {title}")


def hours_since_fired(alert):
    lf = parse_ts(alert.get('last_fired'))
    return 1e9 if lf is None else (now_utc() - lf).total_seconds() / 3600


def fired_today_ist(alert):
    lf = parse_ts(alert.get('last_fired'))
    if lf is None:
        return False
    ist = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
    return lf.astimezone(ist).date() == now_utc().astimezone(ist).date()


def since_for(alert, fallback_days=3):
    """Event alerts only react to rows newer than the last fire (or N days back on first run)."""
    lf = parse_ts(alert.get('last_fired'))
    return lf or (now_utc() - datetime.timedelta(days=fallback_days))


def run_market(alerts):
    """price_above / price_below / pct_move / volume_spike — one batched stock_master read."""
    todo = [a for a in alerts if a['alert_type'] in MARKET_TYPES and a.get('symbol')]
    if not todo:
        return
    syms = sorted({a['symbol'] for a in todo})
    rows = []
    for i in range(0, len(syms), 100):
        chunk = ','.join(f'"{s}"' for s in syms[i:i + 100])
        rows += get(f'stock_master?select=symbol,name,latest_price,price_change_pct,rvol_1d&symbol=in.({chunk})')
    sm = {r['symbol']: r for r in rows}
    for a in todo:
        s = sm.get(a['symbol'])
        if not s or s.get('latest_price') is None:
            continue
        px, chg, rvol = s['latest_price'], s.get('price_change_pct'), s.get('rvol_1d')
        t, th, link = a['alert_type'], a.get('threshold'), f"/stocks/{a['symbol']}"
        if t == 'price_above' and th is not None and px > th and hours_since_fired(a) >= 24:
            fire(a, f"{a['symbol']} rose above ₹{th:g}", f"Now ₹{px:,} ({'+' if (chg or 0) >= 0 else ''}{chg}% today)", link)
        elif t == 'price_below' and th is not None and px < th and hours_since_fired(a) >= 24:
            fire(a, f"{a['symbol']} dropped below ₹{th:g}", f"Now ₹{px:,} ({'+' if (chg or 0) >= 0 else ''}{chg}% today)", link)
        elif t == 'price_change_pct' and th is not None and chg is not None and abs(chg) >= th and not fired_today_ist(a):
            fire(a, f"{a['symbol']} moved {chg:+.2f}% today", f"Crossed your ±{th:g}% alert. Now ₹{px:,}", link)
        elif t == 'volume_spike' and rvol is not None and rvol >= 3 and not fired_today_ist(a):
            fire(a, f"{a['symbol']} volume spike: {rvol:.1f}× usual", f"Trading at {rvol:.1f}× its average daily volume. ₹{px:,} ({chg:+.2f}%)", link)


def run_events(alerts):
    """insider / promoter / news — only rows filed AFTER the alert's last fire."""
    for a in [x for x in alerts if x['alert_type'] == 'insider_trade' and x.get('symbol')]:
        since = iso(since_for(a))
        minv = f"&value_cr=gte.{a['threshold']}" if a.get('threshold') else ''
        rows = get(f"insider_trades?select=insider_name,trade_type,value_cr,trade_date,created_at&symbol=eq.{a['symbol']}"
                   f"&created_at=gt.{since}{minv}&order=created_at.desc&limit=3")
        if rows and not fired_today_ist(a):
            r0 = rows[0]
            more = f" (+{len(rows)-1} more)" if len(rows) > 1 else ''
            fire(a, f"Insider trade in {a['symbol']}{more}",
                 f"{r0.get('insider_name') or 'Insider'}: {r0.get('trade_type') or 'trade'}"
                 + (f" · ₹{r0['value_cr']:g} Cr" if r0.get('value_cr') else ''),
                 f"/stocks/{a['symbol']}")
    for a in [x for x in alerts if x['alert_type'] == 'promoter_action' and x.get('symbol')]:
        since = iso(since_for(a))
        rows = get(f"promoter_actions?select=action_type,action_description,filing_date,created_at&symbol=eq.{a['symbol']}"
                   f"&created_at=gt.{since}&order=created_at.desc&limit=3")
        if rows and not fired_today_ist(a):
            r0 = rows[0]
            fire(a, f"Promoter activity in {a['symbol']}",
                 (r0.get('action_description') or r0.get('action_type') or 'New promoter filing')[:140],
                 f"/stocks/{a['symbol']}")
    for a in [x for x in alerts if x['alert_type'] == 'news_keyword' and x.get('keyword')]:
        since = iso(since_for(a, fallback_days=1))
        kw = a['keyword'].replace('*', '').replace(',', ' ').strip()
        if not kw:
            continue
        rows = get(f"news_items?select=headline,source,published_at&headline=ilike.*{requests.utils.quote(kw)}*"
                   f"&published_at=gt.{since}&order=published_at.desc&limit=3")
        if rows and not fired_today_ist(a):
            r0 = rows[0]
            more = f" (+{len(rows)-1} more)" if len(rows) > 1 else ''
            fire(a, f'News: "{kw}"{more}', (r0.get('headline') or '')[:140], '/news')


def run(types='all', apply=False):
    """Programmatic entrypoint (used by intraday_movers_cron each tick with types='price')."""
    global APPLY
    APPLY = apply
    alerts = get('user_alerts?select=*&is_active=eq.true&limit=5000')
    print(f"[alerts] {len(alerts)} active · mode={'APPLY' if APPLY else 'DRY-RUN'} · types={types}", flush=True)
    if not alerts:
        return
    if types in ('all', 'price'):
        run_market(alerts)
    if types in ('all', 'events'):
        run_events(alerts)
    print('[alerts] done', flush=True)


if __name__ == '__main__':
    run(TYPES_ARG, APPLY)
