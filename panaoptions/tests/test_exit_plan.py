"""The R-multiple exit plan: half at +1.5R, breakeven, trail the rest 1R —
live in the paper ledger and in the backtest's replay."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from panaoptions.backtest_spreads import _walk_r
from panaoptions.ledger.paper import PaperLedger
from panaoptions.models import Candle, Direction, ExitReason, OptionContract, OptionRight, Signal
from panaoptions.risk.guardrails import RiskManager

ET = ZoneInfo("America/New_York")
TS = datetime(2026, 9, 23, 10, 0, tzinfo=ET)


@pytest.fixture
def rcfg(cfg):
    cfg.data["risk"].update({"exit_style": "r_multiple", "scale_out_r": 1.5,
                             "runner_trail_r": 1.0, "take_profit_1_size_pct": 50.0,
                             "slippage_per_contract": 0.0})
    return cfg


def _open(cfg, quantity=2, direction=Direction.LONG):
    ledger = PaperLedger(cfg, RiskManager(cfg))
    c = OptionContract(symbol="SPY", right=OptionRight.CALL, strike=500, expiry="2026-09-25",
                       dte=2, bid=0.99, ask=1.01, delta=0.5)
    stop = 99.0 if direction is Direction.LONG else 101.0
    signal = Signal(id="S", ts=TS, symbol="SPY", direction=direction, contract=c,
                    quantity=quantity, entry_price=1.00, stop_price=0.55, target_1=1.4,
                    target_2=1.7, underlying_at_entry=100.0, underlying_support=stop)
    return ledger, ledger.open(signal, TS)


def test_half_goes_at_one_and_a_half_r_then_breakeven_then_the_trail(rcfg):
    ledger, trade = _open(rcfg)
    assert trade.risk_r == 1.0 and trade.underlying_entry == 100.0
    # +1.0R: nothing yet (the premium +40% target of the old plan is ignored).
    assert ledger.mark(trade.id, 1.45, 101.0, TS) == []
    # +1.5R: half sold, the underlying stop moves to the entry.
    fills = ledger.mark(trade.id, 1.60, 101.5, TS)
    assert [f.reason for f in fills] == ["TARGET_1"] and trade.remaining == 1
    assert trade.underlying_support == 100.0
    # Runs to +3R, then gives back 1R: the trail takes the rest.
    assert ledger.mark(trade.id, 2.20, 103.0, TS) == []
    fills = ledger.mark(trade.id, 1.90, 102.0, TS)
    assert trade.exit_reason is ExitReason.TRAIL and not trade.is_open


def test_one_contract_is_not_halved_it_moves_to_breakeven(rcfg):
    ledger, trade = _open(rcfg, quantity=1)
    assert ledger.mark(trade.id, 1.60, 101.6, TS) == []           # no partial sale
    assert trade.breakeven_armed and trade.remaining == 1
    ledger.mark(trade.id, 0.98, 99.9, TS)                         # back through the entry
    assert trade.exit_reason is ExitReason.UNDERLYING_BREAK


def test_puts_use_the_same_plan_downward(rcfg):
    ledger, trade = _open(rcfg, direction=Direction.SHORT)
    fills = ledger.mark(trade.id, 1.60, 98.5, TS)
    assert [f.reason for f in fills] == ["TARGET_1"]
    assert trade.underlying_support == 100.0


def _bars(path):
    t0 = datetime(2026, 9, 23, 14, 0, tzinfo=UTC)                 # 10:00 ET
    return [Candle(ts=t0 + timedelta(minutes=5 * i), open=o, high=h, low=lo, close=c,
                   volume=1000) for i, (o, h, lo, c) in enumerate(path)]


def test_the_replay_walks_the_same_plan(rcfg):
    when = datetime(2026, 9, 23, 10, 0, tzinfo=ET)
    bars = _bars([(100, 100.4, 99.8, 100.2), (100.2, 101.6, 100.1, 101.5),   # +1.5R
                  (101.5, 103.0, 101.4, 102.8), (102.8, 102.9, 101.9, 102.0)])  # trail
    legs = _walk_r(bars, when, rcfg, Direction.LONG, 100.0, 99.0, split=True)
    assert [(round(p, 2), f, why) for _, p, f, why in legs] == [
        (101.5, 0.5, "TARGET_1"), (102.0, 0.5, "TRAIL")]
    one = _walk_r(bars, when, rcfg, Direction.LONG, 100.0, 99.0, split=False)
    assert [(f, why) for _, _, f, why in one] == [(1.0, "TRAIL")]
    stopped = _walk_r(_bars([(100, 100.2, 98.9, 99.0)]), when, rcfg, Direction.LONG,
                      100.0, 99.0, split=True)
    assert [(p, f, why) for _, p, f, why in stopped] == [(99.0, 1.0, "STOP")]


def test_the_validation_uses_the_configured_plan(rcfg):
    from panaoptions import validate
    from panaoptions.backtest_spreads import Result
    r = Result(symbol="SPY", ts="2026-09-23T10:00-04:00", direction="LONG", tier="primary",
               pnl=-10.0, exit_ts="2026-09-23T10:30-04:00", pnl_r=40.0, pnl_r1=30.0,
               exit_ts_r="2026-09-23T11:00-04:00", exit_ts_r1="2026-09-23T11:30-04:00",
               exit_reason_r="TRAIL")
    assert validate.outcome(r, rcfg, 2) == (40.0, "2026-09-23T11:00-04:00", "TRAIL")
    assert validate.outcome(r, rcfg, 1)[0] == 30.0
    rcfg.data["risk"]["exit_style"] = "auto"
    assert validate.outcome(r, rcfg, 2)[0] == -10.0
