"""The blended paper profile: active entries, bounded risk.

Each case is one of the recommended rules: 1% per trade, a 4% portfolio heat
cap, a 4% daily circuit breaker that flattens and locks, 3-7 DTE options, an
IV rank ceiling, a macro news blackout, and no chart trade against the
options read.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.agents.cmio import CMIOAgent
from app.agents.risk import RiskManager
from app.core import blackout
from app.core.models import AgentReport, Bias, MarketContext, NewsItem, Quote, TradeSignal

ET = ZoneInfo("America/New_York")


def test_the_shipped_numbers(cfg):
    assert cfg.get("risk.risk_per_trade_pct") == 1.0
    assert cfg.get("risk.max_daily_loss_pct") == 4.0
    assert cfg.get("risk.max_portfolio_heat_pct") == 4.0
    assert cfg.get("risk.reject_if_iv_rank_above") == 80.0
    assert (cfg.get("derivatives.min_days_to_expiry"),
            cfg.get("derivatives.max_days_to_expiry")) == (3, 7)
    assert cfg.get("consensus.conflict_policy") == "technicals_unless_derivatives_oppose"


# --------------------------------------------------------------------------- #
@pytest.fixture
def rm(cfg):
    cfg.settings["system"]["no_new_entry_after"] = "23:59"
    m = RiskManager(cfg)
    m.set_capital(100_000)
    return m


def _ctx(price=1000.0):
    return MarketContext(symbol="RELIANCE", cycle_id="t",
                         quote=Quote(symbol="RELIANCE", last_price=price),
                         indicators={"primary": {"atr": 8.0}})


def test_heat_cap_trims_the_trade_that_would_breach_it(rm):
    rm.state.open_risk = 3_500.0             # 3.5% already at risk
    sig = rm.evaluate(_ctx(), Bias.BULLISH, [], 0.6, ["candlestick"])
    assert sig.total_risk <= 500.0 + 1e-6    # only the 0.5% left
    assert "portfolio heat" in sig.rationale


def test_heat_cap_refuses_when_full(rm):
    rm.state.open_risk = 4_000.0
    assert any("Portfolio heat cap" in r for r in rm.desk_checks())


def test_heat_is_counted_on_open_and_released_on_close(rm):
    sig = TradeSignal(id="S", symbol="X",
                      instrument={"symbol": "X", "tradingsymbol": "X"},
                      side="BUY", entry=100, stop_loss=99, target=102,
                      total_risk=1000.0, notional=10_000.0)
    rm.register_open(sig)
    assert rm.state.open_risk == 1000.0
    rm.register_close(sig, -1000.0)
    assert rm.state.open_risk == 0.0


def test_a_restart_restores_what_is_still_open(rm):
    import json
    payload = TradeSignal(id="S", symbol="X",
                          instrument={"symbol": "X", "tradingsymbol": "X"},
                          side="BUY", entry=100, stop_loss=99, target=102,
                          total_risk=1000.0, notional=10_000.0).model_dump_json()
    assert rm.restore_open([{"payload": payload}, {"payload": json.dumps({})}]) == 1
    assert rm.state.open_positions == 1 and rm.state.open_risk == 1000.0


# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_circuit_breaker_flattens_halts_and_disarms(cfg, monkeypatch):
    from app.learning.outcomes import OutcomeTracker

    rm = RiskManager(cfg)
    rm.set_capital(100_000)
    rm.state.realised_pnl = -3_800.0

    class _Day:
        disarmed = ""

        def _auto_disarm(self, reason):
            self.disarmed = reason

        def status(self):
            return {}

    tracker = OutcomeTracker(broker=None, cfg=cfg, risk_manager=rm)
    tracker.trading_day = _Day()
    closed: list[str] = []

    async def _close(row, price, status, detail):
        closed.append(detail)
        return {"signal_id": row["id"]}

    monkeypatch.setattr(tracker, "_close", _close)
    rm.set_unrealised(-300.0)                          # -4,100 in total
    out = await tracker._maybe_trip_breaker([({"id": "A"}, 99.0), ({"id": "B"}, 50.0)])
    assert closed == ["circuit_breaker", "circuit_breaker"] and len(out) == 2
    assert rm.state.halted and "circuit breaker" in rm.state.halt_reason
    assert tracker.trading_day.disarmed


@pytest.mark.asyncio
async def test_circuit_breaker_waits_until_the_limit(cfg):
    from app.learning.outcomes import OutcomeTracker

    rm = RiskManager(cfg)
    rm.set_capital(100_000)
    rm.state.realised_pnl = -3_000.0
    tracker = OutcomeTracker(broker=None, cfg=cfg, risk_manager=rm)
    assert await tracker._maybe_trip_breaker([({"id": "A"}, 99.0)]) == []
    assert not rm.state.halted


# --------------------------------------------------------------------------- #
def _cmio_vote(cfg, reports):
    return CMIOAgent(cfg)._weighted_vote(MarketContext(symbol="SPY", cycle_id="t"),
                                         reports)


def _r(agent, score):
    bias = Bias.BULLISH if score > 0 else Bias.BEARISH
    return AgentReport(agent_id=agent, symbol="SPY", bias=bias, score=score,
                       confidence=0.8)


def test_the_chart_does_not_trade_against_the_options_read(cfg):
    d = _cmio_vote(cfg, [_r("candlestick", 0.6), _r("derivatives", -0.4)])
    assert d["proceed"] is False
    assert any("oppose the chart" in c for c in d["conflicts"])


def test_the_chart_still_wins_against_news(cfg):
    d = _cmio_vote(cfg, [_r("candlestick", 0.6), _r("news_sentiment", -0.4)])
    assert d["proceed"] is True and d["composite_score"] > 0


# --------------------------------------------------------------------------- #
def test_a_scheduled_release_starts_a_blackout(cfg):
    cfg.switch_market("US")
    try:
        at = datetime(2026, 10, 28, 13, 50, tzinfo=ET)       # FOMC 14:00
        assert "FOMC" in blackout.reason(cfg, [], now=at.astimezone(UTC))
        after = datetime(2026, 10, 28, 14, 16, tzinfo=ET)
        assert blackout.reason(cfg, [], now=after.astimezone(UTC)) == ""
    finally:
        cfg.switch_market("IN")


def test_a_fresh_macro_headline_starts_a_blackout(cfg):
    now = datetime.now(UTC)
    fresh = NewsItem(title="Fed decision: rates held", source="x",
                     published=now - timedelta(minutes=3))
    stale = NewsItem(title="CPI report hotter than expected", source="x",
                     published=now - timedelta(minutes=40))
    assert "Fed decision" in blackout.reason(cfg, [fresh], now=now)
    assert blackout.reason(cfg, [stale], now=now) == ""


def test_a_blackout_refuses_new_entries(rm):
    rm.blackout_reason = "News blackout: FOMC decision at 14:00"
    assert "News blackout: FOMC decision at 14:00" in rm.desk_checks()


# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_the_chain_is_taken_from_the_3_to_7_day_window(cfg):
    from app.data.market import MarketDataService

    today = date.today()
    asked: list = []

    class _Broker:
        supports_options = True

        async def get_expiries(self, symbol):
            return [(today + timedelta(days=d)).isoformat() for d in (0, 1, 5, 12)]

        async def get_option_chain(self, symbol, expiry=None):
            asked.append(expiry)

    svc = MarketDataService(_Broker(), cfg)
    await svc._chain_in_window("RELIANCE")
    assert asked == [(today + timedelta(days=5)).isoformat()]


@pytest.mark.asyncio
async def test_no_chain_rather_than_a_0dte_one(cfg):
    from app.data.market import MarketDataService

    today = date.today()

    class _Broker:
        async def get_expiries(self, symbol):
            return [today.isoformat(), (today + timedelta(days=1)).isoformat()]

        async def get_option_chain(self, symbol, expiry=None):
            raise AssertionError("must not fetch a 0-1 DTE chain")

    assert await MarketDataService(_Broker(), cfg)._chain_in_window("X") is None


def test_iv_rank_refuses_rich_premium(rm, monkeypatch):
    from app.storage import db

    monkeypatch.setattr(db, "iv_history", lambda symbol, days=252: [10.0 + i for i in range(30)])
    ctx = _ctx()
    ctx.indicators["derivatives"] = {"atm_iv": 38.0}           # rank ~97
    assert "IV rank" in rm._iv_too_rich(ctx)
    ctx.indicators["derivatives"] = {"atm_iv": 20.0}           # rank ~34
    assert rm._iv_too_rich(ctx) == ""
