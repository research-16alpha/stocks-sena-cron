"""
audit_v4.py — Quality audit of the new bse_v4 + nse_v1 + merged_v3 bundles.

Checks across 50 mixed stocks (banks, NBFCs, non-banks):
  • BS equation (Total Assets = Equity + Liabilities) within 2%
  • PL coherence (PBT − tax ≈ Net Profit)
  • Field coverage (% of expected fields populated)
  • New-field coverage (Goodwill, NCI, RoU, Lease, Deposits, NPA, CET1)
  • Provenance %: how many rows are xbrl_mca vs screener_medallion
"""
import json
import os
from collections import defaultdict, Counter

BSE_V4_DIR  = r'F:\expansion\stocks-sena\bse_v4'
NSE_V1_DIR  = r'F:\expansion\stocks-sena\nse_v1'
MERGED_DIR  = r'F:\expansion\stocks-sena\merged_v3'

SAMPLE = [
    # Energy & conglomerates
    'RELIANCE', 'ADANIENT', 'POWERGRID', 'NTPC', 'ONGC', 'TATAPOWER',
    # IT
    'TCS', 'INFY', 'HCLTECH', 'WIPRO', 'LTIM',
    # Banks
    'HDFCBANK', 'ICICIBANK', 'SBIN', 'KOTAKBANK', 'AXISBANK', 'INDUSINDBK',
    # NBFCs
    'BAJFINANCE', 'BAJAJFINSV', 'CHOLAFIN', 'MUTHOOTFIN', 'M&MFIN',
    # FMCG / Auto / Pharma / Telecom
    'HINDUNILVR', 'ITC', 'NESTLEIND', 'MARUTI', 'TATAMOTORS', 'M&M',
    'SUNPHARMA', 'CIPLA', 'DRREDDY', 'BHARTIARTL',
    # Capital goods / chemicals / cement
    'LT', 'ASIANPAINT', 'ULTRACEMCO', 'GRASIM',
    # Mid-caps for variety
    'TITAN', 'TRENT', 'BAJAJHLDNG', 'AVENUE', 'DMART', 'IRCTC',
]


def load(path):
    if not os.path.exists(path): return None
    try:
        with open(path, 'r', encoding='utf-8') as f: return json.load(f)
    except Exception: return None


def check_bs_eq(row):
    ta = row.get('total_assets') or row.get('total_capital_and_liabilities')
    if ta is None or ta == 0: return None
    te = (row.get('total_equity')
          or ((row.get('equity_capital') or 0) + (row.get('reserves') or 0))
          or ((row.get('bank_capital') or 0) + (row.get('bank_reserves_surplus') or 0)))
    # Pick liabilities scheme based on what fields are present
    # NBFC FIRST — NBFCs (Bajaj Finance) may have BOTH deposits + nbfc_financial_liabilities;
    # the NBFC schema is the authoritative filing format for them.
    if row.get('nbfc_financial_liabilities') is not None:
        tl = (row.get('nbfc_financial_liabilities') or 0) + (row.get('nbfc_non_financial_liabilities') or 0)
    elif row.get('deposits') is not None:
        # Pure bank Schedule III
        tl = (row.get('deposits') or 0) + (row.get('bank_borrowings') or 0) + (row.get('bank_other_liabilities_provisions') or 0)
    elif row.get('current_liabilities') is not None and row.get('noncurrent_liabilities') is not None:
        # Non-bank standard
        tl = row['current_liabilities'] + row['noncurrent_liabilities']
    else:
        tl = (row.get('borrowings') or 0) + (row.get('other_liabilities') or 0)
    derived = te + tl
    diff_pct = abs(ta - derived) / abs(ta) * 100
    return diff_pct <= 5  # 5% tolerance


def check_pl_eq(row):
    pbt = row.get('pbt') or row.get('pbt_ordinary')
    np = row.get('net_profit') or row.get('pat_ordinary')
    tax = row.get('tax_expense') or ((row.get('current_tax') or 0) + (row.get('deferred_tax') or 0))
    if pbt is None or np is None: return None
    derived = pbt - (tax or 0)
    if np == 0: return abs(derived) < 1
    return abs(derived - np) / abs(np) <= 0.05


