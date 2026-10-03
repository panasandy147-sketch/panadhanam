"""SJK 9-15-21 v2 — the 9 / 15 / 21 EMA Master Trading Strategy: the
engine's two trigger modes, the three-part chop filter, the per-cycle caps,
the desk's wiring (analyst, risk desk, exits) and the attributed backtest."""
from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from app.core.models import Bias, MarketContext, Quote
from app.strategies import OWN_PLAN
from app.strategies import sjk_9_15_21 as m
from tests.random_tape import tape

BREAKOUT_SEED, PULLBACK_SEED = 0, 12


def run(bars, **kw):
    eng = m.SJK91521Engine(**kw)
    return eng, [eng.on_bar_update(b) for b in bars]


def entries(sigs):
    return [s for s in sigs if s.signal_type in ("BUY", "SELL")]


def test_an_alignment_breakout_follows_the_spec():
    bars = tape(BREAKOUT_SEED)
    _, sigs = run(bars)
    s = next(x for x in entries(sigs) if x.trigger_mode == m.BREAKOUT)
    k = sigs.index(s)
    b, meta = bars[k], s.metadata
    long = s.signal_type == "BUY"
    rng = b.high - b.low
    if long:
        assert meta["ema9"] > meta["ema15"] > meta["ema21"]
        assert b.close > b.open and (b.close - b.low) >= 0.6 * rng
        swing = min(x.low for x in bars[k - 4:k + 1])
        assert s.stop_loss_price == pytest.approx(
            min(swing, meta["ema21"]) - 0.2 * meta["atr14"], abs=1e-3)
    else:
        assert meta["ema21"] > meta["ema15"] > meta["ema9"]
        assert b.close < b.open and (b.high - b.close) >= 0.6 * rng
    risk = abs(s.entry_price - s.stop_loss_price)
    assert abs(s.take_profit_price - s.entry_price) == pytest.approx(2 * risk, abs=1e-3)
    assert s.risk_reward_ratio == 2.0


def test_a_ribbon_pullback_follows_the_spec():
    bars = tape(PULLBACK_SEED)
    _, sigs = run(bars)
    s = next(x for x in entries(sigs) if x.trigger_mode == m.PULLBACK)
    b, meta = bars[sigs.index(s)], s.metadata
    if s.signal_type == "BUY":
        assert b.low <= meta["ema15"] and b.close >= meta["ema21"]
        assert b.close > meta["ema9"] and b.close > b.open
    else:
        assert b.high >= meta["ema15"] and b.close <= meta["ema21"]
        assert b.close < meta["ema9"] and b.close < b.open


def test_at_most_one_of_each_trigger_per_alignment_cycle():
    eng = m.SJK91521Engine(rr=1000.0)             # targets never hit: positions stay open...
    cycles: dict[tuple[int, str], int] = {}
    cycle_no, last = 0, None
    for b in tape(PULLBACK_SEED, n=400):
        s = eng.on_bar_update(b)
        if eng.cycle != last:
            cycle_no, last = cycle_no + 1, eng.cycle
        if s.signal_type in ("BUY", "SELL"):
            key = (cycle_no, s.trigger_mode)
            cycles[key] = cycles.get(key, 0) + 1
    assert cycles and max(cycles.values()) == 1


def test_the_chop_filter():
    slopes_up = (0.1, 0.1, 0.1)
    assert "spread compression" in m.chop_reason(1, 100.0, 99.99, 99.98, slopes_up, 1.0, 0)
    assert "slope divergence" in m.chop_reason(1, 102, 101, 100, (0.1, -0.1, 0.1), 1.0, 0)
    assert "whip-saw" in m.chop_reason(1, 102, 101, 100, slopes_up, 1.0, 2)
    assert m.chop_reason(1, 102, 101, 100, slopes_up, 1.0, 1) == ""


def test_pullback_only_never_takes_a_breakout():
    _, sigs = run(tape(BREAKOUT_SEED), triggers="pullback")
    assert all(s.trigger_mode == m.PULLBACK for s in entries(sigs))


def test_the_trade_signal_serialises():
    _, sigs = run(tape(BREAKOUT_SEED))
    d = entries(sigs)[0].to_dict()
    assert {"timestamp", "signal_type", "trigger_mode", "entry_price", "stop_loss_price",
            "take_profit_price", "risk_reward_ratio", "reason"} <= set(d)


