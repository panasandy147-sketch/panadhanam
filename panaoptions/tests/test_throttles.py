"""The tournament rules: 3% daily lockout, 2% risk a trade, 2 open, 4 a day,
a verified 1:3 for the sweep and the value-area rejection, and the history
validation that has to pass before the rule book is trusted."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from panaoptions.backtest_spreads import Result
from panaoptions.ledger.paper import PaperLedger
from panaoptions.models import Direction, Indicators, OptionContract, OptionRight, Setup, SetupType
from panaoptions.risk.guardrails import RiskManager, planned_loss_per_contract

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 9, 23, 10, 0, tzinfo=ET)


@pytest.fixture
def shipped(cfg):
    """The shipped rules on the shipped $4,000 account (conftest pins the old
    ones for the loop tests)."""
    from panaoptions.config import Config
    fresh = Config()
    cfg.data["account"]["starting_capital"] = 4000.0
    for key in ("max_risk_per_trade_pct", "max_daily_trades", "max_open_trades",
                "daily_loss_limit_pct"):
        cfg.data["risk"][key] = fresh.data["risk"][key]
    return cfg


def _contract(mid=2.00, delta=0.45, symbol="AAPL"):
    return OptionContract(symbol=symbol, right=OptionRight.CALL, strike=230, expiry="2026-09-25",
                          dte=2, bid=mid - 0.02, ask=mid + 0.02, delta=delta)


def _setup(close=230.0, stop=229.0, strategy=SetupType.ORB_VWAP, target=0.0, symbol="AAPL"):
    return Setup(symbol=symbol, ts=NOW, direction=Direction.LONG, strategy=strategy,
                 pattern="x", indicators=Indicators(close=close, atr=1.0),
                 underlying_support=stop, underlying_target=target, entry_trigger=close)


# --------------------------------------------------------------------------- #
# The numbers
# --------------------------------------------------------------------------- #
def test_the_shipped_numbers_are_the_tournament_ones():
    from panaoptions.config import Config
    for cfg in (Config(), Config(profile="zerodte"), Config(profile="scalp")):
        g = cfg.get
        assert g("risk.daily_loss_limit_pct") == 3.0
        assert g("risk.max_risk_per_trade_pct") == 2.0
        assert g("risk.max_open_trades") == 2
        assert g("risk.max_daily_trades") == 4
    risk = RiskManager(Config())
    assert risk.daily_limit == 120.0 and risk.risk_per_trade == 80.0


def test_india_gets_the_same_rules_in_rupees(tmp_path, monkeypatch):
    from panaoptions import config as config_mod
    from panaoptions import markets
    monkeypatch.setattr(config_mod, "ENV_PATH", tmp_path / "absent.env")
    try:
        india = config_mod.Config(market="IN")
        risk = RiskManager(india)
        assert risk.daily_limit == 10500.0 and risk.risk_per_trade == 7000.0
    finally:
        markets.activate("US")


# --------------------------------------------------------------------------- #
# 2% risk a trade
# --------------------------------------------------------------------------- #
def test_the_loss_at_the_stop_is_the_first_exit_to_fire(shipped):
    # 0.45 delta x $1 to the stop x 100 = $45, before the 45% backstop ($90).
    assert planned_loss_per_contract(shipped, _contract(2.00), 230.0, 229.0) == 45.0
    # A far stop: the 45% premium backstop fires first.
    assert planned_loss_per_contract(shipped, _contract(2.00), 230.0, 220.0) == 90.0


def test_the_size_is_capped_by_the_loss_not_only_the_premium(shipped):
    risk = RiskManager(shipped)
    signal, why = risk.size(_setup(), _contract(2.00), "S1", NOW)
    # $800 buys four $200 contracts, but $80 of risk at $45 each allows one.
    assert signal is not None, why
    assert signal.quantity == 1


def test_a_contract_that_alone_risks_more_than_2pct_is_refused(shipped):
    risk = RiskManager(shipped)
    signal, why = risk.size(_setup(stop=220.0), _contract(2.00), "S1", NOW)
    assert signal is None and "Risk per trade" in why and "$80.00" in why


# --------------------------------------------------------------------------- #
# 2 open, 4 a day
# --------------------------------------------------------------------------- #
def test_the_fifth_trade_of_the_day_is_refused(shipped):
    risk = RiskManager(shipped)
    risk.roll_day("2026-09-23")
    risk.state.trades_taken = 4
    signal, why = risk.size(_setup(), _contract(), "S5", NOW)
    assert signal is None and "Daily trade limit: 4 of 4" in why


def test_a_third_open_position_is_refused(shipped):
    risk = RiskManager(shipped)
    risk.state.open_trades = 2
    signal, why = risk.size(_setup(), _contract(), "S3", NOW)
    assert signal is None and "limit is 2" in why


# --------------------------------------------------------------------------- #
# The 3% breaker and the lockout
# --------------------------------------------------------------------------- #
def test_a_3pct_loss_locks_out_the_order_path(shipped):
    risk = RiskManager(shipped)
    risk.roll_day("2026-09-23")
    risk.record_pnl(-121.0)
    assert risk.state.halted and "Locked out" in risk.state.halt_reason
    signal, why = risk.size(_setup(), _contract(), "S", NOW)
    assert signal is None and "halted" in why.lower()
    # And the lowest layer refuses too, whatever path asks.
    from panaoptions.models import Signal
    order = Signal(id="X", ts=NOW, symbol="AAPL", direction=Direction.LONG,
                   contract=_contract(), quantity=1, entry_price=2.0, stop_price=1.1)
    with pytest.raises(PermissionError):
        PaperLedger(shipped, risk).open(order, NOW)


@pytest.fixture
def desk(shipped, monkeypatch, tmp_path):
    from panaoptions.app import OptionsDesk
    from panaoptions.ledger import store
    from tests.test_app import FakeFeed
    monkeypatch.setattr(store, "_conn", None)
    monkeypatch.setattr(store, "db_path", lambda: tmp_path / "t.db")
    shipped.data["contracts"]["max_contract_price"] = 2.00
    return OptionsDesk(cfg=shipped, feed=FakeFeed())


@pytest.mark.asyncio
async def test_the_lockout_cancels_flattens_and_survives_a_restart(desk, shipped):
    from panaoptions.app import OptionsDesk
    from panaoptions.ledger import store
    from panaoptions.models import Signal
    from tests.test_app import FakeFeed

    desk.risk.roll_day("2026-09-23")
    trade = desk.ledger.open(Signal(id="S", ts=NOW, symbol="AAPL", direction=Direction.LONG,
                                    contract=_contract(0.80), quantity=1, entry_price=0.80,
                                    stop_price=0.44), NOW)
    desk.candidate = {"symbol": "QQQ", "taken": False}
    desk.risk.record_pnl(-125.0)                   # a realised loss past $120

    actions = await desk._enforce_lockout(NOW)
    assert "circuit breaker: locked out for the day" in actions
    assert not desk.ledger.open_trades and not trade.is_open      # flattened
    assert desk.candidate is None                                 # pending cancelled
    assert store.lockout("2026-09-23")["reason"].startswith("daily loss limit hit")

    # A restart the same day is still locked out; the next day is not.
    store.save_session("2026-09-23", desk.risk.state)
    again = OptionsDesk(cfg=shipped, feed=FakeFeed())
    assert again.risk.roll_day("2026-09-23")
    again._restore_day("2026-09-23")
    # (-125 realised plus what flattening the open position booked)
    assert again.risk.state.halted and again.risk.state.realised_pnl <= -125.0
    assert again.risk.roll_day("2026-09-24")
    again._restore_day("2026-09-24")
    assert not again.risk.state.halted


# --------------------------------------------------------------------------- #
# A verified 1:3 for the sweep and the value-area rejection
# --------------------------------------------------------------------------- #
def test_a_value_area_rejection_needs_its_own_target_at_3r(shipped):
    from panaoptions.engine import reward
    from panaoptions.models import SessionLevels
    levels = SessionLevels()
    short_poc = _setup(strategy=SetupType.VA_REJECTION, target=232.0)     # 2R
    target, rr, why, _ = reward.project(short_poc, levels, shipped)
    assert why and "only 2.0R" in why and "verified 1:3" in why
    far_poc = _setup(strategy=SetupType.VA_REJECTION, target=233.5)       # 3.5R
    target, rr, why, _ = reward.project(far_poc, levels, shipped)
    assert not why and target == 233.5
    # Other strategies may still use a projected 3R when the road is open.
    orb = _setup(strategy=SetupType.ORB_VWAP, target=232.0)
    target, rr, why, _ = reward.project(orb, levels, shipped)
    assert not why and target == 233.0


# --------------------------------------------------------------------------- #
# The history validation
# --------------------------------------------------------------------------- #
def _fill(hh, mm, pnl, symbol="SPY", out_hh=None, strategy="ORB + VWAP", day=23):
    t = datetime(2026, 9, day, hh, mm, tzinfo=ET)
    end = (datetime(2026, 9, day, out_hh, mm, tzinfo=ET) if out_hh
           else t + timedelta(minutes=30))
    return Result(symbol=symbol, ts=t.isoformat(timespec="minutes"), direction="LONG",
                  tier="primary", contract=f"{symbol} C", cost=100.0, pnl=pnl, mid=1.00,
                  delta=0.45, lot=100, spot=500.0, stop=499.0, strategy=strategy,
                  exit_ts=end.isoformat(timespec="minutes"), side="LONG_CALL",
                  exit_reason="TARGET" if pnl > 0 else "STOP")


def test_the_simulation_applies_the_throttles_and_the_breaker(shipped):
    from panaoptions import validate
    shipped.data["risk"]["slippage_per_contract"] = 0.0
    fills = [_fill(10, 0, 90, "SPY"), _fill(10, 0, 90, "QQQ"),
             _fill(10, 5, 90, "AAPL"),                   # a third at once
             _fill(11, 0, -45, "SPY"), _fill(11, 0, -45, "QQQ"),
             _fill(12, 0, 90, "NVDA")]                   # the fifth of the day
    taken, skipped, locked, dd = validate.simulate(fills, shipped)
    assert [t.symbol for t in taken] == ["QQQ", "SPY", "QQQ", "SPY"]      # same time: by name
    assert skipped["max open trades (2)"] == 1
    assert skipped["daily trade limit (4)"] == 1
    # $45 planned at the stop per contract, $80 a trade -> one contract: R = pnl / 45.
    assert [t.r for t in taken] == [2.0, 2.0, -1.0, -1.0]

    losers = [_fill(10, 0, -90, "SPY"), _fill(10, 0, -45, "QQQ"),
              _fill(11, 0, 90, "AAPL")]                  # after -$135: locked out
    taken, skipped, locked, dd = validate.simulate(losers, shipped)
    assert locked == {"2026-09-23"} and len(taken) == 2
    assert skipped["daily circuit breaker (locked out)"] == 1
    assert dd == pytest.approx(135 / 4000 * 100, abs=0.01)


def test_the_verdict_needs_expectancy_drawdown_and_enough_trades(shipped):
    from panaoptions import validate
    shipped.data["risk"]["slippage_per_contract"] = 0.0
    good = [_fill(10, 0, 90, day=d) for d in range(1, 25)]
    v = validate.judge(*validate.simulate(good, shipped), shipped, ["SPY"], 25)
    assert v.verdict == "PASS" and v.expectancy_r == 2.0 and v.trades >= 20
    few = validate.judge(*validate.simulate(good[:5], shipped), shipped, ["SPY"], 5)
    assert few.verdict == "INCONCLUSIVE"
    bad = [_fill(10, 0, 20 if d % 2 else -45, day=d) for d in range(1, 29)]
    v = validate.judge(*validate.simulate(bad, shipped), shipped, ["SPY"], 28)
    assert v.verdict == "FAIL" and any("expectancy" in r for r in v.reasons)
    md = validate.markdown(v)
    assert "Backtest validation — FAIL" in md and "By strategy" in md


def test_the_start_check_shows_the_last_verdict(shipped):
    from panaoptions import preflight, validate
    from panaoptions.journal import store as journal_store

    def finding():
        return next((f for f in preflight.check(shipped)
                     if f.setting == "backtest.validation"), None)

    assert "not been validated" in finding().problem
    folder = journal_store.JOURNAL_DIR / "backtest"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "validation-latest.json").write_text(json.dumps(
        {"verdict": "FAIL", "reasons": ["expectancy +0.10R is below 0.5R"],
         "at": "2026-09-29T10:00:00"}), encoding="utf-8")
    assert "FAIL" in finding().problem and "0.10R" in finding().problem
    (folder / "validation-latest.json").write_text(json.dumps(
        {"verdict": "PASS", "reasons": [], "at": "2026-09-29T10:00:00"}), encoding="utf-8")
    assert finding() is None
    assert validate.latest()["verdict"] == "PASS"


# --------------------------------------------------------------------------- #
# Ranking the day's slots; POC off; walk-forward
# --------------------------------------------------------------------------- #
def test_poc_bounce_is_off_on_every_desk():
    from panaoptions.config import Config
    for cfg in (Config(), Config(profile="zerodte"), Config(profile="scalp")):
        assert cfg.get("strategies.poc_bounce.enabled") is False
    from panaoptions.engine.strategies import ALL
    from panaoptions.models import SetupType as ST
    cfg = Config()
    enabled = {cls(cfg).name for cls in ALL if cls(cfg).enabled}
    assert ST.POC_BOUNCE not in enabled


def test_without_a_validation_the_priority_list_decides(shipped):
    from panaoptions import ranking
    assert ranking.key(SetupType.ORB_VWAP) == "orb_vwap" == ranking.key("ORB + VWAP")
    assert ranking.preferred(shipped, SetupType.PD_LIQUIDITY_SWEEP)
    assert ranking.preferred(shipped, SetupType.ORB_VWAP)
    assert not ranking.preferred(shipped, SetupType.VWAP_EMA_PULLBACK)
    # 4 a day, 2 reserved: a non-preferred strategy may take the first two only.
    assert ranking.slot_refusal(shipped, SetupType.VWAP_EMA_PULLBACK, 1) == ""
    assert "kept for the strategies" in ranking.slot_refusal(
        shipped, SetupType.VWAP_EMA_PULLBACK, 2)
    assert ranking.slot_refusal(shipped, SetupType.ORB_VWAP, 3) == ""


def test_a_validated_edge_overrides_the_list(shipped):
    from panaoptions import ranking
    edge = {"vwap_ema_pullback": 0.6, "orb_vwap": -0.2}
    assert ranking.preferred(shipped, SetupType.VWAP_EMA_PULLBACK, edge)
    assert not ranking.preferred(shipped, SetupType.ORB_VWAP, edge)
    assert (ranking.score(shipped, SetupType.VWAP_EMA_PULLBACK, edge)
            > ranking.score(shipped, SetupType.ORB_VWAP, edge))
    # Too few fills to count as known.
    raw = {"orb_vwap": {"fills": 3, "expectancy_r": 2.0},
           "va_rejection": {"fills": 9, "expectancy_r": 0.7}}
    assert ranking.edge_from(raw, shipped) == {"va_rejection": 0.7}


def test_the_desk_refuses_a_weak_strategy_the_reserved_slots(shipped):
    risk = RiskManager(shipped)
    risk.roll_day("2026-09-23")
    risk.state.trades_taken = 2
    weak = _setup(strategy=SetupType.VWAP_EMA_PULLBACK)
    signal, why = risk.size(weak, _contract(), "S", NOW)
    assert signal is None and "Reserved slots" in why
    signal, why = risk.size(_setup(strategy=SetupType.ORB_VWAP), _contract(), "S", NOW)
    assert signal is not None, why


def test_the_simulation_keeps_the_last_slots_for_the_better_strategies(shipped):
    from panaoptions import validate
    shipped.data["risk"]["slippage_per_contract"] = 0.0
    pull = "VWAP / 9-EMA Pullback"
    fills = [_fill(10, 0, 45, "SPY", strategy=pull), _fill(10, 40, 45, "QQQ", strategy=pull),
             _fill(11, 20, 45, "AAPL", strategy=pull),          # 3rd weak one: held back
             _fill(12, 0, 90, "NVDA"), _fill(12, 50, 90, "AMD")]  # ORB: the reserved slots
    taken, skipped, _, _ = validate.simulate(fills, shipped)
    assert [t.symbol for t in taken] == ["SPY", "QQQ", "NVDA", "AMD"]
    assert skipped["reserved slots (kept for the better strategies)"] == 1


def test_the_walk_forward_ranks_on_the_first_half_and_judges_the_second(shipped):
    from panaoptions import validate
    shipped.data["risk"]["slippage_per_contract"] = 0.0
    pull = "VWAP / 9-EMA Pullback"
    early = [_fill(10, 0, 90, "SPY", strategy=pull, day=d) for d in (1, 2, 3, 4, 5, 6)]
    late = [_fill(10, 0, -45, "SPY", strategy=pull, day=d) for d in (7, 8, 9, 10, 11, 12)]
    v = validate.judge_results(early + late, shipped, ["SPY"], 12)
    assert "walk-forward" in v.period and "judged on 2026-09-07" in v.period
    # Ranked as a winner on days 1-6, judged on days 7-12 where it lost.
    assert v.trades == 6 and v.expectancy_r == -1.0
    # The saved edge covers the whole period (what the live desk ranks by).
    assert v.edge["vwap_ema_pullback"]["fills"] == 12
    assert "Strategy edge" in validate.markdown(v)
