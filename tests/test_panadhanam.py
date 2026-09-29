"""The $4,000 account's guardrails, the absolute news veto, and the Friday
Ollama feedback — the three mandatory checks first:

  1. Anti-stacking: a symbol that closed a trade is blocked for 60 minutes
     and free again after 61.
  2. News polarity beyond ±0.60 against the trade is an absolute veto.
  3. Option premium per trade flexes from 20% ($800) to 25% ($1,000) on
     SPY and QQQ.

then the rest of what came with them: the 10% circuit breaker, the 7% spread
limit, stops on the underlying, and the Ollama parser and weights file.
"""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta

import pytest

from app.agents import consensus
from app.agents import risk_manager as guard
from app.agents.cmio import CMIOAgent
from app.agents.risk import RiskManager
from app.core.models import (
    AgentReport,
    Bias,
    Candle,
    MarketContext,
    OptionChain,
    Quote,
    SignalStatus,
)

T0 = datetime(2026, 9, 28, 10, 0)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def us(cfg):
    """The US desk on the shipped $4,000 account, entries allowed all day."""
    cfg.switch_market("US")
    cfg.settings["system"]["no_new_entry_after"] = "23:59"
    yield cfg
    cfg.switch_market("IN")


@pytest.fixture
def rm(us, monkeypatch):
    from app.storage import db

    monkeypatch.setattr(db, "open_signals", lambda: [])
    monkeypatch.setattr(db, "last_exit", lambda s: None)
    manager = RiskManager(us)
    manager.set_capital(4_000)
    return manager


def _report(agent: str, score: float, **extra) -> AgentReport:
    bias = Bias.BULLISH if score > 0 else Bias.BEARISH if score < 0 else Bias.NEUTRAL
    return AgentReport(agent_id=agent, symbol="SPY", bias=bias, score=score,
                       confidence=0.8, extra=extra)


def _option_ctx(symbol: str, premium: float, *, spot: float = 500.0, atr: float = 0.6,
                bid: float | None = None, ask: float | None = None,
                option_type: str = "CE", candles: list[Candle] | None = None,
                delta: float = 0.5):
    """A context whose derivatives analyst picked one option leg."""
    expiry = (date.today() + timedelta(days=5)).isoformat()
    half = premium * 0.01
    leg = {"strike": spot, "option_type": option_type, "ltp": premium,
           "bid": bid if bid is not None else round(premium - half, 2),
           "ask": ask if ask is not None else round(premium + half, 2),
           "delta": delta if option_type == "CE" else -delta, "expiry": expiry}
    ctx = MarketContext(
        symbol=symbol, cycle_id="t", quote=Quote(symbol=symbol, last_price=spot),
        indicators={"primary": {"atr": atr}},
        option_chain=OptionChain(underlying=symbol, spot=spot, expiry=expiry),
        candles={"5m": candles or []})
    deriv = _report("derivatives", 0.5 if option_type == "CE" else -0.5,
                    suggested_leg=leg)
    ctx.__dict__["_reports"] = [deriv]
    return ctx, [deriv]


def _evaluate(rm, ctx, reports, bias=Bias.BULLISH):
    return rm.evaluate(ctx, bias, reports, 0.6 if bias == Bias.BULLISH else -0.6,
                       ["derivatives: bullish"])


# =========================================================================== #
# 1. Anti-stacking cooldown: 60 minutes
# =========================================================================== #
def test_cooldown_blocks_within_60_minutes_and_allows_after_61():
    blacklist = guard.CooldownBlacklist(minutes=60)
    blacklist.add("XOM", T0)
    for minutes in (0, 1, 30, 59, 59.9):
        assert blacklist.blocked("XOM", T0 + timedelta(minutes=minutes)), minutes
    assert not blacklist.blocked("XOM", T0 + timedelta(minutes=61))
    assert not blacklist.blocked("AVGO", T0)              # others untouched
    assert "cooldown blacklist" in blacklist.reason("XOM", T0 + timedelta(minutes=10))
    assert blacklist.reason("XOM", T0 + timedelta(minutes=61)) == ""


