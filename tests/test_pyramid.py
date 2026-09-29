"""The Standard Pyramid (Base-50-25) and the no-averaging-down mandate."""
from __future__ import annotations

import json

import pytest

from app.agents import pyramid
from app.agents.risk import RiskManager
from app.core.models import Instrument, Quote, Side, SignalStatus, TradeSignal

ROW = {"id": "S1", "symbol": "RELIANCE", "side": "BUY", "entry": 100.0, "stop_loss": 99.0,
       "target": 104.0, "quantity": 100, "entry_spot": 0.0}


@pytest.fixture
def pcfg(cfg):
    cfg.settings["risk"]["pyramid"] = {"enabled": True, "target_r": 3.0,
                                       "levels": pyramid.DEFAULT_LEVELS}
    cfg.settings["system"]["no_new_entry_after"] = "23:59"
    cfg.settings["system"]["square_off_time"] = "23:59"
    return cfg


# --------------------------------------------------------------------------- #
# The arithmetic
# --------------------------------------------------------------------------- #
def test_base_50_25_with_the_stop_stepping_up_behind_it(pcfg):
    st = pyramid.start(pcfg, ROW)
    assert (st.r_unit, st.target, st.level) == (1.0, 103.0, 0)       # TP at +3R
    assert pyramid.plan(pcfg, st, 100.95, None, True) is None          # not yet +1R

    one = pyramid.plan(pcfg, st, 101.0, None, True)                    # +1.0R
    assert (one.level, one.quantity, one.new_stop) == (1, 50, 100.0)   # 50%, stop -> base
    assert one.avg_entry == pytest.approx((100 * 100 + 50 * 101) / 150, abs=1e-4)
    st = pyramid.apply(st, one)
    # Worst case now: the whole 150 stopped at the base entry = -0.5R.
    assert (100.0 - st.avg_entry) * st.qty == pytest.approx(-50, abs=0.05)

    assert pyramid.plan(pcfg, st, 101.9, None, True) is None
    two = pyramid.plan(pcfg, st, 102.0, None, True)                    # +2.0R
    assert (two.level, two.quantity, two.new_stop) == (2, 25, 101.0)   # 25%, stop -> L1 fill
    st = pyramid.apply(st, two)
    assert st.qty == 175                                               # the 175% position
    # Stopped at the Level 1 price: +0.75R locked in.
    assert (101.0 - st.avg_entry) * st.qty == pytest.approx(75, abs=0.05)
    # The single target at +3R from the base closes all 175.
    pnl = (st.target - st.avg_entry) * st.qty
    assert pyramid.base_r_multiple(st, pnl) == pytest.approx(4.25, abs=0.001)
    assert pyramid.plan(pcfg, st, 102.9, None, True) is None           # no Level 3


def test_a_short_pyramids_downward(pcfg):
    st = pyramid.start(pcfg, {**ROW, "side": "SELL", "stop_loss": 101.0})
    assert st.target == 97.0
    one = pyramid.plan(pcfg, st, 99.0, None, False)
    assert (one.quantity, one.new_stop) == (50, 100.0)


def test_an_add_smaller_than_a_lot_still_moves_the_stop(pcfg):
    st = pyramid.start(pcfg, {**ROW, "quantity": 75})
    one = pyramid.plan(pcfg, st, 101.0, None, True, unit=75)           # 50% of 1 lot
    assert one.quantity == 0 and one.new_stop == 100.0
    assert "rounds to 0" in one.note


# --------------------------------------------------------------------------- #
# No averaging down
# --------------------------------------------------------------------------- #
def test_the_risk_desk_refuses_an_add_in_a_drawdown(pcfg):
    rm = RiskManager(pcfg)
    assert any("No averaging down" in r for r in rm.approve_add("RELIANCE", -120.0, 5_000))
    assert rm.approve_add("RELIANCE", 250.0, 5_000) == []


def test_a_new_order_on_a_losing_held_symbol_is_refused(pcfg, monkeypatch):
    from app.storage import db
    rm = RiskManager(pcfg)
    monkeypatch.setattr(db, "open_signals", lambda: [{"symbol": "RELIANCE"}])
    monkeypatch.setattr(db, "last_exit", lambda s: None)
    rm.position_pnl["RELIANCE"] = -80.0
    assert any("No averaging down" in r for r in rm.symbol_checks("RELIANCE"))
    rm.position_pnl["RELIANCE"] = 80.0
    assert not any("No averaging down" in r for r in rm.symbol_checks("RELIANCE"))


