from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import math
from typing import Any

import pandas as pd
import requests

BINANCE_BASE = 'https://api.binance.com'
SESSION = requests.Session()
SESSION.headers.update({'User-Agent': 'manual-panel/1.0'})


class ScreenerError(RuntimeError):
    pass


@dataclass
class Candidate:
    symbol: str
    score: float
    price: float
    breakout_gap_pct: float
    rsi: float
    vol_ratio: float
    change_3m_pct: float
    atr_pct: float
    range_position: float
    trend: str
    note: str

    def as_dict(self) -> dict[str, Any]:
        return {
            'symbol': self.symbol,
            'score': round(self.score, 4),
            'price': round(self.price, 8),
            'breakout_gap_pct': round(self.breakout_gap_pct, 4),
            'rsi': round(self.rsi, 2),
            'vol_ratio': round(self.vol_ratio, 2),
            'change_3m_pct': round(self.change_3m_pct, 4),
            'atr_pct': round(self.atr_pct, 4),
            'range_position': round(self.range_position, 4),
            'trend': self.trend,
            'note': self.note,
        }


def _get_json(path: str, params: dict[str, Any] | None = None, timeout: int = 15):
    url = f'{BINANCE_BASE}{path}'
    response = SESSION.get(url, params=params or {}, timeout=timeout)
    response.raise_for_status()
    return response.json()


def fetch_symbols(quote_asset: str) -> list[str]:
    info = _get_json('/api/v3/exchangeInfo')
    out: list[str] = []
    for row in info.get('symbols', []):
        if row.get('status') == 'TRADING' and row.get('quoteAsset') == quote_asset.upper():
            out.append(str(row['symbol']))
    return out


def fetch_tickers() -> list[dict[str, Any]]:
    return _get_json('/api/v3/ticker/24hr')


def klines_to_df(symbol: str, interval: str = '1m', limit: int = 180, include_live: bool = False) -> pd.DataFrame:
    raw = _get_json('/api/v3/klines', params={'symbol': symbol, 'interval': interval, 'limit': limit}, timeout=10)
    if not raw:
        raise ScreenerError(f'Brak danych świec dla {symbol}')
    if len(raw) < 60:
        raise ScreenerError(f'Za mało świec dla {symbol}')
    rows = raw if include_live else (raw[:-1] if len(raw) > 2 else raw)
    df = pd.DataFrame(rows, columns=[
        'open_time', 'open', 'high', 'low', 'close', 'volume', 'close_time', 'quote_volume',
        'n_trades', 'taker_buy_base', 'taker_buy_quote', 'ignore',
    ])
    df = df[['open_time', 'open', 'high', 'low', 'close', 'volume']].copy()
    for col in ['open', 'high', 'low', 'close', 'volume']:
        df[col] = df[col].astype(float)
    df['open_time'] = pd.to_datetime(df['open_time'], unit='ms', utc=True)
    return df


def compute_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, math.nan)
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.where(~((avg_loss == 0) & (avg_gain > 0)), 100.0)
    rsi = rsi.where(~((avg_gain == 0) & (avg_loss > 0)), 0.0)
    rsi = rsi.where(~((avg_gain == 0) & (avg_loss == 0)), 50.0)
    return rsi.fillna(50.0)


def compute_macd_hist(close: pd.Series) -> pd.Series:
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    return macd - signal


