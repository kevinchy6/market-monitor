#!/usr/bin/env python3
"""
S&P 500 Market Breadth Calculator (yfinance version)
Fetches all S&P 500 constituents, calculates:
  - Advancers / Decliners (today's % change)
  - % of stocks above 20-day MA
  - % of stocks above 50-day MA
Outputs breadth.json for the frontend.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen, Request
import re

try:
    import yfinance as yf
except ImportError:
    print("[ERROR] yfinance not installed. Run: pip install yfinance")
    sys.exit(1)

# ─── CONFIG ───────────────────────────────────────────────────────────
BATCH_SIZE = 20          # symbols per yfinance.download batch
SLEEP_BETWEEN = 1.0      # seconds between batches
OUTPUT_FILE = "breadth.json"


# ─── STEP 1: Get S&P 500 constituent list from Wikipedia ─────────────
def get_sp500_symbols():
    """Fetch S&P 500 tickers from Wikipedia."""
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urlopen(req, timeout=30) as resp:
            html = resp.read().decode("utf-8")
    except Exception as e:
        print(f"[ERROR] Failed to fetch S&P 500 list: {e}")
        sys.exit(1)

    # Find the constituents table by id
    start = html.find('id="constituents"')
    if start != -1:
        start = html.rfind("<table", 0, start)
    if start == -1:
        print("[ERROR] Could not find S&P 500 constituents table")
        sys.exit(1)

    end = html.find("</table>", start)
    table_html = html[start:end]

    symbols = []
    rows = table_html.split("<tr")
    for row in rows[2:]:  # skip header rows
        td_start = row.find("<td")
        if td_start == -1:
            continue
        td_end = row.find("</td>", td_start)
        cell = row[td_start:td_end]
        text = re.sub(r'<[^>]+>', '', cell).strip()
        if text and 0 < len(text) <= 6:
            ticker = text.replace(".", "-")
            symbols.append(ticker)

    print(f"[INFO] Found {len(symbols)} S&P 500 symbols")
    return symbols


# ─── STEP 2: Fetch data in batches using yfinance ────────────────────
def fetch_all_data(symbols):
    """Fetch 3-month daily data for all symbols using yfinance batch download."""
    all_data = {}
    total = len(symbols)

    print(f"[INFO] Fetching data for {total} symbols in batches of {BATCH_SIZE}...")

    for i in range(0, total, BATCH_SIZE):
        batch = symbols[i:i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        total_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE
        print(f"  Batch {batch_num}/{total_batches} ({len(batch)} symbols)...", end=" ", flush=True)

        try:
            # yfinance.download handles auth/crumb automatically
            df = yf.download(
                batch,
                period="3mo",
                interval="1d",
                group_by="ticker",
                progress=False,
                threads=True,
            )

            if df.empty:
                print("EMPTY")
                continue

            fetched_count = 0
            for sym in batch:
                try:
                    if len(batch) == 1:
                        sym_df = df
                    else:
                        sym_df = df[sym]

                    closes = sym_df["Close"].dropna().tolist()
                    if len(closes) >= 2:
                        all_data[sym] = closes
                        fetched_count += 1
                except (KeyError, TypeError):
                    pass

            print(f"{fetched_count}/{len(batch)} OK")
        except Exception as e:
            print(f"ERROR: {e}")

        if i + BATCH_SIZE < total:
            time.sleep(SLEEP_BETWEEN)

    print(f"[INFO] Successfully fetched {len(all_data)}/{total} symbols")
    return all_data


# ─── STEP 3: Calculate breadth metrics ───────────────────────────────
def calculate_breadth(all_data):
    """Compute breadth metrics from close price data."""
    advancers = 0
    decliners = 0
    unchanged = 0
    above_20d = 0
    above_50d = 0
    total_20d = 0
    total_50d = 0
    counted = 0

    for sym, closes in all_data.items():
        if len(closes) < 2:
            continue

        counted += 1
        current = closes[-1]
        prev = closes[-2]

        # Advancer / Decliner
        if current > prev:
            advancers += 1
        elif current < prev:
            decliners += 1
        else:
            unchanged += 1

        # Above 20D MA
        if len(closes) >= 20:
            sma20 = sum(closes[-20:]) / 20
            total_20d += 1
            if current > sma20:
                above_20d += 1

        # Above 50D MA
        if len(closes) >= 50:
            sma50 = sum(closes[-50:]) / 50
            total_50d += 1
            if current > sma50:
                above_50d += 1

    if counted == 0:
        print("[ERROR] No valid data to compute breadth")
        sys.exit(1)

    adv_pct = round((advancers / counted) * 100, 1)
    dec_pct = round((decliners / counted) * 100, 1)
    above20_pct = round((above_20d / total_20d) * 100, 1) if total_20d > 0 else None
    above50_pct = round((above_50d / total_50d) * 100, 1) if total_50d > 0 else None

    # Determine label
    if adv_pct > 60:
        label = "STRONG BREADTH"
        label_class = "strong"
    elif adv_pct < 40:
        label = "WEAK BREADTH"
        label_class = "weak"
    else:
        label = "NEUTRAL"
        label_class = "neutral"

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    return {
        "updated": now_utc,
        "stocks_counted": counted,
        "advancers": advancers,
        "decliners": decliners,
        "unchanged": unchanged,
        "adv_pct": adv_pct,
        "dec_pct": dec_pct,
        "above_20d_pct": above20_pct,
        "above_50d_pct": above50_pct,
        "label": label,
        "label_class": label_class,
    }


# ─── MAIN ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 60)
    print("S&P 500 Market Breadth Calculator (yfinance)")
    print(f"yfinance version: {yf.__version__}")
    print("=" * 60)

    output_dir = os.environ.get("OUTPUT_DIR", str(Path(__file__).parent.parent))
    output_path = os.path.join(output_dir, OUTPUT_FILE)

    symbols = get_sp500_symbols()
    if len(symbols) < 400:
        print(f"[WARN] Only found {len(symbols)} symbols, expected ~500")

    all_data = fetch_all_data(symbols)
    breadth = calculate_breadth(all_data)

    print("\n" + "=" * 60)
    print(f"  Stocks counted:   {breadth['stocks_counted']}")
    print(f"  Advancers:        {breadth['advancers']} ({breadth['adv_pct']}%)")
    print(f"  Decliners:        {breadth['decliners']} ({breadth['dec_pct']}%)")
    print(f"  Unchanged:        {breadth['unchanged']}")
    print(f"  > 20D MA:         {breadth['above_20d_pct']}%")
    print(f"  > 50D MA:         {breadth['above_50d_pct']}%")
    print(f"  Label:            {breadth['label']}")
    print("=" * 60)

    with open(output_path, "w") as f:
        json.dump(breadth, f, indent=2)

    print(f"\n[OK] Saved to {output_path}")