# --------------------------------------------------------------------------- #
# End to end through the outcome tracker
# --------------------------------------------------------------------------- #
class _Broker:
    name = "fake"
    is_paper_account = True

    def __init__(self):
        self.price = 100.0

    async def get_quote(self, symbol):
        return Quote(symbol=symbol, last_price=self.price)


@pytest.mark.asyncio
async def test_the_tracker_adds_moves_the_stop_and_exits_all_at_3r(pcfg):
    from app.learning.outcomes import OutcomeTracker
    from app.storage import db
    db.init_db()
    sig = TradeSignal(id="SIG-PYR", instrument=Instrument(symbol="RELIANCE",
                                                          tradingsymbol="RELIANCE"),
                      side=Side.BUY, entry=100.0, stop_loss=99.0, target=104.0,
                      quantity=100, risk_reward=4.0, status=SignalStatus.OPEN,
                      notional=10_000.0, total_risk=100.0)
    db.save_signal(sig)
    rm = RiskManager(pcfg)
    rm.set_capital(1_000_000)
    broker = _Broker()
    tracker = OutcomeTracker(broker, pcfg, risk_manager=rm)

    for price in (100.2, 101.0, 102.0):
        broker.price = price
        assert await tracker.poll() == []
    row = db.get_signal("SIG-PYR")
    assert row["quantity"] == 175 and row["stop_loss"] == 101.0 and row["target"] == 103.0
    state = json.loads(row["payload"])["pyramid"]
    assert [a["level"] for a in state["adds"]] == [1, 2]

    broker.price = 103.0                                  # +3R from the base
    closed = await tracker.poll()
    assert closed and closed[0]["status"] == "CLOSED_TARGET"
    assert closed[0]["r_multiple"] == pytest.approx(4.25, abs=0.01)   # vs the BASE 1R
    assert closed[0]["pnl"] == pytest.approx(425.0, abs=0.5)


@pytest.mark.asyncio
async def test_after_level_one_a_reversal_exits_all_at_the_base_entry(pcfg):
    from app.learning.outcomes import OutcomeTracker
    from app.storage import db
    db.init_db()
    sig = TradeSignal(id="SIG-PYR2", instrument=Instrument(symbol="INFY", tradingsymbol="INFY"),
                      side=Side.BUY, entry=100.0, stop_loss=99.0, target=104.0, quantity=100,
                      risk_reward=4.0, status=SignalStatus.OPEN, notional=10_000.0,
                      total_risk=100.0)
    db.save_signal(sig)
    rm = RiskManager(pcfg)
    rm.set_capital(1_000_000)
    broker = _Broker()
    tracker = OutcomeTracker(broker, pcfg, risk_manager=rm)
    for price in (101.0, 100.5):
        broker.price = price
        await tracker.poll()
    broker.price = 99.95                                  # back through the base entry
    closed = await tracker.poll()
    assert closed[0]["status"] == "CLOSED_STOP"
    assert closed[0]["r_multiple"] == pytest.approx(-0.5, abs=0.01)   # not -1R


def test_the_rules_page_explains_the_pyramid(pcfg):
    from app.core.rules import build
    doc = build(pcfg)
    [sec] = [s for s in doc["sections"] if s["title"] == "The Standard Pyramid (Base-50-25)"]
    text = json.dumps(sec)
    assert "No averaging down" in text and "+1R" in text and "+2R" in text
    assert "50%" in text and "25%" in text and "+3R" in text


def test_the_replay_walk_plain_vs_pyramid(pcfg):
    from types import SimpleNamespace as B
    up = [B(low=99.9, high=101.0, close=100.9), B(low=100.8, high=102.0, close=101.9),
          B(low=101.8, high=103.0, close=102.9)]
    plain = pyramid.walk(up, True, 100.0, 99.0, pcfg, pyramid_on=False)
    pyr = pyramid.walk(up, True, 100.0, 99.0, pcfg)
    assert plain == {"r": 3.0, "outcome": "TARGET", "bars": 3, "adds": 0}
    assert pyr["outcome"] == "TARGET" and pyr["adds"] == 2
    assert pyr["r"] == pytest.approx(4.25, abs=0.001)          # 175% to +3R
    back = [B(low=99.9, high=101.0, close=100.9), B(low=99.5, high=100.5, close=99.8)]
    assert pyramid.walk(back, True, 100.0, 99.0, pcfg, pyramid_on=False)["outcome"] \
        == "SQUARE_OFF"
    stopped = pyramid.walk(back, True, 100.0, 99.0, pcfg)
    assert stopped["outcome"] == "STOP_L1" and stopped["r"] == pytest.approx(-0.5, abs=0.001)
