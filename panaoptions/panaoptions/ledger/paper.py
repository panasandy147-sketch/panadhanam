"""The paper ledger: open, manage, scale out, close, and keep the statistics.

Nothing here touches a broker. Fills are simulated at the level that triggered
them, minus slippage on every side of every trade — including each leg of a
scale-out, because a half-exit is a real fill with a real cost and a ledger
that forgets it flatters every result.

Exit precedence inside one bar matters. When a bar's range covers both the
stop and a target, the STOP is taken. Without tick data the order is
unknowable, and choosing the happy answer would inflate the win rate of every
session ever run.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from panaoptions.logging import get_logger
from panaoptions.models import Direction, ExitReason, Fill, PaperTrade, Signal

log = get_logger("ledger")


class PaperLedger:
    def __init__(self, cfg, risk) -> None:
        self.cfg = cfg
        self.risk = risk
        self.open_trades: dict[str, PaperTrade] = {}
        self.closed: list[PaperTrade] = []

    @property
    def slippage(self) -> float:
        return float(self.cfg.get("risk.slippage_per_contract", 0.02))

    # ------------------------------------------------------------------ #
    def open(self, signal: Signal, ts: datetime | None = None) -> PaperTrade:
        """Enter at the mid plus slippage — you pay up to get filled."""
        ts = ts or signal.ts
        fill_price = round(signal.entry_price + self.slippage, 4)

        trade = PaperTrade(
            id=f"PT-{uuid.uuid4().hex[:8].upper()}",
            signal_id=signal.id, symbol=signal.symbol,
            direction=signal.direction, contract_label=signal.contract.label,
            opened_at=ts, quantity=signal.quantity, entry_price=fill_price,
            stop_price=signal.stop_price, target_1=signal.target_1,
            target_2=signal.target_2,
            underlying_support=signal.underlying_support,
            strategy=signal.strategy,
            invalidation_note=signal.invalidation_note,
            remaining=signal.quantity, max_price_seen=fill_price,
        )
        trade.fills.append(Fill(ts=ts, quantity=signal.quantity,
                                price=fill_price, reason="ENTRY"))

        self.open_trades[trade.id] = trade
        self.risk.state.open_trades = len(self.open_trades)
        self.risk.state.trades_taken += 1
        log.info("OPEN %s x%d @ %.2f (stop %.2f, TP1 %.2f, TP2 %.2f)",
                 trade.contract_label, trade.quantity, fill_price,
                 trade.stop_price, trade.target_1, trade.target_2)
        return trade

    # ------------------------------------------------------------------ #
    def mark(self, trade_id: str, contract_price: float,
             underlying_price: float | None = None,
             ts: datetime | None = None,
             ema_fast: float | None = None) -> list[Fill]:
        """Apply one price update to an open trade. Returns any fills made."""
        trade = self.open_trades.get(trade_id)
        if not trade or not trade.is_open:
            return []
        ts = ts or datetime.now()
        fills: list[Fill] = []
        trade.max_price_seen = max(trade.max_price_seen, contract_price)

        # 1. The stop, first and always. See the module docstring.
        if contract_price <= trade.stop_price:
            fills.append(self._exit(trade, trade.stop_price, ts,
                                    ExitReason.STOP))
            return fills

        # 2. The underlying invalidating the reason for the trade. The option
        #    may not have hit its own stop yet, but the setup has gone.
        if underlying_price is not None and trade.underlying_support:
            broken = (underlying_price < trade.underlying_support
                      if trade.direction is Direction.LONG
                      else underlying_price > trade.underlying_support)
            if broken:
                fills.append(self._exit(trade, contract_price, ts,
                                        ExitReason.UNDERLYING_BREAK))
                return fills

        if self._trailing(trade):
            return fills + self._manage_trail(trade, contract_price,
                                              underlying_price, ts, ema_fast)

        # 3. First target: scale out, then move the stop to breakeven.
        if not trade.breakeven_armed and contract_price >= trade.target_1:
            share = float(self.cfg.get("risk.take_profit_1_size_pct", 50.0))
            half = max(1, int(round(trade.remaining * share / 100.0)))
            fills.append(self._reduce(trade, half, trade.target_1, ts,
                                      "TARGET_1"))
            trade.breakeven_armed = True
            if bool(self.cfg.get("risk.breakeven_after_tp1", True)):
                trade.stop_price = trade.entry_price
                log.info("%s scaled out %d at TP1 — stop to breakeven %.2f",
                         trade.contract_label, half, trade.stop_price)

        # 4. Second target, or the EMA trail once the runner is alone.
        if trade.is_open and contract_price >= trade.target_2:
            fills.append(self._exit(trade, trade.target_2, ts,
                                    ExitReason.TARGET_2))
            return fills

        if (trade.is_open and trade.breakeven_armed
                and bool(self.cfg.get("risk.trail_on_ema", True))
                and ema_fast is not None and underlying_price is not None):
            trailing_out = (underlying_price < ema_fast
                            if trade.direction is Direction.LONG
                            else underlying_price > ema_fast)
            if trailing_out:
                fills.append(self._exit(trade, contract_price, ts,
                                        ExitReason.TRAIL))
        return fills

    # ------------------------------------------------------------------ #
    def _trailing(self, trade: PaperTrade) -> bool:
        """Should this trade run to a trailing exit rather than scale out?

        A single contract cannot be halved, so "exit 50% at +40%" quietly
        becomes "exit everything at +40%" and the runner the strategy depends
        on never exists. `auto` catches exactly that case; `trail` forces it.
        """
        style = str(self.cfg.get("risk.exit_style", "scale")).lower()
        if style == "trail":
            return True
        if style != "auto":
            return False
        share = float(self.cfg.get("risk.take_profit_1_size_pct", 50.0))
        return int(round(trade.remaining * share / 100.0)) < 1 or trade.remaining < 2

    def _manage_trail(self, trade: PaperTrade, contract_price: float,
                      underlying_price: float | None, ts: datetime,
                      ema_fast: float | None) -> list[Fill]:
        """Breakeven at the trigger, then hold until the 9 EMA gives way.

        No profit target at all: the exit is the trend ending, which is what
        lets one contract still catch a runner.
        """
        trigger = float(self.cfg.get("risk.breakeven_trigger_pct", 35.0))
        if not trade.breakeven_armed:
            if contract_price >= trade.entry_price * (1 + trigger / 100.0):
                trade.breakeven_armed = True
                trade.stop_price = trade.entry_price
                log.info("%s reached +%.0f%% — stop moved to breakeven %.2f, "
                         "now trailing the 9 EMA",
                         trade.contract_label, trigger, trade.stop_price)
            return []

        if ema_fast is None or underlying_price is None:
            return []
        closed_against = (underlying_price < ema_fast
                          if trade.direction is Direction.LONG
                          else underlying_price > ema_fast)
        if closed_against:
            return [self._exit(trade, contract_price, ts, ExitReason.EMA_TRAIL)]
        return []

    # ------------------------------------------------------------------ #
    def close(self, trade_id: str, price: float, reason: ExitReason,
              ts: datetime | None = None) -> Fill | None:
        trade = self.open_trades.get(trade_id)
        if not trade or not trade.is_open:
            return None
        return self._exit(trade, price, ts or datetime.now(), reason)

    def close_all(self, prices: dict[str, float], reason: ExitReason,
                  ts: datetime | None = None) -> list[Fill]:
        """Square everything off — the 15:45 sweep, or the circuit breaker."""
        out = []
        for trade_id in list(self.open_trades):
            trade = self.open_trades[trade_id]
            price = prices.get(trade.contract_label, trade.entry_price)
            fill = self.close(trade_id, price, reason, ts)
            if fill:
                out.append(fill)
        return out

    # ------------------------------------------------------------------ #
    def _reduce(self, trade: PaperTrade, quantity: int, price: float,
                ts: datetime, reason: str) -> Fill:
        fill_price = round(price - self.slippage, 4)
        pnl = round((fill_price - trade.entry_price) * quantity
                    * self.cfg.multiplier, 2)
        trade.remaining -= quantity
        trade.realised_pnl = round(trade.realised_pnl + pnl, 2)
        trade.fills.append(Fill(ts=ts, quantity=-quantity, price=fill_price,
                                reason=reason))
        self.risk.record_pnl(pnl)
        return trade.fills[-1]

    def _exit(self, trade: PaperTrade, price: float, ts: datetime,
              reason: ExitReason) -> Fill:
        fill = self._reduce(trade, trade.remaining, price, ts, reason.value)
        trade.closed_at = ts
        trade.exit_reason = reason
        self.open_trades.pop(trade.id, None)
        self.closed.append(trade)
        self.risk.state.open_trades = len(self.open_trades)
        log.info("CLOSE %s @ %.2f (%s) — trade P&L %+.2f",
                 trade.contract_label, fill.price, reason.value,
                 trade.realised_pnl)
        return fill

    # ------------------------------------------------------------------ #
    def stats(self) -> dict[str, Any]:
        trades = self.closed
        if not trades:
            return {"trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
                    "total_pnl": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
                    "profit_factor": 0.0, "expectancy": 0.0, "open": len(self.open_trades),
                    "by_exit": {}}

        wins = [t for t in trades if t.realised_pnl > 0]
        losses = [t for t in trades if t.realised_pnl < 0]
        gross_win = sum(t.realised_pnl for t in wins)
        gross_loss = abs(sum(t.realised_pnl for t in losses))

        by_exit: dict[str, dict[str, Any]] = {}
        for t in trades:
            key = t.exit_reason.value if t.exit_reason else "UNKNOWN"
            slot = by_exit.setdefault(key, {"count": 0, "pnl": 0.0})
            slot["count"] += 1
            slot["pnl"] = round(slot["pnl"] + t.realised_pnl, 2)

        total = round(sum(t.realised_pnl for t in trades), 2)
        return {
            "trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(trades) * 100, 1),
            "total_pnl": total,
            "avg_win": round(gross_win / len(wins), 2) if wins else 0.0,
            "avg_loss": round(-gross_loss / len(losses), 2) if losses else 0.0,
            # Profit factor is undefined with no losses; 0.0 would read as
            # "terrible" when it means "nothing has gone wrong yet".
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
            "expectancy": round(total / len(trades), 2),
            "open": len(self.open_trades),
            "by_exit": by_exit,
        }
