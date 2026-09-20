"""Market profiles, switching, and the conventions that differ between them."""
from __future__ import annotations

import pytest

from app.agents.risk import RiskManager
from app.brokers.paper import PaperBroker
from app.core import clock
from app.core.markets import load_profiles
from app.core.models import Bias, InstrumentType, MarketContext, Quote
from app.scheduler import TradingEngine


@pytest.fixture(autouse=True)
def _restore_market(cfg):
    """Every test leaves the desk back on India."""
    yield
    cfg.switch_market("IN")


@pytest.fixture
async def engine(cfg):
    cfg.switch_market("IN")
    cfg.settings["system"]["no_new_entry_after"] = "23:59"
    broker = PaperBroker(config={"total_capital": 100_000})
    await broker.connect()
    eng = TradingEngine(broker, cfg)

    async def _no_news():
        return []

    async def _no_macro():
        from app.core.models import MacroSnapshot
        return MacroSnapshot()

    eng.news.fetch = _no_news
    eng.macro.fetch = _no_macro
    return eng


# --------------------------------------------------------------------------- #
# Profiles
# --------------------------------------------------------------------------- #
def test_both_markets_load():
    profiles = load_profiles()
    assert {"IN", "US"} <= set(profiles)


def test_profiles_differ_where_it_matters():
    p = load_profiles()
    india, us = p["IN"], p["US"]

    assert india.currency_symbol == "₹" and us.currency_symbol == "$"
    assert india.timezone == "Asia/Kolkata"
    assert us.timezone == "America/New_York"
    # India sizes options in exchange lots; the US uses a 100x multiplier.
    assert india.lot_based is True
    assert us.lot_based is False
    assert us.contract_multiplier == 100
    # NSE weeklies are Thursday (3), US weeklies Friday (4).
    assert india.weekly_expiry_weekday == 3
    assert us.weekly_expiry_weekday == 4
    # Yahoo needs a suffix for Indian equities, none for US.
    assert india.yahoo_suffix == ".NS"
    assert us.yahoo_suffix == ""


def test_strike_ladders_match_each_market():
    p = load_profiles()
    assert p["IN"].strike_step("NIFTY", 24_200) == 50
    assert p["IN"].strike_step("BANKNIFTY", 51_500) == 100
    assert p["US"].strike_step("SPY", 585) == 1
    # An unknown symbol falls back to a percentage, snapped to a real increment.
    step = p["US"].strike_step("UNKNOWN", 200)
    assert step in {0.5, 1, 2.5, 5, 10, 25, 50, 100, 250, 500}


# --------------------------------------------------------------------------- #
# Switching
# --------------------------------------------------------------------------- #
def test_switching_swaps_session_universe_and_feeds(cfg):
    cfg.switch_market("IN")
    assert cfg.get("system.market_open") == "09:15"
    india_symbols = {w["symbol"] for w in cfg.watchlist()}
    assert "NIFTY 50" in india_symbols

    cfg.switch_market("US")
    assert cfg.active_market == "US"
    assert cfg.get("system.market_open") == "09:30"
    assert cfg.get("system.timezone") == "America/New_York"

    us_symbols = {w["symbol"] for w in cfg.watchlist()}
    assert "SPY" in us_symbols
    assert not (india_symbols & us_symbols), "the watchlists must not bleed"

    # News and macro must follow the market too.
    sources = " ".join(s["name"] for s in cfg.get("news.sources"))
    assert "CNBC" in sources and "Moneycontrol" not in sources
    macro_keys = {m["key"] for m in cfg.get("macro.global_indices")}
    assert "us10y" in macro_keys and "usdinr" not in macro_keys


def test_unknown_market_is_refused_and_leaves_the_desk_alone(cfg):
    cfg.switch_market("IN")
    active = cfg.switch_market("ZZ")
    assert active == "IN"


def test_broker_falls_back_when_unavailable_in_this_market(cfg):
    """Zerodha cannot quote AAPL — asking for it on the US desk must not stick."""
    cfg.switch_market("US")
    cfg.settings["execution"]["broker"] = "zerodha"
    assert cfg.broker_name == "paper"      # US default, not the Indian broker
    cfg.settings["execution"]["broker"] = "paper"