def compute_atr_pct(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df['high']
    low = df['low']
    close = df['close']
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = tr.rolling(period).mean()
    return (atr / close.replace(0, math.nan)) * 100


def detect_candle_patterns(df: pd.DataFrame) -> dict[str, Any]:
    if len(df) < 3:
        return {'name': None, 'message': 'Brak wzorca'}
    prev = df.iloc[-2]
    cur = df.iloc[-1]
    p_open, p_close = float(prev['open']), float(prev['close'])
    c_open, c_close = float(cur['open']), float(cur['close'])
    c_high, c_low = float(cur['high']), float(cur['low'])

    body = abs(c_close - c_open)
    full = max(c_high - c_low, 1e-12)
    upper_wick = c_high - max(c_open, c_close)
    lower_wick = min(c_open, c_close) - c_low

    bullish_engulfing = (p_close < p_open) and (c_close > c_open) and (c_open <= p_close) and (c_close >= p_open)
    bearish_engulfing = (p_close > p_open) and (c_close < c_open) and (c_open >= p_close) and (c_close <= p_open)
    hammer = lower_wick >= body * 2.2 and upper_wick <= body * 0.8 and (body / full) < 0.4
    shooting_star = upper_wick >= body * 2.2 and lower_wick <= body * 0.8 and (body / full) < 0.4

    if bullish_engulfing:
        return {'name': 'bullish_engulfing', 'message': 'Możliwy wzrost'}
    if bearish_engulfing:
        return {'name': 'bearish_engulfing', 'message': 'Możliwy spadek'}
    if hammer:
        return {'name': 'hammer', 'message': 'Możliwe odbicie'}
    if shooting_star:
        return {'name': 'shooting_star', 'message': 'Ryzyko spadku'}
    return {'name': None, 'message': 'Brak wzorca'}


def compute_fuel_score(close: pd.Series, rsi: pd.Series, macd_hist: pd.Series, fuel_cfg: dict[str, Any]) -> dict[str, Any]:
    last_rsi = float(rsi.iloc[-1])
    last_macd = float(macd_hist.iloc[-1])
    momentum_pct = float(close.pct_change(3).iloc[-1] * 100)

    score = 0.0
    if last_rsi < 35:
        score += 1.0 * float(fuel_cfg.get('rsi_weight', 1.0))
    if last_macd > 0:
        score += 1.0 * float(fuel_cfg.get('macd_weight', 1.0))
    if momentum_pct > -0.2:
        score += 1.0 * float(fuel_cfg.get('momentum_weight', 1.0))

    normalized = max(0.0, min(3.0, score))
    if normalized < 1.0:
        text = 'brak paliwa'
    elif normalized < 2.3:
        text = 'możliwe odbicie'
    else:
        text = 'mocny setup'
    icons = '⛽' * max(0, min(3, int(round(normalized))))
    return {
        'score': round(normalized, 2),
        'icons': icons,
        'text': text,
        'momentum_pct': round(momentum_pct, 4),
    }


def _knife_filter_hit(df: pd.DataFrame, rsi_series: pd.Series, knife_cfg: dict[str, Any]) -> tuple[bool, str]:
    if not bool(knife_cfg.get('knife_filter_enabled', True)):
        return False, ''
    lookback = max(3, int(knife_cfg.get('knife_lookback', 6)))
    if len(df) <= lookback:
        return False, ''

    close = df['close']
    ema50 = close.ewm(span=50, adjust=False).mean()
    ref_close = float(close.iloc[-lookback])
    last_close = float(close.iloc[-1])
    drop_pct = ((last_close / ref_close) - 1.0) * 100 if ref_close else 0.0
    last_rsi = float(rsi_series.iloc[-1])
    below_ema = last_close < float(ema50.iloc[-1])

    too_deep = drop_pct <= float(knife_cfg.get('knife_max_drop_pct', -3.0))
    low_rsi = last_rsi <= float(knife_cfg.get('knife_rsi_threshold', 28.0))
    if too_deep and low_rsi and below_ema:
        return True, f'knife_filter drop={drop_pct:.2f}% rsi={last_rsi:.2f}'
    return False, ''


def analyze_symbol(symbol: str, cfg: dict[str, Any], knife_cfg: dict[str, Any]) -> Candidate | None:
    lookback_high = int(cfg['lookback_high_bars'])
    lookback_low = int(cfg['lookback_low_bars'])
    df = klines_to_df(symbol, '1m', limit=max(180, lookback_low + 60))

    close = df['close']
    high = df['high']
    low = df['low']
    volume = df['volume']

    ema7 = close.ewm(span=7, adjust=False).mean()
    ema25 = close.ewm(span=25, adjust=False).mean()
    ema99 = close.ewm(span=99, adjust=False).mean()
    rsi = compute_rsi(close)
    macd_hist = compute_macd_hist(close)
    atr_pct = compute_atr_pct(df)

    knife_hit, knife_reason = _knife_filter_hit(df, rsi, knife_cfg)
    if knife_hit:
        print(f'[SCREENER_DEBUG] {symbol} rejected_reason={knife_reason}')
        return None

    last_close = float(close.iloc[-1])
    last_rsi = float(rsi.iloc[-1])
    last_macd_hist = float(macd_hist.iloc[-1])
    last_atr_pct = float(atr_pct.iloc[-1]) if pd.notna(atr_pct.iloc[-1]) else 0.0
    recent_high = float(high.iloc[-lookback_high:].max())
    recent_low = float(low.iloc[-lookback_low:].min())
    gap_pct = ((recent_high - last_close) / last_close) * 100 if last_close else 999.0
    range_span = max(recent_high - recent_low, 1e-12)
    range_position = (last_close - recent_low) / range_span
    vol_ratio = float(volume.iloc[-1] / max(volume.iloc[-21:-1].mean(), 1e-12))
    change_1m_pct = float(close.pct_change(1).iloc[-1] * 100)
    change_3m_pct = float((last_close / close.iloc[-4] - 1) * 100)
    ema_spread_pct = float(((ema9.iloc[-1] - ema21.iloc[-1]) / last_close) * 100) if last_close else 0.0

    trend = 'UP' if ema9.iloc[-1] > ema21.iloc[-1] > ema50.iloc[-1] and last_close > ema9.iloc[-1] else 'MIX'

    effective_rsi_max = 40.0
    effective_min_vol_ratio = min(float(cfg.get('min_vol_ratio', 0.0)), 0.30)
    effective_max_gap = max(float(cfg.get('max_distance_to_breakout_pct', 1.2)), 8.0)
    effective_atr_min = max(0.02, min(float(cfg.get('atr_pct_min', 0.15)), 0.05))
    effective_atr_max = max(float(cfg.get('atr_pct_max', 2.8)), 6.0)
    effective_max_change_1m = max(float(cfg.get('max_change_1m_pct', 0.45)), 2.5)
    effective_max_change_3m = max(float(cfg.get('max_change_3m_pct', 0.90)), 5.0)

    if last_rsi > effective_rsi_max or gap_pct < 0.0 or gap_pct > effective_max_gap or vol_ratio < effective_min_vol_ratio:
        return None
    if last_atr_pct < effective_atr_min or last_atr_pct > effective_atr_max:
        return None
    if change_1m_pct > effective_max_change_1m or change_3m_pct > effective_max_change_3m:
        return None

    breakout_score = max(0.0, (effective_max_gap - gap_pct)) * 0.6
    volume_score = min(2.5, max(0.0, (vol_ratio - 0.3) * 1.8))
    rsi_score = max(0.0, (40.0 - last_rsi) / 4.0)
    range_score = max(0.0, 2.0 - abs(range_position - 0.25) * 3.0)
    rebound_bonus = max(0.0, -change_3m_pct) * 0.9
    chase_penalty = max(0.0, change_3m_pct - 0.8) * 1.5
    macd_score = min(0.8, max(0.0, (last_macd_hist + 0.05) * 10.0))
    atr_score = max(0.0, 1.0 - abs(last_atr_pct - 0.7))
    trend_score = 0.4 if trend == 'UP' else 0.0
    ema_score = max(-0.5, min(0.6, ema_spread_pct * 8.0))
    score = breakout_score + volume_score + rsi_score + range_score + rebound_bonus + macd_score + atr_score + trend_score + ema_score - chase_penalty

    note = (
        f'oversold-rebound | RSI {last_rsi:.1f} | vol x{vol_ratio:.2f} | '
        f'gap {gap_pct:.2f}% | 3m {change_3m_pct:.2f}%'
    )
    return Candidate(
        symbol=symbol,
        score=score,
        price=last_close,
        breakout_gap_pct=gap_pct,
        rsi=last_rsi,
        vol_ratio=vol_ratio,
        change_3m_pct=change_3m_pct,
        atr_pct=last_atr_pct,
        range_position=range_position,
        trend=trend,
        note=note,
    )


def run_scan(config: dict[str, Any]) -> list[dict[str, Any]]:
    scanner = config['scanner']
    knife_cfg = config.get('knife_filter', {})
    quote_asset = config['trading']['quote_asset']
    blacklist = {str(x).upper().strip() for x in scanner.get('blacklist', [])}
    tickers = fetch_tickers()
    allowed_symbols = set(fetch_symbols(quote_asset))
    effective_min_quote_volume = min(float(scanner.get('min_quote_volume', 0.0)), 100000.0)

    prefilter_rows: list[tuple[str, float]] = []
    for row in tickers:
        symbol = str(row.get('symbol', ''))
        if symbol not in allowed_symbols or symbol in blacklist:
            continue
        quote_volume = float(row.get('quoteVolume') or 0.0)
        prefilter_rows.append((symbol, quote_volume))

    universe: list[tuple[str, float]] = [(symbol, qv) for symbol, qv in prefilter_rows if qv >= effective_min_quote_volume]
    if not universe and prefilter_rows:
        universe = prefilter_rows[: min(20, len(prefilter_rows))]

    top_limit = max(20, int(scanner.get('top_volume_limit', 50)))
    universe.sort(key=lambda x: x[1], reverse=True)
    top_symbols = [sym for sym, _ in universe[:top_limit]]
    if not top_symbols:
        raise ScreenerError('Brak par spełniających minimalną płynność.')

    ticker_price_map = {str(row.get('symbol', '')): float(row.get('lastPrice') or 0.0) for row in tickers if row.get('symbol')}

    candidates: list[Candidate] = []
    workers = max(1, int(scanner.get('workers', 8)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(analyze_symbol, sym, scanner, knife_cfg): sym for sym in top_symbols}
        for fut in as_completed(futures):
            try:
                item = fut.result()
                if item is not None:
                    candidates.append(item)
            except Exception:
                continue

    candidates.sort(key=lambda x: x.score, reverse=True)
    target_pairs = min(10, max(3, int(scanner.get('top_pairs', 5))))
    selected = [c.as_dict() for c in candidates[:target_pairs]]

    if len(selected) < target_pairs:
        selected_symbols_set = {c['symbol'] for c in selected}
        missing = target_pairs - len(selected)
        fallback_symbols = [sym for sym in top_symbols if sym not in selected_symbols_set][:missing]
        for sym in fallback_symbols:
            selected.append({
                'symbol': sym,
                'score': 0.0,
                'price': round(ticker_price_map.get(sym, 0.0), 8),
                'breakout_gap_pct': 0.0,
                'rsi': 50.0,
                'vol_ratio': 0.0,
                'change_3m_pct': 0.0,
                'atr_pct': 0.0,
                'range_position': 0.0,
                'trend': 'MIX',
                'note': 'fallback_liquidity_candidate',
            })
    return selected


def build_chart_payload(symbol: str, interval: str = '1m', limit: int = 220, fuel_cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    df = klines_to_df(symbol, interval=interval, limit=limit, include_live=True)
    close = df['close']
    ema7 = close.ewm(span=7, adjust=False).mean()
    ema25 = close.ewm(span=25, adjust=False).mean()
    ema99 = close.ewm(span=99, adjust=False).mean()
    rsi = compute_rsi(close)
    macd_hist = compute_macd_hist(close)

    candles, ema7_line, ema25_line, ema99_line, rsi_line = [], [], [], [], []
    for pos, row in enumerate(df.itertuples(index=False)):
        ts = int(row.open_time.timestamp())
        o, h, l, c = float(row.open), float(row.high), float(row.low), float(row.close)
        if not all(math.isfinite(v) for v in (o, h, l, c)):
            continue
        candles.append({'time': ts, 'open': o, 'high': h, 'low': l, 'close': c})
        e7, e25, e99, r = float(ema7.iloc[pos]), float(ema25.iloc[pos]), float(ema99.iloc[pos]), float(rsi.iloc[pos])
        if math.isfinite(e7):
            ema7_line.append({'time': ts, 'value': round(e7, 8)})
        if math.isfinite(e25):
            ema25_line.append({'time': ts, 'value': round(e25, 8)})
        if math.isfinite(e99):
            ema99_line.append({'time': ts, 'value': round(e99, 8)})
        if math.isfinite(r):
            rsi_line.append({'time': ts, 'value': round(r, 4)})

    pattern = detect_candle_patterns(df)
    fuel = compute_fuel_score(close, rsi, macd_hist, fuel_cfg or {})
    rsi_value = float(rsi.iloc[-1]) if len(rsi) else 50.0
    return {
        'symbol': symbol,
        'interval': interval,
        'candles': candles,
        'ema7': ema7_line,
        'ema25': ema25_line,
        'ema99': ema99_line,
        'ema9': ema7_line,
        'ema21': ema25_line,
        'ema50': ema99_line,
        'rsi': rsi_line,
        'rsi_value': round(rsi_value, 2),
        'fuel': fuel,
        'pattern': pattern,
    }
