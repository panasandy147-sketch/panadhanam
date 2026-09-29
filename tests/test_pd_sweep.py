"""The Previous Day Liquidity Sweep (failed breakout), US and India.

  * a 5m/15m candle pierces the PDH/PDL and closes back inside the range;
  * only a Shooting Star / Bearish Engulfing approves a PDH sweep (short),
    only a Hammer / Bullish Engulfing a PDL sweep (long); scored 1.0;
  * stop exactly 2 ticks beyond the wick, target VWAP or 3R (whichever is
    further), and no time stop.
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest

from app.agents import consensus
from app.agents.candlestick import CandlestickAgent
from app.agents.cmio import CMIOAgent
from app.agents.risk import RiskManager
from app.core.models import AgentReport, Bias, Candle, MarketContext, Quote, SignalStatus
from app.strategies import pd_sweep

TZ = "Asia/Kolkata"
TODAY = date(2026, 9, 29)
PREV = {"high": 1020.0, "low": 1000.0, "close": 1015.0}


def _bars(rows, tf_minutes=5, start=datetime(2026, 9, 29, 4, 0, tzinfo=UTC)):
    """Today's bars (IST 09:30 on); the LAST row is the candle being judged."""
    return [Candle(ts=start + timedelta(minutes=tf_minutes * i), open=o, high=h, low=lo,
                   close=c, volume=1000) for i, (o, h, lo, c) in enumerate(rows)]


LEAD = [(1010.0, 1012.0, 1008.0, 1011.0)] * 8
# Shooting star above the PDH: pierces 1020 (wick 1024), body small, closes 1016.5.
SHOOTING_STAR = LEAD + [(1016.0, 1024.0, 1015.8, 1016.5)]
# Hammer below the PDL: pierces 1000 (wick 996), closes back at 1003.5.
HAMMER = LEAD + [(1003.0, 1003.8, 996.0, 1003.5)]


# --------------------------------------------------------------------------- #
# 1. Detection
# --------------------------------------------------------------------------- #
def test_a_pdh_sweep_with_a_shooting_star_is_a_confirmed_short():
    s = pd_sweep.detect({"5m": _bars(SHOOTING_STAR)}, PREV, TZ, TODAY)
    assert s.side == "PDH" and s.direction == -1 and s.confirmed
    assert s.pattern == "shooting_star" and s.wick == 1024.0 and s.timeframe == "5m"
    assert "closed back inside" in s.note


def test_a_pdl_sweep_with_a_hammer_is_a_confirmed_long():
    s = pd_sweep.detect({"5m": _bars(HAMMER)}, PREV, TZ, TODAY)
    assert s.side == "PDL" and s.direction == 1 and s.confirmed and s.wick == 996.0


def test_a_bullish_engulfing_confirms_a_pdl_sweep():
    rows = LEAD + [(1004.0, 1004.5, 1001.0, 1001.5), (1001.0, 1006.0, 998.0, 1005.5)]
    s = pd_sweep.detect({"5m": _bars(rows)}, PREV, TZ, TODAY)
    assert s.confirmed and s.pattern == "bullish_engulfing"


def test_a_close_beyond_the_level_is_a_breakout_not_a_sweep():
    rows = LEAD + [(1016.0, 1024.0, 1015.8, 1022.0)]            # closed above the PDH
    assert pd_sweep.detect({"5m": _bars(rows)}, PREV, TZ, TODAY) is None


def test_a_sweep_without_the_reversal_candle_is_not_approved():
    rows = LEAD + [(1016.0, 1021.0, 1012.0, 1018.0)]            # green, no shooting star
    s = pd_sweep.detect({"5m": _bars(rows)}, PREV, TZ, TODAY)
    assert s is not None and not s.confirmed and "not approved" in s.note


def test_the_15_minute_candle_is_checked_when_the_5_minute_one_is_not_a_sweep():
    s = pd_sweep.detect({"5m": _bars(LEAD), "15m": _bars(SHOOTING_STAR, 15)},
                        PREV, TZ, TODAY)
    assert s.confirmed and s.timeframe == "15m"


