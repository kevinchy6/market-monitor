#!/usr/bin/env python3
"""
Update data.json — fetch 1-year daily candles for all dashboard tickers
from Yahoo Finance using the yfinance library (handles auth/crumb automatically).
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import yfinance as yf
except ImportError:
    print("[ERROR] yfinance not installed. Run: pip install yfinance")
    sys.exit(1)

# ─── All dashboard tickers ────────────────────────────────────────────
SYMBOLS = [
    # Equity Alternatives
    "ZN=F", "DX-Y.NYB", "ZB=F", "CL=F", "GC=F", "BTC-USD",
    # Global Equities
    "IEV", "VXUS", "VTI", "EEM", "EEMA", "MCHI",
    # US Equity Indices
    "ARKK", "RSP", "IWM", "TLT", "DIA", "SPY", "QQQ",
    # Sectors
    "GDX", "IYT", "IGV", "XLF", "XRT", "XHB", "KRE", "IYR",
    "ITA", "XLI", "XLE", "XLY", "XLB", "XLP", "XLV", "BLOK",
    "XLU", "XBI", "MSOS", "XLK", "KWEB", "TAN", "SOXX",
]


def fetch_one(symbol):
    """Fetch 1-year daily chart data for one symbol using yfinance."""
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period="1y", interval="1d")
        if hist.empty:
            return None

        # Convert to same JSON structure the frontend expects
        # (matching Yahoo Finance v8 chart API format)
        timestamps = [int(ts.timestamp()) for ts in hist.index]
        opens = [float(v) if v == v else None for v in hist["Open"]]
        highs = [float(v) if v == v else None for v in hist["High"]]
        lows = [float(v) if v == v else None for v in hist["Low"]]
        closes = [float(v) if v == v else None for v in hist["Close"]]
        volumes = [int(v) if v == v else None for v in hist["Volume"]]

        # Build v8-compatible structure
        result = {
            "chart": {
                "result": [{
                    "meta": {
                        "symbol": symbol,
                        "regularMarketPrice": closes[-1] if closes else None,
                        "previousClose": closes[-2] if len(closes) >= 2 else None,
                    },
                    "timestamp": timestamps,
                    "indicators": {
                        "quote": [{
                            "open": opens,
                            "high": highs,
                            "low": lows,
                            "close": closes,
                            "volume": volumes,
                        }]
                    }
                }]
            }
        }
        return result
    except Exception as e:
        print(f"  [ERROR] {symbol}: {e}")
        return None


def main():
    output_dir = os.environ.get("OUTPUT_DIR", str(Path(__file__).parent.parent))
    output_path = os.path.join(output_dir, "data.json")

    print("=" * 60)
    print("Dashboard Data Updater (yfinance)")
    print(f"Symbols: {len(SYMBOLS)}")
    print(f"yfinance version: {yf.__version__}")
    print("=" * 60)

    results = {}
    for i, sym in enumerate(SYMBOLS):
        print(f"  [{i+1}/{len(SYMBOLS)}] {sym}...", end=" ", flush=True)
        data = fetch_one(sym)
        if data:
            results[sym] = data
            print("OK")
        else:
            print("SKIP")
        # Small delay to avoid rate limits
        if i < len(SYMBOLS) - 1:
            time.sleep(0.3)

    print(f"\n[INFO] Fetched {len(results)}/{len(SYMBOLS)} symbols")

    if len(results) == 0:
        print("[ERROR] No data fetched at all — aborting")
        sys.exit(1)

    with open(output_path, "w") as f:
        json.dump(results, f)

    size_kb = os.path.getsize(output_path) / 1024
    print(f"[OK] Saved to {output_path} ({size_kb:.0f} KB)")


if __name__ == "__main__":
    main()
