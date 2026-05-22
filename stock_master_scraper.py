"""
Stock Master Scraper · Stocks Sena
====================================
Monthly job (1st of month). Refreshes fundamental data for top stocks.

Source: Yahoo Finance (yfinance library) for basic ratios
Could be extended with Screener.in pages with respectful rate-limiting.

Writes to: stock_master
"""

import os
from datetime import datetime
import time
from supabase import create_client

# pip install yfinance
try:
    import yfinance as yf
except ImportError:
    print("ERROR: yfinance not installed. Run: pip install yfinance")
    yf = None

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

# Top 100 NSE stocks (extend as needed)
SYMBOLS = [
    "HDFCBANK", "RELIANCE", "TCS", "ICICIBANK", "INFY", "BHARTIARTL", "ITC",
    "SBIN", "LT", "HINDUNILVR", "AXISBANK", "KOTAKBANK", "BAJFINANCE", "ASIANPAINT",
    "MARUTI", "WIPRO", "HCLTECH", "TITAN", "NESTLEIND", "TATAMOTORS", "TATASTEEL",
    "ADANIGREEN", "ADANIENT", "ADANIPORTS", "ULTRACEMCO", "POWERGRID", "NTPC", "ONGC",
    "COALINDIA", "JSWSTEEL", "GRASIM", "HINDALCO", "BAJAJFINSV", "BAJAJ-AUTO", "M&M",
    "DRREDDY", "CIPLA", "DIVISLAB", "SUNPHARMA", "BRITANNIA", "TECHM", "EICHERMOT",
    "HEROMOTOCO", "TATACONSUM", "BPCL", "IOC", "INDUSINDBK", "SBILIFE", "HDFCLIFE",
    "PIDILITIND", "HAVELLS", "LUPIN", "AUROPHARMA", "DABUR", "GODREJCP", "BERGEPAINT",
    "MUTHOOTFIN", "BANKBARODA", "IDFCFIRSTB", "DLF", "GAIL", "VEDL", "JINDALSTEL",
    "TATAPOWER", "AMBUJACEM", "ACC", "ZOMATO", "PAYTM", "NYKAA", "DMART",
    "PFC", "RECLTD", "HINDPETRO", "TORNTPHARM", "PETRONET", "MARICO", "COLPAL",
    "BIOCON", "PEL", "AMBUJACEMENT", "CHOLAFIN", "ICICIPRULI", "ICICIGI", "MOTHERSON",
    "VOLTAS", "PAGEIND", "TRENT", "JUBLFOOD", "MPHASIS", "PERSISTENT", "LTIM",
    "COFORGE", "MFSL", "DABUR", "GLAND", "ABBOTINDIA", "SHREECEM", "RAMCOCEM",
    "BANKINDIA", "PNB", "BANKBARODA", "UNIONBANK",
]


def fetch_stock_data(symbol):
    if yf is None:
        return None
    try:
        ticker = yf.Ticker(f"{symbol}.NS")
        info = ticker.info
        if not info or "regularMarketPrice" not in info:
            return None

        return {
            "symbol": symbol,
            "name": info.get("longName", symbol),
            "exchange": "NSE",
            "sector": info.get("sector"),
            "industry": info.get("industry"),
            "market_cap_cr": (info.get("marketCap") or 0) / 1e7,  # convert to crore
            "latest_price": info.get("regularMarketPrice"),
            "pe_ratio": info.get("trailingPE"),
            "pb_ratio": info.get("priceToBook"),
            "roe_pct": (info.get("returnOnEquity") or 0) * 100 if info.get("returnOnEquity") else None,
            "debt_equity": info.get("debtToEquity"),
            "div_yield_pct": (info.get("dividendYield") or 0) * 100 if info.get("dividendYield") else None,
            "high_52w": info.get("fiftyTwoWeekHigh"),
            "low_52w": info.get("fiftyTwoWeekLow"),
            "last_synced": datetime.now().isoformat(),
        }
    except Exception as e:
        print(f"[WARN] {symbol} fetch failed: {e}")
        return None


def main():
    if yf is None:
        print("[ERROR] Cannot run without yfinance.")
        return

    print(f"[INFO] Refreshing {len(SYMBOLS)} stocks...")
    success_count = 0

    for i, symbol in enumerate(SYMBOLS):
        data = fetch_stock_data(symbol)
        if data:
            try:
                supabase.table("stock_master").upsert(data).execute()
                success_count += 1
            except Exception as e:
                print(f"[WARN] Insert failed for {symbol}: {e}")
        # Respectful rate limit
        time.sleep(0.2)
        if (i + 1) % 20 == 0:
            print(f"[INFO] {i + 1}/{len(SYMBOLS)} processed")

    print(f"[OK] {datetime.now().isoformat()} · {success_count}/{len(SYMBOLS)} stocks updated")


if __name__ == "__main__":
    main()
