"""SJK 9/21 · VWAP · ADX (sjk912_vwapadx) — the engine's mock driver: a valid
long, a valid short, and the rejections (ADX <= threshold, VWAP violated,
no confirmation, chop), then the desk's wiring and the backtest."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.strategies import OWN_PLAN
from app.strategies import sjk912_vwapadx as m


def tape(kind: str = "long", rise: float = 0.45, start=datetime(2026, 10, 5, 9, 15)):
    """30 flat bars at 100 (the VWAP's anchor), 25 falling, 25 rising fast:
    the 9 EMA crosses the 21 EMA above VWAP with a strong ADX. 'short' is the
    mirror image."""
    px, p = [], 100.0
    for k in range(30):
        p = 100 + 0.05 * (-1) ** k
        px.append(p)
    for _ in range(25):
        p -= 0.12
        px.append(p)
    for _ in range(25):
        p += rise
        px.append(p)
    if kind == "short":
        px = [200 - x for x in px]
    return [SimpleNamespace(ts=start + timedelta(minutes=5 * i), open=x, high=x + 0.08,
                            low=x - 0.08, close=x, volume=1000.0) for i, x in enumerate(px)]


def run(bars, **kw):
    eng = m.SJK912_VWAP_ADX_Engine(**kw)
    return eng, [eng.on_bar_update(b) for b in bars]


def entries(events):
    return [e for e in events if e.action in (m.BUY, m.SELL)]


# --------------------------------------------------------------------------- #
# 1. A valid long
# --------------------------------------------------------------------------- #
def test_a_valid_long_enters_on_the_confirmation_close_with_its_stop_and_target():
    bars = tape("long")
    _, events = run(bars)
    [buy] = entries(events)
    k = events.index(buy)
    cross = bars[k - 1]                                     # the Crossover Candle
    assert "crossover candle" in events[k - 1].reason      # never entered on it
    assert buy.action == m.BUY and buy.trigger_type == m.BULLISH
    assert buy.entry_price == pytest.approx(bars[k].close)              # confirmation close
    assert buy.stop_loss == pytest.approx(cross.low)                    # crossover LOW
    risk = buy.entry_price - buy.stop_loss
    assert buy.take_profit == pytest.approx(buy.entry_price + 2 * risk)  # 1:2
    meta = buy.metadata
    assert meta["adx"] > 20 and buy.entry_price > meta["vwap"]
    assert buy.entry_price > meta["ema_fast"] > meta["ema_slow"]
    assert len(buy.signal_id) == 36                                     # a GUID
    # ...and it is managed: out at the 1:2 target (+2R).
    [out] = [e for e in events if e.action == m.EXIT]
    assert out.reason == "target" and out.metadata["r"] == pytest.approx(2.0)


# --------------------------------------------------------------------------- #
# 2. A valid short
# --------------------------------------------------------------------------- #
def test_a_valid_short_mirrors_it():
    bars = tape("short")
    _, events = run(bars)
    [sell] = entries(events)
    k = events.index(sell)
    assert sell.action == m.SELL and sell.trigger_type == m.BEARISH
    assert sell.stop_loss == pytest.approx(bars[k - 1].high)            # crossover HIGH
    risk = sell.stop_loss - sell.entry_price
    assert sell.take_profit == pytest.approx(sell.entry_price - 2 * risk)
    assert sell.entry_price < sell.metadata["vwap"]


# --------------------------------------------------------------------------- #
# 3. The rejections
# --------------------------------------------------------------------------- #
def test_adx_at_or_below_the_threshold_suppresses_the_entry():
    _, events = run(tape("long"), adx_min=60.0)       # this tape's ADX is ~48
    assert entries(events) == []
    assert any("ADX" in e.reason and "<= 60" in e.reason for e in events)


def test_a_crossover_on_the_wrong_side_of_vwap_is_ignored():
    _, events = run(tape("long", rise=0.15))          # crosses back while below VWAP
    assert entries(events) == []
    assert any("below VWAP — ignored" in e.reason for e in events)


def test_no_confirmation_no_trade():
    bars = tape("long")
    _, events = run(bars)
    k = next(i for i, e in enumerate(events) if "crossover candle" in e.reason)
    # The candle after the crossover closes back under the 9 EMA.
    bars[k + 1] = SimpleNamespace(**{**vars(bars[k + 1]), "close": bars[k].close - 0.6,
                                     "low": bars[k].close - 0.7})
    _, events = run(bars[:k + 2])
    assert entries(events) == []
    assert "not confirmed" in events[-1].reason


def test_a_compressed_band_is_sideways():
    _, events = run(tape("long"), band_pct=5.0)        # everything is "compressed"
    assert entries(events) == []
    assert any(m.SIDEWAYS in e.reason and "compressed" in e.reason for e in events)


def test_the_opposing_crossover_exit():
    bars = tape("long")
    # After the long, price rolls over hard: the bearish crossover exits it.
    last = bars[-1].close
    t = bars[-1].ts
    for i in range(1, 30):
        x = last - 0.5 * i
        bars.append(SimpleNamespace(ts=t + timedelta(minutes=5 * i), open=x, high=x + 0.08,
                                    low=x - 0.08, close=x, volume=1000.0))
    _, events = run(bars, exit_mode="cross", rr=50.0)
    [out] = [e for e in events if e.action == m.EXIT]
    assert out.reason in ("opposing crossover", "stop")


def test_the_vwap_resets_each_market_day():
    vw = m.SessionVwap()
    assert vw.update("d1", 11, 9, 10, 100) == pytest.approx(10.0)
    assert vw.update("d1", 21, 19, 20, 100) == pytest.approx(15.0)
    assert vw.update("d2", 31, 29, 30, 100) == pytest.approx(30.0)   # new session
    assert vw.update("d3", 31, 29, 30, 0) == pytest.approx(30.0)     # no volume: equal weights


def test_the_signal_event_serialises():
    ev = m.SignalEvent(timestamp=datetime(2026, 10, 5, 10, 0, tzinfo=UTC), action=m.BUY)
    d = ev.to_dict()
    assert d["action"] == "BUY" and d["timestamp"].startswith("2026-10-05T10:00")


# --------------------------------------------------------------------------- #
# The desk's wiring
# --------------------------------------------------------------------------- #
def test_it_is_an_own_plan_setup_with_its_settings(cfg):
    assert OWN_PLAN[m.SETUP_NAME][0] == "sjk912_vwapadx"
    assert m.SETUP_NAME in cfg.get("risk.no_time_stop_setups")
    assert cfg.get("sjk912_vwapadx.adx_min") == 20.0


def test_detect_candles_reports_the_entry_on_the_latest_bar(cfg):
    cfg.switch_market("IN")
    bars = tape("long", start=datetime(2026, 10, 5, 3, 45, tzinfo=UTC))   # 09:15 IST
    _, events = run(bars)
    k = events.index(entries(events)[0])
    found = m.detect_candles(bars[:k + 1], cfg, "Asia/Kolkata")
    assert found and found["direction"] == "LONG" and found["setup"] == m.SETUP_NAME
    assert m.detect_candles(bars[:k + 2], cfg, "Asia/Kolkata") is None   # the bar after


def test_the_backtest_grades_the_trade(cfg):
    cfg.switch_market("IN")
    bars = tape("long", start=datetime(2026, 10, 5, 3, 45, tzinfo=UTC))
    trades = m.backtest(bars, cfg, "Asia/Kolkata")
    assert len(trades) == 1 and trades[0]["outcome"] == "TARGET"
    assert trades[0]["r"] == pytest.approx(float(cfg.get("sjk912_vwapadx.rr")), abs=0.01)
