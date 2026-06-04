# -*- coding: utf-8 -*-
"""
NSE insider-trading (SEBI PIT Reg 7(2)/7(3)) ingester via the NEW feed.

The legacy /api/corporates-pit JSON froze at ~April 2026 (returns April data,
0 for May/June). NSE moved live insider disclosures to /api/corporates-pit-gg,
which lists XBRL filings (symbol, regulation, broadcast time, xml url). This:
  1. pulls that filing list for a window,
  2. fetches + parses each XBRL for the structured transaction(s),
  3. writes insider_trades rows (same schema as backfill_insider), deduped
     app-side (the table has no unique constraint -> plain insert).

Usage:
  python nse_insider_xbrl_cron.py                       # daily: last 7 days
  python nse_insider_xbrl_cron.py --days 14
  python nse_insider_xbrl_cron.py --from 01-06-2026 --to 05-06-2026
  python nse_insider_xbrl_cron.py --dry-run
"""
import sys, time, argparse, re
from datetime import date, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
import requests

# reuse auth + helpers + the proven insert from the legacy insider backfill
from backfill_insider import URL, SB_H, num, pdate, post_rows

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
PAGE = "https://www.nseindia.com/companies-listing/corporate-filings-insider-trading"
GG_API = "https://www.nseindia.com/api/corporates-pit-gg?index={idx}&from_date={f}&to_date={t}"

# transaction-level XBRL elements (one set per disclosure context)
TXN_FIELDS = {
    "CategoryOfPerson", "NameOfThePerson", "TypeOfInstrument",
    "SecuritiesHeldPriorToAcquisitionOrDisposalNumberOfSecurity",
    "SecuritiesAcquiredOrDisposedNumberOfSecurity",
    "SecuritiesAcquiredOrDisposedValueOfSecurity",
    "SecuritiesAcquiredOrDisposedTransactionType",
    "SecuritiesHeldPostAcquistionOrDisposalNumberOfSecurity",
    "DateOfAllotmentAdviceOrAcquisitionOfSharesOrSaleOfSharesSpecifyFromDate",
    "DateOfAllotmentAdviceOrAcquisitionOfSharesOrSaleOfSharesSpecifyToDate",
    "ModeOfAcquisitionOrDisposal", "DateOfIntimationToCompany",
}
EL_RE = re.compile(r'<in-bse-co:([A-Za-z0-9]+)\s+contextRef="([^"]+)"[^>]*>([^<]+)</in-bse-co:\1>')


def new_session():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
                      "Accept-Encoding": "gzip, deflate"})
    try:
        s.get("https://www.nseindia.com/", timeout=15)
        s.get(PAGE, timeout=15)
    except Exception as e:
        print("  [warm]", e, file=sys.stderr)
    return s


def fetch_list(s, idx, f, t, tries=4):
    h = {"Referer": PAGE, "Accept": "application/json, */*", "X-Requested-With": "XMLHttpRequest"}
    for i in range(tries):
        try:
            r = s.get(GG_API.format(idx=idx, f=f, t=t), headers=h, timeout=60)
            if r.status_code == 200 and "json" in r.headers.get("content-type", ""):
                d = r.json()
                return d if isinstance(d, list) else (d.get("data") or [])
        except Exception as e:
            print(f"  [GG ERR] {idx} {f}->{t} try{i}: {e}", file=sys.stderr)
        try:
            s.get(PAGE, timeout=15)
        except Exception:
            pass
        time.sleep(1.5 * (i + 1))
    return []


def trade_dir(txn_type: str):
    s = (txn_type or "").lower()
    if any(k in s for k in ("buy", "acqui", "purchase", "subscrib", "allot", "invocation")):
        return "buy"
    if any(k in s for k in ("sell", "sale", "dispos")):
        return "sell"
    return None  # pledge / encumbrance / revoke -> not a directional trade


def parse_xbrl(text):
    by_ctx = {}
    for m in EL_RE.finditer(text):
        name, ctx, val = m.group(1), m.group(2), m.group(3).strip()
        if name in TXN_FIELDS:
            by_ctx.setdefault(ctx, {})[name] = val
    return [d for d in by_ctx.values()
            if d.get("SecuritiesAcquiredOrDisposedNumberOfSecurity")]


