"""
tag_news_items.py
=================
For every news item without tags, scan headline + summary for stock symbols
+ company names. Insert matches into `news_tags`.

Matching rules:
  - Exact NSE symbol match (word boundary): RELIANCE, TCS, HDFCBANK, etc.
    Skip symbols < 4 chars to avoid false positives (e.g., "TCS" matches inside
    "the TCS announced" — fine; but "FE" or "M" would explode).
  - Company name match: "Reliance Industries" -> RELIANCE.
    Drop common suffixes ("Ltd", "Limited") before matching.

Skip news already in news_tags.

Run:
  python tag_news_items.py             # all untagged items
  python tag_news_items.py --limit 50  # test
"""
import argparse
import json
import os
import re
import sys
import time
import requests

KEY = os.environ.get('SUPABASE_SERVICE_KEY')
if not KEY:
    try:
        with open('e:/Stocks sena/.supabase-service-key', 'r') as f:
            KEY = f.read().strip()
    except FileNotFoundError:
        print('[ERR] SUPABASE_SERVICE_KEY required', file=sys.stderr); sys.exit(1)
URL = os.environ.get('SUPABASE_URL', 'https://tbeadvvkqyrhtendttrg.supabase.co')
H = {'apikey': KEY, 'Authorization': f'Bearer {KEY}'}

# Symbols too short or generic to safely match by symbol alone
DENY_SYMBOLS = {'M', 'A', 'I', 'IT', 'CG', 'NA', 'IPO', 'NSE', 'BSE', 'RBI',
                'SEBI', 'GST', 'FY', 'YTD', 'EPS', 'PB', 'PE', 'AI'}

# Macro/RBI headline keywords. When ANY of these match the headline+summary
# (case-insensitive), we set news_items.category. Powers the MACRO tab in the
# Feed screen; the client-side fallback in useNews.ts mirrors this list.
RBI_KEYWORDS = ('rbi', 'monetary policy', 'repo rate', 'reverse repo', 'mpc ')
MACRO_KEYWORDS = (
    'inflation', 'cpi ', 'wpi ', 'iip ', 'gdp ',
    'fiscal deficit', 'current account', 'forex reserves',
    'rupee', 'inr ', 'dollar', 'usdinr', 'usd/inr',
    'fii ', 'fpi ', 'dii ', 'foreign portfolio',
    'crude', 'brent', 'oil price',
    'sebi ', 'finance ministry', 'union budget', 'gst council', 'gst ',
)


def derive_category(headline: str, summary: str) -> str | None:
    text = (headline + ' ' + (summary or '')).lower()
    if not text.strip():
        return None
    if any(kw in text for kw in RBI_KEYWORDS):
        return 'rbi'
    if any(kw in text for kw in MACRO_KEYWORDS):
        return 'macro'
    return None

# Company name suffixes to strip before matching
SUFFIX_RX = re.compile(
    r'\s+(ltd\.?|limited|industries|company|corporation|corp\.?|inc\.?|pvt\.?|private|holdings?|group|enterprises)\.?\s*$',
    re.IGNORECASE,
)
LEADING_ARTICLE_RX = re.compile(r'^(the)\s+', re.IGNORECASE)

# Tokens that are too generic to identify a single company on their own. A name
# made up ENTIRELY of these (e.g. "Bank of India", "Central Bank") is skipped —
# it would false-match "Reserve Bank of India", "European Central Bank", etc.
GENERIC_TOKENS = {
    'bank', 'india', 'indian', 'the', 'new', 'national', 'central', 'state',
    'finance', 'financial', 'corp', 'corporation', 'power', 'steel', 'motors',
    'cement', 'chemicals', 'textiles', 'auto', 'ltd', 'limited', 'industries',
    'company', 'enterprises', 'holdings', 'group', 'services', 'products',
    'of', 'and', 'global', 'international', 'general', 'first', 'union',
}


def fetch_stock_master() -> list:
    """Returns [(symbol, normalized_company_words)] for matching."""
    out = []
    offset = 0
    while True:
        r = requests.get(
            f'{URL}/rest/v1/stock_master?select=symbol,name',
            headers={**H, 'Range': f'{offset}-{offset+999}'}, timeout=30,
        )
        batch = r.json()
        if not batch:
            break
        for row in batch:
            sym = row['symbol']
            if sym.startswith('BSE_') or (sym.startswith('BSE') and sym[3:].isdigit()):
                continue
            name = row.get('name') or sym
            # Strip suffix words + a leading "The" so "The Federal Bank Ltd" -> "Federal Bank"
            clean = SUFFIX_RX.sub('', name).strip()
            clean = LEADING_ARTICLE_RX.sub('', clean).strip()
            out.append((sym, clean.lower()))
        if len(batch) < 1000:
            break
        offset += 1000
    return out


