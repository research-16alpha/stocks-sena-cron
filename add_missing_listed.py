"""
add_missing_listed.py
=====================
One-off COVERAGE builder. Inserts stock_master rows for currently-listed (active)
companies in listing_master that we don't yet hold, resolving each to its Kite
instrument so the existing price + fundamentals crons pick them up immediately.

Why this exists: a coverage audit found ~1,050 active listed companies absent from
stock_master, including real liquid NSE names (Honeywell Automation, P&G Hygiene,
Castrol, NOCIL, Kalpataru, NLC India, MMTC...). They were simply never ingested.

Resolution (broker-tradeable only, NSE preferred):
  1. Kite NSE EQ tradingsymbol == listing_master.nse_symbol  (or the base before '-')
  2. Kite BSE EQ exchange_token == listing_master.bse_scrip_code
A company with no Kite instrument on either exchange is the genuinely-untradeable
tail (no broker price feed) -> skipped + reported, never faked.

Collision-safe: skips anything whose resolved symbol / isin / kite_token already
exists in stock_master, or collides within this batch. Never creates a duplicate.

New rows carry: symbol (=Kite tradingsymbol), name, isin, kite_token,
kite_tradingsymbol, kite_exchange, bse_scrip_code (BSE rows), is_active=True.
Everything else (price, mcap, fundamentals) is filled by the existing daily crons.

Run:  py -3.11 add_missing_listed.py            # DRY RUN: resolve + report, no writes
      py -3.11 add_missing_listed.py --commit   # insert the resolved rows
"""
import argparse, json, os, re

from kite_daily_update import kite, sb  # shared Kite client (vault token) + Supabase


