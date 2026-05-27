"""
shareholding_scraper.py
=======================
Pulls latest quarterly shareholding pattern from NSE for each stock in
stock_master and parses the SEBI-mandated XBRL filing to extract BOTH:

  1. Aggregate (promoter % / public % / employee trust %)  -> shareholding_periods
  2. Individual holders (name, shares, % per category)     -> shareholding_holders

PRIMARY SOURCE — no aggregator dependency. Data comes directly from:
  - NSE shareholding master:
    https://www.nseindia.com/api/corporate-share-holdings-master?index=equities&symbol=...
  - SEBI-mandated XBRL filing (linked from above):
    https://nsearchives.nseindia.com/corporate/xbrl/SHP_...

XBRL is the official SEBI Reg 30/31 disclosure every listed company files
within 21 days of quarter end. Includes ALL 1%+ holders by name + PAN.

Schedule: daily — only fetches if the latest filing date on NSE is newer
than what we've stored. So most days it does nothing for most stocks.
"""

import os
import sys
import time
import re
from datetime import datetime
from xml.etree import ElementTree as ET

import requests
from supabase import create_client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
sb = create_client(SUPABASE_URL, SUPABASE_KEY)

NSE_HOME = "https://www.nseindia.com"
NSE_SHP_URL = "https://www.nseindia.com/api/corporate-share-holdings-master"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-shareholding-patterns",
    "Connection": "keep-alive",
}

# XBRL namespaces. The shp namespace URI changes per SEBI taxonomy version
# (e.g. 2018-03-31, 2025-10-31). Auto-detect from the root tag.
XBRLI_NS = "http://www.xbrl.org/2003/instance"
XBRLDI_NS = "http://xbrl.org/2006/xbrldi"

# Map NSE XBRL category members to our compact categories.
# Two sources of category info in SEBI XBRL:
#   1. explicitMember CategoryOfShareholdersAxis  -> aggregate per category
#      uses full SEBI member names (e.g. "MutualFundsOrUTIMember")
#   2. typedMember DetailsSharesHeldBy<Cat>Axis   -> individual holders
#      uses abbreviated forms (e.g. "IndividualsOrHUF", "OthersIndianShareholders")
# Both forms are mapped here.
CATEGORY_MAP = {
    # Full SEBI member names (from explicitMember)
    "IndividualsOrHinduUndividedFamilyMember": "individual_holder",
    "BodiesCorporateMember": "body_corporate",
    "ForeignNationalsMember": "foreign_national",
    "DirectorsAndDirectorsRelativesMember": "director",
    "RelativesOfPromotersOtherThanPromoterGroupMember": "promoter_relative",
    "MutualFundsOrUTIMember": "mutual_fund",
    "InsuranceCompaniesMember": "insurance",
    "BanksMember": "bank_fi",
    "OtherFinancialInstitutionsMember": "other_fi",
    "NBFCsRegisteredWithRBIMember": "nbfc",
    "ProvidentFundsOrPensionFundsMember": "pension_fund",
    "AlternativeInvestmentFundsMember": "alternative_fund",
    "SovereignWealthFundsDomesticMember": "sovereign_fund",
    "InstitutionsForeignPortfolioInvestorCategoryOneMember": "fpi_cat1",
    "InstitutionsForeignPortfolioInvestorCategoryTwoMember": "fpi_cat2",
    "ForeignDirectInvestmentMember": "fdi",
    "OtherInstitutionsForeignMember": "other_foreign",
    "ResidentIndividualShareholdersHoldingNominalShareCapitalInExcessOfRsTwoLakhMember": "individual_above_2L",
    "ResidentIndividualShareholdersHoldingNominalShareCapitalUpToRsTwoLakhMember": "individual_under_2L",
    "NonResidentIndiansMember": "nri",
    "InvestorEducationAndProtectionFundMember": "iepf",
    "KeyManagerialPersonnelMember": "kmp",
    "AssociateCompaniesOrSubsidiariesMember": "associate_company",
    "CentralGovernmentOrPresidentOfIndiaMember": "central_govt",
    "StateGovernmentsOrGovernorsMember": "state_govt",
    "OtherIndianShareholdersMember": "other_indian",
    "OtherNonInstitutionsMember": "other_non_institutional",
    "CustodianOrDRHolderMember": "custodian_dr",
    # Abbreviated forms (from typedMember DetailsSharesHeldBy<Cat>Axis names + "Member")
    "IndividualsOrHUFMember": "individual_holder",
    "OthersIndianShareholdersMember": "other_indian",
    "BodiesCorporateMember": "body_corporate",
    "ProvidentFundsOrPensionFundsMember": "pension_fund",
    "OtherInstitutionsForeignMember": "other_foreign",
    "OtherNonInstitutionsMember": "other_non_institutional",
}