def test_yesterdays_candle_is_not_todays_sweep():
    old = _bars(SHOOTING_STAR, start=datetime(2026, 9, 28, 4, 0, tzinfo=UTC))
    assert pd_sweep.detect({"5m": old}, PREV, TZ, TODAY) is None


# --------------------------------------------------------------------------- #
# 2. The candlestick analyst
# --------------------------------------------------------------------------- #
def _ctx(rows, price=None, vwap=1012.0, atr=4.0, market_symbol="RELIANCE", tick=None):
    bars = _bars(rows)
    close = price if price is not None else bars[-1].close
    ctx = MarketContext(symbol=market_symbol, cycle_id="t", candles={"5m": bars},
                        quote=Quote(symbol=market_symbol, last_price=close))
    sweep = pd_sweep.detect({"5m": bars}, PREV, TZ, TODAY)
    ctx.indicators = {
        "primary": {"last_close": close, "atr": atr, "vwap": vwap,
                    "above_vwap": close > vwap, "high": close * 1.01, "low": close * 0.99,
                    "patterns": []},
        "previous_day": PREV, "pd_sweep": sweep.to_dict() if sweep else None,
        "by_timeframe": {},
    }
    ctx.__dict__["_reports"] = []
    return ctx


def test_a_confirmed_sweep_scores_one(cfg):
    report = CandlestickAgent(cfg).analyse_rules(_ctx(SHOOTING_STAR))
    assert report.score == -1.0 and report.bias == Bias.BEARISH
    assert report.extra["setup"] == "PD Liquidity Sweep"
    assert report.invalidation_level == 1024.0
    long = CandlestickAgent(cfg).analyse_rules(_ctx(HAMMER))
    assert long.score == 1.0 and long.invalidation_level == 996.0


def test_an_unconfirmed_sweep_gets_no_setup(cfg):
    rows = LEAD + [(1016.0, 1021.0, 1012.0, 1018.0)]
    report = CandlestickAgent(cfg).analyse_rules(_ctx(rows))
    assert (report.extra or {}).get("setup") is None and abs(report.score) < 1.0
    assert any("not approved" in e.value for e in report.evidence)


# --------------------------------------------------------------------------- #
# 3. The risk desk
# --------------------------------------------------------------------------- #
@pytest.fixture
def rm(cfg):
    m = RiskManager(cfg)
    m.cfg.settings["system"]["no_new_entry_after"] = "23:59"
    # Other tests close trades on these names; the re-entry cooldown has its
    # own tests and must not make this file depend on the order it runs in.
    m.cfg.settings["risk"]["reentry_cooldown_minutes"] = 0
    m.set_capital(100_000)
    return m


def test_the_short_stop_is_two_ticks_beyond_the_wick_and_the_target_is_3r(rm, cfg):
    ctx = _ctx(SHOOTING_STAR, vwap=1014.0)                  # VWAP only 0.3R away
    reports = [CandlestickAgent(cfg).analyse_rules(ctx)]
    sig = rm.evaluate(ctx, Bias.BEARISH, reports, 0.8, ["a", "b"])
    tick = float(cfg.instrument_meta("RELIANCE").get("tick_size") or 0.05)
    assert sig.stop_loss == pytest.approx(1024.0 + 2 * tick)
    risk = sig.stop_loss - sig.entry
    assert sig.target == pytest.approx(sig.entry - 3 * risk, abs=0.02)
    assert sig.setup == "PD Liquidity Sweep"
    assert not any("too tight" in r for r in sig.rejection_reasons)


def test_the_target_is_vwap_when_vwap_is_further_than_3r(rm, cfg):
    # Hammer at 1003.5, stop 996 - 2 ticks: risk ~7.6; 3R ~ 1026; VWAP 1030.
    prev_room = dict(PREV, high=1060.0)
    ctx = _ctx(HAMMER, vwap=1030.0)
    ctx.indicators["previous_day"] = prev_room
    reports = [CandlestickAgent(cfg).analyse_rules(ctx)]
    sig = rm.evaluate(ctx, Bias.BULLISH, reports, 0.8, ["a", "b"])
    assert sig.target == pytest.approx(1030.0)
    assert sig.risk_reward >= 3.0


