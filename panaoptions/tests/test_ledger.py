"""The paper ledger: scale-outs, breakeven, trailing, slippage, statistics."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from panaoptions.ledger.paper import PaperLedger
from panaoptions.models import Direction, ExitReason, OptionContract, OptionRight, Signal
from panaoptions.risk.guardrails import RiskManager

TS = datetime(2026, 9, 22, 9, 40)


@pytest.fixture
def ledger(cfg):
    return PaperLedger(cfg, RiskManager(cfg))


def _signal(quantity=4, entry=0.50):
    contract = OptionContract(symbol="AAPL", right=OptionRight.CALL, strike=245,
                              expiry="2026-10-02", dte=10,
                              bid=entry - 0.01, ask=entry + 0.01, delta=0.18)
    return Signal(id="S1", ts=TS, symbol="AAPL", direction=Direction.LONG,
                  contract=contract, quantity=quantity, entry_price=entry,
                  stop_price=round(entry * 0.8, 2),
                  target_1=round(entry * 1.4, 2), target_2=round(entry * 1.7, 2),
                  underlying_support=229.0)


# --------------------------------------------------------------------------- #
def test_entry_pays_slippage(ledger):
    trade = ledger.open(_signal(), TS)
    assert trade.entry_price == 0.52, "you pay up to get filled"


def test_the_stop_wins_when_one_bar_covers_both_levels(ledger):
    trade = ledger.open(_signal(), TS)
    # A bar whose range spans the stop AND the target. Without tick data the
    # order is unknowable, and the optimistic reading inflates every backtest.
    ledger.mark(trade.id, 0.40, underlying_price=231.0, ts=TS)
    assert trade.exit_reason is ExitReason.STOP
    assert trade.realised_pnl < 0


def test_first_target_scales_out_half_and_arms_breakeven(ledger, cfg):
    cfg.data['risk']['exit_style'] = 'scale'
    trade = ledger.open(_signal(quantity=4), TS)
    ledger.mark(trade.id, 0.70, underlying_price=231.0, ts=TS)

    assert trade.remaining == 2
    assert trade.breakeven_armed
    assert trade.stop_price == trade.entry_price
    assert trade.realised_pnl > 0
    assert trade.is_open


def test_a_single_contract_trails_instead_of_scaling(ledger):
    # One contract cannot be halved, so "exit 50% at +40%" would quietly
    # become "exit everything at +40%" and the runner would never exist.
    trade = ledger.open(_signal(quantity=1, entry=0.50), TS)
    entry = trade.entry_price

    # +35% arms breakeven and does NOT close the position.
    ledger.mark(trade.id, entry * 1.36, underlying_price=231.0, ts=TS,
                ema_fast=230.0)
    assert trade.is_open
    assert trade.breakeven_armed
    assert trade.stop_price == entry

    # It keeps running while price holds the 9 EMA...
    ledger.mark(trade.id, entry * 1.9, underlying_price=233.0,
                ts=TS + timedelta(minutes=10), ema_fast=231.0)
    assert trade.is_open, "a target must not cut the runner short"

    # ...and exits when a candle closes the wrong side of it.
    ledger.mark(trade.id, entry * 1.8, underlying_price=230.5,
                ts=TS + timedelta(minutes=15), ema_fast=231.0)
    assert not trade.is_open
    assert trade.exit_reason is ExitReason.EMA_TRAIL
    assert trade.realised_pnl > 0


def test_the_trailing_runner_gives_back_nothing_below_breakeven(ledger):
    trade = ledger.open(_signal(quantity=1, entry=0.50), TS)
    entry = trade.entry_price
    ledger.mark(trade.id, entry * 1.36, underlying_price=231.0, ts=TS,
                ema_fast=230.0)

    # The trade rolls over and comes back to entry: the breakeven stop fires.
    ledger.mark(trade.id, entry, underlying_price=231.0,
                ts=TS + timedelta(minutes=20), ema_fast=230.0)
    assert not trade.is_open
    assert trade.realised_pnl == pytest.approx(-self_slippage(ledger), abs=1e-6)


def self_slippage(ledger):
    """Breakeven still costs the exit slippage — the fill is real."""
    return ledger.slippage * ledger.cfg.multiplier


def test_a_two_contract_position_still_scales_out(ledger, cfg):
    cfg.data["risk"]["exit_style"] = "auto"
    trade = ledger.open(_signal(quantity=2), TS)
    ledger.mark(trade.id, 0.70, underlying_price=231.0, ts=TS)

    assert trade.remaining == 1, "two contracts CAN be halved"
    assert trade.is_open
    assert trade.breakeven_armed


def test_trail_can_be_forced_for_any_size(ledger, cfg):
    cfg.data["risk"]["exit_style"] = "trail"
    trade = ledger.open(_signal(quantity=4, entry=0.50), TS)
    # +35% of the 0.52 fill is 0.702, so mark clearly above it.
    ledger.mark(trade.id, 0.75, underlying_price=231.0, ts=TS, ema_fast=230.0)

    assert trade.remaining == 4, "trail mode never scales out"
    assert trade.breakeven_armed


def test_the_runner_stops_at_breakeven_not_at_a_loss(ledger, cfg):
    cfg.data['risk']['exit_style'] = 'scale'
    trade = ledger.open(_signal(quantity=4), TS)
    ledger.mark(trade.id, 0.70, underlying_price=231.0, ts=TS)
    entry = trade.entry_price

    ledger.mark(trade.id, entry, underlying_price=231.0,
                ts=TS + timedelta(minutes=10))
    assert not trade.is_open
    assert trade.realised_pnl > 0, "the scaled-out half is already banked"


def test_a_broken_underlying_level_closes_the_trade(ledger):
    trade = ledger.open(_signal(), TS)
    ledger.mark(trade.id, 0.60, underlying_price=228.0, ts=TS)

    assert trade.exit_reason is ExitReason.UNDERLYING_BREAK, \
        "the option has not hit its stop, but the reason for owning it has gone"


def test_the_ema_trail_only_applies_after_the_first_target(ledger, cfg):
    cfg.data['risk']['exit_style'] = 'scale'
    trade = ledger.open(_signal(quantity=4), TS)

    # Below the EMA, but TP1 has not printed, so the trail is not armed yet.
    ledger.mark(trade.id, 0.60, underlying_price=230.0, ts=TS, ema_fast=231.0)
    assert trade.is_open and trade.remaining == 4

    ledger.mark(trade.id, 0.70, underlying_price=232.0, ts=TS, ema_fast=231.0)
    ledger.mark(trade.id, 0.68, underlying_price=230.0,
                ts=TS + timedelta(minutes=5), ema_fast=231.0)
    assert trade.exit_reason is ExitReason.TRAIL


def test_slippage_is_charged_on_every_leg_of_a_scale_out(ledger, cfg):
    cfg.data['risk']['exit_style'] = 'scale'
    trade = ledger.open(_signal(quantity=4), TS)
    ledger.mark(trade.id, 0.70, underlying_price=231.0, ts=TS)
    ledger.mark(trade.id, 0.85, underlying_price=232.0, ts=TS)

    exits = [f for f in trade.fills if f.quantity < 0]
    assert len(exits) == 2
    assert exits[0].price == 0.68 and exits[1].price == 0.83


def test_closing_everything_squares_off_at_the_given_price(ledger):
    trade = ledger.open(_signal(), TS)
    ledger.close_all({trade.contract_label: 0.55}, ExitReason.DAY_END, TS)

    assert not ledger.open_trades
    assert trade.exit_reason is ExitReason.DAY_END


def test_a_closed_trade_cannot_be_marked_again(ledger):
    trade = ledger.open(_signal(), TS)
    ledger.mark(trade.id, 0.40, ts=TS)
    booked = trade.realised_pnl

    assert ledger.mark(trade.id, 0.20, ts=TS) == []
    assert trade.realised_pnl == booked


def test_losses_reach_the_circuit_breaker(cfg):
    ledger = PaperLedger(cfg, RiskManager(cfg))
    for i in range(5):
        signal = _signal(quantity=4, entry=0.50)
        signal.id = f"S{i}"
        trade = ledger.open(signal, TS)
        ledger.mark(trade.id, 0.40, ts=TS)
        ledger.risk.state.open_trades = 0

    assert ledger.risk.state.halted, \
        "the ledger's realised losses must feed the daily limit"


def test_profit_factor_is_none_rather_than_zero_with_no_losses(ledger, cfg):
    cfg.data["risk"]["exit_style"] = "scale"
    trade = ledger.open(_signal(quantity=2), TS)
    ledger.mark(trade.id, 0.70, ts=TS)
    ledger.mark(trade.id, 0.85, ts=TS)

    stats = ledger.stats()
    assert stats["profit_factor"] is None, \
        "0.0 would read as 'terrible' when it means 'nothing has gone wrong'"
    assert stats["win_rate"] == 100.0
    assert stats["by_exit"]["TARGET_2"]["count"] == 1
