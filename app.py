from __future__ import annotations

from datetime import datetime, timezone
from functools import wraps
import os
from pathlib import Path
import sqlite3
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
_COMBO_DB_PATH = '/root/screener-bot/screener.db'
_COMBO_CACHE_TTL_SEC = 30.0
_COMBO_CACHE_LOCK = threading.Lock()
_COMBO_CACHE: dict[str, Any] = {'expires_at': 0.0, 'table': None, 'column_map': None}


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


def _rsi_bucket(rsi_value: float) -> str:
    if rsi_value < 30:
        return 'lt30'
    if rsi_value < 40:
        return '30_39'
    if rsi_value < 50:
        return '40_49'
    if rsi_value < 60:
        return '50_59'
    if rsi_value < 70:
        return '60_69'
    return 'ge70'


def _fuel_bucket(fuel_score: float) -> str:
    if fuel_score >= 2.3:
        return 'high'
    if fuel_score >= 1.0:
        return 'mid'
    return 'low'


def _build_combo_key(payload: dict[str, Any]) -> str:
    pattern_name = str((payload.get('pattern') or {}).get('name') or 'none').strip().lower()
    fuel_score = float((payload.get('fuel') or {}).get('score') or 0.0)
    rsi_value = float(payload.get('rsi_value') or 50.0)
    return f'pattern={pattern_name}|fuel={_fuel_bucket(fuel_score)}|rsi={_rsi_bucket(rsi_value)}'


def _pick_existing_column(cols: list[str], candidates: list[str]) -> str | None:
    cols_set = {c.lower() for c in cols}
    for item in candidates:
        if item.lower() in cols_set:
            return item
    return None


