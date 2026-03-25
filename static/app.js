let state = null;
let candleChart, rsiChart, candleSeries, ema9Series, ema21Series, ema50Series, rsiSeries, rsi30Series, rsi70Series;
let currentSymbol = null;
let currentInterval = '1m';
let lastChartPayload = null;
let chartPollingTimer = null;
let stateRefreshTimer = null;
let chartPollingInFlight = false;

const $ = (id) => document.getElementById(id);

function showFlash(msg, isError = false) {
  const el = $('flash');
  el.textContent = msg || '';
  el.className = 'flash ' + (isError ? 'red' : 'green');
  if (msg) {
    setTimeout(() => { if (el.textContent === msg) el.textContent = ''; }, 4500);
  }
}

async function apiGet(url) {
  const sep = url.includes('?') ? '&' : '?';
  const bust = `_=${Date.now()}`;
  const finalUrl = `${url}${sep}${bust}`;
  const res = await fetch(finalUrl, { cache: 'no-store' });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || 'Błąd API');
  return data;
}

async function apiPost(url, payload = {}) {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || 'Błąd API');
  return data;
}

function initCharts() {
  candleChart = LightweightCharts.createChart($('candleChart'), {
    layout: { background: { color: '#020617' }, textColor: '#cbd5e1' },
    grid: { vertLines: { color: '#0f172a' }, horzLines: { color: '#0f172a' } },
    rightPriceScale: { borderColor: '#1f2937' },
    timeScale: { borderColor: '#1f2937', timeVisible: true, secondsVisible: false },
    width: $('candleChart').clientWidth,
    height: 420,
  });
  candleSeries = candleChart.addCandlestickSeries({
    upColor: '#22c55e', downColor: '#ef4444', borderVisible: false,
    wickUpColor: '#22c55e', wickDownColor: '#ef4444'
  });
  ema9Series = candleChart.addLineSeries({ color: '#22c55e', lineWidth: 2 });
  ema21Series = candleChart.addLineSeries({ color: '#38bdf8', lineWidth: 2 });
  ema50Series = candleChart.addLineSeries({ color: '#fbbf24', lineWidth: 2 });

  rsiChart = LightweightCharts.createChart($('rsiChart'), {
    layout: { background: { color: '#020617' }, textColor: '#cbd5e1' },
    grid: { vertLines: { color: '#0f172a' }, horzLines: { color: '#0f172a' } },
    rightPriceScale: { borderColor: '#1f2937', scaleMargins: { top: 0.2, bottom: 0.2 } },
    timeScale: { borderColor: '#1f2937', timeVisible: true, secondsVisible: false },
    width: $('rsiChart').clientWidth,
    height: 180,
  });
  rsiSeries = rsiChart.addLineSeries({ color: '#a855f7', lineWidth: 2 });
  rsi30Series = rsiChart.addLineSeries({ color: '#64748b', lineWidth: 1, lineStyle: 2 });
  rsi70Series = rsiChart.addLineSeries({ color: '#64748b', lineWidth: 1, lineStyle: 2 });

  candleChart.timeScale().subscribeVisibleTimeRangeChange((range) => {
    if (range) rsiChart.timeScale().setVisibleRange(range);
  });

  window.addEventListener('resize', () => {
    candleChart.applyOptions({ width: $('candleChart').clientWidth });
    rsiChart.applyOptions({ width: $('rsiChart').clientWidth });
  });
}

function setSelectOptions(select, items, selectedValue) {
  const current = selectedValue || select.value;
  select.innerHTML = '';
  items.forEach((item) => {
    const opt = document.createElement('option');
    if (typeof item === 'object') {
      opt.value = item.value;
      opt.textContent = item.label;
    } else {
      opt.value = item;
      opt.textContent = item;
    }
    if (String(opt.value) === String(current)) opt.selected = true;
    select.appendChild(opt);
  });
}

function fmt(num, digits = 4) {
  if (num === null || num === undefined || Number.isNaN(Number(num))) return '-';
  return Number(num).toFixed(digits);
}