def load_all(table, cols):
    out, off = [], 0
    while True:
        d = sb.table(table).select(cols).range(off, off + 999).execute().data or []
        out += d
        if len(d) < 1000:
            break
        off += 1000
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--commit', action='store_true', help='actually insert (default is dry-run)')
    args = ap.parse_args()

    # 1) Kite instrument masters (EQ only)
    nse = kite.instruments('NSE')
    bse = kite.instruments('BSE')
    nse_by_tsym = {i['tradingsymbol']: i for i in nse
                   if i.get('instrument_type') == 'EQ' and i.get('segment') == 'NSE'}
    bse_by_scrip = {str(i['exchange_token']): i for i in bse
                    if i.get('instrument_type') == 'EQ' and i.get('segment') == 'BSE'}
    print(f'[kite] {len(nse_by_tsym)} NSE EQ · {len(bse_by_scrip)} BSE EQ', flush=True)

    # 2) what we already have (collision guards)
    sm = load_all('stock_master', 'symbol,isin,kite_token,kite_tradingsymbol')
    have_sym = {(r['symbol'] or '').upper() for r in sm}
    have_kts = {(r['kite_tradingsymbol'] or '').upper() for r in sm if r.get('kite_tradingsymbol')}
    have_isin = {r['isin'] for r in sm if r.get('isin')}
    have_tok = {r['kite_token'] for r in sm if r.get('kite_token')}
    print(f'[sm] {len(sm)} existing rows', flush=True)

    # 3) active listed companies we do NOT already hold (by isin or symbol)
    lm = load_all('listing_master', 'isin,nse_symbol,bse_scrip_code,name,status')
    missing = [r for r in lm if r.get('status') == 'active'
               and r.get('isin') not in have_isin
               and (r.get('nse_symbol') or '').upper() not in have_sym]
    print(f'[lm] {len(missing)} active listed companies missing from stock_master', flush=True)

    # ETFs / mutual-fund units / rights / partly-paid trade as "EQ" on Kite but are NOT
    # companies -> never let them into stock_master.
    def is_not_a_company(tsym, kite_name, lm_name):
        u = f"{lm_name or ''} {kite_name or ''}".upper()
        if any(k in u for k in ('MUTUAL FUND', 'ETF', 'EXCHANGE TRADED', 'BEES',
                                'INDEX FUND', 'BHARAT BOND', 'GOLDBEES', 'LIQUIDBEES')):
            return True
        t = (tsym or '').upper()
        if re.search(r'(ETF|BEES|IETF|BETF|GETF|LIQ)$', t) or t.endswith('-PP') or '-RE' in t:
            return True
        return False

    rows, used_sym, used_isin, used_tok = [], set(), set(), set()
    n_nse = n_bse = n_unresolved = n_collide = n_fund = 0
    unresolved, samples, funds = [], [], []

    for m in missing:
        nsym = (m.get('nse_symbol') or '').strip()
        scrip = str(m.get('bse_scrip_code') or '').strip() or None
        isin = (m.get('isin') or '').strip() or None
        name = (m.get('name') or '').strip()

        inst = exch = None
        # NSE first: exact symbol, then base (drop series suffix like -B / -RE / -BE)
        for cand in filter(None, [nsym, nsym.split('-')[0] if '-' in nsym else None]):
            if cand in nse_by_tsym:
                inst, exch = nse_by_tsym[cand], 'NSE'; break
        # BSE fallback: scrip code == Kite BSE exchange_token
        if not inst and scrip and scrip in bse_by_scrip:
            inst, exch = bse_by_scrip[scrip], 'BSE'

        if not inst:
            n_unresolved += 1
            if len(unresolved) < 40:
                unresolved.append(f"{nsym or scrip or isin}: {name[:34]}")
            continue

        tsym = inst['tradingsymbol']
        tok = inst['instrument_token']
        kisin = isin  # listing_master isin is authoritative for these
        U = tsym.upper()
        if is_not_a_company(tsym, inst.get('name'), name):
            n_fund += 1
            if len(funds) < 20:
                funds.append(f"{exch} {tsym}: {name[:30]}")
            continue
        # collision: already in DB, or already chosen in this batch
        if (U in have_sym or U in have_kts or tok in have_tok or (kisin and kisin in have_isin)
                or U in used_sym or tok in used_tok or (kisin and kisin in used_isin)):
            n_collide += 1
            continue

        row = {'symbol': tsym, 'name': name or inst.get('name') or tsym,
               'kite_token': tok, 'kite_tradingsymbol': tsym, 'kite_exchange': exch,
               'is_active': True}
        if kisin:
            row['isin'] = kisin
        if exch == 'BSE' and scrip:
            row['bse_scrip_code'] = scrip
        rows.append(row)
        used_sym.add(U); used_tok.add(tok)
        if kisin:
            used_isin.add(kisin)
        if exch == 'NSE':
            n_nse += 1
        else:
            n_bse += 1
        if len(samples) < 30:
            samples.append(f"  {exch} {tsym:14} tok={tok:<9} {name[:38]}")

    print(f'\n[resolve] insertable={len(rows)}  (NSE={n_nse}, BSE={n_bse})  '
          f'collisions_skipped={n_collide}  etf/fund_excluded={n_fund}  '
          f'no_kite_instrument={n_unresolved}', flush=True)
    print(f'[sample etf/fund excluded] ({n_fund} total): ' + ' | '.join(funds[:14]))
    print('[sample insertable]'); print('\n'.join(samples))
    print(f'[sample NO kite instrument — the untradeable tail] ({n_unresolved} total):')
    print('   ' + ' | '.join(unresolved[:18]))
    json.dump(rows, open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
              '_add_missing_preview.json'), 'w'), indent=2)

    if not args.commit:
        print('\nDRY RUN — nothing written. Re-run with --commit to insert.')
        return

    ok = 0
    for i in range(0, len(rows), 200):
        chunk = rows[i:i + 200]
        try:
            sb.table('stock_master').insert(chunk).execute(); ok += len(chunk)
        except Exception as e:
            # fall back to row-by-row so one bad row doesn't drop the batch
            for r in chunk:
                try:
                    sb.table('stock_master').insert(r).execute(); ok += 1
                except Exception as e2:
                    print(f"  insert err {r['symbol']}: {str(e2)[:70]}")
    print(f'[OK] inserted {ok}/{len(rows)} new stock_master rows.')


if __name__ == '__main__':
    main()
