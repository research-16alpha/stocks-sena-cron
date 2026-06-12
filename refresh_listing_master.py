"""
refresh_listing_master.py
=========================
DISCOVERY layer for the new-listing onboard. Refreshes the full NSE+BSE listing universe
into `listing_master` (PK = isin) so add_missing_listed.py can find newly-IPO'd companies.

Sources:  NSE EQUITY_L.csv  +  BSE ListofScripData
Fail-safe: if a source is unreachable (Cloudflare throttles Actions IPs) it skips that
exchange instead of wiping rows; if BOTH fail it aborts WITHOUT writing. Upsert merges,
never deletes — so a transient miss can't shrink the universe.

Run:  py refresh_listing_master.py          # write
      py refresh_listing_master.py --dry     # fetch + report, no write
"""
import argparse, csv, datetime, io, json, os, re, time
import requests

URL = os.environ.get('SUPABASE_URL', 'https://tbeadvvkqyrhtendttrg.supabase.co').rstrip('/')
KEY = os.environ.get('SUPABASE_SERVICE_KEY') or open(r'e:/Stocks sena/.supabase-service-key').read().strip()
SBH = {'apikey': KEY, 'Authorization': f'Bearer {KEY}'}
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'
BSE_H = {'User-Agent': UA, 'Accept': 'application/json', 'Origin': 'https://www.bseindia.com', 'Referer': 'https://www.bseindia.com/'}
BSE_MASTER = 'https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w'
NSE_EQUITY_L = 'https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv'


# BSE appends disclosure markers to scrip names ("Foo Ltd-$", "Bar Ltd*");
# strip them so display names stay clean everywhere downstream.
def clean_name(n):
    return re.sub(r'[\s\-]*[\$\*#]+\s*$', '', (n or '').strip()).strip()


def _retry(fn, n=4):
    last = None
    for i in range(n):
        try:
            return fn()
        except Exception as e:
            last = e; time.sleep(1.5 * (i + 1))
    raise last


def fetch_bse():
    s = requests.Session(); s.headers.update(BSE_H)
    try:
        s.get('https://www.bseindia.com/', timeout=15)
    except Exception:
        pass

    def go():
        r = s.get(BSE_MASTER, params={'Group': '', 'Scripcode': '', 'industry': '', 'segment': 'Equity', 'status': ''}, timeout=90)
        r.raise_for_status(); return r.json()
    return _retry(go)


def fetch_nse():
    def go():
        r = requests.get(NSE_EQUITY_L, headers={'User-Agent': UA}, timeout=30)
        r.raise_for_status(); return r.text
    out = []
    for row in csv.DictReader(io.StringIO(_retry(go))):
        isin = (row.get(' ISIN NUMBER') or row.get('ISIN NUMBER') or '').strip().upper()
        if not isin:
            continue
        out.append({'symbol': (row.get('SYMBOL') or '').strip(), 'name': (row.get('NAME OF COMPANY') or '').strip(),
                    'isin': isin, 'listing_date': (row.get(' DATE OF LISTING') or '').strip()})
    return out


def _date(s):
    for fmt in ('%d-%b-%Y', '%d-%b-%y'):
        try:
            return datetime.datetime.strptime(s, fmt).date().isoformat()
        except Exception:
            pass
    return None


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--dry', action='store_true'); a = ap.parse_args()
    bse = nse = None
    try:
        bse = fetch_bse(); print(f'[listing] BSE scrips: {len(bse)}', flush=True)
    except Exception as e:
        print(f'[listing] BSE fetch FAILED: {e}', flush=True)
    try:
        nse = fetch_nse(); print(f'[listing] NSE listed: {len(nse)}', flush=True)
    except Exception as e:
        print(f'[listing] NSE fetch FAILED: {e}', flush=True)
    if not bse and not nse:
        print('[listing] both sources down — ABORT (no write, universe preserved)'); return

    rows = {}

    def row(isin):
        return rows.setdefault(isin, {'isin': isin, 'nse_symbol': None, 'bse_scrip_code': None,
                                      'name': None, 'status': None, 'exchange': None, 'face_value': None, 'listing_date': None})

    for b in (bse or []):
        isin = (b.get('ISIN_NUMBER') or '').strip().upper()
        if not isin:
            continue
        st = (b.get('Status') or '').strip()
        r = row(isin)
        if r['bse_scrip_code'] is None or st == 'Active':  # prefer the Active row on duplicate ISIN
            r['bse_scrip_code'] = str(b.get('SCRIP_CD') or '').strip() or r['bse_scrip_code']
            r['name'] = clean_name(b.get('Issuer_Name') or b.get('Scrip_Name')) or r['name']
            r['status'] = 'active' if st == 'Active' else (st.lower() or r['status'])
            try:
                fv = b.get('FACE_VALUE')
                r['face_value'] = float(fv) if fv not in (None, '') else r['face_value']
            except Exception:
                pass
    for n in (nse or []):
        r = row(n['isin'])
        r['nse_symbol'] = n['symbol']
        r['name'] = r['name'] or clean_name(n['name'])
        r['status'] = 'active'  # presence in EQUITY_L == actively listed
        ld = _date(n['listing_date'])
        if ld:
            r['listing_date'] = ld

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    payload = []
    for isin, r in rows.items():
        r['exchange'] = 'NSE+BSE' if (r['nse_symbol'] and r['bse_scrip_code']) else ('NSE' if r['nse_symbol'] else 'BSE')
        r['status'] = r['status'] or 'active'
        r['fetched_at'] = now
        payload.append(r)
    active = sum(1 for r in payload if r['status'] == 'active')
    print(f'[listing] built {len(payload)} rows ({active} active)', flush=True)
    if a.dry:
        print('[listing] dry — nothing written'); return

    ok = 0
    for j in range(0, len(payload), 500):
        chunk = payload[j:j + 500]
        rr = requests.post(f'{URL}/rest/v1/listing_master?on_conflict=isin',
                           headers={**SBH, 'Content-Type': 'application/json', 'Prefer': 'resolution=merge-duplicates'},
                           data=json.dumps(chunk), timeout=60)
        if rr.status_code in (200, 201, 204):
            ok += len(chunk)
        else:
            print(f'  upsert err {rr.status_code}: {rr.text[:160]}', flush=True)
    print(f'[listing] upserted {ok}/{len(payload)} into listing_master', flush=True)


if __name__ == '__main__':
    main()
