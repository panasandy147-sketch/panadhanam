"""Market-data feed parsing.

These test the PARSERS against recorded payload shapes, not the network. The
live endpoints are exercised on the user's machine; here we prove that a
well-formed response becomes correct candles/chains, and — more importantly —
that a malformed or empty one produces NOTHING rather than a fabricated price.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.data.feeds.nse import NSEFeed, nse_symbol
from app.data.feeds.yahoo import YahooFeed, yahoo_ticker


# --------------------------------------------------------------------------- #
# Ticker mapping
# --------------------------------------------------------------------------- #
def test_indian_symbols_map_to_yahoo_tickers(cfg):
    cfg.switch_market("IN")
    assert yahoo_ticker("NIFTY 50") == "^NSEI"
    assert yahoo_ticker("NIFTY BANK") == "^NSEBANK"
    assert yahoo_ticker("RELIANCE") == "RELIANCE.NS"     # equities need .NS
    cfg.switch_market("IN")


def test_us_symbols_need_no_suffix(cfg):
    cfg.switch_market("US")
    assert yahoo_ticker("AAPL") == "AAPL"
    assert yahoo_ticker("SPY") == "SPY"
    cfg.switch_market("IN")


def test_nse_symbol_aliases():
    assert nse_symbol("NIFTY 50") == "NIFTY"
    assert nse_symbol("NIFTY BANK") == "BANKNIFTY"
    assert nse_symbol("reliance") == "RELIANCE"


# --------------------------------------------------------------------------- #
# Yahoo candle parsing
# --------------------------------------------------------------------------- #
def _yahoo_chart(stamps, o, h, low, c, v):
    return {"timestamp": stamps,
            "indicators": {"quote": [{"open": o, "high": h, "low": low,
                                      "close": c, "volume": v}]}}


def test_yahoo_candles_parse_cleanly():
    base = int(datetime(2026, 9, 18, 9, 15, tzinfo=UTC).timestamp())
    result = _yahoo_chart(
        [base, base + 300, base + 600],
        [100.0, 101.0, 102.0], [101.5, 102.5, 103.0],
        [99.5, 100.5, 101.5], [101.0, 102.0, 102.5],
        [10_000, 12_000, 9_000])

    candles = YahooFeed._parse_candles(result)
    assert len(candles) == 3
    assert candles[0].open == 100.0 and candles[0].close == 101.0
    assert candles[-1].volume == 9_000
    assert all(c.high >= c.low for c in candles)
    assert candles[0].ts < candles[-1].ts


def test_yahoo_skips_untraded_buckets_rather_than_inventing_prices():
    """A null OHLC slot means no trade happened. Filling it with a neighbouring
    price would manufacture a candle that never existed."""
    base = int(datetime(2026, 9, 18, 9, 15, tzinfo=UTC).timestamp())
    result = _yahoo_chart(
        [base, base + 300, base + 600],
        [100.0, None, 102.0], [101.0, None, 103.0],
        [99.0, None, 101.0], [100.5, None, 102.5],
        [1000, None, 2000])

    candles = YahooFeed._parse_candles(result)
    assert len(candles) == 2, "the null bar must be dropped, not filled"
    assert [c.open for c in candles] == [100.0, 102.0]


def test_yahoo_handles_an_empty_response():
    assert YahooFeed._parse_candles({}) == []
    assert YahooFeed._parse_candles({"timestamp": [], "indicators": {}}) == []


def test_yahoo_option_chain_parses_oi_and_iv():
    exp = int(datetime(2026, 9, 25, tzinfo=UTC).timestamp())
    result = {
        "quote": {"regularMarketPrice": 585.0},
        "options": [{
            "expirationDate": exp,
            "calls": [{"strike": 585.0, "lastPrice": 4.2, "bid": 4.1, "ask": 4.3,
                       "openInterest": 12_000, "volume": 3_400,
                       "impliedVolatility": 0.185}],
            "puts": [{"strike": 585.0, "lastPrice": 3.9, "bid": 3.8, "ask": 4.0,
                      "openInterest": 9_500, "volume": 2_100,
                      "impliedVolatility": 0.191}],
        }],
    }
    chain = YahooFeed._parse_chain("SPY", result)
    assert chain and chain.spot == 585.0
    assert chain.expiry == "2026-09-25"
    assert len(chain.legs) == 2

    call = next(leg for leg in chain.legs if leg.option_type == "CE")
    assert call.oi == 12_000
    assert call.iv == pytest.approx(0.185)
    # Mid is fairer than a stale last print on a thin strike.
    assert call.ltp == pytest.approx(4.2)


def test_yahoo_chain_returns_none_when_there_are_no_legs():
    assert YahooFeed._parse_chain("SPY", {"options": [{"calls": [], "puts": []}]}) is None
    assert YahooFeed._parse_chain("SPY", {}) is None


# --------------------------------------------------------------------------- #
# NSE option chain parsing — the source of genuine OI
# --------------------------------------------------------------------------- #
def _nse_payload():
    return {
        "records": {
            "underlyingValue": 24_200.5,
            "expiryDates": ["25-Sep-2026", "02-Oct-2026"],
            "data": [
                {"strikePrice": 24_200, "expiryDate": "25-Sep-2026",
                 "CE": {"lastPrice": 120.5, "openInterest": 45_000,
                        "changeinOpenInterest": 5_200, "totalTradedVolume": 98_000,
                        "impliedVolatility": 14.5},
                 "PE": {"lastPrice": 98.2, "openInterest": 61_000,
                        "changeinOpenInterest": -3_100, "totalTradedVolume": 120_000,
                        "impliedVolatility": 15.1}},
                {"strikePrice": 24_250, "expiryDate": "25-Sep-2026",
                 "CE": {"lastPrice": 95.0, "openInterest": 38_000,
                        "changeinOpenInterest": 2_000, "totalTradedVolume": 70_000,
                        "impliedVolatility": 14.8}},
                # A different expiry must not leak into this chain.
                {"strikePrice": 24_200, "expiryDate": "02-Oct-2026",
                 "CE": {"lastPrice": 210.0, "openInterest": 11_000,
                        "changeinOpenInterest": 900, "totalTradedVolume": 15_000,
                        "impliedVolatility": 15.9}},
            ],
        }
    }


def test_nse_chain_parses_the_nearest_expiry_only():
    chain = NSEFeed.parse_chain("NIFTY", _nse_payload())
    assert chain and chain.spot == 24_200.5
    assert chain.expiry == "2026-09-25"
    assert len(chain.legs) == 3          # 2 CE + 1 PE from the near expiry
    assert all(leg.strike in {24_200, 24_250} for leg in chain.legs)


def test_nse_chain_carries_real_oi_change():
    """The OI delta is what makes long-buildup vs short-covering knowable.
    Yahoo does not provide it; the exchange does."""
    chain = NSEFeed.parse_chain("NIFTY", _nse_payload())
    call = next(leg for leg in chain.legs
                if leg.option_type == "CE" and leg.strike == 24_200)
    put = next(leg for leg in chain.legs if leg.option_type == "PE")

    assert call.oi == 45_000 and call.oi_change == 5_200
    assert put.oi == 61_000 and put.oi_change == -3_100
    # NSE quotes IV in percent; the models expect a decimal.
    assert call.iv == pytest.approx(0.145)


def test_nse_chain_can_select_a_later_expiry():
    chain = NSEFeed.parse_chain("NIFTY", _nse_payload(), expiry="2026-10-02")
    assert chain.expiry == "2026-10-02"
    assert len(chain.legs) == 1


def test_nse_chain_returns_none_on_an_empty_payload():
    assert NSEFeed.parse_chain("NIFTY", {}) is None
    assert NSEFeed.parse_chain("NIFTY", {"records": {"data": []}}) is None


def test_nse_derived_metrics_work_on_parsed_data():
    """End to end: a real payload must produce a usable PCR and buildup read."""
    from app.indicators.derivatives import analyse
    chain = NSEFeed.parse_chain("NIFTY", _nse_payload())
    out = analyse(chain, spot_change_pct=0.4, cfg={})

    assert out["pcr_oi"] > 0
    assert out["buildup"] in {"LONG_BUILDUP", "SHORT_BUILDUP", "SHORT_COVERING",
                              "LONG_UNWINDING", "NEUTRAL"}
    assert out["max_pain"] in {24_200, 24_250}


# --------------------------------------------------------------------------- #
# Feeds must never place orders
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_data_feeds_refuse_to_trade():
    from app.core.models import Instrument, Side
    inst = Instrument(symbol="SPY", tradingsymbol="SPY")
    for feed in (YahooFeed(), NSEFeed()):
        result = await feed.place_order(inst, Side.BUY, 1, 100.0)
        assert result.ok is False
        assert "cannot place orders" in result.message
