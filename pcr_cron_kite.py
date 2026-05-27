"""
pcr_cron_kite.py
================
Replacement for pcr_cron.py — fetches NIFTY weekly options OI via Kite Connect
and computes Put/Call ratio. Writes one row to nifty_pcr_history per run.

Was: NSE option-chain API → blocked by Akamai WAF (403 from GitHub Actions IPs).
Now: Kite Connect — works fine. Token expires daily; needs fresh push to
KITE_ACCESS_TOKEN secret each morning OR runs locally with credential file.

Schedule: every 10 min during market hours (9:15-15:30 IST weekdays).
"""
import os
import sys
import json
import time
import urllib.request
from datetime import datetime

try:
    from kiteconnect import KiteConnect
except ImportError:
    print('[ERR] pip install kiteconnect', file=sys.stderr)
    sys.exit(1)


def load_kite_credentials() -> dict:
    """Two paths: env vars (GitHub Actions secret) or local file."""
    api_key = os.environ.get('KITE_API_KEY')
    access_token = os.environ.get('KITE_ACCESS_TOKEN')
    if api_key and access_token:
        return {'api_key': api_key, 'access_token': access_token}
    # Fallback: local file (for dev runs from PC)
    cred_file = r'e:\Stocks sena\.kite-credentials'
    if not os.path.exists(cred_file):
        print('[ERR] No Kite credentials (env or .kite-credentials)', file=sys.stderr)
        sys.exit(1)
    creds = {}
    with open(cred_file, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or '=' not in line: continue
            k, v = line.split('=', 1)
            creds[k.strip()] = v.strip()
    return creds


def fetch_nifty_pcr(kite: KiteConnect) -> dict | None:
    """Fetch NIFTY options for nearest weekly expiry, compute aggregate PCR."""
    print('[fetch] Loading NFO instruments...')
    fno = kite.instruments('NFO')
    nifty = [i for i in fno
             if i.get('name') == 'NIFTY'
             and i.get('instrument_type') in ('CE', 'PE')
             and i.get('segment') == 'NFO-OPT']
    if not nifty:
        print('[ERR] No NIFTY options found in instruments', file=sys.stderr)
        return None
    # Find nearest expiry
    today = datetime.now().date()
    future_expiries = sorted({inst['expiry'] for inst in nifty if inst.get('expiry') and inst['expiry'] >= today})
    if not future_expiries:
        print('[ERR] No future expiries found', file=sys.stderr)
        return None
    nearest = future_expiries[0]
    print(f'[fetch] Nearest expiry: {nearest}')
    target = [i for i in nifty if i.get('expiry') == nearest]
    print(f'[fetch] Strikes in nearest expiry: {len(target)//2}')  # CE + PE per strike

    # Build instrument list for kite.quote() — needs full "NFO:tradingsymbol" keys
    pairs = [f"NFO:{i['tradingsymbol']}" for i in target]
    # Kite caps at 500 per call, batch
    quote_data: dict = {}
    for i in range(0, len(pairs), 250):
        batch = pairs[i:i + 250]
        try:
            q = kite.quote(batch)
            quote_data.update(q)
        except Exception as e:
            print(f'[ERR] kite.quote batch {i // 250}: {e}', file=sys.stderr)
            return None
        time.sleep(0.35)  # rate limit

    # Aggregate OI by CE / PE
    ce_oi = 0
    pe_oi = 0
    ce_count = 0
    pe_count = 0
    for inst in target:
        key = f"NFO:{inst['tradingsymbol']}"
        q = quote_data.get(key, {})
        oi = q.get('oi') or 0
        if inst['instrument_type'] == 'CE':
            ce_oi += oi
            ce_count += 1
        else:
            pe_oi += oi
            pe_count += 1

    if ce_oi == 0:
        print('[ERR] All CE OI is zero — Kite data issue?', file=sys.stderr)
        return None

    pcr = pe_oi / ce_oi
    # Get NIFTY spot for reference
    spot_quote = quote_data.get('NSE:NIFTY 50') or {}
    spot = spot_quote.get('last_price') or 0
    if not spot:
        try:
            s = kite.ltp(['NSE:NIFTY 50'])
            spot = s.get('NSE:NIFTY 50', {}).get('last_price', 0)
        except Exception:
            pass

    from datetime import timezone
    return {
        'captured_at': datetime.now(timezone.utc).isoformat(),
        'expiry': str(nearest),
        'spot': spot,
        'total_put_oi': pe_oi,
        'total_call_oi': ce_oi,
        'pcr': round(pcr, 4),
        'source': 'kite_connect',
    }


def upload_to_supabase(row: dict) -> bool:
    with open(r'e:\Stocks sena\.supabase-service-key') as f:
        key = f.read().strip()
    url = 'https://tbeadvvkqyrhtendttrg.supabase.co/rest/v1/nifty_pcr_history'
    h = {
        'apikey': key, 'Authorization': f'Bearer {key}',
        'Content-Type': 'application/json',
        'Prefer': 'return=minimal,resolution=merge-duplicates',
    }
    try:
        req = urllib.request.Request(url, data=json.dumps([row]).encode(), headers=h, method='POST')
        r = urllib.request.urlopen(req, timeout=15)
        return r.status in (200, 201)
    except urllib.error.HTTPError as e:
        body = e.read()[:300].decode('utf-8', 'ignore')
        print(f'[ERR] upload {e.code}: {body}', file=sys.stderr)
        return False
    except Exception as e:
        print(f'[ERR] upload: {e}', file=sys.stderr)
        return False


def main():
    creds = load_kite_credentials()
    kite = KiteConnect(api_key=creds['api_key'])
    kite.set_access_token(creds['access_token'])
    try:
        profile = kite.profile()
        print(f'[auth] OK — {profile.get("user_name")} ({profile.get("user_id")})')
    except Exception as e:
        print(f'[ERR] auth: {e}', file=sys.stderr)
        sys.exit(1)

    row = fetch_nifty_pcr(kite)
    if not row:
        print('[done] No PCR row to insert')
        sys.exit(1)

    print(f'[pcr] expiry={row["expiry"]} spot={row["spot"]} PCR={row["pcr"]:.4f} '
          f'(PE_OI={row["total_put_oi"]:,} CE_OI={row["total_call_oi"]:,})')

    if upload_to_supabase(row):
        print('[ok] inserted into nifty_pcr_history')
    else:
        print('[err] upload failed', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
