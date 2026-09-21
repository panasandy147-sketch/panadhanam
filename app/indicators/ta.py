"""Technical indicators on plain pandas — no TA-Lib, no compilation step.

Every function takes a DataFrame with columns [open, high, low, close, volume]
and returns either a Series or a float. Deliberately boring and testable.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from app.core.models import Candle


def candles_to_df(candles: list[Candle]) -> pd.DataFrame:
    if not candles:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.DataFrame([c.model_dump() for c in candles])
    df["ts"] = pd.to_datetime(df["ts"])
    return df.set_index("ts").sort_index()


def resample(candles: list[Candle], minutes: int) -> list[Candle]:
    """Roll a fine series up into a coarser one (5m bars -> 15m bars).

    Used by Practice Day: the higher timeframe has to be built from the bars
    already seen, never fetched separately, or the replay would be looking at
    a 15m bar that has not finished printing yet.

    The final bucket is kept even when it is still forming — that mirrors live
    trading, where you read the developing 15m candle rather than wait for it.
    """
    if not candles or minutes <= 0:
        return []
    step = timedelta(minutes=minutes)
    out: list[Candle] = []
    bucket_start: datetime | None = None
    for c in sorted(candles, key=lambda x: x.ts):
        if bucket_start is None or c.ts - bucket_start >= step:
            bucket_start = c.ts
            out.append(Candle(ts=c.ts, open=c.open, high=c.high,
                              low=c.low, close=c.close, volume=c.volume))
            continue
        cur = out[-1]
        cur.high = max(cur.high, c.high)
        cur.low = min(cur.low, c.low)
        cur.close = c.close
        cur.volume += c.volume
    return out


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period, min_periods=1).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100 - (100 / (1 + rs))
    # avg_loss == 0 means there were no down moves at all: that is RSI 100
    # (fully overbought), not the neutral 50 a naive fillna would produce.
    # Only a flat series, where there is no movement either way, is truly 50.
    no_loss = avg_loss == 0.0
    out = out.mask(no_loss & (avg_gain > 0), 100.0)
    out = out.mask(no_loss & (avg_gain == 0.0), 50.0)
    return out.fillna(50.0)


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    ranges = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1)
    return ranges.max(axis=1)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    return true_range(df).ewm(alpha=1 / period, adjust=False).mean()


def vwap(df: pd.DataFrame) -> pd.Series:
    """Session VWAP. Resets each calendar day so intraday levels stay meaningful."""
    if df.empty:
        return pd.Series(dtype=float)
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    vol = df["volume"].replace(0.0, np.nan)
    day = df.index.normalize() if isinstance(df.index, pd.DatetimeIndex) else None
    if day is None:
        cum_pv = (typical * vol).cumsum()
        cum_v = vol.cumsum()
    else:
        cum_pv = (typical * vol).groupby(day).cumsum()
        cum_v = vol.groupby(day).cumsum()
    out = cum_pv / cum_v
    return out.fillna(typical)


def bollinger(df: pd.DataFrame, period: int = 20, mult: float = 2.0) -> dict[str, pd.Series]:
    mid = sma(df["close"], period)
    sd = df["close"].rolling(period, min_periods=1).std().fillna(0.0)
    return {"mid": mid, "upper": mid + mult * sd, "lower": mid - mult * sd}


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> dict[str, pd.Series]:
    line = ema(series, fast) - ema(series, slow)
    sig = ema(line, signal)
    return {"macd": line, "signal": sig, "hist": line - sig}


def supertrend(df: pd.DataFrame, period: int = 10, mult: float = 3.0) -> pd.Series:
    """+1 uptrend / -1 downtrend."""
    if len(df) < 2:
        return pd.Series([0] * len(df), index=df.index)
    hl2 = (df["high"] + df["low"]) / 2.0
    a = atr(df, period)
    upper, lower = hl2 + mult * a, hl2 - mult * a
    direction = np.ones(len(df), dtype=int)
    close = df["close"].to_numpy()
    up, low = upper.to_numpy(), lower.to_numpy()
    for i in range(1, len(df)):
        if close[i] > up[i - 1]:
            direction[i] = 1
        elif close[i] < low[i - 1]:
            direction[i] = -1
        else:
            direction[i] = direction[i - 1]
    return pd.Series(direction, index=df.index)


def volume_surge(df: pd.DataFrame, lookback: int = 20) -> float:
    """Latest volume as a multiple of its recent average. 1.0 = normal."""
    if df.empty or df["volume"].sum() == 0:
        return 1.0
    avg = df["volume"].tail(lookback).mean()
    if not avg:
        return 1.0
    return float(df["volume"].iloc[-1] / avg)


def swing_levels(df: pd.DataFrame, lookback: int = 20) -> dict[str, float]:
    """Recent structural high/low — used as real stop-loss anchors."""
    if df.empty:
        return {"high": 0.0, "low": 0.0}
    window = df.tail(lookback)
    return {"high": float(window["high"].max()), "low": float(window["low"].min())}


def detect_regime(df: pd.DataFrame, atr_period: int = 14) -> str:
    """Classify the tape. The learning loop keeps separate stats per regime, so a
    setup that only works in trends isn't penalised for a chop day."""
    if len(df) < 30:
        return "rangebound"
    close = df["close"]
    e9, e21, e50 = ema(close, 9), ema(close, 21), ema(close, 50)
    last_atr = float(atr(df, atr_period).iloc[-1])
    price = float(close.iloc[-1])
    atr_pct = (last_atr / price * 100) if price else 0.0

    recent = close.tail(30)
    slope = float(np.polyfit(range(len(recent)), recent.to_numpy(), 1)[0])
    slope_pct = (slope / price * 100) if price else 0.0

    if atr_pct > 1.5:
        return "volatile"
    if e9.iloc[-1] > e21.iloc[-1] > e50.iloc[-1] and slope_pct > 0.02:
        return "trending_up"
    if e9.iloc[-1] < e21.iloc[-1] < e50.iloc[-1] and slope_pct < -0.02:
        return "trending_down"
    return "rangebound"


def compute_all(df: pd.DataFrame, cfg: dict | None = None) -> dict:
    """One call that produces the full indicator snapshot an agent needs."""
    cfg = cfg or {}
    if df.empty:
        return {}
    close = df["close"]
    emas = cfg.get("emas", [9, 21, 50])
    out: dict = {
        "last_close": float(close.iloc[-1]),
        "rsi": float(rsi(close, cfg.get("rsi_period", 14)).iloc[-1]),
        "atr": float(atr(df, cfg.get("atr_period", 14)).iloc[-1]),
        "vwap": float(vwap(df).iloc[-1]),
        "volume_surge": round(volume_surge(df, cfg.get("breakout_lookback", 20)), 2),
        "supertrend": int(supertrend(df).iloc[-1]),
        "regime": detect_regime(df),
    }
    for p in emas:
        out[f"ema{p}"] = float(ema(close, p).iloc[-1])

    out.update(swing_levels(df, cfg.get("breakout_lookback", 20)))
    m = macd(close)
    out["macd_hist"] = float(m["hist"].iloc[-1])

    price = out["last_close"]
    out["above_vwap"] = price > out["vwap"]
    out["atr_pct"] = round(out["atr"] / price * 100, 3) if price else 0.0
    if len(emas) >= 3:
        a, b, c = (out[f"ema{p}"] for p in emas[:3])
        out["ema_stacked_bull"] = a > b > c
        out["ema_stacked_bear"] = a < b < c
    if len(df) >= 2:
        prev = float(close.iloc[-2])
        out["change_pct"] = round((price - prev) / prev * 100, 3) if prev else 0.0
    return out
