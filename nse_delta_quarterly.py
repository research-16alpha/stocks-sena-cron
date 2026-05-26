"""
nse_delta_quarterly.py
======================
Daily delta cron for NSE quarterly results — uses NSE's results-comparision
endpoint which returns pre-parsed XBRL data directly (no download/parse needed).

Sources:
  NSE: https://www.nseindia.com/api/results-comparision?symbol=<SYMBOL>
       Returns latest 5 quarters of structured quarterly results with all
       re_* field names. Handles BOTH banks (bankNonBnking='B') and
       non-banks (bankNonBnking='NB'/None) with different field schemas.

Field mapping (re_* → our bundle fields):

  Non-bank:
    re_net_sale              -> sales
    re_oth_inc / re_oth_inc_new -> other_income
    re_tot_exp_exc_pro_cont  -> expenses
    re_depr_und_exp          -> depreciation
    re_int_new               -> finance_costs / interest
    re_pro_loss_bef_tax      -> pbt
    re_tax                   -> tax_expense
    re_net_profit            -> net_profit
    re_basic_eps             -> basic_eps / eps
    re_diluted_eps           -> diluted_eps

  Bank-additional:
    re_int_earned            -> interest_income (sales for banks)
    re_int_expd              -> interest_expended
    re_int_dis_adv_bills     -> interest_on_advances
    re_oper_exp              -> operating_expenses
    re_grs_npa_per           -> gross_npa_pct
    re_per_grs_npa           -> net_npa_pct
    re_cet_1_ret             -> cet1_ratio
    re_amt_grs_np_asst       -> total_assets (loosely; better than nothing)

Unit conversion: NSE returns values in LAKHS (1 lakh = 0.01 crore).
Divide by 100 to convert to crore (our standard unit).

Updates fundamentals-v2 bundle directly (no XBRL fetch).

Run:
  python nse_delta_quarterly.py                      # all stocks in bundle
  python nse_delta_quarterly.py --syms RELIANCE,TCS  # specific
  python nse_delta_quarterly.py --limit 50           # test
"""
import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fetch_new_filings import prime_nse_session  # reuse session priming

KEY = os.environ.get('SUPABASE_SERVICE_KEY')
if not KEY:
    try:
        with open('e:/Stocks sena/.supabase-service-key', 'r') as f:
            KEY = f.read().strip()
    except FileNotFoundError:
        print('[ERR] SUPABASE_SERVICE_KEY env var or local file required', file=sys.stderr)
        sys.exit(1)
URL = os.environ.get('SUPABASE_URL', 'https://tbeadvvkqyrhtendttrg.supabase.co')
H = {'apikey': KEY, 'Authorization': f'Bearer {KEY}'}

BUCKET = 'fundamentals-v2'
NSE_ENDPOINT = 'https://www.nseindia.com/api/results-comparision'

WORKERS = 4  # NSE is rate-sensitive; keep low
SLEEP_BETWEEN = 0.4

# How "fresh" a record must be to attempt merge (skip historical noise)
CUTOFF_DAYS_BACK = 540  # 18 months — covers latest 5-6 quarters


def parse_date(s: str):
    """NSE uses 'DD-MMM-YYYY'. Returns date or None."""
    if not s:
        return None
    for fmt in ('%d-%b-%Y', '%d-%B-%Y'):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def to_crore(v):
    """NSE values are in lakhs. Convert to crore by dividing by 100. None-safe."""
    if v is None or v == '' or v == '-':
        return None
    try:
        return round(float(v) / 100.0, 2)
    except (TypeError, ValueError):
        return None


def to_float(v):
    """Direct float conversion (for ratios/EPS). None-safe."""
    if v is None or v == '' or v == '-':
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def map_record_non_bank(rec: dict) -> dict:
    """Map NSE re_* record (non-bank) to our quarterly_results row schema."""
    return {
        'sales': to_crore(rec.get('re_net_sale')),
        'revenue_from_operations': to_crore(rec.get('re_net_sale')),
        'expenses': to_crore(rec.get('re_tot_exp_exc_pro_cont')),
        'operating_profit': None,  # computed below
        'opm_pct': None,
        'other_income': to_crore(rec.get('re_oth_inc') or rec.get('re_oth_inc_new')),
        'interest': to_crore(rec.get('re_int_new')),
        'finance_costs': to_crore(rec.get('re_int_new')),
        'depreciation': to_crore(rec.get('re_depr_und_exp')),
        'pbt': to_crore(rec.get('re_pro_loss_bef_tax')),
        'tax_expense': to_crore(rec.get('re_tax')),
        'current_tax': to_crore(rec.get('re_curr_tax')),
        'deferred_tax': to_crore(rec.get('re_deff_tax')),
        'tax_pct': None,  # computed below
        'net_profit': to_crore(rec.get('re_net_profit')),
        'eps': to_float(rec.get('re_basic_eps')),
        'basic_eps': to_float(rec.get('re_basic_eps')),
        'diluted_eps': to_float(rec.get('re_diluted_eps')),
        'face_value': to_float(rec.get('re_face_val')),
        'exceptional_items': to_crore(rec.get('re_excepn_items') or rec.get('re_excepn_items_new')),
    }


