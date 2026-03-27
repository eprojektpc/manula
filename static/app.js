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
}

async function apiGet(url) {
  const sep = url.includes('?') ? '&' : '?';
  const res = await fetch(`${url}${sep}_=${Date.now()}`, { cache: 'no-store' });
  if (res.status === 401) {
    window.location.href = '/login';
    throw new Error('Brak sesji');
  }
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || 'Błąd API');
  return data;
}

async function apiPost(url, payload = {}) {
  const res = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
  if (res.status === 401) {
    window.location.href = '/login';
    throw new Error('Brak sesji');
  }
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || 'Błąd API');
  return data;
}

function setDebugValue(id, value) {
  const el = $(id);
  if (el) el.textContent = value == null || value === '' ? '-' : String(value);
}

function fmt(num, digits = 4) {
  if (num === null || num === undefined || Number.isNaN(Number(num))) return '-';
  return Number(num).toFixed(digits);
}

function setSelectOptions(select, items, selectedValue) {
  const current = selectedValue || select.value;
  select.innerHTML = '';
  items.forEach((item) => {
    const opt = document.createElement('option');
    opt.value = typeof item === 'object' ? item.value : item;
    opt.textContent = typeof item === 'object' ? item.label : item;
    if (String(opt.value) === String(current)) opt.selected = true;
    select.appendChild(opt);
  });
}

function initCharts() {
  candleChart = LightweightCharts.createChart($('candleChart'), {
    layout: { background: { color: '#0b0f14' }, textColor: '#e6edf3' },
    grid: { vertLines: { color: '#1f2a36' }, horzLines: { color: '#1f2a36' } },
    width: $('candleChart').clientWidth,
    height: 340,
  });
  candleSeries = candleChart.addCandlestickSeries({ upColor: '#22c55e', downColor: '#ef4444', borderVisible: false, wickUpColor: '#22c55e', wickDownColor: '#ef4444' });
  ema9Series = candleChart.addLineSeries({ color: '#22c55e', lineWidth: 2 });
  ema21Series = candleChart.addLineSeries({ color: '#38bdf8', lineWidth: 2 });
  ema50Series = candleChart.addLineSeries({ color: '#fbbf24', lineWidth: 2 });

  rsiChart = LightweightCharts.createChart($('rsiChart'), {
    layout: { background: { color: '#0b0f14' }, textColor: '#e6edf3' },
    grid: { vertLines: { color: '#1f2a36' }, horzLines: { color: '#1f2a36' } },
    width: $('rsiChart').clientWidth,
    height: 160,
  });
  rsiSeries = rsiChart.addLineSeries({ color: '#a855f7', lineWidth: 2 });
  rsi30Series = rsiChart.addLineSeries({ color: '#22c55e', lineWidth: 1, lineStyle: 2 });
  rsi70Series = rsiChart.addLineSeries({ color: '#ef4444', lineWidth: 1, lineStyle: 2 });

  window.addEventListener('resize', () => {
    candleChart.applyOptions({ width: $('candleChart').clientWidth });
    rsiChart.applyOptions({ width: $('rsiChart').clientWidth });
  });
}