def test_the_risk_manager_refuses_a_re_entry_inside_the_hour(rm, monkeypatch):
    """The XOM/AVGO pattern: closed, then bought straight back."""
    from app.storage import db

    monkeypatch.setattr(db, "last_exit",
                        lambda s: (datetime.now() - timedelta(minutes=20)).isoformat()
                        if s == "SPY" else None)
    ctx, reports = _option_ctx("SPY", 9.50)
    signal = _evaluate(rm, ctx, reports)
    assert signal.status == SignalStatus.REJECTED
    assert any("cooldown blacklist" in r for r in signal.rejection_reasons)

    monkeypatch.setattr(db, "last_exit",
                        lambda s: (datetime.now() - timedelta(minutes=61)).isoformat())
    assert rm.symbol_checks("SPY") == []


def test_a_close_blacklists_the_symbol_at_once(rm):
    """No database round-trip needed: register_close starts the clock."""
    ctx, reports = _option_ctx("SPY", 9.50)
    signal = _evaluate(rm, ctx, reports)
    rm.register_open(signal)
    rm.register_close(signal, -20.0)
    assert any("cooldown blacklist" in r for r in rm.symbol_checks("SPY"))
    assert "SPY" in rm.snapshot()["cooldown"]


def test_the_blacklist_survives_a_restart_through_the_database():
    closed = (T0 - timedelta(minutes=15)).isoformat()
    fresh = guard.CooldownBlacklist(60, loader=lambda s: closed if s == "AVGO" else None)
    assert fresh.blocked("AVGO", T0)
    assert fresh.minutes_left("AVGO", T0) == pytest.approx(45.0)
    assert not fresh.blocked("AVGO", T0 + timedelta(minutes=46))


def test_the_shipped_cooldown_is_60_minutes(cfg):
    assert cfg.get("risk.reentry_cooldown_minutes") == 60


# =========================================================================== #
# 2. News veto: polarity beyond ±0.60 against the trade
# =========================================================================== #
@pytest.mark.parametrize("polarity, direction, vetoed", [
    (-0.65, +1, True),       # bearish news under a BUY
    (+0.65, -1, True),       # bullish news under a SELL
    (-0.90, +1, True),
    (-0.60, +1, False),      # exactly the threshold: not beyond it
    (-0.55, +1, False),
    (-0.65, -1, False),      # bearish news under a SELL agrees
    (+0.65, +1, False),
])
def test_news_polarity_beyond_060_against_the_trade_vetoes(cfg, polarity, direction,
                                                           vetoed):
    reason = consensus.news_veto([_report("news_sentiment", polarity)], direction, cfg)
    assert bool(reason) is vetoed, reason
    if vetoed:
        assert "News veto" in reason and f"{polarity:+.2f}" in reason


def test_the_news_veto_overrides_a_strong_chart(cfg):
    """Chart and options both say BUY, strongly; news at -0.65 still kills it."""
    reports = [_report("candlestick", 0.85), _report("derivatives", 0.70),
               _report("macro_flow", 0.30), _report("news_sentiment", -0.65)]
    decision = CMIOAgent(cfg)._weighted_vote(
        MarketContext(symbol="SPY", cycle_id="t"), reports)
    assert decision["proceed"] is False
    assert any("News veto" in c for c in decision["conflicts"])
    assert "News veto" in decision["rationale"]

    reports[-1] = _report("news_sentiment", -0.40)          # mild: no veto
    decision = CMIOAgent(cfg)._weighted_vote(
        MarketContext(symbol="SPY", cycle_id="t"), reports)
    assert decision["proceed"] is True


def test_the_news_veto_also_kills_a_short(cfg):
    reports = [_report("candlestick", -0.80), _report("derivatives", -0.60),
               _report("news_sentiment", +0.70)]
    decision = CMIOAgent(cfg)._weighted_vote(
        MarketContext(symbol="SPY", cycle_id="t"), reports)
    assert decision["proceed"] is False


def test_a_model_synthesis_cannot_undo_the_news_veto(cfg):
    reports = [_report("candlestick", 0.85), _report("news_sentiment", -0.70)]
    llm_said_go = {"bias": Bias.BULLISH, "composite_score": 0.7, "proceed": True,
                   "confirmations": ["a", "b"], "conflicts": [], "rationale": "go"}
    out = CMIOAgent(cfg)._enforce_news_veto(llm_said_go, reports)
    assert out["proceed"] is False and any("News veto" in c for c in out["conflicts"])


