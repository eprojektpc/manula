from __future__ import annotations

from decimal import Decimal, ROUND_DOWN
from functools import lru_cache
from pathlib import Path
from typing import Any
import hashlib
import hmac
import os
import time
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent
BASE_URL = 'https://api.binance.com'
SESSION = requests.Session()
SESSION.headers.update({'User-Agent': 'manual-panel/1.0'})


def _load_env() -> None:
    for path in (REPO_ROOT / '.env', Path('/root/.env')):
        if path.exists():
            load_dotenv(path, override=True)


def _clean_env(value: str | None) -> str:
    return str(value or '').strip().strip('"').strip("'")


def _resolve_env(*aliases: str) -> str:
    for alias in aliases:
        value = _clean_env(os.environ.get(alias))
        if value:
            return value
    return ''


@lru_cache(maxsize=1)
def credentials() -> tuple[str, str]:
    _load_env()
    api_key = _resolve_env('BINANCE_API_KEY', 'API_KEY')
    api_secret = _resolve_env('BINANCE_API_SECRET', 'API_SECRET')
    if not api_key or not api_secret:
        raise RuntimeError('Brak BINANCE_API_KEY/BINANCE_API_SECRET albo API_KEY/API_SECRET.')
    return api_key, api_secret


def _request(method: str, path: str, *, params: dict[str, Any] | None = None, signed: bool = False, timeout: int = 20):
    params = {k: v for k, v in (params or {}).items() if v is not None}
    headers = {}
    if signed:
        api_key, api_secret = credentials()
        params['timestamp'] = int(time.time() * 1000)
        params.setdefault('recvWindow', 15000)
        query = urlencode(params, doseq=True)
        signature = hmac.new(api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        params['signature'] = signature
        headers['X-MBX-APIKEY'] = api_key
    url = f'{BASE_URL}{path}'
    if method == 'GET':
        response = SESSION.get(url, params=params, headers=headers, timeout=timeout)
    else:
        response = SESSION.post(url, params=params, headers=headers, timeout=timeout)
    if response.status_code >= 400:
        try:
            payload = response.json()
        except Exception:
            payload = response.text
        raise RuntimeError(f'Binance API error {response.status_code}: {payload}')
    return response.json()


def _plain(value: Decimal) -> str:
    s = format(value.normalize(), 'f')
    if '.' in s:
        s = s.rstrip('0').rstrip('.')
    return s or '0'


def get_price(symbol: str) -> float | None:
    ticker = _request('GET', '/api/v3/ticker/price', params={'symbol': str(symbol).upper().strip()}, signed=False)
    return float(ticker['price']) if ticker and 'price' in ticker else None


def get_asset_balance(asset: str) -> float:
    account = _request('GET', '/api/v3/account', signed=True)
    target = str(asset).upper().strip()
    for bal in account.get('balances', []):
        if bal.get('asset') == target:
            return float(bal.get('free') or 0.0)
    return 0.0


def get_lot_info(symbol: str) -> tuple[float, str, float]:
    info = _request('GET', '/api/v3/exchangeInfo', params={'symbol': str(symbol).upper().strip()}, signed=False)
    rows = info.get('symbols') or []
    if not rows:
        raise RuntimeError(f'Brak symbol info dla {symbol}')
    row = rows[0]
    min_qty = 0.0
    step_size = '0.00000001'
    min_notional = 0.0
    for f in row.get('filters', []):
        if f.get('filterType') == 'LOT_SIZE':
            min_qty = float(f.get('minQty', 0.0))
            step_size = str(f.get('stepSize', '0.00000001'))
        elif f.get('filterType') in {'MIN_NOTIONAL', 'NOTIONAL'}:
            min_notional = float(f.get('minNotional', 0.0))
    return min_qty, step_size, min_notional


def quantize_to_step(value: float | Decimal, step_size_str: str) -> Decimal:
    value_dec = Decimal(str(value))
    step = Decimal(str(step_size_str))
    if step <= 0:
        return value_dec
    return (value_dec / step).to_integral_value(rounding=ROUND_DOWN) * step


def _parse_order_response(order: Any, fallback_price: float | None, fallback_qty: float | None) -> dict[str, Any]:
    if not isinstance(order, dict):
        return {
            'order_id': None,
            'executed_qty': float(fallback_qty or 0.0),
            'avg_price': float(fallback_price or 0.0),
            'value': float((fallback_qty or 0.0) * (fallback_price or 0.0)),
            'raw': order,
        }

    executed_qty = float(order.get('executedQty') or fallback_qty or 0.0)
    quote_value = float(order.get('cummulativeQuoteQty') or 0.0)
    fills = order.get('fills') or []
    avg_price = float(fallback_price or 0.0)
    if fills:
        try:
            qty_sum = sum(float(f.get('qty', 0.0)) for f in fills)
            quote_sum = sum(float(f.get('qty', 0.0)) * float(f.get('price', 0.0)) for f in fills)
            if qty_sum > 0:
                avg_price = quote_sum / qty_sum
        except Exception:
            pass
    elif executed_qty > 0 and quote_value > 0:
        avg_price = quote_value / executed_qty
    if quote_value <= 0:
        quote_value = executed_qty * avg_price
    return {
        'order_id': str(order.get('orderId') or ''),
        'executed_qty': float(executed_qty),
        'avg_price': float(avg_price),
        'value': float(quote_value),
        'raw': order,
    }


def buy_quote(symbol: str, quote_amount: float) -> dict[str, Any]:
    symbol = str(symbol or '').upper().strip()
    price = float(get_price(symbol) or 0.0)
    _, _, min_notional = get_lot_info(symbol)
    quote_dec = Decimal(str(quote_amount)).quantize(Decimal('0.01'), rounding=ROUND_DOWN)
    if quote_dec <= 0:
        raise ValueError('Budżet BUY musi być > 0.')
    if float(quote_dec) < float(min_notional):
        raise ValueError(f'Budżet {quote_dec} jest poniżej minNotional {min_notional}.')

    quote_asset = 'USDC' if symbol.endswith('USDC') else ('USDT' if symbol.endswith('USDT') else None)
    if quote_asset:
        balance = Decimal(str(get_asset_balance(quote_asset)))
        if balance < quote_dec:
            raise ValueError(f'Za mało {quote_asset}: masz {balance}, potrzebujesz {quote_dec}.')

    order = _request('POST', '/api/v3/order', signed=True, params={
        'symbol': symbol,
        'side': 'BUY',
        'type': 'MARKET',
        'quoteOrderQty': _plain(quote_dec),
        'newOrderRespType': 'FULL',
    })
    parsed = _parse_order_response(order, price, float(quote_dec) / price if price else 0.0)
    parsed['symbol'] = symbol
    parsed['side'] = 'BUY'
    return parsed


def sell_quantity(symbol: str, quantity: float) -> dict[str, Any]:
    symbol = str(symbol or '').upper().strip()
    price = float(get_price(symbol) or 0.0)
    min_qty, step_size, min_notional = get_lot_info(symbol)
    qty = quantize_to_step(quantity, step_size)
    if qty <= 0:
        raise ValueError('Ilość SELL po zaokrągleniu do stepSize wyszła 0.')
    if float(qty) < float(min_qty):
        raise ValueError(f'Ilość {qty} jest poniżej minQty {min_qty}.')
    if float(qty) * price < float(min_notional):
        raise ValueError(f'Wartość pozycji {float(qty) * price:.6f} jest poniżej minNotional {min_notional}.')

    base_asset = symbol[:-4] if symbol.endswith('USDC') or symbol.endswith('USDT') else symbol
    balance_qty = quantize_to_step(get_asset_balance(base_asset), step_size)
    qty = min(qty, balance_qty)
    if qty <= 0:
        raise ValueError(f'Brak salda {base_asset} do sprzedaży.')

    order = _request('POST', '/api/v3/order', signed=True, params={
        'symbol': symbol,
        'side': 'SELL',
        'type': 'MARKET',
        'quantity': _plain(qty),
        'newOrderRespType': 'FULL',
    })
    parsed = _parse_order_response(order, price, float(qty))
    parsed['symbol'] = symbol
    parsed['side'] = 'SELL'
    return parsed
