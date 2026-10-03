"""sjk912RSi v2 — the engine's long, short and rejections, the Pine Script,
and the wiring."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.strategies import OWN_PLAN
from app.strategies import sjk912rsi as m
from tests.random_tape import tape

ROOT = Path(__file__).resolve().parents[1]
LONG_SEED, SHORT_SEED = 1, 0


def run(bars, **kw):
    eng = m.SJK912_RSI_Engine(**kw)
    return [eng.on_bar_update(b) for b in bars]


def first(events, action):
    return next(e for e in events if e.action == action)


def test_a_valid_long():
    bars = tape(LONG_SEED)
    events = run(bars)
    buy = first(events, m.BUY)
    k = events.index(buy)
    b, meta = bars[k], buy.metadata
    assert b.close > meta["ema9"] and b.close > meta["ema21"] and b.low >= meta["ema21"]
    assert b.close > b.open and 50 < meta["rsi"] < 68
    assert meta["bars_after_cross"] <= 3
    raw = min(x.low for x in bars[k - 4:k + 1])                   # lowest low of 5
    assert buy.stop_loss == pytest.approx(min(raw, b.close - 0.5 * meta["atr14"]), abs=1e-3)
    risk = buy.entry_price - buy.stop_loss
    assert buy.take_profit == pytest.approx(buy.entry_price + 2 * risk, abs=1e-3)


def test_a_valid_short():
    bars = tape(SHORT_SEED)
    events = run(bars)
    sell = first(events, m.SELL)
    k = events.index(sell)
    b, meta = bars[k], sell.metadata
    assert b.close < meta["ema21"] and b.high <= meta["ema21"] and b.close < b.open
    assert 32 < meta["rsi"] < 50
    assert sell.stop_loss > sell.entry_price > sell.take_profit


def test_the_rsi_band_and_the_warmup_reject():
    bars = tape(LONG_SEED)
    assert not any(e.action == m.BUY for e in run(bars, rsi_long_max=50.01))   # band shut
    assert not any(e.action in (m.BUY, m.SELL) for e in run(bars, warmup_bars=10_000))


def test_one_trade_per_crossover_cycle():
    events = run(tape(LONG_SEED), rr=100.0)            # a target never reached
    entries = [e for e in events if e.action in (m.BUY, m.SELL)]
    bars_after = [e.metadata["bars_after_cross"] for e in entries]
    assert all(b <= 3 for b in bars_after)


def test_the_rsi_is_wilders():
    r = m.Rsi(14)
    vals = [r.update(100 + i) for i in range(16)]
    assert vals[13] is None and vals[14] == pytest.approx(100.0)


def test_the_pine_script_carries_the_v2_rules():
    pine = (ROOT / "pine" / "sjk912RSi.pine").read_text()
    for needle in ('strategy("sjk912RSi"', "process_orders_on_close = true",
                   "calc_on_every_tick = false", "slippage = 2", "default_qty_value = 100",
                   "var bool enteredOnCurrentCross = false", "rsi < rsiLongMax",
                   "ta.atr(14)", "table.new", "Profit Factor", "alertcondition(longNow",
                   "alertcondition(shortNow", "color.green", "color.red"):
        assert needle in pine, needle


def test_it_is_an_own_plan_setup(cfg):
    assert OWN_PLAN[m.SETUP_NAME] == ("sjk912rsi", 2.0)
    assert cfg.get("sjk912rsi.rsi_long_max") == 68.0 and cfg.get("sjk912rsi.cross_within") == 3
