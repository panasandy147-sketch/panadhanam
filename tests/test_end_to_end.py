"""End to end: when the criteria are met the desk paper-trades, and it records
why it bought and why it sold.

Everything here is the real pipeline — the CMIO's vote, the risk manager's
sizing and gates, the dispatcher's arming check, the paper broker's fill, the
outcome tracker's exit and the journal's grading. Only the analysts' votes
are scripted, so each case meets (or misses) the criteria exactly; the
analysts themselves are tested on real candles elsewhere.

The criteria being exercised, from config/settings.yaml:
    consensus.min_confirmations   2      analysts agreeing at |score| >= 0.25
    consensus.min_composite_score 0.35   weighted vote strength
    risk.min_risk_reward          2.0    target at least twice the stop
    plus: no news or fundamental veto, a sane stop distance, a size that fits
    the capital, and a trading day that is armed.
"""
from __future__ import annotations

import json

import pytest

from app.brokers.paper import PaperBroker
from app.core.models import AgentReport, Bias, Quote, SignalStatus
from app.scheduler import TradingEngine
from app.storage import db

SYMBOL = "RELIANCE"


# --------------------------------------------------------------------------- #
@pytest.fixture
async def engine(cfg, monkeypatch):
    cfg.switch_market("IN")
    cfg.settings["system"]["square_off_time"] = "23:59"
    cfg.settings["system"]["no_new_entry_after"] = "23:59"
    cfg.settings["execution"]["auto_place_orders"] = True
    broker = PaperBroker(config={"total_capital": 100_000})
    await broker.connect()
    eng = TradingEngine(broker, cfg)
    eng.risk.set_capital(100_000)

    # No network in tests: nothing from the news or macro feeds.
    async def _nothing():
        return []

    async def _no_macro():
        return None

    monkeypatch.setattr(eng.news, "fetch", _nothing)
    monkeypatch.setattr(eng.macro, "fetch", _no_macro)
    # Start each case with an empty book.
    for row in db.open_signals():
        db.update_outcome(row["id"], row["entry"], 0.0, 0.0, "CLOSED_TIME")
    yield eng
    cfg.settings["execution"]["auto_place_orders"] = False
    # The trades these cases close are graded into the shared journal; leave
    # it as it was so other tests' totals are not these trades' P&L.
    from app.journal import store
    store.init_journal()
    conn = db.get_conn()
    conn.execute("DELETE FROM journal_entries WHERE id LIKE 'LIVE-%'")
    conn.commit()


def _votes(engine, votes: dict[str, tuple[float, float]], *,
           invalidation_pct: float = 1.0, eligible: bool = True,
           news_high_impact: bool = False) -> None:
    """Replace each analyst's run() with a scripted report.

    votes maps agent_id -> (score, confidence). The candlestick analyst names
    a structural stop `invalidation_pct` below the price, which is what the
    risk manager prefers over an ATR stop.
    """
    for agent in engine.desk.analysts:
        aid = agent.agent_id

        async def _run(ctx, aid=aid):
            price = ctx.quote.last_price
            if aid == "fundamental":
                return AgentReport(agent_id=aid, symbol=ctx.symbol,
                                   confidence=1.0, extra={"eligible": eligible,
                                   "fails": [] if eligible else ["debt/equity 3.1"]},
                                   rationale="passes the quality screen"
                                   if eligible else "fails the quality screen")
            score, confidence = votes.get(aid, (0.0, 0.5))
            bias = (Bias.BULLISH if score > 0 else
                    Bias.BEARISH if score < 0 else Bias.NEUTRAL)
            report = AgentReport(
                agent_id=aid, symbol=ctx.symbol, bias=bias, score=score,
                confidence=confidence,
                rationale={
                    "candlestick": "Bullish engulfing on volume 2.4x average, "
                                   "above VWAP, EMA 9 over EMA 21",
                    "derivatives": "Put writing at the ATM strike; PCR 1.3",
                    "news_sentiment": "Mildly positive coverage",
                    "macro_flow": "FII net buyers",
                }.get(aid, ""),
            )
            if aid == "candlestick":
                report.invalidation_level = round(
                    price * (1 - invalidation_pct / 100), 2)
            if aid == "news_sentiment" and news_high_impact:
                report.extra["has_high_impact"] = True
            return report

        agent.run = _run


