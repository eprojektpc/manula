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


def parse_optional_float(value: Any, field_name: str) -> float | None:
    if value in (None, ''):
        return None
    if isinstance(value, str):
        value = value.strip().replace(',', '.')
        if value == '':
            return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'Nieprawidłowa wartość pola "{field_name}": {value}') from exc


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


def _slot_settings_map() -> dict[int, dict[str, Any]]:
    cfg = load_config()
    rows = storage.get_slot_settings(int(cfg['trading']['slot_count']))
    out: dict[int, dict[str, Any]] = {}
    for row in rows:
        item = dict(row)
        item['auto_enabled'] = bool(item.get('auto_enabled'))
        out[int(item['slot'])] = item
    return out


def _preferred_and_reserved_symbols(slot: int) -> tuple[str | None, set[str]]:
    settings = _slot_settings_map()
    preferred = str(settings.get(slot, {}).get('symbol') or '').upper().strip() or None

    reserved: set[str] = set()
    for other_slot, row in settings.items():
        if int(other_slot) == int(slot):
            continue
        sym = str(row.get('symbol') or '').upper().strip()
        if sym:
            reserved.add(sym)

    for pos in storage.get_open_positions():
        pos_slot = int(pos.get('slot') or 0)
        if pos_slot == int(slot):
            continue
        sym = str(pos.get('symbol') or '').upper().strip()
        if sym:
            reserved.add(sym)
    return preferred, reserved


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
            slot_settings = _slot_settings_map()
            positions = storage.get_open_positions()
            for pos in positions:
                try:
                    slot = int(pos['slot'])
                    setting = slot_settings.get(slot, {})
                    if not bool(setting.get('auto_enabled')):
                        continue

                    price = float(get_price(pos['symbol']) or 0.0)
                    if price <= 0:
                        continue
                    entry_price = float(pos['entry_price'])
                    pnl_pct = ((price - entry_price) / entry_price) * 100.0
                    pnl_value = (price - entry_price) * float(pos['quantity'])
                    storage.update_position_metrics(slot, price, pnl_pct, pnl_value, utc_now_iso())

                    tp_pct = float(pos.get('tp_pct') or 0.0)
                    sl_pct = float(pos.get('sl_pct') or 0.0)
                    if tp_pct > 0 and pnl_pct >= tp_pct:
                        execute_sell(slot=slot, reason='AUTO_TP')
                    elif sl_pct > 0 and pnl_pct <= (-sl_pct):
                        execute_sell(slot=slot, reason='AUTO_SL')
                except Exception:
                    continue
            time.sleep(max(1, int(cfg['trading']['monitor_interval_sec'])))


def get_open_position_by_symbol(symbol: str) -> dict[str, Any] | None:
    symbol = str(symbol or '').upper().strip()
    for pos in storage.get_open_positions():
        if pos['symbol'] == symbol:
            return pos
    return None


