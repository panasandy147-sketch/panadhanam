"""SJK 1 — the user's 50 / 200 EMA pullback continuation, as panadhanam
trades it: the detector, the candlestick analyst's report, the risk desk's
exact swing stop and 1:2.5 target (judged at 1:2.5), and the optional
breakeven / trail in the outcome tracker."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.core.models import Bias, Candle, Quote
from app.strategies import sjk1
from tests.sjk1_data import long_tape, mirror


def _candles(tape=None, end=datetime(2026, 9, 29, 6, 0, tzinfo=UTC)):    # 11:30 IST
    h, lo, c, _ = tape or long_tape()
    n = len(c)
    return [Candle(ts=end - timedelta(minutes=5 * (n - 1 - k)), open=c[k - 1] if k else c[k],
                   high=h[k], low=lo[k], close=c[k], volume=1000.0) for k in range(n)]


# --------------------------------------------------------------------------- #
# The detector (the same code as panaoptions')
# --------------------------------------------------------------------------- #
def test_long_short_and_the_rules_that_stop_them():
    h, lo, c, i = long_tape()
    s = sjk1.detect(h, lo, c)
    assert s["direction"] == "LONG" and s["entry"] == c[i]
    assert abs(s["target"] - (s["entry"] + 2.5 * (s["entry"] - s["stop"]))) < 1e-3
    assert sjk1.detect(h[:-1], lo[:-1], c[:-1]) is None              # not yet through
    assert sjk1.detect(h + [h[-1] + .1], lo + [lo[-1] + .1], c + [c[-1] + .1]) is None
    assert sjk1.detect(*long_tape(deep=True)[:3]) is None             # closed below 200
    assert sjk1.detect(*long_tape(shallow=True)[:3]) is None          # never at the 50
    assert sjk1.detect(*mirror(h, lo, c))["direction"] == "SHORT"


def test_only_inside_its_window_on_the_markets_clock(cfg):
    cfg.switch_market("IN")
    assert sjk1.detect_candles(_candles(), cfg, "Asia/Kolkata")["setup"] == sjk1.SETUP_NAME
    late = _candles(end=datetime(2026, 9, 29, 9, 30, tzinfo=UTC))      # 15:00 IST
    assert sjk1.detect_candles(late, cfg, "Asia/Kolkata") is None


# --------------------------------------------------------------------------- #
# The analyst and the risk desk
# --------------------------------------------------------------------------- #
def _ctx(cfg, symbol="RELIANCE"):
    from app.core.models import MarketContext
    bars = _candles()
    found = sjk1.detect_candles(bars, cfg, "Asia/Kolkata")
    ctx = MarketContext(symbol=symbol, cycle_id="t", candles={"5m": bars},
                        quote=Quote(symbol=symbol, last_price=bars[-1].close))
    ctx.indicators = {"primary": {"last_close": bars[-1].close, "atr": 0.4, "vwap": 112.0,
                                  "above_vwap": True, "high": 113.6, "low": 111.7,
                                  "patterns": []},
                      "previous_day": {"high": 113.55, "low": 108.0, "close": 112.0},
                      "by_timeframe": {}, "sjk1": found}
    ctx.__dict__["_reports"] = []
    return ctx, found


@pytest.fixture
def rm(cfg):
    from app.agents.risk import RiskManager
    cfg.switch_market("IN")
    m = RiskManager(cfg)
    m.cfg.settings["system"]["no_new_entry_after"] = "23:59"
    m.cfg.settings["risk"]["reentry_cooldown_minutes"] = 0
    m.set_capital(350_000)
    return m


def test_the_analyst_reports_it_as_a_complete_setup(cfg):
    from app.agents.candlestick import CandlestickAgent
    cfg.switch_market("IN")
    ctx, found = _ctx(cfg)
    report = CandlestickAgent(cfg).analyse_rules(ctx)
    assert report.extra["setup"] == sjk1.SETUP_NAME and report.score > 0
    assert report.invalidation_level == pytest.approx(found["stop"])


def test_the_risk_desk_takes_its_swing_stop_and_judges_it_at_one_to_two_and_a_half(rm, cfg):
    from app.agents.candlestick import CandlestickAgent
    ctx, found = _ctx(cfg)
    report = CandlestickAgent(cfg).analyse_rules(ctx)
    sig = rm.evaluate(ctx, Bias.BULLISH, [report], 0.8, [sjk1.SETUP_NAME])
    assert sig.setup == sjk1.SETUP_NAME
    assert sig.stop_loss == pytest.approx(found["stop"], abs=0.01)    # AT the swing
    assert sig.risk_reward == pytest.approx(2.5, abs=0.02)
    # 2.5 < the desk's 1:3, and still not refused for it — nor for the PDH
    # sitting just above the entry (no room check)
    assert not any("R:R" in r or "room" in r.lower() for r in sig.rejection_reasons), \
        sig.rejection_reasons


# --------------------------------------------------------------------------- #
# The outcome tracker: breakeven at 1.5R, then the trail (both optional)
# --------------------------------------------------------------------------- #
def _row(stop=99.0):
    return {"id": "SIG-SJK", "symbol": "RELIANCE", "side": "BUY", "instrument_type": "EQ",
            "entry": 100.0, "stop_loss": stop, "target": 102.5, "quantity": 10,
            "notional": 1000.0, "total_risk": 10.0,
            "payload": json.dumps({"setup": sjk1.SETUP_NAME})}


def test_breakeven_then_trail_only_when_switched_on(cfg, monkeypatch):
    from app.learning.outcomes import OutcomeTracker
    from app.storage import db
    saved = []
    monkeypatch.setattr(db, "update_position", lambda sid, **kw: saved.append(kw))
    t = OutcomeTracker(None, cfg)
    assert t._own_stop_step(_row(), 101.6, None)["stop_loss"] == 99.0  # off by default
    monkeypatch.setitem(cfg.settings["sjk1"], "breakeven_r", 1.5)
    monkeypatch.setitem(cfg.settings["sjk1"], "trail_r", 1.0)
    row = t._own_stop_step(_row(), 101.4, None)
    assert row["stop_loss"] == 99.0 and not saved                      # +1.4R
    row = t._own_stop_step(row, 101.6, None)
    assert row["stop_loss"] == pytest.approx(100.6)    # breakeven, then 1R behind 1.6R
    assert saved[-1]["stop_loss"] == pytest.approx(100.6)
    other = {**_row(), "payload": json.dumps({"setup": "Volatility Breakout"})}
    assert t._own_stop_step(other, 103.0, None)["stop_loss"] == 99.0


def test_the_rules_page_describes_it(cfg):
    from app.core import rules
    cfg.switch_market("IN")
    titles = [s["title"] for s in rules.build(cfg)["sections"]]
    assert any(t.startswith("Strategy: SJK 1") for t in titles)
