#!/usr/bin/env python3
"""
S&P 500 constituent leaders & laggards.

For every current S&P 500 member it computes price returns (split-adjusted,
dividends excluded) over four windows, all measured from the last close
before the window starts to the latest session (`as_of`):

  - YTD          last close of prior year      -> as_of
  - QTD          last close of prior quarter   -> as_of
  - MTD          last close of prior month     -> as_of
  - Last year    last close of year-2          -> last close of year-1

Constituents come from Wikipedia's "List of S&P 500 companies" table, with
the datasets/s-and-p-500-companies CSV and a local cache as fallbacks.
Members that were not yet listed at the start of a window get no return for
that window (shown as excluded). Members are the CURRENT index list, so the
"last year" ranking has survivorship bias (names removed since are absent).

Outputs sp500_leaders.json:
  {
    updated, as_of, session_state, count,
    periods: {ytd|qtd|mtd|ly: {label, start, end, n, median, pct_up, index}},
    rows: [[sym, name, sector, price, ytd, qtd, mtd, ly], ...]
  }
The frontend ranks rows client-side (top / bottom N, sector filter).
"""

import csv
import io
import json
import math
import os
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen

try:
    import numpy as np
    import pandas as pd
    import yfinance as yf
except ImportError:
    print("[ERROR] yfinance/pandas not installed. Run: pip install yfinance lxml")
    sys.exit(1)

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))
from update_breadth import fetch_official_bars  # noqa: E402  (reuse v8 latest-bar repair)

OUTPUT_FILE = "sp500_leaders.json"
CACHE_FILE = SCRIPT_DIR / "sp500_constituents.csv"
BATCH_SIZE = 100
SLEEP_BETWEEN = 0.5
INDEX_SYMBOL = "^GSPC"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def log(msg):
    print(msg, flush=True)


# ─── STEP 1: Constituents ─────────────────────────────────────────────
def _get(url, timeout=30):
    with urlopen(Request(url, headers={"User-Agent": UA}), timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def from_wikipedia():
    html = _get("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")
    t = pd.read_html(io.StringIO(html), attrs={"id": "constituents"})[0]
    return [(str(r["Symbol"]).strip(), str(r["Security"]).strip(), str(r["GICS Sector"]).strip())
            for _, r in t.iterrows()]


def from_datasets_csv():
    txt = _get("https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv")
    rd = csv.DictReader(io.StringIO(txt))
    return [(r["Symbol"].strip(), (r.get("Security") or r.get("Name") or "").strip(),
             (r.get("GICS Sector") or r.get("Sector") or "").strip()) for r in rd]


def load_constituents():
    for name, fn in (("Wikipedia", from_wikipedia), ("datasets CSV", from_datasets_csv)):
        for attempt in range(2):
            try:
                rows = fn()
                if len(rows) >= 480:
                    log(f"[INFO] Constituents from {name}: {len(rows)}")
                    with open(CACHE_FILE, "w", newline="") as f:
                        w = csv.writer(f)
                        w.writerow(["Symbol", "Security", "GICS Sector"])
                        w.writerows(sorted(rows))
                    return rows
                log(f"[WARN] {name} returned only {len(rows)} rows")
            except Exception as e:
                log(f"[WARN] {name} attempt {attempt + 1} failed: {str(e)[:120]}")
                time.sleep(3)
    if CACHE_FILE.exists():
        with open(CACHE_FILE) as f:
            rows = [(r["Symbol"], r["Security"], r["GICS Sector"]) for r in csv.DictReader(f)]
        log(f"[INFO] Using cached constituents ({len(rows)})")
        return rows
    log("[ERROR] No constituent list available")
    sys.exit(1)


def yf_sym(s):
    return s.replace(".", "-").replace("/", "-")


# ─── STEP 2: Price history ────────────────────────────────────────────
def fetch_closes(symbols, start):
    """{sym: pd.Series of split-adjusted closes (no dividend adj), date-indexed}."""
    out = {}
    nb = (len(symbols) + BATCH_SIZE - 1) // BATCH_SIZE
    log(f"[INFO] Downloading daily closes since {start} for {len(symbols)} symbols in {nb} batches...")
    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i:i + BATCH_SIZE]
        df = None
        for attempt in range(2):
            try:
                df = yf.download(batch, start=start, interval="1d", group_by="ticker",
                                 progress=False, threads=True, auto_adjust=False)
                break
            except Exception as e:
                log(f"  [WARN] batch {i // BATCH_SIZE + 1} attempt {attempt + 1}: {str(e)[:80]}")
                time.sleep(5)
        if df is None or df.empty:
            continue
        for s in batch:
            try:
                c = (df if len(batch) == 1 else df[s])["Close"]
                out[s] = c
            except Exception:
                pass
        if i + BATCH_SIZE < len(symbols):
            time.sleep(SLEEP_BETWEEN)
    log(f"[INFO] Got history for {len(out)}/{len(symbols)}")
    return out


