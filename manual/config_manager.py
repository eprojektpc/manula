from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any
import yaml

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / 'config.yml'

DEFAULT_CONFIG: dict[str, Any] = {
    'app': {
        'host': '0.0.0.0',
        'port': 5099,
        'debug': False,
        'secret_key': 'manual-panel-secret-change-me',
    },
    'trading': {
        'quote_asset': 'USDC',
        'default_budget': 25.0,
        'slot_count': 3,
        'tp_pct': 0.70,
        'sl_pct': 0.60,
        'monitor_interval_sec': 3,
        'allow_manual_sell_without_slot': False,
    },
    'scanner': {
        'enabled': True,
        'scan_interval_sec': 90,
        'top_pairs': 2,
        'top_volume_limit': 35,
        'min_quote_volume': 1000000.0,
        'lookback_high_bars': 20,
        'lookback_low_bars': 40,
        'min_distance_to_breakout_pct': 0.08,
        'max_distance_to_breakout_pct': 1.20,
        'min_vol_ratio': 1.15,
        'rsi_min': 48.0,
        'rsi_max': 63.0,
        'ema_spread_min_pct': 0.03,
        'atr_pct_min': 0.15,
        'atr_pct_max': 2.80,
        'min_range_position': 0.58,
        'max_range_position': 0.93,
        'max_change_1m_pct': 0.45,
        'max_change_3m_pct': 0.90,
        'min_macd_hist': -0.02,
        'workers': 8,
        'blacklist': [],
    },
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG)
        return deepcopy(DEFAULT_CONFIG)
    try:
        data = yaml.safe_load(CONFIG_PATH.read_text(encoding='utf-8')) or {}
        if not isinstance(data, dict):
            data = {}
        return _deep_merge(DEFAULT_CONFIG, data)
    except Exception:
        return deepcopy(DEFAULT_CONFIG)


def save_config(data: dict[str, Any]) -> dict[str, Any]:
    cfg = _deep_merge(DEFAULT_CONFIG, data or {})
    CONFIG_PATH.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding='utf-8')
    return cfg


FIELDS = {
    'trading.default_budget': float,
    'trading.slot_count': int,
    'trading.tp_pct': float,
    'trading.sl_pct': float,
    'trading.monitor_interval_sec': int,
    'scanner.enabled': bool,
    'scanner.scan_interval_sec': int,
    'scanner.top_pairs': int,
    'scanner.top_volume_limit': int,
    'scanner.min_quote_volume': float,
    'scanner.lookback_high_bars': int,
    'scanner.lookback_low_bars': int,
    'scanner.min_distance_to_breakout_pct': float,
    'scanner.max_distance_to_breakout_pct': float,
    'scanner.min_vol_ratio': float,
    'scanner.rsi_min': float,
    'scanner.rsi_max': float,
    'scanner.ema_spread_min_pct': float,
    'scanner.atr_pct_min': float,
    'scanner.atr_pct_max': float,
    'scanner.min_range_position': float,
    'scanner.max_range_position': float,
    'scanner.max_change_1m_pct': float,
    'scanner.max_change_3m_pct': float,
    'scanner.min_macd_hist': float,
    'scanner.workers': int,
    'scanner.blacklist': list,
    'trading.quote_asset': str,
}


def _coerce(value: Any, caster):
    if caster is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {'1', 'true', 'yes', 'on', 'tak'}
        return bool(value)
    if caster is list:
        if isinstance(value, list):
            return [str(x).strip().upper() for x in value if str(x).strip()]
        if isinstance(value, str):
            raw = [x.strip().upper() for x in value.replace('\n', ',').split(',')]
            return [x for x in raw if x]
        return []
    if caster is int:
        return int(float(value))
    if caster is float:
        return float(value)
    return str(value).strip()


def update_from_flat_payload(payload: dict[str, Any]) -> dict[str, Any]:
    cfg = load_config()
    for dotted, caster in FIELDS.items():
        if dotted not in payload:
            continue
        section, key = dotted.split('.', 1)
        cfg.setdefault(section, {})
        cfg[section][key] = _coerce(payload[dotted], caster)
    return save_config(cfg)
