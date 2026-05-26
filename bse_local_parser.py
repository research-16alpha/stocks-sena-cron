"""
bse_local_parser.py — v2 (comprehensive extraction)
====================================================
Parses every available BSE filing format from the local mirror at
  D:\\BSE_SCRAPPER_DATA\\downloads\\financials\\{scrip_code}_{company_name}\\

Extracts EVERYTHING we can, including:
  • Annual + Quarterly P&L, BS, CF
  • CONSOLIDATED + STANDALONE stored separately (analysts pick which to use)
  • Segment data (per-segment revenue / profit / assets / liabilities)
  • Pre-computed ratios filed by the company (D/E, ISC, DSC)
  • Working capital changes, dividends paid, share buybacks (CF detail)
  • Reserves, total equity, lease liabilities (BS detail)
  • Older XBRL XML (in-bse-fin namespace, 2007-2020) — different tag names
  • CSV fallback for oldest filings (2007-2017)

File types handled:
  MC* (March Consolidated/Standalone, Audited iXBRL)  → Annual P&L + BS + CF
  MQ* (March Quarter, Audited iXBRL)                  → Q4 + Annual P&L (subset of MC)
  DQ/SQ/JQ (Quarterly, Unaudited XBRL XML)            → Quarterly P&L
  *.csv (legacy 2007-2017 quarterly key-value CSVs)   → Quarterly P&L
  *_results.csv                                       → Index/metadata only

Output JSON shape matches FundamentalsBundleV2 (see useStockFundamentals.ts):
  - annual_pl_consolidated[] / annual_pl_standalone[]
  - annual_bs_consolidated[] / annual_bs_standalone[]
  - annual_cf_consolidated[] / annual_cf_standalone[]
  - quarterly_results_consolidated[] / quarterly_results_standalone[]
  - annual_ratios[]
  - segments_annual[] / segments_quarterly[]
  - annual_pl / annual_bs / annual_cf / quarterly_results  (BACK-COMPAT pointers)
"""

import os
import re
import sys
import csv
import json
import glob
from collections import defaultdict
from typing import Dict, List, Optional, Tuple
from datetime import date

# ════════════════════════════════════════════════════════════════════════════
# REGEX PATTERNS
# ════════════════════════════════════════════════════════════════════════════

# iXBRL fact: <ix:nonFraction ... name='in-capmkt:TAG' contextRef='CTX' scale='S' sign='-' ...>VALUE</ix:nonFraction>
IXBRL_FACT_RE = re.compile(
    r"""<ix:nonFraction\b([^>]*)>([^<]*)</ix:nonFraction>""",
    re.IGNORECASE,
)
IXBRL_NONNUMERIC_RE = re.compile(
    r"""<ix:nonNumeric\b([^>]*)>([^<]*)</ix:nonNumeric>""",
    re.IGNORECASE,
)
ATTR_NAME = re.compile(r"""name=['"](?:in-capmkt|in-bse-fin):([A-Za-z]+)['"]""")
ATTR_CTX = re.compile(r"""contextRef=['"]([^'"]+)['"]""")
ATTR_SCALE = re.compile(r"""scale=['"](-?\d+)['"]""")
ATTR_SIGN = re.compile(r"""sign=['"]([-+])['"]""")

# Older BSE XBRL (in-bse-fin namespace, 2007-2020)
XBRL_BSEFIN_FACT_RE = re.compile(
    r"""<in-bse-fin:([A-Za-z]+)\s+[^>]*?contextRef=['"]([^'"]+)['"][^>]*?>([^<]*)</in-bse-fin:[A-Za-z]+>""",
)

# Context detection
CTX_DIM_EXPLICIT_RE = re.compile(
    r"""<xbrldi:explicitMember[^>]+dimension=['"][^'"]*?:([A-Za-z]+Axis)['"][^>]*>([^<]+)</xbrldi:explicitMember>"""
)
CTX_DIM_TYPED_RE = re.compile(
    r"""<xbrldi:typedMember[^>]+dimension=['"][^'"]*?:([A-Za-z]+Axis)['"][^>]*>"""
)


# ════════════════════════════════════════════════════════════════════════════
# CORE PARSING
# ════════════════════════════════════════════════════════════════════════════