def map_record_bank(rec: dict) -> dict:
    """Map NSE re_* record (bank schema) to our quarterly_results row."""
    int_earned = to_crore(rec.get('re_int_earned'))
    return {
        # Banks report "interest income" as their top-line equivalent of sales
        'sales': int_earned,  # for compatibility with existing UI/queries
        'interest_income': int_earned,
        'interest_expended': to_crore(rec.get('re_int_expd')),
        'interest_on_advances': to_crore(rec.get('re_int_dis_adv_bills')),
        'income_on_investments': to_crore(rec.get('re_income_inv')),
        'total_income': to_crore(rec.get('re_tot_inc')),
        'other_income': to_crore(rec.get('re_oth_inc')),
        'operating_expenses': to_crore(rec.get('re_oper_exp')),
        'employee_benefit_expense': to_crore(rec.get('re_prov_emp_pay')),
        'operating_expenses_pre_provisions': to_crore(rec.get('re_oper_exp_bef_pro_cont')),
        'provisions_contingencies': to_crore(rec.get('re_oth_pro_cont')),
        'pbt': to_crore(rec.get('re_pro_loss_bef_tax')),
        'tax_expense': to_crore(rec.get('re_tax')),
        'net_profit': to_crore(rec.get('re_net_profit')),
        'eps': to_float(rec.get('re_basic_eps')),
        'basic_eps': to_float(rec.get('re_basic_eps')),
        'diluted_eps': to_float(rec.get('re_diluted_eps')),
        'face_value': to_float(rec.get('re_face_val')),
        'gross_npa': to_crore(rec.get('re_grs_npa')),
        'gross_npa_pct': to_float(rec.get('re_grs_npa_per')),
        'net_npa_pct': to_float(rec.get('re_per_grs_npa')),
        'cet1_ratio': to_float(rec.get('re_cet_1_ret')),
        'return_on_assets': to_float(rec.get('re_ret_asset')),
        'capital_adequacy_ratio': to_float(rec.get('re_cap_ade_rat')),
        'total_assets_amt': to_crore(rec.get('re_amt_grs_np_asst')),  # rough proxy
    }


def derive_metrics(row: dict, is_bank: bool):
    """Compute opm_pct, tax_pct if base fields available."""
    sales = row.get('sales')
    expenses = row.get('expenses')
    if sales and expenses and sales > 0:
        op_profit = sales - expenses
        row['operating_profit'] = round(op_profit, 2)
        row['opm_pct'] = round(op_profit / sales * 100, 2)
    pbt = row.get('pbt')
    tax = row.get('tax_expense')
    if pbt and pbt != 0 and tax is not None:
        row['tax_pct'] = round(tax / pbt * 100, 2)


def fetch_nse_results(symbol: str, session) -> dict:
    """Returns the raw JSON from results-comparision. {} on failure."""
    try:
        r = session.get(NSE_ENDPOINT, params={'symbol': symbol}, timeout=15)
        if r.status_code != 200:
            return {}
        return r.json()
    except Exception:
        return {}


def download_bundle(sym: str) -> dict:
    try:
        r = requests.get(f'{URL}/storage/v1/object/public/{BUCKET}/{sym}.json', timeout=20)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def upload_bundle(sym: str, bundle: dict) -> bool:
    payload = json.dumps(bundle, default=str, separators=(',', ':')).encode('utf-8')
    h = {**H, 'Content-Type': 'application/json', 'x-upsert': 'true'}
    url = f'{URL}/storage/v1/object/{BUCKET}/{sym}.json'
    try:
        r = requests.post(url, headers=h, data=payload, timeout=30)
        if r.status_code in (200, 201):
            return True
        r = requests.put(url, headers=h, data=payload, timeout=30)
        return r.status_code in (200, 201)
    except Exception:
        return False


