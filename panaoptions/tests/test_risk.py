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


TS = datetime(2026, 9, 22, 9, 40)


def _a_setup():
    return Setup(symbol="AAPL", ts=TS, direction=Direction.LONG,
                 pattern="Hammer", indicators=Indicators(close=230.0),
                 underlying_support=229.0)


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


def test_the_position_limit_is_what_the_config_says(risk, setup, cfg):
    """Three at once by default, and the refusal names the number.

    The desk reads every watchlist symbol in the same instant and takes as
    many setups as it has room for, so this limit is now a real gate rather
    than a formality — it decides how much of the account can be exposed to
    a single bad ten minutes.
    """
    assert int(cfg.get("risk.max_open_trades")) == 3
    risk.state.open_trades = 3
    signal, why = _size(risk, setup)
    assert signal is None
    assert "limit is 3" in why


def test_one_at_a_time_is_still_available(risk, setup, cfg):
    cfg.data["risk"]["max_open_trades"] = 1
    risk.state.open_trades = 1
    signal, why = _size(risk, setup)
    assert signal is None
    assert "limit is 1" in why


def test_the_premium_stop_is_a_backstop_in_underlying_mode(risk, setup, cfg):
    # The strategy's invalidation level is what normally fires; this stop only
    # catches a gap or a collapse in the option itself, so it is set wide.
    assert cfg.get("risk.stop_mode") == "underlying"
    signal, _ = _size(risk, setup, mid=1.00)

    assert signal.stop_price == 0.55          # 45% disaster backstop
    assert signal.target_1 == 1.40
    assert signal.target_2 == 1.70


def test_premium_mode_still_gives_the_tight_percentage_stop(risk, setup, cfg):
    cfg.data["risk"]["stop_mode"] = "premium"
    signal, _ = _size(risk, setup, mid=1.00)
    assert signal.stop_price == 0.80          # 20% of the contract price


def test_the_signal_carries_the_strategy_and_its_invalidation(risk, setup):
    from panaoptions.models import SetupType

    setup.strategy = SetupType.ORB_VWAP
    setup.underlying_support = 229.4
    setup.invalidation_note = "a 5m close back inside the opening range"
    signal, _ = _size(risk, setup, mid=0.80)

    assert signal.strategy is SetupType.ORB_VWAP
    assert signal.underlying_support == 229.4
    assert "opening range" in signal.invalidation_note


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
    risk.state.open_trades = int(risk.cfg.get("risk.max_open_trades", 1))
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


# --------------------------------------------------------------------------- #
# Total exposure, once more than one position can be open
# --------------------------------------------------------------------------- #
def test_the_per_trade_cap_is_not_the_whole_rule(cfg):
    """Three trades at 20% each is 60% deployed, and the per-trade cap is
    satisfied every single time.

    With one position allowed the two rules are the same rule. The moment a
    watchlist makes several symbols live at once, they are not.
    """
    cfg.data["account"]["starting_capital"] = 10_000.0
    cfg.data["risk"]["max_open_trades"] = 5
    cfg.data["risk"]["max_capital_deployed_pct"] = 20.0
    cfg.data["risk"]["max_total_deployed_pct"] = 40.0
    risk = RiskManager(cfg)

    contract = _contract(mid=2.00)
    first, _ = risk.size(_a_setup(), contract, "S1", TS)
    assert first is not None
    risk.state.open_trades = 1
    risk.state.deployed = first.cost(cfg.multiplier)

    second, _ = risk.size(_a_setup(), contract, "S2", TS)
    assert second is not None
    risk.state.open_trades = 2
    risk.state.deployed += second.cost(cfg.multiplier)

    # Two at 20% is the 40% ceiling. The third is refused even though it
    # passes the per-trade rule on its own.
    third, refusal = risk.size(_a_setup(), contract, "S3", TS)
    assert third is None
    assert "already at work" in refusal
    assert "40%" in refusal


def test_the_ceiling_defaults_to_the_per_trade_budget(cfg):
    """Unset, the total cap must not be looser than the per-trade one.

    A missing key defaulting to "no limit" would silently remove the ceiling
    for anyone who upgrades without editing their config.
    """
    cfg.data["account"]["starting_capital"] = 10_000.0
    cfg.data["risk"]["max_open_trades"] = 5
    cfg.data["risk"]["max_capital_deployed_pct"] = 20.0
    cfg.data["risk"].pop("max_total_deployed_pct", None)
    risk = RiskManager(cfg)

    first, _ = risk.size(_a_setup(), _contract(mid=2.00), "S1", TS)
    assert first is not None
    risk.state.open_trades = 1
    risk.state.deployed = first.cost(cfg.multiplier)

    second, refusal = risk.size(_a_setup(), _contract(mid=2.00), "S2", TS)
    assert second is None and "already at work" in refusal


def test_a_partial_position_still_fits_under_the_ceiling(cfg):
    """Room for two contracts but not four should buy two, not refuse."""
    cfg.data["account"]["starting_capital"] = 10_000.0
    cfg.data["risk"]["max_open_trades"] = 5
    cfg.data["risk"]["max_capital_deployed_pct"] = 20.0
    cfg.data["risk"]["max_total_deployed_pct"] = 30.0
    risk = RiskManager(cfg)
    risk.state.open_trades = 1
    risk.state.deployed = 2_000.0          # 20% already at work

    signal, refusal = risk.size(_a_setup(), _contract(mid=2.00), "S2", TS)
    assert signal is not None, refusal
    # $1,000 of room left, $200 a contract → five, not the ten the per-trade
    # budget alone would have allowed.
    assert signal.cost(cfg.multiplier) <= 1_000.0