def fetch_untagged_news(limit: int = 0) -> list:
    """News items with no entries in news_tags."""
    # Get IDs that already have tags
    tagged = set()
    offset = 0
    while True:
        r = requests.get(
            f'{URL}/rest/v1/news_tags?select=news_id',
            headers={**H, 'Range': f'{offset}-{offset+999}'}, timeout=30,
        )
        batch = r.json()
        if not batch:
            break
        for row in batch:
            tagged.add(row['news_id'])
        if len(batch) < 1000:
            break
        offset += 1000

    # Fetch all news items, filter to untagged
    out = []
    offset = 0
    while True:
        r = requests.get(
            f'{URL}/rest/v1/news_items?select=id,headline,summary,category&order=published_at.desc.nullslast',
            headers={**H, 'Range': f'{offset}-{offset+999}'}, timeout=30,
        )
        batch = r.json()
        if not batch:
            break
        for row in batch:
            if row['id'] not in tagged:
                out.append(row)
                if limit and len(out) >= limit:
                    return out
        if len(batch) < 1000:
            break
        offset += 1000
    return out


def patch_categories(items_with_cat: list) -> int:
    """PATCH news_items.category for items where we derived a non-null bucket."""
    if not items_with_cat:
        return 0
    ok = 0
    for it in items_with_cat:
        r = requests.patch(
            f'{URL}/rest/v1/news_items?id=eq.{it["id"]}',
            headers={**H, 'Content-Type': 'application/json', 'Prefer': 'return=minimal'},
            data=json.dumps({'category': it['category']}),
            timeout=15,
        )
        if r.status_code in (200, 204):
            ok += 1
    return ok


def tag_one(item: dict, stock_list: list) -> list:
    """Returns list of (news_id, symbol) tuples discovered in headline+summary.

    PRECISION-FIRST: match the full distinctive company name (suffix + leading
    article stripped) as a WHOLE phrase with word boundaries. We deliberately do
    NOT match the bare NSE symbol — many tickers are common English words
    (GLOBAL, DOLLAR, FOCUS, RETAIL, TOTAL, TECHNO) and matching them as words
    tagged every "global markets"/"dollar"/"retail" headline to a random stock.
    We also skip names made entirely of generic tokens ("Bank of India") which
    would false-match "Reserve Bank of India" etc. Showing fewer but CORRECT
    tags beats leaking unrelated market news onto a stock page.
    """
    text = (item.get('headline') or '') + ' ' + (item.get('summary') or '')
    text_lower = text.lower()

    found = set()
    for sym, full_lower in stock_list:
        if len(full_lower) < 7:                    # too short to be safely distinctive
            continue
        toks = full_lower.split()
        if all(t in GENERIC_TOKENS for t in toks):  # purely generic name -> skip
            continue
        if re.search(r'\b' + re.escape(full_lower) + r'\b', text_lower):
            found.add(sym)
    return [(item['id'], s) for s in found]


def insert_tags(rows: list) -> int:
    if not rows:
        return 0
    headers = {**H, 'Content-Type': 'application/json',
               'Prefer': 'resolution=ignore-duplicates,return=minimal'}
    r = requests.post(
        f'{URL}/rest/v1/news_tags?on_conflict=news_id,symbol',
        headers=headers,
        data=json.dumps([{'news_id': nid, 'symbol': s} for nid, s in rows]),
        timeout=30,
    )
    if r.status_code not in (200, 201, 204):
        print(f'  [INSERT ERR] {r.status_code} {r.text[:200]}', file=sys.stderr)
        return 0
    return len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=0)
    args = ap.parse_args()

    print('[INFO] Loading stock_master...')
    stocks = fetch_stock_master()
    print(f'[INFO]   {len(stocks)} stocks loaded')

    print('[INFO] Loading untagged news...')
    news = fetch_untagged_news(args.limit)
    print(f'[INFO]   {len(news)} untagged items')

    total_tags = 0
    items_tagged = 0
    items_skipped = 0
    t0 = time.time()

    # Batch insert
    pending = []
    cat_pending = []
    cats_set = 0
    BATCH = 200
    for i, item in enumerate(news, 1):
        # Compute category from headline + summary (independent of stock tags)
        cat = derive_category(item.get('headline') or '', item.get('summary') or '')
        if cat and not item.get('category'):
            cat_pending.append({'id': item['id'], 'category': cat})
            if len(cat_pending) >= 50:
                cats_set += patch_categories(cat_pending)
                cat_pending = []

        tags = tag_one(item, stocks)
        if not tags:
            items_skipped += 1
            continue
        items_tagged += 1
        pending.extend(tags)
        if len(pending) >= BATCH:
            total_tags += insert_tags(pending)
            pending = []
        if i % 100 == 0:
            print(f'  [{i}/{len(news)}] tagged={items_tagged} skipped={items_skipped} '
                  f'tags={total_tags + len(pending)} cats={cats_set + len(cat_pending)}')

    if pending:
        total_tags += insert_tags(pending)
    if cat_pending:
        cats_set += patch_categories(cat_pending)

    print()
    print('-' * 60)
    print(f'  Items scanned       : {len(news)}')
    print(f'  Items with tags     : {items_tagged}')
    print(f'  Items skipped       : {items_skipped}')
    print(f'  Total tags added    : {total_tags}')
    print(f'  Categories set      : {cats_set}')
    print(f'  Elapsed             : {time.time()-t0:.1f}s')


if __name__ == '__main__':
    main()