function fillConfig(cfg) {
  $('cfg_quote_asset').value = cfg.trading.quote_asset;
  $('cfg_scan_interval_sec').value = cfg.scanner.scan_interval_sec;
  $('cfg_top_pairs').value = cfg.scanner.top_pairs;
  $('cfg_top_volume_limit').value = cfg.scanner.top_volume_limit;
  $('cfg_min_quote_volume').value = cfg.scanner.min_quote_volume;
  $('cfg_lookback_high_bars').value = cfg.scanner.lookback_high_bars;
  $('cfg_lookback_low_bars').value = cfg.scanner.lookback_low_bars;
  $('cfg_min_distance_to_breakout_pct').value = cfg.scanner.min_distance_to_breakout_pct;
  $('cfg_max_distance_to_breakout_pct').value = cfg.scanner.max_distance_to_breakout_pct;
  $('cfg_min_vol_ratio').value = cfg.scanner.min_vol_ratio;
  $('cfg_rsi_min').value = cfg.scanner.rsi_min;
  $('cfg_rsi_max').value = cfg.scanner.rsi_max;
  $('cfg_ema_spread_min_pct').value = cfg.scanner.ema_spread_min_pct;
  $('cfg_atr_pct_min').value = cfg.scanner.atr_pct_min;
  $('cfg_atr_pct_max').value = cfg.scanner.atr_pct_max;
  $('cfg_min_range_position').value = cfg.scanner.min_range_position;
  $('cfg_max_range_position').value = cfg.scanner.max_range_position;
  $('cfg_max_change_1m_pct').value = cfg.scanner.max_change_1m_pct;
  $('cfg_max_change_3m_pct').value = cfg.scanner.max_change_3m_pct;
  $('cfg_min_macd_hist').value = cfg.scanner.min_macd_hist;
  $('cfg_workers').value = cfg.scanner.workers;
  $('cfg_default_budget').value = cfg.trading.default_budget;
  $('cfg_slot_count').value = cfg.trading.slot_count;
  $('cfg_tp_pct').value = cfg.trading.tp_pct;
  $('cfg_sl_pct').value = cfg.trading.sl_pct;
  $('cfg_monitor_interval_sec').value = cfg.trading.monitor_interval_sec;
  $('cfg_enabled').value = cfg.scanner.enabled ? 'true' : 'false';
  $('cfg_blacklist').value = (cfg.scanner.blacklist || []).join(', ');
  $('budgetInput').value = cfg.trading.default_budget;
  $('tpInput').value = cfg.trading.tp_pct;
  $('slInput').value = cfg.trading.sl_pct;
}

function collectConfigPayload() {
  return {
    'trading.quote_asset': $('cfg_quote_asset').value,
    'scanner.scan_interval_sec': $('cfg_scan_interval_sec').value,
    'scanner.top_pairs': $('cfg_top_pairs').value,
    'scanner.top_volume_limit': $('cfg_top_volume_limit').value,
    'scanner.min_quote_volume': $('cfg_min_quote_volume').value,
    'scanner.lookback_high_bars': $('cfg_lookback_high_bars').value,
    'scanner.lookback_low_bars': $('cfg_lookback_low_bars').value,
    'scanner.min_distance_to_breakout_pct': $('cfg_min_distance_to_breakout_pct').value,
    'scanner.max_distance_to_breakout_pct': $('cfg_max_distance_to_breakout_pct').value,
    'scanner.min_vol_ratio': $('cfg_min_vol_ratio').value,
    'scanner.rsi_min': $('cfg_rsi_min').value,
    'scanner.rsi_max': $('cfg_rsi_max').value,
    'scanner.ema_spread_min_pct': $('cfg_ema_spread_min_pct').value,
    'scanner.atr_pct_min': $('cfg_atr_pct_min').value,
    'scanner.atr_pct_max': $('cfg_atr_pct_max').value,
    'scanner.min_range_position': $('cfg_min_range_position').value,
    'scanner.max_range_position': $('cfg_max_range_position').value,
    'scanner.max_change_1m_pct': $('cfg_max_change_1m_pct').value,
    'scanner.max_change_3m_pct': $('cfg_max_change_3m_pct').value,
    'scanner.min_macd_hist': $('cfg_min_macd_hist').value,
    'scanner.workers': $('cfg_workers').value,
    'scanner.blacklist': $('cfg_blacklist').value,
    'scanner.enabled': $('cfg_enabled').value,
    'trading.default_budget': $('cfg_default_budget').value,
    'trading.slot_count': $('cfg_slot_count').value,
    'trading.tp_pct': $('cfg_tp_pct').value,
    'trading.sl_pct': $('cfg_sl_pct').value,
    'trading.monitor_interval_sec': $('cfg_monitor_interval_sec').value,
  };
}