# --------------------------------------------------------------------------- #
# The clock is the market's, not the server's
# --------------------------------------------------------------------------- #
def test_session_is_evaluated_in_market_time():
    ist = clock.market_now("Asia/Kolkata")
    et = clock.market_now("America/New_York")
    # Same instant, different wall clocks — this is the whole point.
    assert ist.utcoffset() != et.utcoffset()
    assert clock.session_phase("Asia/Kolkata", "08:45", "09:15", "15:30") in {
        "closed", "premarket", "open", "postmarket", "weekend"}


def test_weekend_is_never_an_open_session():
    for tz in ("Asia/Kolkata", "America/New_York"):
        if not clock.is_trading_day(tz):
            assert clock.is_open(tz, "09:15", "15:30") is False


# --------------------------------------------------------------------------- #
# Sizing conventions
# --------------------------------------------------------------------------- #
def _ctx(symbol: str, price: float) -> MarketContext:
    ctx = MarketContext(symbol=symbol, cycle_id="t",
                        quote=Quote(symbol=symbol, last_price=price))
    ctx.indicators = {"primary": {"last_close": price, "atr": price * 0.01,
                                  "high": price * 1.02, "low": price * 0.98}}
    ctx.__dict__["_reports"] = []
    return ctx


def test_us_equity_sizes_in_single_shares(cfg):
    cfg.switch_market("US")
    cfg.settings["system"]["no_new_entry_after"] = "23:59"
    rm = RiskManager(cfg)
    sig = rm.evaluate(_ctx("AAPL", 232.0), Bias.BULLISH, [], 0.6, ["a", "b"])
    assert sig.instrument.instrument_type == InstrumentType.EQUITY
    assert sig.unit_size == 1
    assert sig.unit_label == "share"
    assert sig.quantity > 1


def test_indian_equity_keeps_its_lot_semantics(cfg):
    cfg.switch_market("IN")
    cfg.settings["system"]["no_new_entry_after"] = "23:59"
    rm = RiskManager(cfg)
    sig = rm.evaluate(_ctx("RELIANCE", 2950.0), Bias.BULLISH, [], 0.6, ["a", "b"])
    assert sig.unit_size == 1          # cash equity is still 1 share
    assert sig.capital_at_risk_pct <= 1.01


@pytest.mark.asyncio
async def test_paper_broker_follows_the_active_market(cfg):
    cfg.switch_market("US")
    b = PaperBroker(config={"total_capital": 100_000})
    await b.connect()

    spy = await b.get_quote("SPY")
    assert spy and 400 < spy.last_price < 800, "SPY must be seeded near a US level"

    chain = await b.get_option_chain("SPY")
    assert chain
    strikes = sorted({leg.strike for leg in chain.legs})
    step = min(b - a for a, b in zip(strikes, strikes[1:], strict=False))
    assert step == pytest.approx(1.0), "SPY strikes ladder in $1 increments"

    # US weeklies expire on a Friday.
    from datetime import datetime
    assert datetime.fromisoformat(chain.expiry).weekday() == 4


@pytest.mark.asyncio
async def test_switch_refuses_to_orphan_open_positions(engine):
    """Switching markets with a live position would hand it to a broker that
    cannot manage it. The desk must refuse rather than silently abandon it."""
    eng = engine
    eng.risk.state.open_positions = 1

    result = await eng.switch_market("US")
    assert result["switched"] is False
    assert "open" in result["reason"].lower()
    assert eng.cfg.active_market == "IN", "the market must not have changed"


@pytest.mark.asyncio
async def test_switch_rebuilds_the_whole_desk(engine):
    eng = engine
    assert eng.cfg.active_market == "IN"

    result = await eng.switch_market("US")
    assert result["switched"] is True
    assert eng.cfg.active_market == "US"

    # Everything derived from the old market must have been rebuilt.
    assert eng.timezone == "America/New_York"
    assert {w["symbol"] for w in eng.cfg.watchlist()} >= {"SPY", "AAPL"}
    assert eng.risk.snapshot()["currency"] == "$"
    assert eng._fundamentals == {}

    quote = await eng.broker.get_quote("SPY")
    assert quote and quote.last_price > 100


@pytest.mark.asyncio
async def test_switching_back_and_forth_is_stable(engine):
    eng = engine
    for expected in ("US", "IN", "US", "IN"):
        await eng.switch_market(expected)
        assert eng.cfg.active_market == expected
        assert len(eng.cfg.watchlist()) > 0
        assert eng.risk.snapshot()["capital"] > 0
