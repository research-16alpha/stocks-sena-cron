"""
bse_feeds.py
============
UNIFIED BSE corporate-filing scraper via the LIVE AnnSubCategoryGetData endpoint (the
AnnGetData one died Jun-2026; this paginated variant - the one the user's annual-report
scraper uses - works). ONE broad scrape per window feeds MULTIPLE tables, so we never
re-paginate the (heavy) broad category per feed:

  corporate_announcements : ALL filings for BSE-ONLY companies (NSE already covers the
                            dual-listed ones; BSE-only had ZERO feed coverage). PK news_id.
  concall_transcripts     : "Earnings Call Transcript" filings, gap-filled vs NSE
                            (symbol+quarter, source=BSE), link straight to the PDF.

(Insider / SAST routes slot in the same way later.)

scrip_code -> our symbol via stock_master.bse_scrip_code. Times are IST -> UTC.

Modes:  py bse_feeds.py --days 7            # recent window (also the daily-cron mode)
        py bse_feeds.py --years 2           # historical backfill
        add --dry to count without writing.
"""
import argparse, datetime, os, threading, time, uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

URL = os.environ.get("SUPABASE_URL", "https://tbeadvvkqyrhtendttrg.supabase.co").rstrip("/")
KEY = os.environ.get("SUPABASE_SERVICE_KEY") or open(r"e:/Stocks sena/.supabase-service-key").read().strip()
SB_H = {"apikey": KEY, "Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
BSE_H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
         "Accept": "application/json", "Origin": "https://www.bseindia.com",
         "Referer": "https://www.bseindia.com/corporates/ann.html"}
API = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
UTC = datetime.timezone.utc
NS_BSE = uuid.uuid5(uuid.NAMESPACE_URL, "stockssena-bse-announcements")
PRECALL = ("intimation of", "schedule of", "notice of", "will be held", "prior intimation", "newspaper")
MATERIAL_KW = ("transcript", "result", "order", "acquisition", "investor", "analyst", "dividend",
               "bonus", "split", "buyback", "merger", "fund rais", "preferential", "rating",
               "resignation", "appointment", "open offer", "scheme of arrangement")


# ── reusable BSE core ─────────────────────────────────────────────────────────
_tl = threading.local()


def bse_session():
    s = getattr(_tl, "s", None)
    if s is None:
        s = requests.Session(); s.headers.update(BSE_H)
        try:
            s.get("https://www.bseindia.com/", timeout=15)
        except Exception:
            pass
        _tl.s = s
    return s


def fetch_window(frm, to, cat="Company Update"):
    s = bse_session(); rows, page = [], 1
    while page <= 400:
        url = (f"{API}?pageno={page}&strCat={cat.replace(' ', '%20')}&strPrevDate={frm}"
               f"&strScrip=&strSearch=P&strToDate={to}&strType=C&subcategory=-1")
        t = None
        for attempt in range(4):
            try:
                r = s.get(url, timeout=45)
                if r.status_code == 200 and r.text.strip():
                    t = (r.json() or {}).get("Table") or []; break
            except Exception:
                pass
            time.sleep(0.6 * (attempt + 1))
        if not t:
            break
        rows += t
        if len(t) < 50:
            break
        page += 1; time.sleep(0.1)
    return rows


def attach_url(name, news_dt):
    if not name:
        return None
    try:
        age = (datetime.date.today() - datetime.datetime.fromisoformat(str(news_dt)[:19]).date()).days
    except Exception:
        age = 999
    return f"https://www.bseindia.com/xml-data/corpfiling/{'AttachLive' if age < 8 else 'AttachHis'}/{name}"


def ist_to_utc(nd):
    try:
        return datetime.datetime.fromisoformat(str(nd)[:19]).replace(tzinfo=IST).astimezone(UTC).isoformat()
    except Exception:
        return None


def rest_set(query, fields):
    """Paginated REST fetch -> list of tuples of `fields`."""
    out, off = [], 0
    while True:
        b = requests.get(f"{URL}/rest/v1/{query}&limit=1000&offset={off}", headers=SB_H, timeout=30).json()
        if not isinstance(b, list):
            break
        out += [tuple(x.get(f) for f in fields) for x in b]
        if len(b) < 1000:
            break
        off += 1000
    return out


def load_scrip_map():
    return {str(s).strip(): sym for sym, s in
            rest_set("stock_master?select=symbol,bse_scrip_code&is_active=eq.true&bse_scrip_code=not.is.null",
                     ["symbol", "bse_scrip_code"]) if s}


def bse_only_scrips():
    """Scrip codes of companies listed on BSE but NOT NSE -> safe to add announcements for."""
    return {str(s).strip() for (s,) in
            rest_set("listing_master?select=bse_scrip_code&status=eq.active&nse_symbol=is.null&bse_scrip_code=not.is.null",
                     ["bse_scrip_code"]) if s}


def existing_concall_pairs():
    return set(rest_set("concall_transcripts?select=symbol,quarter", ["symbol", "quarter"]))


# ── concall routing helpers ───────────────────────────────────────────────────
def infer_quarter(d):
    m, y = d.month, d.year
    if m in (4, 5, 6):      q, pe, fy = "Q4", datetime.date(y, 3, 31), y
    elif m in (7, 8, 9):    q, pe, fy = "Q1", datetime.date(y, 6, 30), y + 1
    elif m in (10, 11, 12): q, pe, fy = "Q2", datetime.date(y, 9, 30), y + 1
    else:                   q, pe, fy = "Q3", datetime.date(y - 1, 12, 31), y
    return f"{q}FY{str(fy)[2:]}", pe.isoformat()