def parse_xbrl_text(text: str) -> Tuple[Dict[str, Dict[str, str]], Dict[str, Dict]]:
    """
    Parse XBRL/iXBRL text → (facts_by_tag, contexts).

    facts[tag] = { ctx_id: value_str }
    contexts[ctx_id] = {
      type: 'period' | 'instant',
      start/end | date: 'YYYY-MM-DD',
      dim: True/False,                # whether context has any dimensions
      dimensions: { axis_name: member_value, ... } | None
    }
    """
    facts: Dict[str, Dict[str, str]] = defaultdict(dict)
    contexts: Dict[str, Dict] = {}

    # Pass 1: parse all contexts
    for m in re.finditer(
        r"<xbrli:context\s+id=['\"]([^'\"]+)['\"][^>]*>(.*?)</xbrli:context>",
        text, re.DOTALL,
    ):
        ctx_id = m.group(1)
        body = m.group(2)

        # Detect dimensions
        dimensions: Dict[str, str] = {}
        for dm in CTX_DIM_EXPLICIT_RE.finditer(body):
            axis_name = dm.group(1)
            member = dm.group(2).strip()
            # member may be like "in-capmkt:OperatingSegments1Member" — extract bare name
            if ':' in member:
                member = member.split(':', 1)[1]
            dimensions[axis_name] = member
        for dm in CTX_DIM_TYPED_RE.finditer(body):
            axis_name = dm.group(1)
            # Typed member values are arbitrary text — for our purposes mark as present
            dimensions.setdefault(axis_name, 'typed')

        is_dim = bool(dimensions)

        per_m = re.search(
            r"<xbrli:period>\s*<xbrli:startDate>([\d-]+)</xbrli:startDate>\s*<xbrli:endDate>([\d-]+)</xbrli:endDate>",
            body, re.DOTALL,
        )
        ins_m = re.search(
            r"<xbrli:period>\s*<xbrli:instant>([\d-]+)</xbrli:instant>",
            body, re.DOTALL,
        )
        if per_m:
            contexts[ctx_id] = {
                'type': 'period',
                'start': per_m.group(1),
                'end': per_m.group(2),
                'dim': is_dim,
                'dimensions': dimensions if is_dim else None,
            }
        elif ins_m:
            contexts[ctx_id] = {
                'type': 'instant',
                'date': ins_m.group(1),
                'dim': is_dim,
                'dimensions': dimensions if is_dim else None,
            }

    # Pass 2: iXBRL facts (newer files)
    for m in IXBRL_FACT_RE.finditer(text):
        attrs = m.group(1)
        raw = m.group(2).strip().replace(',', '').replace('(', '-').replace(')', '')
        name_m = ATTR_NAME.search(attrs)
        ctx_m = ATTR_CTX.search(attrs)
        if not name_m or not ctx_m:
            continue
        tag, ctx = name_m.group(1), ctx_m.group(1)
        scale_m = ATTR_SCALE.search(attrs)
        scale = int(scale_m.group(1)) if scale_m else 0
        sign_m = ATTR_SIGN.search(attrs)
        sign = -1 if (sign_m and sign_m.group(1) == '-') else 1
        try:
            val = float(raw) * (10 ** scale) * sign
            facts[tag][ctx] = str(val)
        except (ValueError, TypeError):
            facts[tag][ctx] = raw

    # Pass 3: older XBRL XML facts (in-bse-fin)
    for m in XBRL_BSEFIN_FACT_RE.finditer(text):
        tag, ctx, raw = m.group(1), m.group(2), m.group(3).strip()
        if ctx not in facts[tag]:
            facts[tag][ctx] = raw

    return facts, contexts


