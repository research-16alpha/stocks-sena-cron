"""
ipo_calendar_scrape.py  (v2 - NSE + BSE, complete coverage)
===========================================================
Refreshes public.ipo_calendar from BOTH exchanges' official feeds:

  NSE /api/all-upcoming-issues?category=ipo   upcoming/closed NSE issues (band, dates)
  NSE /api/ipo-current-issue                  live issues incl. NSE-SME, subscription
  BSE GetPublicIssue_par                      current/recent BSE public issues (IPO+SME;
                                              OFS/rights are EXCLUDED - not IPOs)
  BSE MoreCompanyN?Fromdt=YYYY&type=1         EVERY listing of the year (mainboard+SME)
                                              with issue price, listing date, listing-day
                                              close and current price - the official
                                              aftermarket record the /ipo page shows.

Listed rows are matched to stock_master by symbol, else by exact (case-insensitive)
company name, so the site can link to the stock page. Run from the laptop nightly
chain (NSE blocks cloud; BSE works anywhere but we keep one chain).

Run:  py -3.11 ipo_calendar_scrape.py [--dry-run]
"""
import datetime as dt
import json
import os
import re
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
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36'
_MON = {'JAN': '01', 'FEB': '02', 'MAR': '03', 'APR': '04', 'MAY': '05', 'JUN': '06',
        'JUL': '07', 'AUG': '08', 'SEP': '09', 'OCT': '10', 'NOV': '11', 'DEC': '12'}
TODAY = dt.date.today().isoformat()


def to_iso(d):
    try:
        day, mon, yr = str(d).strip().split('-')
        return f'{yr}-{_MON[mon.upper()]}-{int(day):02d}'
    except Exception:
        return None


def iso_dt(s):
    """'2026-06-08T00:00:00' -> '2026-06-08'."""
    s = str(s or '')
    return s[:10] if re.match(r'\d{4}-\d{2}-\d{2}', s) else None


def slug_key(name):
    return re.sub(r'[^A-Z0-9]+', '-', (name or '').upper()).strip('-')[:40]


def nse_session():
    s = requests.Session()
    s.headers.update({'User-Agent': UA, 'Accept': 'application/json',
                      'Referer': 'https://www.nseindia.com/', 'Accept-Language': 'en-US,en;q=0.9'})
    try:
        s.get('https://www.nseindia.com/', timeout=20)
    except Exception:
        pass
    return s


def bse_session():
    s = requests.Session()
    s.headers.update({'User-Agent': UA, 'Accept': 'application/json, text/plain, */*',
                      'Referer': 'https://www.bseindia.com/', 'Origin': 'https://www.bseindia.com'})
    return s


def norm_name(n):
    """Strict NORMALIZED-exact company-name key: lowercase, alphanumerics only,
    legal suffixes dropped. "Sai Parenteral's Limited" == "SAI PARENTERALS LTD".
    This is normalization of an exact match - NOT fuzzy scoring (house rule)."""
    s = (n or '').lower()
    s = re.sub(r'\((india|formerly[^)]*)\)', ' ', s)
    s = re.sub(r'[^a-z0-9]+', '', s)
    for suf in ('limited', 'ltd', 'pvtltd', 'privatelimited'):
        if s.endswith(suf):
            s = s[: -len(suf)]
    return s


def fetch_stock_names():
    """symbol + normalized-name maps for matching listed companies to our universe."""
    by_name, by_sym = {}, set()
    for off in range(0, 9000, 1000):
        try:
            rows = requests.get(f'{URL}/rest/v1/stock_master?select=symbol,name&limit=1000&offset={off}',
                                headers=H, timeout=30).json()
        except Exception:
            break
        if not isinstance(rows, list) or not rows:
            break
        for r in rows:
            if r.get('name'):
                by_name[norm_name(r['name'])] = r['symbol']
            by_sym.add(r.get('symbol'))
        if len(rows) < 1000:
            break
    return by_name, by_sym


