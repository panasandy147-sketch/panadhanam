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
    rm.set_capital(100_000)      # this test is about share semantics, not size
    sig = rm.evaluate(_ctx("AAPL", 232.0), Bias.BULLISH, [], 0.6, ["a", "b"])
    assert sig.instrument.instrument_type == InstrumentType.EQUITY
    assert sig.unit_size == 1
    assert sig.unit_label == "share"
    assert sig.quantity > 1


def test_indian_equity_keeps_its_lot_semantics(cfg):
    cfg.switch_market("IN")
    cfg.settings["system"]["no_new_entry_after"] = "23:59"
    rm = RiskManager(cfg)
    rm.set_capital(100_000)
    sig = rm.evaluate(_ctx("RELIANCE", 2950.0), Bias.BULLISH, [], 0.6, ["a", "b"])
    assert sig.unit_size == 1          # cash equity is still 1 share
    assert sig.capital_at_risk_pct <= float(cfg.get("risk.risk_per_trade_pct")) + 0.01


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


def test_an_index_is_never_proposed_as_a_cash_trade(cfg):
    """You cannot buy NIFTY or FINNIFTY at spot — only its options or futures.
    Showing a cash projection for one invites an order that cannot be placed."""
    cfg.switch_market("IN")
    cfg.settings["system"]["no_new_entry_after"] = "23:59"
    rm = RiskManager(cfg)
    rm.set_capital(1_000_000)          # big enough that size is not the blocker

    sig = rm.evaluate(_ctx("FINNIFTY", 23_100.0), Bias.BULLISH, [], 0.6, ["a", "b"])
    assert sig.instrument.instrument_type == InstrumentType.EQUITY
    assert sig.status.value == "REJECTED"
    assert any("index" in r.lower() for r in sig.rejection_reasons)


def test_us_index_etfs_are_ordinary_shares(cfg):
    """SPY and QQQ are ETFs, not indices — they must stay tradeable as shares."""
    cfg.switch_market("US")
    cfg.settings["system"]["no_new_entry_after"] = "23:59"
    rm = RiskManager(cfg)
    rm.set_capital(100_000)

    sig = rm.evaluate(_ctx("SPY", 585.0), Bias.BULLISH, [], 0.6, ["a", "b"])
    assert sig.quantity > 0
    assert not any("index" in r.lower() for r in sig.rejection_reasons)


# --------------------------------------------------------------------------- #
# Following the session: one app, both markets, never at the same time.
# --------------------------------------------------------------------------- #
def _pin_sessions(monkeypatch, config, **open_markets):
    """Pin which markets are trading, without moving the clock."""
    from app.core.markets import MarketProfile

    monkeypatch.setattr(
        MarketProfile, "is_in_session",
        lambda self: open_markets.get(self.code, False))
    return config


@pytest.fixture
def following(cfg):
    cfg.settings.setdefault("markets", {})["auto_follow_session"] = True
    return cfg


@pytest.mark.asyncio
async def test_the_desk_moves_to_whichever_market_has_opened(
        engine, following, monkeypatch):
    _pin_sessions(monkeypatch, following, IN=False, US=True)

    assert await engine.maybe_follow_session() == "US"
    assert following.active_market == "US"


@pytest.mark.asyncio
async def test_a_market_still_trading_is_never_interrupted(
        engine, following, monkeypatch):
    _pin_sessions(monkeypatch, following, IN=True, US=True)

    assert await engine.maybe_follow_session() is None
    assert following.active_market == "IN", \
        "leaving a live session mid-flight would strand the day's state"


@pytest.mark.asyncio
async def test_nothing_moves_while_a_position_is_open(
        engine, following, monkeypatch):
    # Switching rebuilds the broker, and the new one cannot manage the old
    # market's positions.
    _pin_sessions(monkeypatch, following, IN=False, US=True)
    engine.risk.state.open_positions = 1

    assert await engine.maybe_follow_session() is None
    assert following.active_market == "IN"


@pytest.mark.asyncio
async def test_nothing_moves_when_every_market_is_shut(
        engine, following, monkeypatch):
    _pin_sessions(monkeypatch, following, IN=False, US=False)

    assert await engine.maybe_follow_session() is None
    assert following.active_market == "IN"


@pytest.mark.asyncio
async def test_following_can_be_switched_off(engine, following, monkeypatch):
    following.settings["markets"]["auto_follow_session"] = False
    _pin_sessions(monkeypatch, following, IN=False, US=True)

    assert await engine.maybe_follow_session() is None
    assert following.active_market == "IN"


@pytest.mark.asyncio
async def test_the_desk_stays_put_when_its_own_market_is_the_open_one(
        engine, following, monkeypatch):
    await engine.switch_market("US")
    _pin_sessions(monkeypatch, following, IN=False, US=True)

    assert await engine.maybe_follow_session() is None
    assert following.active_market == "US"


def test_the_two_sessions_never_overlap(cfg):
    """The premise the whole feature rests on.

    India trades 03:45-10:00 UTC and the US 13:30-20:00 UTC. If that ever
    stopped being true, one desk could not serve both, and this test should
    fail rather than the behaviour quietly degrading.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    def utc_window(profile):
        session = profile.session
        day = datetime(2026, 9, 23, tzinfo=ZoneInfo(profile.timezone))
        edges = []
        for key in ("market_open", "market_close"):
            hour, _, minute = str(session[key]).partition(":")
            stamp = day.replace(hour=int(hour), minute=int(minute or 0)) \
                       .astimezone(ZoneInfo("UTC"))
            edges.append(stamp.hour * 60 + stamp.minute)
        return tuple(edges)

    india, us = utc_window(cfg.profiles["IN"]), utc_window(cfg.profiles["US"])
    assert india[1] <= us[0] or us[1] <= india[0], \
        f"sessions overlap: India {india} vs US {us} (minutes UTC)"
