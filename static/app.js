let state = null;
let symbolsCache = [];
let candleChart, rsiChart, candleSeries, ema9Series, ema21Series, ema50Series, rsiSeries, rsi30Series, rsi70Series;
const charts = {};
const series = {};
const slotChartPayloads = {};
const slotChartInFlight = {};
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
    Object.entries(charts).forEach(([slot, chart]) => {
      const container = document.querySelector(`[data-slot-chart="${slot}"]`);
      if (container) chart.applyOptions({ width: container.clientWidth });
    });
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

function slotCardHtml(slot) {
  const pnlClass = Number(slot.pnl_pct || 0) >= 0 ? 'green' : 'red';
  const isOpen = slot.status === 'OPEN';
  const symbolSelect = symbolsCache.map((s) => `<option value="${s}" ${s === slot.symbol ? 'selected' : ''}>${s}</option>`).join('');

  return `
    <h3>Slot ${slot.slot}</h3>
    <div class="slot-form-grid">
      <label>Symbol<select data-field="symbol">${symbolSelect}</select></label>
      <label>Budżet<input data-field="budget" type="number" step="0.01" value="${slot.config_budget ?? state?.config?.trading?.default_budget ?? 25}"></label>
      <label>TP %<input data-field="tp_pct" type="number" step="0.01" value="${slot.config_tp_pct ?? state?.config?.trading?.tp_pct ?? 0.7}"></label>
      <label>SL %<input data-field="sl_pct" type="number" step="0.01" value="${slot.config_sl_pct ?? state?.config?.trading?.sl_pct ?? 0.6}"></label>
      <label class="checkbox-inline"><input data-field="auto_enabled" type="checkbox" ${slot.auto_enabled ? 'checked' : ''}> Auto mode</label>
    </div>

    <div class="button-row slot-actions">
      <button class="primary" data-action="buy">BUY</button>
      <button class="danger" data-action="sell" ${isOpen ? '' : 'disabled'}>SELL</button>
      <button data-action="save">Zapisz slot</button>
      <button data-action="chart">Pokaż na wykresie</button>
    </div>

    <div class="slot-chart-wrap">
      <div class="slot-chart-header">Wykres slotu ${slot.slot} · <span data-slot-symbol>${slot.symbol || '-'}</span></div>
      <div class="slot-chart" data-slot-chart="${slot.slot}"></div>
    </div>

    <div class="kv">
      <div>Status</div><div>${slot.status}</div>
      <div>Pozycja</div><div>${isOpen ? `${slot.symbol} @ ${fmt(slot.entry_price, 6)}` : 'Brak'}</div>
      <div>Current</div><div>${isOpen ? fmt(slot.current_price, 6) : '-'}</div>
      <div>Qty</div><div>${isOpen ? fmt(slot.quantity, 6) : '-'}</div>
      <div>TP / SL</div><div>${isOpen ? `${fmt(slot.tp_price,6)} / ${fmt(slot.sl_price,6)}` : '-'}</div>
      <div>PNL live %</div><div class="${pnlClass}">${isOpen ? `${fmt(slot.pnl_pct, 2)}%` : '0.00%'}</div>
      <div>PNL live</div><div class="${pnlClass}">${fmt(slot.pnl_value || 0, 4)}</div>
      <div>PNL realized</div><div class="${Number(slot.realized_pnl || 0) >= 0 ? 'green' : 'red'}">${fmt(slot.realized_pnl || 0, 4)}</div>
    </div>
  `;
}

async function saveSlotConfig(slotId, card) {
  const payload = {
    symbol: card.querySelector('[data-field="symbol"]').value,
    budget: card.querySelector('[data-field="budget"]').value,
    tp_pct: card.querySelector('[data-field="tp_pct"]').value,
    sl_pct: card.querySelector('[data-field="sl_pct"]').value,
    auto_enabled: card.querySelector('[data-field="auto_enabled"]').checked,
  };
  await apiPost(`/api/slot/${slotId}/config`, payload);
}

function wireSlotCard(card, slot) {
  const slotId = slot.slot;
  const symbolSelect = card.querySelector('[data-field="symbol"]');
  symbolSelect.addEventListener('change', () => {
    const symbolLabel = card.querySelector('[data-slot-symbol]');
    if (symbolLabel) symbolLabel.textContent = symbolSelect.value || '-';
  });

  card.querySelector('[data-action="save"]').addEventListener('click', async () => {
    try {
      await saveSlotConfig(slotId, card);
      showFlash(`Slot ${slotId} zapisany.`);
      await loadState();
    } catch (e) {
      showFlash(e.message, true);
    }
  });

  card.querySelector('[data-action="buy"]').addEventListener('click', async () => {
    try {
      await saveSlotConfig(slotId, card);
      const symbol = card.querySelector('[data-field="symbol"]').value;
      await apiPost('/api/buy', {
        slot: slotId,
        symbol,
        budget: card.querySelector('[data-field="budget"]').value,
        tp_pct: card.querySelector('[data-field="tp_pct"]').value,
        sl_pct: card.querySelector('[data-field="sl_pct"]').value,
      });
      showFlash(`BUY wykonany na slot ${slotId}.`);
      await loadState();
      if (symbol) {
        currentSymbol = symbol;
        $('pairSelect').value = symbol;
        await refreshChart(true);
      }
    } catch (e) {
      showFlash(e.message, true);
    }
  });

  card.querySelector('[data-action="sell"]').addEventListener('click', async () => {
    try {
      await apiPost('/api/sell', { slot: slotId });
      showFlash(`SELL wykonany na slot ${slotId}.`);
      await loadState();
    } catch (e) {
      showFlash(e.message, true);
    }
  });

  card.querySelector('[data-action="chart"]').addEventListener('click', async () => {
    const symbol = card.querySelector('[data-field="symbol"]').value;
    if (!symbol) return;
    currentSymbol = symbol;
    $('pairSelect').value = symbol;
    await refreshChart(true);
  });
}