function renderCandidates(candidates) {
  const body = $('candidatesBody');
  body.innerHTML = '';
  candidates.forEach((row, idx) => {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${idx + 1}</td><td><button data-symbol="${row.symbol}" class="linklike">${row.symbol}</button></td><td>${fmt(row.score,2)}</td><td>${fmt(row.rsi,1)}</td>`;
    body.appendChild(tr);
  });
  body.querySelectorAll('button[data-symbol]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      currentSymbol = btn.dataset.symbol;
      $('pairSelect').value = currentSymbol;
      await refreshChart(true);
    });
  });
}

function renderSlots(slots) {
  const wrap = $('positionsCards');
  wrap.innerHTML = '';
  slots.forEach((pos) => {
    const card = document.createElement('div');
    card.className = 'card slot-card';
    if (pos.status !== 'OPEN') {
      card.innerHTML = `<h3>Slot ${pos.slot}</h3><div class="muted">Pusty</div>`;
      wrap.appendChild(card);
      return;
    }
    const pnlClass = Number(pos.pnl_pct || 0) >= 0 ? 'green' : 'red';
    card.innerHTML = `
      <h3>Slot ${pos.slot} · ${pos.symbol}</h3>
      <div class="kv">
        <div>Entry</div><div>${fmt(pos.entry_price, 6)}</div>
        <div>Current</div><div>${fmt(pos.current_price, 6)}</div>
        <div>Qty</div><div>${fmt(pos.quantity, 6)}</div>
        <div>TP / SL</div><div>${fmt(pos.tp_price,6)} / ${fmt(pos.sl_price,6)}</div>
        <div>Status</div><div>${pos.status}</div>
        <div>PnL %</div><div class="${pnlClass}">${fmt(pos.pnl_pct, 2)}%</div>
        <div>PnL value</div><div class="${pnlClass}">${fmt(pos.pnl_value, 4)}</div>
      </div>`;
    wrap.appendChild(card);
  });
}

function renderTrades(trades) {
  const body = $('tradesBody');
  body.innerHTML = '';
  trades.forEach((row) => {
    const pnlClass = Number(row.pnl_pct || 0) >= 0 ? 'green' : 'red';
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${row.created_at || '-'}</td><td>${row.slot ?? '-'}</td><td>${row.symbol}</td><td>${row.side}</td><td>${fmt(row.price,6)}</td><td>${fmt(row.quantity,6)}</td><td class="${pnlClass}">${row.pnl_pct == null ? '-' : fmt(row.pnl_pct,2)}</td><td>${row.reason || '-'}</td>`;
    body.appendChild(tr);
  });
}

function updateIndicators(data) {
  const rsiVal = Number(data.rsi_value || 0);
  $('rsiValue').textContent = fmt(rsiVal, 2);
  $('rsiValue').className = rsiVal < 30 ? 'green' : (rsiVal > 70 ? 'red' : '');
  $('fuelIcons').textContent = data.fuel?.icons || '';
  $('fuelText').textContent = data.fuel?.text || '-';
  const pattern = data.pattern?.name ? `${data.pattern.name} · ${data.pattern.message}` : 'Brak wzorca';
  $('patternAlert').textContent = pattern;
}

function setBaseChart(data, fit = false) {
  candleSeries.setData(data.candles || []);
  ema9Series.setData(data.ema9 || []);
  ema21Series.setData(data.ema21 || []);
  ema50Series.setData(data.ema50 || []);
  rsiSeries.setData(data.rsi || []);
  rsi30Series.setData((data.rsi || []).map((x) => ({ time: x.time, value: 30 })));
  rsi70Series.setData((data.rsi || []).map((x) => ({ time: x.time, value: 70 })));
  candleSeries.setMarkers(data.markers || []);
  if (fit) {
    candleChart.timeScale().fitContent();
    rsiChart.timeScale().fitContent();
  }
}

function applyIncremental(nextData) {
  if (!lastChartPayload) {
    setBaseChart(nextData, true);
    lastChartPayload = nextData;
    return;
  }
  const nextCandles = nextData.candles || [];
  if (!nextCandles.length) return;
  const lastCandle = nextCandles[nextCandles.length - 1];
  candleSeries.update(lastCandle);

  const updateLast = (series, values) => {
    if (Array.isArray(values) && values.length) series.update(values[values.length - 1]);
  };
  updateLast(ema9Series, nextData.ema9);
  updateLast(ema21Series, nextData.ema21);
  updateLast(ema50Series, nextData.ema50);
  updateLast(rsiSeries, nextData.rsi);

  const rsiLine = nextData.rsi || [];
  if (rsiLine.length) {
    const t = rsiLine[rsiLine.length - 1].time;
    rsi30Series.update({ time: t, value: 30 });
    rsi70Series.update({ time: t, value: 70 });
  }
  candleSeries.setMarkers(nextData.markers || []);
  lastChartPayload = nextData;
}

