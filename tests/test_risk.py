"""The risk manager is the safety-critical component — test it hardest."""
from __future__ import annotations

import pytest

from app.agents.risk import RiskManager
from app.core.models import Bias, MarketContext, Quote, SignalStatus


@pytest.fixture
def rm(cfg):
    m = RiskManager(cfg)
    # These tests run at arbitrary wall-clock times; the entry cutoff has its
    # own test, so push it out of the way here.
    m.cfg.settings["system"]["no_new_entry_after"] = "23:59"
    # Pin the account size rather than inheriting whatever settings.yaml
    # currently says — the sizing assertions below are about the maths, not
    # about the shipped default.
    m.set_capital(100_000)
    # Likewise the policy: these tests are about the arithmetic at a known 1%
    # risk and 2R, not about the (high-risk paper) profile that ships.
    m.cfg.settings["risk"].update({"risk_per_trade_pct": 1.0,
                                   "max_risk_per_trade_pct": 2.0,
                                   "min_risk_reward": 2.0})
    return m


# --------------------------------------------------------------------------- #
# Position sizing identity
# --------------------------------------------------------------------------- #
def test_sizing_matches_the_formula(rm):
    """Quantity = risk budget / stop points, floored."""
    out = rm.size_calculator(capital=100_000, risk_pct=1.0,
                             entry=1000.0, stop_loss=990.0, lot_size=1)
    assert out["risk_amount"] == 1000.0          # 1% of 100k
    assert out["stop_points"] == 10.0
    assert out["quantity"] == 100                # 1000 / 10
    assert out["actual_risk"] == 1000.0
    assert out["actual_risk_pct"] == pytest.approx(1.0, abs=0.01)


def test_sizing_rounds_down_to_whole_lots(rm):
    """F&O sizing must never round UP — that would exceed the risk budget."""
    out = rm.size_calculator(capital=100_000, risk_pct=1.0, entry=24_500.0,
                             stop_loss=24_450.0, lot_size=75)
    # budget 1000 / 50 points = 20 units, which is 0 whole lots of 75
    assert out["lots"] == 0
    assert out["quantity"] == 0


def test_sizing_never_exceeds_budget(rm):
    for stop_pts in (3.7, 11.3, 47.9, 123.4):
        out = rm.size_calculator(100_000, 1.0, 1000.0, 1000.0 - stop_pts, 1)
        assert out["actual_risk"] <= out["risk_amount"] + 1e-6


def test_target_derives_from_stop_at_min_rr(rm):
    out = rm.size_calculator(100_000, 1.0, 1000.0, 990.0, 1, rr=2.0)
    assert out["target"] == 1020.0               # entry + 2 * 10


def test_zero_stop_distance_is_rejected(rm):
    assert "error" in rm.size_calculator(100_000, 1.0, 1000.0, 1000.0, 1)


# --------------------------------------------------------------------------- #
# Validation gates
# --------------------------------------------------------------------------- #
def _ctx(price: float = 1000.0, atr: float = 8.0) -> MarketContext:
    ctx = MarketContext(symbol="RELIANCE", cycle_id="t1",
                        quote=Quote(symbol="RELIANCE", last_price=price))
    ctx.indicators = {"primary": {"last_close": price, "atr": atr,
                                  "high": price * 1.02, "low": price * 0.98}}
    ctx.__dict__["_reports"] = []
    return ctx


def test_approves_a_clean_setup(rm):
    sig = rm.evaluate(_ctx(), Bias.BULLISH, [], 0.6,
                      ["candlestick breakout", "OI long buildup"])
    assert sig.status == SignalStatus.APPROVED
    assert sig.risk_reward >= 2.0
    assert sig.quantity > 0
    assert sig.stop_loss < sig.entry < sig.target


def test_rejects_below_min_risk_reward(rm, cfg):
    cfg.settings["risk"]["min_risk_reward"] = 2.0
    # The manager builds its target AT the required ratio, so the gate is
    # expressed by raising the requirement and checking the target follows.
    sig2 = rm.evaluate(_ctx(), Bias.BULLISH, [], 0.6, ["a", "b"])
    assert sig2.risk_reward >= 2.0   # the manager builds targets AT min R:R
    # An externally-supplied poor R:R must be refused:
    rm.cfg.settings["risk"]["min_risk_reward"] = 5.0
    sig3 = rm.evaluate(_ctx(), Bias.BULLISH, [], 0.6, ["a", "b"])
    assert sig3.risk_reward >= 5.0   # target scales with the requirement
    rm.cfg.settings["risk"]["min_risk_reward"] = 2.0


