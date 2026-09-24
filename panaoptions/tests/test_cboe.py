"""CBOE delayed chains: no account, no key, greeks included.

All offline — the recorded response shape is the contract, so if CBOE
changes it that shows up as a failing test rather than as a quiet day where
nothing trades.
"""
from __future__ import annotations

from datetime import date

import pytest

from panaoptions.data.cboe import CboeChains, cboe_symbol, parse_chain, parse_occ
from panaoptions.models import OptionRight


# --------------------------------------------------------------------------- #
# The OCC symbol
# --------------------------------------------------------------------------- #
def test_the_strike_is_in_thousandths():
    """A naive int() is out by a factor of a thousand.

    Every contract would look absurdly far out of the money and the delta
    filter would reject the whole chain — a silent, total failure.
    """
    parsed = parse_occ("AAPL261016C00350000")
    assert parsed is not None
    expiry, right, strike = parsed
    assert strike == 350.0
    assert expiry == date(2026, 10, 16)
    assert right is OptionRight.CALL


def test_a_fractional_strike_survives():
    parsed = parse_occ("SPY261016P00612500")
    assert parsed is not None
    assert parsed[2] == 612.5
    assert parsed[1] is OptionRight.PUT


@pytest.mark.parametrize("occ", [
    "", "NOTANOPTION", "AAPL26106C00350000",      # short date
    "AAPL261016X00350000",                        # not a call or a put
    "AAPL261332C00350000",                        # month 13
])
def test_rubbish_is_refused_rather_than_guessed(occ):
    assert parse_occ(occ) is None


def test_a_short_root_still_parses():
    parsed = parse_occ("F261016C00012000")
    assert parsed is not None and parsed[2] == 12.0


# --------------------------------------------------------------------------- #
# Index roots
# --------------------------------------------------------------------------- #
def test_cash_settled_indices_use_an_underscore_root():
    assert cboe_symbol("SPX") == "_SPX"
    assert cboe_symbol("^VIX") == "_VIX"
    assert cboe_symbol("SPY") == "SPY"
    assert cboe_symbol("aapl") == "AAPL"


# --------------------------------------------------------------------------- #
# The chain
# --------------------------------------------------------------------------- #
def _payload():
    return {"timestamp": "2026-09-24 16:15:00", "symbol": "AAPL",
            "data": {"current_price": 337.37, "options": [
                {"option": "AAPL261016C00340000", "bid": 12.10, "ask": 12.35,
                 "iv": 0.2814, "delta": 0.5213, "open_interest": 4180,
                 "volume": 912},
                {"option": "AAPL261016P00340000", "bid": 14.00, "ask": 14.25,
                 "iv": 0.2902, "delta": -0.4787, "open_interest": 2210,
                 "volume": 455},
                {"option": "AAPL261120C00350000", "bid": 9.50, "ask": 9.75,
                 "iv": 0.2750, "delta": 0.4401, "open_interest": 880,
                 "volume": 120},
            ]}}


def test_the_greeks_come_from_cboe_not_from_an_estimate():
    """The whole reason this beats Yahoo.

    Yahoo serves no greeks at all, so that path computes delta here with
    Black-Scholes — a reasonable estimate that every delta band in the config
    is then applied to.
    """
    chain = parse_chain(_payload(), "AAPL", today=date(2026, 9, 24))
    call = next(c for c in chain if c.right is OptionRight.CALL
                and c.strike == 340)
    assert call.delta == pytest.approx(0.5213)
    assert call.implied_volatility == pytest.approx(0.2814)
    assert call.bid == 12.10 and call.ask == 12.35
    assert call.open_interest == 4180


def test_a_put_keeps_its_negative_delta():
    chain = parse_chain(_payload(), "AAPL", today=date(2026, 9, 24))
    put = next(c for c in chain if c.right is OptionRight.PUT)
    assert put.delta == pytest.approx(-0.4787)


def test_dte_is_measured_from_today_not_from_the_response():
    chain = parse_chain(_payload(), "AAPL", today=date(2026, 9, 24))
    near = next(c for c in chain if c.expiry == "2026-10-16")
    far = next(c for c in chain if c.expiry == "2026-11-20")
    assert near.dte == 22 and far.dte == 57


