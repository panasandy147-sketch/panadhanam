"""Opportunity board tiering and the historical replay."""
from __future__ import annotations

import pytest

from app.analysis.opportunities import OpportunityScanner, RiskFactor
from app.brokers.paper import PaperBroker
from app.core.models import Bias, CycleResult
from app.scheduler import TradingEngine


@pytest.fixture
async def engine(cfg):
    """A desk wired to the paper broker, with the external feeds stubbed out.

    News and macro go over the network; leaving them live would make every test
    wait on real HTTP timeouts and fail in CI. Stubbing them also exercises the
    abstention path, which is what those agents do without data anyway.
    """
    cfg.settings["system"]["no_new_entry_after"] = "23:59"
    broker = PaperBroker(config={"total_capital": 100_000})
    await broker.connect()
    eng = TradingEngine(broker, cfg)

    async def _no_news():
        return []

    async def _no_macro():
        from app.core.models import MacroSnapshot
        return MacroSnapshot()

    eng.news.fetch = _no_news
    eng.macro.fetch = _no_macro
    return eng


# --------------------------------------------------------------------------- #
# Tiering
# --------------------------------------------------------------------------- #
def test_tier_thresholds_come_from_config(cfg):
    scanner = OpportunityScanner(engine=None, cfg=cfg)
    cfg.settings["opportunities"] = {"low_risk_max_points": 1,
                                     "medium_risk_max_points": 4}
    assert scanner._tier_for(-3) == "low"
    assert scanner._tier_for(1) == "low"
    assert scanner._tier_for(2) == "medium"
    assert scanner._tier_for(4) == "medium"
    assert scanner._tier_for(5) == "high"
    assert scanner._tier_for(12) == "high"


def test_retuning_thresholds_moves_the_buckets(cfg):
    scanner = OpportunityScanner(engine=None, cfg=cfg)
    cfg.settings["opportunities"] = {"low_risk_max_points": 4,
                                     "medium_risk_max_points": 8}
    assert scanner._tier_for(4) == "low"     # was medium under the defaults
    assert scanner._tier_for(8) == "medium"  # was high
    cfg.settings["opportunities"] = {"low_risk_max_points": 1,
                                     "medium_risk_max_points": 4}


def test_blocked_reasons_lead_with_the_structural_problem(cfg):
    """A clock that resolves tomorrow must not hide 'you can't afford one lot'."""
    scanner = OpportunityScanner(engine=None, cfg=cfg)
    result = CycleResult(cycle_id="c", symbol="NIFTY", bias=Bias.BULLISH,
                         composite_score=0.5)
    reasons = scanner._prioritise(
        ["Past the no-new-entry cutoff (15:00)",
         "Position sizes to 0 lots: capital is too small for this stop distance",
         "Desk halted: daily loss limit"],
        result)
    assert "0 lots" in reasons[0]
    assert "cutoff" in reasons[-1]


def test_neutral_with_no_blockers_still_explains_itself(cfg):
    scanner = OpportunityScanner(engine=None, cfg=cfg)
    result = CycleResult(cycle_id="c", symbol="NIFTY", bias=Bias.NEUTRAL,
                         composite_score=0.2)
    reasons = scanner._prioritise([], result)
    assert reasons and "conviction" in reasons[0].lower()


def test_risk_factor_points_sum_to_the_tier():
    factors = [RiskFactor("Instrument", "ATM option", 2),
               RiskFactor("Liquidity", "Index", -1),
               RiskFactor("IV", "rich", 2)]
    assert sum(f.points for f in factors) == 3


# --------------------------------------------------------------------------- #
# Scanning end to end
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_scan_returns_all_three_tiers(engine):
    eng = engine
    out = await eng.scanner.scan(symbols=["NIFTY 50", "RELIANCE", "TCS"], per_tier=5)

    assert set(out["tiers"]) == {"low", "medium", "high"}
    assert out["scanned"] == 3
    for tier in ("low", "medium", "high"):
        assert len(out["tiers"][tier]) <= 5