def warm_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    s.get(NSE_HOME, timeout=10)
    time.sleep(1)
    s.get("https://www.nseindia.com/companies-listing/corporate-filings-shareholding-patterns", timeout=10)
    time.sleep(1)
    return s


def fetch_master(session: requests.Session, symbol: str) -> list[dict] | None:
    """Returns list of filings (newest first) for a symbol."""
    try:
        r = session.get(NSE_SHP_URL, params={"index": "equities", "symbol": symbol}, timeout=20)
        if r.status_code != 200:
            return None
        data = r.json()
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return None
    except Exception as e:
        print(f"[shp] {symbol} master fetch: {e}", file=sys.stderr)
        return None


def parse_nse_date(s: str) -> str | None:
    if not s: return None
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def to_int(v):
    if v is None: return None
    try: return int(float(str(v).replace(",", "").strip()))
    except (ValueError, TypeError): return None


def to_num(v):
    if v is None: return None
    try: return float(str(v).replace(",", "").strip())
    except (ValueError, TypeError): return None


def fetch_xbrl(session: requests.Session, url: str) -> bytes | None:
    try:
        r = session.get(url, timeout=30)
        if r.status_code != 200: return None
        return r.content
    except Exception as e:
        print(f"[shp] xbrl fetch: {e}", file=sys.stderr)
        return None


