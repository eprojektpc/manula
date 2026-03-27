from __future__ import annotations

from datetime import datetime, timezone
from functools import wraps
import os
from pathlib import Path
import threading
import time
from typing import Any

from flask import Flask, jsonify, redirect, render_template, request, session, url_for

from config_manager import load_config, update_from_flat_payload
from order_bridge import buy_quote, get_price, sell_quantity
from screener import ScreenerError, build_chart_payload, fetch_symbols, fetch_tickers, run_scan
import storage

BASE_DIR = Path(__file__).resolve().parent

app = Flask(__name__, template_folder=str(BASE_DIR / 'templates'), static_folder=str(BASE_DIR / 'static'))
app.config['JSON_SORT_KEYS'] = False
app.secret_key = load_config()['app']['secret_key']

storage.init_db()

_SCAN_LOCK = threading.Lock()
_SELL_LOCK = threading.Lock()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def scanner_status() -> dict[str, Any]:
    return storage.get_state('screener_status', {
        'running': False,
        'enabled': True,
        'last_scan_at': None,
        'next_scan_at': None,
        'last_status': 'INIT',
        'last_error': None,
        'candidate_count': 0,
    })


def save_scanner_status(status: dict[str, Any]) -> None:
    storage.set_state('screener_status', status, utc_now_iso())


def is_authenticated() -> bool:
    return bool(session.get('logged_in'))


def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not is_authenticated():
            if request.path.startswith('/api/'):
                return jsonify({'ok': False, 'error': 'Brak autoryzacji'}), 401
            return redirect(url_for('login'))
        return fn(*args, **kwargs)

    return wrapped


