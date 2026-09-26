#!/usr/bin/env python3
"""
Update data.json — fetch 1-year daily candles for all dashboard tickers
from Yahoo Finance using the yfinance library (handles auth/crumb automatically).
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
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
    "MAGS", "ARKK", "RSP", "IWM", "TLT", "DIA", "SPY", "QQQ",
    # Sectors
    "GDX", "IYT", "IGV", "XLF", "XRT", "XHB", "KRE", "IYR",
    "ITA", "XLI", "XLE", "XLY", "XLB", "XLP", "XLV", "BLOK",
    "XLU", "XBI", "ARKG", "HACK", "DRAM", "XLK", "KWEB", "TAN", "SOXX",
]



def expected_last_trading_date():
    """Most recent US weekday whose daily candle should exist by now (UTC).
    Holidays may cause a false 'stale' flag — retries then accept, harmless."""
    now = datetime.now(timezone.utc)
    d = now.date()
    # before ~14:00 UTC the current day's candle may not exist yet
    if now.hour < 14:
        d -= timedelta(days=1)
    while d.weekday() >= 5:  # Sat/Sun
        d -= timedelta(days=1)
    return d


def _fetch_latest_close(symbol):
    """Ask Yahoo's v8 chart endpoint for `meta.regularMarketPrice` -- the most
    recent print, populated even when the daily bar hasn't aggregated yet.
    Returns (price, session_date) or None."""
    try:
        import requests
        from datetime import datetime as _dt, timezone as _tz
        r = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
            params={"range": "5d", "interval": "1d"},
            headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        j = r.json()
        res = j["chart"]["result"][0]
        px = res["meta"].get("regularMarketPrice")
        if not isinstance(px, (int, float)) or px != px:
            return None
        session_ts = res["meta"].get("regularMarketTime") or res["timestamp"][-1]
        session_date = _dt.fromtimestamp(int(session_ts), _tz.utc).date()
        return (float(px), session_date)
    except Exception:
        return None


def fetch_one(symbol):
    """Fetch 1-year daily chart data for one symbol using yfinance."""
    try:
        ticker = yf.Ticker(symbol)
        # Cache-busting: explicit start/end timestamps make the request URL unique
        # per run, so Yahoo's CDN can't serve a stale cached "range=1y" response.
        now = datetime.now(timezone.utc)
        hist = None
        latest_price_meta = None   # from ticker.fast_info / v8 chart, only used
                                   # to fill a trailing NaN close (Yahoo lag).
        for attempt in range(3):
            h = ticker.history(start=now - timedelta(days=370), end=now + timedelta(days=1),
                               interval="1d")
            if h.empty:
                time.sleep(5)
                continue
            # If the LAST row has NaN Close, Yahoo's daily aggregator is lagging
            # today's close. `meta.regularMarketPrice` from the v8 chart endpoint
            # usually already has it -- pull that and stitch it into the frame
            # before we drop the NaN row.
            if len(h) and h["Close"].iloc[-1] != h["Close"].iloc[-1]:
                latest_price_meta = _fetch_latest_close(symbol)
                if latest_price_meta:
                    px, ts = latest_price_meta
                    # Only stitch if the meta timestamp matches (or is later than)
                    # that NaN row -- guards against filling a random stale price.
                    last_ts = h.index[-1]
                    if ts >= last_ts.date():
                        h.loc[h.index[-1], ["Open", "High", "Low", "Close"]] = px
                        if h["Volume"].iloc[-1] != h["Volume"].iloc[-1]:
                            h.loc[h.index[-1], "Volume"] = 0
            h = h.dropna(subset=["Close"], how="any")
            if h.empty:
                time.sleep(5)
                continue
            hist = h
            last_date = h.index[-1].date()
            if last_date >= expected_last_trading_date():
                break  # fresh
            print(f"[stale: last={last_date}, retry {attempt+1}]", end=" ", flush=True)
            time.sleep(10)
        if hist is None or hist.empty:
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

    existing = {}
    if os.path.exists(output_path):
        try:
            with open(output_path) as f:
                existing = json.load(f)
        except Exception:
            pass

    def old_is_usable(sym):
        """An existing entry is only worth keeping if its regularMarketPrice is
        a real number. Otherwise the frontend renders blanks/zeros."""
        try:
            price = existing[sym]["chart"]["result"][0]["meta"]["regularMarketPrice"]
            return isinstance(price, (int, float)) and price == price   # not NaN
        except Exception:
            return False

    results = {}
    for i, sym in enumerate(SYMBOLS):
        print(f"  [{i+1}/{len(SYMBOLS)}] {sym}...", end=" ", flush=True)
        data = fetch_one(sym)
        if not data:
            # One more attempt with a longer pause -- often clears a transient
            # Yahoo rate-limit that gave us NaN closes on the first try.
            time.sleep(3)
            data = fetch_one(sym)
        if data:
            results[sym] = data
            print("OK")
        elif sym in existing and old_is_usable(sym):
            results[sym] = existing[sym]
            print("KEPT-OLD")
        else:
            print("SKIP")
        # Small delay to avoid rate limits
        if i < len(SYMBOLS) - 1:
            time.sleep(0.3)

    print(f"\n[INFO] Fetched {len(results)}/{len(SYMBOLS)} symbols")

    if len(results) == 0:
        print("[ERROR] No data fetched at all — aborting")
        sys.exit(1)

    results["_meta"] = {"updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}

    with open(output_path, "w") as f:
        json.dump(results, f)

    size_kb = os.path.getsize(output_path) / 1024
    print(f"[OK] Saved to {output_path} ({size_kb:.0f} KB)")


if __name__ == "__main__":
    main()