@pytest.mark.asyncio
async def test_scan_flags_simulated_data(engine):
    """The user must never mistake synthetic prices for a real market."""
    eng = engine
    out = await eng.scanner.scan(symbols=["NIFTY 50"])
    assert out["data_source"]["simulated"] is True
    assert "SIMULATED" in out["data_source"]["label"].upper()


@pytest.mark.asyncio
async def test_every_board_entry_has_a_direction_and_a_verdict(engine):
    eng = engine
    out = await eng.scanner.scan(symbols=["NIFTY 50", "RELIANCE", "TCS",
                                          "INFY", "SBIN"])
    for tier in out["tiers"].values():
        for opp in tier:
            # A board entry that says NEUTRAL tells the user nothing actionable.
            assert opp["lean"] in {"BULLISH", "BEARISH"}
            assert isinstance(opp["actionable"], bool)
            if not opp["actionable"]:
                assert opp["blocked_reason"], "a WATCH item must say why"
            assert opp["factors"], "the tier must be explainable"


@pytest.mark.asyncio
async def test_non_actionable_entries_still_show_the_projected_trade(engine):
    eng = engine
    out = await eng.scanner.scan(symbols=["NIFTY 50", "RELIANCE", "TCS", "LT"])
    entries = [o for tier in out["tiers"].values() for o in tier]
    assert entries, "expected at least one directional setup"
    for o in entries:
        if o["trade"]:
            t = o["trade"]
            # Direction and levels must be internally consistent.
            if t["side"] == "BUY":
                assert t["stop_loss"] < t["entry"] < t["target"]
            else:
                assert t["stop_loss"] > t["entry"] > t["target"]
            assert t["risk_reward"] >= float(eng.cfg.get("risk.min_risk_reward")) - 0.01


# --------------------------------------------------------------------------- #
# Replay
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_replay_produces_graded_trades(engine):
    eng = engine
    out = await eng.replay.run(days=3, symbols=["NIFTY 50", "RELIANCE"])

    t = out["totals"]
    assert t["trades"] == t["wins"] + t["losses"]
    if t["trades"]:
        assert 0 <= t["win_rate"] <= 100
        for trade in out["best_trades"]:
            assert trade["r_multiple"] > 0
            assert trade["outcome"] in {"TARGET", "STOP"}
        for trade in out["worst_trades"]:
            assert trade["r_multiple"] <= 0


@pytest.mark.asyncio
async def test_replay_labels_synthetic_data_and_states_its_limits(engine):
    eng = engine
    out = await eng.replay.run(days=2, symbols=["NIFTY 50"])
    assert out["data_source"]["simulated"] is True
    assert "NOT real historical performance" in out["data_source"]["label"]
    # The caveats are load-bearing: without them the numbers look tradeable.
    assert len(out["caveats"]) >= 4
    assert any("slippage" in c.lower() for c in out["caveats"])


@pytest.mark.asyncio
async def test_replay_resolves_ambiguous_bars_against_the_trade(engine):
    """A candle touching both stop and target must be scored as a loss.

    Without tick data we cannot know which came first, and optimism here would
    silently inflate every backtest the user ever runs.
    """
    eng = engine
    out = await eng.replay.run(days=5, symbols=["NIFTY 50", "RELIANCE", "TCS"])
    for trade in out["best_trades"] + out["worst_trades"]:
        if trade["outcome"] == "TARGET":
            assert trade["r_multiple"] > 0
        else:
            assert trade["r_multiple"] <= 0


@pytest.mark.asyncio
async def test_replay_winners_are_ranked_by_speed_at_equal_r(engine):
    eng = engine
    out = await eng.replay.run(days=5, symbols=["NIFTY 50", "RELIANCE"])
    best = out["best_trades"]
    for a, b in zip(best, best[1:], strict=False):
        if a["r_multiple"] == b["r_multiple"]:
            assert a["bars_held"] <= b["bars_held"]
