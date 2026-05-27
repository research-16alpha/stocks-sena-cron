"""
kite_backfill_ohlcv.py
======================
One-time deep backfill of historical OHLCV for EVERY stock in stock_master
using Zerodha Kite Connect API. After this runs, the `daily` bucket has full
historical data for ~100% of the universe.

Going forward, daily Yahoo cron (already deployed) just appends today's bar —
Kite is not used for daily delta.

CREDENTIAL HANDLING (security):
  - Reads API key + secret + access token from e:/Stocks sena/.kite-credentials
  - This file is local-only, .gitignored, NEVER pushed to any repo or cloud
  - Only the OHLCV DATA itself goes to Supabase (no credentials)

USAGE:
  1. Create e:/Stocks sena/.kite-credentials with:
       api_key=xxxx
       api_secret=xxxx
       access_token=xxxx
  2. Run: py kite_backfill_ohlcv.py

If access_token is missing or expired, the script prints the login URL.
Visit it, authorize, then paste the request_token from the redirect URL.
The script will exchange it for access_token and save back to credentials file.
"""
import os
import sys
import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple

try:
    from kiteconnect import KiteConnect
except ImportError:
    print('[ERR] pip install kiteconnect', file=sys.stderr)
    sys.exit(1)

CRED_FILE = r'e:\Stocks sena\.kite-credentials'
SUPABASE_KEY_FILE = r'e:\Stocks sena\.supabase-service-key'
SUPABASE_URL = 'https://tbeadvvkqyrhtendttrg.supabase.co'
BUCKET = 'daily'

# Kite historical_data official rate limit: 3 requests/second.
# We enforce this with a global token-bucket so total throughput across ALL
# workers stays ≤ 3 req/sec. Going over triggers NetworkException + data loss.
HIST_INTERVAL = 'day'
LOOKBACK_YEARS = 7  # covers pre-COVID through current — fits Supabase 1GB free tier
WORKERS = 3                # 3 workers * 1 token-per-sec budget = 3 req/sec
RATE_LIMIT_RPS = 3.0       # Kite historical-data documented max
MIN_INTERVAL = 1.0 / RATE_LIMIT_RPS  # 0.333s between requests globally

import threading
_rate_lock = threading.Lock()
_last_call_ts = [0.0]

def rate_limit_wait():
    """Block until the next request slot is available (global, thread-safe)."""
    with _rate_lock:
        now = time.time()
        wait = (_last_call_ts[0] + MIN_INTERVAL) - now
        if wait > 0:
            time.sleep(wait)
        _last_call_ts[0] = time.time()


def load_credentials() -> Dict[str, str]:
    """Load Kite credentials from local file. Returns dict or empty if missing."""
    creds = {}
    if not os.path.exists(CRED_FILE):
        return creds
    with open(CRED_FILE, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            creds[k.strip()] = v.strip()
    return creds


def save_credentials(creds: Dict[str, str]) -> None:
    with open(CRED_FILE, 'w', encoding='utf-8') as f:
        for k, v in creds.items():
            f.write(f'{k}={v}\n')


def ensure_kite_session(creds: Dict[str, str]) -> KiteConnect:
    """Return an authenticated KiteConnect client. Runs OAuth flow if needed."""
    if not creds.get('api_key'):
        print('[ERR] Missing api_key in .kite-credentials', file=sys.stderr)
        sys.exit(1)
    # api_secret only needed if we have to do OAuth flow (no access_token or expired)

    kite = KiteConnect(api_key=creds['api_key'])

    # Try existing access_token first
    if creds.get('access_token'):
        try:
            kite.set_access_token(creds['access_token'])
            # Sanity check — fetch profile to validate token
            profile = kite.profile()
            print(f"[auth] OK — logged in as {profile.get('user_name')} ({profile.get('user_id')})")
            return kite
        except Exception as e:
            print(f'[auth] Existing token invalid ({e}). Need fresh login.')

    # Need fresh OAuth flow — requires api_secret
    if not creds.get('api_secret'):
        print('[ERR] No valid access_token and no api_secret to do OAuth flow.', file=sys.stderr)
        print('      Either paste a fresh access_token, or add api_secret=xxxx to .kite-credentials.', file=sys.stderr)
        sys.exit(1)
    print()
    print('=' * 70)
    print('KITE LOGIN REQUIRED')
    print('=' * 70)
    print(f'1. Open this URL in your browser:')
    print(f'   {kite.login_url()}')
    print('2. Log in with your Kite credentials')
    print("3. After login you'll be redirected to your registered URL with ?request_token=XXX")
    print('4. Paste the request_token here:')
    print()
    rt = input('request_token: ').strip()
    if not rt:
        print('[ERR] No request_token provided. Aborting.', file=sys.stderr)
        sys.exit(1)
    data = kite.generate_session(rt, api_secret=creds['api_secret'])
    creds['access_token'] = data['access_token']
    save_credentials(creds)
    print(f'[auth] Access token saved to {CRED_FILE}')
    kite.set_access_token(data['access_token'])
    return kite


def fetch_supabase_symbols() -> List[str]:
    """All symbols from stock_master."""
    with open(SUPABASE_KEY_FILE, 'r') as f:
        key = f.read().strip()
    headers = {'apikey': key, 'Authorization': f'Bearer {key}'}
    syms = []
    offset = 0
    while True:
        url = f'{SUPABASE_URL}/rest/v1/stock_master?select=symbol&limit=1000&offset={offset}'
        r = urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20)
        batch = json.loads(r.read())
        if not batch:
            break
        syms.extend(s['symbol'] for s in batch)
        offset += 1000
        if len(batch) < 1000:
            break
    return syms


