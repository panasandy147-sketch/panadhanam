"""Alpaca adapter and the paper-vs-real-money order gate.

Uses a mocked transport rather than the live API, so the request shapes and
the response parsing are pinned without needing an account.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.agents.dispatcher import Dispatcher
from app.brokers.alpaca import AlpacaBroker
from app.brokers.paper import PaperBroker
from app.core.models import Instrument, InstrumentType, Side, SignalStatus, TradeSignal

_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _patch(monkeypatch, handler):
    transport = httpx.MockTransport(handler)

    def _factory(**kwargs):
        kwargs.pop("transport", None)
        return _REAL_ASYNC_CLIENT(transport=transport, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _factory)


def _creds(paper: str = "true") -> dict[str, str]:
    return {"api_key": "pk_test", "api_secret": "sk_test", "paper": paper}


def _routes(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/v2/account":
        # The adapter must actually send both auth headers.
        assert request.headers.get("APCA-API-KEY-ID") == "pk_test"
        assert request.headers.get("APCA-API-SECRET-KEY") == "sk_test"
        return httpx.Response(200, json={"equity": "100000", "cash": "100000",
                                         "buying_power": "200000",
                                         "status": "ACTIVE"})
    if path.endswith("/snapshot"):
        return httpx.Response(200, json={
            "latestTrade": {"p": 585.42},
            "latestQuote": {"bp": 585.40, "ap": 585.44},
            "dailyBar": {"c": 585.42, "v": 45_000_000},
            "prevDailyBar": {"c": 581.90}})
    if path.endswith("/bars"):
        return httpx.Response(200, json={"bars": [
            {"t": "2026-09-18T13:30:00Z", "o": 580.0, "h": 581.0, "l": 579.5,
             "c": 580.8, "v": 1_000_000},
            {"t": "2026-09-18T13:35:00Z", "o": 580.8, "h": 582.0, "l": 580.5,
             "c": 581.7, "v": 1_200_000}]})
    if path == "/v2/options/contracts":
        return httpx.Response(200, json={"option_contracts": [
            {"symbol": "SPY260925C00585000", "strike_price": "585", "type": "call",
             "expiration_date": "2026-09-25", "open_interest": "12000"},
            {"symbol": "SPY260925P00585000", "strike_price": "585", "type": "put",
             "expiration_date": "2026-09-25", "open_interest": "9500"}]})
    if path == "/v1beta1/options/snapshots":
        return httpx.Response(200, json={"snapshots": {
            "SPY260925C00585000": {
                "latestTrade": {"p": 4.25}, "latestQuote": {"bp": 4.2, "ap": 4.3},
                "greeks": {"delta": 0.52, "gamma": 0.03, "theta": -0.18,
                           "vega": 0.11},
                "impliedVolatility": 0.187, "dailyBar": {"v": 3400}},
            "SPY260925P00585000": {
                "latestTrade": {"p": 3.90}, "latestQuote": {"bp": 3.85, "ap": 3.95},
                "greeks": {"delta": -0.48}, "impliedVolatility": 0.191,
                "dailyBar": {"v": 2100}}}})
    if path == "/v2/orders" and request.method == "POST":
        body = json.loads(request.content)
        return httpx.Response(201, json={"id": "ord-1", "status": "accepted",
                                         "echo": body})
    if path == "/v2/positions":
        return httpx.Response(200, json=[{"symbol": "SPY", "qty": "10"}])
    return httpx.Response(404, json={"message": "not found"})


@pytest.fixture
def us(cfg):
    cfg.switch_market("US")
    yield cfg
    cfg.switch_market("IN")


# --------------------------------------------------------------------------- #
# Connection & account type
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_connects_to_the_paper_endpoint_by_default(us, monkeypatch):
    """You must opt IN to real money, never out of it."""
    _patch(monkeypatch, _routes)
    b = AlpacaBroker(credentials=_creds())
    assert await b.connect() is True
    assert b.is_paper_account is True
    await b.disconnect()


@pytest.mark.asyncio
async def test_real_money_is_flagged_when_paper_is_false(us, monkeypatch):
    _patch(monkeypatch, _routes)
    b = AlpacaBroker(credentials=_creds(paper="false"))
    await b.connect()
    assert b.is_paper_account is False
    await b.disconnect()


@pytest.mark.asyncio
async def test_missing_credentials_fail_cleanly(us):
    b = AlpacaBroker(credentials={})
    assert await b.connect() is False


@pytest.mark.asyncio
async def test_bad_credentials_return_false_not_an_exception(us, monkeypatch):
    def handler(request):
        return httpx.Response(401, json={"message": "unauthorized"})
    _patch(monkeypatch, handler)
    assert await AlpacaBroker(credentials=_creds()).connect() is False


# --------------------------------------------------------------------------- #
# Market data
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_quote_computes_change_from_the_previous_close(us, monkeypatch):
    _patch(monkeypatch, _routes)
    b = AlpacaBroker(credentials=_creds())
    await b.connect()
    q = await b.get_quote("SPY")
    assert q.last_price == pytest.approx(585.42)
    assert q.change_pct == pytest.approx((585.42 - 581.90) / 581.90 * 100, abs=0.01)
    await b.disconnect()


@pytest.mark.asyncio
async def test_candles_parse_in_chronological_order(us, monkeypatch):
    _patch(monkeypatch, _routes)
    b = AlpacaBroker(credentials=_creds())
    await b.connect()
    candles = await b.get_candles("SPY", "5m", 50)
    assert len(candles) == 2
    assert candles[0].ts < candles[1].ts
    assert candles[-1].close == pytest.approx(581.7)
    await b.disconnect()


@pytest.mark.asyncio
async def test_option_chain_carries_oi_iv_and_greeks(us, monkeypatch):
    _patch(monkeypatch, _routes)
    b = AlpacaBroker(credentials=_creds(), config={"strikes_around_atm": 5})
    await b.connect()
    chain = await b.get_option_chain("SPY")

    assert chain is not None
    assert chain.expiry == "2026-09-25"
    call = next(leg for leg in chain.legs if leg.option_type == "CE")
    assert call.oi == 12_000
    assert call.iv == pytest.approx(0.187)
    assert call.delta == pytest.approx(0.52)
    # Mid beats a stale last print on a thin strike.
    assert call.ltp == pytest.approx(4.25)
    await b.disconnect()


@pytest.mark.asyncio
async def test_chain_feeds_the_derivatives_maths(us, monkeypatch):
    from app.indicators.derivatives import analyse
    _patch(monkeypatch, _routes)
    b = AlpacaBroker(credentials=_creds())
    await b.connect()
    chain = await b.get_option_chain("SPY")
    metrics = analyse(chain, 0.6, {})
    assert metrics["pcr_oi"] == pytest.approx(9500 / 12000, abs=0.01)
    assert metrics["atm_iv"] > 0
    await b.disconnect()


# --------------------------------------------------------------------------- #
# Orders
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_limit_order_sends_the_right_shape(us, monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/orders" and request.method == "POST":
            captured.update(json.loads(request.content))
        return _routes(request)

    _patch(monkeypatch, handler)
    b = AlpacaBroker(credentials=_creds())
    await b.connect()

    inst = Instrument(symbol="SPY", tradingsymbol="SPY",
                      instrument_type=InstrumentType.EQUITY, exchange="ARCA")
    result = await b.place_order(inst, Side.BUY, 10, 585.40, "LIMIT", tag="SIG-1")

    assert result.ok and result.order_id == "ord-1"
    assert captured["symbol"] == "SPY"
    assert captured["side"] == "buy"
    assert captured["qty"] == "10"
    assert captured["type"] == "limit"
    assert captured["limit_price"] == "585.40"
    # US brokers have no intraday product code; MIS maps to a day order.
    assert captured["time_in_force"] == "day"
    await b.disconnect()


@pytest.mark.asyncio
async def test_a_rejected_order_reports_the_reason(us, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/orders" and request.method == "POST":
            return httpx.Response(403, json={"message": "insufficient buying power"})
        return _routes(request)

    _patch(monkeypatch, handler)
    b = AlpacaBroker(credentials=_creds())
    await b.connect()
    inst = Instrument(symbol="SPY", tradingsymbol="SPY")
    result = await b.place_order(inst, Side.BUY, 10_000, 585.0)
    assert result.ok is False
    assert "buying power" in result.message
    await b.disconnect()


# --------------------------------------------------------------------------- #
# The gate that matters: practice vs real money
# --------------------------------------------------------------------------- #
def _signal() -> TradeSignal:
    return TradeSignal(
        id="SIG-GATE",
        instrument=Instrument(symbol="SPY", tradingsymbol="SPY"),
        side=Side.BUY, entry=585.0, stop_loss=580.0, target=595.0,
        quantity=10, risk_reward=2.0, status=SignalStatus.APPROVED)


@pytest.mark.asyncio
async def test_a_paper_account_places_orders_without_the_live_switches(cfg):
    """Sending an order to a simulator IS the practice. Gating it behind the
    real-money switches would make practising impossible."""
    cfg.settings["execution"]["auto_place_orders"] = True
    broker = PaperBroker(config={"total_capital": 100_000})
    await broker.connect()
    assert broker.is_paper_account is True

    result = await Dispatcher(broker, cfg).dispatch(_signal())
    assert result["order"]["ok"] is True
    cfg.settings["execution"]["auto_place_orders"] = False


@pytest.mark.asyncio
async def test_a_real_money_account_still_needs_every_switch(cfg, monkeypatch):
    """The three-switch guard must survive: a real account without them stays
    alert-only no matter what the broker supports."""
    monkeypatch.delenv("TRADING_MODE", raising=False)
    monkeypatch.delenv("ENABLE_LIVE_ORDERS", raising=False)
    cfg.settings["execution"]["auto_place_orders"] = True

    broker = PaperBroker(config={"total_capital": 100_000})
    await broker.connect()
    broker.is_paper_account = False        # pretend it is real money

    result = await Dispatcher(broker, cfg).dispatch(_signal())
    assert result["order"]["ok"] is False
    assert "alert-only" in result["order"]["message"]
    cfg.settings["execution"]["auto_place_orders"] = False


@pytest.mark.asyncio
async def test_auto_place_orders_off_means_no_order_anywhere(cfg):
    cfg.settings["execution"]["auto_place_orders"] = False
    broker = PaperBroker(config={"total_capital": 100_000})
    await broker.connect()
    result = await Dispatcher(broker, cfg).dispatch(_signal())
    assert result["order"]["ok"] is False