# =========================================================================== #
# 3. Index capital flex: 20% ($800) -> 25% ($1,000) on SPY / QQQ
# =========================================================================== #
@pytest.mark.parametrize("symbol, cap", [
    ("SPY", 1000.0), ("QQQ", 1000.0), ("DIA", 1000.0),
    ("AAPL", 800.0), ("XOM", 800.0), ("AVGO", 800.0)])
def test_deployment_cap_flexes_to_25pct_for_index_etfs(us, symbol, cap):
    assert guard.deployment_cap(us, 4000, symbol) == cap


def test_india_flexes_on_its_own_indices(cfg):
    cfg.switch_market("IN")
    assert guard.deployment_cap(cfg, 350000, "NIFTY 50") == 87500.0     # 25%
    assert guard.deployment_cap(cfg, 350000, "RELIANCE") == 70000.0     # 20%
    assert guard.deployment_cap(cfg, 350000, "SPY") == 70000.0


def test_dji_is_read_as_dia(cfg):
    cfg.settings["risk"]["index_symbols"] = ["SPY", "DJI"]
    try:
        assert guard.deployment_cap(cfg, 4000, "DIA") == 1000.0
    finally:
        cfg.settings["risk"]["index_symbols"] = ["SPY", "QQQ", "DIA"]


@pytest.mark.parametrize("symbol", ["SPY", "QQQ"])
def test_a_950_index_contract_is_approved_under_the_25pct_cap(rm, symbol):
    ctx, reports = _option_ctx(symbol, 9.50)
    signal = _evaluate(rm, ctx, reports)
    assert signal.status == SignalStatus.APPROVED, signal.rejection_reasons
    assert signal.quantity == 100 and signal.unit_label == "contract"
    assert signal.notional == pytest.approx(950.0)


def test_the_same_950_contract_on_a_stock_hits_the_20pct_cap(rm):
    ctx, reports = _option_ctx("AAPL", 9.50)
    signal = _evaluate(rm, ctx, reports)
    assert signal.status == SignalStatus.REJECTED
    assert any("20% capital deployment cap" in r and "$800" in r
               for r in signal.rejection_reasons), signal.rejection_reasons


def test_without_the_flex_spy_would_have_been_refused(rm, us):
    us.settings["risk"]["index_max_capital_deployed_pct"] = 20.0
    try:
        signal = _evaluate(rm, *_option_ctx("SPY", 9.50))
        assert signal.status == SignalStatus.REJECTED
    finally:
        us.settings["risk"]["index_max_capital_deployed_pct"] = 25.0


# =========================================================================== #
# The circuit breaker, the spread limit, and stops on the underlying
# =========================================================================== #
def test_the_daily_circuit_breaker_is_120_on_4000(rm):
    assert rm.state.daily_loss_limit == 120.0                # 3%
    rm.state.realised_pnl = -80.0
    rm.set_unrealised(-39.0)
    assert not any("Daily loss" in r for r in rm.desk_checks())
    rm.set_unrealised(-40.0)
    assert any("Daily loss limit" in r for r in rm.desk_checks())
    assert rm.state.halted


@pytest.mark.parametrize("bid, ask, ok", [
    (9.20, 9.80, True),      # 6.3%
    (9.17, 9.83, True),      # ~7.0%
    (9.10, 9.90, False),     # 8.4%
    (8.00, 11.00, False)])
def test_spread_over_7pct_of_mid_is_refused(rm, bid, ask, ok):
    ctx, reports = _option_ctx("SPY", 9.50, bid=bid, ask=ask)
    signal = _evaluate(rm, ctx, reports)
    wide = [r for r in signal.rejection_reasons if "spread" in r]
    assert (not wide) is ok, signal.rejection_reasons


def test_an_unquoted_leg_passes_unless_asked_to_refuse(cfg):
    assert guard.spread_rejection(cfg, 0, 0) == ""
    cfg.settings["risk"]["reject_unquoted_spread"] = True
    try:
        assert "no two-sided quote" in guard.spread_rejection(cfg, 0, 0)
    finally:
        cfg.settings["risk"]["reject_unquoted_spread"] = False


def _bars(lows: list[float]) -> list[Candle]:
    return [Candle(ts=T0 + timedelta(minutes=5 * i), open=low + 0.3, high=low + 0.6,
                   low=low, close=low + 0.4, volume=1000) for i, low in enumerate(lows)]


