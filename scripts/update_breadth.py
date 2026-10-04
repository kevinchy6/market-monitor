#!/usr/bin/env python3
"""
US Market Breadth Calculator — liquid common-stock + ADR pool.

Universe (Phase 0, one Nasdaq screener call): every US-listed common stock /
ADR with market cap ≥ $500M and price ≥ $3 (warrants, units, rights,
preferreds, notes excluded). ~3,000 names. Falls back to the cached
`breadth_universe.txt` if the API is unavailable.

For the latest session it computes:
  - Advancers vs decliners              (close vs prior close)
  - New 52-week highs vs new lows       (high ≥ prior-252-day max / low ≤ min)
  - Above open vs below open            (close vs today's open)
  - Up on volume vs down on volume      (advance/decline on volume > 50-day avg)
  - Up over 4% vs down over 4%
  - % above 20-day / 50-day moving average
  - NH-NL line (cumulative new highs − new lows, last ~250 sessions)

Yahoo returns the newest daily bar with NaN fields for hours after the close;
those rows are repaired from the v8 chart endpoint (official OHLCV) so the
numbers always describe the session in `as_of`.

Outputs breadth.json for the frontend.
"""

import json
import math
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen, Request

try:
    import yfinance as yf
    import pandas as pd
    import numpy as np
except ImportError:
    print("[ERROR] yfinance/pandas not installed. Run: pip install yfinance")
    sys.exit(1)

# ─── CONFIG ───────────────────────────────────────────────────────────
MCAP_MIN = 500_000_000      # $500M
PRICE_MIN = 3.0
BATCH_SIZE = 100            # symbols per yfinance.download batch
SLEEP_BETWEEN = 0.5         # seconds between batches
HISTORY_PERIOD = "2y"       # need 252 prior sessions for 52-week highs/lows + NH-NL history
NHNL_POINTS = 250           # sessions kept in the NH-NL line
OUTPUT_FILE = "breadth.json"
SCRIPT_DIR = Path(__file__).parent
UNIVERSE_CACHE = SCRIPT_DIR / "breadth_universe.txt"

BAD_NAME = re.compile(
    r"warrant|\bunits?\b|\brights?\b|preferred|preference|subordinate|\bnotes?\b|"
    r"debenture|\bbond\b|%|\bETF\b|\bfund\b|\btrust units", re.I)


def log(msg):
    print(msg, flush=True)


# ─── STEP 1: Universe ─────────────────────────────────────────────────
def fetch_nasdaq_rows():
    url = ("https://api.nasdaq.com/api/screener/stocks"
           "?tableonly=true&limit=25&offset=0&download=true")
    req = Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://www.nasdaq.com",
        "Referer": "https://www.nasdaq.com/",
    })
    for attempt in range(3):
        try:
            with urlopen(req, timeout=60) as resp:
                rows = json.loads(resp.read().decode())["data"]["rows"]
            if rows and len(rows) > 1000:
                return rows
        except Exception as e:
            log(f"[WARN] Nasdaq screener attempt {attempt + 1} failed: {e}")
            time.sleep(10)
    return None


def build_universe():
    rows = fetch_nasdaq_rows()
    if rows is None:
        if UNIVERSE_CACHE.exists():
            syms = [l.strip() for l in UNIVERSE_CACHE.read_text().splitlines() if l.strip()]
            log(f"[INFO] Nasdaq API unavailable — using cached universe ({len(syms)})")
            return syms
        log("[ERROR] No universe available")
        sys.exit(1)

    syms = []
    for r in rows:
        try:
            s = r["symbol"].strip()
            if "^" in s or " " in s or not s:
                continue                      # preferreds / units
            if BAD_NAME.search(r.get("name") or ""):
                continue
            mcap = float(r.get("marketCap") or 0)
            px = float((r.get("lastsale") or "$0").replace("$", "").replace(",", ""))
            if mcap < MCAP_MIN or px < PRICE_MIN:
                continue
            syms.append(s.replace("/", "-").replace(".", "-"))
        except Exception:
            continue
    syms = sorted(set(syms))
    log(f"[INFO] Universe: {len(rows)} listed → {len(syms)} common stock + ADR, cap ≥ ${MCAP_MIN/1e6:.0f}M, price ≥ ${PRICE_MIN:.0f}")
    try:
        UNIVERSE_CACHE.write_text("\n".join(syms) + "\n")
    except Exception:
        pass
    return syms


