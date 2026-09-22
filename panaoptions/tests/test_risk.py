"""The $500 account rules. These are the ones that must never bend."""
from __future__ import annotations

from datetime import datetime

import pytest

from panaoptions.models import Direction, Indicators, OptionContract, OptionRight, Setup
from panaoptions.risk.guardrails import RiskManager


@pytest.fixture
def risk(cfg):
    return RiskManager(cfg)


@pytest.fixture
def setup():
    return Setup(symbol="AAPL", ts=datetime(2026, 9, 22, 9, 40),
                 direction=Direction.LONG, pattern="Hammer",
                 indicators=Indicators(close=230.0), underlying_support=229.0)


def _contract(mid=0.80):
    return OptionContract(symbol="AAPL", right=OptionRight.CALL, strike=245,
                          expiry="2026-10-02", dte=10,
                          bid=round(mid - 0.02, 2), ask=round(mid + 0.02, 2),
                          delta=0.18)


def _size(risk, setup, mid=0.80):
    return risk.size(setup, _contract(mid), "S1", datetime(2026, 9, 22, 9, 40))


# --------------------------------------------------------------------------- #
def test_deployed_capital_and_capital_at_risk_are_different_numbers(risk):
    # The specification says "risk 20% per trade". With a 20% stop that is 4%
    # of the account actually at risk. Conflating the two is how people end up
    # five times more exposed than they believe.
    profile = risk.describe()
    assert profile["max_deployed_pct"] == 20.0
    assert profile["risk_per_trade_pct"] == 4.0
    assert profile["risk_per_trade"] == 20.0      # $20 of a $500 account


def test_position_size_never_exceeds_the_deployment_budget(risk, setup):
    signal, _ = _size(risk, setup, mid=0.30)
    assert signal is not None
    assert signal.cost() <= 100.0, "20% of $500 is the ceiling"
    assert signal.quantity == 3


def test_a_contract_dearer_than_the_budget_is_refused_with_the_arithmetic(risk, setup):
    signal, why = _size(risk, setup, mid=4.60)
    assert signal is None
    assert "$460.00" in why and "$100.00" in why


def test_only_one_trade_at_a_time(risk, setup):
    risk.state.open_trades = 1
    signal, why = _size(risk, setup)
    assert signal is None
    assert "One trade at a time" in why


def test_the_stop_is_twenty_percent_of_the_contract_price(risk, setup):
    signal, _ = _size(risk, setup, mid=1.00)
    assert signal.stop_price == 0.80
    assert signal.target_1 == 1.40
    assert signal.target_2 == 1.70


def test_the_circuit_breaker_latches_at_the_daily_limit(risk, setup):
    risk.record_pnl(-30.0)
    assert not risk.state.halted
    assert risk.remaining_loss_budget == 20.0

    risk.record_pnl(-25.0)
    assert risk.state.halted
    assert risk.remaining_loss_budget == 0.0

    signal, why = _size(risk, setup)
    assert signal is None and "halted" in why.lower()


def test_a_profit_does_not_un_halt_a_halted_desk(risk, setup):
    risk.record_pnl(-60.0)
    assert risk.state.halted

    risk.record_pnl(+100.0)
    assert risk.state.halted, \
        "the day's decision stands; winning it back is exactly the impulse " \
        "the breaker exists to stop"


def test_a_new_session_clears_yesterdays_halt(risk, setup):
    risk.roll_day("2026-09-22")
    risk.record_pnl(-60.0)
    assert risk.state.halted

    risk.roll_day("2026-09-23")
    assert not risk.state.halted
    assert risk.state.realised_pnl == 0.0
    assert _size(risk, setup)[0] is not None


def test_rolling_to_the_same_day_does_not_wipe_the_running_total(risk):
    risk.roll_day("2026-09-22")
    risk.record_pnl(-15.0)
    risk.roll_day("2026-09-22")
    assert risk.state.realised_pnl == -15.0


def test_every_refusal_is_counted_so_a_dead_rule_shows_up(risk, setup):
    risk.state.open_trades = 1
    for _ in range(3):
        _size(risk, setup)
    assert sum(risk.state.rejections.values()) == 3


# --------------------------------------------------------------------------- #
# The daily limit scales with the account. Pinning it to a dollar figure means
# raising capital leaves a limit smaller than one stop-out.
# --------------------------------------------------------------------------- #
def test_the_daily_limit_is_a_percentage_of_capital_by_default(cfg):
    assert RiskManager(cfg).daily_limit == 50.0          # 10% of $500

    cfg.data["account"]["starting_capital"] = 2000.0
    assert RiskManager(cfg).daily_limit == 200.0, \
        "$50 was the instance at $500, not the rule"


def test_an_absolute_limit_still_overrides_the_percentage(cfg):
    cfg.data["risk"]["daily_loss_limit"] = 35.0
    assert RiskManager(cfg).daily_limit == 35.0


def test_raising_capital_no_longer_leaves_a_one_trade_day(cfg):
    from panaoptions.preflight import check

    cfg.data["account"]["starting_capital"] = 2000.0
    risk = RiskManager(cfg)

    one_stop = (cfg.capital * float(cfg.get("risk.max_capital_deployed_pct"))
                / 100 * float(cfg.get("risk.stop_loss_pct")) / 100)
    assert risk.daily_limit > one_stop, \
        "the breaker must not trip on the first loser"
    assert not [f for f in check(cfg) if f.level == "blocker"]


def test_the_breaker_still_latches_at_the_scaled_limit(cfg):
    cfg.data["account"]["starting_capital"] = 2000.0
    risk = RiskManager(cfg)

    risk.record_pnl(-150.0)
    assert not risk.state.halted
    assert risk.remaining_loss_budget == 50.0

    risk.record_pnl(-60.0)
    assert risk.state.halted
    assert "-210.00" in risk.state.halt_reason
    assert "-200.00 limit" in risk.state.halt_reason