def _combo_table_info() -> tuple[str, dict[str, str]] | tuple[None, None]:
    now = time.time()
    with _COMBO_CACHE_LOCK:
        if _COMBO_CACHE['expires_at'] > now:
            return _COMBO_CACHE['table'], _COMBO_CACHE['column_map']

    if not os.path.exists(_COMBO_DB_PATH):
        with _COMBO_CACHE_LOCK:
            _COMBO_CACHE.update({'expires_at': now + _COMBO_CACHE_TTL_SEC, 'table': None, 'column_map': None})
        return None, None

    table: str | None = None
    column_map: dict[str, str] | None = None
    try:
        with sqlite3.connect(_COMBO_DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            candidate_tables = [str(r['name']) for r in rows]
            score_rows: list[tuple[int, str, dict[str, str]]] = []
            for tbl in candidate_tables:
                cols = [str(r['name']) for r in conn.execute(f'PRAGMA table_info("{tbl}")').fetchall()]
                combo_col = _pick_existing_column(cols, ['combo_key', 'combo', 'combo_id', 'key'])
                if not combo_col:
                    continue
                col_map = {
                    'combo_key': combo_col,
                    'hit_tp_rate': _pick_existing_column(cols, ['hit_tp_rate', 'tp_rate', 'success_rate', 'win_rate']),
                    'hits_tp': _pick_existing_column(cols, ['hits_tp', 'tp_hits', 'tp_count', 'wins']),
                    'avg_max_profit': _pick_existing_column(cols, ['avg_max_profit', 'avg_profit', 'mean_max_profit']),
                    'wilson': _pick_existing_column(cols, ['wilson', 'wilson_score', 'wilson_lb']),
                    'n': _pick_existing_column(cols, ['n', 'trials', 'sample_size', 'count']),
                    'last_ts': _pick_existing_column(cols, ['last_ts', 'updated_at', 'last_seen', 'ts']),
                }
                score = sum(1 for key in ['hit_tp_rate', 'hits_tp', 'avg_max_profit', 'wilson', 'n', 'last_ts'] if col_map.get(key))
                name_bonus = 2 if any(x in tbl.lower() for x in ('combo', 'outcome', 'recommend')) else 0
                score_rows.append((score + name_bonus, tbl, col_map))

            if score_rows:
                score_rows.sort(key=lambda x: x[0], reverse=True)
                _, table, column_map = score_rows[0]
    except Exception:
        table, column_map = None, None

    with _COMBO_CACHE_LOCK:
        _COMBO_CACHE.update({'expires_at': now + _COMBO_CACHE_TTL_SEC, 'table': table, 'column_map': column_map})
    return table, column_map


def _fetch_combo_stats(combo_key: str) -> dict[str, Any] | None:
    table, column_map = _combo_table_info()
    if not table or not column_map:
        return None

    combo_col = column_map.get('combo_key')
    if not combo_col:
        return None

    wanted = ['combo_key', 'hit_tp_rate', 'hits_tp', 'avg_max_profit', 'wilson', 'n', 'last_ts']
    select_parts: list[str] = []
    for key in wanted:
        col = column_map.get(key)
        if col:
            select_parts.append(f'"{col}" AS "{key}"')
        elif key == 'combo_key':
            select_parts.append(f'"{combo_col}" AS "combo_key"')
        else:
            select_parts.append(f'NULL AS "{key}"')

    query = f'''
        SELECT {", ".join(select_parts)}
        FROM "{table}"
        WHERE "{combo_col}" = ?
        ORDER BY COALESCE("last_ts", 0) DESC
        LIMIT 1
    '''
    try:
        with sqlite3.connect(_COMBO_DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(query, (combo_key,)).fetchone()
            if not row:
                return None
            return {key: row[key] for key in wanted}
    except Exception:
        return None


def _normalize_rate_to_fraction(rate_value: Any) -> float:
    rate = float(rate_value or 0.0)
    return rate / 100.0 if rate > 1.0 else rate


def _scan_best_symbol_by_combo(*, slot: int, min_combo_rate: float, min_sample: int | None = None, interval: str = '1m') -> dict[str, Any]:
    cfg = load_config()
    candidates = run_scan(cfg)
    scan_time = utc_now_iso()
    run_status = 'OK' if candidates else 'EMPTY'
    storage.save_scan_results(
        scan_time,
        run_status,
        candidates,
        meta={'quote_asset': cfg['trading']['quote_asset'], 'source': f'combo_slot_{slot}'},
    )

    if not candidates:
        return {'ok': False, 'slot': slot, 'scan_time': scan_time, 'message': 'Brak kandydatów dla slotu.'}

    preferred_symbol, reserved_symbols = _preferred_and_reserved_symbols(slot)
    slot_cfg = _slot_settings_map().get(int(slot), {})
    selected: dict[str, Any] | None = None

    def _candidate_priority(item: dict[str, Any]) -> tuple[int, int, float, float, float]:
        sym = str(item.get('symbol') or '').upper().strip()
        preferred_rank = 1 if preferred_symbol and sym == preferred_symbol else 0
        reserved_rank = 0 if sym in reserved_symbols else 1
        return (
            preferred_rank,
            reserved_rank,
            float(item.get('hit_tp_rate') or 0.0),
            float(item.get('wilson') or 0.0),
            float(item.get('avg_max_profit') or 0.0),
        )

    for row in candidates:
        symbol = str(row.get('symbol') or '').upper().strip()
        if not symbol:
            continue

        try:
            chart = build_chart_payload(symbol, interval=interval, fuel_cfg=cfg.get('fuel', {}))
        except Exception:
            continue

        combo_key = _build_combo_key(chart)
        combo_stats = _fetch_combo_stats(combo_key)
        if not combo_stats:
            continue

        hit_rate_fraction = _normalize_rate_to_fraction(combo_stats.get('hit_tp_rate'))
        sample_size = int(float(combo_stats.get('n') or 0))
        if hit_rate_fraction < min_combo_rate:
            continue
        if min_sample is not None and sample_size < int(min_sample):
            continue

        candidate = {
            'symbol': symbol,
            'combo_key': combo_key,
            'hit_tp_rate': combo_stats.get('hit_tp_rate'),
            'hits_tp': combo_stats.get('hits_tp'),
            'avg_max_profit': combo_stats.get('avg_max_profit'),
            'wilson': combo_stats.get('wilson'),
            'n': combo_stats.get('n'),
            'last_ts': combo_stats.get('last_ts'),
            'scan_row': row,
            'scan_time': scan_time,
        }

        if selected is None or _candidate_priority(candidate) > _candidate_priority(selected):
            selected = candidate

    if not selected:
        return {
            'ok': False,
            'slot': slot,
            'scan_time': scan_time,
            'message': 'Brak par spełniających warunki combo.',
            'min_combo_rate': min_combo_rate,
            'min_sample': min_sample,
        }

    symbol = str(selected['symbol'])
    storage.upsert_slot_setting(
        slot=slot,
        symbol=symbol,
        budget=slot_cfg.get('budget'),
        tp_pct=slot_cfg.get('tp_pct'),
        sl_pct=slot_cfg.get('sl_pct'),
        auto_enabled=slot_cfg.get('auto_enabled'),
        updated_at=scan_time,
    )

    storage.save_slot_scan_result(
        slot,
        {
            'status': 'OK',
            'symbol': symbol,
            'scan_time': scan_time,
            'source': 'combo',
            'message': 'Wybrano symbol na podstawie combo.',
            'combo_key': selected.get('combo_key'),
            'hit_tp_rate': selected.get('hit_tp_rate'),
            'wilson': selected.get('wilson'),
            'n': selected.get('n'),
            'avg_max_profit': selected.get('avg_max_profit'),
            'candidate': selected.get('scan_row'),
        },
        scan_time,
    )

    return {
        'ok': True,
        'slot': slot,
        'symbol': symbol,
        'combo_key': selected.get('combo_key'),
        'hit_tp_rate': selected.get('hit_tp_rate'),
        'hits_tp': selected.get('hits_tp'),
        'wilson': selected.get('wilson'),
        'n': selected.get('n'),
        'avg_max_profit': selected.get('avg_max_profit'),
        'last_ts': selected.get('last_ts'),
        'scan_time': scan_time,
        'message': f'Wybrano {symbol} na podstawie combo.',
    }


def _combo_message_and_level(combo: dict[str, Any] | None) -> tuple[str, str]:
    if not combo:
        return 'Combo: brak danych historycznych', 'none'

    hit_rate = float(combo.get('hit_tp_rate') or 0.0)
    if hit_rate <= 1.0:
        hit_rate *= 100.0
    n = int(float(combo.get('n') or 0))
    wilson = combo.get('wilson')
    wilson_val = float(wilson) if wilson is not None else None

    if wilson_val is not None:
        if wilson_val >= 0.60 and n >= 20:
            level = 'strong'
            label = 'MOCNE COMBO'
        elif wilson_val >= 0.50 and n >= 10:
            level = 'medium'
            label = 'ŚREDNIE COMBO'
        else:
            level = 'weak'
            label = 'SŁABE COMBO'
    else:
        if hit_rate >= 60 and n >= 20:
            level = 'strong'
            label = 'MOCNE COMBO'
        elif hit_rate >= 50 and n >= 10:
            level = 'medium'
            label = 'ŚREDNIE COMBO'
        else:
            level = 'weak'
            label = 'SŁABE COMBO'

    msg = f'Combo: {label} · skuteczność TP {hit_rate:.0f}% · próba {n}'
    if wilson_val is not None:
        msg += f' · wilson {wilson_val:.2f}'
    return msg, level


def _attach_combo_signal(payload: dict[str, Any]) -> dict[str, Any]:
    combo_key = _build_combo_key(payload)
    combo_stats = _fetch_combo_stats(combo_key)
    message, level = _combo_message_and_level(combo_stats)

    payload['combo_signal'] = {
        'message': message,
        'level': level,
        'combo_key': combo_key,
        'hit_tp_rate': combo_stats.get('hit_tp_rate') if combo_stats else None,
        'hits_tp': combo_stats.get('hits_tp') if combo_stats else None,
        'avg_max_profit': combo_stats.get('avg_max_profit') if combo_stats else None,
        'wilson': combo_stats.get('wilson') if combo_stats else None,
        'n': combo_stats.get('n') if combo_stats else None,
        'last_ts': combo_stats.get('last_ts') if combo_stats else None,
        'db_path': _COMBO_DB_PATH,
    }
    return payload


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
        slot_scan = storage.get_slot_scan_result(slot) or {}
        saved_symbol = str(setting.get('symbol') or '').upper().strip()
        pos_symbol = str((pos or {}).get('symbol') or '').upper().strip()
        scanned_symbol = str(slot_scan.get('symbol') or '').upper().strip()
        auto_enabled = bool(setting.get('auto_enabled'))

        if saved_symbol:
            slot_symbol = saved_symbol
        elif pos_symbol:
            slot_symbol = pos_symbol
        elif auto_enabled and scanned_symbol:
            # Dla slotu w trybie auto trzymaj ostatni symbol ze skanu slotu
            # (nawet po wylogowaniu/zalogowaniu), dopóki auto nie wybierze nowego.
            slot_symbol = scanned_symbol
        else:
            slot_symbol = default_symbol()

        realized = storage.slot_realized_pnl(slot)
        base = {
            'slot': slot,
            'symbol': slot_symbol,
            'auto_enabled': auto_enabled,
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
    payload = _attach_combo_signal(payload)
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
    payload = _attach_combo_signal(payload)
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
    payload = _attach_combo_signal(payload)
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


@app.route('/api/scan_combo_for_slot', methods=['POST'])
@login_required
def scan_combo_for_slot():
    try:
        data = request.get_json(force=True, silent=True) or {}
        cfg = load_config()
        slot_count = int(cfg['trading']['slot_count'])
        slot = int(data.get('slot_id') or 0)
        if slot < 1 or slot > slot_count:
            raise ValueError(f'Nieprawidłowy slot. Użyj 1..{slot_count}.')

        min_combo_rate_input = parse_optional_float(data.get('min_combo_rate'), 'min_combo_rate')
        min_combo_rate = float(min_combo_rate_input if min_combo_rate_input is not None else 0.5)
        if min_combo_rate > 1.0:
            min_combo_rate /= 100.0
        if min_combo_rate < 0 or min_combo_rate > 1:
            raise ValueError('min_combo_rate musi być z zakresu 0..1 lub 0..100%.')

        min_sample_input = parse_optional_float(data.get('min_sample'), 'min_sample')
        min_sample = int(min_sample_input) if min_sample_input is not None else None
        if min_sample is not None and min_sample < 0:
            raise ValueError('min_sample nie może być ujemne.')

        interval = str(data.get('interval') or '1m').strip() or '1m'
        print('COMBO API CALLED', slot, min_combo_rate, min_sample)

        with _SCAN_LOCK:
            result = _scan_best_symbol_by_combo(
                slot=slot,
                min_combo_rate=min_combo_rate,
                min_sample=min_sample,
                interval=interval,
            )

        http_code = 200 if result.get('ok') else 404
        result['success'] = bool(result.get('ok'))
        result['slot_id'] = slot
        return jsonify(result), http_code
    except Exception as exc:
        code = 400 if isinstance(exc, ValueError) else 500
        return jsonify({'success': False, 'ok': False, 'error': str(exc)}), code


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