def is_transcript(subcat, headline, attach):
    blob = " ".join(filter(None, (subcat, headline, attach))).lower()
    if "transcript" not in blob:
        return False
    return not any(x in (subcat + " " + headline).lower() for x in PRECALL)


def windows(years=None, days=None):
    end = datetime.date.today()
    start = end - datetime.timedelta(days=int(days if days else 365.25 * years))
    out, cur = [], start
    step = 7 if (days and days <= 14) else 15
    while cur <= end:
        nxt = min(cur + datetime.timedelta(days=step), end)
        out.append((cur.strftime("%Y%m%d"), nxt.strftime("%Y%m%d"))); cur = nxt + datetime.timedelta(days=1)
    return out


def upsert(table, rows, conflict):
    ok = 0
    for j in range(0, len(rows), 500):
        chunk = rows[j:j + 500]
        r = requests.post(f"{URL}/rest/v1/{table}?on_conflict={conflict}",
                          headers={**SB_H, "Prefer": "resolution=merge-duplicates"}, json=chunk, timeout=60)
        if r.status_code in (200, 201, 204):
            ok += len(chunk)
        else:
            print(f"  {table} err {r.status_code}: {r.text[:140]}", flush=True)
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=float)
    ap.add_argument("--days", type=int)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    if not a.years and not a.days:
        a.days = 2

    scrip2sym = load_scrip_map()
    bse_only = bse_only_scrips()
    have_cc = existing_concall_pairs()
    print(f"[bse-feeds] {len(scrip2sym)} scrips mapped · {len(bse_only)} BSE-only scrips · "
          f"{len(have_cc)} concall pairs covered", flush=True)
    wins = windows(years=a.years, days=a.days)
    print(f"[bse-feeds] {len(wins)} windows · {a.workers} workers · "
          f"{'DRY' if a.dry else 'WRITE'}", flush=True)

    all_rows, done = [], 0
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(fetch_window, frm, to): (frm, to) for frm, to in wins}
        for fut in as_completed(futs):
            frm, to = futs[fut]
            r = fut.result(); all_rows += r; done += 1
            print(f"  [{done}/{len(wins)}] {frm}..{to}: {len(r)} rows", flush=True)

    # ── route ──
    ann, cc = {}, {}
    for x in all_rows:
        scrip = str(x.get("SCRIP_CD")).strip()
        sym = scrip2sym.get(scrip)
        if not sym:
            continue
        sub = x.get("SUBCATNAME") or ""; head = x.get("HEADLINE") or ""; att = x.get("ATTACHMENTNAME") or ""
        cat = " / ".join(filter(None, (x.get("CATEGORYNAME"), sub)))
        # route 1: announcements (BSE-only companies only)
        if scrip in bse_only and x.get("NEWSID"):
            nid = str(uuid.uuid5(NS_BSE, str(x.get("NEWSID"))))
            ann[nid] = {"news_id": nid, "symbol": sym, "scrip_code": scrip,
                        "category": (cat[:120] or "Announcement"), "headline": (head or x.get("NEWSSUB") or "")[:300],
                        "detail": (x.get("NEWSSUB") or "")[:1000], "pdf_url": attach_url(att, x.get("NEWS_DT")),
                        "filed_at": ist_to_utc(x.get("NEWS_DT")), "critical": False,
                        "material": any(k in (cat + " " + head).lower() for k in MATERIAL_KW),
                        "company_name": x.get("SLONGNAME")}
        # route 2: concall transcripts (gap-fill vs NSE, PDF only)
        if is_transcript(sub, head, att) and att.lower().endswith(".pdf"):
            try:
                dt = datetime.datetime.fromisoformat((x.get("NEWS_DT") or "")[:19])
            except Exception:
                continue
            q, pe = infer_quarter(dt.date())
            if (sym, q) in have_cc:
                continue
            k = (sym, q)
            row = {"symbol": sym, "quarter": q, "period_end": pe, "filed_at": dt.isoformat(),
                   "source": "BSE", "source_url": attach_url(att, x.get("NEWS_DT")),
                   "title": (head or "Earnings call transcript")[:200], "has_text": False}
            if k not in cc or row["filed_at"] > cc[k]["filed_at"]:
                cc[k] = row

    ann_rows, cc_rows = list(ann.values()), list(cc.values())
    mat = sum(1 for r in ann_rows if r["material"])
    print(f"[bse-feeds] routed -> announcements(BSE-only)={len(ann_rows)} ({mat} material) · "
          f"concalls(new)={len(cc_rows)}", flush=True)
    if a.dry:
        for r in cc_rows[:10]:
            print(f"   CC {r['symbol']:12} {r['quarter']:8} {(r['source_url'] or '')[-40:]}")
        for r in ann_rows[:6]:
            print(f"   AN {r['symbol']:12} {r['category'][:34]:34} {str(r['filed_at'])[:10]}")
        print("[dry] nothing written."); return

    a_ok = upsert("corporate_announcements", ann_rows, "news_id") if ann_rows else 0
    c_ok = upsert("concall_transcripts", cc_rows, "symbol,quarter,source") if cc_rows else 0
    print(f"[bse-feeds] upserted: announcements={a_ok} · concalls={c_ok}", flush=True)


if __name__ == "__main__":
    main()