def test_a_sweep_in_the_us_uses_the_us_tick(rm, cfg):
    cfg.switch_market("US")
    try:
        ctx = _ctx(SHOOTING_STAR, market_symbol="AAPL")
        reports = [CandlestickAgent(cfg).analyse_rules(ctx)]
        sig = rm.evaluate(ctx, Bias.BEARISH, reports, 0.8, ["a", "b"])
        tick = float(cfg.instrument_meta("AAPL").get("tick_size") or 0.01)
        assert sig.stop_loss == pytest.approx(1024.0 + 2 * tick)
    finally:
        cfg.switch_market("IN")


def test_an_ordinary_trade_keeps_the_ordinary_stop(rm, cfg):
    ctx = _ctx(LEAD + [(1010.0, 1012.0, 1008.0, 1011.0)])
    ctx.indicators["pd_sweep"] = None
    sig = rm.evaluate(ctx, Bias.BULLISH, [], 0.6, ["a", "b"])
    assert sig.setup == ""


def test_a_sweep_is_never_time_stopped(cfg):
    from app.learning.outcomes import OutcomeTracker
    cfg.settings["risk"]["time_stop_minutes"] = 30
    cfg.settings["risk"]["time_stop_setups"] = ["Mean Reversion", "PD Liquidity Sweep"]
    tracker = OutcomeTracker.__new__(OutcomeTracker)
    tracker.cfg = cfg
    row = {"ts": (datetime.now(UTC) - timedelta(hours=2)).isoformat(), "entry": 10.0,
           "side": "BUY", "payload": json.dumps({"setup": "PD Liquidity Sweep"})}
    assert tracker._time_stop_hit(row, 9.9) is False


# --------------------------------------------------------------------------- #
# The committee
# --------------------------------------------------------------------------- #
def _r(agent, score, **kw):
    bias = Bias.BULLISH if score > 0 else Bias.BEARISH if score < 0 else Bias.NEUTRAL
    return AgentReport(agent_id=agent, symbol="RELIANCE", bias=bias, score=score,
                       confidence=0.8, **kw)


def test_the_trend_filter_stands_aside_for_a_sweep(cfg):
    ctx = _ctx(SHOOTING_STAR, vwap=1012.0)                  # a short ABOVE VWAP
    sweep = CandlestickAgent(cfg).analyse_rules(ctx)
    assert consensus.sweep_backs([sweep], -0.5)
    decision = CMIOAgent(cfg)._weighted_vote(ctx, [sweep, _r("derivatives", -0.3)])
    assert decision["proceed"] is True, decision["rationale"]
    assert any("liquidity sweep" in c for c in decision["conflicts"])
    # The same short without the sweep is refused as counter-trend.
    plain = CMIOAgent(cfg)._weighted_vote(
        ctx, [_r("candlestick", -0.6), _r("derivatives", -0.3)])
    assert plain["proceed"] is False


def test_the_desk_refuses_nothing_on_oi_for_a_sweep_by_default(cfg):
    from app.agents import fno_confluence
    ctx = _ctx(SHOOTING_STAR)
    ctx.indicators["derivatives"] = {"oi_known": False}
    sweep = CandlestickAgent(cfg).analyse_rules(ctx)
    assert fno_confluence.confluence_reason(ctx, Bias.BEARISH, [sweep], cfg) == ""
    cfg.settings["pd_sweep"]["require_rising_oi"] = True
    assert "open interest is unknown" in fno_confluence.confluence_reason(
        ctx, Bias.BEARISH, [sweep], cfg)
    cfg.settings["pd_sweep"]["require_rising_oi"] = False


def test_a_sweep_signal_is_approved_end_to_end(rm, cfg):
    ctx = _ctx(SHOOTING_STAR)
    sweep = CandlestickAgent(cfg).analyse_rules(ctx)
    sig = rm.evaluate(ctx, Bias.BEARISH, [sweep], 0.8, ["PD Liquidity Sweep", "VP"])
    assert sig.status == SignalStatus.APPROVED, sig.rejection_reasons
