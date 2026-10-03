"""SJK 9/21 · VWAP · ADX v2 (sjk912_vwapadx): the engine's long, short,
confirmation expiry, chop / ADX / VWAP rejections, the split exit, the
backtest runner's report, and the desk's wiring."""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.strategies import OWN_PLAN
from app.strategies import sjk912_vwapadx as m
from tests.random_tape import tape

LONG_SEED, SHORT_SEED = 28, 36


def run(bars, **kw):
    eng = m.SJK912_VWAP_ADX_Engine(window=None, square_off=None, **kw)
    return eng, [eng.on_bar_update(b) for b in bars]


def first(events, action):
    return next(e for e in events if e.action == action)


def test_a_valid_long_follows_the_spec():
    bars = tape(LONG_SEED)
    _, events = run(bars)
    buy = first(events, m.BUY)
    k = events.index(buy)
    cross, conf = bars[k - 1], bars[k]
    assert "crossover candle" in events[k - 1].reason          # bar t-1, never entered on
    meta = buy.metadata
    assert buy.trigger_type == m.BULLISH
    assert conf.close > meta["ema9"] and conf.close > meta["ema21"] and conf.close > meta["vwap"]
    assert meta["adx"] > 20 and meta["di_plus"] > meta["di_minus"]
    assert abs(conf.close - meta["vwap"]) <= 2.5 * meta["atr14"]
    structural = min(cross.low, conf.low) - 2 * 0.01
    assert buy.stop_loss == pytest.approx(min(structural, conf.close - meta["atr14"]), abs=1e-3)
    assert buy.entry_price - buy.stop_loss >= meta["atr14"] - 1e-3        # 1 x ATR floor
    risk = buy.entry_price - buy.stop_loss
    assert buy.take_profit == pytest.approx(buy.entry_price + 2 * risk, abs=1e-3)
    assert "opposing 9/21" in buy.take_profit2_rule
    assert len(buy.signal_id) == 36


def test_a_valid_short_mirrors_it():
    bars = tape(SHORT_SEED)
    _, events = run(bars)
    sell = first(events, m.SELL)
    meta = sell.metadata
    assert sell.trigger_type == m.BEARISH and meta["di_minus"] > meta["di_plus"]
    assert sell.stop_loss > sell.entry_price > sell.take_profit
    assert sell.stop_loss - sell.entry_price >= meta["atr14"] - 1e-3


def test_the_confirmation_expires_on_the_bar_after_the_crossover():
    bars = tape(LONG_SEED)
    _, events = run(bars)
    k = events.index(first(events, m.BUY))
    # The confirmation candle closes back under the 9 EMA: the setup expires…
    bad = SimpleNamespace(**{**vars(bars[k]), "close": bars[k - 1].low - 0.5,
                             "low": bars[k - 1].low - 0.6})
    _, ev2 = run(bars[:k] + [bad] + bars[k + 1:k + 3])
    assert ev2[k].action == m.NONE and "setup expired" in ev2[k].reason
    # …and the bars after it can never confirm the old crossover.
    assert all(e.action not in (m.BUY, m.SELL) for e in ev2[k:])


def test_adx_and_vwap_rules_reject():
    bars = tape(LONG_SEED)
    _, events = run(bars, adx_min=99.0)
    assert not any(e.action == m.BUY for e in events)
    assert any("ADX" in e.reason and "<= 99" in e.reason for e in events)
    _, events = run(bars, vwap_cap_atr=0.0001)
    assert not any(e.action in (m.BUY, m.SELL) for e in events)
    assert any("over-extended" in e.reason for e in events)


def test_the_split_exit_books_half_at_target_one_and_trails_to_breakeven():
    eng, events = run(tape(LONG_SEED), exit_mode="split")
    [partial] = [e for e in events if e.action == m.PARTIAL_EXIT_TP1]
    assert partial.metadata["r_booked"] == pytest.approx(1.0)       # half of a 2R target
    # The runner: half the size, its stop moved to the entry (breakeven).
    assert eng.state == m.RUNNER
    assert eng.position["size"] == 0.5 and eng.position["stop"] == eng.position["entry"]
    # Variant A takes the whole position at Target 1 instead.
    _, a = run(tape(LONG_SEED), exit_mode="target")
    assert any(e.action == m.FULL_EXIT and e.reason == "target" for e in a)
    assert not any(e.action == m.PARTIAL_EXIT_TP1 for e in a)


def test_the_vwap_resets_each_market_day():
    vw = m.SessionVwap()
    assert vw.update("d1", 11, 9, 10, 100) == pytest.approx(10.0)
    assert vw.update("d1", 21, 19, 20, 100) == pytest.approx(15.0)
    assert vw.update("d2", 31, 29, 30, 100) == pytest.approx(30.0)


def test_the_backtest_runner_reports_variant_a_and_b(cfg):
    cfg.switch_market("US")
    try:
        out = m.BacktestRunner(cfg, "America/New_York").run(
            {"X": tape(LONG_SEED, start=datetime(2026, 10, 5, 13, 30, tzinfo=UTC))})
    finally:
        cfg.switch_market("IN")
    for v in ("A_full_at_tp1", "B_split_runner"):
        assert {"trades", "tp1_hit_rate", "expectancy_r", "profit_factor",
                "max_drawdown_r"} <= set(out[v]) or out[v] == {"trades": 0}


def test_it_is_an_own_plan_setup(cfg):
    assert OWN_PLAN[m.SETUP_NAME][0] == "sjk912_vwapadx"
    assert m.SETUP_NAME in cfg.get("risk.no_time_stop_setups")
    assert cfg.get("sjk912_vwapadx.vwap_cap_atr") == 2.5