def build_kite_token_map(kite: KiteConnect) -> Dict[str, Tuple[int, str]]:
    """
    Returns {tradingsymbol: (instrument_token, exchange)} for NSE + BSE equities.
    For symbols listed on both exchanges, NSE wins (better volume).
    """
    print('[map] Fetching Kite NSE instruments...')
    nse = kite.instruments('NSE')
    print(f'[map] NSE: {len(nse)} instruments')
    print('[map] Fetching Kite BSE instruments...')
    bse = kite.instruments('BSE')
    print(f'[map] BSE: {len(bse)} instruments')

    mapping: Dict[str, Tuple[int, str]] = {}
    # NSE first (higher priority)
    for inst in nse:
        if inst.get('segment') != 'NSE' or inst.get('instrument_type') != 'EQ':
            continue
        sym = inst.get('tradingsymbol')
        token = inst.get('instrument_token')
        if sym and token:
            mapping[sym] = (token, 'NSE')
    # BSE fills the gap (BSE-only stocks)
    for inst in bse:
        if inst.get('segment') != 'BSE' or inst.get('instrument_type') != 'EQ':
            continue
        sym = inst.get('tradingsymbol')
        token = inst.get('instrument_token')
        scrip = inst.get('exchange_token')  # BSE-prefixed lookup
        if not (sym and token):
            continue
        if sym not in mapping:
            mapping[sym] = (token, 'BSE')
        # Also index by BSE scrip code (for our BSE500013-style symbols)
        if scrip:
            mapping[f'BSE{scrip}'] = (token, 'BSE')
            mapping[f'BSE_{scrip}'] = (token, 'BSE')
    print(f'[map] Combined mapping: {len(mapping)} unique entries')
    return mapping


KITE_MAX_DAYS_PER_CALL = 2000  # Kite caps daily interval to 2000 calendar days

def _fetch_chunk(kite: KiteConnect, instrument_token: int, from_date, to_date) -> Optional[list]:
    """Fetch one ≤2000-day chunk with rate limit + retry on rate-limit error."""
    rate_limit_wait()
    try:
        return kite.historical_data(
            instrument_token=instrument_token,
            from_date=from_date,
            to_date=to_date,
            interval='day',
        )
    except Exception as e:
        msg = str(e)
        if 'rate' in msg.lower() or '429' in msg or 'too many' in msg.lower():
            time.sleep(2.0)
            rate_limit_wait()
            try:
                return kite.historical_data(
                    instrument_token=instrument_token,
                    from_date=from_date, to_date=to_date, interval='day',
                )
            except Exception:
                return None
        # Other errors (instrument has no data, etc.) — log first one, suppress rest
        return None


def fetch_historical(kite: KiteConnect, instrument_token: int, years: int = LOOKBACK_YEARS) -> Optional[List[list]]:
    """Fetch daily OHLCV for one instrument, chunked into 2000-day windows."""
    end = datetime.now().date()
    earliest = end - timedelta(days=years * 365)
    all_rows = []
    cur_to = end
    while cur_to > earliest:
        cur_from = max(earliest, cur_to - timedelta(days=KITE_MAX_DAYS_PER_CALL - 1))
        chunk = _fetch_chunk(kite, instrument_token, cur_from, cur_to)
        if chunk is None:
            # If first chunk failed, abandon — instrument probably has no data
            if not all_rows:
                return None
            break
        if not chunk:
            break
        all_rows = chunk + all_rows
        # Step back one day before chunk start to avoid duplicate
        cur_to = cur_from - timedelta(days=1)
        if cur_from <= earliest:
            break

    if not all_rows:
        return None

    # Dedupe + sort (chunks may overlap by a day at boundaries)
    seen_dates = set()
    bars = []
    for row in all_rows:
        d = row['date'].strftime('%Y-%m-%d')
        if d in seen_dates:
            continue
        seen_dates.add(d)
        bars.append([
            d,
            round(float(row['open']), 2),
            round(float(row['high']), 2),
            round(float(row['low']), 2),
            round(float(row['close']), 2),
            int(row.get('volume') or 0),
        ])
    bars.sort(key=lambda r: r[0])
    return bars


