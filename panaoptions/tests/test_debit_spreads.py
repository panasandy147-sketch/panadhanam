"""Over-budget setups: debit spread, delta tiers, rolling spread, and the log.

  1. A 0.40-0.50 delta contract over the per-trade budget is not refused: the
     desk builds a bull call (or bear put) debit spread that fits.
  2. The primary tier is 0.40-0.50 delta; 0.30-0.39 is the fallback, liquid
     contracts only.
  3. The spread check uses a rolling 1-minute volume-weighted spread, so a
     momentary spike does not refuse a valid entry.
  4. The log says plainly: converted to a spread, or skipped on a hard risk
     failure.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from panaoptions import clock
from panaoptions.engine import contracts
from panaoptions.engine.liquidity import SpreadTracker, liquidity_problem
from panaoptions.models import Direction, ExitReason, OptionContract, OptionRight
from panaoptions.risk.gatekeeper import RiskGatekeeper
from tests.test_app import FakeFeed

ET = ZoneInfo("America/New_York")


def opt(strike, delta, mid, right=OptionRight.CALL, dte=2, width=0.04, oi=900,
        volume=300, symbol="SPY", expiry="2026-09-25"):
    return OptionContract(symbol=symbol, right=right, strike=strike, expiry=expiry,
                          dte=dte, bid=round(mid - width / 2, 2),
                          ask=round(mid + width / 2, 2),
                          delta=delta if right is OptionRight.CALL else -delta,
                          implied_volatility=0.2, open_interest=oi, volume=volume)


def calls():
    """SPY ~500: the 0.45 delta 500C is $6.00 ($600) — over a $400 budget."""
    return [opt(500, 0.45, 6.00), opt(502, 0.38, 4.90), opt(505, 0.30, 3.40),
            opt(508, 0.22, 2.20), opt(512, 0.14, 1.20), opt(495, 0.62, 9.10)]


@pytest.fixture
def desk_cfg(cfg):
    cfg.data["contracts"].update(min_dte=0, max_dte=4, min_delta=0.40, max_delta=0.50,
                                 max_contract_price=10.0, min_contract_price=0.10)
    return cfg


def _choose(cfg, chain, direction=Direction.LONG, budget=400.0):
    return contracts.choose("SPY", chain, direction, cfg, budget=budget)


# --------------------------------------------------------------------------- #
# 1. The debit spread
# --------------------------------------------------------------------------- #
def test_an_over_budget_call_becomes_a_bull_call_spread(desk_cfg):
    search = _choose(desk_cfg, calls())
    c = search.chosen
    assert c is not None and search.tier == "debit_spread" and search.budget_fallback
    assert c.is_spread and c.structure == "bull call spread"
    assert c.long_leg.strike == 500 and 0.40 <= abs(c.long_leg.delta) <= 0.50
    assert c.short_leg.strike > 500                        # sold further OTM
    assert c.cost(100) <= 400.0
    assert c.width > c.mid                                 # profit is possible
    # The widest spread the budget allows: 500/508 costs 3.80 ($380).
    assert c.short_leg.strike == 508 and c.mid == pytest.approx(3.80)
    assert "CONVERTED to a bull call debit spread" in search.note
    assert "naked $600" in search.note


def test_an_over_budget_put_becomes_a_bear_put_spread(desk_cfg):
    chain = [opt(500, 0.46, 6.20, OptionRight.PUT), opt(495, 0.30, 3.60, OptionRight.PUT),
             opt(492, 0.22, 2.60, OptionRight.PUT)]
    search = _choose(desk_cfg, chain, Direction.SHORT)
    c = search.chosen
    assert c.structure == "bear put spread" and c.short_leg.strike < c.long_leg.strike
    assert c.cost(100) <= 400.0


def test_a_contract_that_fits_is_bought_outright(desk_cfg):
    search = _choose(desk_cfg, calls(), budget=700.0)
    assert search.tier == "primary" and not search.chosen.is_spread


def test_the_spread_respects_the_per_share_ceiling_too(desk_cfg):
    desk_cfg.data["contracts"]["max_contract_price"] = 3.00   # "$300 a contract"
    desk_cfg.data["contracts"]["index_max_contract_price"] = 3.00
    search = _choose(desk_cfg, calls(), budget=5000.0)
    assert search.chosen.is_spread and search.chosen.mid <= 3.00


def test_the_spread_legs_must_be_liquid(desk_cfg):
    chain = calls()
    for c in chain:
        if c.strike != 500:
            c.open_interest, c.volume = 5, 0                  # nothing to sell into
    desk_cfg.data["contracts"]["budget_fallback_min_delta"] = 0
    search = _choose(desk_cfg, chain)
    assert search.chosen is None
    assert "SKIPPED — hard risk failure" in search.note
    assert "debit spread:" in search.note


def test_a_spread_with_poor_reward_to_risk_is_not_built(desk_cfg):
    desk_cfg.data["contracts"]["debit_spread"] = {"enabled": True, "min_reward_risk": 5.0}
    desk_cfg.data["contracts"]["budget_fallback_min_delta"] = 0
    search = _choose(desk_cfg, calls())
    assert search.chosen is None and "reward:risk" in search.note


# --------------------------------------------------------------------------- #
# 2. Delta tiers
# --------------------------------------------------------------------------- #
def test_with_spreads_off_the_secondary_tier_takes_a_liquid_0_3x_delta(desk_cfg):
    desk_cfg.data["contracts"]["debit_spread"] = {"enabled": False}
    search = _choose(desk_cfg, calls())
    assert search.tier == "secondary_delta"
    assert 0.30 <= abs(search.chosen.delta) < 0.40
    # 0.38 delta costs $490 (over $400); the 0.30 at $340 is the highest that fits.
    assert abs(search.chosen.delta) == 0.30 and search.chosen.cost(100) == 340.0
    assert "secondary tier (0.30-0.39 delta, liquid)" in search.note


def test_the_secondary_tier_refuses_an_illiquid_contract(desk_cfg):
    desk_cfg.data["contracts"]["debit_spread"] = {"enabled": False}
    chain = calls()
    for c in chain:
        c.open_interest, c.volume = 10, 3
    search = _choose(desk_cfg, chain)
    assert search.chosen is None
    assert "failed liquidity" in search.note


def test_nothing_below_0_30_ever(desk_cfg):
    desk_cfg.data["contracts"]["debit_spread"] = {"enabled": False}
    chain = [opt(500, 0.45, 6.00), opt(512, 0.14, 1.20)]
    assert _choose(desk_cfg, chain).chosen is None


def test_the_ladder_tries_the_spread_before_a_lower_delta(desk_cfg):
    assert contracts.DEFAULT_FALLBACK_ORDER == (
        "shorter_expiry", "debit_spread", "secondary_delta")
    search = _choose(desk_cfg, calls())
    assert search.tier == "debit_spread"
    desk_cfg.data["contracts"]["fallback_order"] = ["secondary_delta", "debit_spread"]
    assert _choose(desk_cfg, calls()).tier == "secondary_delta"


def test_liquidity_is_open_interest_or_volume(cfg):
    assert liquidity_problem(opt(500, 0.45, 6, oi=500, volume=0), cfg) == ""
    assert liquidity_problem(opt(500, 0.45, 6, oi=0, volume=80), cfg) == ""
    assert "thin" in liquidity_problem(opt(500, 0.45, 6, oi=20, volume=10), cfg)
    model = opt(500, 0.45, 6, oi=0, volume=0)
    model.estimated = True
    assert liquidity_problem(model, cfg) == ""       # no data to judge; spread still is


# --------------------------------------------------------------------------- #
# The gate, the size, the ledger
# --------------------------------------------------------------------------- #
def _signal(direction="LONG"):
    return {"symbol": "SPY", "direction": direction, "trigger_price": 500.0,
            "invalidation_level": 498.0, "target_price": 506.0,
            "confidence_score": 0.7, "source": "orb_vwap"}


def test_the_gatekeeper_checks_each_leg_and_the_net(desk_cfg):
    gate = RiskGatekeeper(desk_cfg)
    spread = contracts.make_spread(opt(500, 0.45, 6.00), opt(505, 0.30, 3.40))
    decision = gate.review(_signal(), spread, (0.40, 0.50))
    assert any("both legs" in p for p in decision.passed)
    assert any("bull call spread" in p for p in decision.passed)
    wide = contracts.make_spread(opt(500, 0.45, 6.00), opt(505, 0.30, 3.40, width=0.80))
    refused = gate.review(_signal(), wide, (0.40, 0.50))
    assert not refused.approved and any("on a leg" in r for r in refused.rejected)


def test_the_spread_is_sized_on_its_net_and_targets_stay_under_its_width(desk_cfg):
    from panaoptions.risk.guardrails import RiskManager
    from tests.test_architecture import _setup

    desk_cfg.data["account"]["starting_capital"] = 4000.0
    spread = contracts.make_spread(opt(500, 0.45, 6.00), opt(505, 0.30, 3.40))
    signal, why = RiskManager(desk_cfg).size(_setup("SPY"), spread, "S1",
                                             datetime(2026, 9, 23, 10, tzinfo=ET))
    assert signal is not None, why
    assert signal.entry_price == pytest.approx(2.60)
    assert signal.target_2 < 5.0 and signal.target_1 < signal.target_2
    assert signal.cost() <= 1000.0
    assert "BULL CALL SPREAD" in signal.alert_line()


def test_the_ledger_pays_slippage_on_both_legs_and_marks_the_net(desk_cfg):
    from panaoptions.ledger.paper import PaperLedger
    from panaoptions.models import Signal
    from panaoptions.risk.guardrails import RiskManager

    spread = contracts.make_spread(opt(500, 0.45, 6.00), opt(505, 0.30, 3.40))
    risk = RiskManager(desk_cfg)
    ledger = PaperLedger(desk_cfg, risk)
    ts = datetime(2026, 9, 23, 10, tzinfo=ET)
    trade = ledger.open(Signal(id="S", ts=ts, symbol="SPY", direction=Direction.LONG,
                               contract=spread, quantity=1, entry_price=2.60,
                               stop_price=1.40, target_1=3.80, target_2=4.70), ts)
    slip = ledger.slippage
    assert trade.entry_price == pytest.approx(2.60 + 2 * slip)
    assert (trade.structure, trade.legs, trade.max_value) == ("bull call spread", 2, 5.0)
    assert trade.long_label == "SPY 2026-09-25 500C"
    assert trade.short_label == "SPY 2026-09-25 505C"
    ledger.close(trade.id, 3.60, ExitReason.DAY_END, ts)
    assert trade.realised_pnl == pytest.approx((3.60 - 2 * slip - trade.entry_price) * 100)


# --------------------------------------------------------------------------- #
# 3. The rolling 1-minute volume-weighted spread
# --------------------------------------------------------------------------- #
def test_a_one_second_spike_is_averaged_out_by_volume():
    t0 = datetime(2026, 9, 23, 9, 45, tzinfo=ET)
    tracker = SpreadTracker(60)
    spike = opt(500, 0.45, 6.00, width=0.72, volume=100)       # 12% wide, quiet
    tracker.record([spike], t0)
    for i, vol in enumerate((400, 900), start=1):                # 3% wide, trading
        tracker.record([opt(500, 0.45, 6.00, width=0.18, volume=vol)],
                       t0 + timedelta(seconds=20 * i))
    rolling = tracker.rolling("SPY 2026-09-25 500C", t0 + timedelta(seconds=40))
    assert rolling < 4.0            # the spike weighs 1, the traded quotes 300 and 500
    # Older than a minute drops out of the window.
    assert tracker.rolling("SPY 2026-09-25 500C", t0 + timedelta(seconds=150)) is None


def test_the_picker_and_gate_use_the_rolling_figure(desk_cfg):
    t0 = datetime(2026, 9, 23, 9, 45, tzinfo=ET)
    tracker = SpreadTracker(60)
    # 3% wide while 900 contracts traded; then a 12% flash with 5 traded.
    tracker.record([opt(500, 0.45, 3.00, width=0.09, volume=100)], t0)
    tracker.record([opt(500, 0.45, 3.00, width=0.09, volume=1000)],
                   t0 + timedelta(seconds=15))
    now = [opt(500, 0.45, 3.00, width=0.36, volume=1005)]        # 12% right now
    assert contracts.choose("SPY", now, Direction.LONG, desk_cfg, budget=400).chosen is None
    tracker.annotate(now, t0 + timedelta(seconds=30))
    search = contracts.choose("SPY", now, Direction.LONG, desk_cfg, budget=400)
    assert search.chosen is not None and search.chosen.effective_spread_pct <= 7.0
    decision = RiskGatekeeper(desk_cfg).review(_signal(), search.chosen, (0.40, 0.50))
    assert any("1-min volume-weighted" in p for p in decision.passed)


# --------------------------------------------------------------------------- #
# 4. On the desk: the log says what happened
# --------------------------------------------------------------------------- #
class SpreadFeed(FakeFeed):
    """FakeFeed's clean long setup, with a chain whose 0.45 call is over a
    $100 budget but a 110/113 call spread fits."""

    def __init__(self, liquid=True, first_wide=False):
        super().__init__()
        self.liquid, self.first_wide, self.reads = liquid, first_wide, 0

    async def chain_for_window(self, symbol, spot, min_dte, max_dte):
        self.reads += 1
        oi = 900 if self.liquid else 3
        vol = 200 * self.reads if self.liquid else 0
        wide = self.first_wide and self.reads == 1
        return [opt(110, 0.45, 1.60, symbol=symbol, dte=10, expiry="2026-10-02", oi=oi,
                    width=0.40 if wide else 0.04, volume=vol),
                opt(113, 0.28, 0.90, symbol=symbol, dte=10, expiry="2026-10-02", oi=oi,
                    volume=vol),
                opt(116, 0.15, 0.40, symbol=symbol, dte=10, expiry="2026-10-02", oi=oi,
                    volume=vol)]


@pytest.fixture
def desk(cfg, monkeypatch, tmp_path):
    from panaoptions.app import OptionsDesk
    from panaoptions.ledger import store

    monkeypatch.setattr(store, "db_path", lambda: tmp_path / "t.db")
    cfg.data["contracts"]["max_contract_price"] = 2.00
    cfg.data["contracts"]["rolling_spread"] = {"enabled": True, "window_seconds": 60,
                                               "resamples": 2,
                                               "resample_delay_seconds": 0}
    cfg.data["universe"]["symbols"] = ["SPY"]
    # Chain reads are counted here; the daily F&O ingest would add one.
    cfg.data.setdefault("fno", {})["ingest"] = False
    monkeypatch.setattr(clock, "now",
                        lambda tz: datetime(2026, 9, 23, 10, 20, tzinfo=ET))
    return OptionsDesk(cfg=cfg, feed=SpreadFeed())


def _kinds(desk):
    return {e["kind"]: e for e in desk.activity.recent(200)}


def test_the_desk_converts_to_a_spread_and_says_so(desk):
    from panaoptions import audit

    asyncio.run(desk.cycle())
    events = _kinds(desk)
    assert "contract.spread" in events, [e["kind"] for e in desk.activity.recent(200)]
    assert events["contract.spread"]["level"] == "good"
    assert "CONVERTED to a bull call debit spread" in events["contract.spread"]["detail"]
    [trade] = desk.ledger.open_trades.values()
    assert trade.structure == "bull call spread" and trade.tier == "debit_spread"
    assert trade.contract_label == "SPY 2026-10-02 110/113C spread"
    assert "BULL CALL SPREAD" in events["trade.open"]["detail"]
    [buy] = [e for e in audit.entries(datetime(2026, 9, 23).date()) if e["event"] == "BUY"]
    assert buy["structure"] == "bull call spread" and len(buy["legs"]) == 2


def test_the_desk_reprices_a_spread_from_both_legs(desk):
    asyncio.run(desk.cycle())
    [trade] = desk.ledger.open_trades.values()
    assert asyncio.run(desk._contract_price(trade)) == pytest.approx(1.60 - 0.90)


def test_a_setup_no_rung_can_fund_is_skipped_as_a_hard_risk_failure(desk):
    desk.feed = SpreadFeed(liquid=False)
    asyncio.run(desk.cycle())
    events = _kinds(desk)
    assert not desk.ledger.open_trades
    skip = events["contract.skip"]
    assert skip["level"] == "bad" and "SKIPPED — hard risk failure" in skip["detail"]
    assert "debit spread:" in skip["detail"] and "0.30-0.39 delta tier" in skip["detail"]


def test_a_momentary_wide_quote_is_resampled_not_refused(desk):
    desk.cfg.data["contracts"]["debit_spread"] = {"enabled": False}
    desk.cfg.data["contracts"]["budget_fallback_min_delta"] = 0
    desk.cfg.data["account"]["starting_capital"] = 1000.0          # $200 buys the 110C
    desk.risk = type(desk.risk)(desk.cfg)
    desk.gatekeeper.risk = desk.risk
    desk.ledger.risk = desk.risk
    desk.feed = SpreadFeed(first_wide=True)
    asyncio.run(desk.cycle())
    events = _kinds(desk)
    assert desk.feed.reads >= 3                        # sampled again inside the minute
    assert "spread.rolling" in events
    assert desk.ledger.open_trades


def test_the_india_desk_uses_the_same_ladder(tmp_path, monkeypatch):
    from panaoptions import config as config_mod
    from panaoptions import markets

    monkeypatch.setattr(config_mod, "ENV_PATH", tmp_path / "absent.env")
    india = config_mod.Config(market="IN")
    try:
        assert (india.get("contracts.min_delta"), india.get("contracts.max_delta")) == (0.40, 0.50)
        assert india.get("contracts.debit_spread.enabled") is True
        lot = 75
        chain = [opt(24000, 0.45, 180.0, symbol="NIFTY"), opt(24100, 0.33, 120.0, symbol="NIFTY"),
                 opt(24200, 0.24, 75.0, symbol="NIFTY")]
        for c in chain:
            c.multiplier = lot
        search = contracts.choose("NIFTY", chain, Direction.LONG, india, budget=10000.0)
        assert search.chosen.is_spread and search.chosen.cost(lot) <= 10000.0
    finally:
        markets.activate("US")


def test_a_restart_keeps_the_spread_legs(desk):
    from panaoptions.ledger import store

    asyncio.run(desk.cycle())
    [restored] = store.load_open_book()
    assert restored.short_label == "SPY 2026-10-02 113C" and restored.legs == 2

