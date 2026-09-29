"""India mode: NSE options on the same desk, with its own clock, lots and books."""
from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from panaoptions import markets
from panaoptions.models import (
    Candle,
    Direction,
    ExitReason,
    Indicators,
    OptionRight,
    Setup,
    SetupType,
)

IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture
def india(tmp_path, monkeypatch):
    from panaoptions import config as config_mod

    monkeypatch.setattr(config_mod, "ENV_PATH", tmp_path / "absent.env")
    cfg = config_mod.Config(market="IN")
    yield cfg
    markets.activate("US")


# --------------------------------------------------------------------------- #
def test_the_india_overlay_changes_clock_symbols_account_and_contracts(india):
    assert india.market == "IN" and india.timezone == "Asia/Kolkata"
    assert india.capital == 350000 and india.currency == "₹"
    assert "NIFTY" in india.symbols and "SPY" not in india.symbols
    assert india.lot_size("NIFTY") == 75 and india.lot_size("BANKNIFTY") == 30
    assert india.get("session.force_exit_at") == "15:15"
    assert india.get("strategies.orb_vwap.from") == "09:30"
    assert india.get("volume_profile.rth_open") == "09:15"
    assert india.get("agents.macro.futures") == []


def test_the_zerodte_profile_still_runs_on_india_hours(tmp_path, monkeypatch):
    from panaoptions import config as config_mod

    monkeypatch.setattr(config_mod, "ENV_PATH", tmp_path / "absent.env")
    cfg = config_mod.Config(profile="zerodte", market="IN")
    assert cfg.get("strategies.candlestick_at_level.to") == "14:45"   # not 15:00 ET
    assert cfg.last_entry_hhmm <= cfg.get("session.force_exit_at")


def test_india_capital_has_its_own_env_key(tmp_path, monkeypatch):
    from panaoptions import config as config_mod

    monkeypatch.setattr(config_mod, "ENV_PATH", tmp_path / "absent.env")
    monkeypatch.setenv("PANAOPTIONS_CAPITAL", "4000")           # the US account
    monkeypatch.setenv("PANAOPTIONS_CAPITAL_IN", "500000")
    assert config_mod.Config(market="IN").capital == 500000
    assert config_mod.Config(market="US").capital == 4000


# --------------------------------------------------------------------------- #
# NSE chains, lots and costs
# --------------------------------------------------------------------------- #
def _nse_payload(spot=24000.0, expiry="30-Sep-2026"):
    rows = []
    for strike in (23900, 24000, 24100):
        rows.append({"strikePrice": strike, "expiryDate": expiry,
                     "CE": {"lastPrice": 150, "bidprice": 149, "askPrice": 151,
                            "impliedVolatility": 14.0, "openInterest": 100000,
                            "totalTradedVolume": 250000},
                     "PE": {"lastPrice": 140, "bidprice": 139, "askPrice": 141,
                            "impliedVolatility": 15.0, "openInterest": 90000,
                            "totalTradedVolume": 200000}})
    return {"records": {"underlyingValue": spot, "data": rows,
                        "expiryDates": [expiry]}}


def test_nse_chains_parse_with_lots_dte_and_delta():
    from panaoptions.data.nse import parse_chain

    now = datetime(2026, 9, 28, 10, 0, tzinfo=IST)
    chain = parse_chain(_nse_payload(), "NIFTY", 75, now)
    assert len(chain) == 6
    atm_call = next(c for c in chain if c.strike == 24000 and c.right is OptionRight.CALL)
    assert atm_call.multiplier == 75 and atm_call.dte == 2
    assert (atm_call.bid, atm_call.ask) == (149, 151)
    assert 0.45 < atm_call.delta < 0.60
    assert atm_call.cost(100) == pytest.approx(150 * 75)        # the lot, not 100
    atm_put = next(c for c in chain if c.strike == 24000 and c.right is OptionRight.PUT)
    assert -0.55 < atm_put.delta < -0.40


def test_a_same_day_contract_still_gets_a_delta():
    from panaoptions.data.nse import parse_chain

    now = datetime(2026, 9, 30, 11, 0, tzinfo=IST)            # expiry day
    chain = parse_chain(_nse_payload(), "NIFTY", 75, now)
    assert all(c.dte == 0 and c.delta != 0 for c in chain)


