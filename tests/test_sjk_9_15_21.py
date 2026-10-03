"""SJK 9-15-21 — the user's 9 / 15 / 21 EMA Master Strategy (5 Oct 2026), as
panadhanam trades it: the detector, the session window, the candlestick
analyst's report, the risk desk's own stop and 1:2 target (judged at 1:2),
the optional breakeven / trail, and the strategy's own backtest."""
from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta

import pytest

from app.core.models import Bias, Candle, MarketContext, Quote
from app.strategies import OWN_PLAN
from app.strategies import sjk_9_15_21 as m


def tape(kind: str = "up", n: int = 140, flat: int = 80):
    """`flat` bars of sideways noise, then a clean trend ('up' / 'down'), or
    noise throughout ('chop'). Returns highs, lows, closes."""
    closes, price = [], 100.0
    for k in range(n):
        if k < flat or kind == "chop":
            price = 100.0 + 0.15 * math.sin(k * 1.3)
        else:
            price += 0.12 if kind == "up" else -0.12
        closes.append(round(price, 4))
    return [c + 0.05 for c in closes], [c - 0.05 for c in closes], closes


def first_signal(h, lo, c, **kw):
    for i in range(30, len(c) + 1):
        found = m.detect(h[:i], lo[:i], c[:i], **kw)
        if found:
            return i - 1, found
    return None, None


def candles(h, lo, c, end=datetime(2026, 10, 5, 6, 0, tzinfo=UTC)):     # 11:30 IST
    n = len(c)
    return [Candle(ts=end - timedelta(minutes=5 * (n - 1 - k)), open=c[k - 1] if k else c[k],
                   high=h[k], low=lo[k], close=c[k], volume=1000.0) for k in range(n)]


# --------------------------------------------------------------------------- #
# The detector (the same code as panaoptions')
# --------------------------------------------------------------------------- #
def test_a_clean_up_fan_buys_once_and_a_down_fan_sells():
    h, lo, c = tape("up")
    i, found = first_signal(h, lo, c)
    assert found["direction"] == "LONG" and found["kind"] == "alignment confirmed"
    assert found["ema_fast"] > found["ema_mid"] > found["ema_slow"]
    risk = found["entry"] - found["stop"]
    assert found["target"] == pytest.approx(found["entry"] + 2 * risk, abs=1e-3)
    assert [k for k in range(i + 2, len(c) + 1) if m.detect(h[:k], lo[:k], c[:k])] == []
    _, short = first_signal(*tape("down"))
    assert short["direction"] == "SHORT" and short["stop"] > short["entry"] > short["target"]


def test_sideways_chop_never_trades():
    h, lo, c = tape("chop")
    assert first_signal(h, lo, c) == (None, None)
    assert m.market_state(h, lo, c)[0] == m.SIDEWAYS


def test_rr_and_stop_mode_are_settings():
    h, lo, c = tape("up")
    _, three = first_signal(h, lo, c, rr=3.0)
    assert three["target"] == pytest.approx(three["entry"] + 3 * (three["entry"] - three["stop"]),
                                            abs=1e-3)
    _, ema = first_signal(h, lo, c, stop_mode="ema21")
    assert ema["stop"] == pytest.approx(ema["ema_slow"], abs=1e-3)


# --------------------------------------------------------------------------- #
# The window and the analyst
# --------------------------------------------------------------------------- #
def _fan_candles():
    h, lo, c = tape("up")
    i, found = first_signal(h, lo, c)
    return candles(h[:i + 1], lo[:i + 1], c[:i + 1]), found


def test_only_inside_its_window_on_the_markets_clock(cfg):
    cfg.switch_market("IN")
    bars, found = _fan_candles()
    got = m.detect_candles(bars, cfg, "Asia/Kolkata")
    assert got["setup"] == m.SETUP_NAME and got["stop"] == found["stop"]
    h, lo, c = tape("up")
    i, _ = first_signal(h, lo, c)
    late = candles(h[:i + 1], lo[:i + 1], c[:i + 1],
                   end=datetime(2026, 10, 5, 9, 45, tzinfo=UTC))             # 15:15 IST
    assert m.detect_candles(late, cfg, "Asia/Kolkata") is None


