# -*- coding: utf-8 -*-
"""
NSE -> corporate_announcements ingester.

BSE's AnnGetData API stopped returning data (~June 2026, now answers
"No Record Found!" for every window). This pulls the same corporate-announcements
feed from NSE instead, maps it onto the existing corporate_announcements schema,
and upserts (dedup by a deterministic news_id derived from NSE's seq_id).

Writes to the SAME table the website/app already read, so no front-end change.

Usage:
  python nse_corp_announcements_cron.py                          # daily: last 3 days
  python nse_corp_announcements_cron.py --days 7
  python nse_corp_announcements_cron.py --from 01-06-2026 --to 05-06-2026
  python nse_corp_announcements_cron.py --dry-run                # fetch + map, no write
"""
import sys, time, json, uuid, argparse
from datetime import datetime, timedelta
import requests

# reuse auth + the proven upsert from the BSE cron (same table, same conflict key)
from bse_announcements_cron import URL, H, upsert

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
NSE_PAGE = "https://www.nseindia.com/companies-listing/corporate-filings-announcements"
NSE_API = "https://www.nseindia.com/api/corporate-announcements?index=equities&from_date={f}&to_date={t}"
NS = uuid.uuid5(uuid.NAMESPACE_URL, "stockssena-nse-announcements")

# A meaningful announcement (drives material=true curated feed). Non-material rows
# are still stored (the per-stock page shows everything); the flag only gates the
# curated /feed and /news lists.
MATERIAL_KW = (
    'result', 'board meeting', 'dividend', 'bonus', 'stock split', 'order', 'contract',
    'bagging', 'award', 'acquisition', 'acquire', 'merger', 'amalgamation', 'demerger',
    'scheme of arrangement', 'fund rais', 'fundrais', 'allotment', 'takeover', 'sast',
    'open offer', 'buyback', 'buy back', 'investor meet', 'analyst', 'con. call',
    'conference call', 'press release', 'investor presentation', 'credit rating', 'rating',
    'resignation', 'appointment', 'change in director', 'cessation', 'agreement',
    'joint venture', 'memorandum of understanding', 'expansion', 'capacity', 'commenc',
    'commission', 'preferential', 'qip', 'debenture', 'ncd', 'capex', 'delisting',
    'revision in', 'completion of', 'one time settlement', 'resolution plan', 'insolvency',
    'secures', 'wins ', ' won ', 'receipt of',
)
NOISE_KW = (
    'newspaper', 'trading window', 'duplicate', 'record date', 'loss of share',
    'share certificate', 'reconciliation', 'postal ballot', 'advertis', 'transmission',
    'compliance certificate', 'investor complaint', 'change in registrar', 'forfeiture',
    'reg. 74', 'regulation 74', 'general update',
)


def nse_is_material(desc: str, detail: str) -> bool:
    s = (str(desc) + ' ' + str(detail)).lower()
    if any(n in s for n in NOISE_KW):
        return False
    return any(m in s for m in MATERIAL_KW)


def new_session():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
                      "Accept-Encoding": "gzip, deflate"})
    try:
        s.get("https://www.nseindia.com/", timeout=15)
        s.get(NSE_PAGE, timeout=15)
    except Exception as e:
        print("  [warm]", e, file=sys.stderr)
    return s


def fetch_window(s, f, t, tries=4):
    h = {"Referer": NSE_PAGE, "Accept": "application/json, */*",
         "X-Requested-With": "XMLHttpRequest"}
    for i in range(tries):
        try:
            r = s.get(NSE_API.format(f=f, t=t), headers=h, timeout=60)
            if r.status_code == 200 and "json" in r.headers.get("content-type", ""):
                d = r.json()
                return d if isinstance(d, list) else (d.get("data") or [])
        except Exception as e:
            print(f"  [NSE ERR] {f}->{t} try{i}: {e}", file=sys.stderr)
        try:
            s.get(NSE_PAGE, timeout=15)
        except Exception:
            pass
        time.sleep(1.5 * (i + 1))
    return []


