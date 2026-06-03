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

# BSE/SEBI XBRL XML (both old `in-bse-fin` namespace and new `in-capmkt` 2025 schema).
# Captures any element from these two namespaces with a contextRef + scale.
XBRL_BSEFIN_FACT_RE = re.compile(
    r"""<(?:in-bse-fin|in-capmkt):([A-Za-z]+)\s+([^>]*)>([^<]*)</(?:in-bse-fin|in-capmkt):[A-Za-z]+>""",
)
XBRL_BSEFIN_CTX_RE = re.compile(r"""contextRef=['"]([^'"]+)['"]""")
XBRL_BSEFIN_SCALE_RE = re.compile(r"""scale=['"](-?\d+)['"]""")
XBRL_BSEFIN_SIGN_RE = re.compile(r"""sign=['"]([-+])['"]""")
XBRL_BSEFIN_UNIT_RE = re.compile(r"""unitRef=['"]([^'"]+)['"]""")

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
    # iXBRL stores monetary values as (value × 10^scale) raw rupees with scale > 0.
    # To avoid the legacy `to_crores` magnitude heuristic missing tiny line items
    # (e.g. AMBALALSA NCI = 0.58 × 10^5 = 58,000 raw rupees, below the 1e6 threshold),
    # convert monetary facts to crores here. EPS / ratios / per-share values have
    # scale=0 (or absent) so they pass through unchanged.
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
            # Monetary iXBRL facts: scale > 0 means filed in raw rupees, convert to Cr.
            # This bypasses the magnitude heuristic in to_crores() which fails for
            # small line items in small companies (e.g. NCI = 58,000 raw rupees).
            if scale > 0:
                val = val / 1e7
            facts[tag][ctx] = str(val)
        except (ValueError, TypeError):
            facts[tag][ctx] = raw

    # Pass 3: XBRL XML facts (in-bse-fin or in-capmkt SEBI 2025 schema)
    # New SEBI 2025 XML files (e.g. SEBI Integrated Finance NBFC schema) use
    # scale attributes just like iXBRL HTML. Old BSE XML files don't — values
    # are absolute raw rupees. Apply scale only when present.
    for m in XBRL_BSEFIN_FACT_RE.finditer(text):
        tag, attrs, raw = m.group(1), m.group(2), m.group(3).strip()
        ctx_m = XBRL_BSEFIN_CTX_RE.search(attrs)
        if not ctx_m:
            continue
        ctx = ctx_m.group(1)
        if ctx in facts.get(tag, {}):
            continue  # iXBRL Pass 2 already populated this
        scale_m = XBRL_BSEFIN_SCALE_RE.search(attrs)
        sign_m = XBRL_BSEFIN_SIGN_RE.search(attrs)
        if scale_m:
            try:
                scale = int(scale_m.group(1))
                sign = -1 if (sign_m and sign_m.group(1) == '-') else 1
                val = float(raw.replace(',', '').replace('(', '-').replace(')', '')) * (10 ** scale) * sign
                if scale > 0:
                    val = val / 1e7
                facts[tag][ctx] = str(val)
            except (ValueError, TypeError):
                facts[tag][ctx] = raw
        else:
            # No scale: legacy in-bse-fin/in-capmkt XML. These file monetary values
            # as ABSOLUTE rupees (unitRef="INR"), so they must be /1e7 to reach Cr.
            # The old to_crores() magnitude heuristic (>1e6) silently failed for small
            # line items in small companies (e.g. STANPACK net profit 369000 rupees =
            # 0.0369 Cr stayed 369000). unitRef is the reliable signal: "INR" = currency
            # amount → /1e7; "INRPerShare" (EPS/face value) and ratios pass through.
            unit_m = XBRL_BSEFIN_UNIT_RE.search(attrs)
            unit = (unit_m.group(1) if unit_m else '').strip()
            if unit == 'INR':
                try:
                    v = float(raw.replace(',', '').replace('(', '-').replace(')', ''))
                    facts[tag][ctx] = str(v / 1e7)
                except (ValueError, TypeError):
                    facts[tag][ctx] = raw
            else:
                facts[tag][ctx] = raw

    # ── Recover contexts referenced by facts but never <xbrli:context>-defined.
    # Older SEBI taxonomy (in-bse-fin 2019-09-30, FY18-FY21) omits the non-dimensional
    # OneD/FourD/*I context definitions entirely - only dimensional contexts are declared,
    # yet facts still carry contextRef="FourD" (full-year YTD) / "OneD" (Q4) / "...I" (instant).
    # Reconstruct them from the always-present DateOf*FinancialYear / reporting-period facts
    # so pick_top_level_period() and the NSE-style fallback can find the annual context.
    # Year-end-agnostic: copies the filing's own declared dates (Dec year-ends work too).
    def _first_fact(tag):
        d = facts.get(tag) or {}
        return next(iter(d.values()), None)
    referenced = set()
    for _d in facts.values():
        referenced.update(_d.keys())
    undefined = referenced - set(contexts.keys())
    if undefined:
        fy_start = _first_fact('DateOfStartOfFinancialYear')
        fy_end = _first_fact('DateOfEndOfFinancialYear')
        rp_start = _first_fact('DateOfStartOfReportingPeriod')
        rp_end = _first_fact('DateOfEndOfReportingPeriod')
        for cid in undefined:
            if cid.endswith('I') and fy_end:
                contexts[cid] = {'type': 'instant', 'date': fy_end, 'dim': False, 'dimensions': None}
            elif cid in ('FourD', 'EightD') and fy_start and fy_end:
                contexts[cid] = {'type': 'period', 'start': fy_start, 'end': fy_end, 'dim': False, 'dimensions': None}
            elif rp_start and rp_end:
                contexts[cid] = {'type': 'period', 'start': rp_start, 'end': rp_end, 'dim': False, 'dimensions': None}

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
    """Convert raw XBRL ₹ value to ₹ Cr — ONLY a last-resort fallback for values that
    escaped the scale-aware fact extraction above (which already handles scale=/unitRef).

    The threshold MUST sit above the largest plausible value that is ALREADY in crores,
    else it double-divides big balance sheets. India's largest total_assets is SBI at
    ~7.3e6 cr (₹73 lakh cr); LIC ~5.6e6; HDFC Bank ~4e6. The old 1e6 threshold wrongly
    re-divided every bank / LIC / Reliance balance sheet by 1e7 (SBI 7,314,185 -> 0.73).
    1e8 clears all real crore values with margin, while genuine raw-rupee values that
    slipped through (a >=₹10 cr amount filed as absolute rupees = >=1e8) still convert."""
    if v is None or v == 0:
        return v
    if abs(v) > 1e8:
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