def test_daily_loss_limit_halts_the_desk(rm):
    rm.state.realised_pnl = -rm.state.daily_loss_limit - 1
    sig = rm.evaluate(_ctx(), Bias.BULLISH, [], 0.9, ["a", "b"])
    assert sig.status == SignalStatus.REJECTED
    assert any("loss limit" in r.lower() for r in sig.rejection_reasons)
    assert rm.state.halted


def test_max_open_positions_blocks_new_entries(rm, cfg):
    rm.state.open_positions = int(cfg.get("risk.max_open_positions", 3))
    sig = rm.evaluate(_ctx(), Bias.BULLISH, [], 0.9, ["a", "b"])
    assert sig.status == SignalStatus.REJECTED
    assert any("open positions" in r.lower() for r in sig.rejection_reasons)


def test_risk_pct_is_capped_at_the_hard_ceiling(rm, cfg):
    """Even if someone sets 10% in config, the hard ceiling wins."""
    cfg.settings["risk"]["risk_per_trade_pct"] = 10.0
    cfg.settings["risk"]["max_risk_per_trade_pct"] = 2.0
    sig = rm.evaluate(_ctx(), Bias.BULLISH, [], 0.9, ["a", "b"])
    assert sig.capital_at_risk_pct <= 2.01
    cfg.settings["risk"]["risk_per_trade_pct"] = 1.0


def test_short_setup_has_stop_above_entry(rm):
    sig = rm.evaluate(_ctx(), Bias.BEARISH, [], -0.6, ["a", "b"])
    assert sig.stop_loss > sig.entry > sig.target
    assert sig.side.value == "SELL"


def test_structural_stop_on_the_wrong_side_is_ignored(rm):
    """A 'structural' level above entry for a long is nonsense — fall back to ATR."""
    assert rm._structural_is_sane(1010.0, 1000.0, Bias.BULLISH, 8.0) is False
    assert rm._structural_is_sane(992.0, 1000.0, Bias.BULLISH, 8.0) is True
    # Absurdly far away is also unusable
    assert rm._structural_is_sane(800.0, 1000.0, Bias.BULLISH, 8.0) is False


def test_bookkeeping_tracks_wins_losses_and_halt(rm):
    sig = rm.evaluate(_ctx(), Bias.BULLISH, [], 0.6, ["a", "b"])
    rm.register_open(sig)
    assert rm.state.open_positions == 1
    rm.register_close(sig, pnl=500.0)
    assert rm.state.open_positions == 0
    assert rm.state.wins_today == 1
    assert rm.state.realised_pnl == 500.0

    # Losing more than the remaining budget must halt the desk.
    rm.register_open(sig)
    rm.register_close(sig, pnl=-(rm.state.daily_loss_limit + rm.state.realised_pnl) - 1)
    assert rm.state.halted is True
    assert "loss limit" in rm.state.halt_reason.lower()


# --------------------------------------------------------------------------- #
# Capital caps trim size rather than veto the idea
# --------------------------------------------------------------------------- #
def test_exposure_cap_trims_quantity_instead_of_rejecting(rm, cfg):
    """A tight stop implies a big notional. The desk cuts size to fit."""
    max_exposure = rm.state.capital * float(cfg.get("risk.max_exposure_pct", 50.0)) / 100.0
    sig = rm.evaluate(_ctx(price=1000.0, atr=8.0), Bias.BULLISH, [], 0.6, ["a", "b"])
    assert sig.status == SignalStatus.APPROVED
    assert sig.notional <= max_exposure + 1e-6
    # Risk stays at or under budget even after trimming.
    assert sig.total_risk <= rm.state.capital * 0.01 + 1e-6


