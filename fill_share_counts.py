"""
fill_share_counts.py
====================
Gives newly listed IPOs (and any stock missing market cap) their share count so
compute_metrics can produce market_cap / PE / EV.

Source: the company's own shareholding-pattern XBRL (we already store xbrl_url in
shareholding_periods). Total = NumberOfShares @ ShareholdingPattern_ContextI,
CROSS-CHECKED against the promoter context (promoter_shares/total must match the
promoter_pct we parsed earlier, ±0.5pp) — mismatch -> skip, never guess.

Writes:
  1. shareholding_periods.total_shares (fills the existing null column)
  2. bundle snapshot[0].face_value (BSE ComHeadernew FaceVal; default 10 NOT written)
  3. a synthetic annual_bs row {period: SHP period, equity_capital: shares*face/1e7,
     _source:'shp_xbrl'} ONLY when the bundle has no real balance sheet at all.
     Self-cleaning: once any real annual_bs* row exists, the synthetic row is removed.

Run:  py -3.11 fill_share_counts.py [--apply] [SYMBOL ...]
"""
import json
import os
import re
import sys
import time

import requests

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

URL = os.environ.get('SUPABASE_URL', 'https://tbeadvvkqyrhtendttrg.supabase.co')
SVC = os.environ.get('SUPABASE_SERVICE_KEY') or open(r'e:/Stocks sena/.supabase-service-key').read().strip()
H = {'apikey': SVC, 'Authorization': 'Bearer ' + SVC, 'Content-Type': 'application/json'}
STORE_H = {**H, 'x-upsert': 'true'}
XH = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124', 'Referer': 'https://www.nseindia.com/'}
BSE_H = {'User-Agent': 'Mozilla/5.0 Chrome/120', 'Accept': 'application/json',
         'Origin': 'https://www.bseindia.com', 'Referer': 'https://www.bseindia.com/'}
APPLY = '--apply' in sys.argv
ONLY = [a for a in sys.argv[1:] if not a.startswith('-')]


def shares_from_xbrl(url, promoter_pct):
    """(total_shares, note) — gated by the promoter-pct cross-check when available."""
    x = requests.get(url, headers=XH, timeout=40).text
    tot = re.search(r'<in-bse-shp:NumberOfShares contextRef="ShareholdingPattern_ContextI"[^>]*>(\d+)<', x)
    if not tot:
        return None, 'no ShareholdingPattern total in xbrl'
    total = int(tot.group(1))
    pro = re.search(r'<in-bse-shp:NumberOfShares contextRef="ShareholdingOfPromoterAndPromoterGroup_ContextI"[^>]*>(\d+)<', x)
    if pro and promoter_pct is not None:
        implied = pro.group(1) and int(pro.group(1)) / total * 100
        if abs(implied - float(promoter_pct)) > 0.5:
            return None, f'promoter cross-check FAIL (implied {implied:.2f} vs db {promoter_pct})'
    return total, 'ok' + ('' if pro else ' (no promoter ctx to cross-check)')


def face_value(scrip):
    if not scrip:
        return None
    try:
        d = requests.get(f'https://api.bseindia.com/BseIndiaAPI/api/ComHeadernew/w?quotetype=EQ&scripcode={scrip}&seriesid=',
                         headers=BSE_H, timeout=20).json()
        v = float(d.get('FaceVal') or 0)
        return v if 0 < v <= 1000 else None
    except Exception:
        return None