# ─── STEP 2: History ──────────────────────────────────────────────────
FIELDS = ["Open", "High", "Low", "Close", "Volume"]


def fetch_history(symbols):
    """Return {sym: DataFrame[Open, High, Low, Close, Volume]} (daily, oldest→newest)."""
    out = {}
    total = len(symbols)
    nb = (total + BATCH_SIZE - 1) // BATCH_SIZE
    log(f"[INFO] Downloading {HISTORY_PERIOD} daily history for {total} symbols in {nb} batches...")
    t0 = time.time()
    for i in range(0, total, BATCH_SIZE):
        batch = symbols[i:i + BATCH_SIZE]
        for attempt in range(2):
            try:
                df = yf.download(batch, period=HISTORY_PERIOD, interval="1d", group_by="ticker",
                                 progress=False, threads=True, auto_adjust=False)
                break
            except Exception as e:
                log(f"  [WARN] batch {i // BATCH_SIZE + 1} attempt {attempt + 1} failed: {str(e)[:80]}")
                df = None
                time.sleep(5)
        if df is None or df.empty:
            continue
        for s in batch:
            try:
                sdf = df if len(batch) == 1 else df[s]
                sdf = sdf[FIELDS]
                if sdf["Close"].dropna().shape[0] < 60:
                    continue
                out[s] = sdf
            except Exception:
                pass
        if (i // BATCH_SIZE + 1) % 5 == 0 or i + BATCH_SIZE >= total:
            log(f"  batch {i // BATCH_SIZE + 1}/{nb} done — {len(out)} ok, {time.time() - t0:.0f}s")
        if i + BATCH_SIZE < total:
            time.sleep(SLEEP_BETWEEN)
    return out


def fetch_official_bars(symbols, workers=8):
    """{sym: {date, Open, High, Low, Close, Volume}} from Yahoo's v8 chart endpoint
    (range=1d) — the official OHLCV of the latest session, available right after
    the close while the daily aggregator still shows NaN."""
    import concurrent.futures as cf
    from zoneinfo import ZoneInfo
    try:
        from curl_cffi import requests as rq
        mk = lambda: rq.Session(impersonate="chrome")
    except Exception:
        import requests as rq
        mk = lambda: rq.Session()
    tz = ZoneInfo("America/New_York")

    def one(t):
        try:
            r = mk().get(f"https://query2.finance.yahoo.com/v8/finance/chart/{t}",
                         params={"range": "1d", "interval": "1d"}, timeout=10,
                         headers={"User-Agent": "Mozilla/5.0"}).json()
            res = r.get("chart", {}).get("result")
            if not res:
                return t, None
            res = res[0]
            m = res.get("meta", {})
            q = res.get("indicators", {}).get("quote", [{}])[0]
            ts = (res.get("timestamp") or [m.get("regularMarketTime")])[0]
            if not ts:
                return t, None
            bar = {"date": datetime.fromtimestamp(ts, tz).date()}
            for f in FIELDS:
                arr = q.get(f.lower()) or []
                v = arr[-1] if arr else None
                if f == "Close" and m.get("regularMarketPrice"):
                    v = m["regularMarketPrice"]
                if f == "Volume" and m.get("regularMarketVolume"):
                    v = m["regularMarketVolume"]
                if v is None or (isinstance(v, float) and math.isnan(v)):
                    return t, None
                bar[f] = float(v)
            return t, bar
        except Exception:
            return t, None

    out = {}
    with cf.ThreadPoolExecutor(workers) as ex:
        for t, b in ex.map(one, symbols):
            if b:
                out[t] = b
    return out


def repair_latest_bars(hist):
    """Fill / append the newest session where Yahoo's daily bar is NaN."""
    need = []
    for s, df in hist.items():
        last = df.iloc[-1]
        if last[FIELDS].isna().any():
            need.append(s)
    if not need:
        return hist
    log(f"[INFO] {len(need)} symbols have an incomplete latest daily bar; repairing from v8 chart...")
    bars = fetch_official_bars(need)
    fixed = dropped = 0
    for s in need:
        df = hist[s]
        b = bars.get(s)
        last_idx = df.index[-1]
        if b and b["date"] == last_idx.date():
            for f in FIELDS:
                if pd.isna(df.at[last_idx, f]):
                    df.at[last_idx, f] = b[f]
            fixed += 1
        else:
            hist[s] = df.iloc[:-1]           # drop the broken row; as_of filter handles the rest
            dropped += 1
    log(f"[INFO] repaired {fixed}, dropped last row for {dropped}")
    return hist


# ─── STEP 3: Metrics ──────────────────────────────────────────────────
def calculate_breadth(hist):
    # Common as-of session = most common latest date
    last_dates = {s: df.index[-1].date() for s, df in hist.items() if not df.empty}
    as_of = Counter(last_dates.values()).most_common(1)[0][0]
    use = {s: df for s, df in hist.items() if last_dates.get(s) == as_of}
    log(f"[INFO] as_of {as_of}: {len(use)}/{len(hist)} symbols on that session")

    adv = dec = unch = 0
    nh = nl = 0
    above_open = below_open = 0
    up_vol = down_vol = 0
    up4 = down4 = 0
    a20 = a50 = n20 = n50 = 0
    counted = 0

    # NH-NL history: per-date counts across the pool
    nh_hist = Counter()
    nl_hist = Counter()

    for s, df in use.items():
        c = df["Close"].to_numpy(dtype=float)
        o = df["Open"].to_numpy(dtype=float)
        h = df["High"].to_numpy(dtype=float)
        l = df["Low"].to_numpy(dtype=float)
        v = df["Volume"].to_numpy(dtype=float)
        n = len(c)
        if n < 2 or math.isnan(c[-1]) or math.isnan(c[-2]) or c[-2] <= 0:
            continue
        counted += 1
        cur, prev = c[-1], c[-2]
        chg = cur / prev - 1

        if cur > prev:
            adv += 1
        elif cur < prev:
            dec += 1
        else:
            unch += 1

        if not math.isnan(o[-1]):
            if cur > o[-1]:
                above_open += 1
            elif cur < o[-1]:
                below_open += 1

        if n >= 51:
            avgv = np.nanmean(v[-51:-1])
            if not math.isnan(v[-1]) and avgv > 0 and v[-1] > avgv:
                if cur > prev:
                    up_vol += 1
                elif cur < prev:
                    down_vol += 1

        if chg >= 0.04:
            up4 += 1
        elif chg <= -0.04:
            down4 += 1

        if n >= 20:
            n20 += 1
            if cur > np.nanmean(c[-20:]):
                a20 += 1
        if n >= 50:
            n50 += 1
            if cur > np.nanmean(c[-50:]):
                a50 += 1

        # 52-week new highs / lows — today and historical (for the NH-NL line)
        if n >= 253:
            hs = pd.Series(h)
            ls = pd.Series(l)
            prior_max = hs.shift(1).rolling(252, min_periods=200).max().to_numpy()
            prior_min = ls.shift(1).rolling(252, min_periods=200).min().to_numpy()
            start = max(252, n - NHNL_POINTS)
            dates = df.index[start:]
            is_nh = h[start:] >= prior_max[start:]
            is_nl = l[start:] <= prior_min[start:]
            for d, x, y in zip(dates, is_nh, is_nl):
                if x:
                    nh_hist[d.date()] += 1
                if y:
                    nl_hist[d.date()] += 1
            if is_nh[-1]:
                nh += 1
            if is_nl[-1]:
                nl += 1
        elif n >= 60:
            # young listing: compare with full available history
            if h[-1] >= np.nanmax(h[:-1]):
                nh += 1
                nh_hist[as_of] += 1
            if l[-1] <= np.nanmin(l[:-1]):
                nl += 1
                nl_hist[as_of] += 1

    if counted == 0:
        log("[ERROR] No valid data to compute breadth")
        sys.exit(1)

    def pct(a, b):
        return round(a / (a + b) * 100, 1) if (a + b) > 0 else None

    adv_pct = round(adv / counted * 100, 1)
    dec_pct = round(dec / counted * 100, 1)

    # NH-NL cumulative line
    all_dates = sorted(set(nh_hist) | set(nl_hist))
    all_dates = [d for d in all_dates if d <= as_of][-NHNL_POINTS:]
    cum = 0
    line = []
    for d in all_dates:
        cum += nh_hist[d] - nl_hist[d]
        line.append([d.isoformat(), nh_hist[d], nl_hist[d], cum])

    if adv_pct > 60:
        label, label_class = "STRONG BREADTH", "strong"
    elif adv_pct < 40:
        label, label_class = "WEAK BREADTH", "weak"
    else:
        label, label_class = "NEUTRAL", "neutral"

    # Session state: intraday if as_of is today (ET) and before 16:05 ET
    from zoneinfo import ZoneInfo
    now_et = datetime.now(ZoneInfo("America/New_York"))
    intraday = (as_of == now_et.date()) and (now_et.hour, now_et.minute) < (16, 5)

    return {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "as_of": as_of.isoformat(),
        "session_state": "intraday" if intraday else "eod",
        "universe": {
            "name": "Common stock + ADR liquid pool",
            "size": len(use),
            "mcap_min": MCAP_MIN,
            "price_min": PRICE_MIN,
        },
        "stocks_counted": counted,
        # legacy keys (kept for compatibility)
        "advancers": adv, "decliners": dec, "unchanged": unch,
        "adv_pct": adv_pct, "dec_pct": dec_pct,
        "above_20d_pct": round(a20 / n20 * 100, 1) if n20 else None,
        "above_50d_pct": round(a50 / n50 * 100, 1) if n50 else None,
        "label": label, "label_class": label_class,
        # new pairs: [up, down, up-share %]
        "pairs": {
            "adv_dec":   {"up": adv,        "down": dec,        "pct": pct(adv, dec)},
            "nh_nl":     {"up": nh,         "down": nl,         "pct": pct(nh, nl)},
            "open":      {"up": above_open, "down": below_open, "pct": pct(above_open, below_open)},
            "volume":    {"up": up_vol,     "down": down_vol,   "pct": pct(up_vol, down_vol)},
            "pct4":      {"up": up4,        "down": down4,      "pct": pct(up4, down4)},
        },
        "nhnl_line": line,                    # [[date, nh, nl, cumulative], ...]
        "nhnl_value": line[-1][3] if line else None,
    }


# ─── MAIN ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log("=" * 60)
    log("US Market Breadth — liquid pool (yfinance)")
    log(f"yfinance version: {yf.__version__}")
    log("=" * 60)

    output_dir = os.environ.get("OUTPUT_DIR", str(SCRIPT_DIR.parent))
    output_path = os.path.join(output_dir, OUTPUT_FILE)

    t0 = time.time()
    symbols = build_universe()
    hist = fetch_history(symbols)
    hist = repair_latest_bars(hist)
    b = calculate_breadth(hist)
    p = b["pairs"]

    log("\n" + "=" * 60)
    log(f"  Session:          {b['as_of']} ({b['session_state']})")
    log(f"  Pool:             {b['universe']['size']} (counted {b['stocks_counted']})")
    log(f"  Adv / Dec:        {p['adv_dec']['up']} / {p['adv_dec']['down']}  ({p['adv_dec']['pct']}%)")
    log(f"  New hi / lo:      {p['nh_nl']['up']} / {p['nh_nl']['down']}  ({p['nh_nl']['pct']}%)")
    log(f"  Above/below open: {p['open']['up']} / {p['open']['down']}  ({p['open']['pct']}%)")
    log(f"  Up/down on vol:   {p['volume']['up']} / {p['volume']['down']}  ({p['volume']['pct']}%)")
    log(f"  Up/down >4%:      {p['pct4']['up']} / {p['pct4']['down']}  ({p['pct4']['pct']}%)")
    log(f"  > 20D / 50D MA:   {b['above_20d_pct']}% / {b['above_50d_pct']}%")
    log(f"  NH-NL line:       {b['nhnl_value']} ({len(b['nhnl_line'])} pts)")
    log(f"  Total time:       {time.time() - t0:.0f}s")
    log("=" * 60)

    os.makedirs(output_dir, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(b, f, indent=1)
    log(f"\n[OK] Saved to {output_path}")