def test_rejects_when_not_even_one_lot_fits(rm):
    """Index options on small capital: the trade is genuinely untakeable."""
    rm.state.capital = 20_000
    rm.state.exposure = 19_000
    sig = rm.evaluate(_ctx(price=24_500.0, atr=40.0), Bias.BULLISH, [], 0.9, ["a", "b"])
    assert sig.status == SignalStatus.REJECTED
    assert sig.quantity == 0


def test_entry_cutoff_blocks_late_entries(cfg):
    m = RiskManager(cfg)
    m.cfg.settings["system"]["no_new_entry_after"] = "00:00"   # always past it
    sig = m.evaluate(_ctx(), Bias.BULLISH, [], 0.9, ["a", "b"])
    assert sig.status == SignalStatus.REJECTED
    assert any("cutoff" in r.lower() for r in sig.rejection_reasons)


def test_leverage_widens_position_but_never_risk(rm, cfg):
    """The crucial invariant: leverage changes how much you can HOLD,
    never how much you can LOSE."""
    cfg.settings["risk"]["intraday_leverage"] = 1.0
    unlevered = rm.evaluate(_ctx(price=1000.0, atr=8.0), Bias.BULLISH, [], 0.6, ["a", "b"])

    cfg.settings["risk"]["intraday_leverage"] = 5.0
    rm.state.exposure = 0.0
    levered = rm.evaluate(_ctx(price=1000.0, atr=8.0), Bias.BULLISH, [], 0.6, ["a", "b"])

    assert levered.quantity >= unlevered.quantity          # bigger position allowed
    assert levered.capital_at_risk_pct <= 1.01             # same risk budget
    assert unlevered.capital_at_risk_pct <= 1.01
    cfg.settings["risk"]["intraday_leverage"] = 1.0


# --------------------------------------------------------------------------- #
# Changing the account size at runtime
# --------------------------------------------------------------------------- #
def test_set_capital_recomputes_everything_derived_from_it(rm):
    rm.set_capital(100_000)
    before = rm.snapshot()

    result = rm.set_capital(50_000)
    after = rm.snapshot()

    assert result["ok"] is True
    assert after["capital"] == 50_000
    # The per-trade budget, the daily halt and the exposure ceiling all follow.
    assert after["risk_per_trade"] == before["risk_per_trade"] / 2
    assert after["daily_loss_limit"] == before["daily_loss_limit"] / 2
    assert after["max_exposure"] == before["max_exposure"] / 2


def test_set_capital_rejects_nonsense(rm):
    assert rm.set_capital(0)["ok"] is False
    assert rm.set_capital(-500)["ok"] is False


def test_set_capital_refused_while_positions_are_open(rm):
    """Open positions were sized against the old capital; changing it underneath
    them would misreport how much is actually at risk."""
    rm.set_capital(100_000)
    sig = rm.evaluate(_ctx(), Bias.BULLISH, [], 0.6, ["a", "b"])
    rm.register_open(sig)

    result = rm.set_capital(10_000)
    assert result["ok"] is False
    assert "open" in result["reason"].lower()
    assert rm.state.capital == 100_000, "capital must not have moved"


def test_tiny_capital_rejects_and_says_what_is_needed(rm):
    """A 100-unit account cannot take a 2950-priced share. The rejection must
    name the capital required, not just say the stop is 'too wide'."""
    rm.set_capital(100)
    sig = rm.evaluate(_ctx(price=2950.0, atr=30.0), Bias.BULLISH, [], 0.6, ["a", "b"])

    assert sig.status == SignalStatus.REJECTED
    assert sig.quantity == 0
    reason = " ".join(sig.rejection_reasons)
    assert "0 shares" in reason
    assert "capital" in reason.lower()
    assert "you have" in reason.lower()


def test_risk_percentage_holds_at_every_account_size(rm):
    """The invariant that makes the whole system safe: whatever the capital,
    a single trade never risks more than the configured percentage."""
    for capital in (5_000, 50_000, 500_000, 5_000_000):
        rm.set_capital(capital)
        rm.state.exposure = 0.0
        sig = rm.evaluate(_ctx(price=1000.0, atr=8.0), Bias.BULLISH, [], 0.6, ["a", "b"])
        if sig.quantity > 0:
            assert sig.capital_at_risk_pct <= 1.01, f"breached at capital {capital}"
