"""Profiles: the same tape, two desks, two different answers.

The default profile reads 5m/15m bars and buys 7-45 day contracts. On a
quiet index ETF grinding through a $3 range it correctly does nothing — the
moves it is built to trade are not there. The scalp profile reads 1-minute
bars and buys same-day contracts, where those same moves ARE the trade.

These tests pin that difference down with one tape, because "the desk took
nothing" and "the desk is broken" look identical from an empty position list.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from panaoptions.config import Config, available_profiles
from panaoptions.data.premarket import screen
from panaoptions.models import Candle

# 2026-09-23. September in New York is UTC-4, so 13:30 UTC is the 09:30 bell.
BELL = datetime(2026, 9, 23, 13, 30, tzinfo=UTC)


def _minute(offset: int) -> datetime:
    return BELL + timedelta(minutes=offset)


def qqq_afternoon() -> list[Candle]:
    """QQQ, 1-minute, the afternoon in the screenshot.

    Session low 738.50 around 13:08, a grind up to 741.30 by 13:54, a flush
    to 739.75, a retest that fails, and a drift out. A $2.80 range on a $740
    name: 0.38%, which is the point — it is a completely ordinary afternoon,
    and that is what makes it the right test.
    """
    shape = [
        # (minutes after the bell, close) — the turning points of that tape.
        (0, 741.5), (60, 740.2), (150, 739.4), (210, 738.9),
        (218, 738.5),                      # the session low
        (230, 739.6), (245, 740.2),        # the reclaim
        (248, 739.4), (255, 740.0),        # the higher low
        (264, 741.3),                      # the afternoon high
        (272, 739.75),                     # the flush
        (280, 741.1), (290, 741.2),        # the retest
        (302, 740.2), (308, 741.15),
        (330, 740.7), (336, 740.82),
    ]
    bars: list[Candle] = []
    for (start, price_a), (end, price_b) in zip(shape, shape[1:], strict=False):
        span = max(end - start, 1)
        for i in range(span):
            price = price_a + (price_b - price_a) * i / span
            nxt = price_a + (price_b - price_a) * (i + 1) / span
            bars.append(Candle(
                ts=_minute(start + i), open=price,
                high=max(price, nxt) + 0.06, low=min(price, nxt) - 0.06,
                close=nxt, volume=90_000.0))
    return bars


class _QQQFeed:
    """Yesterday's close 747.46, so the day is -0.89% — the screenshot."""

    async def connect(self):
        return True

    async def quote(self, symbol):
        return {"previous_close": 747.46, "last_price": 740.82}

    async def candles(self, symbol, interval="5m", include_prepost=False):
        if interval == "1d":
            return [Candle(ts=BELL - timedelta(days=d), open=747, high=749,
                           low=745, close=747.46, volume=40_000_000.0)
                    for d in range(20, 0, -1)]
        return qqq_afternoon()


# --------------------------------------------------------------------------- #
def test_the_scalp_profile_exists_and_is_a_different_desk():
    assert "scalp" in available_profiles()
    default, scalp = Config(), Config(profile="scalp")

    assert default.get("technical.timeframe") == "5m"
    assert scalp.get("technical.timeframe") == "1m"
    assert default.get("contracts.min_dte") == 7
    assert scalp.get("contracts.min_dte") == 0
    assert scalp.get("contracts.max_dte") == 1
    # And it is never ambiguous which one is running.
    assert default.profile_label == "default"
    assert scalp.profile_label == "scalp"


def test_a_profile_only_changes_what_it_names():
    """A shallow merge would wipe every sibling setting it did not restate."""
    scalp = Config(profile="scalp")
    # Named in the profile.
    assert scalp.get("contracts.min_dte") == 0
    # Not named anywhere in it, and must survive.
    assert scalp.get("contracts.contract_multiplier") == 100
    assert scalp.get("technical.fast_ema") == 9
    assert scalp.get("journal.auto_grade") is True


