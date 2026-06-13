"""
gmp_scraper.py
==============
Grey-market premium (GMP) for live/upcoming IPOs, from TWO independent sources so a
single source can't mislead (GMP is informal grey-market data and sources disagree):
  - PRIMARY:  investorgain JSON API (clean: name, GMP, GMP%, price, dates)
  - VERIFY:   ipowatch HTML table (name + GMP)

Matches by normalized company name against ipo_calendar rows that are Open/Upcoming,
writes gmp / gmp_pct / gmp_updated / gmp_sources. gmp_sources records BOTH readings so
the UI can show the spread and a divergence is visible, never hidden. No recommendations.

Run:  py -3.11 gmp_scraper.py            # write
      py -3.11 gmp_scraper.py --dry      # report, no write
"""
import os
import re
import sys
import html
import datetime as dt

import requests

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

URL = os.environ.get('SUPABASE_URL', 'https://tbeadvvkqyrhtendttrg.supabase.co').rstrip('/')
KEY = os.environ.get('SUPABASE_SERVICE_KEY')
if not KEY:
    with open(r'e:/Stocks sena/.supabase-service-key') as f:
        KEY = f.read().strip()
H = {'apikey': KEY, 'Authorization': f'Bearer {KEY}'}
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'
DRY = '--dry' in sys.argv

IG_API = 'https://webnodejs.investorgain.com/cloud/report/data-read/331/1/5/{y}/{fy}/0/all'


def norm(s):
    s = re.sub(r'<[^>]+>', ' ', html.unescape(str(s or '')))
    s = re.sub(r'[^a-z0-9 ]', ' ', s.lower())
    s = re.sub(r'\b(limited|ltd|pvt|private|india|the|co|company|ipo|sme)\b', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()


def first_num(s):
    s = html.unescape(str(s or '')).replace('₹', '').replace(',', '')
    m = re.search(r'-?\d+\.?\d*', s)
    return float(m.group(0)) if m else None


def fetch_investorgain():
    y = dt.date.today().year
    fy = f'{y}-{str(y + 1)[2:]}'
    out = {}
    try:
        r = requests.get(IG_API.format(y=y, fy=fy), headers={'User-Agent': UA}, timeout=25)
        for row in (r.json().get('reportTableData') or []):
            name = row.get('~ipo_name') or re.sub(r'<[^>]+>', '', row.get('Name') or '')
            gmp = first_num(row.get('GMP'))
            pct = first_num(row.get('~gmp_percent_calc'))
            if name and gmp is not None:
                out[norm(name)] = {'gmp': gmp, 'pct': pct, 'name': name.strip()}
    except Exception as e:
        print(f'[gmp] investorgain fetch failed: {e}', file=sys.stderr)
    return out


def fetch_ipowatch():
    out = {}
    try:
        r = requests.get('https://ipowatch.in/ipo-grey-market-premium-latest-ipo-gmp/', headers={'User-Agent': UA}, timeout=25)
        for row in re.findall(r'<tr[^>]*>(.*?)</tr>', r.text, re.S):
            cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.S)
            if len(cells) >= 2:
                name = re.sub(r'<[^>]+>', '', cells[0]).strip()
                gmp = first_num(cells[1])
                if name and gmp is not None and 'gmp' not in name.lower():
                    out[norm(name)] = {'gmp': gmp, 'name': name}
    except Exception as e:
        print(f'[gmp] ipowatch fetch failed: {e}', file=sys.stderr)
    return out


def main():
    today = dt.date.today().isoformat()
    cal = requests.get(f'{URL}/rest/v1/ipo_calendar?select=symbol,company_name,status,issue_end'
                       f'&or=(status.eq.Open,status.eq.Upcoming,issue_end.gte.{today})', headers=H, timeout=30).json()
    if not cal:
        print('[gmp] no open/upcoming IPOs in calendar'); return
    ig, iw = fetch_investorgain(), fetch_ipowatch()
    print(f'[gmp] sources: investorgain {len(ig)} | ipowatch {len(iw)} | calendar open/upcoming {len(cal)}')

    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    n = 0
    for c in cal:
        key = norm(c['company_name'])
        p = ig.get(key) or next((v for k, v in ig.items() if k and (k in key or key in k)), None)
        v = iw.get(key) or next((x for k, x in iw.items() if k and (k in key or key in k)), None)
        if not p and not v:
            continue
        gmp = p['gmp'] if p else v['gmp']
        pct = p.get('pct') if p else None
        srcs = []
        if p:
            srcs.append(f"Investorgain ₹{p['gmp']:g}")
        if v:
            srcs.append(f"IPOWatch ₹{v['gmp']:g}")
        sources = ' · '.join(srcs)
        diverge = p and v and abs(p['gmp'] - v['gmp']) > max(2, 0.3 * max(p['gmp'], v['gmp'], 1))
        flag = '  [DIVERGE]' if diverge else ''
        print(f"  {c['company_name'][:32]:32s} GMP ₹{gmp:>6g}  ({sources}){flag}")
        if not DRY:
            requests.patch(f"{URL}/rest/v1/ipo_calendar?symbol=eq.{requests.utils.quote(c['symbol'])}",
                           headers={**H, 'Content-Type': 'application/json', 'Prefer': 'return=minimal'},
                           json={'gmp': gmp, 'gmp_pct': pct, 'gmp_updated': stamp, 'gmp_sources': sources}, timeout=20)
            # append one point per symbol per day for the GMP trend chart (upsert = idempotent)
            requests.post(f'{URL}/rest/v1/ipo_gmp_history?on_conflict=symbol,date',
                          headers={**H, 'Content-Type': 'application/json', 'Prefer': 'resolution=merge-duplicates,return=minimal'},
                          json={'symbol': c['symbol'], 'date': dt.date.today().isoformat(), 'gmp': gmp, 'gmp_pct': pct}, timeout=20)
            n += 1
    print(f'[gmp] {"dry-run" if DRY else f"updated {n}"} IPOs @ {stamp}')


if __name__ == '__main__':
    main()
