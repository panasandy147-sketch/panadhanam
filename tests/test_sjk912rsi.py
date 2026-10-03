"""sjk912RSi — the user's 9/21 EMA + RSI crossover (5 Oct 2026): the engine's
long, short and rejections, the Pine Script's presence, and the wiring."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.strategies import OWN_PLAN
from app.strategies import sjk912rsi as m

ROOT = Path(__file__).resolve().parents[1]


def tape(kind: str = "long", red: bool = False, start=datetime(2026, 10, 5, 9, 15)):
    """Flat at 100, a fall, then a fast rise: the 9 EMA crosses the 21 on a
    green candle opening and closing above both. 'short' mirrors it; `red`
    makes every rising candle close below its open."""
    px, p = [], 100.0
    for k in range(30):
        p = 100 + 0.05 * (-1) ** k
        px.append(p)
    for _ in range(25):
        p -= 0.12
        px.append(p)
    for _ in range(25):
        p += 0.45
        px.append(p)
    if kind == "short":
        px = [200 - x for x in px]
    out = []
    for i, x in enumerate(px):
        o = px[i - 1] if i else x
        if red and i >= 55:
            o = x + 0.1 if kind == "long" else x - 0.1
        out.append(SimpleNamespace(ts=start + timedelta(minutes=5 * i), open=o,
                                   high=max(o, x) + 0.05, low=min(o, x) - 0.05, close=x))
    return out


def run(bars, **kw):
    eng = m.SJK912_RSI_Engine(**kw)
    return [eng.on_bar_update(b) for b in bars]


def entries(events):
    return [e for e in events if e.action in (m.BUY, m.SELL)]


def test_a_valid_long():
    bars = tape("long")
    events = run(bars)
    [buy] = entries(events)
    k = events.index(buy)
    b = bars[k]
    meta = buy.metadata
    assert buy.action == m.BUY and buy.trigger_type == m.BULLISH
    assert b.open > meta["ema_fast"] and b.open > meta["ema_slow"]
    assert b.close > meta["ema_fast"] and b.close > b.open and meta["rsi"] > 50
    assert buy.stop_loss == pytest.approx(min(x.low for x in bars[k - 5:k]))   # 5 bars before
    risk = buy.entry_price - buy.stop_loss
    assert buy.take_profit == pytest.approx(buy.entry_price + 2 * risk)
    [out] = [e for e in events if e.action == m.EXIT]
    assert out.reason == "target" and out.metadata["r"] == pytest.approx(2.0)


def test_a_valid_short():
    bars = tape("short")
    events = run(bars)
    [sell] = entries(events)
    k = events.index(sell)
    assert sell.action == m.SELL and sell.metadata["rsi"] < 50
    assert sell.stop_loss == pytest.approx(max(x.high for x in bars[k - 5:k]))
    assert sell.take_profit == pytest.approx(
        sell.entry_price - 2 * (sell.stop_loss - sell.entry_price))


def test_rsi_on_the_wrong_side_of_the_threshold_rejects():
    assert entries(run(tape("long"), rsi_level=99.0)) == []


def test_a_red_candle_never_confirms_a_long():
    assert entries(run(tape("long", red=True))) == []


def test_one_entry_per_crossover():
    events = run(tape("long"), rr=100.0)        # a target never reached
    assert len(entries(events)) == 1


def test_the_rsi_is_wilders():
    r = m.Rsi(14)
    vals = [r.update(100 + i) for i in range(16)]
    assert vals[13] is None and vals[14] == pytest.approx(100.0)    # only gains


def test_the_pine_script_ships_with_the_same_rules():
    pine = (ROOT / "pine" / "sjk912RSi.pine").read_text()
    for needle in ('strategy("sjk912RSi"', "process_orders_on_close = true",
                   "ta.ema(close, fastLen)", "ta.rsi(close, rsiLen)", "ta.lowest(low, swingN)[1]",
                   "color.green", "color.red", "alertcondition(longNow",
                   "alertcondition(shortNow"):
        assert needle in pine, needle


def test_it_is_an_own_plan_setup_with_its_settings(cfg):
    assert OWN_PLAN[m.SETUP_NAME] == ("sjk912rsi", 2.0)
    assert m.SETUP_NAME in cfg.get("risk.no_time_stop_setups")
    assert cfg.get("sjk912rsi.rsi_level") == 50.0


def test_detect_candles_and_the_backtest(cfg):
    cfg.switch_market("IN")
    bars = tape("long", start=datetime(2026, 10, 5, 3, 45, tzinfo=UTC))     # 09:15 IST
    events = run(bars)
    k = events.index(entries(events)[0])
    found = m.detect_candles(bars[:k + 1], cfg, "Asia/Kolkata")
    assert found and found["direction"] == "LONG" and found["setup"] == m.SETUP_NAME
    trades = m.backtest(bars, cfg, "Asia/Kolkata")
    assert len(trades) == 1 and trades[0]["outcome"] == "TARGET"
