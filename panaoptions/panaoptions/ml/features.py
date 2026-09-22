"""Feature engineering. Every feature is computable from bars 0..N only.

A feature that peeks at bar N+1 makes a model that looks brilliant in backtest
and loses money live. The rule enforced here: only shift(+k) for k >= 0, and
every rolling window ends at the current bar.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from panaoptions.engine import indicators as ta

FEATURE_COLUMNS = [
    "rsi_14", "macd_hist", "dist_from_vwap_atr", "rvol", "bb_width",
    "body_to_range", "upper_wick_ratio", "lower_wick_ratio",
    "ema_spread_atr", "atr_pct", "overnight_gap_pct", "hv_ratio_5d",
    "minutes_into_session",
]


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    # A pure uptrend has zero average loss, which is RSI 100, not the neutral
    # 50 that a naive fillna produces.
    return out.where(~((loss == 0) & (gain > 0)), 100.0).fillna(50.0)


def macd_histogram(close: pd.Series) -> pd.Series:
    macd = ta.ema(close, 12) - ta.ema(close, 26)
    return macd - ta.ema(macd, 9)


def bollinger_width(close: pd.Series, period: int = 20) -> pd.Series:
    """Band width as a fraction of the middle band — a squeeze detector."""
    mid = close.rolling(period, min_periods=period // 2).mean()
    sd = close.rolling(period, min_periods=period // 2).std()
    return ((mid + 2 * sd) - (mid - 2 * sd)) / mid.replace(0, np.nan)


def build(df: pd.DataFrame, daily: pd.DataFrame | None = None) -> pd.DataFrame:
    """One row of features per bar, aligned to that bar's index."""
    if df.empty:
        return pd.DataFrame(columns=FEATURE_COLUMNS)

    out = pd.DataFrame(index=df.index)
    close = df["close"]
    atr = ta.atr(df, 14)
    safe_atr = atr.replace(0, np.nan)

    out["rsi_14"] = rsi(close, 14)
    out["macd_hist"] = macd_histogram(close)
    # Distances are normalised by ATR so a $570 SPY and a $160 AMD are on the
    # same scale. An unnormalised distance just teaches the model the ticker.
    out["dist_from_vwap_atr"] = (close - ta.vwap(df)) / safe_atr

    avg_volume = df["volume"].shift(1).rolling(20, min_periods=5).mean()
    out["rvol"] = df["volume"] / avg_volume.replace(0, np.nan)
    out["bb_width"] = bollinger_width(close)

    rng = (df["high"] - df["low"]).replace(0, np.nan)
    body = (close - df["open"]).abs()
    upper = df["high"] - df[["close", "open"]].max(axis=1)
    lower = df[["close", "open"]].min(axis=1) - df["low"]
    out["body_to_range"] = body / rng
    out["upper_wick_ratio"] = upper / rng
    out["lower_wick_ratio"] = lower / rng

    out["ema_spread_atr"] = (ta.ema(close, 9) - ta.ema(close, 20)) / safe_atr
    out["atr_pct"] = atr / close.replace(0, np.nan) * 100

    out["overnight_gap_pct"] = _overnight_gap(df)
    out["hv_ratio_5d"] = _hv_ratio(df, daily)
    out["minutes_into_session"] = _minutes_into_session(df)

    return out.replace([np.inf, -np.inf], np.nan)


def _overnight_gap(df: pd.DataFrame) -> pd.Series:
    """Today's first bar against yesterday's last close, held all day."""
    day = pd.Series(df.index.normalize(), index=df.index)
    first_open = df.groupby(day)["open"].transform("first")
    prev_close = df.groupby(day)["close"].last().shift(1)
    mapped = day.map(prev_close)
    return ((first_open - mapped) / mapped.replace(0, np.nan) * 100).astype(float)


def _hv_ratio(df: pd.DataFrame, daily: pd.DataFrame | None) -> pd.Series:
    """Recent realised volatility against its own longer average."""
    if daily is None or daily.empty:
        returns = df["close"].pct_change()
        short = returns.rolling(78, min_periods=20).std()     # ~1 session
        long = returns.rolling(390, min_periods=60).std()     # ~5 sessions
        return (short / long.replace(0, np.nan)).astype(float)

    returns = daily["close"].pct_change()
    ratio = (returns.rolling(5, min_periods=3).std()
             / returns.rolling(20, min_periods=10).std().replace(0, np.nan))
    # Daily values are as of that day's close, so shift before mapping them
    # onto intraday bars — otherwise a 09:35 bar would know today's close.
    ratio = ratio.shift(1)
    ratio.index = pd.to_datetime(ratio.index).normalize()
    return pd.Series(df.index.normalize(), index=df.index).map(ratio).astype(float)


def _minutes_into_session(df: pd.DataFrame) -> pd.Series:
    idx = df.index
    return pd.Series((idx.hour * 60 + idx.minute) - (9 * 60 + 30),
                     index=idx).astype(float)
