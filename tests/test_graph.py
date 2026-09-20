"""End-to-end: data → analysts → CMIO → risk → dispatch."""
from __future__ import annotations

import pytest

from app.agents.graph import TradingDesk
from app.agents.risk import RiskManager
from app.brokers.paper import PaperBroker
from app.core.models import Bias, SignalStatus


# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_paper_broker_serves_a_full_dataset():
    b = PaperBroker(config={"total_capital": 100_000})
    await b.connect()

    quote = await b.get_quote("NIFTY")
    assert quote and quote.last_price > 0

    candles = await b.get_candles("NIFTY", "5m", 100)
    assert len(candles) == 100
    assert all(c.high >= c.low for c in candles)
    assert all(c.high >= c.close >= c.low for c in candles)

    chain = await b.get_option_chain("NIFTY")
    assert chain and len(chain.legs) > 20
    assert {leg.option_type for leg in chain.legs} == {"CE", "PE"}


@pytest.mark.asyncio
async def test_context_builder_populates_indicators(cfg):
    from app.data.market import MarketDataService
    b = PaperBroker(config={"total_capital": 100_000})
    await b.connect()

    ctx = await MarketDataService(b, cfg).build_context("NIFTY", "test-cycle")
    assert ctx.quote is not None
    assert ctx.indicators["primary"]["last_close"] > 0
    assert "derivatives" in ctx.indicators
    assert ctx.indicators["derivatives"]["pcr_oi"] >= 0
    assert ctx.regime is not None


@pytest.mark.asyncio
async def test_full_cycle_runs_and_returns_a_result(cfg):
    from app.data.market import MarketDataService
    b = PaperBroker(config={"total_capital": 100_000})
    await b.connect()
    cfg.settings["system"]["no_new_entry_after"] = "23:59"

    desk = TradingDesk(b, cfg, risk_manager=RiskManager(cfg))
    ctx = await MarketDataService(b, cfg).build_context("NIFTY", "cy-1")
    result = await desk.run_cycle(ctx)

    assert result.symbol == "NIFTY"
    assert isinstance(result.bias, Bias)
    assert -1.0 <= result.composite_score <= 1.0
    assert len(result.reports) >= 2
    # Either a signal was produced, or there is a stated reason why not.
    assert result.signal is not None or result.rejected


@pytest.mark.asyncio
async def test_analysts_that_lack_data_abstain_rather_than_agree(cfg):
    """The whole design rests on this: no data must never read as consensus."""
    from app.agents.macro_flow import MacroFlowAgent
    from app.agents.news_sentiment import NewsSentimentAgent
    from app.core.models import MarketContext

    ctx = MarketContext(symbol="NIFTY", cycle_id="c", news=[], macro=None)
    for agent in (NewsSentimentAgent(cfg), MacroFlowAgent(cfg)):
        report = agent.analyse_rules(ctx)
        assert report.data_available is False
        assert report.score == 0.0


@pytest.mark.asyncio
async def test_cmio_excludes_abstaining_agents_from_the_vote(cfg):
    from app.agents.cmio import CMIOAgent
    from app.core.models import AgentReport, MarketContext

    ctx = MarketContext(symbol="NIFTY", cycle_id="c")
    reports = [
        AgentReport(agent_id="candlestick", symbol="NIFTY", score=0.8,
                    confidence=0.9, bias=Bias.BULLISH),
        AgentReport(agent_id="derivatives", symbol="NIFTY", score=0.6,
                    confidence=0.8, bias=Bias.BULLISH),
        AgentReport(agent_id="news_sentiment", symbol="NIFTY", score=0.0,
                    confidence=0.0, data_available=False),
    ]
    decision = CMIOAgent(cfg)._weighted_vote(ctx, reports)
    assert decision["bias"] == Bias.BULLISH
    assert decision["composite_score"] > 0.5
    assert "news_sentiment" in decision["abstained"]