def test_the_option_stop_is_the_5m_swing_low_plus_2_ticks(rm):
    """499.00 is the lowest low of the last completed 5m bars; the stop sits
    two ticks beneath it on SPY (tick 0.01), not on the option's premium."""
    bars = _bars([499.8, 499.4, 499.0, 499.3, 499.6, 499.7, 499.9])
    ctx, reports = _option_ctx("SPY", 9.50, candles=bars, atr=0.5)
    signal = _evaluate(rm, ctx, reports)
    assert signal.underlying_stop == pytest.approx(498.98)
    assert "5m swing low" in signal.underlying_stop_note
    # The premium stop is just where the option marks when SPY gets there.
    assert signal.stop_loss == pytest.approx(9.50 - (500 - 498.98) * 0.5, abs=0.01)


def test_without_a_usable_swing_the_stop_is_15x_atr(rm):
    ctx, reports = _option_ctx("SPY", 9.50, atr=0.6)
    signal = _evaluate(rm, ctx, reports)
    assert signal.underlying_stop == pytest.approx(500 - 1.5 * 0.6)
    assert "ATR" in signal.underlying_stop_note


def test_a_put_stop_sits_above_the_5m_swing_high(rm):
    bars = _bars([500.1, 500.3, 500.2, 500.0, 499.9, 499.8, 499.7])   # highs +0.6
    ctx, reports = _option_ctx("SPY", 9.50, candles=bars, atr=0.5, option_type="PE")
    signal = _evaluate(rm, ctx, reports, bias=Bias.BEARISH)
    assert signal.underlying_stop == pytest.approx(500.3 + 0.6 + 0.02)


@pytest.mark.asyncio
async def test_the_exit_follows_the_underlying_not_premium_noise(rm):
    """A premium dip with the stock above its level holds; the stock closing
    through the level exits, whatever the premium says."""
    from app.learning.outcomes import OutcomeTracker

    ctx, reports = _option_ctx("SPY", 9.50, atr=0.6)
    signal = _evaluate(rm, ctx, reports)
    row = {"id": signal.id, "symbol": "SPY", "instrument_type": "CE", "side": "BUY",
           "entry": signal.entry, "stop_loss": signal.stop_loss, "target": signal.target,
           "quantity": signal.quantity, "entry_spot": 500.0, "entry_delta": 0.5,
           "payload": signal.model_dump_json(), "ts": datetime.now().isoformat()}
    tracker = OutcomeTracker(broker=None, cfg=rm.cfg)
    ustop = tracker._underlying_stop(row)
    assert ustop == pytest.approx(499.1)
    assert not guard.underlying_stop_hit(499.5, ustop, True)
    assert guard.underlying_stop_hit(499.05, ustop, True)
    assert not guard.underlying_stop_hit(499.5, 500.9, False)          # a put
    assert guard.underlying_stop_hit(501.0, 500.9, False)


# =========================================================================== #
# The Friday Ollama feedback
# =========================================================================== #
from scripts import ollama_feedback as fb  # noqa: E402

CURRENT = {a: 1.0 for a in fb.AGENTS}


@pytest.mark.parametrize("answer", [
    '{"adjustments": {"candlestick": 0.1, "news_sentiment": -0.05}, "rationale": "r"}',
    'Here you go:\n```json\n{"adjustments": {"candlestick": 0.1, '
    '"news_sentiment": -0.05}}\n```',
    '{"adjustments": {"candlestick": 0.1, "news_sentiment": -0.05,},}',
    '{"adjustments": {"CANDLESTICK": "0.1", "news_sentiment": -0.05}}',
])
def test_the_parser_reads_every_reasonable_shape(answer):
    out = fb.parse_response(answer, CURRENT)
    assert out.ok, out.errors
    assert out.weights["candlestick"] == pytest.approx(1.1)
    assert out.weights["news_sentiment"] == pytest.approx(0.95)


