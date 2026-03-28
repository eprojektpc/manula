let state = null;
let symbolsCache = [];
const slotCards = {};
const slotChartState = {};
let slotChartsResizeObserver = null;
let stateRefreshTimer = null;
let chartPollingTimer = null;
let loadStateRequestSeq = 0;

const INTERVALS = ['1m', '3m', '5m', '15m', '1h'];
const $ = (id) => document.getElementById(id);

function showFlash(msg, isError = false) {
  const el = $('flash');
  if (!el) return;
  el.textContent = msg || '';
  el.className = 'flash ' + (isError ? 'red' : 'green');
}

function showSlotFlash(slotId, msg, isError = false) {
  const card = slotCards[slotId];
  if (!card) return;
  const el = card.querySelector('[data-role="slotFlash"]');
  if (!el) return;
  el.textContent = msg || '';
  el.className = 'slot-flash ' + (isError ? 'red' : 'green');
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

function fmt(num, digits = 4) {
  if (num === null || num === undefined || Number.isNaN(Number(num))) return '-';
  return Number(num).toFixed(digits);
}

function setInputValueIfIdle(input, value) {
  if (!input) return;
  if (document.activeElement === input) return;
  input.value = value ?? '';
}

function setText(card, role, value) {
  const el = card.querySelector(`[data-role="${role}"]`);
  if (el) el.textContent = value == null || value === '' ? '-' : String(value);
}

function slotCardHtml(slot) {
  const symbolSelect = symbolsCache.map((s) => `<option value="${s}" ${s === slot.symbol ? 'selected' : ''}>${s}</option>`).join('');
  const intervalOptions = INTERVALS.map((i) => `<option value="${i}" ${i === '1m' ? 'selected' : ''}>${i}</option>`).join('');

  return `
    <h3>Slot ${slot.slot}</h3>
    <div class="slot-form-grid">
      <label>Symbol<select data-field="symbol">${symbolSelect}</select></label>
      <label>Interwał<select data-field="interval">${intervalOptions}</select></label>
      <label>Budżet<input data-field="budget" type="number" step="0.01"></label>
      <label>TP %<input data-field="tp_pct" type="number" step="0.01"></label>
      <label>SL %<input data-field="sl_pct" type="number" step="0.01"></label>
      <label class="checkbox-inline"><input data-field="auto_enabled" type="checkbox"> Auto mode</label>
    </div>

    <div class="button-row slot-actions utility-actions">
      <button data-action="save">Zapisz slot</button>
      <button data-action="scan">Skanuj slot ${slot.slot}</button>
      <button data-action="refresh-chart">Odśwież wykres</button>
    </div>
    <div class="slot-flash" data-role="slotFlash"></div>

    <div class="slot-indicators" data-role="slot-indicators">
      <div class="pill">RSI: <span data-role="rsiValue">-</span></div>
      <div class="pill">Fuel: <span data-role="fuelIcons"></span> <span data-role="fuelText">-</span></div>
      <div class="pill">Pattern: <span data-role="patternAlert">-</span></div>
      <div class="pill">Price: <span data-role="currentPrice">-</span></div>
      <div class="pill">PNL live %: <span data-role="pnlPct">0.00%</span></div>
      <div class="pill">PNL live: <span data-role="pnlLive">0.0000</span></div>
    </div>

    <div class="slot-chart-wrap">
      <div class="button-row slot-actions trade-actions chart-trade-actions">
        <button class="primary" data-action="buy">BUY</button>
        <button class="danger" data-action="sell">SELL</button>
      </div>
      <div class="slot-chart-header">Wykres slotu ${slot.slot} · <span data-role="slotSymbol">${slot.symbol || '-'}</span> · <span data-role="slotInterval">1m</span></div>
      <div class="slot-chart-main" data-slot-candle="${slot.slot}"></div>
      <div class="slot-chart-rsi" data-slot-rsi="${slot.slot}"></div>
      <div class="chart-debug-box slot-debug-box">
        <div><strong>Debug</strong></div>
        <div>last update time: <span data-role="debugLastUpdate">-</span></div>
        <div>server time: <span data-role="debugServerTime">-</span></div>
        <div>last candle time: <span data-role="debugLastCandleTime">-</span></div>
        <div>status połączenia: <span data-role="debugConnection">-</span></div>
      </div>
    </div>

    <div class="kv slot-meta">
      <div>Status</div><div data-role="status">-</div>
      <div>Pozycja</div><div data-role="position">-</div>
      <div>Current</div><div data-role="current">-</div>
      <div>Qty</div><div data-role="qty">-</div>
      <div>TP / SL</div><div data-role="tpSl">-</div>
      <div>PNL realized</div><div data-role="pnlRealized">-</div>
      <div>Ostatni scan</div><div data-role="slotLastScan">-</div>
      <div>Wynik scan</div><div data-role="slotScanSymbol">-</div>
    </div>
  `;
}

function initSlotChart(slotId) {
  if (slotChartState[slotId]) return;

  const candleEl = document.querySelector(`[data-slot-candle="${slotId}"]`);
  const rsiEl = document.querySelector(`[data-slot-rsi="${slotId}"]`);
  if (!candleEl || !rsiEl) return;

  const common = {
    layout: { background: { color: '#0b0f14' }, textColor: '#9fb0c3' },
    grid: { vertLines: { color: '#1f2a36' }, horzLines: { color: '#1f2a36' } },
    rightPriceScale: { borderColor: '#1f2a36' },
    timeScale: { borderColor: '#1f2a36', timeVisible: true, secondsVisible: false },
  };

  const candleChart = LightweightCharts.createChart(candleEl, {
    ...common,
    width: candleEl.clientWidth,
    height: candleEl.clientHeight,
    handleScroll: { pressedMouseMove: true, vertTouchDrag: true, horzTouchDrag: true, mouseWheel: true },
    handleScale: { axisPressedMouseMove: true, pinch: true, mouseWheel: true },
  });

  const rsiChart = LightweightCharts.createChart(rsiEl, {
    ...common,
    width: rsiEl.clientWidth,
    height: rsiEl.clientHeight,
    handleScroll: { pressedMouseMove: true, vertTouchDrag: true, horzTouchDrag: true, mouseWheel: true },
    handleScale: { axisPressedMouseMove: true, pinch: true, mouseWheel: true },
  });

  const candles = candleChart.addCandlestickSeries({ upColor: '#22c55e', downColor: '#ef4444', borderVisible: false, wickUpColor: '#22c55e', wickDownColor: '#ef4444' });
  const ema7 = candleChart.addLineSeries({ color: '#22c55e', lineWidth: 2 });
  const ema25 = candleChart.addLineSeries({ color: '#38bdf8', lineWidth: 2 });
  const ema99 = candleChart.addLineSeries({ color: '#fbbf24', lineWidth: 2 });

  const rsi = rsiChart.addLineSeries({ color: '#a855f7', lineWidth: 2 });
  const rsi30 = rsiChart.addLineSeries({ color: '#22c55e', lineWidth: 1, lineStyle: 2 });
  const rsi70 = rsiChart.addLineSeries({ color: '#ef4444', lineWidth: 1, lineStyle: 2 });

  slotChartState[slotId] = {
    slotId,
    candleEl,
    rsiEl,
    candleChart,
    rsiChart,
    series: { candles, ema7, ema25, ema99, rsi, rsi30, rsi70 },
    lastPayload: null,
    inFlight: false,
    priceLine: null,
  };
}

function applySlotChartData(slotId, payload, fit = false) {
  const chartState = slotChartState[slotId];
  if (!chartState) return;

  const { candles, ema7, ema25, ema99, rsi, rsi30, rsi70 } = chartState.series;
  const nextCandles = payload.candles || [];
  const nextRsi = payload.rsi || [];
  const prev = chartState.lastPayload;

  const mustReset = !prev || prev.symbol !== payload.symbol || prev.interval !== payload.interval || fit;

  if (mustReset || nextCandles.length < 3) {
    candles.setData(nextCandles);
    ema7.setData(payload.ema7 || []);
    ema25.setData(payload.ema25 || []);
    ema99.setData(payload.ema99 || []);
    rsi.setData(nextRsi);
    rsi30.setData(nextRsi.map((x) => ({ time: x.time, value: 30 })));
    rsi70.setData(nextRsi.map((x) => ({ time: x.time, value: 70 })));
    candles.setMarkers(payload.markers || []);
    if (fit) {
      chartState.candleChart.timeScale().fitContent();
      chartState.rsiChart.timeScale().fitContent();
    }
  } else {
    const updateLast = (ser, values) => {
      if (Array.isArray(values) && values.length) {
        ser.update(values[values.length - 1]);
      }
    };

    updateLast(candles, nextCandles);
    updateLast(ema7, payload.ema7);
    updateLast(ema25, payload.ema25);
    updateLast(ema99, payload.ema99);
    updateLast(rsi, nextRsi);

    if (nextRsi.length) {
      const t = nextRsi[nextRsi.length - 1].time;
      rsi30.update({ time: t, value: 30 });
      rsi70.update({ time: t, value: 70 });
    }

    candles.setMarkers(payload.markers || []);
  }

  if (chartState.priceLine) candles.removePriceLine(chartState.priceLine);
  if (payload.current_price) {
    chartState.priceLine = candles.createPriceLine({
      price: Number(payload.current_price),
      color: '#f97316',
      lineWidth: 2,
      lineStyle: 2,
      axisLabelVisible: true,
      title: 'Current',
    });
  }

  chartState.lastPayload = payload;
}

function updateSlotPanel(slotData) {
  const card = slotCards[slotData.slot];
  if (!card) return;

  const hasPosition = slotData.status === 'OPEN';
  const pnlPct = Number(slotData.pnl_pct || 0);
  const pnlValue = Number(slotData.pnl_value || 0);
  const pnlClass = pnlPct >= 0 ? 'green' : 'red';
  const realizedClass = Number(slotData.realized_pnl || 0) >= 0 ? 'green' : 'red';

  const sellBtn = card.querySelector('[data-action="sell"]');
  if (sellBtn) sellBtn.disabled = !hasPosition;

  setInputValueIfIdle(card.querySelector('[data-field="budget"]'), slotData.config_budget ?? state?.config?.trading?.default_budget ?? 25);
  setInputValueIfIdle(card.querySelector('[data-field="tp_pct"]'), slotData.config_tp_pct ?? state?.config?.trading?.tp_pct ?? 0.7);
  setInputValueIfIdle(card.querySelector('[data-field="sl_pct"]'), slotData.config_sl_pct ?? state?.config?.trading?.sl_pct ?? 0.6);
  const autoInput = card.querySelector('[data-field="auto_enabled"]');
  if (autoInput && document.activeElement !== autoInput) autoInput.checked = !!slotData.auto_enabled;

  const symbolInput = card.querySelector('[data-field="symbol"]');
  if (symbolInput && document.activeElement !== symbolInput) symbolInput.value = slotData.symbol || symbolInput.value;

  setText(card, 'slotSymbol', slotData.symbol || '-');
  setText(card, 'status', slotData.status || '-');
  setText(card, 'position', hasPosition ? `${slotData.symbol} @ ${fmt(slotData.entry_price, 6)}` : 'Brak pozycji');
  setText(card, 'current', hasPosition ? fmt(slotData.current_price, 6) : '-');
  setText(card, 'qty', hasPosition ? fmt(slotData.quantity, 6) : '-');
  setText(card, 'tpSl', hasPosition ? `${fmt(slotData.tp_price, 6)} / ${fmt(slotData.sl_price, 6)}` : '-');

  const pnlPctEl = card.querySelector('[data-role="pnlPct"]');
  pnlPctEl.textContent = hasPosition ? `${fmt(pnlPct, 2)}%` : '0.00%';
  pnlPctEl.className = pnlClass;

  const pnlLiveEl = card.querySelector('[data-role="pnlLive"]');
  pnlLiveEl.textContent = hasPosition ? fmt(pnlValue, 4) : '0.0000';
  pnlLiveEl.className = pnlClass;

  const pnlRealizedEl = card.querySelector('[data-role="pnlRealized"]');
  pnlRealizedEl.textContent = fmt(slotData.realized_pnl || 0, 4);
  pnlRealizedEl.className = realizedClass;

  const slotScan = state?.slot_scan_results?.[String(slotData.slot)] || null;
  setText(card, 'slotLastScan', slotScan?.scan_time || '-');
  setText(card, 'slotScanSymbol', slotScan?.symbol || '-');
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

  card.querySelector('[data-action="save"]').addEventListener('click', async () => {
    try {
      await saveSlotConfig(slotId, card);
      showSlotFlash(slotId, `Slot ${slotId} zapisany.`);
      await loadState();
      await refreshSlotChart(slotId, true);
    } catch (e) {
      showSlotFlash(slotId, e.message, true);
    }
  });

  card.querySelector('[data-action="buy"]').addEventListener('click', async () => {
    try {
      await saveSlotConfig(slotId, card);
      await apiPost('/api/buy', {
        slot: slotId,
        symbol: card.querySelector('[data-field="symbol"]').value,
        budget: card.querySelector('[data-field="budget"]').value,
        tp_pct: card.querySelector('[data-field="tp_pct"]').value,
        sl_pct: card.querySelector('[data-field="sl_pct"]').value,
      });
      showSlotFlash(slotId, `BUY wykonany na slot ${slotId}.`);
      await loadState();
      await refreshSlotChart(slotId, true);
    } catch (e) {
      showSlotFlash(slotId, e.message, true);
    }
  });

  card.querySelector('[data-action="sell"]').addEventListener('click', async () => {
    try {
      await apiPost('/api/sell', { slot: slotId });
      showSlotFlash(slotId, `SELL wykonany na slot ${slotId}.`);
      await loadState();
      await refreshSlotChart(slotId, true);
    } catch (e) {
      showSlotFlash(slotId, e.message, true);
    }
  });

  card.querySelector('[data-action="scan"]').addEventListener('click', async () => {
    try {
      const result = await apiPost(`/api/slot/${slotId}/scan`, {});
      const symbolInput = card.querySelector('[data-field="symbol"]');
      symbolInput.value = result.symbol;
      await loadState();
      await refreshSlotChart(slotId, true, true);
      showSlotFlash(slotId, `Skan slotu ${slotId} zakończony. Wybrano ${result.symbol}.`);
    } catch (e) {
      showSlotFlash(slotId, e.message, true);
    }
  });

  card.querySelector('[data-action="refresh-chart"]').addEventListener('click', async () => {
    await refreshSlotChart(slotId, true);
  });

  card.querySelector('[data-field="symbol"]').addEventListener('change', async () => {
    setText(card, 'slotSymbol', card.querySelector('[data-field="symbol"]').value || '-');
    await refreshSlotChart(slotId, true, true);
  });

  card.querySelector('[data-field="interval"]').addEventListener('change', async () => {
    setText(card, 'slotInterval', card.querySelector('[data-field="interval"]').value || '1m');
    await refreshSlotChart(slotId, true, true);
  });

  card.querySelector('[data-field="auto_enabled"]').addEventListener('change', async () => {
    try {
      await saveSlotConfig(slotId, card);
      showSlotFlash(slotId, `Auto mode dla slotu ${slotId} zapisany (TP/SL bez zmian).`);
      await loadState();
    } catch (e) {
      showSlotFlash(slotId, e.message, true);
    }
  });
}

function ensureSlotCard(slotData) {
  if (slotCards[slotData.slot]) return slotCards[slotData.slot];

  const wrap = $('positionsCards');
  const card = document.createElement('div');
  card.className = 'card slot-card';
  card.dataset.slot = String(slotData.slot);
  card.innerHTML = slotCardHtml(slotData);
  wrap.appendChild(card);

  slotCards[slotData.slot] = card;
  wireSlotCard(card, slotData);
  initSlotChart(slotData.slot);
  observeSlotChartResize(slotData.slot);
  return card;
}

function cleanupRemovedSlots(slots) {
  const active = new Set(slots.map((s) => String(s.slot)));
  Object.keys(slotCards).forEach((slotId) => {
    if (active.has(slotId)) return;
    const card = slotCards[slotId];
    if (card) card.remove();
    delete slotCards[slotId];

    const chartState = slotChartState[slotId];
    if (chartState) {
      chartState.candleChart.remove();
      chartState.rsiChart.remove();
      delete slotChartState[slotId];
    }
  });
}

async function refreshSlotChart(slotId, fit = false, force = false) {
  const card = slotCards[slotId];
  const chartState = slotChartState[slotId];
  if (!card || !chartState) return;
  if (chartState.inFlight) return;

  const symbol = card.querySelector('[data-field="symbol"]').value;
  const interval = card.querySelector('[data-field="interval"]').value || '1m';
  if (!symbol) return;

  chartState.inFlight = true;
  try {
    const data = await apiGet(`/api/slot_chart?slot_id=${encodeURIComponent(slotId)}&symbol=${encodeURIComponent(symbol)}&interval=${encodeURIComponent(interval)}`);

    setText(card, 'slotSymbol', data.symbol || symbol);
    setText(card, 'slotInterval', data.interval || interval);
    setText(card, 'rsiValue', fmt(data.rsi_value, 2));
    setText(card, 'fuelIcons', data.fuel?.icons || '');
    setText(card, 'fuelText', data.fuel?.text || '-');
    setText(card, 'patternAlert', data.pattern?.name ? `${data.pattern.name} · ${data.pattern.message}` : 'Brak wzorca');
    setText(card, 'currentPrice', fmt(data.current_price, 6));

    setText(card, 'debugLastUpdate', data.debug?.last_update_time);
    setText(card, 'debugServerTime', data.debug?.server_time);
    setText(card, 'debugLastCandleTime', data.debug?.last_candle_time);
    setText(card, 'debugConnection', 'connected');

    applySlotChartData(slotId, data, fit || force);
  } catch (e) {
    setText(card, 'debugConnection', `error: ${e.message}`);
  } finally {
    chartState.inFlight = false;
  }
}

async function refreshAllSlotCharts() {
  const ids = Object.keys(slotCards);
  await Promise.all(ids.map((slotId) => refreshSlotChart(slotId)));
}

function renderCandidates(candidates) {
  const body = $('candidatesBody');
  body.innerHTML = '';
  candidates.forEach((row, idx) => {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${idx + 1}</td><td>${row.symbol}</td><td>${fmt(row.score, 2)}</td><td>${fmt(row.rsi, 1)}</td>`;
    body.appendChild(tr);
  });
}

function renderTrades(trades) {
  const body = $('tradesBody');
  body.innerHTML = '';
  trades.forEach((row) => {
    const pnlClass = Number(row.pnl_pct || 0) >= 0 ? 'green' : 'red';
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${row.created_at || '-'}</td><td>${row.slot ?? '-'}</td><td>${row.symbol}</td><td>${row.side}</td><td>${fmt(row.price, 6)}</td><td>${fmt(row.quantity, 6)}</td><td class="${pnlClass}">${row.pnl_pct == null ? '-' : fmt(row.pnl_pct, 2)}</td><td>${row.reason || '-'}</td>`;
    body.appendChild(tr);
  });
}

function syncSlots(slots) {
  slots.forEach((slotData) => {
    ensureSlotCard(slotData);
    updateSlotPanel(slotData);
  });
  cleanupRemovedSlots(slots);
}

function startChartPolling() {
  if (chartPollingTimer) clearInterval(chartPollingTimer);
  const intervalMs = Math.max(700, Number(state?.config?.ui?.refresh_interval_sec || 0.8) * 1000);
  chartPollingTimer = setInterval(refreshAllSlotCharts, intervalMs);
}

function startStateRefresh() {
  if (stateRefreshTimer) return;
  stateRefreshTimer = setInterval(loadState, 3000);
}

function resizeAllCharts() {
  Object.values(slotChartState).forEach((chartState) => {
    if (!chartState.candleEl.isConnected || !chartState.rsiEl.isConnected) return;
    const candleWidth = chartState.candleEl.clientWidth;
    const candleHeight = chartState.candleEl.clientHeight;
    const rsiWidth = chartState.rsiEl.clientWidth;
    const rsiHeight = chartState.rsiEl.clientHeight;
    if (!candleWidth || !candleHeight || !rsiWidth || !rsiHeight) return;

    chartState.candleChart.applyOptions({ width: chartState.candleEl.clientWidth, height: chartState.candleEl.clientHeight });
    chartState.rsiChart.applyOptions({ width: chartState.rsiEl.clientWidth, height: chartState.rsiEl.clientHeight });
  });
}

function observeSlotChartResize(slotId) {
  const chartState = slotChartState[slotId];
  if (!chartState) return;
  if (!slotChartsResizeObserver) {
    slotChartsResizeObserver = new ResizeObserver(() => {
      resizeAllCharts();
    });
  }
  slotChartsResizeObserver.observe(chartState.candleEl);
  slotChartsResizeObserver.observe(chartState.rsiEl);
}

async function loadState() {
  const requestSeq = ++loadStateRequestSeq;
  const nextState = await apiGet('/api/state');
  let nextSymbols = symbolsCache;
  if (!nextSymbols.length) {
    const symbolsResponse = await apiGet('/api/symbols/all');
    nextSymbols = symbolsResponse.symbols || [];
  }

  if (requestSeq !== loadStateRequestSeq) return;

  state = nextState;
  symbolsCache = nextSymbols;

  $('scanStatus').textContent = `${state.scanner_status.last_status || '-'}${state.scanner_status.running ? ' · running' : ''}`;
  $('lastScan').textContent = state.scanner_status.last_scan_at || '-';
  $('nextScan').textContent = state.scanner_status.next_scan_at || '-';

  renderCandidates(state.candidates || []);
  syncSlots(state.slot_cards || []);
  renderTrades(state.trades || []);
}

document.addEventListener('DOMContentLoaded', async () => {
  window.addEventListener('resize', resizeAllCharts);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
      resizeAllCharts();
      refreshAllSlotCharts();
    }
  });

  await loadState();
  await refreshAllSlotCharts();
  requestAnimationFrame(() => resizeAllCharts());
  setTimeout(() => resizeAllCharts(), 250);
  startChartPolling();
  startStateRefresh();
  showFlash('Panel gotowy. Skanowanie dostępne per slot.');
});