def main():
    rows = {}

    def put(key, **kv):
        r = rows.get(key) or {'symbol': key}
        for k, v in kv.items():
            if v is not None or k not in r:
                r[k] = v
        rows[key] = r

    # ---- NSE: upcoming + current ----
    ns = nse_session()
    try:
        for x in ns.get('https://www.nseindia.com/api/all-upcoming-issues?category=ipo', timeout=25).json() or []:
            sym = (x.get('symbol') or '').strip().upper()
            if not sym:
                continue
            put(sym, company_name=(x.get('companyName') or sym).strip(), series=x.get('series'),
                issue_start=to_iso(x.get('issueStartDate')), issue_end=to_iso(x.get('issueEndDate')),
                price_band=x.get('issuePrice'), issue_size=str(x.get('issueSize') or '') or None,
                exch='NSE')
    except Exception as e:
        print('[warn] NSE upcoming:', str(e)[:80])
    try:
        for x in ns.get('https://www.nseindia.com/api/ipo-current-issue', timeout=25).json() or []:
            sym = (x.get('symbol') or '').strip().upper()
            if not sym:
                continue
            put(sym, company_name=(x.get('companyName') or sym).strip(), series=x.get('series'),
                issue_start=to_iso(x.get('issueStartDate')), issue_end=to_iso(x.get('issueEndDate')),
                shares_bid=str(x.get('noOfsharesBid') or '') or None,
                subscription={k: x.get(k) for k in ('noOfsharesBid', 'noOfsharesOffered', 'noOfTimesSubscribed') if x.get(k) is not None} or None,
                exch=('NSE+BSE' if str(x.get('isBse')) == '1' else 'NSE'))
    except Exception as e:
        print('[warn] NSE current:', str(e)[:80])

    # ---- BSE: current public issues (exclude OFS/rights - not IPOs) ----
    bs = bse_session()
    try:
        d = bs.get('https://api.bseindia.com/BseIndiaAPI/api/GetPublicIssue_par/w', timeout=25).json()
        items = d if isinstance(d, list) else next((v for v in d.values() if isinstance(v, list)), [])
        for x in items:
            # IR_FLAG_FULL taxonomy (observed): IPO = real IPO; OTB = buyback/open offer;
            # DPI = debt public issue; RI = rights; OFS = offer for sale; CMN = misc.
            # ONLY 'IPO' belongs on an IPO page.
            flag = (x.get('IR_FLAG_FULL') or x.get('IR_flag') or '').upper()
            if flag != 'IPO':
                continue
            sym = (x.get('short_name') or '').strip().upper() or slug_key(x.get('Scrip_Name'))
            if not sym:
                continue
            platform = 'SME' if 'SME' in (x.get('eXCHANGE_PLATFORM') or '').upper() else 'Mainboard'
            put(sym, company_name=(x.get('Scrip_Name') or sym).strip().title(),
                issue_start=iso_dt(x.get('Start_Dt')), issue_end=iso_dt(x.get('End_Dt')),
                price_band=x.get('Price_Band'), platform=platform, exch='BSE')
    except Exception as e:
        print('[warn] BSE issues:', str(e)[:80])

    # ---- BSE: ALL listings this year + last year (official aftermarket record) ----
    by_name, by_sym = fetch_stock_names()
    years = {TODAY[:4], str(int(TODAY[:4]) - 1)} if TODAY[5:7] == '01' else {TODAY[:4]}
    src_failed = False
    for yr in sorted(years):
        items = None
        for attempt in range(3):
            try:
                d = bs.get(f'https://api.bseindia.com/BseIndiaAPI/api/MoreCompanyN/w?Fromdt={yr}&company=&flag=1&type=1', timeout=30).json()
                items = d if isinstance(d, list) else next((v for v in d.values() if isinstance(v, list)), [])
                if items:
                    break
            except Exception as e:
                print(f'[warn] BSE listings {yr} try{attempt + 1}:', str(e)[:80])
            import time as _t
            _t.sleep(2 * (attempt + 1))
        if not items:
            src_failed = True
            print(f'[warn] BSE listings {yr}: EMPTY after retries - prune disabled this run')
            continue
        for x in items:
            name = re.sub(r'\s+', ' ', (x.get('CompanyName') or '')).strip()
            short = (x.get('Company_Short_Name') or '').strip().upper()
            matched = (short if short in by_sym else None) or by_name.get(norm_name(name))
            key = short or matched or slug_key(name)
            if not key:
                continue
            ip = x.get('IssuePrice')
            put(key, company_name=name or key, status='Listed',
                issue_price=ip, listed_on=iso_dt(x.get('ListedOn')),
                listing_day_close=x.get('ListingDayClose'), current_price=x.get('CurrentPrice'),
                matched_symbol=matched, exch=rows.get(key, {}).get('exch') or 'BSE')

    # ---- status normalisation for issue rows ----
    listed_syms = set()
    try:
        lm = requests.get(f'{URL}/rest/v1/listing_master?select=nse_symbol&listing_date=gte.{(dt.date.today()-dt.timedelta(days=200)).isoformat()}',
                          headers=H, timeout=25).json()
        listed_syms = {(r.get('nse_symbol') or '').upper() for r in lm}
    except Exception:
        pass
    for r in rows.values():
        if r.get('status') == 'Listed':
            continue
        st, en = r.get('issue_start'), r.get('issue_end')
        if r['symbol'] in listed_syms:
            r['status'] = 'Listed'
        elif st and st > TODAY:
            r['status'] = 'Upcoming'
        elif st and en and st <= TODAY <= en:
            r['status'] = 'Open'
        elif en and en < TODAY:
            r['status'] = 'Closed'

    # ---- self-heal: auto-onboard listed companies we don't track yet ----
    # A fresh listing (e.g. CMR Green, listed 2026-06-10) appears in BSE's feed before our
    # weekly onboarding cron runs. Resolve via Kite instruments (normalized-exact name
    # match) and insert the stock_master row so prices flow from the next tick.
    unmatched = [r for r in rows.values() if r.get('status') == 'Listed' and not r.get('matched_symbol')]
    if unmatched and not DRY:
        try:
            from kite_daily_update import kite, sb  # token from app_config (daily)
            insts = []
            for exch in ('NSE', 'BSE'):
                insts += [(exch, i) for i in kite.instruments(exch) if i.get('instrument_type') == 'EQ']
            by_kite = {}
            for exch, i in insts:
                k = norm_name(i.get('name'))
                if k and (k not in by_kite or exch == 'NSE'):   # prefer NSE listing
                    by_kite[k] = (exch, i)
            for r in unmatched:
                hit = by_kite.get(norm_name(r['company_name']))
                if not hit:
                    print(f"  [onboard] no Kite instrument for {r['company_name'][:40]} - skipped (untradeable?)")
                    continue
                exch, i = hit
                tsym = i['tradingsymbol']
                if sb.table('stock_master').select('symbol').eq('symbol', tsym).execute().data:
                    r['matched_symbol'] = tsym
                    continue
                row = {'symbol': tsym, 'name': r['company_name'], 'kite_token': i['instrument_token'],
                       'kite_tradingsymbol': tsym, 'kite_exchange': exch, 'is_active': True}
                if exch == 'BSE' and i.get('exchange_token'):
                    row['bse_scrip_code'] = str(i['exchange_token'])
                try:
                    sb.table('stock_master').insert(row).execute()
                    r['matched_symbol'] = tsym
                    print(f"  [onboard] ADDED {tsym} ({exch}) for {r['company_name'][:40]}")
                except Exception as e:
                    print(f"  [onboard] insert {tsym} failed: {str(e)[:70]}")
        except Exception as e:
            print('[warn] auto-onboard skipped:', str(e)[:90])

    KEYS = ['symbol', 'company_name', 'series', 'status', 'issue_start', 'issue_end',
            'price_band', 'issue_size', 'shares_bid', 'subscription', 'platform', 'exch',
            'issue_price', 'listed_on', 'listing_day_close', 'current_price', 'matched_symbol',
            'fetched_at']
    now = dt.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
    out = []
    for r in rows.values():
        r['fetched_at'] = now
        out.append({k: r.get(k) for k in KEYS})

    n_open = sum(1 for r in out if r['status'] == 'Open')
    n_up = sum(1 for r in out if r['status'] == 'Upcoming')
    n_listed = sum(1 for r in out if r['status'] == 'Listed')
    print(f'[ipo] {len(out)} rows: {n_open} open, {n_up} upcoming, {n_listed} listed '
          f'({sum(1 for r in out if r["status"] == "Listed" and r.get("matched_symbol"))} matched to stock pages)')
    for r in sorted([x for x in out if x['status'] != 'Listed'], key=lambda x: x.get('issue_start') or '')[-8:]:
        print(f"  {r['status']:9} {r['symbol'][:14]:14} {r['company_name'][:34]:34} {r.get('issue_start')} -> {r.get('issue_end')}")
    if DRY:
        return
    resp = requests.post(f'{URL}/rest/v1/ipo_calendar', headers={**H, 'Prefer': 'resolution=merge-duplicates,return=minimal'},
                         data=json.dumps(out, default=str), timeout=60)
    resp.raise_for_status()
    # prune rows this run did NOT produce (stale keys: slug rows that re-matched to real
    # symbols, NSE 'upcoming' that became BSE 'Listed' under another key). SAFETY LATCH:
    # never prune when a source failed or the listed count collapsed - a transient fetch
    # failure must NOT wipe good rows (it did once, 2026-06-10, before this latch).
    if src_failed or n_listed == 0:
        print(f'[ipo] upserted {len(out)} rows; prune SKIPPED (src_failed={src_failed}, listed={n_listed})')
    else:
        pr = requests.delete(f'{URL}/rest/v1/ipo_calendar?fetched_at=lt.{now}',
                             headers={**H, 'Prefer': 'return=representation'}, timeout=40)
        pruned = len(pr.json()) if pr.status_code == 200 else 0
        print(f'[ipo] upserted {len(out)} rows, pruned {pruned} stale')


if __name__ == '__main__':
    main()