def process_symbol(sym: str, session, force_overwrite: bool = False) -> tuple:
    """Fetch NSE quarterly, merge into bundle. Returns (sym, added, overwrote, status)."""
    nse = fetch_nse_results(sym, session)
    items = nse.get('resCmpData') or []
    if not items:
        return (sym, 0, 0, 'NO_NSE_DATA')

    is_bank = nse.get('bankNonBnking') == 'B'

    bundle = download_bundle(sym)
    if not bundle:
        return (sym, 0, 0, 'NO_BUNDLE')

    existing_q = bundle.get('quarterly_results') or []
    existing_periods = {r.get('period'): i for i, r in enumerate(existing_q)}

    added = 0
    overwrote = 0
    for rec in items:
        to_dt = parse_date(rec.get('re_to_dt'))
        from_dt = parse_date(rec.get('re_from_dt'))
        if not to_dt:
            continue
        period = to_dt.strftime('%Y-%m-%d')

        if is_bank:
            mapped = map_record_bank(rec)
        else:
            mapped = map_record_non_bank(rec)
        derive_metrics(mapped, is_bank)

        mapped['period'] = period
        mapped['_source'] = 'nse_delta_quarterly'
        mapped['_filed_at'] = rec.get('re_create_dt')
        mapped['_audited'] = rec.get('re_res_type') == 'A'
        mapped['_bank'] = is_bank

        if period in existing_periods:
            if force_overwrite:
                existing_q[existing_periods[period]] = mapped
                overwrote += 1
            # otherwise skip
        else:
            existing_q.append(mapped)
            added += 1

    if added == 0 and overwrote == 0:
        return (sym, 0, 0, 'NO_NEW')

    existing_q.sort(key=lambda r: r.get('period') or '')
    bundle['quarterly_results'] = existing_q
    bundle.setdefault('provenance', {})['last_nse_quarterly_delta'] = time.strftime(
        '%Y-%m-%dT%H:%M:%SZ', time.gmtime()
    )

    if upload_bundle(sym, bundle):
        return (sym, added, overwrote, 'OK')
    return (sym, added, overwrote, 'UPLOAD_ERR')


def fetch_v2_symbols() -> list:
    syms = []
    offset = 0
    while True:
        r = requests.post(
            f'{URL}/storage/v1/object/list/{BUCKET}',
            headers={**H, 'Content-Type': 'application/json'},
            json={'prefix': '', 'limit': 1000, 'offset': offset,
                  'sortBy': {'column': 'name', 'order': 'asc'}}, timeout=30,
        )
        b = r.json()
        if not b:
            break
        for o in b:
            n = o.get('name', '')
            if n.endswith('.json'):
                syms.append(n[:-5])
        if len(b) < 1000:
            break
        offset += 1000
    # Skip BSE_<scrip> — NSE endpoint won't have them
    syms = [s for s in syms if not s.startswith('BSE_') and not (s.startswith('BSE') and s[3:].isdigit())]
    return syms


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--syms', type=str, default='')
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--force', action='store_true',
                    help='Overwrite existing quarters even if periods already in bundle')
    args = ap.parse_args()

    if args.syms:
        syms = [s.strip().upper() for s in args.syms.split(',') if s.strip()]
    else:
        print('[INFO] Listing fundamentals-v2 bundles...')
        syms = fetch_v2_symbols()
    if args.limit:
        syms = syms[:args.limit]
    print(f'[INFO] Processing {len(syms)} symbols (BSE_<scrip> skipped)')

    session = prime_nse_session()

    ok = added_total = overwrote_total = no_data = no_bundle = upload_err = no_new = exc = 0
    t0 = time.time()

    def safe(sym):
        try:
            return process_symbol(sym, session, args.force)
        except Exception as e:
            return (sym, 0, 0, f'EXC:{e}')

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(safe, s): s for s in syms}
        for i, fut in enumerate(as_completed(futs), 1):
            sym, a, o, status = fut.result()
            if status == 'OK':
                ok += 1
                added_total += a
                overwrote_total += o
                if a > 0 or o > 0:
                    if i <= 30 or i % 100 == 0:
                        print(f'  [{i}/{len(syms)}] {sym:<14} +{a} new, ~{o} overwrote')
            elif status == 'NO_NEW':
                no_new += 1
            elif status == 'NO_NSE_DATA':
                no_data += 1
            elif status == 'NO_BUNDLE':
                no_bundle += 1
            elif status == 'UPLOAD_ERR':
                upload_err += 1
            else:
                exc += 1
            if i % 200 == 0:
                rate = i / (time.time() - t0)
                eta = (len(syms) - i) / rate if rate > 0 else 0
                print(f'  [{i}/{len(syms)}] ok={ok} +{added_total} no_data={no_data}  '
                      f'rate={rate:.1f}/s eta={eta:.0f}s')

    print()
    print('-' * 60)
    print(f'  Total              : {len(syms)}')
    print(f'  OK with updates    : {ok}')
    print(f'  + new periods      : {added_total}')
    print(f'  ~ overwrote periods: {overwrote_total}')
    print(f'  No new quarters    : {no_new}')
    print(f'  No NSE data        : {no_data}')
    print(f'  No bundle          : {no_bundle}')
    print(f'  Upload errors      : {upload_err}')
    print(f'  Exceptions         : {exc}')
    print(f'  Elapsed            : {time.time()-t0:.1f}s')


if __name__ == '__main__':
    main()