function renderCandidates(candidates) {
  const body = $('candidatesBody');
  body.innerHTML = '';
  candidates.forEach((row, idx) => {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${idx + 1}</td>
      <td><button data-symbol="${row.symbol}" class="linklike">${row.symbol}</button></td>
      <td>${fmt(row.score, 2)}</td>
      <td>${fmt(row.breakout_gap_pct, 2)}</td>
      <td>${fmt(row.rsi, 1)}</td>
      <td>x${fmt(row.vol_ratio, 2)}</td>
    `;
    body.appendChild(tr);
  });
  body.querySelectorAll('button[data-symbol]').forEach((btn) => {
    btn.addEventListener('click', () => {
      currentSymbol = btn.dataset.symbol;
      $('pairSelect').value = currentSymbol;
      refreshChart();
    });
  });
}

function renderPositions(positions) {
  const wrap = $('positionsCards');
  wrap.innerHTML = '';
  if (!positions.length) {
    wrap.innerHTML = '<div class="card">Brak otwartych pozycji.</div>';
    return;
  }
  positions.forEach((pos) => {
    const pnlClass = Number(pos.pnl_pct || 0) >= 0 ? 'green' : 'red';
    const card = document.createElement('div');
    card.className = 'card';
    card.innerHTML = `
      <h3>Slot ${pos.slot} · ${pos.symbol}</h3>
      <div class="kv">
        <div>Entry</div><div>${fmt(pos.entry_price, 6)}</div>
        <div>Current</div><div>${fmt(pos.current_price, 6)}</div>
        <div>Qty</div><div>${fmt(pos.quantity, 6)}</div>
        <div>TP / SL</div><div>${fmt(pos.tp_pct, 2)}% / ${fmt(pos.sl_pct, 2)}%</div>
        <div>PnL %</div><div class="${pnlClass}">${fmt(pos.pnl_pct, 2)}%</div>
        <div>PnL value</div><div class="${pnlClass}">${fmt(pos.pnl_value, 4)}</div>
        <div>Licznik</div><div class="${pnlClass}">${Number(pos.pnl_value || 0) >= 0 ? 'zysk' : 'strata'} live</div>
      </div>
    `;
    wrap.appendChild(card);
  });
}

function renderTrades(trades) {
  const body = $('tradesBody');
  body.innerHTML = '';
  trades.forEach((row) => {
    const pnlClass = Number(row.pnl_pct || 0) >= 0 ? 'green' : 'red';
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${row.created_at || '-'}</td>
      <td>${row.slot ?? '-'}</td>
      <td>${row.symbol}</td>
      <td>${row.side}</td>
      <td>${fmt(row.price, 6)}</td>
      <td>${fmt(row.quantity, 6)}</td>
      <td class="${pnlClass}">${row.pnl_pct == null ? '-' : fmt(row.pnl_pct, 2)}</td>
      <td class="${pnlClass}">${row.pnl_value == null ? '-' : fmt(row.pnl_value, 4)}</td>
      <td>${row.reason || '-'}</td>
    `;
    body.appendChild(tr);
  });
}

