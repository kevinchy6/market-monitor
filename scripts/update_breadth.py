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
    """Fetch 3-month daily closes for all symbols.

    Yahoo frequently returns TODAY's daily bar with a NaN close for hours after
    the session ends (the daily aggregator lags). If we silently drop it, the
    breadth numbers describe the PREVIOUS session while the timestamp says
    today. So for any symbol whose latest daily close is NaN we fill it from a
    batched 60-minute download (last completed hourly bar of that session).

    Returns (all_data, as_of_date) where all_data maps symbol -> list of closes
    ending on as_of_date, and as_of_date is the common trading date used.
    """
    from collections import Counter
    all_data = {}
    last_dates = {}
    need_fill = []
    total = len(symbols)

    print(f"[INFO] Fetching data for {total} symbols in batches of {BATCH_SIZE}...")

    for i in range(0, total, BATCH_SIZE):
        batch = symbols[i:i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        total_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE
        print(f"  Batch {batch_num}/{total_batches} ({len(batch)} symbols)...", end=" ", flush=True)

        try:
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
                    sym_df = df if len(batch) == 1 else df[sym]
                    close_s = sym_df["Close"]
                    if close_s.dropna().shape[0] < 2:
                        continue
                    last_idx = close_s.index[-1]
                    last_val = close_s.iloc[-1]
                    if last_val != last_val:  # NaN -> today's bar not aggregated yet
                        need_fill.append(sym)
                        closes = close_s.iloc[:-1].dropna().tolist()
                        all_data[sym] = closes + [None]          # placeholder
                        last_dates[sym] = last_idx.date()
                    else:
                        all_data[sym] = close_s.dropna().tolist()
                        last_dates[sym] = last_idx.date()
                    fetched_count += 1
                except (KeyError, TypeError):
                    pass

            print(f"{fetched_count}/{len(batch)} OK")
        except Exception as e:
            print(f"ERROR: {e}")

        if i + BATCH_SIZE < total:
            time.sleep(SLEEP_BETWEEN)

    # ── Fill NaN last bars from hourly data ─────────────────────────────
    if need_fill:
        print(f"[INFO] {len(need_fill)} symbols have a NaN latest daily close; filling from 60m bars...")
        for i in range(0, len(need_fill), 50):
            batch = need_fill[i:i + 50]
            try:
                hdf = yf.download(batch, period="5d", interval="60m", group_by="ticker",
                                  progress=False, threads=True, prepost=False)
                for sym in batch:
                    try:
                        hs = (hdf if len(batch) == 1 else hdf[sym])["Close"].dropna()
                        if hs.empty:
                            continue
                        want = last_dates[sym]
                        same_day = hs[[ts.date() == want for ts in hs.index]]
                        if same_day.empty:
                            continue
                        all_data[sym][-1] = float(same_day.iloc[-1])
                    except (KeyError, TypeError):
                        pass
            except Exception as e:
                print(f"  [WARN] hourly fill batch failed: {e}")
            if i + 50 < len(need_fill):
                time.sleep(SLEEP_BETWEEN)

    # Drop symbols we could not fill; they would be counted on the wrong day.
    unfilled = [s_ for s_, c in all_data.items() if c and c[-1] is None]
    for s_ in unfilled:
        del all_data[s_]
        last_dates.pop(s_, None)
    if unfilled:
        print(f"[WARN] {len(unfilled)} symbols dropped (no fill available): {unfilled[:10]}{'...' if len(unfilled) > 10 else ''}")

    # Enforce a single common as-of date (the most common latest date).
    if not last_dates:
        print("[ERROR] No data fetched")
        sys.exit(1)
    as_of = Counter(last_dates.values()).most_common(1)[0][0]
    off = [s_ for s_, d in last_dates.items() if d != as_of]
    for s_ in off:
        del all_data[s_]
    if off:
        print(f"[WARN] {len(off)} symbols dropped (latest bar != {as_of}): {off[:10]}{'...' if len(off) > 10 else ''}")

    print(f"[INFO] Successfully fetched {len(all_data)}/{total} symbols, as of {as_of}")
    return all_data, as_of


# ─── STEP 3: Calculate breadth metrics ───────────────────────────────
def calculate_breadth(all_data, as_of=None):
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
        "as_of": as_of.isoformat() if as_of else None,   # trading session the numbers describe
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

    all_data, as_of = fetch_all_data(symbols)
    breadth = calculate_breadth(all_data, as_of)

    print("\n" + "=" * 60)
    print(f"  As of session:    {breadth['as_of']}")
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
