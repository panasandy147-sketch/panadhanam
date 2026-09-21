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


# --------------------------------------------------------------------------- #
# End-to-end plumbing: a connected feed's prices must reach the dashboard
#
# The live endpoints cannot be reached from CI, so these inject a stand-in feed
# that returns known values. That proves the WIRING — if the network allows the
# real feed to connect, its numbers travel the same path to the same places.
# --------------------------------------------------------------------------- #
from app.brokers.base import BrokerAdapter  # noqa: E402
from app.core.models import Candle, OptionChain, OptionLeg, Quote  # noqa: E402


class _StubFeed(BrokerAdapter):
    """A feed with unmistakable prices, so a synthetic fallback can't be
    mistaken for the real thing in an assertion."""

    name = "stubfeed"
    supports_options = True
    supports_live_orders = False

    MARKER = 1234.56

    async def connect(self) -> bool:
        self._connected = True
        return True

    async def get_quote(self, symbol):
        return Quote(symbol=symbol, last_price=self.MARKER, change_pct=1.23,
                     volume=987_654)

    async def get_candles(self, symbol, timeframe, count=200):
        from datetime import timedelta
        base = datetime(2026, 9, 18, 9, 15, tzinfo=UTC)
        out = []
        for i in range(count):
            price = self.MARKER - (count - i) * 0.1
            out.append(Candle(
                ts=base + timedelta(minutes=5 * i),   # strictly increasing
                open=price, high=price * 1.002, low=price * 0.998,
                close=price, volume=10_000 + i))
        out[-1] = Candle(ts=out[-1].ts, open=self.MARKER, high=self.MARKER,
                         low=self.MARKER, close=self.MARKER, volume=50_000)
        return out

    async def get_expiries(self, underlying):
        return ["2026-09-25"]

    async def get_option_chain(self, underlying, expiry=None):
        legs = []
        for i in range(-3, 4):
            strike = round(self.MARKER + i * 50)
            for opt in ("CE", "PE"):
                legs.append(OptionLeg(strike=strike, option_type=opt,
                                      ltp=50 - abs(i) * 5, oi=10_000 * (4 - abs(i)),
                                      oi_change=500 * i, volume=1_000, iv=0.15))
        return OptionChain(underlying=underlying, spot=self.MARKER,
                           expiry="2026-09-25", legs=legs)

    async def place_order(self, *a, **kw):
        from app.brokers.base import OrderResult
        return OrderResult(False, message="stub feed cannot place orders")


@pytest.mark.asyncio
async def test_paper_broker_serves_feed_prices_not_synthetic_ones():
    """The whole point of attaching a feed: real prices, simulated fills."""
    from app.brokers.paper import PaperBroker

    broker = PaperBroker(config={"total_capital": 100_000})
    await broker.connect()
    broker.data_source = _StubFeed()

    quote = await broker.get_quote("NIFTY 50")
    assert quote.last_price == _StubFeed.MARKER, "the feed must win over the simulator"

    candles = await broker.get_candles("NIFTY 50", "5m", 50)
    assert candles[-1].close == _StubFeed.MARKER

    chain = await broker.get_option_chain("NIFTY 50")
    assert chain.spot == _StubFeed.MARKER

    # Real expiries come from the feed too, not the synthetic weekly generator.
    assert await broker.get_expiries("NIFTY 50") == ["2026-09-25"]


@pytest.mark.asyncio
async def test_feed_prices_flow_through_to_the_opportunity_board(cfg):
    """Feed → broker → context → agents → risk → board, with real numbers."""
    from app.brokers.paper import PaperBroker
    from app.scheduler import TradingEngine

    cfg.switch_market("IN")
    cfg.settings["system"]["no_new_entry_after"] = "23:59"

    broker = PaperBroker(config={"total_capital": 100_000})
    await broker.connect()
    broker.data_source = _StubFeed()

    engine = TradingEngine(broker, cfg)
    engine.risk.set_capital(10_000_000)     # big enough not to be the blocker

    async def _no_news():
        return []

    async def _no_macro():
        from app.core.models import MacroSnapshot
        return MacroSnapshot()

    engine.news.fetch = _no_news
    engine.macro.fetch = _no_macro

    out = await engine.scanner.scan(symbols=["RELIANCE"])

    # Provenance must report the attached feed, not claim simulation.
    assert out["data_source"]["simulated"] is False
    assert "stubfeed" in out["data_source"]["sources"]
    assert "SIMULATED" not in out["data_source"]["label"].upper()

    entries = [o for tier in out["tiers"].values() for o in tier]
    assert entries, "the feed's prices should produce a directional read"
    trade = next(o["trade"] for o in entries if o["trade"])
    # Entry is anchored to the feed's price, not to a generated one.
    assert abs(trade["entry"] - _StubFeed.MARKER) < _StubFeed.MARKER * 0.05