class ScreenerWorker(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.stop_event = threading.Event()
        self.trigger_event = threading.Event()

    def run_once(self) -> None:
        cfg = load_config()
        status = scanner_status()
        status.update({'running': True, 'enabled': bool(cfg['scanner']['enabled']), 'last_error': None})
        save_scanner_status(status)
        scan_time = utc_now_iso()
        try:
            candidates = run_scan(cfg)
            run_status = 'OK' if candidates else 'EMPTY'
            storage.save_scan_results(scan_time, run_status, candidates, meta={'quote_asset': cfg['trading']['quote_asset']})
            status.update({
                'running': False,
                'enabled': bool(cfg['scanner']['enabled']),
                'last_scan_at': scan_time,
                'last_status': run_status,
                'candidate_count': len(candidates),
                'last_error': None,
            })
            save_scanner_status(status)
        except Exception as exc:
            err = str(exc)
            storage.save_scan_results(scan_time, 'ERROR', [], meta={'error': err})
            status.update({
                'running': False,
                'enabled': bool(cfg['scanner']['enabled']),
                'last_scan_at': scan_time,
                'last_status': 'ERROR',
                'last_error': err,
                'candidate_count': 0,
            })
            save_scanner_status(status)

    def run(self) -> None:
        while not self.stop_event.is_set():
            cfg = load_config()
            interval = max(15, int(cfg['scanner']['scan_interval_sec']))
            status = scanner_status()
            next_scan_ts = time.time() + interval
            status.update({
                'enabled': bool(cfg['scanner']['enabled']),
                'next_scan_at': datetime.fromtimestamp(next_scan_ts, tz=timezone.utc).isoformat(),
            })
            save_scanner_status(status)

            if bool(cfg['scanner']['enabled']):
                with _SCAN_LOCK:
                    self.run_once()
            self.trigger_event.wait(interval)
            self.trigger_event.clear()

    def trigger_now(self) -> None:
        self.trigger_event.set()


class PositionMonitor(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.stop_event = threading.Event()

    def run(self) -> None:
        while not self.stop_event.is_set():
            cfg = load_config()
            positions = storage.get_open_positions()
            for pos in positions:
                try:
                    price = float(get_price(pos['symbol']) or 0.0)
                    if price <= 0:
                        continue
                    entry_price = float(pos['entry_price'])
                    pnl_pct = ((price - entry_price) / entry_price) * 100.0
                    pnl_value = (price - entry_price) * float(pos['quantity'])
                    storage.update_position_metrics(int(pos['slot']), price, pnl_pct, pnl_value, utc_now_iso())

                    tp_price = float(pos.get('tp_price') or 0.0)
                    sl_price = float(pos.get('sl_price') or 0.0)
                    if tp_price > 0 and price >= tp_price:
                        execute_sell(slot=int(pos['slot']), reason='AUTO_TP')
                    elif sl_price > 0 and price <= sl_price:
                        execute_sell(slot=int(pos['slot']), reason='AUTO_SL')
                except Exception:
                    continue
            time.sleep(max(1, int(cfg['trading']['monitor_interval_sec'])))


def get_open_position_by_symbol(symbol: str) -> dict[str, Any] | None:
    symbol = str(symbol or '').upper().strip()
    for pos in storage.get_open_positions():
        if pos['symbol'] == symbol:
            return pos
    return None


def execute_buy(symbol: str, slot: int, budget: float | None = None, tp_pct: float | None = None, sl_pct: float | None = None) -> dict[str, Any]:
    cfg = load_config()
    symbol = str(symbol or '').upper().strip()
    if not symbol:
        raise ValueError('Brak symbolu do BUY.')
    if storage.get_open_position(slot):
        raise ValueError(f'Slot {slot} jest już zajęty.')

    budget_final = float(budget if budget is not None else cfg['trading']['default_budget'])
    tp_final = float(tp_pct if tp_pct is not None else cfg['trading']['tp_pct'])
    sl_final = float(sl_pct if sl_pct is not None else cfg['trading']['sl_pct'])

    chart = build_chart_payload(symbol, interval='1m', fuel_cfg=cfg.get('fuel', {}))
    order = buy_quote(symbol, budget_final)
    entry = float(order['avg_price'])
    tp_price = entry * (1 + tp_final / 100.0)
    sl_price = entry * (1 - sl_final / 100.0)

    now = utc_now_iso()
    storage.upsert_open_position(
        slot=slot,
        symbol=symbol,
        entry_price=entry,
        quantity=float(order['executed_qty']),
        budget=budget_final,
        tp_pct=tp_final,
        sl_pct=sl_final,
        tp_price=tp_price,
        sl_price=sl_price,
        order_id=order.get('order_id'),
        opened_at=now,
        fuel_score=float(chart.get('fuel', {}).get('score', 0.0)),
        pattern_info=str(chart.get('pattern', {}).get('name') or ''),
    )
    storage.record_trade(
        slot=slot,
        symbol=symbol,
        side='BUY',
        price=entry,
        quantity=float(order['executed_qty']),
        value=float(order['value']),
        pnl_pct=None,
        pnl_value=None,
        reason='MANUAL_BUY',
        order_id=order.get('order_id'),
        created_at=now,
    )
    return {'ok': True, 'order': order, 'position': storage.get_open_position(slot)}


def execute_sell(slot: int | None = None, symbol: str | None = None, reason: str = 'MANUAL_SELL') -> dict[str, Any]:
    with _SELL_LOCK:
        pos = storage.get_open_position(int(slot)) if slot is not None else get_open_position_by_symbol(symbol)
        if not pos:
            raise ValueError('Nie znaleziono otwartej pozycji do SELL.')

        order = sell_quantity(pos['symbol'], float(pos['quantity']))
        sell_price = float(order['avg_price'])
        qty = float(order['executed_qty'])
        entry_price = float(pos['entry_price'])
        pnl_pct = ((sell_price - entry_price) / entry_price) * 100.0 if entry_price else 0.0
        pnl_value = (sell_price - entry_price) * qty
        now = utc_now_iso()

        storage.close_position(int(pos['slot']), now, reason=reason)
        storage.record_trade(
            slot=int(pos['slot']),
            symbol=pos['symbol'],
            side='SELL',
            price=sell_price,
            quantity=qty,
            value=float(order['value']),
            pnl_pct=pnl_pct,
            pnl_value=pnl_value,
            reason=reason,
            order_id=order.get('order_id'),
            created_at=now,
        )
        return {'ok': True, 'order': order, 'pnl_pct': pnl_pct, 'pnl_value': pnl_value, 'closed_slot': pos['slot']}


def list_symbols() -> list[str]:
    cfg = load_config()
    quote_asset = str(cfg['trading']['quote_asset']).upper()
    try:
        allowed = set(fetch_symbols(quote_asset))
        tickers = fetch_tickers()
        ranked = [(str(row.get('symbol') or '').upper(), float(row.get('quoteVolume') or 0.0)) for row in tickers]
        symbols = [sym for sym, _ in sorted(ranked, key=lambda x: x[1], reverse=True) if sym in allowed]
    except Exception:
        symbols = []

    extras = [str(pos.get('symbol') or '').upper() for pos in storage.get_open_positions()]
    extras.extend(str(c.get('symbol') or '').upper() for c in storage.latest_scan_candidates(50))
    extras.append(default_symbol())

    out: list[str] = []
    for sym in extras + symbols:
        if sym and sym not in out:
            out.append(sym)
    return out


def default_symbol() -> str:
    positions = storage.get_open_positions()
    if positions:
        return positions[0]['symbol']
    candidates = storage.latest_scan_candidates(5)
    if candidates:
        return candidates[0]['symbol']
    cfg = load_config()
    return f'BTC{cfg["trading"]["quote_asset"]}'


def _asset_version(filename: str) -> int:
    try:
        path = os.path.join(app.static_folder or 'static', filename)
        return int(os.path.getmtime(path))
    except Exception:
        return int(time.time())


def _slots_payload() -> list[dict[str, Any]]:
    cfg = load_config()
    slot_count = int(cfg['trading']['slot_count'])
    open_map = {int(x['slot']): x for x in storage.get_open_positions()}
    slots: list[dict[str, Any]] = []
    for slot in range(1, slot_count + 1):
        pos = open_map.get(slot)
        if pos:
            pos = dict(pos)
            pos['status'] = 'OPEN'
            slots.append(pos)
        else:
            slots.append({'slot': slot, 'status': 'EMPTY'})
    return slots


@app.after_request
def add_no_cache_headers(response):
    if request.path in {'/', '/login'} or request.path.endswith('.html') or request.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store, no-cache, max-age=0, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    return response


@app.route('/login', methods=['GET', 'POST'])
def login():
    if is_authenticated():
        return redirect(url_for('index'))

    error = None
    if request.method == 'POST':
        cfg = load_config()
        u = str(request.form.get('username') or '').strip()
        p = str(request.form.get('password') or '').strip()
        if u == str(cfg.get('auth', {}).get('username', '')) and p == str(cfg.get('auth', {}).get('password', '')):
            session['logged_in'] = True
            session['username'] = u
            return redirect(url_for('index'))
        error = 'Niepoprawny login lub hasło.'
    return render_template('login.html', error=error)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/')
@login_required
def index():
    asset_version = max(_asset_version('app.js'), _asset_version('style.css'))
    return render_template('index.html', asset_version=asset_version, user=session.get('username', 'user'))


@app.route('/api/settings', methods=['GET', 'POST'])
@login_required
def api_settings():
    if request.method == 'GET':
        return jsonify(load_config())
    payload = request.get_json(force=True, silent=True) or {}
    cfg = update_from_flat_payload(payload)
    return jsonify({'ok': True, 'config': cfg})


@app.route('/api/state')
@login_required
def api_state():
    cfg = load_config()
    return jsonify({
        'config': cfg,
        'scanner_status': scanner_status(),
        'candidates': storage.latest_scan_candidates(10),
        'positions': storage.get_open_positions(),
        'slot_cards': _slots_payload(),
        'trades': storage.list_trades(50),
        'default_symbol': default_symbol(),
        'slots': list(range(1, int(cfg['trading']['slot_count']) + 1)),
    })


@app.route('/api/candles')
@login_required
def api_candles():
    symbol = request.args.get('symbol') or default_symbol()
    interval = (request.args.get('interval') or '1m').strip()
    cfg = load_config()
    payload = build_chart_payload(symbol, interval=interval, fuel_cfg=cfg.get('fuel', {}))
    markers = []
    for pos in storage.get_open_positions():
        if pos['symbol'] == symbol:
            try:
                ts = int(datetime.fromisoformat(pos['opened_at']).timestamp())
            except Exception:
                ts = int(time.time())
            markers.append({'time': ts, 'position': 'belowBar', 'color': '#22c55e', 'shape': 'arrowUp', 'text': f"ENTRY S{pos['slot']} @ {float(pos['entry_price']):.6f}"})
    payload['markers'] = markers
    candles = payload.get('candles') or []
    last_candle = candles[-1] if candles else {}
    payload['debug'] = {
        'last_update_time': datetime.now(timezone.utc).isoformat(),
        'server_time': int(time.time()),
        'last_candle_time': last_candle.get('time'),
        'last_candle_close': last_candle.get('close'),
        'status': 'ok',
        'poll_interval_sec': int(cfg.get('ui', {}).get('refresh_interval_sec', 1)),
    }
    return jsonify(payload)


@app.route('/api/price')
@login_required
def api_price():
    symbol = str(request.args.get('symbol') or default_symbol()).upper()
    return jsonify({'symbol': symbol, 'price': float(get_price(symbol) or 0.0), 'ts': time.time()})


@app.route('/api/symbols')
@login_required
def api_symbols():
    return jsonify({'symbols': list_symbols()})


@app.route('/api/symbols/all')
@login_required
def api_symbols_all():
    cfg = load_config()
    quote_asset = str(cfg['trading']['quote_asset']).upper()
    symbols = sorted({str(sym).upper() for sym in fetch_symbols(quote_asset)})
    return jsonify({'symbols': symbols, 'quote_asset': quote_asset, 'count': len(symbols)})


@app.route('/api/positions')
@login_required
def api_positions():
    return jsonify(storage.get_open_positions())


@app.route('/api/trades')
@login_required
def api_trades():
    limit = int(request.args.get('limit', 100))
    return jsonify(storage.list_trades(limit))


@app.route('/api/scans/current')
@login_required
def api_scans_current():
    return jsonify({'status': scanner_status(), 'candidates': storage.latest_scan_candidates(10)})


@app.route('/api/scans/history')
@login_required
def api_scans_history():
    limit = int(request.args.get('limit', 50))
    return jsonify(storage.scan_history(limit))


@app.route('/api/scan/run', methods=['POST'])
@login_required
def api_scan_run():
    SCREENING_WORKER.trigger_now()
    return jsonify({'ok': True, 'message': 'Scan został wyzwolony.'})


@app.route('/api/buy', methods=['POST'])
@login_required
def api_buy():
    data = request.get_json(force=True, silent=True) or {}
    result = execute_buy(
        symbol=str(data.get('symbol') or '').upper(),
        slot=int(data.get('slot')),
        budget=float(data['budget']) if data.get('budget') not in (None, '') else None,
        tp_pct=float(data['tp_pct']) if data.get('tp_pct') not in (None, '') else None,
        sl_pct=float(data['sl_pct']) if data.get('sl_pct') not in (None, '') else None,
    )
    return jsonify(result)


@app.route('/api/sell', methods=['POST'])
@login_required
def api_sell():
    data = request.get_json(force=True, silent=True) or {}
    slot_raw = data.get('slot')
    result = execute_sell(slot=int(slot_raw) if slot_raw not in (None, '') else None, symbol=data.get('symbol'), reason='MANUAL_SELL')
    return jsonify(result)


@app.errorhandler(Exception)
def handle_error(exc):
    code = 500
    if isinstance(exc, ValueError):
        code = 400
    elif isinstance(exc, ScreenerError):
        code = 500
    return jsonify({'ok': False, 'error': str(exc)}), code


SCREENING_WORKER = ScreenerWorker()
POSITION_MONITOR = PositionMonitor()
SCREENING_WORKER.start()
POSITION_MONITOR.start()


if __name__ == '__main__':
    cfg = load_config()
    app.run(host=cfg['app']['host'], port=int(cfg['app']['port']), debug=bool(cfg['app']['debug']), threaded=True)