def repair_latest(closes):
    """Yahoo's newest daily bar is often NaN for a while after the close (and
    during the session for some names). Fill it from the v8 chart endpoint."""
    if not closes:
        return closes
    # latest index date present in the downloads (the session we want)
    latest = Counter(c.index[-1].date() for c in closes.values() if len(c)).most_common(1)[0][0]
    need = [s for s, c in closes.items()
            if len(c) and c.index[-1].date() == latest and pd.isna(c.iloc[-1])]
    if need:
        log(f"[INFO] {len(need)} symbols have a NaN latest close; repairing from v8 chart...")
        bars = fetch_official_bars(need)
        fixed = 0
        for s in need:
            b = bars.get(s)
            if b and b["date"] == latest:
                closes[s] = closes[s].copy()
                closes[s].iloc[-1] = b["Close"]
                fixed += 1
        log(f"[INFO] repaired {fixed}/{len(need)}")
    return {s: c.dropna() for s, c in closes.items()}


# ─── STEP 3: Returns ──────────────────────────────────────────────────
def close_on_or_before(c, d):
    """(date, close) of the last bar on or before `d`, or None."""
    sub = c[c.index.date <= d]
    if sub.empty:
        return None
    return sub.index[-1].date(), float(sub.iloc[-1])


def windows(as_of):
    y, m = as_of.year, as_of.month
    q_start_month = 3 * ((m - 1) // 3) + 1
    return {
        "ytd": {"label": "YTD", "base": date(y, 1, 1) - timedelta(days=1), "end": as_of},
        "qtd": {"label": f"QTD (Q{(m - 1) // 3 + 1})",
                "base": date(y, q_start_month, 1) - timedelta(days=1), "end": as_of},
        "mtd": {"label": "MTD", "base": date(y, m, 1) - timedelta(days=1), "end": as_of},
        "ly":  {"label": str(y - 1), "base": date(y - 1, 1, 1) - timedelta(days=1),
                "end": date(y, 1, 1) - timedelta(days=1)},
    }


def period_return(c, base_d, end_d, as_of_mode):
    b = close_on_or_before(c, base_d)
    if b is None:
        return None
    # Must have traded within ~1 week before the window start (excludes late IPOs/spin-offs
    # whose first bar is after the boundary — close_on_or_before already returns None — and
    # stale/halted histories).
    if (base_d - b[0]).days > 7:
        return None
    if as_of_mode:
        e = (c.index[-1].date(), float(c.iloc[-1]))
        if e[0] != end_d:
            return None
    else:
        e = close_on_or_before(c, end_d)
        if e is None or (end_d - e[0]).days > 7:
            return None
    if b[1] <= 0:
        return None
    return round((e[1] / b[1] - 1) * 100, 2)


def main():
    t0 = time.time()
    output_dir = os.environ.get("OUTPUT_DIR", str(SCRIPT_DIR.parent))
    output_path = os.path.join(output_dir, OUTPUT_FILE)

    members = load_constituents()
    meta = {}
    for s, name, sector in members:
        meta[yf_sym(s)] = (s, name, sector)
    symbols = sorted(meta)

    today = datetime.now(timezone.utc).date()
    start = date(today.year - 2, 12, 1).isoformat()   # covers last trading day of year-2
    closes = fetch_closes(symbols + [INDEX_SYMBOL], start)
    closes = repair_latest(closes)

    idx = closes.pop(INDEX_SYMBOL, None)
    last_dates = Counter(c.index[-1].date() for c in closes.values() if len(c))
    if not last_dates:
        log("[ERROR] No price data")
        sys.exit(1)
    as_of = last_dates.most_common(1)[0][0]
    log(f"[INFO] as_of {as_of} ({last_dates[as_of]}/{len(closes)} members on that session)")

    W = windows(as_of)
    rows = []
    for ys in symbols:
        c = closes.get(ys)
        if c is None or c.empty:
            continue
        sym, name, sector = meta[ys]
        price = round(float(c.iloc[-1]), 2) if c.index[-1].date() == as_of else None
        rets = [period_return(c, W[k]["base"], W[k]["end"], k != "ly") for k in ("ytd", "qtd", "mtd", "ly")]
        if price is None and all(r is None for r in rets):
            continue
        rows.append([sym, name, sector, price] + rets)

    periods = {}
    for j, k in enumerate(("ytd", "qtd", "mtd", "ly")):
        vals = [r[4 + j] for r in rows if r[4 + j] is not None]
        w = W[k]
        idx_ret = period_return(idx, w["base"], w["end"], k != "ly") if idx is not None and len(idx) else None
        # actual base / end trading dates (from the index, else first member)
        ref = idx if idx is not None and len(idx) else next(iter(closes.values()))
        bd = close_on_or_before(ref, w["base"])
        ed = close_on_or_before(ref, w["end"])
        periods[k] = {
            "label": w["label"],
            "start": bd[0].isoformat() if bd else None,
            "end": ed[0].isoformat() if ed else None,
            "n": len(vals),
            "median": round(float(np.median(vals)), 2) if vals else None,
            "pct_up": round(sum(v > 0 for v in vals) / len(vals) * 100, 1) if vals else None,
            "index": idx_ret,
        }

    from zoneinfo import ZoneInfo
    now_et = datetime.now(ZoneInfo("America/New_York"))
    intraday = (as_of == now_et.date()) and (now_et.hour, now_et.minute) < (16, 5)

    out = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "as_of": as_of.isoformat(),
        "session_state": "intraday" if intraday else "eod",
        "count": len(rows),
        "method": "Price return, split-adjusted, dividends excluded; current S&P 500 members.",
        "periods": periods,
        "cols": ["symbol", "name", "sector", "price", "ytd", "qtd", "mtd", "ly"],
        "rows": rows,
    }

    log("\n" + "=" * 60)
    for k, p in periods.items():
        vals = sorted((r[4 + list(periods).index(k)], r[0]) for r in rows if r[4 + list(periods).index(k)] is not None)
        top = ", ".join(f"{s} {v:+.1f}%" for v, s in vals[-3:][::-1])
        bot = ", ".join(f"{s} {v:+.1f}%" for v, s in vals[:3])
        log(f"  {p['label']:<10} {p['start']}→{p['end']}  n={p['n']}  SPX {p['index']}%  "
            f"median {p['median']}%  up {p['pct_up']}%")
        log(f"     top: {top}")
        log(f"     bot: {bot}")
    log(f"  Total time: {time.time() - t0:.0f}s")
    log("=" * 60)

    os.makedirs(output_dir, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    log(f"[OK] Saved {len(rows)} rows to {output_path}")


if __name__ == "__main__":
    main()
