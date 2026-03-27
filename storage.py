from __future__ import annotations

from pathlib import Path
import json
import sqlite3
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / 'manual.db'


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f'PRAGMA table_info({table})').fetchall()
    return {str(r['name']) for r in rows}


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    if column not in _table_columns(conn, table):
        conn.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')


def init_db() -> None:
    conn = connect()
    cur = conn.cursor()
    cur.executescript(
        '''
        CREATE TABLE IF NOT EXISTS positions (
            slot INTEGER PRIMARY KEY,
            symbol TEXT NOT NULL,
            entry_price REAL NOT NULL,
            quantity REAL NOT NULL,
            budget REAL NOT NULL,
            tp_pct REAL NOT NULL,
            sl_pct REAL NOT NULL,
            tp_price REAL,
            sl_price REAL,
            current_price REAL,
            pnl_pct REAL DEFAULT 0,
            pnl_value REAL DEFAULT 0,
            order_id TEXT,
            close_reason TEXT,
            fuel_score REAL,
            pattern_info TEXT,
            opened_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'OPEN'
        );

        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slot INTEGER,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            price REAL NOT NULL,
            quantity REAL NOT NULL,
            value REAL NOT NULL,
            pnl_pct REAL,
            pnl_value REAL,
            reason TEXT,
            order_id TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS scan_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_time TEXT NOT NULL,
            scan_status TEXT NOT NULL,
            meta_json TEXT
        );

        CREATE TABLE IF NOT EXISTS scan_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_run_id INTEGER NOT NULL,
            rank_idx INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            score REAL NOT NULL,
            price REAL NOT NULL,
            breakout_gap_pct REAL,
            rsi REAL,
            vol_ratio REAL,
            change_3m_pct REAL,
            atr_pct REAL,
            range_position REAL,
            trend TEXT,
            note TEXT,
            FOREIGN KEY(scan_run_id) REFERENCES scan_runs(id)
        );

        CREATE TABLE IF NOT EXISTS app_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        '''
    )

    # Safe migrations for older DB files.
    _ensure_column(conn, 'positions', 'tp_price', 'REAL')
    _ensure_column(conn, 'positions', 'sl_price', 'REAL')
    _ensure_column(conn, 'positions', 'close_reason', 'TEXT')
    _ensure_column(conn, 'positions', 'fuel_score', 'REAL')
    _ensure_column(conn, 'positions', 'pattern_info', 'TEXT')

    conn.commit()
    conn.close()


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def get_state(key: str, default: Any = None) -> Any:
    conn = connect()
    row = conn.execute('SELECT value FROM app_state WHERE key=?', (key,)).fetchone()
    conn.close()
    if not row:
        return default
    try:
        return json.loads(row['value'])
    except Exception:
        return row['value']


def set_state(key: str, value: Any, updated_at: str) -> None:
    payload = json.dumps(value, ensure_ascii=False)
    conn = connect()
    conn.execute(
        '''
        INSERT INTO app_state(key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        ''',
        (key, payload, updated_at),
    )
    conn.commit()
    conn.close()


def get_open_position(slot: int) -> dict[str, Any] | None:
    conn = connect()
    row = conn.execute('SELECT * FROM positions WHERE slot=? AND status="OPEN"', (slot,)).fetchone()
    conn.close()
    return row_to_dict(row)


def get_open_positions() -> list[dict[str, Any]]:
    conn = connect()
    rows = conn.execute('SELECT * FROM positions WHERE status="OPEN" ORDER BY slot').fetchall()
    conn.close()
    return rows_to_dicts(rows)


def upsert_open_position(*, slot: int, symbol: str, entry_price: float, quantity: float, budget: float, tp_pct: float, sl_pct: float, tp_price: float, sl_price: float, order_id: str | None, opened_at: str, fuel_score: float | None = None, pattern_info: str | None = None) -> None:
    conn = connect()
    conn.execute(
        '''
        INSERT INTO positions(slot, symbol, entry_price, quantity, budget, tp_pct, sl_pct, tp_price, sl_price, current_price, pnl_pct, pnl_value, order_id, close_reason, fuel_score, pattern_info, opened_at, updated_at, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, NULL, ?, ?, ?, ?, 'OPEN')
        ON CONFLICT(slot) DO UPDATE SET
            symbol=excluded.symbol,
            entry_price=excluded.entry_price,
            quantity=excluded.quantity,
            budget=excluded.budget,
            tp_pct=excluded.tp_pct,
            sl_pct=excluded.sl_pct,
            tp_price=excluded.tp_price,
            sl_price=excluded.sl_price,
            current_price=excluded.current_price,
            pnl_pct=0,
            pnl_value=0,
            order_id=excluded.order_id,
            close_reason=NULL,
            fuel_score=excluded.fuel_score,
            pattern_info=excluded.pattern_info,
            opened_at=excluded.opened_at,
            updated_at=excluded.updated_at,
            status='OPEN'
        ''',
        (slot, symbol, entry_price, quantity, budget, tp_pct, sl_pct, tp_price, sl_price, entry_price, order_id, fuel_score, pattern_info, opened_at, opened_at),
    )
    conn.commit()
    conn.close()


