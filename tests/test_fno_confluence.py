"""Previous-day F&O confluence, the new reversal patterns and the 1:3 gate."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest

from app.agents import fno_confluence as fc
from app.agents.risk import RiskManager
from app.core.models import (
    AgentReport,
    Bias,
    Candle,
    MarketContext,
    OptionChain,
    OptionLeg,
    Quote,
    SignalStatus,
)
from app.indicators import patterns
from app.indicators.derivatives import analyse

TZ = "Asia/Kolkata"
TODAY = date(2026, 9, 29)


def _df(rows):
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"]).assign(volume=1000)


# --------------------------------------------------------------------------- #
# Patterns
# --------------------------------------------------------------------------- #
def test_tweezer_bottom_and_top():
    flat = [(100.0, 100.5, 99.5, 100.0)] * 5
    assert patterns.tweezer_bottom(_df(flat + [(100.0, 100.1, 99.0, 99.2),
                                               (99.2, 100.0, 99.0, 99.9)]))[0]
    assert patterns.tweezer_top(_df(flat + [(100.0, 101.0, 99.9, 100.8),
                                            (100.8, 101.0, 100.1, 100.2)]))[0]


def test_double_rejection_needs_a_real_pullback():
    rows = [(100.0, 100.6, 99.5, 100.4), (100.4, 101.0, 100.2, 100.4),
            (100.4, 100.6, 100.1, 100.3), (100.3, 100.7, 100.3, 100.6),
            (100.6, 100.8, 100.5, 100.75), (100.75, 101.0, 100.5, 100.62)]
    ok, strength, note = patterns.double_rejection_top(_df(rows))
    assert ok and "4 bars apart" in note
    assert not patterns.double_rejection_top(_df([(100.4, 101.0, 100.2, 100.4)] * 6))[0]
    mirrored = [(200 - o, 200 - lo, 200 - h, 200 - c) for o, h, lo, c in rows]
    assert patterns.double_rejection_bottom(_df(mirrored))[0]


def test_the_new_patterns_are_scanned_and_classed_as_reversals():
    assert {"tweezer_bottom", "double_rejection_bottom"} <= patterns.REVERSALS[1]
    assert {"tweezer_top", "double_rejection_top"} <= patterns.REVERSALS[-1]
    hits = patterns.scan(_df([(100.0, 100.5, 99.5, 100.0)] * 5
                             + [(100.0, 100.1, 99.0, 99.2), (99.2, 100.0, 99.0, 99.9)]),
                         ["tweezer_bottom"])
    assert [h["name"] for h in hits] == ["tweezer_bottom"] and hits[0]["direction"] == 1


# --------------------------------------------------------------------------- #
# The previous day and its open interest
# --------------------------------------------------------------------------- #
def _chain(call_change=500.0, put_change=-200.0, synthetic=False):
    legs = [OptionLeg(strike=1000, option_type="CE", oi=10000, oi_change=call_change),
            OptionLeg(strike=1000, option_type="PE", oi=8000, oi_change=put_change)]
    return OptionChain(underlying="RELIANCE", spot=1000.0, expiry="2026-10-02", legs=legs,
                       synthetic=synthetic)


def test_the_chain_read_carries_call_and_put_oi_change():
    d = analyse(_chain(), 0.5, {})
    assert (d["call_oi"], d["put_oi"], d["call_oi_change"], d["put_oi_change"]) == (
        10000, 8000, 500, -200)
    assert d["oi_known"] is True
    assert analyse(_chain(synthetic=True), 0.5, {})["oi_known"] is False


def _daily():
    start = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)
    rows = [(990, 1000, 980, 995), (995, 1010, 990, 1005), (1005, 1020, 1000, 1015)]
    return [Candle(ts=start + timedelta(days=i), open=o, high=h, low=lo, close=c)
            for i, (o, h, lo, c) in enumerate(rows)] + [
        Candle(ts=datetime(2026, 9, 29, 5, 0, tzinfo=UTC), open=1015, high=1018,
               low=1012, close=1016)]                    # today's bar: not the previous day


def test_previous_day_levels_come_from_the_last_completed_session():
    prev = fc.previous_day({"1d": _daily()}, TZ, TODAY)
    assert (prev["high"], prev["low"], prev["close"], prev["close_before"]) == (
        1020, 1000, 1015, 1005)


@pytest.mark.parametrize("price,oi,label", [
    (1, 1, "Long Buildup"), (-1, 1, "Short Buildup"), (1, -1, "Short Covering"),
    (-1, -1, "Long Unwinding"), (None, 1, "unknown")])
def test_the_buildup(price, oi, label):
    assert fc.buildup(price, oi) == label


def test_the_day_picture(cfg):
    pic = fc.picture("RELIANCE", fc.previous_day({"1d": _daily()}, TZ, TODAY),
                     analyse(_chain(), 0.5, {}))
    assert pic["pdh"] == 1020 and pic["bias"] == "Long Buildup"
    assert pic["call_oi_change"] == 500


# --------------------------------------------------------------------------- #
# The confluence gate
# --------------------------------------------------------------------------- #
def _ctx(close, lows, highs, call=500.0, put=-200.0, known=True, prev=None):
    bars = [Candle(ts=datetime(2026, 9, 29, 4, 0, tzinfo=UTC) + timedelta(minutes=5 * i),
                   open=close, high=h, low=lo, close=close)
            for i, (lo, h) in enumerate(zip(lows, highs, strict=False))]
    ctx = MarketContext(symbol="RELIANCE", cycle_id="t", candles={"5m": bars},
                        quote=Quote(symbol="RELIANCE", last_price=close))
    ctx.indicators = {
        "primary": {"last_close": close, "atr": 6.0, "high": close * 1.02,
                    "low": close * 0.98},
        "previous_day": prev or {"high": 1020.0, "low": 1000.0, "close": 1015.0,
                                 "close_before": 1005.0},
        "derivatives": {"call_oi_change": call, "put_oi_change": put, "oi_known": known},
    }
    ctx.__dict__["_reports"] = []
    return ctx


def _reports(*names):
    return [AgentReport(agent_id="candlestick", symbol="RELIANCE",
                        extra={"patterns": list(names)})]


@pytest.fixture
def on(cfg):
    cfg.settings["fno_confluence"] = {"enabled": True, "touch_atr": 0.15,
                                      "lookback_bars": 3, "when_oi_unknown": "block",
                                      "room_check": True}
    return cfg


def test_a_bullish_reversal_at_a_pdl_sweep_with_call_oi_rising_passes(on):
    ctx = _ctx(1003.0, [1004, 999.5, 1001], [1006, 1005, 1004])
    assert fc.confluence_reason(ctx, Bias.BULLISH, _reports("tweezer_bottom"), on) == ""


def test_a_bullish_reversal_away_from_the_pdl_is_refused(on):
    ctx = _ctx(1010.0, [1008, 1007, 1009], [1012, 1011, 1012])
    why = fc.confluence_reason(ctx, Bias.BULLISH, _reports("hammer"), on)
    assert "not at the previous-day low" in why


def test_a_bullish_reversal_needs_call_oi_rising(on):
    ctx = _ctx(1003.0, [1004, 999.5, 1001], [1006, 1005, 1004], call=-50)
    assert "call OI is not rising" in fc.confluence_reason(
        ctx, Bias.BULLISH, _reports("tweezer_bottom"), on)


def test_a_bearish_reversal_needs_a_pdh_test_and_put_oi_rising(on):
    at_pdh = _ctx(1017.0, [1014, 1015, 1016], [1019, 1020.5, 1018], put=300)
    assert fc.confluence_reason(at_pdh, Bias.BEARISH, _reports("tweezer_top"), on) == ""
    puts_falling = _ctx(1017.0, [1014, 1015, 1016], [1019, 1020.5, 1018], put=-300)
    assert "put OI is not rising" in fc.confluence_reason(
        puts_falling, Bias.BEARISH, _reports("double_rejection_top"), on)


def test_unknown_oi_blocks_unless_allowed(on):
    ctx = _ctx(1003.0, [1004, 999.5, 1001], [1006, 1005, 1004], known=False)
    assert "open interest is unknown" in fc.confluence_reason(
        ctx, Bias.BULLISH, _reports("tweezer_bottom"), on)
    on.settings["fno_confluence"]["when_oi_unknown"] = "allow"
    assert fc.confluence_reason(ctx, Bias.BULLISH, _reports("tweezer_bottom"), on) == ""


def test_a_trade_not_driven_by_a_reversal_is_not_filtered(on):
    ctx = _ctx(1010.0, [1008, 1007, 1009], [1012, 1011, 1012])
    assert fc.confluence_reason(ctx, Bias.BULLISH, _reports("flag"), on) == ""


# --------------------------------------------------------------------------- #
# The 1:3 gate
# --------------------------------------------------------------------------- #
def test_the_room_check(on):
    on.settings["risk"]["min_risk_reward"] = 3.0
    ctx = _ctx(1003.0, [1004, 999.5, 1001], [1006, 1005, 1004])
    # Entry 1003, stop 998: 3R = 1018 — beyond the 1020 PDH? no: 17/5 = 3.4R of room.
    assert fc.room_reason(ctx, Bias.BULLISH, 1003.0, 998.0, on) == ""
    # A wider stop: 3R needs 1024, the PDH at 1020 is only 2.1R away.
    assert "2.1R of room before the previous-day high 1,020.00" in fc.room_reason(
        ctx, Bias.BULLISH, 1003.0, 995.0, on)


@pytest.fixture
def rm(on):
    m = RiskManager(on)
    m.cfg.settings["system"]["no_new_entry_after"] = "23:59"
    m.set_capital(100_000)
    return m


def test_the_shipped_minimum_is_one_to_three():
    from app.core.config import get_config
    cfg = get_config()
    cfg.reload()
    assert float(cfg.get("risk.min_risk_reward")) == 3.0
    assert cfg.get("fno_confluence.enabled") is True


def test_the_risk_desk_refuses_a_reversal_without_the_confluence(rm):
    ctx = _ctx(1010.0, [1008, 1007, 1009], [1012, 1011, 1012])
    sig = rm.evaluate(ctx, Bias.BULLISH, _reports("tweezer_bottom"), 0.7, ["a", "b"])
    assert sig.status == SignalStatus.REJECTED
    assert any("F&O confluence" in r for r in sig.rejection_reasons)


def test_the_risk_desk_builds_a_one_to_three_target(rm):
    rm.cfg.settings["risk"]["min_risk_reward"] = 3.0
    ctx = _ctx(1003.0, [1004, 999.5, 1001], [1006, 1005, 1004],
               prev={"high": 1200.0, "low": 1000.0, "close": 1015.0})
    sig = rm.evaluate(ctx, Bias.BULLISH, _reports("tweezer_bottom"), 0.7, ["a", "b"])
    assert sig.risk_reward >= 3.0
    assert not any("F&O confluence" in r for r in sig.rejection_reasons)