def upload_to_supabase(symbol: str, bundle: dict) -> bool:
    with open(SUPABASE_KEY_FILE, 'r') as f:
        key = f.read().strip()
    payload = json.dumps(bundle, separators=(',', ':')).encode('utf-8')
    url = f'{SUPABASE_URL}/storage/v1/object/{BUCKET}/{symbol}.json'
    headers = {
        'apikey': key,
        'Authorization': f'Bearer {key}',
        'Content-Type': 'application/json',
        'x-upsert': 'true',
    }
    try:
        req = urllib.request.Request(url, data=payload, headers=headers, method='POST')
        r = urllib.request.urlopen(req, timeout=30)
        return r.status in (200, 201)
    except urllib.error.HTTPError as e:
        # 409 = exists, try PUT
        if e.code == 409:
            try:
                req = urllib.request.Request(url, data=payload, headers=headers, method='PUT')
                r = urllib.request.urlopen(req, timeout=30)
                return r.status in (200, 201)
            except Exception:
                return False
        print(f'  [UPLOAD ERR] {symbol}: HTTP {e.code}', file=sys.stderr)
        return False
    except Exception as e:
        print(f'  [UPLOAD ERR] {symbol}: {e}', file=sys.stderr)
        return False


def process_one(args) -> Tuple[str, str, int]:
    """Worker: fetch + upload one symbol. Returns (sym, status, bar_count)."""
    sym, token, exchange, kite = args
    bars = fetch_historical(kite, token)
    if not bars:
        return (sym, 'NO_DATA', 0)
    bundle = {
        'symbol': sym,
        'interval': '1d',
        'from': bars[0][0],
        'to': bars[-1][0],
        'bars': bars,
        'source': f'kite_{exchange.lower()}',
        'instrument_token': token,
    }
    ok = upload_to_supabase(sym, bundle)
    return (sym, 'OK' if ok else 'UPLOAD_ERR', len(bars))


def fetch_already_done_today() -> set:
    """List bundles in `daily` bucket that were uploaded TODAY (UTC).
    Skip these on resume — they're already fresh from this run."""
    with open(SUPABASE_KEY_FILE, 'r') as f:
        key = f.read().strip()
    headers = {'apikey': key, 'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'}
    today_utc = datetime.utcnow().strftime('%Y-%m-%d')
    done = set()
    offset = 0
    while True:
        req = urllib.request.Request(
            f'{SUPABASE_URL}/storage/v1/object/list/{BUCKET}',
            data=json.dumps({'prefix': '', 'limit': 1000, 'offset': offset}).encode(),
            headers=headers, method='POST',
        )
        r = urllib.request.urlopen(req, timeout=30)
        batch = json.loads(r.read())
        if not batch:
            break
        for f in batch:
            updated = (f.get('updated_at') or '')[:10]
            if updated == today_utc and f['name'].endswith('.json'):
                done.add(f['name'][:-5])
        if len(batch) < 1000:
            break
        offset += 1000
    return done


def main():
    creds = load_credentials()
    kite = ensure_kite_session(creds)

    # Check if --resume — skip stocks already done today (for graceful restart)
    resume = '--resume' in sys.argv or '--no-skip' not in sys.argv
    already_done = set()
    if resume:
        print('[resume] Checking which bundles were already updated today...')
        already_done = fetch_already_done_today()
        print(f'[resume] {len(already_done)} bundles already fresh — will skip')

    print()
    print('[load] Fetching stock_master symbols...')
    target_syms = fetch_supabase_symbols()
    print(f'[load] {len(target_syms)} symbols to backfill')

    kite_map = build_kite_token_map(kite)

    # Match our symbols to Kite tokens
    matched = []
    unmatched = []
    skipped_done = 0
    for sym in target_syms:
        if sym in already_done:
            skipped_done += 1
            continue
        if sym in kite_map:
            token, exch = kite_map[sym]
            matched.append((sym, token, exch))
        else:
            unmatched.append(sym)
    print(f'[match] matched={len(matched)} unmatched={len(unmatched)} skipped(already-done)={skipped_done}')
    if unmatched[:5]:
        print(f'  unmatched sample: {unmatched[:5]}')

    # Now fetch + upload
    t0 = time.time()
    ok = no_data = upload_err = 0
    work = [(sym, token, exch, kite) for sym, token, exch in matched]

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for i, fut in enumerate(as_completed([ex.submit(process_one, w) for w in work]), 1):
            try:
                sym, status, bars = fut.result()
            except Exception as e:
                print(f'  [EXCEPTION] {e}', file=sys.stderr)
                continue
            if status == 'OK':
                ok += 1
            elif status == 'NO_DATA':
                no_data += 1
            else:
                upload_err += 1
            if i % 100 == 0 or i <= 5:
                elapsed = time.time() - t0
                rate = i / elapsed if elapsed > 0 else 0
                eta = (len(work) - i) / rate if rate > 0 else 0
                print(f'  {i}/{len(work)}  ok={ok} no_data={no_data} upload_err={upload_err}  '
                      f'rate={rate:.1f}/s  ETA={eta:.0f}s')

    print()
    print('=' * 60)
    print(f'[done] ok={ok}  no_data={no_data}  upload_err={upload_err}')
    print(f'       unmatched (no Kite token): {len(unmatched)}')
    print(f'       elapsed: {time.time()-t0:.0f}s')


if __name__ == '__main__':
    main()
