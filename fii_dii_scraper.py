"""
FII/DII Flow + Block/Bulk Deals Seed
======================================
Seeds last 30 days of realistic FII/DII flows + sample block deals.
Real scraper would pull from NSE EOD daily report.
"""

import os
import random
from datetime import datetime, timedelta
from supabase import create_client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def seed_fii_dii():
    """Seed last 30 trading days of FII/DII flows."""
    random.seed(42)
    today = datetime.now()
    records = []

    for days_ago in range(30):
        d = today - timedelta(days=days_ago)
        if d.weekday() >= 5:  # skip weekends
            continue

        # Realistic ranges
        fii_buy = round(random.uniform(8000, 16000), 0)
        fii_sell = round(random.uniform(7000, 15000), 0)
        dii_buy = round(random.uniform(6000, 12000), 0)
        dii_sell = round(random.uniform(5500, 11500), 0)

        records.append({
            "trade_date": d.strftime("%Y-%m-%d"),
            "fii_buy_cr": fii_buy,
            "fii_sell_cr": fii_sell,
            "fii_net_cr": fii_buy - fii_sell,
            "dii_buy_cr": dii_buy,
            "dii_sell_cr": dii_sell,
            "dii_net_cr": dii_buy - dii_sell,
        })

    for r in records:
        try:
            supabase.table("fii_dii").upsert(r, on_conflict="trade_date").execute()
        except Exception as e:
            print(f"[WARN] FII/DII insert failed for {r['trade_date']}: {e}")

    print(f"[OK] Seeded {len(records)} days of FII/DII flows")


def seed_block_deals():
    """Seed last 7 days of sample block deals."""
    SAMPLE_DEALS = [
        ("HDFCBANK", "DSP Mutual Fund", "ICICI Prudential MF", 250000, 1648.50),
        ("RELIANCE", "Norges Bank Investment", "Mirae Asset", 180000, 2890.00),
        ("TCS", "Vanguard Total Stock", "BlackRock India", 95000, 3895.00),
        ("INFY", "Government Pension Fund Global", "HDFC AMC", 320000, 1845.00),
        ("ICICIBANK", "Capital Group", "Axis Mutual Fund", 280000, 1149.00),
        ("ADANIGREEN", "Promoter Entity", "Public", 210000, 1242.00),
        ("BHARTIARTL", "Goldman Sachs", "JP Morgan", 145000, 1680.00),
        ("SBIN", "LIC of India", "BNP Paribas", 350000, 832.00),
    ]

    today = datetime.now()
    for i, (sym, buyer, seller, qty, price) in enumerate(SAMPLE_DEALS):
        d = today - timedelta(days=i % 7)
        if d.weekday() >= 5:
            d -= timedelta(days=2)
        try:
            supabase.table("block_deals").insert({
                "symbol": sym,
                "trade_date": d.strftime("%Y-%m-%d"),
                "buyer": buyer,
                "seller": seller,
                "quantity": qty,
                "price": price,
                "value_cr": (qty * price) / 1e7,
            }).execute()
        except Exception as e:
            print(f"[WARN] Block deal insert failed for {sym}: {e}")

    print(f"[OK] Seeded {len(SAMPLE_DEALS)} block deals")


def seed_promoter_actions():
    """Seed realistic recent promoter actions until BSE scraper works."""
    SAMPLE = [
        ("ADANIGREEN", "Adani Green Energy", "sale", "Promoter sold 2,10,000 shares (₹47 Cr) at avg ₹1,242. Holding: 67.3% (was 67.9%).", 4),
        ("TATASTEEL", "Tata Steel", "auditor_change", "Auditor change: BSR & Co LLP replaces Deloitte. Effective FY27.", 2),
        ("YESBANK", "Yes Bank", "pledge_increase", "Promoter pledge increased 2.3% to 67.8%. 4th increase in 6 months.", 3),
        ("HDFCLIFE", "HDFC Life Insurance", "purchase", "CEO Vibha Padalkar bought 50,000 shares (₹3.4 Cr) on open market.", 3),
        ("SBIN", "State Bank of India", "rpt_disclosed", "RPT of ₹128 Cr with SBI Capital Markets disclosed in Q4 FY26.", 2),
        ("ICICIBANK", "ICICI Bank", "purchase", "ED Sandeep Bakhshi bought 25,000 shares at ₹1,148 average.", 3),
        ("ZEEL", "Zee Entertainment", "pledge_increase", "Subhash Chandra pledge increased to 95.5% of holding.", 4),
        ("RELIANCE", "Reliance Industries", "rpt_disclosed", "RPT of ₹4,200 Cr with Reliance Jio Infocomm.", 2),
    ]

    today = datetime.now()
    for i, (sym, name, action_type, desc, sev) in enumerate(SAMPLE):
        filing_date = (today - timedelta(hours=i * 4)).isoformat()
        try:
            supabase.table("promoter_actions").insert({
                "symbol": sym,
                "company_name": name,
                "action_type": action_type,
                "action_description": desc,
                "severity": sev,
                "filing_date": filing_date,
                "source_url": f"https://www.bseindia.com/corporates/anndet_new.aspx?scrip={sym}",
            }).execute()
        except Exception as e:
            print(f"[WARN] Promoter action insert failed for {sym}: {e}")

    print(f"[OK] Seeded {len(SAMPLE)} promoter actions")


def seed_insider_trades():
    """Seed insider trades for stock detail pages."""
    SAMPLE = [
        ("HDFCBANK", "Vibha Padalkar", "CEO", "buy", 50000, 1651.00),
        ("HDFCBANK", "Sashidhar Jagdishan", "MD", "buy", 25000, 1648.50),
        ("TCS", "K Krithivasan", "CEO", "sell", 12000, 3890.00),
        ("INFY", "Salil Parekh", "CEO", "buy", 8000, 1845.50),
        ("RELIANCE", "Mukesh Ambani", "Chairman", "sell", 0, 2890.00),  # exempt disclosure
        ("ADANIGREEN", "Gautam Adani", "Chairman", "sell", 210000, 1242.00),
        ("ICICIBANK", "Sandeep Bakhshi", "MD CEO", "buy", 25000, 1148.00),
    ]

    today = datetime.now()
    for i, (sym, name, role, ttype, qty, price) in enumerate(SAMPLE):
        d = today - timedelta(days=i * 2)
        try:
            supabase.table("insider_trades").insert({
                "symbol": sym,
                "insider_name": name,
                "insider_role": role,
                "trade_type": ttype,
                "quantity": qty,
                "avg_price": price,
                "value_cr": (qty * price) / 1e7 if qty else 0,
                "trade_date": d.strftime("%Y-%m-%d"),
                "filed_date": d.strftime("%Y-%m-%d"),
            }).execute()
        except Exception as e:
            print(f"[WARN] Insider trade insert failed for {sym}: {e}")

    print(f"[OK] Seeded {len(SAMPLE)} insider trades")


if __name__ == "__main__":
    print("=== Seeding FII/DII flows ===")
    seed_fii_dii()
    print("\n=== Seeding block deals ===")
    seed_block_deals()
    print("\n=== Seeding promoter actions ===")
    seed_promoter_actions()
    print("\n=== Seeding insider trades ===")
    seed_insider_trades()
    print("\n[DONE]")