def parse_xbrl(xbrl_bytes: bytes) -> dict | None:
    """Returns { holders: [...], total_shares, num_shareholders } or None."""
    try:
        root = ET.fromstring(xbrl_bytes)
    except ET.ParseError as e:
        print(f"[shp] xbrl parse error: {e}", file=sys.stderr)
        return None

    # Auto-detect the shp namespace URI from xmlns declarations
    # (it changes per SEBI taxonomy revision)
    shp_ns = None
    for k, v in root.attrib.items():
        if "in-bse-shp" in k or "shp" in v.lower():
            if "in-bse-shp" in v and "types" not in v:
                shp_ns = v
                break
    # Fallback: walk children if root attribs don't help (some XMLs strip xmlns)
    if not shp_ns:
        for el in root.iter():
            if el.tag.startswith("{") and "in-bse-shp" in el.tag and "types" not in el.tag:
                shp_ns = el.tag.split("}")[0].lstrip("{")
                break
    if not shp_ns:
        print("[shp] could not detect shp namespace", file=sys.stderr)
        return None

    # Build context -> category-member map. SEBI XBRL uses TWO patterns:
    #
    # 1. Category aggregate contexts: explicitMember with CategoryOfShareholdersAxis
    #    e.g. <explicitMember dimension="...CategoryOfShareholdersAxis">
    #             in-bse-shp:MutualFundsOrUTIMember
    #         </explicitMember>
    #
    # 2. Individual holder contexts: typedMember with axis named DetailsSharesHeldBy<Category>Axis
    #    e.g. <typedMember dimension="...DetailsSharesHeldByIndividualsOrHUFAxis">
    #             <IndividualsOrHUFDomain>IndividualsOrHUF_Context15</IndividualsOrHUFDomain>
    #         </typedMember>
    #    Category extracted from dimension name (strip "DetailsSharesHeldBy" prefix + "Axis" suffix).
    ctx_to_member: dict[str, str] = {}
    for ctx in root.iter("{%s}context" % XBRLI_NS):
        ctx_id = ctx.attrib.get("id")
        # explicit (aggregate) pattern
        for m in ctx.iter("{%s}explicitMember" % XBRLDI_NS):
            dim = m.attrib.get("dimension", "")
            if "CategoryOfShareholdersAxis" in dim:
                txt = (m.text or "").strip()
                if ":" in txt:
                    txt = txt.split(":")[-1]
                ctx_to_member[ctx_id] = txt
                break
        if ctx_id in ctx_to_member:
            continue
        # typed (individual holder) pattern. SEBI XBRL uses two prefix variants:
        #   "DetailsSharesHeldBy<Cat>Axis"   (e.g. IndividualsOrHUF)
        #   "DetailsOfSharesHeldBy<Cat>Axis" (e.g. MutualFundsOrUTI, InsuranceCompanies)
        for m in ctx.iter("{%s}typedMember" % XBRLDI_NS):
            dim = m.attrib.get("dimension", "")
            local_dim = dim.split(":")[-1] if ":" in dim else dim
            if not local_dim.endswith("Axis"):
                continue
            cat = None
            for prefix in ("DetailsSharesHeldBy", "DetailsOfSharesHeldBy"):
                if local_dim.startswith(prefix):
                    cat = local_dim[len(prefix):-len("Axis")]
                    break
            if cat:
                ctx_to_member[ctx_id] = cat + "Member"
                break

    pct_tag_candidates = [
        "ShareholdingAsAPercentageOfTotalNumberOfSharesAsAPercentageOfABPlusCPlusD",
        "ShareholdingAsAPercentageOfTotalNumberOfShares",
        "EquityShareCapitalHeldByThePromoterAsAPercentageOfTotalEquityShareCapitalOfTheCompany",
    ]

    # Group all elements by contextRef
    by_ctx: dict[str, dict] = {}
    for el in root.iter():
        ctx_ref = el.attrib.get("contextRef")
        if not ctx_ref: continue
        tag_local = el.tag.split("}", 1)[-1] if "}" in el.tag else el.tag
        by_ctx.setdefault(ctx_ref, {})[tag_local] = (el.text or "").strip()

    # SEBI XBRL pairs each holder across TWO contexts:
    #   D_<Cat>_Context<N> -> identity (name + PAN)
    #   <Cat>_Context<N>   -> quantitative (NumberOfShares, ShareholdingAsAPct)
    # We pair by stripping the "D_" prefix from the identity context.
    holders = []
    for ctx_ref, fields in by_ctx.items():
        name = fields.get("NameOfTheShareholder")
        if not name: continue
        member = ctx_to_member.get(ctx_ref)
        if not member: continue
        category = CATEGORY_MAP.get(member, member.replace("Member", "").lower())

        # Locate paired instant-context for shares + pct
        if ctx_ref.startswith("D_"):
            instant_ref = ctx_ref[2:]   # strip 'D_'
        else:
            instant_ref = ctx_ref
        instant_fields = by_ctx.get(instant_ref, {})

        shares = to_int(
            instant_fields.get("NumberOfShares")
            or instant_fields.get("NumberOfFullyPaidUpEquityShares")
        )
        # Pct is stored as decimal proportion (0.1113 = 11.13%). Convert to percentage.
        pct_raw = to_num(instant_fields.get("ShareholdingAsAPercentageOfTotalNumberOfShares"))
        pct = pct_raw * 100 if pct_raw is not None else None
        pan = (fields.get("PermanentAccountNumberOfShareholder") or "").strip() or None
        # Filter out masked / placeholder PANs like '******'
        if pan and set(pan) <= {"*"}:
            pan = None

        holders.append({
            "category": category,
            "holder_name": name,
            "pan": pan,
            "shares": shares,
            "pct": pct,
        })

    # Totals
    total_shares = None
    num_shareholders = None
    for el in root.iter():
        local = el.tag.split("}", 1)[-1] if "}" in el.tag else el.tag
        if local == "TotalNoOfSharesHeld" and not el.attrib.get("contextRef", "").startswith("D_"):
            total_shares = to_int(el.text)
        if local == "TotalNumberOfShareholders":
            v = to_int(el.text)
            if v is not None: num_shareholders = v

    return {"holders": holders, "total_shares": total_shares, "num_shareholders": num_shareholders}