@pytest.mark.asyncio
async def test_status_reports_simulation_honestly_without_a_feed(cfg):
    """No feed attached must never be described as real data."""
    from app.brokers.paper import PaperBroker
    from app.scheduler import TradingEngine

    cfg.switch_market("IN")
    broker = PaperBroker(config={"total_capital": 100_000})
    await broker.connect()
    broker.data_source = None

    engine = TradingEngine(broker, cfg)
    provenance = engine.data_provenance()

    assert provenance["simulated"] is True
    assert "SIMULATED" in provenance["label"].upper()
    assert provenance["sources"] == []


@pytest.mark.asyncio
async def test_status_names_the_live_sources_when_a_feed_is_attached(cfg):
    from app.brokers.paper import PaperBroker
    from app.data.feeds.stack import FeedStack
    from app.scheduler import TradingEngine

    cfg.switch_market("IN")
    broker = PaperBroker(config={"total_capital": 100_000})
    await broker.connect()

    stack = FeedStack([_StubFeed()])
    await stack.connect()
    broker.data_source = stack

    engine = TradingEngine(broker, cfg)
    provenance = engine.data_provenance()

    assert provenance["simulated"] is False
    assert provenance["sources"] == ["stubfeed"]
    assert provenance["prices_real"] is True
    assert "Real prices" in provenance["label"]
    # Execution is still simulated — that distinction must stay visible.
    assert "paper" in provenance["execution"]


@pytest.mark.asyncio
async def test_feed_stack_falls_through_to_the_next_source():
    """A feed that returns nothing must not stop a later one from answering."""
    from app.data.feeds.stack import FeedStack

    class _Dead(_StubFeed):
        name = "dead"

        async def get_quote(self, symbol):
            return None

        async def get_candles(self, symbol, timeframe, count=200):
            return []

    stack = FeedStack([_Dead(), _StubFeed()])
    await stack.connect()

    quote = await stack.get_quote("NIFTY")
    assert quote.last_price == _StubFeed.MARKER
    assert len(await stack.get_candles("NIFTY", "5m", 10)) > 0


@pytest.mark.asyncio
async def test_feed_stack_returns_nothing_when_every_source_fails():
    """Silence, not invention. The agents then abstain."""
    from app.data.feeds.stack import FeedStack

    class _Dead(_StubFeed):
        name = "dead"

        async def get_quote(self, symbol):
            return None

        async def get_candles(self, symbol, timeframe, count=200):
            return []

        async def get_option_chain(self, underlying, expiry=None):
            return None

    stack = FeedStack([_Dead(), _Dead()])
    await stack.connect()

    assert await stack.get_quote("NIFTY") is None
    assert await stack.get_candles("NIFTY", "5m", 10) == []
    assert await stack.get_option_chain("NIFTY") is None



# --------------------------------------------------------------------------- #
# A fabricated option chain must never hide behind real prices
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_a_synthetic_chain_is_reported_even_when_prices_are_real(cfg):
    """The exact trap: NSE is unreachable and Yahoo serves no Indian chains, so
    OI, PCR and Max Pain get fabricated while the header says "real data".
    Those numbers drive the derivatives analyst, so they must be flagged."""
    from app.brokers.paper import PaperBroker
    from app.data.feeds.stack import describe_data_source

    cfg.switch_market("IN")

    class _PricesOnly(_StubFeed):
        """Real prices, no option chain — exactly the live situation."""
        name = "pricesonly"

        async def get_option_chain(self, underlying, expiry=None):
            return None

    broker = PaperBroker(config={"total_capital": 100_000})
    await broker.connect()
    broker.data_source = _PricesOnly()

    chain = await broker.get_option_chain("NIFTY 50")
    assert chain is not None, "the fallback still runs by default"
    assert broker.synthetic_chain is True

    provenance = describe_data_source(broker)
    assert provenance["prices_real"] is True
    assert provenance["synthetic_chain"] is True
    assert "SIMULATED" in provenance["label"]