def update_position_metrics(slot: int, current_price: float, pnl_pct: float, pnl_value: float, updated_at: str) -> None:
    conn = connect()
    conn.execute(
        'UPDATE positions SET current_price=?, pnl_pct=?, pnl_value=?, updated_at=? WHERE slot=? AND status="OPEN"',
        (current_price, pnl_pct, pnl_value, updated_at, slot),
    )
    conn.commit()
    conn.close()


def close_position(slot: int, updated_at: str, reason: str | None = None) -> None:
    conn = connect()
    conn.execute('UPDATE positions SET status="CLOSED", close_reason=?, updated_at=? WHERE slot=? AND status="OPEN"', (reason, updated_at, slot))
    conn.commit()
    conn.close()


def record_trade(*, slot: int | None, symbol: str, side: str, price: float, quantity: float, value: float, pnl_pct: float | None, pnl_value: float | None, reason: str | None, order_id: str | None, created_at: str) -> int:
    conn = connect()
    cur = conn.execute(
        '''
        INSERT INTO trades(slot, symbol, side, price, quantity, value, pnl_pct, pnl_value, reason, order_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''',
        (slot, symbol, side, price, quantity, value, pnl_pct, pnl_value, reason, order_id, created_at),
    )
    conn.commit()
    trade_id = int(cur.lastrowid)
    conn.close()
    return trade_id


def list_trades(limit: int = 100) -> list[dict[str, Any]]:
    conn = connect()
    rows = conn.execute('SELECT * FROM trades ORDER BY id DESC LIMIT ?', (int(limit),)).fetchall()
    conn.close()
    return rows_to_dicts(rows)


def save_scan_results(scan_time: str, status: str, candidates: list[dict[str, Any]], meta: dict[str, Any] | None = None) -> int:
    conn = connect()
    cur = conn.execute(
        'INSERT INTO scan_runs(scan_time, scan_status, meta_json) VALUES (?, ?, ?)',
        (scan_time, status, json.dumps(meta or {}, ensure_ascii=False)),
    )
    scan_run_id = int(cur.lastrowid)
    for idx, item in enumerate(candidates, start=1):
        conn.execute(
            '''
            INSERT INTO scan_results(scan_run_id, rank_idx, symbol, score, price, breakout_gap_pct, rsi, vol_ratio, change_3m_pct, atr_pct, range_position, trend, note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                scan_run_id,
                idx,
                item.get('symbol'),
                float(item.get('score', 0.0)),
                float(item.get('price', 0.0)),
                float(item.get('breakout_gap_pct', 0.0)),
                float(item.get('rsi', 0.0)),
                float(item.get('vol_ratio', 0.0)),
                float(item.get('change_3m_pct', 0.0)),
                float(item.get('atr_pct', 0.0)),
                float(item.get('range_position', 0.0)),
                str(item.get('trend', '')),
                str(item.get('note', '')),
            ),
        )
    conn.commit()
    conn.close()
    return scan_run_id


def latest_scan_candidates(limit: int = 10) -> list[dict[str, Any]]:
    conn = connect()
    run_row = conn.execute('SELECT id, scan_time, scan_status FROM scan_runs ORDER BY id DESC LIMIT 1').fetchone()
    if not run_row:
        conn.close()
        return []
    rows = conn.execute(
        'SELECT * FROM scan_results WHERE scan_run_id=? ORDER BY rank_idx ASC LIMIT ?',
        (run_row['id'], int(limit)),
    ).fetchall()
    out = rows_to_dicts(rows)
    for row in out:
        row['scan_time'] = run_row['scan_time']
        row['scan_status'] = run_row['scan_status']
    conn.close()
    return out


def scan_history(limit: int = 50) -> list[dict[str, Any]]:
    conn = connect()
    rows = conn.execute(
        '''
        SELECT r.scan_time, r.scan_status, s.rank_idx, s.symbol, s.score, s.price, s.breakout_gap_pct,
               s.rsi, s.vol_ratio, s.change_3m_pct, s.atr_pct, s.range_position, s.trend, s.note
          FROM scan_results s
          JOIN scan_runs r ON r.id = s.scan_run_id
         ORDER BY s.id DESC
         LIMIT ?
        ''',
        (int(limit),),
    ).fetchall()
    conn.close()
    return rows_to_dicts(rows)