@pytest.mark.parametrize("answer, error", [
    (None, "empty"), ("", "empty"),
    ("Candlestick did great, raise it!", "no JSON object"),
    ('{"adjustments": {"candlestick": 0.1', "no JSON object"),
    ('{"adjustments": {"candlestick": 0.1 "x": 1}}', "malformed JSON"),
    ('{"rationale": "fine"}', 'no "adjustments"'),
    ('{"adjustments": {"astrology": 0.5}}', "no usable adjustment"),
    ('{"adjustments": {"candlestick": NaN}}', "no usable adjustment"),
    ('{"adjustments": {"candlestick": true}}', "no usable adjustment"),
])
def test_malformed_answers_change_nothing(answer, error):
    out = fb.parse_response(answer, CURRENT)
    assert not out.ok and out.weights == {}
    assert any(error in e for e in out.errors), out.errors


def test_every_step_and_every_weight_is_bounded():
    out = fb.parse_response('{"adjustments": {"candlestick": 9, "macro_flow": -9}}',
                            CURRENT, max_step=0.15)
    assert out.weights["candlestick"] == pytest.approx(1.15)
    assert out.weights["macro_flow"] == pytest.approx(0.85)


def _journal_week(n: int) -> None:
    from app.journal import store
    from app.journal.models import JournalEntry, SetupType, TradeVerdict

    store.init_journal()
    for i in range(n):
        store.save_entry(JournalEntry(
            id=f"FB-{i}", ts=datetime(2026, 9, 21 + i % 5, 11, 0),
            market="US", symbol="SPY", instrument="SPY",
            setup=SetupType.BREAKOUT, side="BUY", planned_entry=10, planned_stop=9,
            planned_target=12, planned_quantity=100, actual_entry=10, actual_exit=11,
            actual_quantity=100, entry_ts=datetime(2026, 9, 21 + i % 5, 10, 0),
            exit_ts=datetime(2026, 9, 21 + i % 5, 11, 0),
            verdict=TradeVerdict.GOOD_WIN, execution_score=9))


@pytest.fixture
def clean_feedback():
    from app.journal import store

    for table in ("journal_entries",):
        try:
            store.get_conn().execute(f"DELETE FROM {table}")
            store.get_conn().commit()
        except Exception:
            pass
    consensus.STRATEGY_WEIGHTS_PATH.unlink(missing_ok=True)
    yield
    consensus.STRATEGY_WEIGHTS_PATH.unlink(missing_ok=True)


def test_the_friday_run_writes_strategy_weights_json(cfg, clean_feedback):
    _journal_week(6)
    done = asyncio.run(fb.run(
        cfg, date(2026, 9, 21), date(2026, 9, 25),
        answer='{"adjustments": {"candlestick": 0.1}, "rationale": "6 clean wins"}'))
    assert done.applied and done.trades == 6
    saved = json.loads(consensus.STRATEGY_WEIGHTS_PATH.read_text(encoding="utf-8"))
    assert saved["strategy_weights"]["candlestick"] == pytest.approx(1.1)
    # ...and the CMIO now votes with it.
    base = float(cfg.get("weights.candlestick", 1.0))
    assert consensus.effective_weights(cfg)["candlestick"] == pytest.approx(base * 1.1)
    assert json.loads(open(done.record, encoding="utf-8").read())["applied"] is True


def test_a_malformed_friday_answer_writes_nothing(cfg, clean_feedback):
    _journal_week(6)
    done = asyncio.run(fb.run(cfg, date(2026, 9, 21), date(2026, 9, 25),
                              answer="raise candlestick please"))
    assert not done.applied and "unusable" in done.note
    assert not consensus.STRATEGY_WEIGHTS_PATH.exists()


def test_too_few_trades_are_not_learned_from(cfg, clean_feedback):
    _journal_week(2)
    done = asyncio.run(fb.run(cfg, date(2026, 9, 21), date(2026, 9, 25),
                              answer='{"adjustments": {"candlestick": 0.1}}'))
    assert not done.applied and "too few" in done.note


def test_a_corrupt_weights_file_is_ignored(cfg, clean_feedback):
    consensus.STRATEGY_WEIGHTS_PATH.write_text("{not json", encoding="utf-8")
    assert consensus.effective_weights(cfg) == dict(cfg.get("weights"))
    consensus.STRATEGY_WEIGHTS_PATH.write_text(
        '{"strategy_weights": {"candlestick": 99, "risk": 5}}', encoding="utf-8")
    assert consensus.load_multipliers() == {"candlestick": 1.5}