def _price(engine, monkeypatch, last: float) -> None:
    """Move the market. The outcome tracker marks positions from get_quote."""
    async def _quote(symbol):
        return Quote(symbol=symbol, last_price=last, bid=last - 0.05,
                     ask=last + 0.05)

    monkeypatch.setattr(engine.broker, "get_quote", _quote)


# --------------------------------------------------------------------------- #
# Criteria met → it buys, on paper, and says why
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_criteria_met_places_a_paper_trade_and_records_why(engine):
    await engine.trading_day.start()
    _votes(engine, {"candlestick": (0.7, 0.9), "derivatives": (0.6, 0.8),
                    "news_sentiment": (0.3, 0.6), "macro_flow": (0.1, 0.5)})

    [result] = await engine.run_cycle([SYMBOL])

    # It traded.
    assert result["signal_id"], f"no trade: {result['rejected']}"
    open_rows = [r for r in db.open_signals() if r["id"] == result["signal_id"]]
    assert open_rows, "an approved signal must become an open position"
    row = open_rows[0]
    assert row["status"] == SignalStatus.OPEN.value
    assert row["side"] == "BUY" and row["quantity"] > 0

    # On paper, through the broker.
    assert engine.broker._orders, "the paper broker must have taken the order"

    # And it says why: which analysts agreed, the vote, and how it was sized.
    confirmations = json.loads(row["confirmations"])
    assert len(confirmations) >= 2
    assert any("candlestick" in c for c in confirmations)
    assert any("derivatives" in c for c in confirmations)
    assert "Weighted vote" in row["rationale"]
    assert "structural stop" in row["rationale"]      # the stop has a reason
    assert row["counter_argument"]                    # recorded BEFORE the outcome
    # Stop below entry, target at least 2R above it.
    assert row["stop_loss"] < row["entry"] < row["target"]
    assert row["risk_reward"] >= 2.0


@pytest.mark.asyncio
async def test_each_analysts_reasoning_is_kept_with_the_trade(engine):
    """The weekly review shows what every analyst said at entry. That only
    works if the reports are stored against the trade that used them."""
    await engine.trading_day.start()
    _votes(engine, {"candlestick": (0.7, 0.9), "derivatives": (0.6, 0.8)})
    [result] = await engine.run_cycle([SYMBOL])
    assert result["signal_id"]

    reports = db.reports_for_signal(result["signal_id"])
    by_agent = {r["agent_id"]: r for r in reports}
    assert "Bullish engulfing" in by_agent["candlestick"]["rationale"]
    assert "Put writing" in by_agent["derivatives"]["rationale"]


# --------------------------------------------------------------------------- #
# Criteria not met → no trade, and the reason is stated
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_one_voice_is_not_enough(engine):
    await engine.trading_day.start()
    _votes(engine, {"candlestick": (0.8, 0.9)})           # alone

    [result] = await engine.run_cycle([SYMBOL])
    assert result["signal_id"] is None
    assert any("need 2" in r for r in result["rejected"])
    assert not engine.broker._orders


@pytest.mark.asyncio
async def test_a_weak_vote_is_not_enough(engine):
    await engine.trading_day.start()
    # Two confirmations, but diluted by two neutral analysts at high confidence.
    _votes(engine, {"candlestick": (0.3, 0.3), "derivatives": (0.3, 0.3),
                    "news_sentiment": (0.0, 1.0), "macro_flow": (0.0, 1.0)})

    [result] = await engine.run_cycle([SYMBOL])
    assert result["signal_id"] is None
    assert any("neutral band" in r for r in result["rejected"])


@pytest.mark.asyncio
async def test_a_failed_quality_screen_vetoes_a_good_setup(engine):
    await engine.trading_day.start()
    _votes(engine, {"candlestick": (0.7, 0.9), "derivatives": (0.6, 0.8)},
           eligible=False)
    [result] = await engine.run_cycle([SYMBOL])
    assert result["signal_id"] is None
    assert not engine.broker._orders