def test_an_unknown_profile_is_an_error_not_a_shrug():
    """Silently running the default while believing you selected another desk
    is the single most expensive way this could fail."""
    with pytest.raises(FileNotFoundError) as excinfo:
        Config(profile="nonesuch")
    assert "scalp" in str(excinfo.value)      # it names what does exist


# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_the_default_screen_rejects_a_quiet_index_etf():
    """This is why no QQQ trade was ever taken, and it is gate number one.

    QQQ closed -0.89%. The default screen wants a catalyst — a move of at
    least 1% — so QQQ never reaches a strategy at all. Every rule downstream
    is irrelevant; nothing downstream ran.
    """
    cfg = Config()
    reads = await screen(_QQQFeed(), cfg, datetime(2026, 9, 23, 17, 30, tzinfo=UTC))
    qqq = next(r for r in reads if r.symbol == "QQQ")
    assert qqq.gap_pct == pytest.approx(-0.89, abs=0.01)
    assert not qqq.passed
    assert any("inside the |1.0|% threshold" in r for r in qqq.reasons)


@pytest.mark.asyncio
async def test_the_scalp_screen_lets_that_same_tape_through():
    cfg = Config(profile="scalp")
    reads = await screen(_QQQFeed(), cfg, datetime(2026, 9, 23, 17, 30, tzinfo=UTC))
    qqq = next(r for r in reads if r.symbol == "QQQ")
    assert qqq.passed, qqq.reasons


# --------------------------------------------------------------------------- #
def test_a_dollar_move_is_below_the_default_desks_resolution():
    """Even past the screen, the default desk cannot see these setups.

    The patterns are read on 15m bars. The whole afternoon is 22 of them, and
    a $1.10 swing inside a $2.80 range does not print a 15m reversal at a
    level. This is not a bug to fix in the default profile — it is what a
    swing desk correctly ignores.
    """
    from panaoptions.engine import indicators as ta
    from panaoptions.engine.strategies import localise

    bars = qqq_afternoon()
    df = localise(ta.to_frame(bars), "America/New_York")
    fifteens = ta.resample(df, "15min")
    assert len(bars) > 300            # a whole afternoon of 1m bars...
    assert len(fifteens) < 30         # ...is barely two dozen 15m candles
    span = float(df["high"].max() - df["low"].min())
    assert span < 3.5                 # $2.80 on a $740 name: 0.38%


def test_the_scalp_profile_can_afford_what_it_asks_for():
    """The default desk's problem in reverse.

    At 14-30 DTE and 0.50 delta a QQQ contract is several hundred dollars
    against a $400 budget. Same-day contracts on the same name are tens of
    dollars, which is what makes this account able to trade this tape at all.
    """
    from panaoptions import preflight

    scalp = Config(profile="scalp")
    scalp.data["account"]["starting_capital"] = 2000.0
    assert not [f for f in preflight.check(scalp)
                if f.setting == "strategies.candlestick_at_level.patterns"]


def test_the_scalp_profile_tightens_risk_rather_than_loosening_it():
    """More trades at this speed must not mean more risk per trade.

    0-DTE gamma cuts both ways: the +120% moves are real and so is -100% from
    the same size, so the per-trade budget comes down, not up.
    """
    default, scalp = Config(), Config(profile="scalp")
    assert (float(scalp.get("risk.max_capital_deployed_pct"))
            < float(default.get("risk.max_capital_deployed_pct")))
    assert (float(scalp.get("risk.disaster_stop_pct"))
            < float(default.get("risk.disaster_stop_pct")))
    assert (float(scalp.get("risk.daily_loss_limit_pct"))
            < float(default.get("risk.daily_loss_limit_pct")))
    # The stop is still the level on the underlying, not a premium percentage.
    assert scalp.get("risk.stop_mode") == "underlying"
    # And nothing may be held into expiry.
    assert scalp.get("session.force_exit_at") < "15:45"