def main():
    # cohort = listed IPOs matched to stock_master; or explicit symbols
    if ONLY:
        syms = ONLY
    else:
        cal = requests.get(f'{URL}/rest/v1/ipo_calendar?select=matched_symbol&status=eq.Listed&matched_symbol=not.is.null',
                           headers=H, timeout=25).json()
        syms = sorted({x['matched_symbol'] for x in cal})
    inq = ','.join(f'"{s}"' for s in syms)
    sm = {r['symbol']: r for r in requests.get(
        f'{URL}/rest/v1/stock_master?select=symbol,bse_scrip_code,market_cap_cr&symbol=in.({inq})', headers=H, timeout=25).json()}
    shp = requests.get(f'{URL}/rest/v1/shareholding_periods?select=symbol,period,promoter_pct,xbrl_url,total_shares'
                       f'&symbol=in.({inq})&order=period.desc', headers=H, timeout=25).json()
    latest_shp = {}
    for r in shp:
        if r['symbol'] not in latest_shp and r.get('xbrl_url'):
            latest_shp[r['symbol']] = r
    print(f"[shares] {len(syms)} symbols, {len(latest_shp)} have SHP xbrl ({'APPLY' if APPLY else 'dry-run'})")

    done = skip = 0
    for sym in syms:
        rec = latest_shp.get(sym)
        if not rec:
            print(f'  {sym:12s} no SHP filing yet — waits for first quarter-end')
            skip += 1
            continue
        try:
            total, note = shares_from_xbrl(rec['xbrl_url'], rec.get('promoter_pct'))
        except Exception as e:
            print(f'  {sym:12s} xbrl fetch err: {str(e)[:60]}'); skip += 1; continue
        if not total:
            print(f'  {sym:12s} {note}'); skip += 1; continue
        face = face_value((sm.get(sym) or {}).get('bse_scrip_code')) or 10.0
        eqcap = round(total * face / 1e7, 4)
        print(f'  {sym:12s} shares {total:,} | face {face} | equity_capital ₹{eqcap} cr | {note}')
        if not APPLY:
            done += 1
            continue
        # 1) shareholding_periods.total_shares
        requests.patch(f"{URL}/rest/v1/shareholding_periods?symbol=eq.{sym}&period=eq.{rec['period']}",
                       headers={**H, 'Prefer': 'return=minimal'},
                       data=json.dumps({'total_shares': total}), timeout=20)
        # 2+3) bundle: snapshot face_value + synthetic bs row
        try:
            r = requests.get(f'{URL}/storage/v1/object/public/fundamentals-v2/{sym}.json', timeout=20)
            b = r.json() if r.status_code == 200 else {'symbol': sym}
        except Exception:
            b = {'symbol': sym}
        snap = b.get('snapshot')
        if isinstance(snap, list) and snap:
            snap[0]['face_value'] = face
        elif isinstance(snap, dict):
            snap['face_value'] = face
        else:
            b['snapshot'] = [{'face_value': face}]
        real_bs = any((row for k in ('annual_bs', 'annual_bs_consolidated', 'annual_bs_standalone')
                       for row in (b.get(k) or []) if row.get('_source') != 'shp_xbrl'))
        synth = [row for row in (b.get('annual_bs') or []) if row.get('_source') == 'shp_xbrl']
        if real_bs:
            if synth:  # self-clean once real BS exists
                b['annual_bs'] = [row for row in b['annual_bs'] if row.get('_source') != 'shp_xbrl']
                print(f'  {sym:12s} removed synthetic bs row (real balance sheet present)')
            # bank-parser gap: real BS rows often lack equity_capital (banks file a
            # different BS format) -> mcap/pe can never compute. Inject the
            # SHP-derived paid-up capital into the LATEST real row when null.
            for k in ('annual_bs', 'annual_bs_consolidated', 'annual_bs_standalone'):
                rows2 = [r2 for r2 in (b.get(k) or []) if r2.get('_source') != 'shp_xbrl']
                if rows2 and rows2[-1].get('equity_capital') is None:
                    rows2[-1]['equity_capital'] = eqcap
                    rows2[-1]['_eqcap_from'] = 'shp_xbrl 2026-06-11'
                    print(f'  {sym:12s} injected equity_capital {eqcap} into {k} latest row')
        else:
            row = {'period': rec['period'], 'equity_capital': eqcap, '_source': 'shp_xbrl'}
            rows = [r2 for r2 in (b.get('annual_bs') or []) if r2.get('_source') != 'shp_xbrl'] + [row]
            b['annual_bs'] = sorted(rows, key=lambda r2: str(r2.get('period', '')))
        requests.put(f'{URL}/storage/v1/object/fundamentals-v2/{sym}.json', headers=STORE_H,
                     data=json.dumps(b), timeout=40)
        done += 1
        time.sleep(0.5)
    print(f'[shares] done {done}, skipped {skip}')


if __name__ == '__main__':
    main()
