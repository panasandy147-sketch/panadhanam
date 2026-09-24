"""The Tradier feed: same interface as Yahoo, with greeks from the exchange.

Everything here parses recorded response shapes rather than calling out, so
the suite stays offline and a change in Tradier's JSON shows up as a failing
test rather than as a quiet day with no trades.
"""
from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from panaoptions.data.tradier import (
    TradierFeed,
    _intraday_candle,
    _listify,
    parse_chain,
)
from panaoptions.models import OptionRight


# --------------------------------------------------------------------------- #
# The single-element trap
# --------------------------------------------------------------------------- #
def test_a_one_element_list_comes_back_as_a_bare_object():
    """Tradier drops the list when there is exactly one of something.

    Iterating that without noticing walks the dict's KEYS, which is a very
    quiet way to end up with no data on the one expiry a symbol has.
    """
    assert _listify({"date": "2026-10-16"}) == [{"date": "2026-10-16"}]
    assert _listify([{"a": 1}, {"b": 2}]) == [{"a": 1}, {"b": 2}]
    assert _listify(None) == []


# --------------------------------------------------------------------------- #
# Chains, with real greeks
# --------------------------------------------------------------------------- #
def _chain_payload():
    return {"options": {"option": [
        {"option_type": "call", "strike": 350.0, "bid": 8.10, "ask": 8.30,
         "open_interest": 4200, "volume": 915,
         "greeks": {"delta": 0.5821, "mid_iv": 0.3145}},
        {"option_type": "put", "strike": 350.0, "bid": 7.60, "ask": 7.80,
         "open_interest": 3100, "volume": 640,
         "greeks": {"delta": -0.4179, "mid_iv": 0.3202}},
        {"option_type": "call", "strike": 355.0, "bid": 5.90, "ask": 6.05,
         "open_interest": 2200, "volume": 410, "greeks": {"delta": 0.4610}},
    ]}}


def test_the_delta_comes_from_the_exchange_not_from_an_estimate():
    """The whole reason for this feed.

    Yahoo serves no greeks, so that path computes delta with Black-Scholes
    from implied volatility — a decent estimate, and still an estimate that
    every delta band in this app is then applied to.
    """
    chain = parse_chain(_chain_payload(), "AVGO", date(2026, 10, 16))
    assert len(chain) == 3
    call = next(c for c in chain if c.strike == 350 and c.right is OptionRight.CALL)
    assert call.delta == pytest.approx(0.5821)
    assert call.implied_volatility == pytest.approx(0.3145)
    assert call.bid == 8.10 and call.ask == 8.30
    assert call.open_interest == 4200


def test_a_put_keeps_its_negative_delta():
    """The contract filter compares on absolute value; mangling the sign here
    would make every put look like a call."""
    chain = parse_chain(_chain_payload(), "AVGO", date(2026, 10, 16))
    put = next(c for c in chain if c.right is OptionRight.PUT)
    assert put.delta == pytest.approx(-0.4179)


def test_a_contract_with_no_greeks_block_still_parses():
    """Missing greeks must not drop the row — the filter can reject it on
    delta, but only if it is there to be rejected."""
    payload = {"options": {"option": [
        {"option_type": "call", "strike": 350.0, "bid": 8.1, "ask": 8.3}]}}
    chain = parse_chain(payload, "AVGO", date(2026, 10, 16))
    assert len(chain) == 1 and chain[0].delta == 0.0


def test_the_expiry_and_dte_are_carried_onto_every_contract():
    expiry = date(2026, 10, 16)
    chain = parse_chain(_chain_payload(), "AVGO", expiry)
    assert all(c.expiry == "2026-10-16" for c in chain)
    expected = (expiry - datetime.now(UTC).date()).days
    assert all(c.dte == expected for c in chain)


def test_an_empty_or_broken_payload_is_an_empty_chain_not_a_crash():
    assert parse_chain(None, "AVGO", date(2026, 10, 16)) == []
    assert parse_chain({}, "AVGO", date(2026, 10, 16)) == []
    assert parse_chain({"options": None}, "AVGO", date(2026, 10, 16)) == []


def test_a_single_contract_chain_is_not_iterated_as_keys():
    payload = {"options": {"option": {
        "option_type": "call", "strike": 350.0, "bid": 8.1, "ask": 8.3,
        "greeks": {"delta": 0.58}}}}
    chain = parse_chain(payload, "AVGO", date(2026, 10, 16))
    assert len(chain) == 1 and chain[0].strike == 350.0


# --------------------------------------------------------------------------- #
# Bars
# --------------------------------------------------------------------------- #
def test_an_intraday_bar_is_read_as_new_york_time():
    """Tradier's `time` is exchange-local and naive.

    Reading it as UTC would shift every bar four hours and put the whole
    session outside every strategy window — the desk would look broken while
    every individual number on screen looked right.
    """
    bar = _intraday_candle({"time": "2026-09-24T09:35:00", "open": 100.0,
                            "high": 100.5, "low": 99.8, "close": 100.2,
                            "volume": 5000})
    assert bar is not None
    assert bar.ts == datetime(2026, 9, 24, 13, 35, tzinfo=UTC)   # 09:35 EDT
    assert bar.close == 100.2


def test_an_epoch_timestamp_is_also_accepted():
    bar = _intraday_candle({"timestamp": 1790000000, "open": 1.0, "high": 1.0,
                            "low": 1.0, "close": 1.0})
    assert bar is not None and bar.ts.tzinfo is not None


