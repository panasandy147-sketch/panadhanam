"""The alpha/risk split, the four-agent committee and the Friday reflection.

The five mandatory checks, in order:

  1. Alpha independence      strategies return valid signal dicts with no
                             capital input and no broker or network access
  2. Dynamic index budgeting a $950 SPY contract passes the 25% cap that a
                             strict 20% ($800) cap refuses
  3. Spread rejection        > 7% of mid is refused by the gatekeeper
  4. Structural stops        the exit follows the UNDERLYING level; the 45%
                             premium stop remains only as a disaster backstop
  5. Ollama JSON parser      model answers become bounded weight updates, and
                             malformed ones change nothing

followed by the committee itself and its wiring into the desk.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest

from panaoptions import alpha
from panaoptions.models import (
    Direction,
    ExitReason,
    Indicators,
    OptionContract,
    OptionRight,
    Setup,
    SetupType,
)
from panaoptions.risk.gatekeeper import RiskGatekeeper
from panaoptions.risk.guardrails import RiskManager
from tests.test_app import FakeFeed

SESSION = date(2026, 9, 23)
NOW = datetime(2026, 9, 23, 10, 20, tzinfo=UTC)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def account(cfg):
    """The shipped $4,000 account with the shipped risk rules."""
    cfg.data["account"]["starting_capital"] = 4000.0
    cfg.data["risk"]["max_capital_deployed_pct"] = 20.0
    cfg.data["risk"]["index_max_capital_deployed_pct"] = 25.0
    cfg.data["risk"]["index_symbols"] = ["SPY", "QQQ", "DIA"]
    cfg.data["risk"]["max_spread_pct_of_mid"] = 7.0
    cfg.data["risk"]["max_total_deployed_pct"] = 45.0
    cfg.data["risk"]["daily_loss_limit_pct"] = 10.0
    cfg.data["risk"]["daily_loss_limit"] = None
    cfg.data["contracts"]["budget_fallback_min_delta"] = 0.30
    # The same-day profile's contract window.
    cfg.data["contracts"].update(min_dte=0, max_dte=4, min_delta=0.35,
                                 max_delta=0.50, max_spread_pct_of_mid=7.0)
    return cfg


def _contract(symbol="SPY", mid=9.50, delta=0.45, spread_pct=2.0,
              right=OptionRight.CALL, dte=0, strike=500.0) -> OptionContract:
    half = mid * spread_pct / 200.0
    return OptionContract(symbol=symbol, right=right, strike=strike,
                          expiry="2026-09-23", dte=dte, bid=round(mid - half, 4),
                          ask=round(mid + half, 4), delta=delta,
                          implied_volatility=0.18)


def _signal(symbol="SPY", direction="LONG", trigger=500.0, stop=498.0,
            score=0.7) -> dict:
    return {"symbol": symbol, "direction": direction, "trigger_price": trigger,
            "invalidation_level": stop, "confidence_score": score}


def _setup(symbol="SPY", support=498.0, close=500.0,
           band=(0.35, 0.50)) -> Setup:
    return Setup(symbol=symbol, ts=NOW, direction=Direction.LONG,
                 strategy=SetupType.ORB_VWAP, pattern="Opening range breakout",
                 confirmations=["close above the range", "above VWAP",
                                "9 EMA > 21 EMA"],
                 indicators=Indicators(close=close, vwap=close - 1, rvol=2.0,
                                       atr=1.0),
                 underlying_support=support, invalidation_note="back inside",
                 trend_aligned=True, delta_band=band)


# =========================================================================== #
# 1. Alpha independence
# =========================================================================== #
class _NoMoneyConfig:
    """A config that fails the test if anything asks it about money.

    Strategy windows, thresholds and indicator settings are allowed; the
    account, the risk rules, the contract filters and the multiplier are not.
    """

    FORBIDDEN = ("account", "risk", "contracts", "agents")

    def __init__(self, real) -> None:
        self._real = real

    def get(self, path: str, default=None):
        if path.split(".")[0] in self.FORBIDDEN:
            raise AssertionError(f"the alpha engine read {path!r}")
        return self._real.get(path, default)

    @property
    def capital(self):
        raise AssertionError("the alpha engine read the account balance")

    @property
    def multiplier(self):
        raise AssertionError("the alpha engine read the contract multiplier")

    @property
    def timezone(self):
        return self._real.timezone


@pytest.fixture
def offline(monkeypatch):
    """Any network client at all is a failure."""
    import httpx

    def refuse(*_a, **_k):
        raise AssertionError("the alpha engine opened a network connection")

    monkeypatch.setattr(httpx, "AsyncClient", refuse)
    monkeypatch.setattr(httpx, "Client", refuse)


def _fake_tape():
    from panaoptions.engine import levels as levels_mod

    candles = _run(FakeFeed().candles("SPY"))
    return candles, levels_mod.compute(candles, "America/New_York", SESSION)


def test_alpha_returns_a_pure_signal_without_capital_or_broker(cfg, offline):
    candles, levels = _fake_tape()
    signal, setup, attempts = alpha.evaluate("SPY", candles, levels,
                                             _NoMoneyConfig(cfg))
    assert setup is not None and attempts
    assert signal is not None
    out = signal.to_dict()
    assert tuple(out) == alpha.SIGNAL_KEYS
    assert out["symbol"] == "SPY" and out["direction"] == "LONG"
    assert out["invalidation_level"] < out["trigger_price"]
    assert 0.0 < out["confidence_score"] <= 1.0
    assert alpha.validate(out) == []


def _frame(rows) -> pd.DataFrame:
    base = datetime(2026, 9, 23, 14, 0, tzinfo=UTC)
    return pd.DataFrame(
        [{"open": o, "high": h, "low": lo, "close": c, "volume": 1000.0}
         for o, h, lo, c in rows],
        index=[base + timedelta(minutes=5 * i) for i in range(len(rows))])


def test_every_pattern_evaluator_yields_valid_signal_dicts(offline):
    """The raw candlestick evaluators, straight from a frame — no config at all."""
    downtrend = [(101 - i * 0.5, 101.2 - i * 0.5, 100.4 - i * 0.5, 100.5 - i * 0.5)
                 for i in range(10)]
    hammer = downtrend + [(96.0, 96.1, 94.0, 96.05)]
    engulf = downtrend + [(96.2, 96.3, 95.6, 95.7), (95.6, 96.9, 95.5, 96.8)]
    found = []
    for rows in (hammer, engulf):
        for sig in alpha.pattern_signals("AAPL", _frame(rows)):
            assert tuple(sig) == alpha.SIGNAL_KEYS
            assert alpha.validate(sig) == []
            found.append(sig["direction"])
    assert "LONG" in found


def test_signal_validation_catches_a_stop_on_the_wrong_side():
    assert alpha.validate(_signal(trigger=100, stop=101))        # long, stop above
    assert alpha.validate(_signal(direction="SHORT", trigger=100, stop=99))
    assert alpha.validate({**_signal(), "budget": 800})          # money leaked in
    assert alpha.validate({"symbol": "SPY"})                     # missing keys
    assert alpha.validate(_signal(score=1.4))
    assert alpha.validate(_signal()) == []


def test_strategies_hold_no_contract_or_account_tables():
    """Per-pattern contract bands moved to engine/contract_prefs.py."""
    import inspect

    from panaoptions import alpha as alpha_mod
    from panaoptions.engine import patterns, strategies

    for module in (strategies, patterns, alpha_mod):
        source = inspect.getsource(module)
        for word in ("starting_capital", "budget_room", "max_capital_deployed",
                     "RiskManager", "PaperLedger", "chain_for_window"):
            assert word not in source, f"{module.__name__} mentions {word}"


# =========================================================================== #
# 2. Dynamic index budgeting
# =========================================================================== #
def test_spy_gets_the_25pct_cap_and_a_950_contract_passes(account):
    gate = RiskGatekeeper(account)
    contract = _contract("SPY", mid=9.50)                 # $950
    decision = gate.review(_signal("SPY"), contract, (0.35, 0.50))
    assert decision.approved, decision.rejected
    assert decision.cap == 1000.0 and decision.cap_pct == 25.0


def test_the_same_950_contract_fails_a_strict_20pct_cap(account):
    account.data["risk"]["index_max_capital_deployed_pct"] = 20.0
    gate = RiskGatekeeper(account)
    decision = gate.review(_signal("SPY"), _contract("SPY", mid=9.50), (0.35, 0.50))
    assert not decision.approved
    assert decision.cap == 800.0
    assert any("over the 20% cap of $800.00" in r for r in decision.rejected)


def test_a_single_stock_keeps_the_20pct_cap(account):
    gate = RiskGatekeeper(account)
    decision = gate.review(_signal("AAPL"), _contract("AAPL", mid=9.50), (0.35, 0.50))
    assert not decision.approved and decision.cap == 800.0
    assert gate.cap("QQQ") == 1000.0 and gate.cap("DIA") == 1000.0


def test_dji_is_read_as_dia(account):
    account.data["risk"]["index_symbols"] = ["SPY", "DJI"]
    assert RiskGatekeeper(account).cap("DIA") == 1000.0


def test_the_risk_manager_sizes_spy_under_the_index_cap(account):
    risk = RiskManager(account)
    signal, refusal = risk.size(_setup("SPY"), _contract("SPY", mid=9.50),
                                "SIG-1", NOW)
    assert signal is not None, refusal
    assert signal.quantity == 1 and signal.cost() == 950.0
    assert risk.budget_room("SPY") == 1000.0 and risk.budget_room("AAPL") == 800.0

    signal, refusal = risk.size(_setup("AAPL"), _contract("AAPL", mid=9.50),
                                "SIG-2", NOW)
    assert signal is None and "$800.00" in refusal


def test_index_delta_fallback_reaches_down_to_030(account):
    """A 0.45 delta SPY call over the $1,000 cap falls back to the best delta
    that fits, never below 0.30 — and the gatekeeper accepts that delta."""
    from panaoptions.engine import contracts

    chain = [_contract("SPY", mid=12.00, delta=0.45),      # $1,200 — over the cap
             _contract("SPY", mid=6.00, delta=0.32, strike=505),
             _contract("SPY", mid=2.00, delta=0.22, strike=512)]
    for c in chain:
        c.open_interest = 800                              # liquid
    # This test is about the delta tier: no debit-spread rung before it.
    account.data["contracts"].setdefault("debit_spread", {})["enabled"] = False
    gate = RiskGatekeeper(account)
    search = contracts.choose("SPY", chain, Direction.LONG, account,
                              setup=_setup("SPY", band=(0.40, 0.50)),
                              budget=gate.budget_for("SPY"))
    assert search.budget_fallback and search.chosen is not None
    assert abs(search.chosen.delta) == 0.32
    assert gate.review(_signal("SPY"), search.chosen, (0.40, 0.50)).approved
    lottery = gate.review(_signal("SPY"), chain[2], (0.40, 0.50))
    assert not lottery.approved
    assert any("delta 0.22" in r for r in lottery.rejected)


# =========================================================================== #
# 3. Spread rejection
# =========================================================================== #
@pytest.mark.parametrize("spread_pct, approved", [
    (2.0, True), (6.9, True), (7.0, True), (7.1, False), (12.0, False), (30.0, False)])
def test_spread_over_7pct_of_mid_is_rejected(account, spread_pct, approved):
    contract = _contract("SPY", mid=5.00, spread_pct=spread_pct)
    decision = RiskGatekeeper(account).review(_signal("SPY"), contract, (0.35, 0.50))
    assert decision.approved is approved, decision.rejected
    if not approved:
        assert any("wider than 7%" in r for r in decision.rejected)


def test_a_one_sided_quote_is_rejected(account):
    contract = OptionContract(symbol="SPY", right=OptionRight.CALL, strike=500,
                              expiry="2026-09-23", dte=0, bid=0.0, ask=5.0,
                              delta=0.45)
    decision = RiskGatekeeper(account).review(_signal("SPY"), contract, (0.35, 0.50))
    assert not decision.approved
    assert any("two-sided" in r for r in decision.rejected)


def test_a_put_is_not_bought_for_a_long_signal(account):
    contract = _contract("SPY", right=OptionRight.PUT, delta=-0.45)
    decision = RiskGatekeeper(account).review(_signal("SPY"), contract, (0.35, 0.50))
    assert not decision.approved


# =========================================================================== #
# 4. Structural stop mapping
# =========================================================================== #
def _opened(account, support=498.0):
    from panaoptions.ledger.paper import PaperLedger

    account.data["risk"]["stop_mode"] = "underlying"
    account.data["risk"]["disaster_stop_pct"] = 45.0
    account.data["risk"]["max_hold_minutes"] = None
    account.data["risk"]["slippage_per_contract"] = 0.0
    risk = RiskManager(account)
    signal, refusal = risk.size(_setup("SPY", support=support),
                                _contract("SPY", mid=4.00), "SIG-S", NOW)
    assert signal is not None, refusal
    ledger = PaperLedger(account, risk)
    return ledger, ledger.open(signal, NOW), signal


def test_the_stop_is_the_underlying_level_not_the_premium(account):
    ledger, trade, signal = _opened(account)
    assert trade.underlying_support == 498.0
    # The premium stop is only the 45% disaster backstop: 4.00 -> 2.20.
    assert signal.stop_price == pytest.approx(2.20)

    # Option down 30% on an IV wobble, underlying still above its level: held.
    assert ledger.mark(trade.id, 2.80, 499.50, NOW + timedelta(minutes=5)) == []
    assert trade.is_open

    # Underlying closes through the level with the option only 10% down: out.
    fills = ledger.mark(trade.id, 3.60, 497.90, NOW + timedelta(minutes=10))
    assert fills and not trade.is_open
    assert trade.exit_reason is ExitReason.UNDERLYING_BREAK


def test_the_45pct_disaster_stop_still_catches_a_collapse(account):
    ledger, trade, _ = _opened(account)
    fills = ledger.mark(trade.id, 2.00, 499.80, NOW + timedelta(minutes=5))
    assert fills and trade.exit_reason is ExitReason.STOP


def test_a_put_is_stopped_by_the_underlying_rising_through_its_level(account):
    from panaoptions.ledger.paper import PaperLedger

    account.data["risk"]["stop_mode"] = "underlying"
    risk = RiskManager(account)
    setup = _setup("SPY", support=502.0).model_copy(
        update={"direction": Direction.SHORT})
    signal, refusal = risk.size(setup, _contract("SPY", mid=4.00,
                                                 right=OptionRight.PUT, delta=-0.45),
                                "SIG-P", NOW)
    assert signal is not None, refusal
    ledger = PaperLedger(account, risk)
    trade = ledger.open(signal, NOW)
    assert ledger.mark(trade.id, 3.90, 501.0, NOW + timedelta(minutes=5)) == []
    ledger.mark(trade.id, 3.80, 502.3, NOW + timedelta(minutes=10))
    assert trade.exit_reason is ExitReason.UNDERLYING_BREAK


def test_the_daily_circuit_breaker_is_400_on_4000_and_counts_open_losses(account):
    gate = RiskGatekeeper(account)
    assert gate.risk.daily_limit == 400.0
    gate.risk.record_pnl(-250.0)
    assert not gate.breaker_tripped(unrealised=-100.0)        # $350
    assert gate.breaker_tripped(unrealised=-150.0)            # $400
    assert gate.risk.state.halted
    decision = gate.review(_signal("SPY"), _contract("SPY"), (0.35, 0.50))
    assert not decision.approved
    assert any("circuit breaker" in r for r in decision.rejected)


# =========================================================================== #
# 5. Ollama JSON parser and the reflection loop
# =========================================================================== #
from panaoptions.learning import reflect as reflect_mod  # noqa: E402

CURRENT = {k: 1.0 for k in reflect_mod.STRATEGIES}


@pytest.mark.parametrize("answer", [
    '{"adjustments": {"orb_vwap": 0.1, "liquidity_sweep": -0.05}, "rationale": "r"}',
    'Sure! Here is the JSON:\n```json\n{"adjustments": {"orb_vwap": 0.1, '
    '"liquidity_sweep": -0.05}}\n```\nHope that helps.',
    '{"adjustments": {"orb_vwap": 0.1, "liquidity_sweep": -0.05,},}',
    '{"adjustments": {"ORB_VWAP": "0.1", "liquidity_sweep": -0.05}}',
])
def test_parser_reads_every_reasonable_shape(answer):
    out = reflect_mod.parse_response(answer, CURRENT)
    assert out.ok, out.errors
    assert out.weights["orb_vwap"] == pytest.approx(1.1)
    assert out.weights["liquidity_sweep"] == pytest.approx(0.95)
    assert out.weights["vwap_ema_pullback"] == 1.0


@pytest.mark.parametrize("answer, error", [
    (None, "empty"), ("", "empty"), ("   ", "empty"),
    ("I think ORB is doing well this week.", "no JSON object"),
    ('{"adjustments": {"orb_vwap": 0.1', "no JSON object"),
    ('{"adjustments": {"orb_vwap": 0.1 "x": 2}}', "malformed JSON"),
    ('{"rationale": "fine"}', 'no "adjustments"'),
    ('{"adjustments": {"moon_strategy": 0.5}}', "no usable adjustment"),
    ('{"adjustments": {"orb_vwap": "lots"}}', "no usable adjustment"),
    ('{"adjustments": {"orb_vwap": NaN}}', "no usable adjustment"),
    ('{"adjustments": {"orb_vwap": true}}', "no usable adjustment"),
])
def test_malformed_answers_change_nothing(answer, error):
    out = reflect_mod.parse_response(answer, CURRENT)
    assert not out.ok
    assert out.weights == {}
    assert any(error in e for e in out.errors), out.errors


def test_parser_bounds_every_step_and_every_weight():
    out = reflect_mod.parse_response(
        '{"adjustments": {"orb_vwap": 5, "liquidity_sweep": -9}}', CURRENT, max_step=0.15)
    assert out.weights["orb_vwap"] == pytest.approx(1.15)
    assert out.weights["liquidity_sweep"] == pytest.approx(0.85)
    low = reflect_mod.parse_response('{"adjustments": {"orb_vwap": -0.15}}',
                                     {**CURRENT, "orb_vwap": 0.3})
    assert low.weights["orb_vwap"] == reflect_mod.WEIGHT_MIN


def test_parser_accepts_absolute_weights_but_still_steps_them():
    out = reflect_mod.parse_response('{"strategy_weights": {"orb_vwap": 0.2}}', CURRENT)
    assert out.ok and out.weights["orb_vwap"] == pytest.approx(0.85)


def test_parser_notes_unknown_strategies_but_keeps_the_rest():
    out = reflect_mod.parse_response(
        '{"adjustments": {"orb_vwap": 0.05, "made_up": 0.1}}', CURRENT)
    assert out.ok and out.changes == {"orb_vwap": pytest.approx(0.05)}
    assert any("made_up" in e for e in out.errors)


def _grade_week(n: int, strategy=SetupType.ORB_VWAP, pnl=25.0):
    from panaoptions.journal import store as journal_store
    from panaoptions.journal.models import JournalEntry, Verdict

    for i in range(n):
        journal_store.save_entry(JournalEntry(
            id=f"J{i}", trade_id=f"T{i}",
            ts=datetime(2026, 9, 21 + i % 5, 11, 0), symbol="SPY",
            contract="SPY 2026-09-23 500C", strategy=strategy, pnl=pnl,
            verdict=Verdict.GOOD_WIN, execution_score=9))


def test_reflection_patches_learned_yaml_and_the_config_reads_it(cfg, tmp_path):
    from panaoptions import config as config_mod

    _grade_week(6)
    done = _run(reflect_mod.reflect(
        cfg, date(2026, 9, 21), date(2026, 9, 25),
        answer='```json\n{"adjustments": {"orb_vwap": 0.1}, "rationale": "6 clean wins"}\n```'))
    assert done.applied and done.trades == 6
    assert done.changes == {"orb_vwap": pytest.approx(0.1)}
    assert config_mod.LEARNED_PATH.exists()
    assert cfg.get("strategy_weights.orb_vwap") == pytest.approx(1.1)
    assert config_mod.Config().get("strategy_weights.orb_vwap") == pytest.approx(1.1)

    import json
    record = json.loads(open(done.record, encoding="utf-8").read())
    assert record["applied"] and record["input"]["by_strategy"]["orb_vwap"]["trades"] == 6


def test_a_malformed_reflection_writes_no_weights(cfg):
    from panaoptions import config as config_mod

    _grade_week(6)
    done = _run(reflect_mod.reflect(cfg, date(2026, 9, 21), date(2026, 9, 25),
                                    answer="ORB looks good, raise it a bit!"))
    assert not done.applied and "unusable" in done.note
    assert not config_mod.LEARNED_PATH.exists()


def test_too_few_trades_is_not_learned_from(cfg):
    _grade_week(2)
    done = _run(reflect_mod.reflect(cfg, date(2026, 9, 21), date(2026, 9, 25),
                                    answer='{"adjustments": {"orb_vwap": 0.1}}'))
    assert not done.applied and "too few" in done.note


def test_learned_yaml_only_patches_strategy_weights(cfg, tmp_path):
    """A learned file cannot change a risk limit, however it is edited."""
    from panaoptions import config as config_mod

    config_mod.LEARNED_PATH.write_text(
        "strategy_weights: {orb_vwap: 0.7}\nrisk: {daily_loss_limit_pct: 90}\n",
        encoding="utf-8")
    fresh = config_mod.Config()
    assert fresh.get("strategy_weights.orb_vwap") == 0.7
    assert fresh.get("risk.daily_loss_limit_pct") == 10.0


# =========================================================================== #
# The committee
# =========================================================================== #
from panaoptions.agents import base as agents_base  # noqa: E402
from panaoptions.agents import cmio, derivatives, macro, technical  # noqa: E402


def _alpha_signal(direction="LONG", source="orb_vwap"):
    trigger, stop = (500.0, 498.0) if direction == "LONG" else (500.0, 502.0)
    return alpha.AlphaSignal("SPY", direction, trigger, stop, 0.8, source=source)


def test_technical_vetoes_below_15x_relative_volume(cfg):
    setup = _setup().model_copy(update={"indicators": Indicators(close=500, rvol=1.1)})
    thin = _run(technical.vote(_alpha_signal(), setup, [], cfg, screen_rvol=1.2))
    assert thin.veto and "below 1.5x" in thin.reasons[0]
    busy = _run(technical.vote(_alpha_signal(), setup, [], cfg, screen_rvol=2.3))
    assert not busy.veto


class _NewsFeed:
    def __init__(self, titles, futures=0.0):
        self.titles, self.futures = titles, futures

    async def news(self, symbol, count=10):
        return [{"title": t, "publisher": "x", "published": NOW} for t in self.titles]

    async def futures_change(self, symbol):
        return self.futures


@pytest.fixture(autouse=True)
def _fresh_news_cache():
    macro._news_cache.clear()


@pytest.mark.parametrize("title, direction, vetoed", [
    ("Analyst downgrades Apple to sell on weak iPhone demand", "LONG", True),
    ("Company cuts full-year guidance as sales slow", "LONG", True),
    ("SEC investigation into accounting widens", "LONG", True),
    ("Rival agrees to be acquired at 40% premium", "SHORT", True),
    ("Analyst downgrades Apple to sell", "SHORT", False),
    ("Apple unveils new colours for its phone", "LONG", False),
])
def test_macro_vetoes_a_high_impact_catalyst_against_the_trade(cfg, title, direction,
                                                               vetoed):
    out = _run(macro.vote(_alpha_signal(direction), _NewsFeed([title]), cfg, NOW))
    assert out.veto is vetoed, out.reasons


def test_macro_vetoes_futures_moving_hard_against(cfg):
    out = _run(macro.vote(_alpha_signal("LONG"), _NewsFeed([], futures=-1.8), cfg, NOW))
    assert out.veto
    fine = _run(macro.vote(_alpha_signal("SHORT"), _NewsFeed([], futures=-1.8), cfg, NOW))
    assert not fine.veto


def test_macro_is_neutral_on_a_feed_without_news(cfg):
    out = _run(macro.vote(_alpha_signal(), FakeFeed(), cfg, NOW))
    assert not out.veto and out.score == pytest.approx(0.60)


def test_derivatives_vetoes_when_no_contract_qualified(cfg):
    from panaoptions.models import ContractSearch

    out = _run(derivatives.vote(_alpha_signal(), [], ContractSearch(
        symbol="SPY", note="nothing in band"), cfg, 500.0, "2026-09-23"))
    assert out.veto and "nothing in band" in out.reasons[0]


def test_derivatives_scores_flow_and_put_call(cfg):
    from panaoptions.models import ContractSearch

    call = _contract("SPY", mid=3.0).model_copy(update={"volume": 9000,
                                                        "open_interest": 1000})
    put = _contract("SPY", mid=3.0, right=OptionRight.PUT, delta=-0.45).model_copy(
        update={"volume": 500, "open_interest": 4000})
    out = _run(derivatives.vote(_alpha_signal(), [call, put],
                                ContractSearch(symbol="SPY", chosen=call), cfg,
                                500.0, "2026-09-23"))
    assert not out.veto and out.score > 0.8
    assert any("flow agrees" in r for r in out.reasons)


def test_cmio_needs_no_veto_and_the_threshold(cfg):
    sig = _alpha_signal()
    good = [agents_base.AgentVote("Technical", 0.8), agents_base.AgentVote("Derivatives", 0.7),
            agents_base.AgentVote("Macro", 0.6)]
    assert cmio.combine(good, sig, cfg).approved
    vetoed = good[:2] + [agents_base.AgentVote("Macro", 0.9, veto=True, reasons=["x"])]
    assert not cmio.combine(vetoed, sig, cfg).approved
    weak = [agents_base.AgentVote(a, 0.4) for a in ("Technical", "Derivatives", "Macro")]
    assert not cmio.combine(weak, sig, cfg).approved


def test_cmio_applies_the_learned_strategy_weight(cfg):
    votes = [agents_base.AgentVote(a, 0.62) for a in ("Technical", "Derivatives", "Macro")]
    assert cmio.combine(votes, _alpha_signal(), cfg).approved
    cfg.data["strategy_weights"] = {"orb_vwap": 0.8}
    verdict = cmio.combine(votes, _alpha_signal(), cfg)
    assert not verdict.approved and verdict.weight == 0.8
    cfg.data["strategy_weights"] = {"orb_vwap": 9}           # clamped
    assert cmio.combine(votes, _alpha_signal(), cfg).weight == 1.5


def test_the_gatekeeper_overrules_a_committee_approval(account):
    from panaoptions.models import ContractSearch

    wide = _contract("SPY", mid=5.0, spread_pct=12.0)
    verdict = cmio.combine([agents_base.AgentVote(a, 0.9) for a in
                            ("Technical", "Derivatives", "Macro")], _alpha_signal(), account)
    chief = cmio.CMIO(account, RiskGatekeeper(account))
    chief.gate(verdict, _alpha_signal(), _setup(), ContractSearch(symbol="SPY", chosen=wide))
    assert not verdict.approved and "Risk Gatekeeper" in verdict.reason


def test_the_model_can_move_a_score_but_not_lift_a_veto(cfg):
    vote = agents_base.AgentVote("Technical", 0.5, veto=True, reasons=["thin"])
    out = agents_base.blend(vote, agents_base.Opinion(score=1.0, reason="looks great"), cfg)
    assert out.veto and out.score == pytest.approx(0.7)
    macro_vote = agents_base.AgentVote("Macro", 0.6)
    agents_base.blend(macro_vote, agents_base.Opinion(score=0.2, veto=True,
                                                      reason="lawsuit"), cfg,
                      veto_allowed=True)
    assert macro_vote.veto


def test_a_slow_model_times_out_to_the_rules_score(cfg, monkeypatch):
    from panaoptions.ml import llm

    async def slow(**_kw):
        await asyncio.sleep(5)

    monkeypatch.setenv("PANAOPTIONS_AGENT_LLM", "on")
    monkeypatch.setattr(llm, "structured_complete", slow)
    cfg.data.setdefault("agents", {})["llm_timeout_seconds"] = 0.05
    assert _run(agents_base.ask(cfg, "Technical", "role", {})) is None


def test_an_answering_model_is_blended_in(cfg, monkeypatch):
    from panaoptions.ml import llm

    async def answer(**_kw):
        return agents_base.Opinion(score=0.2, reason="15m trend is rolling over")

    monkeypatch.setenv("PANAOPTIONS_AGENT_LLM", "on")
    monkeypatch.setattr(llm, "structured_complete", answer)
    setup = _setup()
    out = _run(technical.vote(_alpha_signal(), setup, [], cfg, screen_rvol=2.0))
    assert out.model_view == "15m trend is rolling over"
    assert out.score < _alpha_signal().confidence_score


# =========================================================================== #
# Wired into the desk
# =========================================================================== #
def test_the_desk_trades_only_after_the_committee_approves(cfg, monkeypatch):
    from zoneinfo import ZoneInfo

    from panaoptions import clock
    from panaoptions.app import OptionsDesk

    et = ZoneInfo("America/New_York")
    monkeypatch.setattr(clock, "now", lambda tz: datetime(2026, 9, 23, 10, 20, tzinfo=et))
    cfg.data["contracts"]["max_contract_price"] = 2.0
    desk = OptionsDesk(cfg=cfg, feed=FakeFeed())
    _run(desk.cycle())
    kinds = [e["kind"] for e in desk.activity.recent(200)]
    assert "vote.approved" in kinds and "trade.open" in kinds
    events = desk.activity.recent(200)
    approved = [e for e in events if e["kind"] == "vote.approved"]
    opened = [e for e in events if e["kind"] == "trade.open"]
    assert {e["detail"].split()[0] for e in approved} >= {"SPY", "QQQ"}
    assert len(opened) == len(approved)             # nothing opened unvoted
    assert all(e["seq"] < o["seq"] for e, o in zip(
        sorted(approved, key=lambda e: e["seq"]),
        sorted(opened, key=lambda e: e["seq"]), strict=True))


def test_a_macro_veto_stops_the_desk_buying(cfg, monkeypatch):
    from zoneinfo import ZoneInfo

    from panaoptions import clock
    from panaoptions.app import OptionsDesk

    class Vetoing(FakeFeed):
        async def news(self, symbol, count=10):
            return [{"title": f"{symbol} downgraded to sell after guidance cut",
                     "publisher": "x", "published": None}]

    et = ZoneInfo("America/New_York")
    monkeypatch.setattr(clock, "now", lambda tz: datetime(2026, 9, 23, 10, 20, tzinfo=et))
    cfg.data["contracts"]["max_contract_price"] = 2.0
    desk = OptionsDesk(cfg=cfg, feed=Vetoing())
    _run(desk.cycle())
    kinds = [e["kind"] for e in desk.activity.recent(200)]
    assert "vote.refused" in kinds and "trade.open" not in kinds
    assert "Macro veto" in desk.candidate["refused"]
    assert not desk.ledger.open_trades


def test_the_zerodte_price_ceiling_does_not_block_the_index_cap(account):
    """$3.50 a share stops single stocks at $350; SPY may reach $10 ($1,000)."""
    from panaoptions.engine import contracts

    account.data["contracts"].update(max_contract_price=3.50,
                                     index_max_contract_price=10.00)
    gate = RiskGatekeeper(account)
    spy = contracts.choose("SPY", [_contract("SPY", mid=9.50)], Direction.LONG,
                           account, setup=_setup("SPY"), budget=gate.budget_for("SPY"))
    assert spy.chosen is not None and spy.chosen.cost() == 950.0
    aapl = contracts.choose("AAPL", [_contract("AAPL", mid=9.50)], Direction.LONG,
                            account, setup=_setup("AAPL"), budget=gate.budget_for("AAPL"))
    assert aapl.chosen is None
