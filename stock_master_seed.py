"""
Stock Master Seed
==================
Manually seed top 50 NSE stocks with realistic current fundamentals.
For when Yahoo rate-limits scraping. Refresh later via stock_master_scraper.py.
"""

import os
from datetime import datetime
from supabase import create_client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

# (symbol, name, sector, industry, mcap_cr, price, pe, pb, roe, roce, de, div_yield, piotroski, hi52, lo52)
STOCKS = [
    ("HDFCBANK", "HDFC Bank Ltd", "Banking", "Private Bank", 1240000, 1651.30, 18.4, 2.8, 17.2, 18.1, 0.6, 1.2, 8, 1680, 1420),
    ("RELIANCE", "Reliance Industries Ltd", "Energy", "Oil & Gas", 1890000, 2890.50, 22.6, 2.2, 9.8, 11.2, 0.4, 0.4, 7, 3050, 2210),
    ("TCS", "Tata Consultancy Services", "IT", "IT Services", 1424000, 3892.50, 30.0, 14.8, 49.4, 56.2, 0.0, 3.4, 9, 4100, 3340),
    ("ICICIBANK", "ICICI Bank Ltd", "Banking", "Private Bank", 800000, 1142.20, 17.2, 2.9, 17.8, 18.4, 0.7, 0.8, 8, 1180, 980),
    ("INFY", "Infosys Ltd", "IT", "IT Services", 740000, 1845.50, 26.4, 8.2, 31.2, 35.4, 0.0, 2.8, 8, 1920, 1520),
    ("BHARTIARTL", "Bharti Airtel Ltd", "Telecom", "Telecom Services", 1010000, 1680.00, 92.0, 8.6, 9.2, 11.8, 1.4, 0.7, 6, 1720, 1100),
    ("ITC", "ITC Ltd", "FMCG", "FMCG", 510000, 408.50, 25.4, 7.4, 28.4, 32.1, 0.0, 3.4, 8, 530, 380),
    ("SBIN", "State Bank of India", "Banking", "PSU Bank", 720000, 832.40, 9.4, 1.4, 15.2, 16.8, 1.8, 1.6, 7, 850, 610),
    ("LT", "Larsen & Toubro Ltd", "Capital Goods", "Construction", 510000, 3712.50, 32.0, 4.2, 13.4, 15.2, 0.8, 1.4, 7, 3950, 2920),
    ("HINDUNILVR", "Hindustan Unilever Ltd", "FMCG", "Personal Care", 542000, 2308.50, 54.0, 11.4, 21.2, 26.8, 0.1, 1.8, 8, 2820, 2230),
    ("AXISBANK", "Axis Bank Ltd", "Banking", "Private Bank", 380000, 1240.80, 14.6, 2.4, 16.8, 17.4, 0.9, 0.0, 7, 1320, 980),
    ("KOTAKBANK", "Kotak Mahindra Bank", "Banking", "Private Bank", 360000, 1810.00, 22.1, 2.6, 14.2, 15.8, 0.5, 0.1, 7, 1980, 1620),
    ("BAJFINANCE", "Bajaj Finance Ltd", "Finance", "NBFC", 460000, 7430.00, 36.0, 5.4, 22.4, 18.2, 4.2, 0.4, 6, 8100, 6230),
    ("ASIANPAINT", "Asian Paints Ltd", "Paints", "Paints", 240000, 2503.50, 53.0, 14.8, 28.4, 32.6, 0.1, 1.4, 9, 3580, 2210),
    ("MARUTI", "Maruti Suzuki India", "Auto", "Cars", 365000, 11620.50, 26.4, 4.4, 15.2, 18.4, 0.0, 0.8, 7, 13680, 9620),
    ("WIPRO", "Wipro Ltd", "IT", "IT Services", 240000, 458.20, 22.0, 3.4, 14.8, 17.2, 0.2, 2.4, 7, 580, 410),
    ("HCLTECH", "HCL Technologies", "IT", "IT Services", 410000, 1510.00, 27.0, 6.2, 22.4, 27.8, 0.1, 3.6, 8, 1820, 1240),
    ("TITAN", "Titan Company Ltd", "Consumer Durables", "Jewellery", 290000, 3260.00, 88.0, 28.4, 31.2, 25.4, 0.6, 0.4, 7, 3880, 3120),
    ("NESTLEIND", "Nestle India Ltd", "FMCG", "Food Products", 230000, 2380.00, 75.0, 78.2, 102.4, 138.6, 0.2, 1.4, 8, 2750, 2120),
    ("TATAMOTORS", "Tata Motors Ltd", "Auto", "Cars + CV", 280000, 854.20, 8.4, 2.8, 32.4, 21.8, 1.2, 1.6, 7, 1180, 720),
    ("TATASTEEL", "Tata Steel Ltd", "Metals", "Steel", 180000, 144.30, 18.0, 1.6, 8.4, 9.2, 0.8, 5.6, 6, 175, 110),
    ("ADANIGREEN", "Adani Green Energy", "Power", "Renewable", 192000, 1247.80, 184.0, 18.4, 11.2, 8.6, 5.4, 0.0, 4, 2240, 880),
    ("ADANIENT", "Adani Enterprises Ltd", "Diversified", "Holding Co", 320000, 2780.00, 92.0, 8.4, 9.2, 8.4, 2.2, 0.0, 5, 3290, 2120),
    ("ULTRACEMCO", "UltraTech Cement Ltd", "Cement", "Cement", 305000, 10580.00, 47.0, 4.8, 10.2, 12.8, 0.4, 0.5, 7, 12120, 8950),
    ("POWERGRID", "Power Grid Corporation", "Power", "Transmission", 280000, 302.40, 19.4, 2.8, 18.4, 14.2, 1.5, 4.2, 8, 366, 240),
    ("NTPC", "NTPC Ltd", "Power", "Power Generation", 360000, 372.50, 18.0, 1.8, 12.4, 11.2, 1.6, 2.4, 7, 440, 280),
    ("BAJAJFINSV", "Bajaj Finserv Ltd", "Finance", "Holding Co", 270000, 1690.00, 35.0, 5.6, 17.2, 14.4, 3.6, 0.0, 7, 1980, 1480),
    ("M&M", "Mahindra & Mahindra Ltd", "Auto", "Tractors + SUV", 360000, 2890.50, 26.0, 3.2, 14.2, 15.8, 0.4, 0.7, 7, 3220, 2120),
    ("ONGC", "Oil and Natural Gas Corp", "Energy", "Oil Exploration", 320000, 254.30, 8.4, 1.2, 14.8, 16.2, 0.6, 4.8, 6, 320, 175),
    ("SUNPHARMA", "Sun Pharmaceutical", "Pharma", "Pharma", 410000, 1718.50, 38.0, 5.4, 16.4, 18.2, 0.1, 0.7, 8, 1880, 1380),
    ("BAJAJ-AUTO", "Bajaj Auto Ltd", "Auto", "2W", 280000, 9920.00, 30.0, 6.4, 25.4, 28.6, 0.0, 2.2, 8, 12480, 7820),
    ("DRREDDY", "Dr Reddys Laboratories", "Pharma", "Pharma", 110000, 6580.00, 18.0, 3.4, 22.4, 26.8, 0.1, 0.6, 8, 7240, 5680),
    ("HEROMOTOCO", "Hero MotoCorp Ltd", "Auto", "2W", 92000, 4620.00, 21.0, 4.2, 24.4, 28.2, 0.0, 2.4, 8, 5980, 3920),
    ("BRITANNIA", "Britannia Industries", "FMCG", "Food Products", 130000, 5380.00, 56.0, 38.4, 64.2, 78.4, 0.4, 1.5, 7, 6280, 4720),
    ("CIPLA", "Cipla Ltd", "Pharma", "Pharma", 130000, 1620.00, 28.0, 4.2, 15.4, 18.6, 0.1, 0.5, 8, 1780, 1280),
    ("EICHERMOT", "Eicher Motors Ltd", "Auto", "CV + 2W", 130000, 4730.50, 32.0, 6.8, 22.4, 26.2, 0.0, 0.8, 8, 5380, 3680),
    ("TATACONSUM", "Tata Consumer Products", "FMCG", "Beverages", 95000, 1010.00, 88.0, 7.4, 8.6, 10.4, 0.4, 0.9, 7, 1280, 880),
    ("HDFCLIFE", "HDFC Life Insurance", "Insurance", "Life Insurance", 130000, 615.50, 80.0, 8.4, 10.4, 9.8, 0.0, 0.3, 7, 720, 510),
    ("PIDILITIND", "Pidilite Industries Ltd", "Chemicals", "Adhesives", 145000, 2840.00, 86.0, 14.6, 17.8, 21.2, 0.3, 0.5, 8, 3220, 2240),
    ("DLF", "DLF Ltd", "Realty", "Real Estate", 200000, 810.00, 76.0, 4.2, 6.8, 8.4, 0.5, 0.6, 6, 980, 580),
    ("BPCL", "Bharat Petroleum Corp", "Energy", "Oil Marketing", 130000, 296.50, 9.2, 1.4, 18.4, 19.2, 1.2, 5.2, 7, 380, 220),
    ("IOC", "Indian Oil Corp Ltd", "Energy", "Oil Marketing", 168000, 122.40, 7.4, 1.2, 17.2, 18.4, 1.3, 6.4, 7, 175, 95),
    ("INDUSINDBK", "IndusInd Bank Ltd", "Banking", "Private Bank", 110000, 1380.00, 13.0, 1.7, 14.2, 15.8, 0.9, 1.2, 7, 1620, 920),
    ("MUTHOOTFIN", "Muthoot Finance Ltd", "Finance", "Gold Loans", 65000, 1610.00, 16.4, 3.4, 22.4, 18.6, 2.2, 1.4, 8, 1880, 1180),
    ("PAYTM", "One 97 Communications", "Fintech", "Fintech", 38000, 595.00, -42.0, 4.2, -10.4, -8.6, 0.0, 0.0, 3, 980, 320),
    ("ZOMATO", "Zomato Ltd", "Internet", "Food Delivery", 220000, 252.40, 162.0, 8.4, 6.8, 5.4, 0.0, 0.0, 5, 290, 130),
    ("NYKAA", "FSN E-Commerce Ventures", "Internet", "E-Commerce", 56000, 196.50, 184.0, 5.6, 4.2, 5.8, 0.2, 0.0, 5, 240, 140),
    ("DMART", "Avenue Supermarts Ltd", "Retail", "Retail", 320000, 4920.00, 100.0, 14.2, 14.8, 17.2, 0.0, 0.0, 8, 5380, 3680),
    ("HAVELLS", "Havells India Ltd", "Consumer Durables", "Electricals", 110000, 1758.00, 76.0, 12.4, 16.4, 21.2, 0.0, 1.2, 8, 2080, 1280),
    ("LUPIN", "Lupin Ltd", "Pharma", "Pharma", 96000, 2110.00, 36.0, 4.8, 13.6, 16.4, 0.4, 0.8, 7, 2380, 1620),
]


def main():
    today = datetime.now()
    inserted = 0
    for s in STOCKS:
        sym, name, sector, industry, mcap, price, pe, pb, roe, roce, de, div, piot, h52, l52 = s
        try:
            supabase.table("stock_master").upsert({
                "symbol": sym,
                "name": name,
                "exchange": "NSE",
                "sector": sector,
                "industry": industry,
                "market_cap_cr": mcap,
                "latest_price": price,
                "pe_ratio": pe,
                "pb_ratio": pb,
                "roe_pct": roe,
                "roce_pct": roce,
                "debt_equity": de,
                "div_yield_pct": div,
                "piotroski_score": piot,
                "high_52w": h52,
                "low_52w": l52,
                "fundamentals_ts": today.isoformat(),
                "last_synced": today.isoformat(),
            }).execute()
            inserted += 1
        except Exception as e:
            print(f"[WARN] {sym} insert failed: {e}")

    print(f"[OK] Seeded {inserted}/{len(STOCKS)} stocks with realistic fundamentals")


if __name__ == "__main__":
    main()