async function refreshChart(fit = false) {
  const symbol = currentSymbol || $('pairSelect').value;
  if (!symbol) return;
  currentSymbol = symbol;
  currentInterval = $('intervalSelect').value || '1m';
  $('chartTitle').textContent = `Wykres · ${symbol} · ${currentInterval}`;
  const data = await apiGet(`/api/candles?symbol=${encodeURIComponent(symbol)}&interval=${encodeURIComponent(currentInterval)}`);
  setBaseChart(data, fit);
  updateIndicators(data);
  setDebugValue('debugLastUpdate', data.debug?.last_update_time);
  setDebugValue('debugServerTime', data.debug?.server_time);
  setDebugValue('debugLastCandleTime', data.debug?.last_candle_time);
  setDebugValue('debugConnection', 'connected');
  lastChartPayload = data;
}

async function refreshChartTick() {
  if (chartPollingInFlight) return;
  chartPollingInFlight = true;
  try {
    const data = await apiGet(`/api/candles?symbol=${encodeURIComponent(currentSymbol)}&interval=${encodeURIComponent(currentInterval)}`);
    applyIncremental(data);
    updateIndicators(data);
    setDebugValue('debugLastUpdate', data.debug?.last_update_time);
    setDebugValue('debugServerTime', data.debug?.server_time);
    setDebugValue('debugLastCandleTime', data.debug?.last_candle_time);
    setDebugValue('debugConnection', 'connected');
  } catch (e) {
    setDebugValue('debugConnection', `error: ${e.message}`);
  } finally {
    chartPollingInFlight = false;
  }
}

function startChartPolling() {
  if (chartPollingTimer) return;
  const intervalMs = Math.max(1000, Number(state?.config?.ui?.refresh_interval_sec || 1) * 1000);
  chartPollingTimer = setInterval(refreshChartTick, intervalMs);
}

function startStateRefresh() {
  if (stateRefreshTimer) return;
  stateRefreshTimer = setInterval(loadState, 5000);
}

async function loadState() {
  state = await apiGet('/api/state');
  setSelectOptions($('slotSelect'), state.slots, $('slotSelect').value || 1);
  setSelectOptions($('pairSelect'), (await apiGet('/api/symbols/all')).symbols || [], currentSymbol || state.default_symbol);
  currentSymbol = $('pairSelect').value || state.default_symbol;

  $('scanStatus').textContent = `${state.scanner_status.last_status || '-'}${state.scanner_status.running ? ' · running' : ''}`;
  $('lastScan').textContent = state.scanner_status.last_scan_at || '-';
  $('nextScan').textContent = state.scanner_status.next_scan_at || '-';

  renderCandidates(state.candidates || []);
  renderSlots(state.slot_cards || []);
  renderTrades(state.trades || []);
}

async function handleBuy() {
  try {
    await apiPost('/api/buy', { symbol: $('pairSelect').value, slot: $('slotSelect').value, budget: $('budgetInput').value, tp_pct: $('tpInput').value, sl_pct: $('slInput').value });
    showFlash('BUY wykonany');
    await loadState();
  } catch (e) {
    showFlash(e.message, true);
  }
}

async function handleSell() {
  try {
    await apiPost('/api/sell', { slot: $('slotSelect').value, symbol: $('pairSelect').value });
    showFlash('SELL wykonany');
    await loadState();
  } catch (e) {
    showFlash(e.message, true);
  }
}

document.addEventListener('DOMContentLoaded', async () => {
  initCharts();
  $('buyBtn').addEventListener('click', handleBuy);
  $('sellBtn').addEventListener('click', handleSell);
  $('scanBtn').addEventListener('click', async () => await apiPost('/api/scan/run', {}));
  $('refreshBtn').addEventListener('click', async () => await refreshChart(true));

  $('pairSelect').addEventListener('change', async () => {
    currentSymbol = $('pairSelect').value;
    await refreshChart(true);
  });
  $('intervalSelect').addEventListener('change', async () => {
    currentInterval = $('intervalSelect').value || '1m';
    await refreshChart(true);
  });

  await loadState();
  await refreshChart(true);
  startChartPolling();
  startStateRefresh();
});
