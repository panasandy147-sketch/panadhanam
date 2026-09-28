"""Extreme options flow in the committee (agents/consensus.py).

  Derivatives Confluence Override   1.41x RVOL under a 17.9x put sweep on a
                                    Tweezer Top is APPROVED; the same setup
                                    without the flow is vetoed at the 1.5x gate.
  Derivatives Flow Conflict Veto    a bullish Tweezer Bottom on IWM under
                                    18.4x put volume is VETOED, however
                                    positive the candlestick score.
  Weight boost                      the Derivatives vote counts double while
                                    an extreme anomaly is on the tape.

(Named as requested; this is the panaoptions suite, run from panaoptions/.)
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from panaoptions import alpha
from panaoptions.agents import cmio, consensus
from panaoptions.agents.base import AgentVote
from panaoptions.models import (
    ContractSearch,
    Direction,
    Indicators,
    OptionContract,
    OptionRight,
    Setup,
    SetupType,
)
from panaoptions.risk.gatekeeper import RiskGatekeeper

NOW = datetime(2026, 9, 28, 14, 30, tzinfo=UTC)
SPOT = 220.0


@pytest.fixture
def desk_cfg(cfg):
    """The shipped $4,000 account on the same-day contract window."""
    cfg.data["account"]["starting_capital"] = 4000.0
    cfg.data["risk"].update(max_capital_deployed_pct=20.0, max_total_deployed_pct=45.0,
                            max_spread_pct_of_mid=7.0, daily_loss_limit=None,
                            daily_loss_limit_pct=10.0)
    cfg.data["contracts"].update(min_delta=0.35, max_delta=0.50,
                                 budget_fallback_min_delta=0.30)
    return cfg


def _tweezer(direction: Direction, rvol: float) -> Setup:
    """A triggered Tweezer Top (short) or Tweezer Bottom (long) on IWM."""
    short = direction is Direction.SHORT
    return Setup(
        symbol="IWM", ts=NOW, direction=direction,
        strategy=SetupType.CANDLESTICK_AT_LEVEL,
        pattern="Tweezer Top" if short else "Tweezer Bottom",
        confirmations=["tweezer on the 5m close", "at the opening range",
                       "price took out the trigger"],
        indicators=Indicators(close=SPOT, vwap=SPOT, rvol=rvol, atr=0.4),
        entry_trigger=SPOT - 0.10 if short else SPOT + 0.10,
        underlying_support=SPOT + 0.60 if short else SPOT - 0.60,
        trend_aligned=True, delta_band=(0.40, 0.50))


def _contract(right: OptionRight, volume: int, open_interest: int,
              strike: float = SPOT, mid: float = 3.00) -> OptionContract:
    return OptionContract(
        symbol="IWM", right=right, strike=strike, expiry="2026-09-28", dte=0,
        bid=round(mid * 0.99, 2), ask=round(mid * 1.01, 2),
        delta=0.45 if right is OptionRight.CALL else -0.45,
        implied_volatility=0.22, volume=volume, open_interest=open_interest)


def _convene(cfg, setup: Setup, chain: list[OptionContract],
             chosen: OptionContract, screen_rvol: float):
    signal = alpha.from_setup(setup)
    assert signal is not None
    chief = cmio.CMIO(cfg, RiskGatekeeper(cfg))
    return asyncio.run(chief.convene(
        signal=signal, setup=setup, candles=[], chain=chain,
        search=ContractSearch(symbol="IWM", chosen=chosen), feed=object(),
        now=NOW, screen_rvol=screen_rvol))


# =========================================================================== #
# Derivatives Confluence Override: RVOL 1.5x -> 1.3x under agreeing flow
# =========================================================================== #
def test_141x_rvol_with_179x_put_flow_is_approved_not_vetoed(desk_cfg):
    """IWM Tweezer Top at 1.41x spot volume, 17,900 puts on 1,000 open
    interest (17.9x): the options tape is the participation."""
    put = _contract(OptionRight.PUT, volume=17_900, open_interest=1_000)
    verdict = _convene(desk_cfg, _tweezer(Direction.SHORT, rvol=1.41), [put], put,
                       screen_rvol=1.41)
    technical = next(v for v in verdict.votes if v.agent == "Technical")
    assert not technical.veto, technical.reasons
    assert any("Derivatives Confluence Override" in r for r in technical.reasons)
    assert any("1.5x → 1.3x" in r for r in technical.reasons)
    assert verdict.approved, verdict.reason
    assert verdict.gate is not None and verdict.gate.approved


def test_the_same_setup_without_the_flow_is_vetoed_at_15x(desk_cfg):
    put = _contract(OptionRight.PUT, volume=500, open_interest=1_000)
    verdict = _convene(desk_cfg, _tweezer(Direction.SHORT, rvol=1.41), [put], put,
                       screen_rvol=1.41)
    assert not verdict.approved
    assert "Technical veto" in verdict.reason and "below 1.5x" in verdict.reason


def test_the_override_does_not_reach_below_13x(desk_cfg):
    put = _contract(OptionRight.PUT, volume=17_900, open_interest=1_000)
    verdict = _convene(desk_cfg, _tweezer(Direction.SHORT, rvol=1.25), [put], put,
                       screen_rvol=1.25)
    assert not verdict.approved and "below 1.3x" in verdict.reason


def test_flow_below_10x_does_not_relax_the_gate(desk_cfg):
    put = _contract(OptionRight.PUT, volume=9_000, open_interest=1_000)   # 9x
    flow = consensus.read([put], desk_cfg)
    assert not flow.extreme
    signal = alpha.from_setup(_tweezer(Direction.SHORT, rvol=1.41))
    assert consensus.rvol_floor(desk_cfg, signal, flow)[0] == 1.5


# =========================================================================== #
# Derivatives Flow Conflict Veto: no calls into heavy institutional put buying
# =========================================================================== #
def test_a_bullish_tweezer_bottom_on_iwm_is_vetoed_by_184x_put_flow(desk_cfg):
    """The candlestick is clean and positive, volume is fine — and 18,400 IWM
    puts on 1,000 open interest (18.4x) veto the call outright."""
    call = _contract(OptionRight.CALL, volume=300, open_interest=2_000)
    heavy_puts = _contract(OptionRight.PUT, volume=18_400, open_interest=1_000,
                           strike=SPOT - 2)
    setup = _tweezer(Direction.LONG, rvol=2.2)
    assert alpha.from_setup(setup).confidence_score > 0.5       # a positive chart
    verdict = _convene(desk_cfg, setup, [call, heavy_puts], call, screen_rvol=2.2)

    technical = next(v for v in verdict.votes if v.agent == "Technical")
    derivs = next(v for v in verdict.votes if v.agent == "Derivatives")
    assert not technical.veto and technical.score > 0.5
    assert derivs.veto
    assert "Derivatives Flow Conflict Veto" in derivs.reasons[0]
    assert "18.4x" in derivs.reasons[0] and "no calls" in derivs.reasons[0]
    assert not verdict.approved
    assert "Derivatives Flow Conflict Veto" in verdict.reason
    assert verdict.gate is None                    # never reached the gatekeeper


def test_heavy_call_flow_vetoes_a_put(desk_cfg):
    put = _contract(OptionRight.PUT, volume=300, open_interest=2_000)
    calls = _contract(OptionRight.CALL, volume=15_000, open_interest=1_000,
                      strike=SPOT + 2)
    verdict = _convene(desk_cfg, _tweezer(Direction.SHORT, rvol=2.0), [put, calls], put,
                       screen_rvol=2.0)
    assert not verdict.approved and "no puts" in verdict.reason


def test_a_model_opinion_cannot_lift_the_flow_veto(desk_cfg):
    from panaoptions.agents.base import Opinion, blend

    vote = AgentVote("Derivatives", 0.0, veto=True,
                     reasons=["Derivatives Flow Conflict Veto — ..."])
    blend(vote, Opinion(score=1.0, reason="the chart looks great"), desk_cfg)
    assert vote.veto


# =========================================================================== #
# The Derivatives vote counts double under an extreme anomaly
# =========================================================================== #
def test_the_derivative_weight_doubles_under_extreme_flow(desk_cfg):
    signal = alpha.from_setup(_tweezer(Direction.SHORT, rvol=2.0))
    votes = [AgentVote("Technical", 0.40), AgentVote("Derivatives", 0.90),
             AgentVote("Macro", 0.40)]
    calm = cmio.combine(votes, signal, desk_cfg)
    flow = consensus.read([_contract(OptionRight.PUT, 17_900, 1_000)], desk_cfg)
    loud = cmio.combine(votes, signal, desk_cfg, flow)
    # 0.35/0.35/0.30 weights: (0.14+0.315+0.12)/1.0 = 0.575 calm;
    # derivatives at 0.70: (0.14+0.63+0.12)/1.35 = 0.659 under the anomaly.
    assert calm.committee == pytest.approx(0.575, abs=0.001)
    assert loud.committee == pytest.approx(0.659, abs=0.001)
    assert consensus.derivative_weight_multiplier(desk_cfg, flow) == 2.0
    assert consensus.derivative_weight_multiplier(desk_cfg, consensus.NONE) == 1.0


def test_the_flow_read_names_the_side_and_the_ratio(desk_cfg):
    flow = consensus.read([_contract(OptionRight.PUT, 18_400, 1_000),
                           _contract(OptionRight.CALL, 300, 2_000)], desk_cfg)
    assert flow.bias == -1 and flow.ratio == pytest.approx(18.4)
    assert "extreme put flow" in flow.line() and "18.4x" in flow.line()
    # Equal extreme volume on both sides says nothing about direction.
    both = consensus.read([_contract(OptionRight.PUT, 12_000, 1_000),
                           _contract(OptionRight.CALL, 12_000, 1_000)], desk_cfg)
    assert not both.extreme