def _ctx(cfg, symbol="RELIANCE"):
    bars, _ = _fan_candles()
    found = m.detect_candles(bars, cfg, "Asia/Kolkata")
    ctx = MarketContext(symbol=symbol, cycle_id="t", candles={"5m": bars},
                        quote=Quote(symbol=symbol, last_price=bars[-1].close))
    ctx.indicators = {"primary": {"last_close": bars[-1].close, "atr": 0.4,
                                  "vwap": bars[-1].close - 1, "above_vwap": True,
                                  "high": bars[-1].high, "low": bars[-1].low, "patterns": []},
                      "previous_day": {"high": 120.0, "low": 95.0, "close": 100.0},
                      "by_timeframe": {}, "sjk_9_15_21": found}
    ctx.__dict__["_reports"] = []
    return ctx, found


def test_the_analyst_reports_it_as_a_complete_setup(cfg):
    from app.agents.candlestick import CandlestickAgent
    cfg.switch_market("IN")
    ctx, found = _ctx(cfg)
    report = CandlestickAgent(cfg).analyse_rules(ctx)
    assert report.extra["setup"] == m.SETUP_NAME and report.score > 0
    assert report.invalidation_level == pytest.approx(found["stop"])


def test_the_risk_desk_takes_its_own_stop_and_judges_it_at_one_to_two(cfg):
    from app.agents.candlestick import CandlestickAgent
    from app.agents.risk import RiskManager
    cfg.switch_market("IN")
    rm = RiskManager(cfg)
    rm.cfg.settings["system"]["no_new_entry_after"] = "23:59"
    rm.cfg.settings["risk"]["reentry_cooldown_minutes"] = 0
    rm.set_capital(350_000)
    ctx, found = _ctx(cfg)
    report = CandlestickAgent(cfg).analyse_rules(ctx)
    sig = rm.evaluate(ctx, Bias.BULLISH, [report], 0.8, [m.SETUP_NAME])
    assert sig.setup == m.SETUP_NAME
    assert sig.stop_loss == pytest.approx(found["stop"], abs=0.01)
    assert sig.risk_reward == pytest.approx(2.0, abs=0.02)
    assert not any("R:R" in r or "room" in r.lower() for r in sig.rejection_reasons), \
        sig.rejection_reasons


def test_it_is_an_own_plan_setup_with_no_time_stop(cfg):
    assert OWN_PLAN[m.SETUP_NAME] == ("sjk_9_15_21", 2.0)
    assert m.SETUP_NAME in cfg.get("risk.no_time_stop_setups")


def test_breakeven_then_trail_only_when_switched_on(cfg, monkeypatch):
    from app.learning.outcomes import OutcomeTracker
    from app.storage import db
    monkeypatch.setattr(db, "update_position", lambda sid, **kw: None)
    row = {"id": "SIG-FAN", "symbol": "RELIANCE", "side": "BUY", "instrument_type": "EQ",
           "entry": 100.0, "stop_loss": 99.0, "target": 102.0, "quantity": 10,
           "notional": 1000.0, "total_risk": 10.0,
           "payload": json.dumps({"setup": m.SETUP_NAME})}
    t = OutcomeTracker(None, cfg)
    assert t._own_stop_step(dict(row), 101.6, None)["stop_loss"] == 99.0   # off by default
    monkeypatch.setitem(cfg.settings["sjk_9_15_21"], "breakeven_r", 1.0)
    assert t._own_stop_step(dict(row), 101.2, None)["stop_loss"] == pytest.approx(100.0)


def test_the_rules_page_describes_it(cfg):
    from app.core import rules
    cfg.switch_market("IN")
    titles = [s["title"] for s in rules.build(cfg)["sections"]]
    assert any(t.startswith("Strategy: SJK 9-15-21") for t in titles)


# --------------------------------------------------------------------------- #
# The backtest
# --------------------------------------------------------------------------- #
def test_the_backtest_takes_one_trade_per_fan_and_grades_it(cfg):
    cfg.switch_market("IN")
    h, lo, c = tape("up", n=160)
    i, _ = first_signal(h, lo, c)
    # The confirming bar at 10:25 IST, the trend running on after it.
    end = datetime(2026, 10, 5, 4, 55, tzinfo=UTC) + timedelta(minutes=5 * (len(c) - 1 - i))
    bars = candles(h, lo, c, end=end)
    trades = m.backtest(bars, cfg, "Asia/Kolkata")
    assert len(trades) == 1
    [t] = trades
    assert t["direction"] == "LONG" and t["outcome"] in ("TARGET", "STOP", "SQUARE_OFF")
    if t["outcome"] == "TARGET":
        assert t["r"] == pytest.approx(2.0, abs=0.01)
    s = m.summary(trades)
    assert s["trades"] == 1 and "avg_r" in s
    assert m.summary([]) == {"trades": 0}
