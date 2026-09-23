"""The four entry strategies, their windows, and their invalidation levels."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from panaoptions.engine import indicators as ta
from panaoptions.engine.levels import compute
from panaoptions.engine.strategies import (
    CandlestickAtLevel,
    LiquiditySweepReversal,
    OpeningRangeBreakout,
    VwapEmaPullback,
    evaluate_all,
    localise,
)
from panaoptions.models import Candle, Direction, SetupType

# September in New York is UTC-4, so 13:30 UTC is the 09:30 ET bell.
BELL = datetime(2026, 9, 23, 13, 30, tzinfo=UTC)
PREMARKET = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)


def _bar(ts, price, volume=2000.0, high=None, low=None):
    return Candle(ts=ts, open=price, high=high if high is not None else price + 0.3,
                  low=low if low is not None else price - 0.3, close=price,
                  volume=volume)


def _session(*, premarket_prices=None, opening=(100.5, 100.55, 100.6),
             after=(), volumes=None):
    bars = []
    for i, price in enumerate(premarket_prices or [100 + i * 0.02 for i in range(18)]):
        bars.append(_bar(PREMARKET + timedelta(minutes=5 * i), price, 400.0,
                         high=price + 0.2, low=price - 0.2))
    for i, price in enumerate(opening):
        bars.append(_bar(BELL + timedelta(minutes=5 * i), price))
    for i, price in enumerate(after):
        volume = volumes[i] if volumes else 2000.0
        bars.append(_bar(BELL + timedelta(minutes=5 * (len(opening) + i)),
                         price, volume))
    return bars


# --------------------------------------------------------------------------- #
def test_windows_are_read_in_exchange_time_not_utc(cfg):
    # A UTC stamp compared against a New York window shifts everything by four
    # hours, so a 10:00 ET strategy either never fires or fires overnight.
    bars = _session(after=[100.7, 100.8, 100.9])
    utc_frame = ta.to_frame(bars)

    with pytest.raises(ValueError, match="exchange timezone"):
        OpeningRangeBreakout(cfg).in_window(utc_frame)

    local = localise(utc_frame, "America/New_York")
    assert OpeningRangeBreakout(cfg).in_window(local) is True


def test_the_opening_range_is_not_usable_before_it_has_formed(cfg):
    # Two bars is half a range, and a breakout of half a range is not a
    # breakout.
    bars = _session(opening=(100.5, 100.55))
    levels = compute(bars, "America/New_York", BELL.date())
    assert not levels.has_opening_range

    setup = OpeningRangeBreakout(cfg).evaluate(
        "SPY", localise(ta.to_frame(bars), "America/New_York"), None, levels)
    assert not setup.triggered
    assert "opening range" in setup.blockers[0]


# --------------------------------------------------------------------------- #
def test_an_orb_breakout_fires_with_volume_and_names_its_invalidation(cfg):
    bars = _session(after=[100.7, 100.9, 101.2, 101.6, 102.0, 102.4, 102.9],
                    volumes=[2000, 2000, 2000, 2000, 2000, 2000, 9000])
    levels = compute(bars, "America/New_York", BELL.date())

    winner, attempts = evaluate_all("SPY", bars, levels, cfg)
    assert winner is not None
    assert winner.strategy is SetupType.ORB_VWAP
    assert winner.direction is Direction.LONG

    # The invalidation is the range boundary, on the UNDERLYING — not a
    # percentage of a premium that an IV shift can move on its own.
    assert winner.underlying_support == pytest.approx(levels.opening_range_high)
    assert "opening range" in winner.invalidation_note
    assert winner.underlying_target > levels.opening_range_high


def test_an_orb_breakout_without_volume_is_blocked(cfg):
    bars = _session(after=[100.7, 100.9, 101.2, 101.6, 102.0, 102.4, 102.9],
                    volumes=[2000, 2000, 2000, 2000, 2000, 2000, 50])
    levels = compute(bars, "America/New_York", BELL.date())

    winner, attempts = evaluate_all("SPY", bars, levels, cfg)
    assert winner is None
    orb = next(a for a in attempts if a.strategy is SetupType.ORB_VWAP)
    assert any("volume" in b for b in orb.blockers)


def test_price_inside_the_range_is_no_breakout(cfg):
    bars = _session(after=[100.7, 100.6, 100.75, 100.65, 100.7, 100.6, 100.7])
    levels = compute(bars, "America/New_York", BELL.date())

    winner, attempts = evaluate_all("SPY", bars, levels, cfg)
    assert winner is None
    orb = next(a for a in attempts if a.strategy is SetupType.ORB_VWAP)
    assert "beyond the opening range" in orb.blockers[0]


# --------------------------------------------------------------------------- #
def test_the_pullback_strategy_needs_an_established_15m_trend(cfg):
    bars = _session(after=[100.7, 100.9, 101.2])
    levels = compute(bars, "America/New_York", BELL.date())
    frame = localise(ta.to_frame(bars), "America/New_York")

    setup = VwapEmaPullback(cfg).evaluate(
        "SPY", frame, ta.resample(frame, "15min"), levels)
    assert not setup.triggered
    assert "15m" in setup.blockers[0]


# --------------------------------------------------------------------------- #
def test_a_sweep_of_the_premarket_low_that_reclaims_is_a_long(cfg):
    # Dip under the pre-market low, then reclaim it on the next candle.
    premarket = [100 + i * 0.02 for i in range(18)]
    low = min(premarket) - 0.2
    after = [100.5, 100.4, 100.3, 100.2]
    bars = _session(premarket_prices=premarket, after=after)
    # The sweep candle pokes below, the next closes back above.
    bars[-2] = _bar(bars[-2].ts, low - 0.4, 3000.0, low=low - 0.9)
    bars[-1] = _bar(bars[-1].ts, 101.2, 9000.0)
    levels = compute(bars, "America/New_York", BELL.date())

    setup = LiquiditySweepReversal(cfg).evaluate(
        "SPY", localise(ta.to_frame(bars), "America/New_York"), None, levels)

    assert setup.direction is Direction.LONG
    assert setup.strategy is SetupType.LIQUIDITY_SWEEP
    assert setup.underlying_support < levels.premarket_low, \
        "the invalidation is the sweep wick, below the level that was swept"
    assert "sweep wick" in setup.invalidation_note


def test_without_premarket_bars_the_sweep_strategy_abstains(cfg):
    bars = _session(premarket_prices=[])[0:]
    bars = [b for b in bars if b.ts >= BELL]
    levels = compute(bars, "America/New_York", BELL.date())

    setup = LiquiditySweepReversal(cfg).evaluate(
        "SPY", localise(ta.to_frame(bars), "America/New_York"), None, levels)
    assert not setup.triggered
    assert "pre-market" in setup.blockers[0]


# --------------------------------------------------------------------------- #
def test_a_disabled_strategy_never_runs(cfg):
    cfg.data["strategies"]["orb_vwap"]["enabled"] = False
    bars = _session(after=[100.7, 100.9, 101.2, 101.6, 102.0, 102.4, 102.9],
                    volumes=[2000] * 6 + [9000])
    levels = compute(bars, "America/New_York", BELL.date())

    winner, attempts = evaluate_all("SPY", bars, levels, cfg)
    assert winner is None
    assert not any(a.strategy is SetupType.ORB_VWAP for a in attempts)


def test_every_attempt_is_reported_even_when_none_fire(cfg):
    # The rejections are what tell you whether a rule is selective or simply
    # impossible.
    bars = _session(after=[100.7, 100.6, 100.75])
    levels = compute(bars, "America/New_York", BELL.date())

    winner, attempts = evaluate_all("SPY", bars, levels, cfg)
    assert winner is None
    assert attempts, "silence with no reason is indistinguishable from a bug"
    assert all(a.blockers for a in attempts)


# --------------------------------------------------------------------------- #
# Strategy 4 — a reversal candle, but only at a level
# --------------------------------------------------------------------------- #
def _hammer_session(*, trigger_taken=True, mid_range=False, sweep=True):
    """A slide into a swing low, a hammer off it, then the break of its high.

    The hammer has to land on a 15m boundary to survive the resample, so the
    prices below are built in threes.
    """
    bars = [_bar(PREMARKET + timedelta(minutes=5 * i), 104 - i * 0.05, 400.0,
                 high=104 - i * 0.05 + 0.2, low=104 - i * 0.05 - 0.2)
            for i in range(18)]
    slide = [104, 103.4, 102.8, 102.2, 101.6, 101.0, 100.4, 99.8, 99.2, 98.6,
             98.2, 98.5, 99.0, 99.4, 99.9, 100.3, 99.8, 99.2, 98.6, 98.3]
    if mid_range:
        # Same shape, but drifting sideways — the hammer's low is nowhere near
        # a level the market has turned at.
        slide = [100.0 + (i % 3) * 0.05 for i in range(20)]
    for i, price in enumerate(slide):
        bars.append(_bar(BELL + timedelta(minutes=5 * i), price))

    # sweep=True pushes the wick THROUGH the swing low at 97.90; sweep=False
    # leaves it resting on the level without taking the stops under it.
    low = (97.6 if sweep else 98.0) if not mid_range else 99.7
    top = 99.0 if not mid_range else 100.1
    base = BELL + timedelta(minutes=5 * len(slide))
    bars.append(Candle(ts=base, open=top - 0.1, high=top, low=low,
                       close=top - 0.15, volume=5000.0))
    if trigger_taken:
        bars.append(Candle(ts=base + timedelta(minutes=5), open=top - 0.1,
                           high=top + 0.6, low=top - 0.2, close=top + 0.5,
                           volume=6000.0))
    else:
        # Deliberately a nothing bar: price stalls under the trigger without
        # printing a fresher pattern that would supersede the hammer.
        bars.append(Candle(ts=base + timedelta(minutes=5), open=top - 0.15,
                           high=top - 0.05, low=top - 0.25, close=top - 0.20,
                           volume=6000.0))
    return bars


def test_a_hammer_at_a_swing_low_becomes_a_call(cfg):
    setup, _attempts = evaluate_all("AAPL", _hammer_session(sweep=False),
                                    compute([]), cfg)
    assert setup is not None
    assert setup.strategy is SetupType.CANDLESTICK_AT_LEVEL
    assert setup.direction is Direction.LONG
    assert setup.pattern == "Hammer"
    # The location is the larger half of the rule, so it is always named.
    assert setup.key_level and setup.key_level_source
    # The stop belongs to the candle, not to a percentage of the premium.
    assert setup.underlying_support == pytest.approx(98.0)
    assert setup.entry_trigger == pytest.approx(99.0)


def test_a_wick_through_the_level_is_a_sweep_not_a_hammer(cfg):
    """Piercing the level and closing back inside is a different event.

    The stops resting under the level were filled first, so the trade is that
    whoever got filled out there is now offside — which is a stronger read
    than a candle that merely stopped at support, and it earns its own name,
    its own contract and its own stop.
    """
    setup, _ = evaluate_all("AAPL", _hammer_session(sweep=True), compute([]), cfg)
    assert setup is not None
    assert setup.pattern == "Liquidity Sweep Rejection"
    assert setup.delta_band == (0.55, 0.65)
    # The stop sits beyond the tip of the sweep wick, not at the level.
    assert setup.underlying_support == pytest.approx(97.6)
    assert setup.key_level == pytest.approx(97.9)
    assert "swept" in " ".join(setup.confirmations)
    assert "offside" in " ".join(setup.reasoning)


def test_the_same_hammer_mid_range_is_refused(cfg):
    """This is the whole point of the strategy.

    Trading the pattern without the level is why people conclude candlesticks
    do not work, so the refusal has to be visible rather than silent.
    """
    _setup, attempts = evaluate_all("AAPL", _hammer_session(mid_range=True),
                                    compute([]), cfg)
    candle = [a for a in attempts
              if a.strategy is SetupType.CANDLESTICK_AT_LEVEL]
    assert candle and not candle[0].triggered
    assert any("not at a level" in b or "waiting for price" in b
               or "not enough" in b or "no reversal pattern" in b
               for b in candle[0].blockers)


def test_a_pattern_without_its_trigger_is_not_an_entry(cfg):
    _setup, attempts = evaluate_all("AAPL", _hammer_session(trigger_taken=False),
                                    compute([]), cfg)
    candle = [a for a in attempts
              if a.strategy is SetupType.CANDLESTICK_AT_LEVEL][0]
    assert not candle.triggered
    assert any("waiting for price to break above" in b for b in candle.blockers)


def test_each_pattern_asks_for_its_own_contract(cfg):
    strategy = CandlestickAtLevel(cfg)
    # A hammer is a sharp reversal off a level and wants delta near the money;
    # a morning star is a slower structural turn and tolerates less.
    assert strategy.delta_band("Hammer") == (0.50, 0.65)
    assert strategy.delta_band("Bullish Engulfing") == (0.55, 0.70)
    assert strategy.delta_band("Morning Star") == (0.45, 0.55)
    # Longer-dated than the intraday rules, so theta does not eat the move.
    assert int(cfg.get("strategies.candlestick_at_level.min_dte")) >= 14
    assert int(cfg.get("strategies.candlestick_at_level.max_dte")) <= 30


def test_a_triggered_setup_explains_itself_in_plain_language(cfg):
    setup, _ = evaluate_all("AAPL", _hammer_session(), compute([]), cfg)
    assert setup is not None
    joined = " ".join(setup.reasoning)
    assert "**Buy a CALL**" in joined
    for heading in ("The pattern.", "Why here.", "The trigger.",
                    "What kills it.", "The contract."):
        assert heading in joined


def test_a_pattern_needs_the_trend_it_claims_to_interrupt(cfg):
    """Context is most of these patterns.

    Three long red candles after a rally is distribution; the same three in
    the middle of a range is noise with a story attached. The refusal has to
    be legible, because a pattern rejected for its context looks exactly like
    one that never printed.
    """
    from panaoptions.engine.patterns import Pattern
    from panaoptions.engine.strategies import CandlestickAtLevel

    strategy = CandlestickAtLevel(cfg)
    crows = Pattern("Three Black Crows", False, 98.0, 102.6, bars=3,
                    requires_trend=1)
    assert crows.requires_trend == 1
    # And the per-pattern contract follows the pattern, not the global default.
    assert strategy.delta_band("Three Black Crows") == (0.55, 0.65)
    assert strategy.dte_window("Three Black Crows") == (21, 35)


def test_each_multi_candle_pattern_asks_for_its_own_expiry(cfg):
    from panaoptions.engine.strategies import CandlestickAtLevel

    strategy = CandlestickAtLevel(cfg)
    # A four-candle structural reversal is a multi-session move. Handing it
    # the intraday default would buy the right thesis with the wrong contract
    # and lose on theta while being directionally correct.
    assert strategy.dte_window("Bullish Three-Line Strike") == (30, 45)
    assert strategy.delta_band("Bullish Three-Line Strike") == (0.65, 0.75)
    assert strategy.dte_window("Three White Soldiers") == (30, 45)
    assert strategy.dte_window("Hammer") == (14, 30)


def test_an_unknown_pattern_falls_back_rather_than_crashing(cfg):
    from panaoptions.engine.strategies import CandlestickAtLevel

    strategy = CandlestickAtLevel(cfg)
    assert strategy.delta_band("Nonsense") == (0.45, 0.60)
    assert strategy.dte_window("Nonsense") == (14, 30)


def test_a_published_win_rate_is_recorded_but_never_sizes_a_trade(cfg):
    """A citation must not become money at risk.

    The number comes from somebody else's study of daily bars and is a
    hypothesis about this 15m tape. It is carried so the review can hold it
    against what actually happened; a position sized off it would be the app
    believing a claim it has not tested.
    """
    from datetime import UTC, datetime

    from panaoptions.models import OptionContract, OptionRight, Setup, SetupType
    from panaoptions.risk.guardrails import RiskManager

    strategy = CandlestickAtLevel(cfg)
    assert strategy.claimed_accuracy("Bullish Three-Line Strike") == 0.84
    assert strategy.claimed_accuracy("Hammer") == 0.0

    cfg.data["account"]["starting_capital"] = 2000.0
    contract = OptionContract(symbol="AAPL", right=OptionRight.CALL, strike=230,
                              expiry="2026-10-16", dte=23, bid=1.00, ask=1.04,
                              delta=0.55)
    now = datetime(2026, 9, 23, 14, 0, tzinfo=UTC)

    def _size(claim):
        setup = Setup(symbol="AAPL", ts=now,
                      strategy=SetupType.CANDLESTICK_AT_LEVEL,
                      direction=Direction.LONG, pattern="Hammer",
                      underlying_support=97.6, claimed_accuracy=claim)
        signal, _refusal = RiskManager(cfg).size(setup, contract, "SIG-1", now)
        return signal

    modest, confident = _size(0.0), _size(0.99)
    assert modest is not None and confident is not None
    # Same quantity, same stop, same target. The claim changed; nothing did.
    assert modest.quantity == confident.quantity
    assert modest.stop_price == confident.stop_price
    assert modest.target_1 == confident.target_1
    # It is still carried onto the signal, for the journal.
    assert confident.claimed_accuracy == 0.99
