"""Two World Cup champions' rules, as switchable filters (off by default):

  * Pau Perdices (2025 World Cup of Forex): a with-trend pullback of 38-50%
    of the impulse, with RSI diverging at its extreme;
  * Kevin McCormick (2021 World Cup futures): DeMark's TD Setup 9 — a
    reversal only once the move it reverses is exhausted.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd

from panaoptions.engine.strategies import demark_check, perdices_check, rsi, td_setup


def _frame(closes, spread=0.2):
    t0 = datetime(2026, 9, 29, 14, 0, tzinfo=UTC)
    idx = [t0 + timedelta(minutes=5 * i) for i in range(len(closes))]
    rows = []
    prev = closes[0]
    for c in closes:
        rows.append({"open": prev, "high": max(prev, c) + spread, "low": min(prev, c) - spread,
                     "close": c, "volume": 1000})
        prev = c
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx))


def test_td_setup_counts_closes_against_the_close_four_bars_earlier():
    falling = pd.Series([100 - i for i in range(15)], dtype=float)
    assert td_setup(falling) == (11, 0)
    rising = pd.Series([100 + i for i in range(13)], dtype=float)
    assert td_setup(rising) == (0, 9)
    assert td_setup(pd.Series([100.0, 101, 100, 101, 100, 101, 100, 101])) == (0, 0)


def test_demark_lets_a_reversal_through_only_after_an_exhausted_move(cfg):
    down = _frame([110 - i for i in range(14)])              # 10 lower closes
    why, note = demark_check(down, True, cfg)
    assert why == "" and "TD buy setup" in note
    why, _ = demark_check(down, False, cfg)                   # no sell setup
    assert "no completed DeMark TD sell setup" in why
    chop = _frame([100, 101, 100, 102, 100, 101, 99, 101, 100, 102, 101, 100])
    assert "no completed" in demark_check(chop, True, cfg)[0]


def test_perdices_takes_a_38_to_50_percent_pullback(cfg):
    # the 24-bar window starts the impulse at ~101.9; up to 110.1, back to
    # 106.4 = 45% of it, and turning up
    up = [100 + i * 0.5 for i in range(21)]                  # 100 .. 110
    pull = [109, 108, 107.5, 107, 106.8, 106.5, 106.9]
    why, note = perdices_check(_frame(up + pull, spread=0.1), True,
                               _cfg_no_div(cfg))
    assert why == "" and "of the impulse" in note


def test_perdices_refuses_a_shallow_or_a_deep_pullback(cfg):
    up = [100 + i * 0.5 for i in range(21)]
    shallow = up + [109.6, 109.3, 109.2, 109.5]               # ~9%
    deep = up + [108, 106, 104, 102, 101, 101.5]              # ~90%
    c = _cfg_no_div(cfg)
    assert "Perdices wants 38%-50%" in perdices_check(_frame(shallow, 0.1), True, c)[0]
    assert "Perdices wants 38%-50%" in perdices_check(_frame(deep, 0.1), True, c)[0]


def test_rsi_is_bounded_and_rises_on_gains():
    s = pd.Series([100 + i for i in range(30)], dtype=float)
    r = rsi(s)
    assert 0 <= r.min() and r.max() <= 100 and r.iloc[-1] > 90


def test_both_ship_switched_off(shipped):
    assert shipped.get("strategies.vwap_ema_pullback.perdices.enabled") is False
    assert shipped.get("demark.enabled") is False


def _cfg_no_div(cfg):
    cfg.data["strategies"]["vwap_ema_pullback"]["perdices"]["rsi_divergence"] = False
    return cfg