@pytest.mark.asyncio
async def test_a_real_chain_clears_the_synthetic_flag(cfg):
    from app.brokers.paper import PaperBroker
    from app.data.feeds.stack import describe_data_source

    cfg.switch_market("IN")
    broker = PaperBroker(config={"total_capital": 100_000})
    await broker.connect()
    broker.data_source = _StubFeed()       # this one DOES serve a chain

    chain = await broker.get_option_chain("NIFTY 50")
    assert chain is not None
    assert broker.synthetic_chain is False
    assert describe_data_source(broker)["synthetic_chain"] is False


@pytest.mark.asyncio
async def test_the_fallback_can_be_switched_off_entirely(cfg):
    """Traders who take F&O seriously can refuse invented chains outright; the
    derivatives analyst then abstains, which is the honest outcome."""
    from app.brokers.paper import PaperBroker

    cfg.switch_market("IN")
    cfg.settings.setdefault("data", {})["synthetic_chain_fallback"] = False

    class _PricesOnly(_StubFeed):
        name = "pricesonly"

        async def get_option_chain(self, underlying, expiry=None):
            return None

    broker = PaperBroker(config={"total_capital": 100_000})
    await broker.connect()
    broker.data_source = _PricesOnly()

    assert await broker.get_option_chain("NIFTY 50") is None
    cfg.settings["data"]["synthetic_chain_fallback"] = True


@pytest.mark.asyncio
async def test_derivatives_analyst_abstains_without_a_chain(cfg):
    """No chain must mean abstention, not a neutral vote — that is what keeps a
    missing feed from being read as agreement."""
    from app.agents.derivatives import DerivativesAgent
    from app.core.models import MarketContext

    ctx = MarketContext(symbol="NIFTY 50", cycle_id="c")
    report = DerivativesAgent(cfg).analyse_rules(ctx)
    assert report.data_available is False
    assert report.score == 0.0


# --------------------------------------------------------------------------- #
# MacroReplay: the macro dashboard as it printed on a past day. The whole point
# is that it refuses to serve a number the replayed bar could not have seen.
# --------------------------------------------------------------------------- #
def _tape(cfg, day, prints, prev_close=100.0):
    from datetime import UTC, datetime

    from app.data.macro import MacroReplay
    replay = MacroReplay(cfg)
    replay._labels = {"us_sp500": "S&P 500 Futures"}
    replay._tape = {"us_sp500": [
        (datetime.combine(day, t).replace(tzinfo=UTC), px) for t, px in prints]}
    replay._prev_close = {"us_sp500": prev_close}
    return replay


def test_macro_replay_serves_the_last_print_at_or_before_the_bar(cfg):
    from datetime import UTC, date, datetime, time

    day = date(2026, 9, 21)
    replay = _tape(cfg, day, [(time(9, 30), 101.0),
                              (time(10, 0), 104.0),
                              (time(10, 30), 108.0)])

    snap = replay.snapshot_at(datetime.combine(day, time(10, 5), tzinfo=UTC))
    assert snap.values["us_sp500"] == 104.0, \
        "10:30's print had not happened yet at 10:05"
    assert snap.changes_pct["us_sp500"] == pytest.approx(4.0)


def test_macro_replay_has_nothing_to_say_before_the_first_print(cfg):
    from datetime import UTC, date, datetime, time

    day = date(2026, 9, 21)
    replay = _tape(cfg, day, [(time(10, 0), 104.0)])
    assert replay.snapshot_at(datetime.combine(day, time(9, 40), tzinfo=UTC)) is None


def test_macro_replay_without_a_tape_abstains_rather_than_guessing(cfg):
    from datetime import UTC, datetime

    from app.data.macro import MacroReplay
    replay = MacroReplay(cfg)
    assert replay.loaded is False
    assert replay.snapshot_at(datetime.now(UTC)) is None


def test_macro_replay_reads_a_yahoo_chart_payload_and_drops_gaps(cfg):
    from app.data.macro import _closes

    payload = {"chart": {"result": [{
        "timestamp": [1758450600, 1758450900, 1758451200],
        "indicators": {"quote": [{"close": [101.0, None, 103.0]}]},
    }]}}
    out = _closes(payload)
    assert [px for _, px in out] == [101.0, 103.0], \
        "a null bucket is a gap in the tape, and filling it would invent a price"
    assert _closes({"chart": {"result": []}}) == []
    assert _closes({}) == []