@pytest.mark.asyncio
async def test_a_stop_too_wide_is_rejected_by_risk_with_the_number(engine, cfg,
                                                                    monkeypatch):
    """The CMIO likes it; the risk manager does not. The reason carries the
    arithmetic, so it can be acted on rather than guessed at.

    The stop here is a sane 1% structural level; the cap is lowered under it,
    which is the same test as a wide stop against the shipped 3% cap.
    """
    monkeypatch.setitem(cfg.settings["risk"], "max_stop_distance_pct", 0.5)
    await engine.trading_day.start()
    _votes(engine, {"candlestick": (0.7, 0.9), "derivatives": (0.6, 0.8)},
           invalidation_pct=1.0)
    [result] = await engine.run_cycle([SYMBOL])
    assert result["signal_id"] is None
    assert any("too wide" in r and "max 0.5%" in r for r in result["rejected"])
    assert not engine.broker._orders


@pytest.mark.asyncio
async def test_a_wild_invalidation_level_is_replaced_by_an_atr_stop(engine):
    """A level far from the price (here 6%) is not a stop anyone would honour.
    The desk sets an ATR stop instead of sizing a position down to nothing
    around it, and says in the rationale that it did."""
    await engine.trading_day.start()
    _votes(engine, {"candlestick": (0.7, 0.9), "derivatives": (0.6, 0.8)},
           invalidation_pct=6.0)
    [result] = await engine.run_cycle([SYMBOL])
    assert result["signal_id"], result["rejected"]
    row = next(r for r in db.recent_signals(limit=20)
               if r["id"] == result["signal_id"])
    assert "ATR stop" in row["rationale"]
    assert abs(row["entry"] - row["stop_loss"]) / row["entry"] < 0.03


@pytest.mark.asyncio
async def test_a_disarmed_day_alerts_but_does_not_trade(engine):
    """The criteria can be met on a day nobody armed. Then it is an alert."""
    _votes(engine, {"candlestick": (0.7, 0.9), "derivatives": (0.6, 0.8)})
    [result] = await engine.run_cycle([SYMBOL])
    assert result["signal_id"]                     # the idea was good...
    assert not engine.broker._orders               # ...but no order went in


# --------------------------------------------------------------------------- #
# The sell: target, stop, and why
# --------------------------------------------------------------------------- #
async def _open_one(engine) -> dict:
    await engine.trading_day.start()
    _votes(engine, {"candlestick": (0.7, 0.9), "derivatives": (0.6, 0.8)})
    [result] = await engine.run_cycle([SYMBOL])
    assert result["signal_id"], result["rejected"]
    return next(r for r in db.open_signals() if r["id"] == result["signal_id"])


@pytest.mark.asyncio
async def test_hitting_the_target_sells_for_a_profit_and_says_so(engine, monkeypatch):
    row = await _open_one(engine)
    _price(engine, monkeypatch, row["target"] + 1.0)

    closed = await engine.outcomes.poll()
    [trade] = [c for c in closed if c["signal_id"] == row["id"]]
    assert trade["status"] == SignalStatus.CLOSED_TARGET.value
    assert trade["pnl"] > 0
    assert trade["r_multiple"] == pytest.approx(row["risk_reward"], abs=0.05)
    assert trade["exit_price"] == pytest.approx(row["target"])
    assert not [r for r in db.open_signals() if r["id"] == row["id"]]


@pytest.mark.asyncio
async def test_hitting_the_stop_sells_for_one_r_and_says_so(engine, monkeypatch):
    row = await _open_one(engine)
    _price(engine, monkeypatch, row["stop_loss"] - 1.0)

    closed = await engine.outcomes.poll()
    [trade] = [c for c in closed if c["signal_id"] == row["id"]]
    assert trade["status"] == SignalStatus.CLOSED_STOP.value
    assert trade["pnl"] < 0
    # A stop is a planned loss of 1R — not more, because the exit is AT the stop.
    assert trade["r_multiple"] == pytest.approx(-1.0, abs=0.01)


@pytest.mark.asyncio
async def test_between_stop_and_target_it_holds(engine, monkeypatch):
    row = await _open_one(engine)
    _price(engine, monkeypatch, (row["entry"] + row["target"]) / 2)
    closed = await engine.outcomes.poll()
    assert not [c for c in closed if c["signal_id"] == row["id"]]
    assert [r for r in db.open_signals() if r["id"] == row["id"]]