def rows_from_filing(s, f):
    sym = (f.get("symbol") or "").strip().upper()
    xu = f.get("xmlFileName")
    if not sym or not xu:
        return []
    try:
        t = s.get(xu, timeout=30, headers={"Referer": "https://www.nseindia.com/"}).text
    except Exception as e:
        print(f"  [xbrl err] {sym}: {e}", file=sys.stderr)
        return []
    bdt = pdate((f.get("broadcastDateTime") or "").split(" ")[0])
    out = []
    for d in parse_xbrl(t):
        tt = trade_dir(d.get("SecuritiesAcquiredOrDisposedTransactionType"))
        if not tt:
            continue
        qty = num(d.get("SecuritiesAcquiredOrDisposedNumberOfSecurity"))
        val = num(d.get("SecuritiesAcquiredOrDisposedValueOfSecurity"))
        if not qty or qty <= 0:
            continue
        td = pdate(d.get("DateOfAllotmentAdviceOrAcquisitionOfSharesOrSaleOfSharesSpecifyFromDate")) or bdt
        fd = pdate(d.get("DateOfIntimationToCompany")) or bdt or td
        value_cr = round(val / 1e7, 2) if val else None
        out.append({
            "symbol": sym,
            "insider_name": (d.get("NameOfThePerson") or "").strip() or None,
            "insider_role": (d.get("CategoryOfPerson") or "").strip() or None,
            "trade_type": tt,
            "quantity": int(qty),
            "avg_price": round(val / qty, 2) if (val and qty) else None,
            "value_cr": value_cr,
            "trade_date": td,
            "filed_date": fd,
            "filing_url": f.get("ixbrl") or xu,
            "raw_data": {
                "instrument": d.get("TypeOfInstrument"),
                "mode": d.get("ModeOfAcquisitionOrDisposal"),
                "txn_type": d.get("SecuritiesAcquiredOrDisposedTransactionType"),
                "prior": d.get("SecuritiesHeldPriorToAcquisitionOrDisposalNumberOfSecurity"),
                "post": d.get("SecuritiesHeldPostAcquistionOrDisposalNumberOfSecurity"),
                "regulation": f.get("regulation"), "source": "nse_pit_gg_xbrl",
            },
        })
    return out


def existing_keys(since_iso):
    """natural keys already in DB for the window -> avoid duplicate inserts."""
    keys, off = set(), 0
    while True:
        r = requests.get(
            f"{URL}/rest/v1/insider_trades"
            f"?select=symbol,insider_name,trade_date,trade_type,quantity"
            f"&trade_date=gte.{since_iso}",
            headers={**SB_H, "Range": f"{off}-{off+999}"}, timeout=40)
        b = r.json() if r.status_code == 200 else []
        if not b:
            break
        for x in b:
            keys.add((x.get("symbol"), x.get("insider_name"), x.get("trade_date"),
                      x.get("trade_type"), x.get("quantity")))
        if len(b) < 1000:
            break
        off += 1000
    return keys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--from", dest="frm", default=None, help="dd-mm-yyyy")
    ap.add_argument("--to", dest="to", default=None, help="dd-mm-yyyy")
    ap.add_argument("--index", default="equities", help="equities,sme,invitsreits (comma)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    today = date.today()
    if args.frm and args.to:
        fr, to = args.frm, args.to
    else:
        to = today.strftime("%d-%m-%Y")
        fr = (today - timedelta(days=args.days)).strftime("%d-%m-%Y")
    print(f"[nse_insider_xbrl] window {fr}->{to} index={args.index} dry={args.dry_run}", flush=True)

    s = new_session()
    filings = []
    for idx in [x.strip() for x in args.index.split(",") if x.strip()]:
        lst = fetch_list(s, idx, fr, to)
        print(f"  {idx}: {len(lst)} filings", flush=True)
        filings += lst

    all_rows = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for rs in ex.map(lambda f: rows_from_filing(s, f), filings):
            all_rows += rs
    print(f"  parsed transactions: {len(all_rows)}", flush=True)

    # dedup within batch
    seen, batch = set(), []
    for r in all_rows:
        k = (r["symbol"], r["insider_name"], r["trade_date"], r["trade_type"], r["quantity"])
        if k in seen:
            continue
        seen.add(k)
        batch.append(r)

    since = (today - timedelta(days=400)).isoformat()
    have = existing_keys(since)
    new = [r for r in batch
           if (r["symbol"], r["insider_name"], r["trade_date"], r["trade_type"], r["quantity"]) not in have]
    newest = max((r["trade_date"] for r in batch if r["trade_date"]), default="-")
    print(f"  unique in window : {len(batch)}")
    print(f"  already in DB    : {len(batch) - len(new)}")
    print(f"  NEW to insert    : {len(new)}")
    print(f"  newest trade_date: {newest}")
    print("  sample NEW:")
    for r in new[:8]:
        print(f"    {r['symbol']:12} {r['trade_date']}  {r['trade_type']:4} qty={r['quantity']:>10} "
              f"val={r['value_cr']}cr  {(r['insider_name'] or '')[:24]} [{r['insider_role']}]")

    if args.dry_run:
        print("\n  DRY-RUN: nothing written.")
        return
    n = post_rows(new)
    print(f"\n  inserted: {n}")


if __name__ == "__main__":
    main()