def test_a_bar_with_no_price_is_dropped_never_invented():
    """A forward-filled candle prints a body and a wick nobody traded, and
    the pattern engine reads that as a signal."""
    assert _intraday_candle({"time": "2026-09-24T09:35:00", "close": 0}) is None
    assert _intraday_candle({"time": "2026-09-24T09:35:00"}) is None
    assert _intraday_candle("not a row") is None


# --------------------------------------------------------------------------- #
# Connecting
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_no_token_refuses_to_connect_rather_than_failing_later():
    feed = TradierFeed(token="")
    assert await feed.connect() is False
    assert feed.connected is False


def test_a_sandbox_token_does_not_point_at_production():
    assert "sandbox" in TradierFeed(token="x", environment="sandbox").base
    assert "api.tradier" in TradierFeed(token="x", environment="production").base
    # An unknown environment must not silently become production.
    assert "sandbox" in TradierFeed(token="x", environment="nonsense").base


@pytest.mark.asyncio
async def test_it_offers_the_same_surface_as_the_yahoo_feed():
    """The desk holds "a feed" and must not be able to tell them apart."""
    from panaoptions.data.feed import YahooFeed

    for name in ("connect", "close", "quote", "candles", "expiries",
                 "option_chain", "chain_for_window", "__aenter__", "__aexit__"):
        assert hasattr(TradierFeed(token="x"), name), name
        assert hasattr(YahooFeed(), name), name
    for attr in ("connected", "options_available", "options_error"):
        assert hasattr(TradierFeed(token="x"), attr), attr


# --------------------------------------------------------------------------- #
# Choosing a provider
# --------------------------------------------------------------------------- #
def test_the_desk_reports_which_provider_it_will_use(cfg, monkeypatch):
    from panaoptions.data.provider import describe

    monkeypatch.delenv("TRADIER_TOKEN", raising=False)
    cfg.data.setdefault("data", {})["provider"] = "yahoo"
    who = describe(cfg)
    assert who["provider"] == "yahoo" and who["ready"]
    # And it says the cost of that choice rather than leaving it implicit.
    assert "estimated" in who["note"]


def test_tradier_without_a_token_is_reported_as_not_ready(cfg, monkeypatch):
    from panaoptions.data.provider import describe

    monkeypatch.delenv("TRADIER_TOKEN", raising=False)
    cfg.data.setdefault("data", {})["provider"] = "tradier"
    who = describe(cfg)
    assert not who["ready"]
    assert "TRADIER_TOKEN" in who["note"]


def test_a_missing_token_falls_back_loudly_not_silently(cfg, monkeypatch,
                                                        caplog):
    """A desk quietly running on a different data source than intended is
    the worst of both outcomes."""
    import logging

    from panaoptions.data.feed import YahooFeed
    from panaoptions.data.provider import make_feed

    monkeypatch.delenv("TRADIER_TOKEN", raising=False)
    cfg.data.setdefault("data", {})["provider"] = "tradier"
    # The app's logger deliberately does not propagate to root; caplog reads
    # the root handler, so turn propagation on just for this assertion.
    monkeypatch.setattr(logging.getLogger("panaoptions"), "propagate", True)
    with caplog.at_level(logging.ERROR, logger="panaoptions.provider"):
        feed = make_feed(cfg)
    assert isinstance(feed, YahooFeed)
    assert any("TRADIER_TOKEN" in r.message for r in caplog.records)


def test_a_token_selects_tradier(cfg, monkeypatch):
    from panaoptions.data.provider import make_feed
    from panaoptions.data.tradier import TradierFeed

    monkeypatch.setenv("TRADIER_TOKEN", "test-token")
    cfg.data.setdefault("data", {})["provider"] = "tradier"
    cfg.data["data"]["tradier_env"] = "sandbox"
    feed = make_feed(cfg)
    assert isinstance(feed, TradierFeed)
    assert "sandbox" in feed.base


def test_a_typo_in_the_provider_name_does_not_stop_the_desk(cfg, monkeypatch,
                                                            caplog):
    import logging

    from panaoptions.data.feed import YahooFeed
    from panaoptions.data.provider import make_feed

    cfg.data.setdefault("data", {})["provider"] = "tradeir"
    monkeypatch.setattr(logging.getLogger("panaoptions"), "propagate", True)
    with caplog.at_level(logging.WARNING, logger="panaoptions.provider"):
        feed = make_feed(cfg)
    assert isinstance(feed, YahooFeed)
    assert any("tradeir" in r.message for r in caplog.records)


def test_the_provider_can_be_set_per_machine_without_editing_yaml(monkeypatch):
    """Yahoo's chain endpoint works for some people and 401s for others.

    That makes the data source exactly the kind of setting that belongs
    beside the machine rather than in a file everyone shares — and it has to
    be settable with one command, because the people hitting it are already
    two failures deep.
    """
    from panaoptions import config as config_mod

    monkeypatch.setenv("PANAOPTIONS_PROVIDER", "cboe")
    monkeypatch.setattr(config_mod, "ENV_PATH", config_mod.ROOT / "absent.env")
    cfg = config_mod.Config()
    assert cfg.get("data.provider") == "cboe"


def test_an_unset_provider_leaves_the_config_alone(monkeypatch):
    from panaoptions import config as config_mod

    monkeypatch.delenv("PANAOPTIONS_PROVIDER", raising=False)
    monkeypatch.setattr(config_mod, "ENV_PATH", config_mod.ROOT / "absent.env")
    assert config_mod.Config().get("data.provider") == "auto"
