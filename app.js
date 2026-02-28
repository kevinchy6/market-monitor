/* app.js — Market Monitor Dashboard */

(function () {
  'use strict';

  const CGI_BIN = '__CGI_BIN__';

  // ========================================
  // DATA CONFIGURATION
  // ========================================

  const SECTIONS = {
    alternatives: {
      title: 'Equity Alternatives',
      items: [
        { name: '10-Yr T-Note Futures', symbol: 'ZN=F' },
        { name: 'U.S. Dollar Index', symbol: 'DX-Y.NYB' },
        { name: 'T-Bond Futures', symbol: 'ZB=F' },
        { name: 'Light Crude Oil Futures', symbol: 'CL=F' },
        { name: 'Gold Futures', symbol: 'GC=F' },
        { name: 'Bitcoin', symbol: 'BTC-USD' },
      ]
    },
    global: {
      title: 'Global Equities',
      items: [
        { name: 'Europe Equity', symbol: 'IEV' },
        { name: 'Total Intl Stock', symbol: 'VXUS' },
        { name: 'Total US Stock', symbol: 'VTI' },
        { name: 'Emerging Markets', symbol: 'EEM' },
      ]
    },
    indices: {
      title: 'US Equity Indices',
      items: [
        { name: 'Innovation', symbol: 'ARKK' },
        { name: 'S&P 500 Equal Wt', symbol: 'RSP' },
        { name: 'Russell 2000', symbol: 'IWM' },
        { name: 'LT US Treasuries', symbol: 'TLT' },
        { name: 'Dow Jones', symbol: 'DIA' },
        { name: 'S&P 500', symbol: 'SPY' },
        { name: 'Nasdaq', symbol: 'QQQ' },
      ]
    },
    sectors: {
      title: 'Sectors',
      items: [
        { name: 'Gold Miners', symbol: 'GDX' },
        { name: 'Transportation', symbol: 'IYT' },
        { name: 'Software', symbol: 'IGV' },
        { name: 'Financials', symbol: 'XLF' },
        { name: 'Retail', symbol: 'XRT' },
        { name: 'Home Builders', symbol: 'XHB' },
        { name: 'Regional Banks', symbol: 'KRE' },
        { name: 'Real Estate', symbol: 'IYR' },
        { name: 'Aerospace + Defense', symbol: 'ITA' },
        { name: 'Industrials', symbol: 'XLI' },
        { name: 'Energy', symbol: 'XLE' },
        { name: 'Consumer Discr.', symbol: 'XLY' },
        { name: 'Materials', symbol: 'XLB' },
        { name: 'Consumer Staples', symbol: 'XLP' },
        { name: 'Health Care', symbol: 'XLV' },
        { name: 'Blockchain', symbol: 'BLOK' },
        { name: 'Utilities', symbol: 'XLU' },
        { name: 'Biotech', symbol: 'XBI' },
        { name: 'US Cannabis', symbol: 'MSOS' },
        { name: 'Technology', symbol: 'XLK' },
        { name: 'Chinese Tech', symbol: 'KWEB' },
        { name: 'Solar Energy', symbol: 'TAN' },
        { name: 'Semiconductors', symbol: 'SOXX' },
      ]
    }
  };

  const ALL_SYMBOLS = [];
  Object.values(SECTIONS).forEach(sec => {
    sec.items.forEach(item => ALL_SYMBOLS.push(item.symbol));
  });

  const dataStore = {};       // symbol -> indicators
  const candleStore = {};     // symbol -> raw candles (needed for period returns)
  let lastUpdated = null;
  let isLoading = false;

  // Sort state per section: { key: colIndex, dir: 'asc' | 'desc' }
  const sortState = {};

  // ========================================
  // DATA PARSERS
  // ========================================

  function parseChartData(data) {
    try {
      const result = data.chart.result[0];
      const timestamps = result.timestamp;
      const quote = result.indicators.quote[0];
      const candles = [];
      for (let i = 0; i < timestamps.length; i++) {
        if (quote.close[i] != null && quote.high[i] != null && quote.low[i] != null) {
          candles.push({
            time: timestamps[i],
            date: new Date(timestamps[i] * 1000),
            open: quote.open[i],
            high: quote.high[i],
            low: quote.low[i],
            close: quote.close[i],
            volume: quote.volume[i]
          });
        }
      }
      return candles;
    } catch (e) { return null; }
  }

  // ========================================
  // TECHNICAL CALCULATIONS
  // ========================================

  function calcSMA(closes, period) {
    if (closes.length < period) return null;
    let sum = 0;
    for (let i = closes.length - period; i < closes.length; i++) sum += closes[i];
    return sum / period;
  }

  function calcATR(candles, period) {
    if (candles.length < period + 1) return null;
    const trs = [];
    for (let i = 1; i < candles.length; i++) {
      const c = candles[i], pc = candles[i - 1].close;
      trs.push(Math.max(c.high - c.low, Math.abs(c.high - pc), Math.abs(c.low - pc)));
    }
    if (trs.length < period) return null;
    let sum = 0;
    for (let i = trs.length - period; i < trs.length; i++) sum += trs[i];
    return sum / period;
  }

  /**
   * Find the close price on or just before a given date.
   * Returns null if no candle found.
   */
  function findCloseOnOrBefore(candles, targetDate) {
    // targetDate is a Date object — we compare by date string YYYY-MM-DD
    const targetStr = dateStr(targetDate);
    for (let i = candles.length - 1; i >= 0; i--) {
      if (dateStr(candles[i].date) <= targetStr) return candles[i].close;
    }
    return null;
  }

  function dateStr(d) {
    return d.getFullYear() + '-' +
      String(d.getMonth() + 1).padStart(2, '0') + '-' +
      String(d.getDate()).padStart(2, '0');
  }

  /**
   * Compute WTD, MTD, YTD returns from candle data.
   */
  function computePeriodReturns(candles) {
    if (!candles || candles.length < 5) return { wtd: null, mtd: null, ytd: null };

    const lastCandle = candles[candles.length - 1];
    const currentClose = lastCandle.close;
    const lastDate = lastCandle.date;

    // --- WTD: from last Friday's close (or most recent trading day before this week) ---
    // Get the Monday of the current week
    const dayOfWeek = lastDate.getDay(); // 0=Sun, 1=Mon, ...
    const mondayOffset = dayOfWeek === 0 ? 6 : dayOfWeek - 1;
    const monday = new Date(lastDate);
    monday.setDate(monday.getDate() - mondayOffset);
    // We want the close of the last trading day BEFORE Monday = last Friday (or before)
    const dayBeforeMonday = new Date(monday);
    dayBeforeMonday.setDate(dayBeforeMonday.getDate() - 1);
    const wtdBase = findCloseOnOrBefore(candles, dayBeforeMonday);
    const wtd = wtdBase ? ((currentClose - wtdBase) / wtdBase) * 100 : null;

    // --- MTD: from last day of prior month ---
    const firstOfMonth = new Date(lastDate.getFullYear(), lastDate.getMonth(), 1);
    const lastDayPrevMonth = new Date(firstOfMonth);
    lastDayPrevMonth.setDate(lastDayPrevMonth.getDate() - 1);
    const mtdBase = findCloseOnOrBefore(candles, lastDayPrevMonth);
    const mtd = mtdBase ? ((currentClose - mtdBase) / mtdBase) * 100 : null;

    // --- YTD: from last trading day of prior year ---
    const firstOfYear = new Date(lastDate.getFullYear(), 0, 1);
    const lastDayPrevYear = new Date(firstOfYear);
    lastDayPrevYear.setDate(lastDayPrevYear.getDate() - 1);
    const ytdBase = findCloseOnOrBefore(candles, lastDayPrevYear);
    const ytd = ytdBase ? ((currentClose - ytdBase) / ytdBase) * 100 : null;

    return { wtd, mtd, ytd };
  }

  function computeIndicators(candles) {
    if (!candles || candles.length < 10) return null;

    const n = candles.length;
    const last = candles[n - 1], prev = candles[n - 2];
    const closes = candles.map(c => c.close);
    const close = last.close, prevClose = prev.close;

    const price = close;
    const pctChange = ((close - prevClose) / prevClose) * 100;

    const atr = calcATR(candles, 14);
    const atrDelta = atr ? (close - prevClose) / atr : null;

    const dayRange = last.high - last.low;
    const dcr = dayRange > 0 ? ((close - last.low) / dayRange) * 100 : 50;

    let high52 = -Infinity, low52 = Infinity;
    for (let i = 0; i < n; i++) {
      if (candles[i].high > high52) high52 = candles[i].high;
      if (candles[i].low < low52) low52 = candles[i].low;
    }
    const range52 = high52 - low52;
    const wr52 = range52 > 0 ? ((close - low52) / range52) * 100 : 50;

    const sma20 = calcSMA(closes, 20);
    const sma50 = calcSMA(closes, 50);
    const sma100 = calcSMA(closes, 100);
    const sma200 = calcSMA(closes, 200);

    let maX = null;
    const smaList = [sma20, sma50, sma100, sma200].filter(v => v != null);
    if (smaList.length > 0) {
      maX = smaList.map(sma => ((close / sma) - 1) * 100).reduce((a, b) => a + b, 0) / smaList.length;
    }

    // ST fl
    let stFlow = { label: '--', color: 'gray' };
    if (sma20 != null && sma50 != null) {
      const pA20 = close > sma20, s20A50 = sma20 > sma50;
      const nearBoth = Math.abs(close / sma20 - 1) < 0.01 && Math.abs(close / sma50 - 1) < 0.01;
      if (nearBoth) stFlow = { label: '2C', color: 'gray' };
      else if (pA20 && s20A50) stFlow = { label: '2B', color: 'green' };
      else if (pA20 && !s20A50) stFlow = { label: '1B', color: 'light-green' };
      else if (!pA20 && s20A50) stFlow = { label: '1R', color: 'yellow' };
      else {
        const pB20 = ((sma20 - close) / sma20) * 100;
        const pGap = ((sma50 - sma20) / sma50) * 100;
        if (pB20 > 10) stFlow = { label: '4A', color: 'deep-red' };
        else if (pB20 > 5) stFlow = { label: '3A', color: 'red' };
        else if (pGap > 3) stFlow = { label: '2A', color: 'orange' };
        else stFlow = { label: '1A', color: 'orange' };
      }
    }

    // LT fl
    let ltFlow = { label: '--', color: 'gray' };
    if (sma50 != null && sma200 != null) {
      const pA50 = close > sma50, s50A200 = sma50 > sma200;
      const cross = Math.abs(sma50 / sma200 - 1) * 100;
      if (s50A200 && pA50) ltFlow = cross < 2 ? { label: '3B', color: 'bright-green' } : { label: '2C', color: 'green' };
      else if (s50A200 && !pA50) ltFlow = { label: '2C', color: 'yellow' };
      else if (!s50A200 && pA50) ltFlow = cross < 2 ? { label: '3A', color: 'orange' } : { label: '1R', color: 'orange' };
      else {
        const pB = ((sma200 - close) / sma200) * 100;
        if (pB > 15) ltFlow = { label: '4A', color: 'deep-red' };
        else if (cross < 2) ltFlow = { label: '3A', color: 'red' };
        else ltFlow = { label: '2A', color: 'red' };
      }
    }

    // Period returns
    const { wtd, mtd, ytd } = computePeriodReturns(candles);

    return { price, pctChange, atrDelta, dcr, wr52, maX, stFlow, ltFlow, wtd, mtd, ytd };
  }

  // ========================================
  // BREADTH
  // ========================================

  function computeBreadth() {
    const syms = SECTIONS.sectors.items.map(i => i.symbol);
    let adv = 0, dec = 0, total = 0, pos = [], neg = [], count = 0;
    syms.forEach(s => {
      const d = dataStore[s]; if (!d) return;
      count++; total += d.pctChange;
      if (d.pctChange > 0) { adv++; pos.push(d.pctChange); }
      else if (d.pctChange < 0) { dec++; neg.push(Math.abs(d.pctChange)); }
    });
    if (count === 0) return null;
    const advPct = (adv / count) * 100, declPct = (dec / count) * 100;
    const avgP = pos.length ? pos.reduce((a, b) => a + b, 0) / pos.length : 0;
    const avgN = neg.length ? neg.reduce((a, b) => a + b, 0) / neg.length : 0.001;
    let label, cls;
    if (advPct > 60) { label = 'STRONG BREADTH'; cls = 'strong'; }
    else if (advPct < 40) { label = 'WEAK BREADTH'; cls = 'weak'; }
    else { label = 'NEUTRAL'; cls = 'neutral'; }
    return { advPct, declPct, stick: total, strin: avgP / avgN, label, labelClass: cls };
  }

  // ========================================
  // RENDERING
  // ========================================

  function hmClass(v, type) {
    if (v == null) return 'cell-neutral';
    if (type === 'pct' || type === 'atr') {
      if (v > 2) return 'cell-positive-strong';
      if (v > 0.5) return 'cell-positive';
      if (v > -0.5) return 'cell-neutral';
      if (v > -2) return 'cell-negative';
      return 'cell-negative-strong';
    }
    if (type === 'dcr') {
      if (v > 70) return 'cell-positive-strong';
      if (v > 50) return 'cell-positive';
      if (v > 30) return 'cell-negative';
      return 'cell-negative-strong';
    }
    if (type === '52wr') {
      if (v > 80) return 'cell-positive-strong';
      if (v > 60) return 'cell-positive';
      if (v > 40) return 'cell-neutral';
      if (v > 20) return 'cell-negative';
      return 'cell-negative-strong';
    }
    if (type === 'max') {
      if (v > 5) return 'cell-positive-strong';
      if (v > 1) return 'cell-positive';
      if (v > -1) return 'cell-neutral';
      if (v > -5) return 'cell-negative';
      return 'cell-negative-strong';
    }
    if (type === 'period') {
      if (v > 5) return 'cell-positive-strong';
      if (v > 0) return 'cell-positive';
      if (v > -5) return 'cell-negative';
      return 'cell-negative-strong';
    }
    return 'cell-neutral';
  }

  const fmt = (v, d) => v == null || isNaN(v) ? '--' : v.toFixed(d);
  const fmtPrice = v => {
    if (v == null || isNaN(v)) return '--';
    return v >= 10000 ? v.toLocaleString('en-US', {maximumFractionDigits: 0}) : v.toFixed(2);
  };
  const fmtPct = v => v == null || isNaN(v) ? '--' : (v > 0 ? '+' : '') + v.toFixed(2) + '%';
  const fmtSigned = v => v == null || isNaN(v) ? '--' : (v > 0 ? '+' : '') + v.toFixed(2);

  function badge(flow) {
    if (!flow || flow.label === '--') return '<span class="flow-badge gray">--</span>';
    return `<span class="flow-badge ${flow.color}">${flow.label}</span>`;
  }

  // Column definitions for sorting
  const COLUMNS = [
    { key: 'name',   label: 'Market', sortable: true,  type: 'string' },
    { key: 'symbol', label: 'Symbol', sortable: true,  type: 'string' },
    { key: 'price',  label: 'Price',  sortable: true,  type: 'num' },
    { key: 'wtd',    label: 'WTD',    sortable: true,  type: 'num' },
    { key: 'mtd',    label: 'MTD',    sortable: true,  type: 'num' },
    { key: 'ytd',    label: 'YTD',    sortable: true,  type: 'num' },
  ];

  function getSortValue(item, colKey) {
    const ind = dataStore[item.symbol];
    if (colKey === 'name') return item.name;
    if (colKey === 'symbol') return item.symbol;
    if (!ind) return null;
    const v = ind[colKey];
    if (v == null || (typeof v === 'number' && isNaN(v))) return null;
    return v;
  }

  function getSortedItems(sectionKey) {
    const sec = SECTIONS[sectionKey];
    const items = [...sec.items]; // shallow copy to sort
    const st = sortState[sectionKey];
    if (!st) return items;

    const col = COLUMNS.find(c => c.key === st.key);
    if (!col) return items;

    items.sort((a, b) => {
      let va = getSortValue(a, st.key);
      let vb = getSortValue(b, st.key);

      // Nulls always go to bottom
      if (va == null && vb == null) return 0;
      if (va == null) return 1;
      if (vb == null) return -1;

      let cmp;
      if (col.type === 'string') {
        cmp = String(va).localeCompare(String(vb));
      } else {
        cmp = va - vb;
      }
      return st.dir === 'asc' ? cmp : -cmp;
    });

    return items;
  }

  function row(item, ind) {
    const emptyCells = '<td class="cell-error">--</td>'.repeat(4);
    if (!ind) return `<tr><td title="${item.name}">${item.name}</td><td>${item.symbol}</td>${emptyCells}</tr>`;
    const { price, wtd, mtd, ytd } = ind;
    return `<tr>
      <td title="${item.name}">${item.name}</td>
      <td>${item.symbol}</td>
      <td>${fmtPrice(price)}</td>
      <td class="${hmClass(wtd, 'period')}">${fmtPct(wtd)}</td>
      <td class="${hmClass(mtd, 'period')}">${fmtPct(mtd)}</td>
      <td class="${hmClass(ytd, 'period')}">${fmtPct(ytd)}</td>
    </tr>`;
  }

  function skeleton(count) {
    let h = '';
    for (let i = 0; i < count; i++) {
      h += `<tr class="skeleton-row">
        <td><span class="skeleton-cell wide"></span></td>
        <td><span class="skeleton-cell narrow"></span></td>
        <td><span class="skeleton-cell"></span></td>
        <td><span class="skeleton-cell"></span></td>
        <td><span class="skeleton-cell"></span></td>
        <td><span class="skeleton-cell"></span></td>
      </tr>`;
    }
    return h;
  }

  function renderSection(key) {
    const tbody = document.getElementById(`tbody-${key}`);
    if (!tbody) return;
    const items = getSortedItems(key);
    tbody.innerHTML = items.map(item => row(item, dataStore[item.symbol])).join('');
  }

  function renderBreadth() {
    const b = computeBreadth();
    const el = document.getElementById('breadth-content');
    if (!el) return;
    if (!b) { el.innerHTML = '<div class="breadth-metric"><div class="breadth-metric-label">Loading...</div><div class="breadth-metric-value">--</div></div>'; return; }
    el.innerHTML = `
      <div class="breadth-metric">
        <div class="breadth-metric-label">Advancers</div>
        <div class="breadth-metric-value" style="color:var(--color-green)">${fmt(b.advPct,0)}%</div>
        <div class="breadth-bar-container"><div class="breadth-bar" style="width:${b.advPct}%;background:var(--color-green)"></div></div>
      </div>
      <div class="breadth-metric">
        <div class="breadth-metric-label">Decliners</div>
        <div class="breadth-metric-value" style="color:var(--color-red)">${fmt(b.declPct,0)}%</div>
        <div class="breadth-bar-container"><div class="breadth-bar" style="width:${b.declPct}%;background:var(--color-red)"></div></div>
      </div>
      <div class="breadth-metric">
        <div class="breadth-metric-label">STICK</div>
        <div class="breadth-metric-value" style="color:${b.stick>=0?'var(--color-green)':'var(--color-red)'}">${fmtSigned(b.stick)}</div>
      </div>
      <div class="breadth-metric">
        <div class="breadth-metric-label">STRIN</div>
        <div class="breadth-metric-value">${fmt(b.strin,2)}</div>
      </div>
      <div class="breadth-label ${b.labelClass}">
        <span class="status-dot ${b.labelClass==='strong'?'live':b.labelClass==='weak'?'error':'stale'}"></span>
        ${b.label}
      </div>`;
  }

  function renderAll() {
    ['alternatives', 'global', 'indices', 'sectors'].forEach(renderSection);
    renderBreadth();
  }

  function showSkeletons() {
    Object.keys(SECTIONS).forEach(k => {
      const tb = document.getElementById(`tbody-${k}`);
      if (tb) tb.innerHTML = skeleton(SECTIONS[k].items.length);
    });
  }

  // ========================================
  // SORT INTERACTION
  // ========================================

  function updateSortIndicators(sectionKey) {
    const table = document.querySelector(`#table-${sectionKey}`);
    if (!table) return;
    const ths = table.querySelectorAll('th[data-sort-key]');
    const st = sortState[sectionKey];
    ths.forEach(th => {
      const arrow = th.querySelector('.sort-arrow');
      if (!arrow) return;
      if (st && th.dataset.sortKey === st.key) {
        th.classList.add('sorted');
        arrow.textContent = st.dir === 'asc' ? '\u25B2' : '\u25BC';
        arrow.style.opacity = '1';
      } else {
        th.classList.remove('sorted');
        arrow.textContent = '\u25B4';
        arrow.style.opacity = '';
      }
    });
  }

  function handleSort(sectionKey, colKey) {
    const st = sortState[sectionKey];
    if (st && st.key === colKey) {
      // Toggle direction, or clear if already desc
      if (st.dir === 'asc') {
        sortState[sectionKey] = { key: colKey, dir: 'desc' };
      } else {
        // Clear sort (return to default order)
        delete sortState[sectionKey];
      }
    } else {
      // Default to descending for numbers, ascending for strings
      const col = COLUMNS.find(c => c.key === colKey);
      sortState[sectionKey] = { key: colKey, dir: col && col.type === 'string' ? 'asc' : 'desc' };
    }
    renderSection(sectionKey);
    updateSortIndicators(sectionKey);
  }

  // ========================================
  // DATA LOADING
  // ========================================

  function processRawData(rawData) {
    Object.keys(rawData).forEach(symbol => {
      const candles = parseChartData(rawData[symbol]);
      if (candles) {
        candleStore[symbol] = candles;
        const indicators = computeIndicators(candles);
        if (indicators) dataStore[symbol] = indicators;
      }
    });
  }

  async function loadCachedData() {
    const statusEl = document.getElementById('last-updated');
    if (statusEl) statusEl.textContent = 'Loading cached data...';
    try {
      const res = await fetch('./data.json');
      if (!res.ok) throw new Error('No cache');
      const raw = await res.json();
      processRawData(raw);
      lastUpdated = new Date();
      renderAll();
      updateTimestamp('cached');
      return true;
    } catch (e) {
      console.warn('No cached data, will fetch live');
      return false;
    }
  }

  async function fetchBatch(symbols) {
    try {
      const res = await fetch(`${CGI_BIN}/yahoo.py/batch`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ symbols, range: '1y', interval: '1d' })
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return await res.json();
    } catch (e) {
      console.error('Batch fetch error:', e);
      return {};
    }
  }

  async function fetchLiveData() {
    if (isLoading) return;
    isLoading = true;
    const btn = document.getElementById('btn-refresh');
    if (btn) btn.classList.add('loading');
    const statusEl = document.getElementById('last-updated');

    const batchSize = 4;
    let fetched = 0;

    for (let i = 0; i < ALL_SYMBOLS.length; i += batchSize) {
      const batch = ALL_SYMBOLS.slice(i, i + batchSize);
      if (statusEl) statusEl.textContent = `Refreshing ${Math.min(fetched + batchSize, ALL_SYMBOLS.length)}/${ALL_SYMBOLS.length}...`;
      try {
        const results = await fetchBatch(batch);
        batch.forEach(s => {
          if (results[s] && !results[s].error) {
            const candles = parseChartData(results[s]);
            if (candles) {
              candleStore[s] = candles;
              const ind = computeIndicators(candles);
              if (ind) dataStore[s] = ind;
            }
          }
        });
        fetched += batch.length;
        renderAll();
      } catch (e) {
        fetched += batch.length;
      }
      if (i + batchSize < ALL_SYMBOLS.length) await new Promise(r => setTimeout(r, 150));
    }

    lastUpdated = new Date();
    updateTimestamp('live');
    isLoading = false;
    if (btn) btn.classList.remove('loading');
  }

  function updateTimestamp(mode) {
    const el = document.getElementById('last-updated');
    if (!el || !lastUpdated) return;
    const hh = String(lastUpdated.getHours()).padStart(2, '0');
    const mm = String(lastUpdated.getMinutes()).padStart(2, '0');
    const ss = String(lastUpdated.getSeconds()).padStart(2, '0');
    const modeLabel = mode === 'cached' ? ' (cached)' : '';
    el.innerHTML = `<span class="status-dot ${mode === 'live' ? 'live' : 'stale'}"></span>Updated ${hh}:${mm}:${ss}${modeLabel}`;
  }

  // ========================================
  // TABLE BUILDER
  // ========================================

  function headerCellHTML(col, sectionKey) {
    if (!col.sortable) {
      return `<th>${col.label}</th>`;
    }
    return `<th data-sort-key="${col.key}" data-section="${sectionKey}" class="sortable-th" role="columnheader" aria-sort="none" tabindex="0">
      <span class="th-content">${col.label}<span class="sort-arrow">\u25B4</span></span>
    </th>`;
  }

  function tableHTML(key) {
    const sec = SECTIONS[key];
    const headerCells = COLUMNS.map(col => headerCellHTML(col, key)).join('');
    return `
      <div class="table-section" id="table-${key}">
        <div class="table-section-header">
          <span class="table-section-title">${sec.title}</span>
          <span class="table-section-count">${sec.items.length} items</span>
        </div>
        <div class="table-wrapper">
          <table class="data-table" role="table" aria-label="${sec.title}">
            <thead><tr>${headerCells}</tr></thead>
            <tbody id="tbody-${key}"></tbody>
          </table>
        </div>
      </div>`;
  }

  // ========================================
  // INIT
  // ========================================

  function bindSortListeners() {
    document.querySelectorAll('.sortable-th').forEach(th => {
      const handler = () => {
        const colKey = th.dataset.sortKey;
        const sectionKey = th.dataset.section;
        if (colKey && sectionKey) handleSort(sectionKey, colKey);
      };
      th.addEventListener('click', handler);
      th.addEventListener('keydown', e => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); handler(); }
      });
    });
  }

  async function init() {
    document.getElementById('left-column').innerHTML =
      tableHTML('alternatives') + tableHTML('global') + tableHTML('indices');
    document.getElementById('right-column').innerHTML = tableHTML('sectors');

    bindSortListeners();
    showSkeletons();

    document.getElementById('btn-refresh').addEventListener('click', () => fetchLiveData());

    // 1) Load cached data instantly
    const hasCached = await loadCachedData();

    // 2) Auto-refresh via CGI every 5 minutes
    setInterval(() => fetchLiveData(), 5 * 60 * 1000);

    // 3) If no cached data, fetch live immediately
    if (!hasCached) fetchLiveData();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

})();