def parse_andt(raw):
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(str(raw).strip(), fmt)
        except Exception:
            pass
    return None


def known_symbols():
    out, off = set(), 0
    while True:
        r = requests.get(f"{URL}/rest/v1/stock_master?select=symbol&is_active=eq.true",
                         headers={**H, "Range": f"{off}-{off+999}"}, timeout=30)
        b = r.json()
        if not b:
            break
        out.update(x["symbol"] for x in b if x.get("symbol"))
        if len(b) < 1000:
            break
        off += 1000
    return out


def to_row(a: dict, known: set):
    sym = (a.get("symbol") or "").strip().upper()
    if not sym or sym not in known:
        return None
    seq = str(a.get("seq_id") or a.get("dt") or "")
    if not seq:
        return None
    desc = (a.get("desc") or "").strip()
    detail = (a.get("attchmntText") or "").strip()
    filed = parse_andt(a.get("an_dt") or a.get("sort_date"))
    return {
        "news_id": str(uuid.uuid5(NS, seq)),
        "symbol": sym,
        "scrip_code": None,
        "category": (desc[:120] or "Announcement"),
        "headline": (desc or detail)[:300],
        "detail": detail[:1000],
        "pdf_url": a.get("attchmntFile") or None,
        "filed_at": filed.isoformat() if filed else None,
        "critical": False,
        "material": nse_is_material(desc, detail),
        "company_name": (a.get("sm_name") or "").strip() or None,
    }


def chunk_windows(fr: str, to: str, max_days=12):
    """Split [fr,to] (dd-mm-yyyy) into <=max_days slices (NSE caps wide queries)."""
    d0 = datetime.strptime(fr, "%d-%m-%Y")
    d1 = datetime.strptime(to, "%d-%m-%Y")
    out = []
    cur = d0
    while cur <= d1:
        end = min(cur + timedelta(days=max_days - 1), d1)
        out.append((cur.strftime("%d-%m-%Y"), end.strftime("%d-%m-%Y")))
        cur = end + timedelta(days=1)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3, help="daily mode: last N days")
    ap.add_argument("--from", dest="frm", default=None, help="dd-mm-yyyy")
    ap.add_argument("--to", dest="to", default=None, help="dd-mm-yyyy")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    today = datetime.now()
    if args.frm and args.to:
        windows = chunk_windows(args.frm, args.to)
    else:
        to_d = today.strftime("%d-%m-%Y")
        fr_d = (today - timedelta(days=args.days)).strftime("%d-%m-%Y")
        windows = [(fr_d, to_d)]

    print(f"[nse_announcements] windows={windows} dry_run={args.dry_run}", flush=True)
    known = known_symbols()
    print(f"  known active symbols: {len(known)}", flush=True)

    s = new_session()
    rows = {}
    raw_total = 0
    for fr, to in windows:
        raw = fetch_window(s, fr, to)
        raw_total += len(raw)
        for a in raw:
            r = to_row(a, known)
            if r:
                rows[r["news_id"]] = r          # dedup by news_id
        print(f"  {fr}->{to}: raw={len(raw)} mapped_total={len(rows)}", flush=True)
        time.sleep(0.5)

    all_rows = list(rows.values())
    mat = sum(1 for r in all_rows if r["material"])
    dated = [r for r in all_rows if r["filed_at"]]
    newest = max((r["filed_at"] for r in dated), default="-")
    print(f"\n  raw fetched   : {raw_total}")
    print(f"  in our universe: {len(all_rows)}")
    print(f"  material       : {mat}")
    print(f"  newest filed_at: {newest}")
    print("  sample material items:")
    for r in [r for r in all_rows if r["material"]][:8]:
        print(f"    [{r['category'][:22]:22}] {r['symbol']:12} {str(r['filed_at'])[:16]}  {r['headline'][:46]}")

    if args.dry_run:
        print("\n  DRY-RUN: nothing written.")
        return
    n = upsert("corporate_announcements", all_rows, "news_id")
    print(f"\n  upserted: {n}")


if __name__ == "__main__":
    main()