@pytest.mark.asyncio
async def test_every_closed_trade_is_graded_in_the_journal(engine, monkeypatch):
    """A P&L number alone teaches nothing; the graded card is the learning."""
    from app.journal import store

    row = await _open_one(engine)
    _price(engine, monkeypatch, row["target"] + 1.0)
    await engine.outcomes.poll()

    entries = store.entries(limit=50)
    mine = [e for e in entries if e["id"] == f"LIVE-{row['id']}"]
    assert mine, "the closed trade must be written to the journal"
    assert mine[0]["verdict"]                         # GOOD_WIN, GOOD_LOSS, ...


@pytest.mark.asyncio
async def test_the_day_report_explains_the_exit(engine, monkeypatch):
    """"Why did it sell" has to be answerable from the page, not the database."""
    row = await _open_one(engine)
    _price(engine, monkeypatch, row["target"] + 1.0)
    await engine.outcomes.poll()

    report = engine.trading_day.report()
    [trade] = [t for t in report["trades"] if t["id"] == row["id"]]
    assert trade["status"] == SignalStatus.CLOSED_TARGET.value
    assert trade["exit_reason"]
    assert "target" in trade["exit_reason"].lower()
    assert trade["why"], "the reason it BOUGHT must be in the report too"


# --------------------------------------------------------------------------- #
# The why reaches every place a person reads
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_the_buy_and_sell_events_carry_the_reasons(engine, monkeypatch):
    """The log line and the desktop notification are built from these events."""
    from app.core.bus import Topic, bus

    seen: list[dict] = []
    publish = bus.publish

    async def _record(topic, payload):
        if topic == Topic.POSITION_UPDATE and isinstance(payload, dict):
            seen.append(payload)
        await publish(topic, payload)

    monkeypatch.setattr(bus, "publish", _record)
    row = await _open_one(engine)
    [opened] = [e for e in seen if e.get("event") == "opened"]
    assert opened["why"]["headline"] == "2 analysts agreed on a long"
    assert "stop" in opened["why"]["plan"]

    _price(engine, monkeypatch, row["stop_loss"] - 1.0)
    await engine.outcomes.poll()
    [closed] = [e for e in seen if e.get("event") == "closed"]
    assert closed["exit_reason"].startswith("Stop ")
    assert "-1.00R" in closed["exit_reason"]


@pytest.mark.asyncio
async def test_the_open_positions_api_says_why_and_how_it_exits(engine):
    from types import SimpleNamespace

    from app.api.routes import positions

    row = await _open_one(engine)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(engine=engine)))
    body = await positions(request)
    [mine] = [p for p in body["open_signals"] if p["id"] == row["id"]]
    assert mine["why"]["confirmations"]
    assert "structural stop" in mine["why"]["stop_basis"]
    assert mine["exit_reason"].startswith("Still held")


@pytest.mark.asyncio
async def test_a_scan_on_an_armed_day_places_nothing(engine):
    """A scan is a question. It ran the full cycle, so on an armed paper day
    it bought every tradeable setup it was asked about."""
    await engine.trading_day.start()
    _votes(engine, {"candlestick": (0.7, 0.9), "derivatives": (0.6, 0.8)})
    before = len(db.open_signals())

    scan = await engine.scanner.scan([SYMBOL])

    assert scan["actionable"] == 1                 # it saw the trade...
    assert not engine.broker._orders               # ...and did not take it
    assert len(db.open_signals()) == before


@pytest.mark.asyncio
async def test_a_day_of_no_trades_still_says_why(engine):
    """The commonest "no" is the CMIO's vote, and it never becomes a signal —
    so a quiet day's review used to show nothing at all, which read as a desk
    that was not running."""
    await engine.trading_day.start()
    _votes(engine, {"candlestick": (0.6, 0.9)})          # one voice only
    await engine.run_cycle([SYMBOL])

    report = engine.trading_day.report()
    assert report["symbols_judged"] >= 1
    assert report["trades_taken"] == 0 or not engine.broker._orders
    reasons = {r["reason"] for r in report["top_rejections"]}
    assert "Not enough confirmations" in reasons
