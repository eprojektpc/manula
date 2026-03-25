from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import math
from typing import Any
import requests
import pandas as pd

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
    if len(raw) < 60:
        raise ScreenerError(f'Za mało świec dla {symbol}')
    rows = raw if include_live else (raw[:-1] if len(raw) > 2 else raw)
    df = pd.DataFrame(rows, columns=[
        'open_time', 'open', 'high', 'low', 'close', 'volume', 'close_time', 'quote_volume',
        'n_trades', 'taker_buy_base', 'taker_buy_quote', 'ignore'
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
    return rsi.fillna(50)


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


def analyze_symbol(symbol: str, cfg: dict[str, Any]) -> Candidate | None:
    lookback_high = int(cfg['lookback_high_bars'])
    lookback_low = int(cfg['lookback_low_bars'])
    df = klines_to_df(symbol, '1m', limit=max(180, lookback_low + 60))

    close = df['close']
    high = df['high']
    low = df['low']
    volume = df['volume']

    ema9 = close.ewm(span=9, adjust=False).mean()
    ema21 = close.ewm(span=21, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    rsi = compute_rsi(close)
    macd_hist = compute_macd_hist(close)
    atr_pct = compute_atr_pct(df)

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

    if trend != 'UP':
        return None
    if not (float(cfg['rsi_min']) <= last_rsi <= float(cfg['rsi_max'])):
        return None
    if not (float(cfg['min_distance_to_breakout_pct']) <= gap_pct <= float(cfg['max_distance_to_breakout_pct'])):
        return None
    if vol_ratio < float(cfg['min_vol_ratio']):
        return None
    if last_atr_pct < float(cfg['atr_pct_min']) or last_atr_pct > float(cfg['atr_pct_max']):
        return None
    if range_position < float(cfg['min_range_position']) or range_position > float(cfg['max_range_position']):
        return None
    if change_1m_pct > float(cfg['max_change_1m_pct']) or change_3m_pct > float(cfg['max_change_3m_pct']):
        return None
    if ema_spread_pct < float(cfg['ema_spread_min_pct']):
        return None
    if last_macd_hist < float(cfg['min_macd_hist']):
        return None

    breakout_score = max(0.0, (float(cfg['max_distance_to_breakout_pct']) - gap_pct)) * 1.8
    volume_score = min(3.0, max(0.0, (vol_ratio - 1.0) * 2.2))
    rsi_score = max(0.0, 1.8 - abs(last_rsi - 54.0) / 6.0)
    range_score = max(0.0, 1.5 - abs(range_position - 0.78) * 4.0)
    chase_penalty = max(0.0, change_3m_pct - 0.35) * 3.0
    macd_score = min(1.2, max(0.0, last_macd_hist * 40.0))
    atr_score = max(0.0, 1.2 - abs(last_atr_pct - 0.9))
    score = breakout_score + volume_score + rsi_score + range_score + macd_score + atr_score - chase_penalty

    note = (
        f'pre-breakout | RSI {last_rsi:.1f} | vol x{vol_ratio:.2f} | '
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
    quote_asset = config['trading']['quote_asset']
    blacklist = {str(x).upper().strip() for x in scanner.get('blacklist', [])}
    tickers = fetch_tickers()
    allowed_symbols = set(fetch_symbols(quote_asset))

    universe: list[tuple[str, float]] = []
    for row in tickers:
        symbol = str(row.get('symbol', ''))
        if symbol not in allowed_symbols:
            continue
        if symbol in blacklist:
            continue
        quote_volume = float(row.get('quoteVolume') or 0.0)
        if quote_volume < float(scanner['min_quote_volume']):
            continue
        universe.append((symbol, quote_volume))

    universe.sort(key=lambda x: x[1], reverse=True)
    top_symbols = [sym for sym, _ in universe[: int(scanner['top_volume_limit'])]]
    if not top_symbols:
        raise ScreenerError('Brak par spełniających minimalną płynność.')

    candidates: list[Candidate] = []
    workers = max(1, int(scanner.get('workers', 8)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(analyze_symbol, sym, scanner): sym for sym in top_symbols}
        for fut in as_completed(futures):
            try:
                item = fut.result()
                if item is not None:
                    candidates.append(item)
            except Exception:
                continue

    candidates.sort(key=lambda x: x.score, reverse=True)
    return [c.as_dict() for c in candidates[: int(scanner['top_pairs'])]]


def build_chart_payload(symbol: str, interval: str = '1m', limit: int = 220) -> dict[str, Any]:
    df = klines_to_df(symbol, interval=interval, limit=limit, include_live=True)
    close = df['close']
    ema9 = close.ewm(span=9, adjust=False).mean()
    ema21 = close.ewm(span=21, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    rsi = compute_rsi(close)

    candles = []
    ema9_line = []
    ema21_line = []
    ema50_line = []
    rsi_line = []
    for pos, row in enumerate(df.itertuples(index=False)):
        ts = int(row.open_time.timestamp())
        candles.append({
            'time': ts,
            'open': float(row.open),
            'high': float(row.high),
            'low': float(row.low),
            'close': float(row.close),
        })
        ema9_line.append({'time': ts, 'value': round(float(ema9.iloc[pos]), 8)})
        ema21_line.append({'time': ts, 'value': round(float(ema21.iloc[pos]), 8)})
        ema50_line.append({'time': ts, 'value': round(float(ema50.iloc[pos]), 8)})
        rsi_line.append({'time': ts, 'value': round(float(rsi.iloc[pos]), 4)})

    return {
        'symbol': symbol,
        'interval': interval,
        'candles': candles,
        'ema9': ema9_line,
        'ema21': ema21_line,
        'ema50': ema50_line,
        'rsi': rsi_line,
    }