# --------------------------------------------------------------------------- #
# The desk's wiring
# --------------------------------------------------------------------------- #
def _live(cfg):
    """The first entry the live detector reports on the India clock."""
    # Bar 198 (an alignment breakout) lands at 11:00 IST the next morning.
    bars = tape(BREAKOUT_SEED, start=datetime(2026, 10, 4, 13, 0, tzinfo=UTC))
    for k in range(30, len(bars)):
        found = m.detect_candles(bars[:k + 1], cfg, "Asia/Kolkata")
        if found:
            return bars[:k + 1], found
    raise AssertionError("no entry in the window")


def test_the_analyst_and_the_risk_desk(cfg):
    from app.agents.candlestick import CandlestickAgent
    from app.agents.risk import RiskManager
    cfg.switch_market("IN")
    bars, found = _live(cfg)
    ctx = MarketContext(symbol="RELIANCE", cycle_id="t", candles={"5m": bars},
                        quote=Quote(symbol="RELIANCE", last_price=bars[-1].close))
    ctx.indicators = {"primary": {"last_close": bars[-1].close, "atr": 0.3,
                                  "vwap": bars[-1].close, "above_vwap": True,
                                  "high": bars[-1].high, "low": bars[-1].low, "patterns": []},
                      "previous_day": {"high": 200.0, "low": 50.0, "close": 100.0},
                      "by_timeframe": {}, "sjk_9_15_21": found}
    ctx.__dict__["_reports"] = []
    report = CandlestickAgent(cfg).analyse_rules(ctx)
    assert report.extra["setup"] == m.SETUP_NAME
    assert report.invalidation_level == pytest.approx(found["stop"])
    rm = RiskManager(cfg)
    rm.cfg.settings["system"]["no_new_entry_after"] = "23:59"
    rm.cfg.settings["risk"]["reentry_cooldown_minutes"] = 0
    rm.set_capital(350_000)
    bias = Bias.BULLISH if found["direction"] == "LONG" else Bias.BEARISH
    sig = rm.evaluate(ctx, bias, [report], 0.8, [m.SETUP_NAME])
    assert sig.setup == m.SETUP_NAME
    assert sig.stop_loss == pytest.approx(found["stop"], abs=0.01)
    assert sig.risk_reward == pytest.approx(2.0, abs=0.02)


def test_it_is_an_own_plan_setup_with_no_time_stop(cfg):
    assert OWN_PLAN[m.SETUP_NAME] == ("sjk_9_15_21", 2.0)
    assert m.SETUP_NAME in cfg.get("risk.no_time_stop_setups")


def test_breakeven_only_when_switched_on(cfg, monkeypatch):
    from app.learning.outcomes import OutcomeTracker
    from app.storage import db
    monkeypatch.setattr(db, "update_position", lambda sid, **kw: None)
    row = {"id": "SIG-FAN", "symbol": "RELIANCE", "side": "BUY", "instrument_type": "EQ",
           "entry": 100.0, "stop_loss": 99.0, "target": 102.0, "quantity": 10,
           "notional": 1000.0, "total_risk": 10.0,
           "payload": json.dumps({"setup": m.SETUP_NAME})}
    t = OutcomeTracker(None, cfg)
    assert t._own_stop_step(dict(row), 101.6, None)["stop_loss"] == 99.0
    monkeypatch.setitem(cfg.settings["sjk_9_15_21"], "breakeven_r", 1.0)
    assert t._own_stop_step(dict(row), 101.2, None)["stop_loss"] == pytest.approx(100.0)


def test_the_rules_page_describes_it(cfg):
    from app.core import rules
    cfg.switch_market("IN")
    titles = [s["title"] for s in rules.build(cfg)["sections"]]
    assert any(t.startswith("Strategy: SJK 9-15-21") for t in titles)


def test_the_attributed_backtest(cfg):
    trades = m.engine_backtest(tape(BREAKOUT_SEED), cfg, "Asia/Kolkata", cost_pct=0.0,
                               window=None, square_off=None)
    assert trades and {t["kind"] for t in trades} <= {m.BREAKOUT, m.PULLBACK}
    assert all(t["stop_atr"] for t in trades)
    s = m.summary(trades)
    assert s["trades"] == len(trades)