function destroySlotCharts() {
  Object.keys(charts).forEach((slot) => {
    try {
      charts[slot].remove();
    } catch (e) {
      // noop
    }
    delete charts[slot];
    delete series[slot];
    delete slotChartPayloads[slot];
    delete slotChartInFlight[slot];
  });
}

function initSlotChart(slot) {
  const container = document.querySelector(`[data-slot-chart="${slot}"]`);
  if (!container) return;
  const chart = LightweightCharts.createChart(container, {
    layout: { background: { color: '#0b0f14' }, textColor: '#9fb0c3' },
    grid: { vertLines: { color: '#1f2a36' }, horzLines: { color: '#1f2a36' } },
    width: container.clientWidth,
    height: 160,
    rightPriceScale: { borderColor: '#1f2a36' },
    timeScale: { borderColor: '#1f2a36', timeVisible: true, secondsVisible: false },
  });

  charts[slot] = chart;
  series[slot] = chart.addCandlestickSeries({ upColor: '#22c55e', downColor: '#ef4444', borderVisible: false, wickUpColor: '#22c55e', wickDownColor: '#ef4444' });
}

function updateSlotChart(slot, data, fit = false) {
  if (!series[slot]) return;
  const candles = data.candles || [];
  const prevCandles = slotChartPayloads[slot]?.candles || [];

  if (!prevCandles.length || candles.length < 3 || fit) {
    series[slot].setData(candles);
    if (fit && charts[slot]) charts[slot].timeScale().fitContent();
  } else {
    const nextLast = candles[candles.length - 1];
    if (nextLast) series[slot].update(nextLast);
  }

  slotChartPayloads[slot] = data;
}

async function refreshSlotChart(slot, fit = false) {
  if (!series[slot] || slotChartInFlight[slot]) return;
  slotChartInFlight[slot] = true;
  try {
    const data = await apiGet(`/chart-data?slot=${encodeURIComponent(slot)}&interval=${encodeURIComponent(currentInterval)}`);
    updateSlotChart(slot, data, fit);
  } catch (e) {
    // do not disrupt UI flash for background slot polling
  } finally {
    slotChartInFlight[slot] = false;
  }
}

function renderSlots(slots) {
  const wrap = $('positionsCards');
  destroySlotCharts();
  wrap.innerHTML = '';
  slots.forEach((slot) => {
    const card = document.createElement('div');
    card.className = 'card slot-card';
    card.innerHTML = slotCardHtml(slot);
    wrap.appendChild(card);
    wireSlotCard(card, slot);
    initSlotChart(slot.slot);
    refreshSlotChart(slot.slot, true);
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

  const prevCandles = lastChartPayload.candles || [];
  const nextCandles = nextData.candles || [];
  if (!nextCandles.length) return;

  const prevLast = prevCandles[prevCandles.length - 1];
  const nextLast = nextCandles[nextCandles.length - 1];
  if (!prevLast || !nextLast || nextCandles.length < 20) {
    setBaseChart(nextData, false);
    lastChartPayload = nextData;
    return;
  }

  candleSeries.update(nextLast);

  const updateLast = (ser, values) => {
    if (Array.isArray(values) && values.length) {
      ser.update(values[values.length - 1]);
    }
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
  if (chartPollingInFlight || !currentSymbol) return;
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

  await Promise.all(Object.keys(charts).map((slot) => refreshSlotChart(slot)));
}

function startChartPolling() {
  if (chartPollingTimer) clearInterval(chartPollingTimer);
  const intervalMs = Math.max(700, Number(state?.config?.ui?.refresh_interval_sec || 0.8) * 1000);
  chartPollingTimer = setInterval(refreshChartTick, intervalMs);
}

function startStateRefresh() {
  if (stateRefreshTimer) return;
  stateRefreshTimer = setInterval(loadState, 3000);
}

async function loadState() {
  state = await apiGet('/api/state');
  if (!symbolsCache.length) {
    const symbolsResponse = await apiGet('/api/symbols/all');
    symbolsCache = symbolsResponse.symbols || [];
    setSelectOptions($('pairSelect'), symbolsCache, currentSymbol || state.default_symbol);
  }
  currentSymbol = currentSymbol || $('pairSelect').value || state.default_symbol;

  $('scanStatus').textContent = `${state.scanner_status.last_status || '-'}${state.scanner_status.running ? ' · running' : ''}`;
  $('lastScan').textContent = state.scanner_status.last_scan_at || '-';
  $('nextScan').textContent = state.scanner_status.next_scan_at || '-';

  renderCandidates(state.candidates || []);
  renderSlots(state.slot_cards || []);
  renderTrades(state.trades || []);
}

document.addEventListener('DOMContentLoaded', async () => {
  initCharts();
  $('scanBtn').addEventListener('click', async () => {
    try {
      await apiPost('/api/scan/run', {});
      showFlash('Ręczny scan uruchomiony.');
    } catch (e) {
      showFlash(e.message, true);
    }
  });
  $('refreshBtn').addEventListener('click', async () => await refreshChart(true));

  $('pairSelect').addEventListener('change', async () => {
    currentSymbol = $('pairSelect').value;
    await refreshChart(true);
  });
  $('intervalSelect').addEventListener('change', async () => {
    currentInterval = $('intervalSelect').value || '1m';
    await refreshChart(true);
    await Promise.all(Object.keys(charts).map((slot) => refreshSlotChart(slot, true)));
  });

  await loadState();
  await refreshChart(true);
  startChartPolling();
  startStateRefresh();
});
