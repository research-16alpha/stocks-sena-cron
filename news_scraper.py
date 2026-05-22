"""
News Aggregator · Stocks Sena
==============================
Pulls 7 RSS feeds every 30 min, tags items by stock symbol, writes to Supabase.

RSS sources (all free, syndication-allowed):
- Moneycontrol Markets
- Economic Times Markets
- LiveMint Markets
- Business Standard Markets
- Reuters India Business
- BusinessLine Markets
- Bloomberg Quint

Deploy as Render cron job every 30 min.
"""

import os
import re
import hashlib
from datetime import datetime
import requests
import xml.etree.ElementTree as ET
from supabase import create_client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

RSS_FEEDS = [
    ("moneycontrol", "https://www.moneycontrol.com/rss/business.xml"),
    ("et_markets", "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
    ("mint", "https://www.livemint.com/rss/markets"),
    ("business_standard", "https://www.business-standard.com/rss/markets-106.rss"),
    ("reuters_india", "https://feeds.reuters.com/reuters/INbusinessNews"),
    ("bl_markets", "https://www.thehindubusinessline.com/markets/feeder/default.rss"),
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 StocksSena News Aggregator",
}

# Symbol detection — match common Indian stock names + tickers
STOCK_KEYWORDS = {
    "HDFCBANK": ["hdfc bank", "hdfcbank"],
    "ICICIBANK": ["icici bank", "icicibank"],
    "SBIN": ["sbi", "state bank", "sbin"],
    "AXISBANK": ["axis bank", "axisbank"],
    "KOTAKBANK": ["kotak", "kotakbank"],
    "TCS": ["tcs", "tata consultancy"],
    "INFY": ["infosys", "infy"],
    "WIPRO": ["wipro"],
    "HCLTECH": ["hcl tech", "hcltech"],
    "RELIANCE": ["reliance industries", "reliance ind", "ril"],
    "TATAMOTORS": ["tata motors", "tatamotors"],
    "TATASTEEL": ["tata steel"],
    "ADANIGREEN": ["adani green"],
    "ADANIENT": ["adani enterprises"],
    "ASIANPAINT": ["asian paints"],
    "MARUTI": ["maruti suzuki", "maruti"],
    "ITC": ["itc ltd", "itc limited"],
    "HINDUNILVR": ["hul", "hindustan unilever"],
    "BAJFINANCE": ["bajaj finance"],
    "TITAN": ["titan company"],
    "NESTLEIND": ["nestle india"],
    "ZOMATO": ["zomato"],
    "PAYTM": ["paytm"],
    "NYKAA": ["nykaa"],
    "NIFTY": ["nifty 50", "nifty"],
    "BANKNIFTY": ["banknifty", "bank nifty"],
    "SENSEX": ["sensex"],
}


def detect_symbols(text):
    text_lower = text.lower()
    return [
        sym
        for sym, keywords in STOCK_KEYWORDS.items()
        if any(kw in text_lower for kw in keywords)
    ]


def fetch_rss(source_name, url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        items = []
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            desc = (item.findtext("description") or "").strip()
            pub_date = (item.findtext("pubDate") or "").strip()
            if title and link:
                # Parse pub_date to ISO
                try:
                    from email.utils import parsedate_to_datetime
                    pub_iso = parsedate_to_datetime(pub_date).isoformat()
                except:
                    pub_iso = datetime.now().isoformat()

                # Strip HTML tags from description
                desc_clean = re.sub(r"<[^>]+>", " ", desc).strip()[:500]

                items.append(
                    {
                        "source": source_name,
                        "source_url": link,
                        "headline": title[:500],
                        "summary": desc_clean,
                        "published_at": pub_iso,
                        "fetched_at": datetime.now().isoformat(),
                    }
                )
        return items
    except Exception as e:
        print(f"[WARN] {source_name} fetch failed: {e}")
        return []


def main():
    all_items = []
    for source, url in RSS_FEEDS:
        items = fetch_rss(source, url)
        all_items.extend(items)
        print(f"[INFO] {source}: fetched {len(items)}")

    if not all_items:
        print("[INFO] No items to insert.")
        return

    # Insert with conflict ignore (source_url is unique)
    inserted = 0
    for item in all_items:
        try:
            result = (
                supabase.table("news_items")
                .upsert(item, on_conflict="source_url")
                .execute()
            )
            if result.data:
                news_id = result.data[0]["id"]
                # Tag with detected symbols
                detected = detect_symbols(item["headline"] + " " + item.get("summary", ""))
                for symbol in detected:
                    try:
                        supabase.table("news_tags").upsert(
                            {"news_id": news_id, "symbol": symbol}, on_conflict="news_id,symbol"
                        ).execute()
                    except:
                        pass
                inserted += 1
        except Exception as e:
            print(f"[WARN] Insert failed for {item['source_url']}: {e}")

    print(f"[OK] {datetime.now().isoformat()} · processed {inserted} news items")


if __name__ == "__main__":
    main()