def stored_periods(symbol: str) -> set[str]:
    """All periods already in shareholding_periods for this symbol."""
    res = (
        sb.table("shareholding_periods")
        .select("period")
        .eq("symbol", symbol)
        .execute()
    )
    return {r["period"] for r in (res.data or [])}


def _persist_one_filing(symbol: str, filing: dict, session: requests.Session) -> str | None:
    """Fetch + parse + upsert one filing entry. Returns short status string."""
    period_iso = parse_nse_date(filing.get("date"))
    if not period_iso:
        return None
    xbrl_url = filing.get("xbrl")
    if not xbrl_url:
        return None

    xb = fetch_xbrl(session, xbrl_url)
    if not xb:
        return f"{period_iso} xbrl fail"
    parsed = parse_xbrl(xb)
    if not parsed:
        return f"{period_iso} parse fail"

    promoter_pct = to_num(filing.get("pr_and_prgrp"))
    public_pct = to_num(filing.get("public_val"))
    emp_trust = to_num(filing.get("employeeTrusts"))
    filing_date = parse_nse_date(filing.get("submissionDate"))

    sb.table("shareholding_periods").upsert({
        "symbol": symbol,
        "period": period_iso,
        "promoter_pct": promoter_pct,
        "public_pct": public_pct,
        "employee_trust_pct": emp_trust,
        "filing_date": filing_date,
        "xbrl_url": xbrl_url,
        "total_shares": parsed.get("total_shares"),
        "num_shareholders": parsed.get("num_shareholders"),
    }, on_conflict="symbol,period").execute()

    holders = parsed.get("holders") or []
    if holders:
        seen = set()
        batch = []
        for h in holders:
            key = (h["category"], h["holder_name"][:200])
            if key in seen: continue
            seen.add(key)
            batch.append({**h, "symbol": symbol, "period": period_iso, "holder_name": h["holder_name"][:200]})
        try:
            sb.table("shareholding_holders").upsert(
                batch, on_conflict="symbol,period,category,holder_name"
            ).execute()
        except Exception as e:
            return f"{period_iso} holder upsert fail: {e}"

    return f"{period_iso}+{len(holders)}h"


def process_symbol(session: requests.Session, symbol: str) -> str:
    """
    Process every filing NSE has for this symbol that isn't already in our DB.
    NSE master returns ~8-12 quarters of history per stock — backfilling this
    once gives us 2-3 years of pattern history without a separate script.
    """
    master = fetch_master(session, symbol)
    if not master:
        return f"{symbol}: no master data"

    already = stored_periods(symbol)
    new_periods: list[str] = []
    for filing in master:
        period_iso = parse_nse_date(filing.get("date"))
        if not period_iso or period_iso in already:
            continue
        status = _persist_one_filing(symbol, filing, session)
        if status:
            new_periods.append(status)
        time.sleep(0.4)  # Small per-XBRL pause; NSE rate-limit friendly

    if not new_periods:
        latest = parse_nse_date(master[0].get("date")) if master else "—"
        return f"{symbol}: up-to-date (latest {latest})"
    return f"{symbol}: +{len(new_periods)} periods → {new_periods[0]} ... {new_periods[-1]}"


def fetch_top_symbols(limit: int) -> list[str]:
    res = (
        sb.table("stock_master")
        .select("symbol")
        .order("market_cap_cr", desc=True)
        .limit(limit)
        .execute()
    )
    return [r["symbol"] for r in (res.data or [])]


def main():
    only_symbol = os.environ.get("ONLY_SYMBOL")
    if only_symbol:
        symbols = [only_symbol]
    else:
        symbols = fetch_top_symbols(200)

    session = warm_session()
    print(f"[shp] processing {len(symbols)} stocks")

    for i, sym in enumerate(symbols, 1):
        status = process_symbol(session, sym)
        if i <= 5 or i % 20 == 0 or "FAILED" in status or "holders" in status:
            print(f"[shp] {i:3d}. {status}")
        time.sleep(1.0)
        # Re-warm session every 50 calls to avoid NSE rate limits
        if i % 50 == 0:
            session = warm_session()

    print(f"[shp] {datetime.now().isoformat()} · done")


if __name__ == "__main__":
    main()
