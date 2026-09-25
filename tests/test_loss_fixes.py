"""Fixes found in a morning of paper losses (3 wins, 23 losses)."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.agents.cmio import CMIOAgent
from app.agents.risk import RiskManager
from app.core.models import AgentReport, Bias, MarketContext, Quote


def test_the_time_stop_no_longer_closes_every_trade(cfg):
    """Signals carry no setup type, and the empty case fell through: the
    30-minute Mean Reversion timer closed every trade not yet green."""
    from app.learning.outcomes import OutcomeTracker

    tracker = OutcomeTracker(broker=None, cfg=cfg)
    opened = (datetime.now(UTC) - timedelta(minutes=45)).isoformat()
    plain = {"ts": opened, "side": "BUY", "entry": 100.0, "payload": json.dumps({})}
    assert tracker._time_stop_hit(plain, price=99.5) is False
    mean_rev = {**plain, "payload": json.dumps({"setup": "Mean Reversion"})}
    assert tracker._time_stop_hit(mean_rev, price=99.5) is True


@pytest.fixture
def rm(cfg):
    cfg.settings["system"]["no_new_entry_after"] = "23:59"
    m = RiskManager(cfg)
    m.set_capital(100_000)
    return m


def _ctx(symbol="NFLX", price=71.03, atr=0.40, invalidation=None):
    ctx = MarketContext(symbol=symbol, cycle_id="t",
                        quote=Quote(symbol=symbol, last_price=price),
                        indicators={"primary": {"atr": atr}})
    if invalidation:
        ctx.__dict__["_reports"] = [AgentReport(
            agent_id="candlestick", symbol=symbol, invalidation_level=invalidation)]
    return ctx


def test_a_stop_inside_normal_noise_is_widened_to_one_atr(rm):
    """NFLX 71.03 with a stop at 71.26 — 0.3%, under one bar's range."""
    sig = rm.evaluate(_ctx(invalidation=71.24), Bias.BEARISH, [], 0.6, ["candlestick"])
    assert sig.stop_loss == pytest.approx(71.43, abs=0.01)     # entry + 1 x 0.40
    assert "widened to 1x ATR" in sig.rationale


def test_a_symbol_already_held_is_not_opened_again(rm, monkeypatch):
    from app.storage import db

    monkeypatch.setattr(db, "open_signals", lambda: [{"symbol": "LLY"}])
    monkeypatch.setattr(db, "last_exit", lambda symbol: None)
    assert any("one position per symbol" in r for r in rm.symbol_checks("LLY"))
    assert rm.symbol_checks("SPY") == []


def test_a_stopped_out_symbol_waits_out_the_cooldown(rm, monkeypatch):
    from app.storage import db

    monkeypatch.setattr(db, "open_signals", lambda: [])
    monkeypatch.setattr(db, "last_exit", lambda s: (datetime.now() - timedelta(minutes=10)).isoformat())
    assert any("waiting 30 min" in r for r in rm.symbol_checks("NFLX"))
    monkeypatch.setattr(db, "last_exit", lambda s: (datetime.now() - timedelta(minutes=45)).isoformat())
    assert rm.symbol_checks("NFLX") == []


def _vote(cfg, score, primary, tf15):
    ctx = MarketContext(symbol="X", cycle_id="t",
                        indicators={"primary": primary, "by_timeframe": {"15m": tf15}})
    bias = Bias.BULLISH if score > 0 else Bias.BEARISH
    return CMIOAgent(cfg)._weighted_vote(ctx, [AgentReport(
        agent_id="candlestick", symbol="X", bias=bias, score=score, confidence=0.9)])


def test_no_short_above_vwap(cfg):
    d = _vote(cfg, -0.6, {"above_vwap": True}, {})
    assert d["proceed"] is False and "short above VWAP" in d["rationale"]


def test_no_long_into_a_falling_15m_trend(cfg):
    d = _vote(cfg, 0.6, {"above_vwap": True}, {"ema_stacked_bear": True})
    assert d["proceed"] is False and "falling 15-minute trend" in d["rationale"]


def test_a_trade_with_the_trend_goes_through(cfg):
    d = _vote(cfg, 0.6, {"above_vwap": True}, {"ema_stacked_bull": True})
    assert d["proceed"] is True
