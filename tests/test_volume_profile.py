"""Volume profile in panadhanam: the engine, the three strategies, the Volume
Profile analyst and the CMIO confluence rule.

  1. POC, VAH and VAL from price/volume arrays.
  2. Value Area rejection flags a VAH rejection (put) and a VAL bounce (call).
  3. LVN pocket breakout finds volume gaps and triggers a fast momentum entry.
  plus: the analyst votes with its structural stop, +0.30 composite at a
  level, an HVN wall penalises or vetoes, and a VAL call is not blocked by
  the trend filter.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.agents import consensus
from app.agents.cmio import CMIOAgent
from app.agents.volume_profile import VolumeProfileAgent
from app.core.models import AgentReport, Bias, Candle, MarketContext, Quote
from app.indicators import volume_profile as vpi
from app.strategies import volume_profile_strategies as vps
from app.strategies.volume_profile_strategies import Bar

BELL_PRICES = [98, 99, 100, 101, 102, 103, 104, 105, 106]
BELL_VOLUMES = [2, 5, 15, 20, 30, 18, 12, 4, 2]
PROFILE = {"prior": vpi.from_arrays(BELL_PRICES, BELL_VOLUMES, 1.0)}   # VAL 100 POC 102 VAH 103
GAP = {"prior": vpi.from_arrays([99, 100, 101, 102, 103, 104, 105, 106],
                                [1, 40, 45, 2, 1, 42, 38, 1], 1.0)}  # LVN 101.5-103.5
ATR = 1.0


# =========================================================================== #
# 1. POC, VAH, VAL
# =========================================================================== #
def test_poc_vah_val_from_price_and_volume_arrays():
    prof = vpi.from_arrays([100, 101, 102, 103, 104], [10, 20, 50, 15, 5], 1.0)
    assert prof.poc == 102
    assert (prof.val, prof.vah) == (101, 102)          # 50 + 20 = 70% exactly


def test_the_value_area_holds_70pct():
    prof = PROFILE["prior"]
    inside = sum(v for p, v in zip(BELL_PRICES, BELL_VOLUMES, strict=True)
                 if prof.val <= p <= prof.vah)
    assert (prof.poc, prof.val, prof.vah) == (102, 100, 103)
    assert inside / sum(BELL_VOLUMES) >= 0.70


def test_lvn_pockets_and_hvns():
    prof = GAP["prior"]
    assert [(z.low, z.high) for z in prof.lvns] == [(101.5, 103.5)]
    assert len(prof.hvns) == 2


# =========================================================================== #
# 2. Value Area rejection
# =========================================================================== #
def test_vah_rejection_flags_a_put_to_the_poc():
    bars = [Bar(102.6, 103.0, 102.4, 102.8), Bar(102.8, 103.1, 102.7, 103.0),
            Bar(103.0, 103.9, 102.65, 102.7)]          # poke to 103.9, star back in
    found = vps.detect_value_area_rejection(bars, PROFILE, ATR)
    assert found is not None and found.right == "PUT"
    assert found.target == 102 and found.invalidation == pytest.approx(103.9)


def test_val_bounce_flags_a_call_to_the_poc():
    bars = [Bar(100.8, 100.9, 100.4, 100.5), Bar(100.5, 100.6, 100.2, 100.3),
            Bar(100.3, 100.65, 99.95, 100.6)]          # tests the VAL, hammer
    found = vps.detect_value_area_rejection(bars, PROFILE, ATR)
    assert found is not None and found.right == "CALL"
    assert found.target == 102 and found.invalidation == pytest.approx(99.95)


def test_no_rejection_without_a_rejection_candle():
    bars = [Bar(102.8, 103.1, 102.7, 103.0), Bar(102.9, 103.6, 102.2, 102.9)]  # doji
    assert vps.detect_value_area_rejection(bars, PROFILE, ATR) is None


# =========================================================================== #
# 3. LVN pocket breakout
# =========================================================================== #
def _shelf(level: float) -> list[Bar]:
    return [Bar(level - 0.1, level + 0.1, level - 0.2, level) for _ in range(3)]


def test_lvn_break_up_on_volume_triggers_a_fast_call():
    bars = _shelf(101.3) + [Bar(101.3, 102.3, 101.25, 102.2)]
    found = vps.detect_lvn_acceleration(bars, GAP, ATR, rvol=2.4)
    assert found is not None and found.right == "CALL"
    assert found.target == pytest.approx(103.5)


def test_lvn_break_down_triggers_a_fast_put():
    bars = _shelf(103.7) + [Bar(103.7, 103.75, 102.6, 102.7)]
    found = vps.detect_lvn_acceleration(bars, GAP, ATR, rvol=1.6)
    assert found is not None and found.right == "PUT"


def test_lvn_break_without_volume_does_not_trigger():
    bars = _shelf(101.3) + [Bar(101.3, 102.3, 101.25, 102.2)]
    assert vps.detect_lvn_acceleration(bars, GAP, ATR, rvol=1.4) is None


# =========================================================================== #
# The analyst, on a real two-session US tape
# =========================================================================== #
BELL_UTC = datetime(2026, 9, 22, 13, 30, tzinfo=UTC)      # 09:30 ET


def _tape() -> list[Candle]:
    out = []
    for i in range(78):                                    # prior session, ~100.5
        mid = 100.5 + (0.3 if i % 3 == 0 else -0.3 if i % 3 == 1 else 0.0)
        out.append(Candle(ts=BELL_UTC + timedelta(minutes=5 * i), open=mid,
                          high=mid + 0.25, low=mid - 0.25, close=mid, volume=1000))
    today = BELL_UTC + timedelta(days=1, minutes=30)
    for i, p in enumerate([100.6, 100.5, 100.45, 100.4, 100.35, 100.3, 100.25, 100.2]):
        out.append(Candle(ts=today + timedelta(minutes=5 * i), open=p + 0.03,
                          high=p + 0.08, low=p - 0.08, close=p, volume=1000))
    out.append(Candle(ts=today + timedelta(minutes=40), open=100.3, high=100.42,
                      low=99.9, close=100.4, volume=1500))    # hammer at the prior VAL
    return out


@pytest.fixture
def us(cfg):
    cfg.switch_market("US")
    yield cfg
    cfg.switch_market("IN")


def _ctx(price: float = 100.4, atr: float = 0.43) -> MarketContext:
    return MarketContext(symbol="SPY", cycle_id="t",
                         quote=Quote(symbol="SPY", last_price=price),
                         candles={"5m": _tape()},
                         indicators={"primary": {"atr": atr, "above_vwap": False},
                                     "by_timeframe": {"15m": {"ema_stacked_bear": True}}})


def test_the_analyst_votes_bullish_at_the_val_with_its_stop(us):
    report = VolumeProfileAgent(us).analyse_rules(_ctx())
    assert report.data_available and report.bias == Bias.BULLISH
    assert report.score == pytest.approx(0.60)
    assert report.extra["setup"] == "va_rejection"
    assert report.extra["level_name"] == "prior VAL"
    assert report.invalidation_level == pytest.approx(99.9)
    assert {"prior", "current"} <= set(report.extra["profiles"])


def test_no_trigger_is_silence_not_a_vote(us):
    ctx = _ctx()
    ctx.candles["5m"] = ctx.candles["5m"][:-1]                 # no hammer yet
    report = VolumeProfileAgent(us).analyse_rules(ctx)
    assert report.score == 0.0 and report.data_available
    assert "profiles" in report.extra


def test_no_candles_abstains(us):
    report = VolumeProfileAgent(us).analyse_rules(MarketContext(symbol="SPY", cycle_id="t"))
    assert report.data_available is False


# =========================================================================== #
# The CMIO: +0.30 at a level, HVN walls, the trend filter
# =========================================================================== #
def _r(agent: str, score: float, **kw) -> AgentReport:
    bias = Bias.BULLISH if score > 0 else Bias.BEARISH if score < 0 else Bias.NEUTRAL
    return AgentReport(agent_id=agent, symbol="SPY", bias=bias, score=score,
                       confidence=0.8, **kw)


def test_a_long_at_the_val_gains_030_in_the_composite(us):
    ctx = _ctx()
    val = vps.profiles_for(ctx.candles["5m"], us)["prior"].val
    ctx.quote = Quote(symbol="SPY", last_price=val)
    adjusted, notes, veto = consensus.volume_profile_confluence(
        ctx, [_r("candlestick", 0.4)], 0.30, us)
    assert adjusted == pytest.approx(0.60) and not veto
    assert any("VAL" in n and "+0.30" in n for n in notes)


def test_a_short_at_the_val_gains_nothing():
    assert vps.level_alignment(100.1, +1, PROFILE, ATR) == "prior VAL 100.00"
    assert vps.level_alignment(100.1, -1, PROFILE, ATR) == ""
    assert vps.level_alignment(102.9, -1, PROFILE, ATR) == "prior VAH 103.00"


def test_the_hvn_wall_rules():
    # HVN 103.5-105.5 ahead of a long.
    assert vps.hvn_wall(103.4, +1, GAP, ATR)[0] == "veto"
    assert vps.hvn_wall(102.8, +1, GAP, ATR)[0] == "penalty"
    assert vps.hvn_wall(101.9, +1, GAP, ATR)[0] == ""
    assert vps.hvn_wall(103.4, +1, GAP, ATR, target=104.5)[0] == ""   # the target
    # A one-bin node holding under 10% of the session is a sliver, not a wall.
    prices = list(range(90, 111))
    volumes = [10] * len(prices)
    volumes[prices.index(103)] = 16                       # HVN, 16/216 = 7.4%
    sliver = {"prior": vpi.from_arrays(prices, volumes, 1.0)}
    assert any(z.low == 102.5 for z in sliver["prior"].hvns)
    assert vps.hvn_wall(102.2, +1, sliver, ATR)[0] == ""
    assert vps.hvn_wall(102.2, +1, sliver, ATR, min_share=0.0)[0] == "penalty"


def test_a_val_call_is_not_blocked_by_the_trend_filter(us):
    """Below VWAP and into a falling 15m trend — exactly where a VAL bounce
    happens. The volume-profile vote makes the trend filter stand aside."""
    ctx = _ctx()
    reports = [VolumeProfileAgent(us).analyse_rules(ctx), _r("candlestick", 0.10)]
    decision = CMIOAgent(us)._weighted_vote(ctx, reports)
    assert decision["proceed"] is True, decision["rationale"]
    assert any("Trend filter stood aside" in c for c in decision["conflicts"])

    # Without the volume-profile setup the same long is refused as counter-trend.
    decision = CMIOAgent(us)._weighted_vote(ctx, [_r("candlestick", 0.60)])
    assert decision["proceed"] is False


def test_the_volume_profile_analyst_can_lead_a_trade(cfg):
    assert "volume_profile" in cfg.get("consensus.lead_analysts")
    assert "volume_profile" in cfg.enabled_analysts()
