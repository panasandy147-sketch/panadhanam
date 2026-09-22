"""Labelling, with the lookahead confined to exactly one place.

The target is a forward-looking question — "did price gain 1.5 ATR within six
bars without first losing 1.0 ATR?" — so building it REQUIRES seeing the
future. That is legitimate at training time and fatal at prediction time, so
it lives in this module alone and nothing here is ever called on live bars.

The path is walked bar by bar rather than compared against the window's max,
because `max(next 6 highs) >= target` says nothing about whether the stop was
hit on the way there. Order matters, and a bar that touches both is scored as
a loss: without tick data the sequence is unknowable, and the optimistic
reading would label losers as winners.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from panaoptions.engine import indicators as ta


def triple_barrier(df: pd.DataFrame, horizon: int = 6,
                   target_atr: float = 1.5, stop_atr: float = 1.0,
                   atr_period: int = 14) -> pd.Series:
    """1 if the target is reached before the stop within `horizon` bars.

    The last `horizon` bars get NaN, not 0: their outcome has not happened
    yet, and calling an unknown outcome a failure would train the model to be
    pessimistic about the end of every day.
    """
    if df.empty:
        return pd.Series(dtype="float64")

    atr = ta.atr(df, atr_period)
    close = df["close"].to_numpy(dtype=float)
    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    atr_values = atr.to_numpy(dtype=float)
    n = len(df)
    labels = np.full(n, np.nan)

    for i in range(n):
        if i + horizon >= n or not np.isfinite(atr_values[i]) or atr_values[i] <= 0:
            continue
        entry = close[i]
        up = entry + target_atr * atr_values[i]
        down = entry - stop_atr * atr_values[i]

        outcome = 0.0
        for j in range(i + 1, i + horizon + 1):
            hit_stop = low[j] <= down
            hit_target = high[j] >= up
            if hit_stop:              # checked first, deliberately
                outcome = 0.0
                break
            if hit_target:
                outcome = 1.0
                break
        labels[i] = outcome

    return pd.Series(labels, index=df.index, name="label")
