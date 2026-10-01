"""Larry Williams' volatility breakout: the first close beyond today's open
+/- k x yesterday's range, with VWAP and the EMAs, stop back toward the open."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from panaoptions.engine import indicators as ta
from panaoptions.engine.strategies import ALL, VolatilityBreakout, localise
from panaoptions.models import Candle, Direction, SessionLevels

LEVELS = SessionLevels(previous_high=102.0, previous_low=98.0, previous_close=100.0)


def _frame(today_closes, yesterday=100.0):
    """Yesterday flat at 100, then today's 5m bars from 09:30 ET (13:30 UTC)."""
    out = []
    y0 = datetime(2026, 9, 28, 13, 30, tzinfo=UTC)
    for i in range(78):
        out.append(Candle(ts=y0 + timedelta(minutes=5 * i), open=yesterday,
                          high=yesterday + 0.2, low=yesterday - 0.2, close=yesterday,
                          volume=1000))
    t0 = datetime(2026, 9, 29, 13, 30, tzinfo=UTC)
    prev = 100.0
    for i, c in enumerate(today_closes):
        out.append(Candle(ts=t0 + timedelta(minutes=5 * i), open=prev,
                          high=max(prev, c) + 0.05, low=min(prev, c) - 0.05, close=c,
                          volume=1500))
        prev = c
    df = localise(ta.to_frame(out), "America/New_York")
    return df, ta.resample(df, "15min")


def _eval(cfg, closes, levels=LEVELS):
    df5, df15 = _frame(closes)
    return VolatilityBreakout(cfg).evaluate("SPY", df5, df15, levels)


def test_it_is_registered_and_ships_off_until_the_walk_forward_says_so(cfg, shipped):
    assert VolatilityBreakout in ALL
    assert shipped.get("strategies.volatility_breakout.enabled") is False


def test_the_first_close_above_open_plus_half_the_range_is_a_call(cfg):
    # open 100, range 4, k 0.5 -> the line is 102.00
    s = _eval(cfg, [100.2, 100.6, 101.0, 101.4, 101.8, 101.95, 102.3])
    assert s.triggered and s.direction is Direction.LONG
    assert s.key_level == 102.0 and s.entry_trigger == 102.3
    # stop at the open (stop_fraction 1.0), target 3R
    assert s.underlying_support == 100.0
    assert round(s.underlying_target, 2) == round(102.3 + 3 * 2.3, 2)
    assert "yesterday's range 4.00" in s.confirmations[0]


def test_a_level_crossed_earlier_is_a_chase_not_a_breakout(cfg):
    s = _eval(cfg, [100.4, 101.0, 101.6, 102.2, 102.4, 102.6])
    assert not s.triggered and "no fresh 5m close" in s.blockers[0]


def test_the_mirror_below_is_a_put_with_the_stop_halfway_back(cfg):
    cfg.data["strategies"]["volatility_breakout"]["stop_fraction"] = 0.5
    s = _eval(cfg, [99.8, 99.4, 99.0, 98.6, 98.2, 98.05, 97.7])
    assert s.triggered and s.direction is Direction.SHORT
    assert s.key_level == 98.0
    assert s.underlying_support == round(97.7 + 0.5 * 2.3, 4)


def test_no_previous_range_no_setup(cfg):
    s = _eval(cfg, [100.2, 102.3], levels=SessionLevels())
    assert not s.triggered and "previous-day range" in s.blockers[0]


def test_k_comes_from_the_config(cfg):
    cfg.data["strategies"]["volatility_breakout"]["k"] = 0.25     # line at 101.00
    s = _eval(cfg, [100.2, 100.6, 100.9, 101.2])
    assert s.triggered and s.key_level == 101.0


def test_the_losing_strategies_are_off_per_market(shipped):
    """1 Oct 2026, 20-session walk-forward: ORB off on the US, the VWAP
    pullback off on India; both beat the old rules in both halves."""
    from panaoptions.config import Config
    assert shipped.get("strategies.orb_vwap.enabled") is False
    assert shipped.get("strategies.vwap_ema_pullback.enabled") is True
    india = Config(market="IN")
    assert india.get("strategies.vwap_ema_pullback.enabled") is False
    assert india.get("strategies.pd_liquidity_sweep.enabled") is True
    for profile in ("scalp", "zerodte"):
        assert Config(profile=profile).get("strategies.orb_vwap.enabled") is True