def num(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    s = str(s).strip().replace(',', '').replace('(', '-').replace(')', '')
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def to_crores(v: Optional[float]) -> Optional[float]:
    """Convert raw XBRL ₹ value to ₹ Cr. Only converts if magnitude clearly indicates absolute rupees."""
    if v is None or v == 0:
        return v
    if abs(v) > 1e6:
        return v / 1e7
    return v


def pick_top_level_period(contexts: Dict[str, Dict], target_days: Tuple[int, int]) -> Optional[str]:
    """Find the longest non-dimensional period whose duration falls in target_days range."""
    best_id = None
    best_days = 0
    for ctx_id, ctx in contexts.items():
        if ctx['type'] != 'period' or ctx.get('dim'):
            continue
        try:
            s = date.fromisoformat(ctx['start'])
            e = date.fromisoformat(ctx['end'])
            days = (e - s).days
            if target_days[0] <= days <= target_days[1] and days > best_days:
                best_days = days
                best_id = ctx_id
        except Exception:
            pass
    return best_id


def pick_top_level_instant(contexts: Dict[str, Dict], period_end: Optional[str] = None) -> Optional[str]:
    """Find the non-dimensional instant context matching period_end (or latest)."""
    best_id = None
    best_date = None
    for ctx_id, ctx in contexts.items():
        if ctx['type'] != 'instant' or ctx.get('dim'):
            continue
        if period_end and ctx['date'] == period_end:
            return ctx_id
        if best_date is None or ctx['date'] > best_date:
            best_date = ctx['date']
            best_id = ctx_id
    return best_id


# ════════════════════════════════════════════════════════════════════════════
# ANNUAL (MC files — iXBRL with full P&L + BS + CF)
# ════════════════════════════════════════════════════════════════════════════

def parse_annual_file(path: str) -> Optional[Dict]:
    """
    Parse one MC*.html (annual audited iXBRL).
    Returns dict with period + pl + bs + cf + ratios + segments.
    """
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            text = f.read()
    except Exception as e:
        print(f'[ERR] Read {path}: {e}', file=sys.stderr)
        return None

    facts, contexts = parse_xbrl_text(text)
    period_ctx = pick_top_level_period(contexts, (350, 380))  # ~12 months
    if not period_ctx:
        return None
    fy_end = contexts[period_ctx]['end']
    instant_ctx = pick_top_level_instant(contexts, fy_end)

    def fnum(tag: str, ctx: Optional[str]) -> Optional[float]:
        if not ctx:
            return None
        return num(facts.get(tag, {}).get(ctx))

    p, i = period_ctx, instant_ctx

    # ─── P&L ───
    revenue = fnum('RevenueFromOperations', p) or fnum('Income', p) or fnum('NetSalesRevenueFromOperations', p)
    other_income = fnum('OtherIncome', p)
    expenses = fnum('Expenses', p) or fnum('Expenditure', p)
    cost_materials = fnum('CostOfMaterialsConsumed', p)
    employee = fnum('EmployeeBenefitExpense', p) or fnum('StaffCost', p)
    finance_costs = fnum('FinanceCosts', p) or fnum('Interest', p)
    depreciation = fnum('DepreciationDepletionAndAmortisationExpense', p) or fnum('Depreciation', p)
    other_expenses = fnum('OtherExpenses', p) or fnum('OtherExpenditure', p)
    purchases = fnum('PurchasesOfStockInTrade', p)
    inv_change = fnum('ChangesInInventoriesOfFinishedGoodsWorkInProgressAndStockInTrade', p)
    pbt = fnum('ProfitBeforeTax', p)
    pbt_exceptional = fnum('ProfitBeforeExceptionalItemsAndTax', p)
    tax = fnum('TaxExpense', p)
    current_tax = fnum('CurrentTax', p)
    deferred_tax = fnum('DeferredTax', p)
    net_profit = fnum('ProfitLossForPeriod', p) or fnum('ProfitLossForPeriodFromContinuingOperations', p) or fnum('NetProfit', p)
    eps_basic = fnum('BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations', p) or fnum('BasicEarningsLossPerShareFromContinuingOperations', p) or fnum('BasicEPSBeforeExtraordinaryItems', p)
    eps_diluted = fnum('DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations', p) or fnum('DilutedEarningsLossPerShareFromContinuingOperations', p) or fnum('DilutedEPSBeforeExtraordinaryItems', p)
    exceptional = fnum('ExceptionalItemsBeforeTax', p)
    comp_income = fnum('ComprehensiveIncomeForThePeriod', p)
    other_comp_income = fnum('OtherComprehensiveIncomeNetOfTaxes', p)

    # EBITDA derive: PBT + Interest + Depreciation − OtherIncome
    ebitda = None
    if pbt is not None:
        parts = [pbt]
        if finance_costs is not None: parts.append(finance_costs)
        if depreciation is not None: parts.append(depreciation)
        if other_income is not None: parts.append(-other_income)
        ebitda = sum(parts)
    operating_profit = ebitda
    opm_pct = (ebitda / revenue * 100) if (ebitda is not None and revenue) else None
    tax_pct = (tax / pbt * 100) if (tax is not None and pbt) else None

    # ─── Balance Sheet (instant) ───
    total_assets = fnum('Assets', i)
    current_assets = fnum('CurrentAssets', i)
    equity_capital = fnum('EquityShareCapital', i) or fnum('PaidUpValueOfEquityShareCapital', i)
    borrowings_curr = fnum('BorrowingsCurrent', i)
    borrowings_noncurr = fnum('BorrowingsNoncurrent', i)
    total_borrowings = None
    if borrowings_curr is not None or borrowings_noncurr is not None:
        total_borrowings = (borrowings_curr or 0) + (borrowings_noncurr or 0)
    cash = fnum('CashAndCashEquivalents', i)
    bank_balance = fnum('BankBalanceOtherThanCashAndCashEquivalents', i)
    curr_invest = fnum('CurrentInvestments', i)
    noncurr_invest = fnum('NoncurrentInvestments', i)
    ppe = fnum('PropertyPlantAndEquipment', i)
    cwip = fnum('CapitalWorkInProgress', i)
    intangibles = fnum('OtherIntangibleAssets', i)
    intangibles_dev = fnum('IntangibleAssetsUnderDevelopment', i)
    curr_liab = fnum('CurrentLiabilities', i)
    noncurr_liab = fnum('NoncurrentLiabilities', i)
    trade_recv_curr = fnum('TradeReceivablesCurrent', i)
    trade_recv_noncurr = fnum('TradeReceivablesNoncurrent', i)
    trade_pay_curr = fnum('TradePayablesCurrent', i)
    trade_pay_noncurr = fnum('TradePayablesNoncurrent', i)
    inventories = fnum('Inventories', i)
    biological = fnum('BiologicalAssetsOtherThanBearerPlants', i)
    curr_tax_assets = fnum('CurrentTaxAssets', i)
    curr_tax_liab = fnum('CurrentTaxLiabilities', i)
    deferred_tax_assets = fnum('DeferredTaxAssets', i)

    # Reserves: typically derivable from total equity − share capital, but only
    # if total_equity is filed. Most filings have a separate Reserves tag too.
    reserves = fnum('Reserves', i) or fnum('OtherEquity', i)
    total_equity = fnum('Equity', i)
    if total_equity is None and equity_capital is not None and reserves is not None:
        total_equity = equity_capital + reserves

    total_investments = None
    if curr_invest is not None or noncurr_invest is not None:
        total_investments = (curr_invest or 0) + (noncurr_invest or 0)

    # ─── Cash Flow (period) ───
    cfo = fnum('CashFlowsFromUsedInOperatingActivities', p)
    cfi = fnum('CashFlowsFromUsedInInvestingActivities', p)
    cff = fnum('CashFlowsFromUsedInFinancingActivities', p)
    cfo_pre_wc = fnum('CashFlowsFromUsedInOperations', p)
    capex_ppe = fnum('PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities', p)
    capex_intang = fnum('PurchaseOfIntangibleAssetsClassifiedAsInvestingActivities', p)
    capex_intang_dev = fnum('PurchaseOfIntangibleAssetsUnderDevelopment', p)
    capex_total = None
    parts = [v for v in [capex_ppe, capex_intang, capex_intang_dev] if v is not None]
    if parts:
        capex_total = sum(abs(p_) for p_ in parts)
    proceeds_sale_ppe = fnum('ProceedsFromSalesOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities', p)
    proceeds_sale_intang = fnum('ProceedsFromSalesOfIntangibleAssetsClassifiedAsInvestingActivities', p)
    # Working capital adjustments
    wc_inventory = fnum('AdjustmentsForDecreaseIncreaseInInventories', p)
    wc_receivables = fnum('AdjustmentsForDecreaseIncreaseInTradeReceivablesCurrent', p)
    wc_payables = fnum('AdjustmentsForIncreaseDecreaseInTradePayablesCurrent', p)
    # Common addbacks
    dep_addback = fnum('AdjustmentsForDepreciationAndAmortisationExpense', p)
    finance_addback = fnum('AdjustmentsForFinanceCosts', p)
    # Dividend / financing detail
    dividend_paid = fnum('DividendsPaidClassifiedAsFinancingActivities', p) or fnum('DividendPaid', p)
    repayment_borrowings = fnum('RepaymentsOfBorrowingsClassifiedAsFinancingActivities', p)
    proceeds_borrowings = fnum('ProceedsFromBorrowingsClassifiedAsFinancingActivities', p)
    cash_end = fnum('CashAndCashEquivalentsCashFlowStatement', i)

    # ─── Ratios filed by company (pre-computed) ───
    de_ratio = fnum('DebtEquityRatio', p) or fnum('DebtEquityRatio', i)
    isc_ratio = fnum('InterestServiceCoverageRatio', p) or fnum('InterestServiceCoverageRatio', i)
    dsc_ratio = fnum('DebtServiceCoverageRatio', p) or fnum('DebtServiceCoverageRatio', i)

    # ─── Segment data ───
    segments = extract_segments(facts, contexts, p, i)

    return {
        'period': fy_end,
        'pl': {
            'period': fy_end,
            'sales': to_crores(revenue),
            'revenue_from_operations': to_crores(revenue),
            'expenses': to_crores(expenses),
            'cost_of_materials': to_crores(cost_materials),
            'employee_benefit_expense': to_crores(employee),
            'finance_costs': to_crores(finance_costs),
            'interest': to_crores(finance_costs),
            'depreciation': to_crores(depreciation),
            'other_expenses': to_crores(other_expenses),
            'purchases_stock_in_trade': to_crores(purchases),
            'changes_in_inventories': to_crores(inv_change),
            'other_income': to_crores(other_income),
            'ebitda': to_crores(ebitda),
            'operating_profit': to_crores(operating_profit),
            'opm_pct': opm_pct,
            'pbt': to_crores(pbt),
            'pbt_before_exceptional': to_crores(pbt_exceptional),
            'tax_expense': to_crores(tax),
            'current_tax': to_crores(current_tax),
            'deferred_tax': to_crores(deferred_tax),
            'tax_pct': tax_pct,
            'net_profit': to_crores(net_profit),
            'eps': eps_basic,
            'basic_eps': eps_basic,
            'diluted_eps': eps_diluted,
            'exceptional_items': to_crores(exceptional),
            'comprehensive_income': to_crores(comp_income),
            'other_comprehensive_income': to_crores(other_comp_income),
        },
        'bs': {
            'period': fy_end,
            'total_assets': to_crores(total_assets),
            'current_assets': to_crores(current_assets),
            'equity_capital': to_crores(equity_capital),
            'reserves': to_crores(reserves),
            'total_equity': to_crores(total_equity),
            'borrowings': to_crores(total_borrowings),
            'borrowings_current': to_crores(borrowings_curr),
            'borrowings_noncurrent': to_crores(borrowings_noncurr),
            'cash_equivalents': to_crores(cash),
            'bank_balance': to_crores(bank_balance),
            'current_investments': to_crores(curr_invest),
            'noncurrent_investments': to_crores(noncurr_invest),
            'investments': to_crores(total_investments),
            'fixed_assets': to_crores(ppe),
            'property_plant_equipment': to_crores(ppe),
            'cwip': to_crores(cwip),
            'intangible_assets': to_crores(intangibles),
            'intangibles_under_dev': to_crores(intangibles_dev),
            'inventories': to_crores(inventories),
            'current_liabilities': to_crores(curr_liab),
            'noncurrent_liabilities': to_crores(noncurr_liab),
            'other_liabilities': to_crores(curr_liab),
            'trade_receivables_current': to_crores(trade_recv_curr),
            'trade_receivables_noncurrent': to_crores(trade_recv_noncurr),
            'trade_receivables': to_crores((trade_recv_curr or 0) + (trade_recv_noncurr or 0)) if (trade_recv_curr or trade_recv_noncurr) else None,
            'trade_payables_current': to_crores(trade_pay_curr),
            'trade_payables_noncurrent': to_crores(trade_pay_noncurr),
            'trade_payables': to_crores((trade_pay_curr or 0) + (trade_pay_noncurr or 0)) if (trade_pay_curr or trade_pay_noncurr) else None,
            'biological_assets': to_crores(biological),
            'current_tax_assets': to_crores(curr_tax_assets),
            'current_tax_liabilities': to_crores(curr_tax_liab),
            'deferred_tax_assets': to_crores(deferred_tax_assets),
        },
        'cf': {
            'period': fy_end,
            'cfo': to_crores(cfo),
            'cfi': to_crores(cfi),
            'cff': to_crores(cff),
            'cfo_before_wc': to_crores(cfo_pre_wc),
            'capex': to_crores(capex_total),
            'capex_ppe': to_crores(capex_ppe),
            'capex_intangibles': to_crores(capex_intang),
            'proceeds_sale_ppe': to_crores(proceeds_sale_ppe),
            'proceeds_sale_intangibles': to_crores(proceeds_sale_intang),
            'wc_change_inventory': to_crores(wc_inventory),
            'wc_change_receivables': to_crores(wc_receivables),
            'wc_change_payables': to_crores(wc_payables),
            'depreciation_addback': to_crores(dep_addback),
            'finance_costs_addback': to_crores(finance_addback),
            'dividend_paid': to_crores(dividend_paid),
            'repayment_borrowings': to_crores(repayment_borrowings),
            'proceeds_borrowings': to_crores(proceeds_borrowings),
            'cash_at_year_end': to_crores(cash_end),
            'net_cash_flow': to_crores((cfo or 0) + (cfi or 0) + (cff or 0)) if any([cfo, cfi, cff]) else None,
        },
        'ratios': {
            'period': fy_end,
            'debt_equity_ratio': de_ratio,
            'interest_service_coverage_ratio': isc_ratio,
            'debt_service_coverage_ratio': dsc_ratio,
        },
        'segments': segments,
    }


# ════════════════════════════════════════════════════════════════════════════
# QUARTERLY (XBRL XML)
# ════════════════════════════════════════════════════════════════════════════

def parse_quarterly_file(path: str) -> Optional[Dict]:
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            text = f.read()
    except Exception:
        return None

    facts, contexts = parse_xbrl_text(text)
    p = pick_top_level_period(contexts, (80, 100))  # ~3 months
    if not p:
        return None
    period_end = contexts[p]['end']

    def fnum(tag: str) -> Optional[float]:
        return num(facts.get(tag, {}).get(p))

    revenue = fnum('RevenueFromOperations') or fnum('Income')
    if revenue is None:
        return None

    other_income = fnum('OtherIncome')
    expenses = fnum('Expenses')
    employee = fnum('EmployeeBenefitExpense')
    finance_costs = fnum('FinanceCosts')
    depreciation = fnum('DepreciationDepletionAndAmortisationExpense')
    pbt = fnum('ProfitBeforeTax')
    tax = fnum('TaxExpense')
    net_profit = fnum('ProfitLossForPeriod') or fnum('ProfitLossForPeriodFromContinuingOperations')

    ebitda = None
    if pbt is not None:
        parts = [pbt]
        if finance_costs is not None: parts.append(finance_costs)
        if depreciation is not None: parts.append(depreciation)
        if other_income is not None: parts.append(-other_income)
        ebitda = sum(parts)
    opm_pct = (ebitda / revenue * 100) if (ebitda is not None and revenue) else None
    tax_pct = (tax / pbt * 100) if (tax is not None and pbt) else None

    return {
        'period': period_end,
        'sales': to_crores(revenue),
        'revenue_from_operations': to_crores(revenue),
        'expenses': to_crores(expenses),
        'employee_benefit_expense': to_crores(employee),
        'operating_profit': to_crores(ebitda),
        'ebitda': to_crores(ebitda),
        'opm_pct': opm_pct,
        'other_income': to_crores(other_income),
        'interest': to_crores(finance_costs),
        'finance_costs': to_crores(finance_costs),
        'depreciation': to_crores(depreciation),
        'pbt': to_crores(pbt),
        'tax_expense': to_crores(tax),
        'tax_pct': tax_pct,
        'net_profit': to_crores(net_profit),
        'eps': fnum('BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations') or fnum('BasicEarningsLossPerShareFromContinuingOperations'),
        'basic_eps': fnum('BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations'),
        'diluted_eps': fnum('DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations'),
        'exceptional_items': to_crores(fnum('ExceptionalItemsBeforeTax')),
        'debt_equity_ratio': fnum('DebtEquityRatio'),
        'interest_service_coverage_ratio': fnum('InterestServiceCoverageRatio'),
        'debt_service_coverage_ratio': fnum('DebtServiceCoverageRatio'),
    }


# ════════════════════════════════════════════════════════════════════════════
# LEGACY CSV PARSER (oldest files, 2007-2017)
# ════════════════════════════════════════════════════════════════════════════

# Map CSV row labels → our normalized field names
CSV_LABEL_MAP = {
    'net sales/revenue from operations': 'sales',
    'other income': 'other_income',
    'total income': 'total_income',
    'expenditure': 'expenses',
    '(increase) / decrease in stock in trade & wip': 'changes_in_inventories',
    'consumption of raw materials': 'cost_of_materials',
    'depreciation': 'depreciation',
    'other expenditure': 'other_expenses',
    'purchases': 'purchases_stock_in_trade',
    'staff cost': 'employee_benefit_expense',
    'interest': 'interest',
    'profit (+)/ loss (-) from ordinary activities before tax': 'pbt',
    'tax': 'tax_expense',
    'current tax including fbt': 'current_tax',
    'deferred tax': 'deferred_tax',
    'net profit (+)/ loss (-) from ordinary activities after tax': 'net_profit',
    'net profit': 'net_profit',
    'equity capital': 'equity_capital',
    'face value (in rs)': 'face_value',
    'basic eps before extraordinary items': 'basic_eps',
    'diluted eps before extraordinary items': 'diluted_eps',
    'basic eps after extraordinary items': 'eps',
    'diluted eps after extraordinary items': 'diluted_eps_after',
}


def parse_csv_annual(path: str) -> Optional[Dict]:
    """
    Parse legacy MC*.csv format (FY07-FY17 annual audited).
    Returns dict with period + pl + bs (CF not available in this format).
    Schema is similar to JQ CSV but with annual numbers + basic BS items
    (equity capital, reserves).
    """
    rows = {}
    period_end = None
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            for line in csv.reader(f):
                if len(line) < 2:
                    continue
                key = line[0].strip().lower()
                val = line[1].strip()
                if key == 'date end':
                    try:
                        from datetime import datetime
                        period_end = datetime.strptime(val, '%d-%b-%y').date().isoformat()
                    except Exception:
                        pass
                # Map common annual fields (legacy CSV labels)
                if key in CSV_LABEL_MAP:
                    rows[CSV_LABEL_MAP[key]] = num(val)
                elif key in (
                    'net sales/revenue from operations',
                    'net sales / income from operations',
                ):
                    rows['sales'] = num(val)
                elif key in ('reserves', 'reserves and surplus'):
                    rows['reserves'] = num(val)
                elif key == 'equity capital':
                    rows['equity_capital'] = num(val)
                elif key == 'profit (+)/ loss (-) from ordinary activities before tax':
                    rows['pbt'] = num(val)
                elif key.startswith('basic & diluted eps') or key.startswith('basic eps'):
                    if 'eps' not in rows:
                        rows['eps'] = num(val)
                elif key in ('other income', 'other operating income'):
                    if 'other_income' not in rows or rows.get('other_income') is None:
                        rows['other_income'] = num(val)
                elif key in ('interest', 'finance costs'):
                    rows['interest'] = num(val)
                elif key in ('depreciation', 'depreciation and amortisation expense'):
                    rows['depreciation'] = num(val)
                elif key.startswith('expenditure'):
                    rows['expenses'] = num(val)
                elif key.startswith('net profit'):
                    if 'net_profit' not in rows or rows.get('net_profit') is None:
                        rows['net_profit'] = num(val)
    except Exception:
        return None

    if not period_end or rows.get('sales') is None:
        return None

    revenue = rows.get('sales')
    pbt = rows.get('pbt')
    finance_costs = rows.get('interest')
    if finance_costs is not None:
        finance_costs = abs(finance_costs)
    depreciation = rows.get('depreciation')
    if depreciation is not None:
        depreciation = abs(depreciation)
    other_income = rows.get('other_income')
    expenses = rows.get('expenses')
    if expenses is not None:
        expenses = abs(expenses)

    ebitda = None
    if pbt is not None:
        parts = [pbt]
        if finance_costs is not None: parts.append(finance_costs)
        if depreciation is not None: parts.append(depreciation)
        if other_income is not None: parts.append(-other_income)
        ebitda = sum(parts)

    return {
        'period': period_end,
        'pl': {
            'period': period_end,
            'sales': to_crores(revenue),
            'revenue_from_operations': to_crores(revenue),
            'expenses': to_crores(expenses),
            'other_income': to_crores(other_income),
            'ebitda': to_crores(ebitda),
            'operating_profit': to_crores(ebitda),
            'opm_pct': (ebitda / revenue * 100) if (ebitda is not None and revenue) else None,
            'interest': to_crores(finance_costs),
            'finance_costs': to_crores(finance_costs),
            'depreciation': to_crores(depreciation),
            'pbt': to_crores(pbt),
            'tax_expense': to_crores(rows.get('tax_expense')),
            'tax_pct': None,
            'net_profit': to_crores(rows.get('net_profit')),
            'eps': rows.get('eps') or rows.get('basic_eps'),
            'basic_eps': rows.get('basic_eps') or rows.get('eps'),
            'diluted_eps': rows.get('diluted_eps'),
            'source_format': 'legacy_csv',
        },
        'bs': {
            'period': period_end,
            'equity_capital': to_crores(rows.get('equity_capital')),
            'reserves': to_crores(rows.get('reserves')),
            'total_equity': to_crores((rows.get('equity_capital') or 0) + (rows.get('reserves') or 0))
                            if (rows.get('equity_capital') or rows.get('reserves')) else None,
            'source_format': 'legacy_csv',
            'note': 'Limited BS — pre-2018 XBRL only had equity items, full BS not in legacy CSV',
        },
        'cf': {
            'period': period_end,
            'note': 'Cash flow not available in pre-FY18 BSE XBRL format. Available only from FY24+ MC iXBRL.',
            'source_format': 'unavailable',
        },
    }


def parse_csv_quarterly(path: str) -> Optional[Dict]:
    """Parse the legacy JQ*.csv format (key-value pairs)."""
    rows = {}
    period_end = None
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            for line in csv.reader(f):
                if len(line) < 2:
                    continue
                key = line[0].strip().lower()
                val = line[1].strip()
                if key in ('date end',):
                    # Format: 30-Jun-08
                    try:
                        from datetime import datetime
                        period_end = datetime.strptime(val, '%d-%b-%y').date().isoformat()
                    except Exception:
                        pass
                elif key in CSV_LABEL_MAP:
                    rows[CSV_LABEL_MAP[key]] = num(val)
    except Exception:
        return None

    if not period_end or 'sales' not in rows or rows.get('sales') is None:
        return None

    revenue = rows.get('sales')
    pbt = rows.get('pbt')
    finance_costs = rows.get('interest')
    if finance_costs is not None:
        finance_costs = abs(finance_costs)  # interest in CSV is negative
    depreciation = rows.get('depreciation')
    if depreciation is not None:
        depreciation = abs(depreciation)
    other_income = rows.get('other_income')

    ebitda = None
    if pbt is not None:
        parts = [pbt]
        if finance_costs is not None: parts.append(finance_costs)
        if depreciation is not None: parts.append(depreciation)
        if other_income is not None: parts.append(-other_income)
        ebitda = sum(parts)

    return {
        'period': period_end,
        'sales': to_crores(revenue),
        'revenue_from_operations': to_crores(revenue),
        'expenses': to_crores(abs(rows['expenses']) if rows.get('expenses') is not None else None),
        'operating_profit': to_crores(ebitda),
        'ebitda': to_crores(ebitda),
        'opm_pct': (ebitda / revenue * 100) if (ebitda is not None and revenue) else None,
        'other_income': to_crores(other_income),
        'interest': to_crores(finance_costs),
        'finance_costs': to_crores(finance_costs),
        'depreciation': to_crores(depreciation),
        'pbt': to_crores(pbt),
        'tax_expense': to_crores(abs(rows['tax_expense']) if rows.get('tax_expense') is not None else None),
        'tax_pct': None,
        'net_profit': to_crores(rows.get('net_profit')),
        'eps': rows.get('eps') or rows.get('basic_eps'),
        'basic_eps': rows.get('basic_eps'),
        'diluted_eps': rows.get('diluted_eps'),
        'employee_benefit_expense': to_crores(abs(rows['employee_benefit_expense']) if rows.get('employee_benefit_expense') is not None else None),
        'cost_of_materials': to_crores(abs(rows['cost_of_materials']) if rows.get('cost_of_materials') is not None else None),
        'source_format': 'legacy_csv',
    }


# ════════════════════════════════════════════════════════════════════════════
# SEGMENT EXTRACTOR
# ════════════════════════════════════════════════════════════════════════════

def extract_segments(facts: Dict[str, Dict[str, str]], contexts: Dict[str, Dict],
                     period_ctx: str, instant_ctx: Optional[str]) -> List[Dict]:
    """
    Walk dimensional contexts where dimension == BusinessSegmentsAxis (or similar)
    and group facts per segment.
    """
    if not period_ctx:
        return []
    period_dates = contexts[period_ctx]
    segments_by_member: Dict[str, Dict] = {}

    # Find all dimensional contexts whose duration matches period_ctx
    target_start = period_dates.get('start')
    target_end = period_dates.get('end')

    for ctx_id, ctx in contexts.items():
        if not ctx.get('dim'):
            continue
        dims = ctx.get('dimensions') or {}
        # Look for a business segment axis
        segment_member = None
        for axis_name, member in dims.items():
            if 'Segment' in axis_name or 'BusinessSegment' in axis_name:
                segment_member = member
                break
        if not segment_member:
            continue

        # Match period (for period-type contexts) or instant (for instant-type)
        if ctx['type'] == 'period':
            if ctx.get('start') != target_start or ctx.get('end') != target_end:
                continue
        elif ctx['type'] == 'instant':
            if ctx.get('date') != target_end:
                continue

        # Pull segment facts associated with this ctx_id
        seg = segments_by_member.setdefault(segment_member, {'segment': segment_member})
        for tag in [
            'SegmentRevenue', 'SegmentRevenueFromOperations', 'InterSegmentRevenue',
            'SegmentProfitBeforeTax', 'SegmentProfitLossBeforeTaxAndFinanceCosts',
            'SegmentFinanceCosts', 'SegmentAssets', 'NetSegmentAssets',
            'SegmentLiabilities', 'NetSegmentLiabilities',
        ]:
            val = num(facts.get(tag, {}).get(ctx_id))
            if val is not None:
                # Snake-case the key
                key = re.sub(r'(?<!^)(?=[A-Z])', '_', tag).lower()
                seg[key] = to_crores(val)

    return list(segments_by_member.values())


# ════════════════════════════════════════════════════════════════════════════
# FOLDER-LEVEL ORCHESTRATION
# ════════════════════════════════════════════════════════════════════════════

def _is_consolidated(path: str) -> bool:
    return 'consolidated' in os.path.basename(path).lower()


def parse_stock_folder(folder: str) -> Optional[Dict]:
    """
    Parse one stock's BSE folder, extracting EVERYTHING.

    Returns:
      annual_pl_consolidated / annual_pl_standalone (and bs / cf / ratios variants)
      quarterly_results_consolidated / quarterly_results_standalone
      segments_annual / segments_quarterly
      annual_pl / annual_bs / annual_cf / quarterly_results  (back-compat aliases)
    """
    if not os.path.isdir(folder):
        return None
    folder_name = os.path.basename(folder)
    m = re.match(r'^(\d+)_(.+)$', folder_name)
    if not m:
        return None
    scrip_code, company_name = m.group(1), m.group(2)

    # Collect annual files (MC = annual audited)
    # FY24+ filed as iXBRL HTML, FY18-FY23 filed as XBRL XML, FY07-FY17 as legacy CSV
    mc_files = sorted(
        glob.glob(os.path.join(folder, f'{scrip_code}_MC*.html'))
        + glob.glob(os.path.join(folder, f'{scrip_code}_MC*.xml'))
    )
    mc_csv_legacy = sorted(glob.glob(os.path.join(folder, f'{scrip_code}_MC*.csv')))

    # Collect quarterly files (XBRL XML)
    quarterly_xml = sorted(
        glob.glob(os.path.join(folder, f'{scrip_code}_DQ*.xml'))
        + glob.glob(os.path.join(folder, f'{scrip_code}_SQ*.xml'))
        + glob.glob(os.path.join(folder, f'{scrip_code}_JQ*.xml'))
        + glob.glob(os.path.join(folder, f'{scrip_code}_MQ*.xml'))
    )

    # Legacy CSV quarterly (oldest data)
    csv_quarterly = sorted(
        glob.glob(os.path.join(folder, f'{scrip_code}_JQ*.csv'))
        + glob.glob(os.path.join(folder, f'{scrip_code}_SQ*.csv'))
        + glob.glob(os.path.join(folder, f'{scrip_code}_DQ*.csv'))
        + glob.glob(os.path.join(folder, f'{scrip_code}_MQ*.csv'))
    )

    # ── Annual: split consolidated vs standalone ──
    pl_c, pl_s = [], []
    bs_c, bs_s = [], []
    cf_c, cf_s = [], []
    ratios = []
    segments_annual = []
    seen_c, seen_s = set(), set()

    for mc in mc_files:
        is_cons = _is_consolidated(mc)
        rec = parse_annual_file(mc)
        if not rec:
            continue
        period = rec['period']
        if is_cons:
            if period in seen_c: continue
            seen_c.add(period)
            pl_c.append(rec['pl']); bs_c.append(rec['bs']); cf_c.append(rec['cf'])
            if rec.get('segments'):
                segments_annual.append({'period': period, 'basis': 'consolidated', 'segments': rec['segments']})
        else:
            if period in seen_s: continue
            seen_s.add(period)
            pl_s.append(rec['pl']); bs_s.append(rec['bs']); cf_s.append(rec['cf'])
            if rec.get('segments'):
                segments_annual.append({'period': period, 'basis': 'standalone', 'segments': rec['segments']})
        # Ratios — store once per period (prefer consolidated)
        if rec.get('ratios') and not any(r['period'] == period for r in ratios):
            ratios.append(rec['ratios'])

    # Legacy MC CSV (FY07-FY17) — fill in older years not covered by MC HTML/XML
    for csv_path in mc_csv_legacy:
        is_cons = _is_consolidated(csv_path)
        rec = parse_csv_annual(csv_path)
        if not rec:
            continue
        period = rec['period']
        if is_cons:
            if period in seen_c: continue
            seen_c.add(period)
            pl_c.append(rec['pl']); bs_c.append(rec['bs']); cf_c.append(rec['cf'])
        else:
            if period in seen_s: continue
            seen_s.add(period)
            pl_s.append(rec['pl']); bs_s.append(rec['bs']); cf_s.append(rec['cf'])

    for arr in (pl_c, pl_s, bs_c, bs_s, cf_c, cf_s, ratios):
        arr.sort(key=lambda r: r['period'])

    # ── Quarterly: split consolidated vs standalone ──
    q_c, q_s = [], []
    seen_q_c, seen_q_s = set(), set()

    for qf in quarterly_xml:
        is_cons = _is_consolidated(qf)
        rec = parse_quarterly_file(qf)
        if not rec:
            continue
        period = rec['period']
        if is_cons:
            if period not in seen_q_c:
                seen_q_c.add(period); q_c.append(rec)
        else:
            if period not in seen_q_s:
                seen_q_s.add(period); q_s.append(rec)

    # Legacy CSV — fill earliest quarterly history (only if not already covered)
    for cf_path in csv_quarterly:
        is_cons = _is_consolidated(cf_path)
        rec = parse_csv_quarterly(cf_path)
        if not rec:
            continue
        period = rec['period']
        if is_cons:
            if period not in seen_q_c:
                seen_q_c.add(period); q_c.append(rec)
        else:
            if period not in seen_q_s:
                seen_q_s.add(period); q_s.append(rec)

    q_c.sort(key=lambda r: r['period'])
    q_s.sort(key=lambda r: r['period'])

    # ── Back-compat aliases ──
    annual_pl = pl_c if pl_c else pl_s
    annual_bs = bs_c if bs_c else bs_s
    annual_cf = cf_c if cf_c else cf_s
    quarterly_results = q_c if q_c else q_s

    return {
        'scrip_code': scrip_code,
        'company_name': company_name,
        'snapshot': [],
        # Split-by-basis (new in v2)
        'annual_pl_consolidated': pl_c,
        'annual_pl_standalone': pl_s,
        'annual_bs_consolidated': bs_c,
        'annual_bs_standalone': bs_s,
        'annual_cf_consolidated': cf_c,
        'annual_cf_standalone': cf_s,
        'quarterly_results_consolidated': q_c,
        'quarterly_results_standalone': q_s,
        'segments_annual': segments_annual,
        # Pre-computed ratios (per period)
        'annual_ratios': ratios,
        # Back-compat (existing app reads these — point to consolidated if avail)
        'annual_pl': annual_pl,
        'annual_bs': annual_bs,
        'annual_cf': annual_cf,
        'quarterly_results': quarterly_results,
        'shareholding': [],
    }


# ════════════════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        sys.exit(1)
    folder = sys.argv[1]
    bundle = parse_stock_folder(folder)
    if bundle is None:
        print(f'No data parsed from {folder}', file=sys.stderr)
        sys.exit(1)
    print(json.dumps(bundle, indent=2, default=str))
