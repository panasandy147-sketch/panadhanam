"""The entry rules, exactly as specified, with every check reported.

    LONG (calls)   price above VWAP, 9 EMA > 20 EMA, bullish engulfing or
                   hammer closing on above-average volume, 15m trend agreeing.
    SHORT (puts)   the mirror image.

A setup that fails records WHY in `blockers`. Silent rejection is how you end
up staring at a screen that does nothing all morning with no idea which rule
is the one that never passes.
"""
from __future__ import annotations

import pandas as pd

from panaoptions.engine import indicators as ta
from panaoptions.engine import patterns
from panaoptions.models import Candle, Direction, Setup


def _trend(df15: pd.DataFrame, cfg) -> int:
    """+1 bullish, -1 bearish, 0 undecided, on the confirmation timeframe."""
    if len(df15) < 3:
        return 0
    fast = ta.ema(df15["close"], int(cfg.get("technical.fast_ema", 9))).iloc[-1]
    slow = ta.ema(df15["close"], int(cfg.get("technical.slow_ema", 20))).iloc[-1]
    if fast > slow:
        return 1
    if fast < slow:
        return -1
    return 0


def evaluate(symbol: str, candles: list[Candle], cfg) -> Setup:
    """Run the rules over the bars seen so far and report the verdict."""
    df = ta.to_frame(candles)
    if len(df) < 21:
        return Setup(symbol=symbol,
                     ts=candles[-1].ts if candles else pd.Timestamp.now(),
                     blockers=[f"only {len(df)} bars — need 21 before the "
                               f"20-EMA and volume average mean anything"])

    snap = ta.compute(df, cfg)
    df15 = ta.resample(df, "15min")
    trend = _trend(df15, cfg)

    multiplier = float(cfg.get("technical.volume_multiplier", 1.0))
    volume_ok = snap.avg_volume > 0 and snap.volume >= snap.avg_volume * multiplier

    bull_pattern = patterns.bullish(df)
    bear_pattern = patterns.bearish(df)

    above_vwap = snap.close > snap.vwap
    below_vwap = snap.close < snap.vwap
    emas_bull = snap.ema_fast > snap.ema_slow
    emas_bear = snap.ema_fast < snap.ema_slow

    setup = Setup(symbol=symbol, ts=df.index[-1].to_pydatetime(),
                  indicators=snap, trend_aligned=False)

    # Which side is even plausible? Both failing is the normal case.
    if bull_pattern and above_vwap and emas_bull:
        direction, pattern, wanted_trend = Direction.LONG, bull_pattern, 1
    elif bear_pattern and below_vwap and emas_bear:
        direction, pattern, wanted_trend = Direction.SHORT, bear_pattern, -1
    else:
        setup.blockers = _why_not(bull_pattern, bear_pattern, above_vwap,
                                  below_vwap, emas_bull, emas_bear)
        return setup

    setup.direction = direction
    setup.pattern = pattern
    setup.trend_aligned = trend == wanted_trend
    setup.confirmations = [
        f"{pattern} on the 5m close",
        f"price {'above' if direction is Direction.LONG else 'below'} VWAP "
        f"({snap.close:.2f} vs {snap.vwap:.2f})",
        f"9 EMA {'>' if direction is Direction.LONG else '<'} 20 EMA "
        f"({snap.ema_fast:.2f} vs {snap.ema_slow:.2f})",
    ]

    if volume_ok:
        setup.confirmations.append(
            f"volume {snap.volume:,.0f} vs {snap.avg_volume:,.0f} average "
            f"(RVOL {snap.rvol:.2f})")
    else:
        setup.blockers.append(
            f"volume {snap.volume:,.0f} is below the {snap.avg_volume:,.0f} "
            f"average — the pattern is not backed by participation")

    if not setup.trend_aligned:
        setup.blockers.append(
            "the 15m trend does not agree with the 5m signal")
    else:
        setup.confirmations.append("15m trend agrees")

    # The level that invalidates the trade: the low (or high) of the signal
    # bar. Break it and the reason for being in the trade has gone.
    last = df.iloc[-1]
    setup.underlying_support = float(
        last["low"] if direction is Direction.LONG else last["high"])
    return setup


def _why_not(bull: str, bear: str, above_vwap: bool, below_vwap: bool,
             emas_bull: bool, emas_bear: bool) -> list[str]:
    """Name the missing condition on the side that came closest."""
    if not bull and not bear:
        return ["no bullish or bearish reversal pattern on this candle"]

    if bull:
        missing = []
        if not above_vwap:
            missing.append("price is not above VWAP")
        if not emas_bull:
            missing.append("9 EMA is not above the 20 EMA")
        return [f"{bull} printed, but {' and '.join(missing)}"]

    missing = []
    if not below_vwap:
        missing.append("price is not below VWAP")
    if not emas_bear:
        missing.append("9 EMA is not below the 20 EMA")
    return [f"{bear} printed, but {' and '.join(missing)}"]