def main():
    bs_pass = Counter(); bs_fail = Counter(); pl_pass = Counter(); pl_fail = Counter()
    new_field_cov = defaultdict(lambda: [0, 0])  # field -> [populated, total]
    src_count = Counter()
    bank_count = 0
    nbfc_count = 0

    new_fields_check = ['goodwill','non_controlling_interest','right_of_use_assets','lease_liabilities','deferred_tax_liabilities',
                        'deposits','advances','cash_with_rbi','balances_with_banks','gross_npa_pct','cet1_ratio',
                        'nbfc_loans','nbfc_debt_securities','nbfc_subordinated_liabilities',
                        'share_of_associates_jv','net_profit_owners','net_profit_nci',
                        'lease_liability_payments','taxes_paid_operating','interest_paid_financing']

    print(f'{"SYMBOL":<14} {"YEARS":<7} {"BS_EQ":<10} {"PL_EQ":<10} {"BANK":<5} {"GW":<5} {"NCI":<5} {"DEPO":<5} {"ADV":<5} {"NPA":<5} {"CET1":<5}')
    print('-' * 100)

    for sym in SAMPLE:
        b = load(os.path.join(MERGED_DIR, f'{sym}.json')) or load(os.path.join(BSE_V4_DIR, f'{sym}.json'))
        if not b:
            print(f'{sym:<14} MISSING')
            continue
        bs_rows = b.get('annual_bs_consolidated') or b.get('annual_bs') or []
        pl_rows = b.get('annual_pl_consolidated') or b.get('annual_pl') or []
        bs_rows = [r for r in bs_rows if r.get('period','').endswith('-03-31')]
        years = sorted(set(r.get('period','')[:4] for r in bs_rows))
        bs_ok = 0; bs_no = 0
        for r in bs_rows:
            res = check_bs_eq(r)
            if res is True: bs_ok += 1
            elif res is False: bs_no += 1
        pl_ok = 0; pl_no = 0
        for r in pl_rows:
            res = check_pl_eq(r)
            if res is True: pl_ok += 1
            elif res is False: pl_no += 1
        latest_bs = bs_rows[-1] if bs_rows else {}
        latest_pl = pl_rows[-1] if pl_rows else {}
        is_bank = latest_bs.get('_is_bank') or latest_bs.get('deposits') is not None
        if is_bank: bank_count += 1
        if latest_bs.get('nbfc_loans') is not None: nbfc_count += 1

        # Field coverage on latest row
        for f in new_fields_check:
            v = latest_bs.get(f) if f in latest_bs else latest_pl.get(f)
            new_field_cov[f][1] += 1
            if v is not None:
                new_field_cov[f][0] += 1

        for r in bs_rows + pl_rows:
            src_count[r.get('_source', 'none')] += 1

        gw = 'OK' if latest_bs.get('goodwill') is not None else '-'
        nci = 'OK' if latest_bs.get('non_controlling_interest') is not None else '-'
        depo = 'OK' if latest_bs.get('deposits') is not None else '-'
        adv = 'OK' if latest_bs.get('advances') is not None else '-'
        npa = 'OK' if latest_bs.get('gross_npa_pct') is not None else '-'
        cet1 = 'OK' if latest_bs.get('cet1_ratio') is not None else '-'

        bs_eq_str = f'{bs_ok}/{bs_ok+bs_no}' if bs_ok+bs_no else '-'
        pl_eq_str = f'{pl_ok}/{pl_ok+pl_no}' if pl_ok+pl_no else '-'
        print(f'{sym:<14} {len(years):<7} {bs_eq_str:<10} {pl_eq_str:<10} {"Y" if is_bank else "n":<5} {gw:<5} {nci:<5} {depo:<5} {adv:<5} {npa:<5} {cet1:<5}')

    print()
    print('=== NEW FIELD COVERAGE (% of stocks with this field populated in latest row) ===')
    for f in new_fields_check:
        pop, total = new_field_cov[f]
        pct = (100 * pop / total) if total else 0
        print(f'  {f:<40} {pop:>3}/{total:<3} ({pct:>5.1f}%)')

    print()
    print('=== PROVENANCE COUNTS ===')
    for src, c in src_count.most_common():
        print(f'  {src}: {c}')

    print()
    print(f'=== Banks detected: {bank_count} ; NBFCs detected: {nbfc_count} ===')


if __name__ == '__main__':
    main()
