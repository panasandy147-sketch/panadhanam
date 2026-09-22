"""Indicators on plain pandas. No TA-Lib, nothing to compile."""
from __future__ import annotations

import pandas as pd

from panaoptions.models import Candle, Indicators


def to_frame(candles: list[Candle]) -> pd.DataFrame:
    if not candles:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.DataFrame([c.model_dump() for c in candles])
    df["ts"] = pd.to_datetime(df["ts"])
    return df.set_index("ts").sort_index()


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def vwap(df: pd.DataFrame) -> pd.Series:
    """Session VWAP, reset each day.

    Carrying yesterday's volume into today's VWAP would anchor the line to a
    price nobody is trading against any more, which is the opposite of what
    VWAP is for.
    """
    typical = (df["high"] + df["low"] + df["close"]) / 3
    notional = typical * df["volume"]
    day = df.index.normalize()
    return (notional.groupby(day).cumsum()
            / df["volume"].groupby(day).cumsum().replace(0, pd.NA)).ffill()


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    return pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    return true_range(df).ewm(alpha=1 / period, adjust=False).mean()


def compute(df: pd.DataFrame, cfg) -> Indicators:
    """The indicator snapshot as of the LAST bar in `df`."""
    if df.empty:
        return Indicators()

    fast = int(cfg.get("technical.fast_ema", 9))
    slow = int(cfg.get("technical.slow_ema", 20))
    lookback = int(cfg.get("technical.volume_lookback", 20))

    close = df["close"]
    volume = df["volume"]
    # The current bar is excluded from its own average, or a big candle would
    # inflate the benchmark it is being measured against.
    avg_volume = volume.shift(1).rolling(lookback, min_periods=3).mean()

    last_avg = float(avg_volume.iloc[-1]) if not pd.isna(avg_volume.iloc[-1]) else 0.0
    last_vol = float(volume.iloc[-1])

    return Indicators(
        ema_fast=float(ema(close, fast).iloc[-1]),
        ema_slow=float(ema(close, slow).iloc[-1]),
        vwap=float(vwap(df).iloc[-1]),
        atr=float(atr(df, int(cfg.get("technical.atr_period", 14))).iloc[-1]),
        volume=last_vol,
        avg_volume=last_avg,
        rvol=round(last_vol / last_avg, 3) if last_avg else 0.0,
        close=float(close.iloc[-1]),
    )


def resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Roll 5-minute bars up to 15-minute ones.

    The higher timeframe is built from bars already seen rather than fetched
    separately, so it can never contain a candle that has not printed yet.
    """
    if df.empty:
        return df
    out = df.resample(rule, label="left", closed="left").agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum",
    })
    return out.dropna(subset=["open", "close"])