_NSE_FILENAME_RE = re.compile(r'Annual_\d{2}-[A-Za-z]{3}-(\d{4})_to_(\d{2})-([A-Za-z]{3})-(\d{4})_', re.IGNORECASE)
_MONTH_NUMS = {'jan':1,'feb':2,'mar':3,'apr':4,'may':5,'jun':6,'jul':7,'aug':8,'sep':9,'oct':10,'nov':11,'dec':12}


def parse_annual_file(path: str) -> Optional[Dict]:
    """
    Parse one annual XBRL filing.
    Supports two formats:
      • BSE iXBRL HTML (MC*.html) — has a single 350-380d period context
      • NSE XBRL XML (Annual_*_Audited.xml) — has Q4 + YTD contexts with
        identical date ranges; we read YTD from a context whose RevenueFromOperations
        value is at least 3.5× the Q-only value, OR fall back to "FourD"/"EightD"
        which are the standard NSE YTD-Q4 context IDs.
    Returns dict with period + pl + bs + cf + ratios + segments.
    """
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            text = f.read()
    except Exception as e:
        print(f'[ERR] Read {path}: {e}', file=sys.stderr)
        return None

    facts, contexts = parse_xbrl_text(text)

    # ── Step 1: try BSE format (single 350-380d period) ──
    period_ctx = pick_top_level_period(contexts, (350, 380))

    fy_end: Optional[str] = None
    instant_ctx: Optional[str] = None

    if period_ctx:
        fy_end = contexts[period_ctx]['end']
        instant_ctx = pick_top_level_instant(contexts, fy_end)
    else:
        # ── Step 2: try NSE format (Q4-audited filing with YTD context) ──
        # First check filename for FY-end hint
        fname = os.path.basename(path)
        m = _NSE_FILENAME_RE.search(fname)
        if m:
            mon_num = _MONTH_NUMS.get(m.group(3).lower(), 3)
            fy_end = f'{m.group(4)}-{mon_num:02d}-{int(m.group(2)):02d}'

        # Identify the YTD context by picking the period whose Revenue value
        # is largest (YTD ≥ Q4-only). This is robust even if context ID naming
        # varies across filers.
        revenue_facts = facts.get('RevenueFromOperations') or facts.get('Income') or facts.get('InterestEarned') or {}
        best_ctx = None
        best_val = 0.0
        for c, v in revenue_facts.items():
            ctx = contexts.get(c)
            if not ctx or ctx.get('dim') or ctx['type'] != 'period':
                continue
            try:
                val = abs(float(v))
            except Exception:
                continue
            if val > best_val:
                best_val = val
                best_ctx = c
        period_ctx = best_ctx

        # Bug 10 — Dec year-end MNCs: the period context's actual end date is the
        # ground truth. The filename hint may say "Mar" generically; do NOT let it
        # override a real Dec-31 (or other) period end from the XBRL context.
        if period_ctx and contexts.get(period_ctx, {}).get('end'):
            fy_end = contexts[period_ctx]['end']

        # Instant: prefer match on fy_end; else latest non-dim instant
        if fy_end:
            instant_ctx = pick_top_level_instant(contexts, fy_end)
        if not instant_ctx:
            instant_ctx = pick_top_level_instant(contexts)

    if not period_ctx:
        return None
    if fy_end is None:
        fy_end = contexts[period_ctx]['end']

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
    depreciation = fnum('DepreciationDepletionAndAmortisationExpense', p) or fnum('DepreciationAndAmortisationExpense', p) or fnum('Depreciation', p)
    other_expenses = fnum('OtherExpenses', p) or fnum('OtherExpenditure', p)
    purchases = fnum('PurchasesOfStockInTrade', p)
    inv_change = fnum('ChangesInInventoriesOfFinishedGoodsWorkInProgressAndStockInTrade', p)
    pbt = fnum('ProfitBeforeTax', p)
    pbt_exceptional = fnum('ProfitBeforeExceptionalItemsAndTax', p)
    tax = fnum('TaxExpense', p)
    current_tax = fnum('CurrentTax', p)
    deferred_tax = fnum('DeferredTax', p)
    # Banks file under ProfitLossFromOrdinaryActivitiesAfterTax instead of
    # ProfitLossForPeriod. Add that as a fallback so net_profit isn't 0 for banks.
    net_profit = (
        fnum('ProfitLossForPeriod', p)
        or fnum('ProfitLossForThePeriod', p)                          # old in-bse-fin 2019-09-30 taxonomy
        or fnum('ProfitLossForPeriodFromContinuingOperations', p)
        or fnum('ProfitLossForThePeriodFromContinuingOperations', p)  # old taxonomy
        or fnum('NetProfit', p)
        or fnum('ProfitLossFromOrdinaryActivitiesAfterTax', p)
    )
    eps_basic = fnum('BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations', p) or fnum('BasicEarningsLossPerShareFromContinuingOperations', p) or fnum('BasicEPSBeforeExtraordinaryItems', p)
    eps_diluted = fnum('DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations', p) or fnum('DilutedEarningsLossPerShareFromContinuingOperations', p) or fnum('DilutedEPSBeforeExtraordinaryItems', p)
    exceptional = fnum('ExceptionalItemsBeforeTax', p) or fnum('ExceptionalItems', p)
    comp_income = fnum('ComprehensiveIncomeForThePeriod', p)
    other_comp_income = fnum('OtherComprehensiveIncomeNetOfTaxes', p) or fnum('OtherComprehensiveIncome', p)
    # Consolidated extras — share of associates/JVs + owners/NCI split
    share_of_assoc = fnum('ShareOfProfitLossOfAssociatesAndJointVenturesAccountedForUsingEquityMethod', p) or fnum('ShareOfProfitLossOfAssociates', p)
    np_owners = fnum('ProfitOrLossAttributableToOwnersOfParent', p)
    np_nci = fnum('ProfitOrLossAttributableToNonControllingInterests', p) or fnum('ProfitLossOfMinorityInterest', p)
    comp_income_owners = fnum('ComprehensiveIncomeForThePeriodAttributableToOwnersOfParent', p)
    comp_income_nci = fnum('ComprehensiveIncomeForThePeriodAttributableToOwnersOfParentNonControllingInterests', p)
    discontinued_after_tax = fnum('ProfitLossFromDiscontinuedOperationsAfterTax', p)
    face_value_eq = fnum('FaceValueOfEquityShareCapital', p) or fnum('FaceValueOfEquityShareCapital', i)
    # Bank-style P&L (HDFCBANK, SBIN, ICICIBANK etc — same namespace, different tags)
    interest_earned = fnum('InterestEarned', p)
    interest_expended = fnum('InterestExpended', p)
    interest_on_advances = fnum('InterestOrDiscountOnAdvancesOrBills', p)
    interest_on_rbi = fnum('InterestOnBalancesWithReserveBankOfIndiaAndOtherInterBankFunds', p)
    income_on_invest = fnum('RevenueOnInvestments', p) or fnum('IncomeOnInvestments', p)
    other_interest = fnum('OtherInterest', p)
    bank_employees_cost = fnum('EmployeesCost', p)
    bank_operating_exp = fnum('OperatingExpenses', p)
    bank_other_op_exp = fnum('OtherOperatingExpenses', p)
    bank_total_exp = fnum('ExpenditureExcludingProvisionsAndContingencies', p)
    bank_opbpc = fnum('OperatingProfitBeforeProvisionAndContingencies', p)
    bank_provisions = fnum('ProvisionsOtherThanTaxAndContingencies', p)
    bank_total_income = fnum('Income', p)
    bank_pbt_ord = fnum('ProfitLossFromOrdinaryActivitiesBeforeTax', p)
    bank_pat_ord = fnum('ProfitLossFromOrdinaryActivitiesAfterTax', p)
    # NBFC-style P&L (Bajaj Finance, Cholafin etc)
    fee_commission_income = fnum('FeesAndCommissionIncome', p)
    fee_commission_expense = fnum('FeesAndCommissionExpense', p)
    dividend_income = fnum('DividendIncome', p)
    rental_income = fnum('RentalIncome', p)
    net_gain_fvtpl = fnum('NetGainOnFairValueChanges', p)
    net_loss_fvtpl = fnum('NetLossOnFairValueChanges', p)
    impairment_fin = fnum('ImpairmentOnFinancialInstruments', p)

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
    # NII = InterestEarned − InterestExpended (banks)
    nii = None
    if interest_earned is not None and interest_expended is not None:
        nii = interest_earned - interest_expended

    # Bank detection: presence of Deposits or InterestEarned signals a bank.
    # NBFC detection: presence of `Loans` BS tag with substantial size + no Deposits.
    is_bank_filing = (interest_earned is not None) or (fnum('Deposits', i) is not None and fnum('Deposits', i) > 0)

    # Bug 8 — bank "sales": banks don't file RevenueFromOperations. Total income =
    # InterestEarned + OtherIncome. Use it as the revenue/sales fallback when the
    # standard revenue tag is absent. `Income` (total income tag) already covers
    # most filers; this catches banks that omit even that tag.
    if is_bank_filing and revenue is None:
        if interest_earned is not None or other_income is not None:
            revenue = (interest_earned or 0) + (other_income or 0)
            opm_pct = (ebitda / revenue * 100) if (ebitda is not None and revenue) else None

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
    deferred_tax_assets = fnum('DeferredTaxAssets', i) or fnum('DeferredTaxAssetsNet', i)
    deferred_tax_liab = fnum('DeferredTaxLiabilities', i) or fnum('DeferredTaxLiabilitiesNet', i)
    # Ind AS 116 — Right-of-use assets + Lease liabilities are filed as separate
    # XBRL tags (when the filer's iXBRL schema supports it). Earlier filings
    # (FY20 and before) often roll RoU into PPE.
    rou_assets = fnum('RightOfUseAssets', i) or fnum('RightOfUseAsset', i)
    lease_liab_curr = fnum('LeaseLiabilitiesCurrent', i)
    lease_liab_noncurr = fnum('LeaseLiabilitiesNoncurrent', i)
    lease_liab_total = None
    if lease_liab_curr is not None or lease_liab_noncurr is not None:
        lease_liab_total = (lease_liab_curr or 0) + (lease_liab_noncurr or 0)

    # Reserves: typically derivable from total equity − share capital, but only
    # if total_equity is filed. Most filings have a separate Reserves tag too.
    reserves = fnum('Reserves', i) or fnum('OtherEquity', i) or fnum('ReserveExcludingRevaluationReserves', i) or fnum('ReservesAndSurplus', i)
    total_equity = fnum('Equity', i) or fnum('EquityAttributableToOwnersOfParent', i)
    if total_equity is None and equity_capital is not None and reserves is not None:
        total_equity = equity_capital + reserves

    total_investments = None
    if curr_invest is not None or noncurr_invest is not None:
        total_investments = (curr_invest or 0) + (noncurr_invest or 0)

    # ─── BS extras — Schedule III non-bank ───
    goodwill = fnum('Goodwill', i)
    investment_property = fnum('InvestmentProperty', i)
    equity_owners = fnum('EquityAttributableToOwnersOfParent', i)
    nci_balance = fnum('NonControllingInterest', i) or fnum('MinorityInterest', i)
    total_liab_filed = fnum('Liabilities', i)
    equity_and_liab = fnum('EquityAndLiabilities', i)
    noncurr_assets_total = fnum('NoncurrentAssets', i)
    loans_curr = fnum('LoansCurrent', i)
    loans_noncurr = fnum('LoansNoncurrent', i)
    fin_assets_curr = fnum('CurrentFinancialAssets', i)
    fin_assets_noncurr = fnum('NoncurrentFinancialAssets', i)
    fin_liab_curr = fnum('CurrentFinancialLiabilities', i)
    fin_liab_noncurr = fnum('NoncurrentFinancialLiabilities', i)
    provisions_curr = fnum('ProvisionsCurrent', i)
    provisions_noncurr = fnum('ProvisionsNoncurrent', i)
    other_fin_assets_curr = fnum('OtherCurrentFinancialAssets', i)
    other_fin_assets_noncurr = fnum('OtherNoncurrentFinancialAssets', i)
    other_fin_liab_curr = fnum('OtherCurrentFinancialLiabilities', i)
    other_fin_liab_noncurr = fnum('OtherNoncurrentFinancialLiabilities', i)
    other_curr_assets_misc = fnum('OtherCurrentAssets', i)
    other_noncurr_assets_misc = fnum('OtherNoncurrentAssets', i)
    other_curr_liab_misc = fnum('OtherCurrentLiabilities', i)
    other_noncurr_liab_misc = fnum('OtherNoncurrentLiabilities', i)
    held_for_sale = fnum('NoncurrentAssetsClassifiedAsHeldForSale', i)
    invest_equity_method = fnum('InvestmentsAccountedForUsingEquityMethod', i)

    # ─── BS extras — Bank Schedule III ───
    bank_capital = fnum('Capital', i)
    bank_reserves_surplus = fnum('ReservesAndSurplus', i)
    bank_deposits = fnum('Deposits', i)
    bank_borrowings = fnum('Borrowings', i)
    bank_other_liab_provisions = fnum('OtherLiabilitiesAndProvisions', i)
    bank_cash_rbi = fnum('CashAndBalancesWithReserveBankOfIndia', i)
    bank_balances_other_banks = fnum('BalancesWithBanksAndMoneyAtCallAndShortNotice', i)
    bank_advances = fnum('Advances', i)
    bank_investments = fnum('Investments', i)  # banks file investments as flat (no curr/noncurr split)
    bank_fixed_assets = fnum('FixedAssets', i)
    bank_other_assets = fnum('OtherAssets', i)
    bank_total_capital_liab = fnum('CapitalAndLiabilities', i)
    # Asset quality (often filed as 0 in annual XBRL; richer quarterly disclosures)
    gross_npa_amt = fnum('GrossNonPerformingAssets', p) or fnum('GrossNonPerformingAssets', i)
    nonperforming_assets = fnum('NonPerformingAssets', p) or fnum('NonPerformingAssets', i)
    gross_npa_pct = fnum('PercentageOfGrossNpa', p) or fnum('PercentageOfGrossNpa', i)
    net_npa_pct = fnum('PercentageOfNpa', p) or fnum('PercentageOfNpa', i)
    cet1_ratio = fnum('CET1Ratio', p) or fnum('CET1Ratio', i)
    addl_tier1 = fnum('AdditionalTier1Ratio', p) or fnum('AdditionalTier1Ratio', i)
    return_on_assets = fnum('ReturnOnAssets', p) or fnum('ReturnOnAssets', i)

    # ─── BS extras — NBFC Schedule III (Bajaj Finance etc) ───
    nbfc_loans = fnum('Loans', i)
    nbfc_fin_assets = fnum('FinanicalAssets', i) or fnum('FinancialAssets', i)  # note: BSE taxonomy has the typo
    nbfc_non_fin_assets = fnum('NonFinancialAssets', i)
    nbfc_fin_liab = fnum('FinancialLiabilities', i)
    nbfc_non_fin_liab = fnum('NonFinancialLiabilities', i)
    nbfc_other_fin_assets = fnum('OtherFinancialAssets', i)
    nbfc_other_fin_liab = fnum('OtherFinancialLiabilities', i)
    nbfc_provisions = fnum('Provisions', i)
    nbfc_debt_securities = fnum('DebtSecurities', i)
    nbfc_subordinated = fnum('SubordinatedLiabilities', i)
    nbfc_derivative_assets = fnum('DerivativeFinancialInstrumentsFinancialAssets', i)
    nbfc_derivative_liab = fnum('DerivativeFinancialInstrumentsFinancialLiabilities', i)

    # ─── Cash Flow (period) ───
    cfo = fnum('CashFlowsFromUsedInOperatingActivities', p)
    cfi = fnum('CashFlowsFromUsedInInvestingActivities', p)
    cff = fnum('CashFlowsFromUsedInFinancingActivities', p)
    cfo_pre_wc = fnum('CashFlowsFromUsedInOperations', p)
    # Bug 9 — capex PPE: the primary tag is missing in 10-15% of filings.
    # Fall back through alternative cash-flow capex tags before giving up.
    capex_ppe = (
        fnum('PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities', p)
        or fnum('CashOutflowForCapitalExpenditure', p)
        or fnum('PaymentsForPurchaseOfPropertyPlantAndEquipment', p)
        or fnum('AcquisitionsOfPropertyPlantAndEquipment', p)
    )
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
    interest_income_addback = fnum('AdjustmentsForInterestIncome', p)
    dividend_income_addback = fnum('AdjustmentsForDividendIncome', p)
    fx_addback = fnum('AdjustmentsForUnrealisedForeignExchangeLossesGains', p)
    sharebased_addback = fnum('AdjustmentsForSharebasedPayments', p)
    impairment_addback = fnum('AdjustmentsForImpairmentLossReversalOfImpairmentLossRecognisedInProfitOrLoss', p)
    # Extra WC adjustments
    wc_other_curr_assets = fnum('AdjustmentsForDecreaseIncreaseInOtherCurrentAssets', p)
    wc_other_curr_liab = fnum('AdjustmentsForIncreaseDecreaseInOtherCurrentLiabilities', p)
    wc_recv_noncurr = fnum('AdjustmentsForDecreaseIncreaseInTradeReceivablesNoncurrent', p)
    wc_pay_noncurr = fnum('AdjustmentsForIncreaseDecreaseInTradePayablesNoncurrent', p)
    wc_other_noncurr_assets = fnum('AdjustmentsForDecreaseIncreaseInOtherNoncurrentAssets', p)
    wc_other_noncurr_liab = fnum('AdjustmentsForIncreaseDecreaseInOtherNoncurrentLiabilities', p)
    wc_provisions_curr = fnum('AdjustmentsForProvisionsCurrent', p)
    wc_provisions_noncurr = fnum('AdjustmentsForProvisionsNoncurrent', p)
    # Operating cash receipts/payments (rare but valuable)
    cash_taxes_paid_op = fnum('IncomeTaxesPaidRefundClassifiedAsOperatingActivities', p)
    cash_int_paid_op = fnum('InterestPaidClassifiedAsOperatingActivities', p)
    cash_int_recv_op = fnum('InterestReceivedClassifiedAsOperatingActivities', p)
    cash_div_recv_op = fnum('DividendsReceivedClassifiedAsOperatingActivities', p)
    # Investing detail
    cash_int_recv_inv = fnum('InterestReceivedClassifiedAsInvestingActivities', p)
    cash_div_recv_inv = fnum('DividendsReceivedClassifiedAsInvestingActivities', p)
    invest_property_purchase = fnum('PurchaseOfInvestmentPropertyClassifiedAsInvestingActivities', p)
    invest_property_sale = fnum('ProceedsFromSalesOfInvestmentPropertyClassifiedAsInvestingActivities', p)
    proceeds_subsidiaries = fnum('ProceedsFromChangesInOwnershipInterestsInSubsidiaries', p)
    payments_subsidiaries = fnum('PaymentsFromChangesInOwnershipInterestsInSubsidiaries', p)
    # Financing detail
    cash_int_paid_fin = fnum('InterestPaidClassifiedAsFinancingActivities', p)
    lease_payments = fnum('PaymentsOfLeaseLiabilitiesClassifiedAsFinancingActivities', p)
    finance_lease_payments = fnum('PaymentsOfFinanceLeaseLiabilitiesClassifiedAsFinancingActivities', p)
    proceeds_shares = fnum('ProceedsFromIssuingSharesClassifiedAsFinancingActivities', p) or fnum('ProceedsFromIssuingShares', p)
    proceeds_debentures = fnum('ProceedsFromIssuingDebenturesNotesBondsEtc', p)
    share_buyback = fnum('PaymentsToAcquireOrRedeemEntitysShares', p)
    proceeds_stock_options = fnum('ProceedsFromExerciseOfStockOptions', p)
    # FX adjustment on cash + net change in cash
    fx_on_cash = fnum('EffectOfExchangeRateChangesOnCashAndCashEquivalents', p)
    net_change_cash = fnum('IncreaseDecreaseInCashAndCashEquivalents', p)
    net_change_cash_pre_fx = fnum('IncreaseDecreaseInCashAndCashEquivalentsBeforeEffectOfExchangeRateChanges', p)
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
            # Consolidated extras
            'share_of_associates_jv': to_crores(share_of_assoc),
            'net_profit_owners': to_crores(np_owners),
            'net_profit_nci': to_crores(np_nci),
            'comprehensive_income_owners': to_crores(comp_income_owners),
            'comprehensive_income_nci': to_crores(comp_income_nci),
            'discontinued_operations_after_tax': to_crores(discontinued_after_tax),
            'face_value': face_value_eq,
            # Bank-style P&L
            'interest_earned': to_crores(interest_earned),
            'interest_expended': to_crores(interest_expended),
            'net_interest_income': to_crores(nii),
            'interest_on_advances': to_crores(interest_on_advances),
            'interest_on_rbi_balances': to_crores(interest_on_rbi),
            'income_on_investments': to_crores(income_on_invest),
            'other_interest_income': to_crores(other_interest),
            'bank_total_income': to_crores(bank_total_income),
            'bank_employees_cost': to_crores(bank_employees_cost),
            'bank_operating_expenses': to_crores(bank_operating_exp),
            'bank_other_operating_expenses': to_crores(bank_other_op_exp),
            'bank_total_expenses': to_crores(bank_total_exp),
            'operating_profit_pre_provisions': to_crores(bank_opbpc),
            'provisions_other_than_tax': to_crores(bank_provisions),
            'pbt_ordinary': to_crores(bank_pbt_ord),
            'pat_ordinary': to_crores(bank_pat_ord),
            # NBFC-style income items
            'fee_commission_income': to_crores(fee_commission_income),
            'fee_commission_expense': to_crores(fee_commission_expense),
            'dividend_income': to_crores(dividend_income),
            'rental_income': to_crores(rental_income),
            'net_gain_fair_value': to_crores(net_gain_fvtpl),
            'net_loss_fair_value': to_crores(net_loss_fvtpl),
            'impairment_on_financial_instruments': to_crores(impairment_fin),
            '_is_bank': is_bank_filing,
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
            'deferred_tax_liabilities': to_crores(deferred_tax_liab),
            'right_of_use_assets': to_crores(rou_assets),
            'lease_liabilities_current': to_crores(lease_liab_curr),
            'lease_liabilities_noncurrent': to_crores(lease_liab_noncurr),
            'lease_liabilities': to_crores(lease_liab_total),
            # Non-bank BS extras
            'goodwill': to_crores(goodwill),
            'investment_property': to_crores(investment_property),
            'equity_attributable_to_owners': to_crores(equity_owners),
            'non_controlling_interest': to_crores(nci_balance),
            'total_liabilities_filed': to_crores(total_liab_filed),
            'equity_and_liabilities': to_crores(equity_and_liab),
            'noncurrent_assets_total': to_crores(noncurr_assets_total),
            'loans_current': to_crores(loans_curr),
            'loans_noncurrent': to_crores(loans_noncurr),
            'financial_assets_current': to_crores(fin_assets_curr),
            'financial_assets_noncurrent': to_crores(fin_assets_noncurr),
            'financial_liabilities_current': to_crores(fin_liab_curr),
            'financial_liabilities_noncurrent': to_crores(fin_liab_noncurr),
            'provisions_current': to_crores(provisions_curr),
            'provisions_noncurrent': to_crores(provisions_noncurr),
            'other_financial_assets_current': to_crores(other_fin_assets_curr),
            'other_financial_assets_noncurrent': to_crores(other_fin_assets_noncurr),
            'other_financial_liabilities_current': to_crores(other_fin_liab_curr),
            'other_financial_liabilities_noncurrent': to_crores(other_fin_liab_noncurr),
            'other_current_assets': to_crores(other_curr_assets_misc),
            'other_noncurrent_assets': to_crores(other_noncurr_assets_misc),
            'other_current_liabilities_misc': to_crores(other_curr_liab_misc),
            'other_noncurrent_liabilities_misc': to_crores(other_noncurr_liab_misc),
            'assets_held_for_sale': to_crores(held_for_sale),
            'investments_equity_method': to_crores(invest_equity_method),
            # Bank Schedule III BS
            'bank_capital': to_crores(bank_capital),
            'bank_reserves_surplus': to_crores(bank_reserves_surplus),
            'deposits': to_crores(bank_deposits),
            'bank_borrowings': to_crores(bank_borrowings),
            'bank_other_liabilities_provisions': to_crores(bank_other_liab_provisions),
            'cash_with_rbi': to_crores(bank_cash_rbi),
            'balances_with_banks': to_crores(bank_balances_other_banks),
            'advances': to_crores(bank_advances),
            'bank_investments': to_crores(bank_investments),
            'bank_fixed_assets': to_crores(bank_fixed_assets),
            'bank_other_assets': to_crores(bank_other_assets),
            'total_capital_and_liabilities': to_crores(bank_total_capital_liab),
            # Asset quality (banks)
            'gross_npa_amount': to_crores(gross_npa_amt),
            'non_performing_assets': to_crores(nonperforming_assets),
            'gross_npa_pct': gross_npa_pct,
            'net_npa_pct': net_npa_pct,
            'cet1_ratio': cet1_ratio,
            'additional_tier1_ratio': addl_tier1,
            'return_on_assets_pct': return_on_assets,
            # NBFC Schedule III BS (Bajaj Finance etc.)
            'nbfc_loans': to_crores(nbfc_loans),
            'nbfc_financial_assets': to_crores(nbfc_fin_assets),
            'nbfc_non_financial_assets': to_crores(nbfc_non_fin_assets),
            'nbfc_financial_liabilities': to_crores(nbfc_fin_liab),
            'nbfc_non_financial_liabilities': to_crores(nbfc_non_fin_liab),
            'nbfc_other_financial_assets': to_crores(nbfc_other_fin_assets),
            'nbfc_other_financial_liabilities': to_crores(nbfc_other_fin_liab),
            'nbfc_provisions': to_crores(nbfc_provisions),
            'nbfc_debt_securities': to_crores(nbfc_debt_securities),
            'nbfc_subordinated_liabilities': to_crores(nbfc_subordinated),
            'nbfc_derivative_assets': to_crores(nbfc_derivative_assets),
            'nbfc_derivative_liabilities': to_crores(nbfc_derivative_liab),
            '_is_bank': is_bank_filing,
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
            'interest_income_addback': to_crores(interest_income_addback),
            'dividend_income_addback': to_crores(dividend_income_addback),
            'fx_addback': to_crores(fx_addback),
            'sharebased_payments_addback': to_crores(sharebased_addback),
            'impairment_addback': to_crores(impairment_addback),
            'wc_change_other_curr_assets': to_crores(wc_other_curr_assets),
            'wc_change_other_curr_liab': to_crores(wc_other_curr_liab),
            'wc_change_receivables_noncurrent': to_crores(wc_recv_noncurr),
            'wc_change_payables_noncurrent': to_crores(wc_pay_noncurr),
            'wc_change_other_noncurr_assets': to_crores(wc_other_noncurr_assets),
            'wc_change_other_noncurr_liab': to_crores(wc_other_noncurr_liab),
            'wc_change_provisions_current': to_crores(wc_provisions_curr),
            'wc_change_provisions_noncurrent': to_crores(wc_provisions_noncurr),
            'taxes_paid_operating': to_crores(cash_taxes_paid_op),
            'interest_paid_operating': to_crores(cash_int_paid_op),
            'interest_received_operating': to_crores(cash_int_recv_op),
            'dividend_received_operating': to_crores(cash_div_recv_op),
            'interest_received_investing': to_crores(cash_int_recv_inv),
            'dividend_received_investing': to_crores(cash_div_recv_inv),
            'investment_property_purchase': to_crores(invest_property_purchase),
            'investment_property_sale': to_crores(invest_property_sale),
            'proceeds_from_subsidiaries': to_crores(proceeds_subsidiaries),
            'payments_to_subsidiaries': to_crores(payments_subsidiaries),
            'interest_paid_financing': to_crores(cash_int_paid_fin),
            'lease_liability_payments': to_crores(lease_payments),
            'finance_lease_payments': to_crores(finance_lease_payments),
            'proceeds_shares': to_crores(proceeds_shares),
            'proceeds_debentures': to_crores(proceeds_debentures),
            'share_buyback': to_crores(share_buyback),
            'proceeds_stock_options': to_crores(proceeds_stock_options),
            'fx_on_cash': to_crores(fx_on_cash),
            'net_change_cash_filed': to_crores(net_change_cash),
            'net_change_cash_pre_fx': to_crores(net_change_cash_pre_fx),
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
    """
    Parse one quarterly XBRL filing (DQ/SQ/JQ/MQ).
    Extended for Sprint 2 to capture bank/NBFC fields (Interest Earned/Expended,
    Deposits, Advances, NPA, CET-1, CRAR — these are filed quarterly under
    SEBI Reg 33, NOT annually).
    """
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            text = f.read()
    except Exception:
        return None

    facts, contexts = parse_xbrl_text(text)
    p = pick_top_level_period(contexts, (80, 100))  # ~3 months
    if not p:
        # NSE/newer XBRL — pick the context with highest revenue-like fact
        revenue_facts = facts.get('RevenueFromOperations') or facts.get('Income') or facts.get('InterestEarned') or {}
        best_ctx = None
        best_val = 0.0
        for c, v in revenue_facts.items():
            ctx = contexts.get(c)
            if not ctx or ctx.get('dim') or ctx['type'] != 'period':
                continue
            try:
                val = abs(float(v))
            except Exception:
                continue
            if val > best_val:
                best_val = val
                best_ctx = c
        p = best_ctx
    if not p:
        return None
    period_end = contexts[p]['end']
    # Also pick instant (year-end / quarter-end snapshot) for BS-like fields
    i_ctx = pick_top_level_instant(contexts, period_end) or pick_top_level_instant(contexts)

    def fnum(tag: str, ctx: Optional[str] = None) -> Optional[float]:
        return num(facts.get(tag, {}).get(ctx or p))

    revenue = fnum('RevenueFromOperations') or fnum('Income')
    interest_earned = fnum('InterestEarned')
    if revenue is None and interest_earned is None:
        return None

    other_income = fnum('OtherIncome')
    expenses = fnum('Expenses')
    employee = fnum('EmployeeBenefitExpense') or fnum('StaffCost') or fnum('EmployeesCost')
    finance_costs = fnum('FinanceCosts') or fnum('Interest')
    depreciation = fnum('DepreciationDepletionAndAmortisationExpense') or fnum('DepreciationAndAmortisationExpense') or fnum('Depreciation')
    pbt = fnum('ProfitBeforeTax') or fnum('ProfitLossFromOrdinaryActivitiesBeforeTax')
    tax = fnum('TaxExpense')
    net_profit = fnum('ProfitLossForPeriod') or fnum('ProfitLossForThePeriod') or fnum('ProfitLossForPeriodFromContinuingOperations') or fnum('ProfitLossForThePeriodFromContinuingOperations') or fnum('NetProfit') or fnum('ProfitLossFromOrdinaryActivitiesAfterTax')

    # ── Bank-style quarterly fields ──
    interest_expended = fnum('InterestExpended')
    interest_on_advances = fnum('InterestOrDiscountOnAdvancesOrBills')
    interest_on_rbi = fnum('InterestOnBalancesWithReserveBankOfIndiaAndOtherInterBankFunds')
    income_on_invest = fnum('RevenueOnInvestments') or fnum('IncomeOnInvestments')
    bank_total_income = fnum('Income')
    bank_employees_cost = fnum('EmployeesCost')
    bank_other_op_exp = fnum('OtherOperatingExpenses')
    bank_total_exp = fnum('ExpenditureExcludingProvisionsAndContingencies')
    bank_opbpc = fnum('OperatingProfitBeforeProvisionAndContingencies')
    bank_provisions = fnum('ProvisionsOtherThanTaxAndContingencies')
    # Asset quality (these are typically filed quarterly, not annually)
    gross_npa_amt = fnum('GrossNonPerformingAssets') or fnum('GrossNonPerformingAssets', i_ctx)
    nonperf = fnum('NonPerformingAssets') or fnum('NonPerformingAssets', i_ctx)
    gross_npa_pct = fnum('PercentageOfGrossNpa') or fnum('PercentageOfGrossNpa', i_ctx)
    net_npa_pct = fnum('PercentageOfNpa') or fnum('PercentageOfNpa', i_ctx)
    cet1 = fnum('CET1Ratio') or fnum('CET1Ratio', i_ctx)
    addl_t1 = fnum('AdditionalTier1Ratio') or fnum('AdditionalTier1Ratio', i_ctx)
    crar = fnum('CapitalAdequacyRatio') or fnum('CapitalAdequacyRatio', i_ctx) or fnum('TotalCapitalRatio')
    return_on_assets = fnum('ReturnOnAssets') or fnum('ReturnOnAssets', i_ctx)
    # Bank BS — present in some quarterly disclosures
    deposits = fnum('Deposits', i_ctx) if i_ctx else None
    advances = fnum('Advances', i_ctx) if i_ctx else None
    cash_with_rbi = fnum('CashAndBalancesWithReserveBankOfIndia', i_ctx) if i_ctx else None
    bank_balances = fnum('BalancesWithBanksAndMoneyAtCallAndShortNotice', i_ctx) if i_ctx else None

    nii = None
    if interest_earned is not None and interest_expended is not None:
        nii = interest_earned - interest_expended

    is_bank_filing = (interest_earned is not None) or (deposits is not None and deposits > 0)

    # Bug 8 — bank quarterly "sales": fall back to total income
    # (InterestEarned + OtherIncome) when the standard revenue tag is absent.
    if is_bank_filing and revenue is None:
        if interest_earned is not None or other_income is not None:
            revenue = (interest_earned or 0) + (other_income or 0)

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
        'eps': fnum('BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations') or fnum('BasicEarningsLossPerShareFromContinuingOperations') or fnum('BasicEarningsPerShareAfterExtraordinaryItems'),
        'basic_eps': fnum('BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations'),
        'diluted_eps': fnum('DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations'),
        'exceptional_items': to_crores(fnum('ExceptionalItemsBeforeTax') or fnum('ExceptionalItems')),
        'debt_equity_ratio': fnum('DebtEquityRatio'),
        'interest_service_coverage_ratio': fnum('InterestServiceCoverageRatio'),
        'debt_service_coverage_ratio': fnum('DebtServiceCoverageRatio'),
        # Bank quarterly fields
        'interest_earned': to_crores(interest_earned),
        'interest_expended': to_crores(interest_expended),
        'net_interest_income': to_crores(nii),
        'interest_on_advances': to_crores(interest_on_advances),
        'interest_on_rbi_balances': to_crores(interest_on_rbi),
        'income_on_investments': to_crores(income_on_invest),
        'bank_total_income': to_crores(bank_total_income),
        'bank_employees_cost': to_crores(bank_employees_cost),
        'bank_other_operating_expenses': to_crores(bank_other_op_exp),
        'bank_total_expenses': to_crores(bank_total_exp),
        'operating_profit_pre_provisions': to_crores(bank_opbpc),
        'provisions_other_than_tax': to_crores(bank_provisions),
        # Asset quality (quarterly)
        'gross_npa_amount': to_crores(gross_npa_amt),
        'non_performing_assets': to_crores(nonperf),
        'gross_npa_pct': gross_npa_pct,
        'net_npa_pct': net_npa_pct,
        'cet1_ratio': cet1,
        'additional_tier1_ratio': addl_t1,
        'capital_adequacy_ratio': crar,
        'return_on_assets_pct': return_on_assets,
        # Bank BS quarter-end snapshot
        'deposits': to_crores(deposits),
        'advances': to_crores(advances),
        'cash_with_rbi': to_crores(cash_with_rbi),
        'balances_with_banks': to_crores(bank_balances),
        '_is_bank': is_bank_filing,
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