@pytest.mark.asyncio
async def test_min_confirmations_gate_blocks_a_lone_voice(cfg):
    from app.agents.cmio import CMIOAgent
    from app.core.models import AgentReport, MarketContext

    cfg.settings["consensus"]["min_confirmations"] = 2
    ctx = MarketContext(symbol="NIFTY", cycle_id="c")
    reports = [
        AgentReport(agent_id="candlestick", symbol="NIFTY", score=0.9,
                    confidence=0.9, bias=Bias.BULLISH),
        AgentReport(agent_id="derivatives", symbol="NIFTY", score=0.05,
                    confidence=0.5, bias=Bias.NEUTRAL),
    ]
    decision = CMIOAgent(cfg)._weighted_vote(ctx, reports)
    assert decision["proceed"] is False
    assert "confirmations" in decision["rationale"]


@pytest.mark.asyncio
async def test_high_impact_bearish_news_vetoes_a_long(cfg):
    from app.agents.cmio import CMIOAgent
    from app.core.models import AgentReport, MarketContext

    ctx = MarketContext(symbol="NIFTY", cycle_id="c")
    reports = [
        AgentReport(agent_id="candlestick", symbol="NIFTY", score=0.9,
                    confidence=0.9, bias=Bias.BULLISH),
        AgentReport(agent_id="derivatives", symbol="NIFTY", score=0.8,
                    confidence=0.9, bias=Bias.BULLISH),
        AgentReport(agent_id="news_sentiment", symbol="NIFTY", score=-0.9,
                    confidence=0.9, bias=Bias.BEARISH,
                    extra={"has_high_impact": True}),
    ]
    decision = CMIOAgent(cfg)._weighted_vote(ctx, reports)
    assert decision["proceed"] is False
    assert any("veto" in c.lower() for c in decision["conflicts"])


@pytest.mark.asyncio
async def test_fundamental_failure_vetoes_the_symbol(cfg):
    from app.agents.cmio import CMIOAgent
    from app.core.models import AgentReport, MarketContext

    ctx = MarketContext(symbol="RELIANCE", cycle_id="c")
    reports = [
        AgentReport(agent_id="candlestick", symbol="RELIANCE", score=0.9,
                    confidence=0.9, bias=Bias.BULLISH),
        AgentReport(agent_id="derivatives", symbol="RELIANCE", score=0.8,
                    confidence=0.9, bias=Bias.BULLISH),
        AgentReport(agent_id="fundamental", symbol="RELIANCE", score=0.0,
                    confidence=0.3, extra={"eligible": False, "fails": ["ROE", "Liquidity"]}),
    ]
    decision = CMIOAgent(cfg)._weighted_vote(ctx, reports)
    assert decision["proceed"] is False
    assert any("Fundamental filter" in c for c in decision["conflicts"])


@pytest.mark.asyncio
async def test_approved_signal_reaches_the_dispatcher_and_is_alert_only(cfg):
    """auto_place_orders defaults to false — an approved signal must alert, not trade."""
    from app.agents.dispatcher import Dispatcher
    from app.core.models import Instrument, Side, TradeSignal

    b = PaperBroker(config={"total_capital": 100_000})
    await b.connect()
    cfg.settings["execution"]["auto_place_orders"] = False

    signal = TradeSignal(
        id="SIG-TEST", instrument=Instrument(symbol="NIFTY", tradingsymbol="NIFTY",
                                             lot_size=75),
        side=Side.BUY, entry=100.0, stop_loss=90.0, target=120.0,
        quantity=75, lots=1, risk_reward=2.0, status=SignalStatus.APPROVED,
        confirmations=["breakout", "OI buildup"],
    )
    result = await Dispatcher(b, cfg).dispatch(signal)
    assert result["dispatched"] is True
    assert result["order"]["ok"] is False
    assert "alert-only" in result["order"]["message"]


@pytest.mark.asyncio
async def test_alert_line_matches_the_desk_format():
    from app.core.models import Instrument, InstrumentType, Side, TradeSignal

    signal = TradeSignal(
        id="S1",
        instrument=Instrument(symbol="NIFTY", tradingsymbol="NIFTY25JAN24500CE",
                              instrument_type=InstrumentType.CALL, strike=24_500,
                              lot_size=75),
        side=Side.BUY, entry=120.5, stop_loss=78.3, target=205.0,
        risk_reward=2.0, confirmations=["breakout", "long buildup"],
    )
    line = signal.alert_line()
    assert "NIFTY" in line and "24500 CE" in line
    assert "BUY" in line and "ENTRY 120.50" in line
    assert "SL 78.30" in line and "TGT 205.00" in line
    assert "breakout, long buildup" in line