def test_yahoo_is_asked_by_its_own_names():
    from panaoptions.data.nse import YahooNames

    names = YahooNames(object(), {"NIFTY": "^NSEI", "BANKNIFTY": "^NSEBANK"}, ".NS")
    assert names.name_for("NIFTY") == "^NSEI"
    assert names.name_for("RELIANCE") == "RELIANCE.NS"
    assert names.name_for("^NSEI") == "^NSEI"


def test_sizing_and_pnl_use_the_nse_lot(india):
    from panaoptions.data.nse import parse_chain
    from panaoptions.ledger.paper import PaperLedger
    from panaoptions.risk.gatekeeper import RiskGatekeeper
    from panaoptions.risk.guardrails import RiskManager

    now = datetime(2026, 9, 28, 10, 0, tzinfo=IST)
    call = next(c for c in parse_chain(_nse_payload(), "NIFTY", 75, now)
                if c.strike == 24000 and c.right is OptionRight.CALL)
    setup = Setup(symbol="NIFTY", ts=now, direction=Direction.LONG,
                  strategy=SetupType.ORB_VWAP, pattern="x", confirmations=["a"],
                  indicators=Indicators(close=24000, atr=40), trend_aligned=True,
                  underlying_support=23950)
    risk = RiskManager(india)
    gate = RiskGatekeeper(india, risk)
    assert gate.cap("NIFTY") == 87500.0                         # 25% index flex
    signal, refusal = risk.size(setup, call, "SIG-IN", now)
    assert signal is not None, refusal
    # 87,500 // 11,250 = 7 lots by deployment; the 2% risk cap (₹7,000 at
    # the stop) is tighter, and it too is judged per LOT.
    from panaoptions.risk.guardrails import planned_loss_per_contract
    per_lot = planned_loss_per_contract(india, call, 24000, 23950)
    lots = min(7, int(7000 // per_lot))
    assert signal.quantity == lots
    assert signal.cost() == pytest.approx(lots * 150 * 75)

    ledger = PaperLedger(india, risk)
    trade = ledger.open(signal, now)
    assert trade.multiplier == 75 and trade.market == "IN"
    ledger.close(trade.id, 160.0, ExitReason.DAY_END, now)
    per_share = 160.0 - float(india.get("risk.slippage_per_contract", 0.02)) - trade.entry_price
    assert trade.realised_pnl == pytest.approx(per_share * lots * 75, abs=1.0)


# --------------------------------------------------------------------------- #
# Session levels on Indian hours
# --------------------------------------------------------------------------- #
def test_the_opening_range_starts_at_0915_ist():
    from panaoptions.engine import levels

    bell = datetime(2026, 9, 28, 3, 45, tzinfo=UTC)             # 09:15 IST
    bars = [Candle(ts=bell + timedelta(minutes=5 * i), open=100 + i, high=101 + i,
                   low=99 + i, close=100 + i, volume=1000) for i in range(6)]
    lv = levels.compute(bars, "Asia/Kolkata", date(2026, 9, 28), "09:15", "15:30")
    assert lv.has_opening_range
    assert (lv.opening_range_low, lv.opening_range_high) == (99, 103)


# --------------------------------------------------------------------------- #
# Separate books, the switch, and Auto
# --------------------------------------------------------------------------- #
def test_each_market_keeps_its_own_books():
    from panaoptions import watchlist
    from panaoptions.journal import store as journal_store
    from panaoptions.ledger import store

    us_data, us_journal = store.DATA_DIR, journal_store.JOURNAL_DIR
    markets.activate("IN")
    assert store.DATA_DIR == us_data / "in"
    assert journal_store.JOURNAL_DIR == us_journal / "in"
    assert watchlist.STORE == us_data / "in" / "watchlist.json"
    markets.activate("US")
    assert store.DATA_DIR == us_data and journal_store.JOURNAL_DIR == us_journal


def test_auto_knows_whose_session_it_is():
    assert markets.which_now(datetime(2026, 9, 29, 5, 0, tzinfo=UTC)) == "IN"   # 10:30 IST
    assert markets.which_now(datetime(2026, 9, 29, 15, 0, tzinfo=UTC)) == "US"  # 11:00 ET
    assert markets.which_now(datetime(2026, 9, 29, 11, 0, tzinfo=UTC)) is None
    assert markets.which_now(datetime(2026, 10, 3, 5, 0, tzinfo=UTC)) is None   # Saturday


def test_the_desk_switches_market_and_writes_india_audit_to_its_own_folder(cfg):
    from panaoptions import audit
    from panaoptions.app import OptionsDesk
    from panaoptions.journal import store as journal_store
    from tests.test_app import FakeFeed

    desk = OptionsDesk(cfg=cfg, feed=FakeFeed())
    us_journal = journal_store.JOURNAL_DIR
    result = asyncio.run(desk.switch_market("IN", feed_factory=lambda c: FakeFeed()))
    assert result["switched"] and desk.cfg.market == "IN"
    assert desk.cfg.currency == "₹" and desk.risk.capital == 350000
    assert "NIFTY" in desk.cfg.symbols
    assert audit.audit_dir() == us_journal / "in" / "audit"
    assert any(e["kind"] == "market" for e in desk.activity.recent(10))
    back = asyncio.run(desk.switch_market("US", feed_factory=lambda c: FakeFeed()))
    assert back["switched"] and desk.cfg.market == "US"
    assert audit.audit_dir() == us_journal / "audit"


def test_a_switch_is_refused_while_a_position_is_open(cfg, monkeypatch):
    from panaoptions import clock
    from panaoptions.app import OptionsDesk
    from tests.test_app import FakeFeed

    et = ZoneInfo("America/New_York")
    monkeypatch.setattr(clock, "now", lambda tz: datetime(2026, 9, 23, 10, 20, tzinfo=et))
    cfg.data["contracts"]["max_contract_price"] = 2.0
    cfg.data["universe"]["symbols"] = ["SPY"]
    desk = OptionsDesk(cfg=cfg, feed=FakeFeed())
    asyncio.run(desk.cycle())
    assert desk.ledger.open_trades
    result = asyncio.run(desk.switch_market("IN", feed_factory=lambda c: FakeFeed()))
    assert not result["switched"] and "position" in result["reason"]
    assert desk.cfg.market == "US"


def test_the_toggle_api_switches_and_remembers(cfg, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from panaoptions.app import OptionsDesk
    from panaoptions.config import saved_market_mode
    from panaoptions.web import server
    from tests.test_app import FakeFeed

    monkeypatch.setattr(server, "get_config", lambda: cfg)
    desk = OptionsDesk(cfg=cfg, feed=FakeFeed())
    desk._feed_factory = lambda c: FakeFeed()

    async def _idle(*_a, **_k):
        return None

    monkeypatch.setattr(desk, "start", _idle)
    with TestClient(server.create_app(desk)) as client:
        assert client.get("/api/market").json()["active"] == "US"
        body = client.post("/api/market", json={"mode": "IN"}).json()
        assert body["active"] == "IN" and body["currency"] == "₹"
        assert saved_market_mode() == "IN"
        status = client.get("/api/status").json()
        assert status["market"]["active"] == "IN"
        assert status["config"]["currency"] == "₹"
        assert client.post("/api/market", json={"mode": "MARS"}).status_code == 400
        back = client.post("/api/market", json={"mode": "US"}).json()
        assert back["active"] == "US"


# --------------------------------------------------------------------------- #
# The estimated-price fallback (NSE refusing)
# --------------------------------------------------------------------------- #
def test_the_nse_expiry_calendar_weekly_and_monthly():
    from panaoptions.data.nse import expiries

    today = date(2026, 9, 28)                                   # a Monday
    nifty = expiries("NIFTY", today, {"NIFTY"})
    assert nifty[:3] == [date(2026, 9, 29), date(2026, 10, 6), date(2026, 10, 13)]
    assert all(d.weekday() == 1 for d in nifty)                 # Tuesdays
    bank = expiries("BANKNIFTY", today, {"NIFTY"})
    assert bank == [date(2026, 9, 29), date(2026, 10, 27)]      # last Tuesdays


def test_an_estimated_chain_is_priced_quoted_and_flagged():
    from panaoptions.data.nse import estimate_chain

    now = datetime(2026, 9, 28, 10, 0, tzinfo=IST)
    chain = estimate_chain("NIFTY", 24010, 0.14, 75, now, weekly={"NIFTY"},
                           steps={"NIFTY": 50}, max_dte=10)
    assert chain and all(c.estimated and c.multiplier == 75 for c in chain)
    assert {c.strike % 50 for c in chain} == {0}                # NSE's strike grid
    atm = next(c for c in chain if c.strike == 24000 and c.dte == 1
               and c.right is OptionRight.CALL)
    assert 40 < atm.mid < 150                                   # 1 day, 14% vol
    assert 0.45 < atm.delta < 0.60
    assert 0 < atm.spread_pct_of_mid <= 2.0
    far = next(c for c in chain if c.strike == 24000 and c.dte == 8
               and c.right is OptionRight.CALL)
    assert far.mid > atm.mid                                    # more time, more premium


class _RefusingNse:
    options_error = ""

    async def open(self):
        return None

    async def close(self):
        return None

    async def probe(self):
        self.options_error = "nseindia.com answered HTTP 403"
        return False

    async def chain_for_window(self, *a):
        self.options_error = "NSE option chain for NIFTY: HTTP 403"
        return []


class _Charts:
    async def quote(self, symbol):
        prices = {"^INDIAVIX": 13.5, "NIFTY": 24010.0, "RELIANCE": 2950.0}
        return {"symbol": symbol, "last_price": prices.get(symbol, 100.0)}

    async def candles(self, symbol, interval="1d", include_prepost=False):
        base = datetime(2026, 8, 1, tzinfo=UTC)
        return [Candle(ts=base + timedelta(days=i), open=2900, high=2960, low=2890,
                       close=2900 * (1.01 if i % 2 else 0.99), volume=1e6)
                for i in range(25)]


def test_when_nse_refuses_the_desk_prices_on_estimates_and_says_so(india):
    from panaoptions.data.nse import EstimatedChains, IndiaChains

    chains = IndiaChains(_RefusingNse(), EstimatedChains(_Charts(), india))
    assert asyncio.run(chains.probe()) is True
    assert chains.estimated_now and "estimated" in chains.chosen
    got = asyncio.run(chains.chain_for_window("NIFTY", 0.0, 0, 10))  # spot looked up
    assert got and all(c.estimated for c in got)
    vix_iv = got[0].implied_volatility
    assert vix_iv == pytest.approx(0.135, abs=0.001)            # India VIX 13.5
    stock = asyncio.run(chains.chain_for_window("RELIANCE", 2950.0, 0, 35))
    assert stock and all(c.multiplier == 500 for c in stock)
    assert 0.12 <= stock[0].implied_volatility <= 0.90         # realised vol, clamped


def test_estimated_trades_are_flagged_in_the_trade_and_the_audit(india):
    from panaoptions import audit
    from panaoptions.data.nse import estimate_chain
    from panaoptions.ledger.paper import PaperLedger
    from panaoptions.risk.guardrails import RiskManager

    markets.activate("IN")
    now = datetime(2026, 9, 28, 10, 0, tzinfo=IST)
    call = next(c for c in estimate_chain("NIFTY", 24000, 0.14, 75, now,
                                          weekly={"NIFTY"}, max_dte=10)
                if c.strike == 24000 and c.dte == 1 and c.right is OptionRight.CALL)
    setup = Setup(symbol="NIFTY", ts=now, direction=Direction.LONG,
                  strategy=SetupType.ORB_VWAP, pattern="x", confirmations=["a"],
                  indicators=Indicators(close=24000, atr=40), trend_aligned=True,
                  underlying_support=23950)
    risk = RiskManager(india)
    signal, refusal = risk.size(setup, call, "SIG-EST", now)
    assert signal is not None, refusal
    trade = PaperLedger(india, risk).open(signal, now)
    assert trade.estimated is True
    record = audit.record_buy(india, trade, signal, setup)
    assert record["estimated_prices"] is True