def test_an_unparseable_row_is_skipped_not_fatal():
    payload = {"data": {"options": [
        {"option": "GARBAGE", "bid": 1, "ask": 2},
        {"option": "AAPL261016C00340000", "bid": 12.1, "ask": 12.35},
        "not even a dict",
    ]}}
    chain = parse_chain(payload, "AAPL", today=date(2026, 9, 24))
    assert len(chain) == 1


def test_an_empty_or_broken_payload_is_an_empty_chain():
    for payload in (None, {}, {"data": None}, {"data": {"options": "nope"}}):
        assert parse_chain(payload, "AAPL", today=date(2026, 9, 24)) == []


# --------------------------------------------------------------------------- #
# The DTE window
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_the_window_filters_and_says_what_was_available(monkeypatch):
    chains = CboeChains()
    parsed = parse_chain(_payload(), "AAPL", today=date(2026, 9, 24))

    async def _all(symbol):
        return parsed

    monkeypatch.setattr(chains, "all_contracts", _all)

    inside = await chains.chain_for_window("AAPL", 337.0, 14, 30)
    assert inside and all(14 <= c.dte <= 30 for c in inside)

    # Nothing in the window must name the expiries that DO exist, or the
    # message is indistinguishable from a dead endpoint.
    empty = await chains.chain_for_window("AAPL", 337.0, 200, 300)
    assert empty == []
    assert "22" in chains.options_error and "57" in chains.options_error


@pytest.mark.asyncio
async def test_expiries_come_back_as_epochs_like_the_other_feeds(monkeypatch):
    chains = CboeChains()
    parsed = parse_chain(_payload(), "AAPL", today=date(2026, 9, 24))

    async def _all(symbol):
        return parsed

    monkeypatch.setattr(chains, "all_contracts", _all)
    stamps = await chains.expiries("AAPL")
    assert len(stamps) == 2 and all(isinstance(e, int) for e in stamps)
    assert stamps == sorted(stamps)


# --------------------------------------------------------------------------- #
# The hybrid
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_the_hybrid_reports_the_chain_sources_error_not_its_own():
    """The desk clears options_error before a lookup and reads it after.

    Shadowing the chain source's field would write the reason to an
    attribute nobody reads, and "no contract" would go back to being
    unexplained.
    """
    from panaoptions.data.hybrid import HybridFeed

    class _Charts:
        chart_error = ""

        async def connect(self):
            return True

        async def close(self):
            return None

    class _Chains:
        options_error = "HTTP 404 from CBOE for WXYZ"

        async def open(self):
            return None

        async def close(self):
            return None

        async def probe(self):
            return False

    feed = HybridFeed(charts=_Charts(), chains=_Chains(), chains_name="CBOE")
    assert await feed.connect() is True          # charts work, so it starts
    assert feed.options_available is False
    assert "404" in feed.options_error

    feed.options_error = ""                      # the desk clears it
    assert feed.chains.options_error == ""       # and it reached the source


@pytest.mark.asyncio
async def test_the_hybrid_will_not_start_without_charts():
    """A missing chain is survivable; no prices at all is not."""
    from panaoptions.data.hybrid import HybridFeed

    class _DeadCharts:
        chart_error = "HTTP 403"

        async def connect(self):
            return False

        async def close(self):
            return None

    feed = HybridFeed(charts=_DeadCharts(), chains=object())
    assert await feed.connect() is False


def test_the_hybrid_offers_the_same_surface_as_a_single_feed():
    from panaoptions.data.feed import YahooFeed
    from panaoptions.data.hybrid import HybridFeed

    feed = HybridFeed(charts=YahooFeed(), chains=CboeChains())
    for name in ("connect", "close", "quote", "candles", "expiries",
                 "option_chain", "chain_for_window", "__aenter__", "__aexit__"):
        assert hasattr(feed, name), name
    for attr in ("connected", "options_available", "options_error"):
        assert hasattr(feed, attr), attr


def test_selecting_cboe_builds_the_hybrid(cfg):
    from panaoptions.data.hybrid import HybridFeed
    from panaoptions.data.provider import describe, make_feed

    cfg.data.setdefault("data", {})["provider"] = "cboe"
    feed = make_feed(cfg)
    assert isinstance(feed, HybridFeed)
    who = describe(cfg)
    assert who["ready"] and "no account" in who["note"]