function renderScanHistory(rows) {
  const body = $('scanHistoryBody');
  body.innerHTML = '';
  rows.forEach((row) => {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${row.scan_time}</td>
      <td>${row.rank_idx}</td>
      <td>${row.symbol}</td>
      <td>${fmt(row.score, 2)}</td>
      <td>${fmt(row.breakout_gap_pct, 2)}</td>
      <td>${fmt(row.rsi, 1)}</td>
      <td>x${fmt(row.vol_ratio, 2)}</td>
      <td>${fmt(row.change_3m_pct, 2)}</td>
      <td>${fmt(row.atr_pct, 2)}</td>
      <td>${row.trend}</td>
    `;
    body.appendChild(tr);
  });
}

function renderChartPayload(data, { fit = false } = {}) {
  lastChartPayload = data;
  candleSeries.setData(data.candles || []);
  ema9Series.setData(data.ema9 || []);
  ema21Series.setData(data.ema21 || []);
  ema50Series.setData(data.ema50 || []);
  candleSeries.setMarkers(data.markers || []);
  rsiSeries.setData(data.rsi || []);
  rsi30Series.setData((data.rsi || []).map((x) => ({ time: x.time, value: 30 })));
  rsi70Series.setData((data.rsi || []).map((x) => ({ time: x.time, value: 70 })));
  if (fit) {
    candleChart.timeScale().fitContent();
    rsiChart.timeScale().fitContent();
  }
}

function updateLineRealtime(series, prevLine, nextLine, label) {
  if (!Array.isArray(nextLine) || !nextLine.length) return prevLine || [];
  if (!Array.isArray(prevLine) || !prevLine.length) {
    series.setData(nextLine);
    return [...nextLine];
  }
  const prevLast = prevLine[prevLine.length - 1];
  const prevLastTs = Number(prevLast.time);
  const incremental = nextLine.filter((item) => Number(item.time) >= prevLastTs);
  incremental.forEach((item) => series.update(item));
  const nextLast = nextLine[nextLine.length - 1];
  console.debug(`[chart] ${label} updated`, {
    points: incremental.length,
    prevLast: prevLastTs,
    nextLast: Number(nextLast.time),
  });
  return [...nextLine];
}

function applyRealtimeChartUpdate(nextData) {
  if (!nextData || !Array.isArray(nextData.candles) || !nextData.candles.length) return;
  if (!lastChartPayload || !Array.isArray(lastChartPayload.candles) || !lastChartPayload.candles.length) {
    renderChartPayload(nextData, { fit: false });
    return;
  }

  const prevCandles = lastChartPayload.candles;
  const nextCandles = nextData.candles;
  const prevLastTs = Number(prevCandles[prevCandles.length - 1].time);
  const incrementalCandles = nextCandles.filter((row) => Number(row.time) >= prevLastTs);
  incrementalCandles.forEach((row) => candleSeries.update(row));

  console.debug('[chart] realtime payload', {
    receivedCandles: nextCandles.length,
    appliedCandles: incrementalCandles.length,
    prevLastCandleTs: prevLastTs,
    nextLastCandleTs: Number(nextCandles[nextCandles.length - 1].time),
  });

  lastChartPayload = {
    ...lastChartPayload,
    ...nextData,
    candles: [...nextCandles],
    ema9: updateLineRealtime(ema9Series, lastChartPayload.ema9, nextData.ema9, 'ema9'),
    ema21: updateLineRealtime(ema21Series, lastChartPayload.ema21, nextData.ema21, 'ema21'),
    ema50: updateLineRealtime(ema50Series, lastChartPayload.ema50, nextData.ema50, 'ema50'),
    rsi: updateLineRealtime(rsiSeries, lastChartPayload.rsi, nextData.rsi, 'rsi'),
    markers: nextData.markers || [],
  };
  candleSeries.setMarkers(lastChartPayload.markers);
  const rsi30 = (lastChartPayload.rsi || []).map((x) => ({ time: x.time, value: 30 }));
  const rsi70 = (lastChartPayload.rsi || []).map((x) => ({ time: x.time, value: 70 }));
  rsi30Series.setData(rsi30);
  rsi70Series.setData(rsi70);
  console.debug('[chart] series.update() applied');
}

async function refreshChart(fit = false) {
  const symbol = currentSymbol || $('pairSelect').value;
  if (!symbol) return;
  currentSymbol = symbol;
  const interval = $('intervalSelect') ? $('intervalSelect').value : currentInterval;
  currentInterval = interval || '1m';
  $('chartTitle').textContent = `Wykres PRO · ${symbol} · ${interval}`;
  const data = await apiGet(`/api/candles?symbol=${encodeURIComponent(symbol)}&interval=${encodeURIComponent(interval)}`);
  renderChartPayload(data, { fit });
}

async function refreshChartRealtime() {
  const symbol = currentSymbol || $('pairSelect').value;
  if (!symbol || chartPollingInFlight) return;
  const interval = $('intervalSelect') ? $('intervalSelect').value : currentInterval;
  chartPollingInFlight = true;
  try {
    const data = await apiGet(`/api/candles?symbol=${encodeURIComponent(symbol)}&interval=${encodeURIComponent(interval)}`);
    applyRealtimeChartUpdate(data);
  } catch (err) {
    console.error('[chart] polling error', err);
  } finally {
    chartPollingInFlight = false;
  }
}

async function refreshAll({ fit = false } = {}) {
  await loadState();
  await refreshChart(fit);
}

async function loadSymbols(selected) {
  try {
    const resp = await apiGet('/api/symbols/all');
    const symbols = Array.isArray(resp.symbols) ? resp.symbols : [];
    if (symbols.length) {
      setSelectOptions($('pairSelect'), symbols, selected || currentSymbol || $('pairSelect').value);
      if (!currentSymbol) currentSymbol = $('pairSelect').value;
    }
  } catch (err) {
    console.error(err);
  }
}

async function loadState() {
  state = await apiGet('/api/state');
  fillConfig(state.config);
  setSelectOptions($('slotSelect'), state.slots, $('slotSelect').value || 1);

  await loadSymbols(currentSymbol || state.default_symbol);
  currentSymbol = $('pairSelect').value || state.default_symbol;

  $('scanStatus').textContent = `${state.scanner_status.last_status || '-'}${state.scanner_status.running ? ' · running' : ''}`;
  $('lastScan').textContent = state.scanner_status.last_scan_at || '-';
  $('nextScan').textContent = state.scanner_status.next_scan_at || '-';

  renderCandidates(state.candidates || []);
  renderPositions(state.positions || []);
  renderTrades(state.trades || []);

  const history = await apiGet('/api/scans/history?limit=50');
  renderScanHistory(history);
}

function startStateRefresh() {
  if (stateRefreshTimer) clearInterval(stateRefreshTimer);
  stateRefreshTimer = setInterval(async () => {
    try {
      await loadState();
    } catch (err) {
      console.error(err);
    }
  }, 5000);
}

function stopChartPolling() {
  if (chartPollingTimer) {
    clearInterval(chartPollingTimer);
    chartPollingTimer = null;
    console.debug('[chart] polling stopped');
  }
}

function startChartPolling() {
  stopChartPolling();
  console.debug('[chart] polling started (1000ms)');
  chartPollingTimer = setInterval(async () => {
    await refreshChartRealtime();
  }, 1000);
}

async function handleBuy() {
  try {
    const payload = {
      symbol: $('pairSelect').value,
      slot: $('slotSelect').value,
      budget: $('budgetInput').value,
      tp_pct: $('tpInput').value,
      sl_pct: $('slInput').value,
    };
    await apiPost('/api/buy', payload);
    showFlash('BUY wykonany.');
    await loadState();
    await refreshChart();
  } catch (err) {
    showFlash(err.message, true);
  }
}

async function handleSell() {
  try {
    await apiPost('/api/sell', { slot: $('slotSelect').value, symbol: $('pairSelect').value });
    showFlash('SELL wykonany.');
    await loadState();
    await refreshChart();
  } catch (err) {
    showFlash(err.message, true);
  }
}

async function handleSaveSettings() {
  try {
    await apiPost('/api/settings', collectConfigPayload());
    showFlash('Ustawienia zapisane.');
    await loadState();
  } catch (err) {
    showFlash(err.message, true);
  }
}

async function handleRunScan() {
  try {
    await apiPost('/api/scan/run', {});
    showFlash('Ręczny scan wyzwolony.');
    setTimeout(async () => { await refreshAll(); }, 1200);
  } catch (err) {
    showFlash(err.message, true);
  }
}

document.addEventListener('DOMContentLoaded', async () => {
  initCharts();
  $('buyBtn').addEventListener('click', handleBuy);
  $('sellBtn').addEventListener('click', handleSell);
  $('saveSettingsBtn').addEventListener('click', handleSaveSettings);
  $('scanBtn').addEventListener('click', handleRunScan);
  $('refreshBtn').addEventListener('click', async () => { await refreshAll({ fit: true }); });
  $('pairSelect').addEventListener('change', async () => {
    currentSymbol = $('pairSelect').value;
    await refreshChart(true);
    startChartPolling();
  });
  if ($('intervalSelect')) $('intervalSelect').addEventListener('change', async () => {
    currentInterval = $('intervalSelect').value || '1m';
    await refreshChart(true);
    startChartPolling();
  });

  await refreshAll({ fit: true });

  startChartPolling();
  startStateRefresh();
});
