"""The high-risk paper profile: what lets a lone strong reading trade, and
the guards that stop that from becoming noise."""
from __future__ import annotations

import pytest

from app.agents.cmio import CMIOAgent
from app.core.models import AgentReport, Bias, MarketContext, OptionChain


def _r(agent: str, score: float, conf: float = 0.8) -> AgentReport:
    bias = Bias.BULLISH if score > 0 else Bias.BEARISH if score < 0 else Bias.NEUTRAL
    return AgentReport(agent_id=agent, symbol="SPY", bias=bias, score=score,
                       confidence=conf)


@pytest.fixture
def cmio(cfg):
    return CMIOAgent(cfg)


def _vote(cmio, reports):
    return cmio._weighted_vote(MarketContext(symbol="SPY", cycle_id="t"), reports)


def test_quiet_analysts_do_not_water_down_a_real_reading(cmio):
    """From the desk's own log: candlestick +0.36, three analysts near zero,
    composite +0.12 — under any bar, so nothing could ever trade."""
    d = _vote(cmio, [_r("candlestick", 0.36), _r("news_sentiment", 0.0),
                     _r("macro_flow", 0.02), _r("derivatives", -0.05)])
    assert d["composite_score"] == pytest.approx(0.36, abs=0.01)
    assert d["proceed"] is True


def test_the_conservative_setting_still_dilutes(cmio, cfg):
    cfg.settings["consensus"]["silent_analysts_dilute"] = True
    d = _vote(cmio, [_r("candlestick", 0.36), _r("news_sentiment", 0.0),
                     _r("macro_flow", 0.02)])
    assert d["composite_score"] < 0.25 and d["proceed"] is False


def test_news_alone_never_trades(cmio):
    """One bearish headline scored -0.36 on every name at 15:38. Alone it
    would have shorted the whole watchlist."""
    d = _vote(cmio, [_r("news_sentiment", -0.36), _r("candlestick", 0.02)])
    assert d["proceed"] is False
    assert "news or macro alone does not trade" in d["rationale"]


def test_news_can_still_support_a_chart_led_trade(cmio):
    d = _vote(cmio, [_r("candlestick", 0.40), _r("news_sentiment", 0.30)])
    assert d["proceed"] is True


def test_a_simulated_chain_gets_no_vote(cfg):
    """Real prices beside invented open interest: the banner says so, and the
    options analyst must not vote on it."""
    from app.agents.derivatives import DerivativesAgent

    ctx = MarketContext(symbol="SPY", cycle_id="t",
                        option_chain=OptionChain(underlying="SPY", spot=500,
                                                 expiry="2026-10-02", synthetic=True),
                        indicators={"derivatives": {"buildup_direction": 1}})
    report = DerivativesAgent(cfg).analyse_rules(ctx)
    assert report.data_available is False
    assert "simulated" in report.rationale


@pytest.mark.parametrize("market", ["US", "IN"])
def test_each_market_scans_three_bands_of_twenty(cfg, market):
    cfg.switch_market(market)
    try:
        bands: dict[str, int] = {}
        for item in cfg.watchlist():
            bands[item["band"]] = bands.get(item["band"], 0) + 1
        assert bands == {"A": 20, "B": 20, "C": 20}
    finally:
        cfg.switch_market("IN")


def test_a_band_can_be_switched_off(cfg):
    cfg.switch_market("US")
    try:
        cfg.universe["active_bands"] = ["A"]
        symbols = {i["symbol"] for i in cfg.watchlist()}
        assert "SPY" in symbols and "PLTR" not in symbols and len(symbols) == 20
    finally:
        cfg.switch_market("IN")


@pytest.mark.asyncio
async def test_cash_only_names_are_not_asked_for_a_chain(cfg):
    """Forty cash-only Indian names asking NSE for a chain every minute is how
    a feed gets rate-limited."""
    from app.data.market import MarketDataService

    asked: list[str] = []

    class _Broker:
        supports_options = True

        async def get_quote(self, symbol):
            return None

        async def get_candles(self, symbol, tf, count=200):
            return []

        async def get_option_chain(self, symbol):
            asked.append(symbol)
            return None

    cfg.switch_market("IN")
    svc = MarketDataService(_Broker(), cfg)
    await svc.build_context(symbol="TRENT", cycle_id="t")       # fno: false
    await svc.build_context(symbol="RELIANCE", cycle_id="t")    # fno: true
    assert asked == ["RELIANCE"]


@pytest.mark.asyncio
async def test_daily_bars_are_not_refetched_every_minute(cfg):
    from app.data.market import MarketDataService

    calls: list[str] = []

    class _Broker:
        async def get_candles(self, symbol, tf, count=200):
            calls.append(tf)
            return [object()]

    svc = MarketDataService(_Broker(), cfg)
    for _ in range(3):
        await svc._candles("SPY", "1d")
        await svc._candles("SPY", "5m")
    assert calls.count("1d") == 1
    assert calls.count("5m") == 3


def test_one_tight_stop_cannot_take_the_whole_exposure(cfg):
    """A $3 stop on QQQ at 2% risk sized to ~$333k — nearly all of a $400k
    limit — and every later setup was refused for want of room."""
    from app.agents.risk import RiskManager
    from app.core.models import Quote

    cfg.switch_market("US")
    try:
        cfg.settings["system"]["no_new_entry_after"] = "23:59"
        rm = RiskManager(cfg)
        rm.set_capital(100_000)
        ctx = MarketContext(symbol="QQQ", cycle_id="t",
                            quote=Quote(symbol="QQQ", last_price=505.0),
                            indicators={"primary": {"atr": 2.0}})
        sig = rm.evaluate(ctx, Bias.BULLISH, [], 0.6, ["candlestick"])
        limit = rm._max_exposure()
        slots = int(cfg.get("risk.max_open_positions"))
        assert sig.notional <= limit / slots + 505.0
    finally:
        cfg.switch_market("IN")