def execute_buy(symbol: str, slot: int, budget: float | None = None, tp_pct: float | None = None, sl_pct: float | None = None, reason: str = 'MANUAL_BUY') -> dict[str, Any]:
    cfg = load_config()
    symbol = str(symbol or '').upper().strip()
    if not symbol:
        raise ValueError('Brak symbolu do BUY.')
    if storage.get_open_position(slot):
        raise ValueError(f'Slot {slot} jest już zajęty.')

    settings = _slot_settings_map().get(int(slot), {})
    budget_final = float(budget if budget is not None else settings.get('budget') or cfg['trading']['default_budget'])
    tp_final = float(tp_pct if tp_pct is not None else settings.get('tp_pct') or cfg['trading']['tp_pct'])
    sl_final = float(sl_pct if sl_pct is not None else settings.get('sl_pct') or cfg['trading']['sl_pct'])

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
    storage.upsert_slot_setting(slot=slot, updated_at=now, symbol=symbol, budget=budget_final, tp_pct=tp_final, sl_pct=sl_final)
    storage.record_trade(
        slot=slot,
        symbol=symbol,
        side='BUY',
        price=entry,
        quantity=float(order['executed_qty']),
        value=float(order['value']),
        pnl_pct=None,
        pnl_value=None,
        reason=reason,
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




def _markers_for_symbol(symbol: str) -> list[dict[str, Any]]:
    markers = []
    for pos in storage.get_open_positions():
        if pos['symbol'] == symbol:
            try:
                ts = int(datetime.fromisoformat(pos['opened_at']).timestamp())
            except Exception:
                ts = int(time.time())
            markers.append({
                'time': ts,
                'position': 'belowBar',
                'color': '#22c55e',
                'shape': 'arrowUp',
                'text': f"ENTRY S{pos['slot']} @ {float(pos['entry_price']):.6f}",
            })
    return markers


def _with_chart_debug(payload: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    candles = payload.get('candles') or []
    last_candle = candles[-1] if candles else {}
    payload['debug'] = {
        'last_update_time': datetime.now(timezone.utc).isoformat(),
        'server_time': int(time.time()),
        'last_candle_time': last_candle.get('time'),
        'last_candle_close': last_candle.get('close'),
        'status': 'ok',
        'poll_interval_sec': float(cfg.get('ui', {}).get('refresh_interval_sec', 1)),
    }
    payload['current_price'] = last_candle.get('close')
    return payload

def _slots_payload() -> list[dict[str, Any]]:
    cfg = load_config()
    slot_count = int(cfg['trading']['slot_count'])
    open_map = {int(x['slot']): x for x in storage.get_open_positions()}
    settings = _slot_settings_map()
    slots: list[dict[str, Any]] = []
    for slot in range(1, slot_count + 1):
        pos = open_map.get(slot)
        setting = settings.get(slot, {})
        realized = storage.slot_realized_pnl(slot)
        base = {
            'slot': slot,
            'symbol': setting.get('symbol') or (pos or {}).get('symbol') or default_symbol(),
            'auto_enabled': bool(setting.get('auto_enabled')),
            'config_budget': setting.get('budget'),
            'config_tp_pct': setting.get('tp_pct'),
            'config_sl_pct': setting.get('sl_pct'),
            'realized_pnl': realized,
        }
        if pos:
            item = dict(pos)
            item.update(base)
            item['status'] = 'OPEN'
            slots.append(item)
        else:
            base.update({'status': 'EMPTY', 'pnl_pct': 0.0, 'pnl_value': 0.0})
            slots.append(base)
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
    slot_scan_results: dict[str, Any] = {}
    for slot in range(1, int(cfg['trading']['slot_count']) + 1):
        slot_scan_results[str(slot)] = storage.get_slot_scan_result(slot)
    return jsonify({
        'config': cfg,
        'scanner_status': scanner_status(),
        'candidates': storage.latest_scan_candidates(10),
        'positions': storage.get_open_positions(),
        'slot_cards': _slots_payload(),
        'slot_scan_results': slot_scan_results,
        'trades': storage.list_trades(50),
        'default_symbol': default_symbol(),
        'slots': list(range(1, int(cfg['trading']['slot_count']) + 1)),
    })


@app.route('/api/slot/<int:slot>/config', methods=['POST'])
@login_required
def api_slot_config(slot: int):
    data = request.get_json(force=True, silent=True) or {}
    symbol = str(data.get('symbol') or '').upper().strip()
    auto_enabled = bool(data.get('auto_enabled')) if 'auto_enabled' in data else None
    budget = parse_optional_float(data.get('budget'), 'budget')
    tp_pct = parse_optional_float(data.get('tp_pct'), 'tp_pct')
    sl_pct = parse_optional_float(data.get('sl_pct'), 'sl_pct')
    now_iso = utc_now_iso()

    storage.upsert_slot_setting(
        slot=slot,
        symbol=symbol if symbol else None,
        auto_enabled=auto_enabled,
        budget=budget,
        tp_pct=tp_pct,
        sl_pct=sl_pct,
        updated_at=now_iso,
    )

    setting = _slot_settings_map().get(slot, {})
    if bool(setting.get('auto_enabled')):
        effective_tp = tp_pct if tp_pct is not None else setting.get('tp_pct')
        effective_sl = sl_pct if sl_pct is not None else setting.get('sl_pct')
        if effective_tp is not None and effective_sl is not None:
            storage.update_open_position_risk(slot=slot, tp_pct=float(effective_tp), sl_pct=float(effective_sl), updated_at=now_iso)
    return jsonify({'ok': True, 'slot': slot})



@app.route('/api/slot_chart')
@login_required
def api_slot_chart():
    cfg = load_config()
    slot_count = int(cfg['trading']['slot_count'])
    slot = int(request.args.get('slot_id', 0))
    if slot < 1 or slot > slot_count:
        raise ValueError(f'Nieprawidłowy slot. Użyj 1..{slot_count}.')

    interval = (request.args.get('interval') or '1m').strip()
    slot_map = {int(item['slot']): item for item in _slots_payload()}
    slot_item = slot_map.get(slot)

    requested_symbol = str(request.args.get('symbol') or '').upper().strip()
    symbol = requested_symbol or str((slot_item or {}).get('symbol') or default_symbol()).upper().strip()

    payload = build_chart_payload(symbol, interval=interval, fuel_cfg=cfg.get('fuel', {}))
    payload['slot'] = slot
    payload['symbol'] = symbol
    payload['markers'] = _markers_for_symbol(symbol)
    payload = _with_chart_debug(payload, cfg)
    return jsonify(payload)


@app.route('/chart-data')
@login_required
def chart_data():
    cfg = load_config()
    slot_count = int(cfg['trading']['slot_count'])
    slot = int(request.args.get('slot', 0))
    if slot < 1 or slot > slot_count:
        raise ValueError(f'Nieprawidłowy slot. Użyj 1..{slot_count}.')

    interval = (request.args.get('interval') or '1m').strip()
    slot_map = {int(item['slot']): item for item in _slots_payload()}
    slot_item = slot_map.get(slot)
    symbol = str((slot_item or {}).get('symbol') or default_symbol()).upper().strip()

    payload = build_chart_payload(symbol, interval=interval, fuel_cfg=cfg.get('fuel', {}))
    payload['slot'] = slot
    payload['symbol'] = symbol
    payload['markers'] = _markers_for_symbol(symbol)
    payload = _with_chart_debug(payload, cfg)
    return jsonify(payload)


@app.route('/api/candles')
@login_required
def api_candles():
    symbol = (request.args.get('symbol') or default_symbol()).upper()
    interval = (request.args.get('interval') or '1m').strip()
    cfg = load_config()
    payload = build_chart_payload(symbol, interval=interval, fuel_cfg=cfg.get('fuel', {}))
    payload['markers'] = _markers_for_symbol(symbol)
    payload = _with_chart_debug(payload, cfg)
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


@app.route('/api/slot/<int:slot>/scan', methods=['POST'])
@login_required
def api_slot_scan(slot: int):
    cfg = load_config()
    slot_count = int(cfg['trading']['slot_count'])
    if slot < 1 or slot > slot_count:
        raise ValueError(f'Nieprawidłowy slot. Użyj 1..{slot_count}.')

    with _SCAN_LOCK:
        candidates = run_scan(cfg)
        scan_time = utc_now_iso()
        run_status = 'OK' if candidates else 'EMPTY'
        storage.save_scan_results(
            scan_time,
            run_status,
            candidates,
            meta={'quote_asset': cfg['trading']['quote_asset'], 'source': f'slot_{slot}'},
        )

        if not candidates:
            storage.save_slot_scan_result(
                slot,
                {'status': 'EMPTY', 'symbol': None, 'scan_time': scan_time, 'message': 'Brak kandydatów dla slotu.'},
                scan_time,
            )
            return jsonify({'ok': False, 'slot': slot, 'message': 'Brak kandydatów.'}), 404

        preferred_symbol, reserved_symbols = _preferred_and_reserved_symbols(slot)
        candidate_rows: list[tuple[str, dict[str, Any]]] = []
        for row in candidates:
            sym = str(row.get('symbol') or '').upper().strip()
            if not sym:
                continue
            candidate_rows.append((sym, row))

        chosen: tuple[str, dict[str, Any]] | None = None
        if preferred_symbol:
            for sym, row in candidate_rows:
                if sym == preferred_symbol:
                    chosen = (sym, row)
                    break

        if chosen is None:
            for sym, row in candidate_rows:
                if sym in reserved_symbols:
                    continue
                chosen = (sym, row)
                break

        if chosen is None and candidate_rows:
            chosen = candidate_rows[0]

        if chosen is None:
            raise ValueError('Skan nie zwrócił poprawnego symbolu.')

        best_symbol, best = chosen
        if not best_symbol:
            raise ValueError('Skan nie zwrócił poprawnego symbolu.')

        storage.upsert_slot_setting(slot=slot, symbol=best_symbol, updated_at=scan_time)
        storage.save_slot_scan_result(
            slot,
            {'status': 'OK', 'symbol': best_symbol, 'scan_time': scan_time, 'candidate': best},
            scan_time,
        )

    return jsonify({'ok': True, 'slot': slot, 'symbol': best_symbol, 'candidate': best, 'scan_time': scan_time})


@app.route('/api/buy', methods=['POST'])
@login_required
def api_buy():
    data = request.get_json(force=True, silent=True) or {}
    slot = int(data.get('slot'))
    slot_cfg = _slot_settings_map().get(slot, {})
    symbol = str(data.get('symbol') or slot_cfg.get('symbol') or '').upper().strip()
    result = execute_buy(
        symbol=symbol,
        slot=slot,
        budget=parse_optional_float(data.get('budget'), 'budget'),
        tp_pct=parse_optional_float(data.get('tp_pct'), 'tp_pct'),
        sl_pct=parse_optional_float(data.get('sl_pct'), 'sl_pct'),
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
