"""Risk Management Officer.

Deliberately NOT an LLM. Every number here is arithmetic against config, so it
is auditable, unit-testable, and immune to a persuasive prompt. The CMIO can
propose whatever it likes; this module decides what actually gets sized.

The sizing identity:

    risk_amount   = capital * risk_per_trade_pct / 100
    stop_points   = |entry - stop_loss|
    quantity      = floor(risk_amount / stop_points)      # lot-rounded for F&O
    risk_reward   = |target - entry| / stop_points        # must be >= min_risk_reward
"""
from __future__ import annotations

import math
import uuid
from datetime import datetime
from typing import Any

from app.core.config import Config, get_config
from app.core.logging import get_logger
from app.core.models import (
    AgentReport,
    Bias,
    Instrument,
    InstrumentType,
    MarketContext,
    RiskState,
    Side,
    SignalStatus,
    TradeSignal,
)
from app.core.registry import register_agent

log = get_logger("agent.risk")


class RiskRejection(Exception):
    def __init__(self, reasons: list[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = reasons


@register_agent("risk")
class RiskManager:
    """Sizing, validation and the daily kill-switch."""

    agent_id = "risk"

    def __init__(self, cfg: Config | None = None) -> None:
        self.cfg = cfg or get_config()
        self.spec = self.cfg.agent("risk")
        self.name = self.spec.get("name", "Risk Management Officer")
        self.state = RiskState(
            capital=float(self.cfg.get("risk.total_capital", 100_000)),
            daily_loss_limit=float(self.cfg.get("risk.total_capital", 100_000))
            * float(self.cfg.get("risk.max_daily_loss_pct", 3.0)) / 100.0,
        )
        self._day = datetime.now().date()

    # ------------------------------------------------------------------ #
    # Day boundary
    # ------------------------------------------------------------------ #
    def roll_day_if_needed(self) -> None:
        today = datetime.now().date()
        if today != self._day:
            log.info("new trading day — resetting daily risk counters")
            self._day = today
            self.state.realised_pnl = 0.0
            self.state.unrealised_pnl = 0.0
            self.state.trades_today = 0
            self.state.wins_today = 0
            self.state.losses_today = 0
            self.state.halted = False
            self.state.halt_reason = ""

    # ------------------------------------------------------------------ #
    # Gate checks
    # ------------------------------------------------------------------ #
    def desk_checks(self) -> list[str]:
        """Reasons the desk is closed for new business, regardless of the setup."""
        self.roll_day_if_needed()
        reasons: list[str] = []

        if self.state.halted:
            reasons.append(f"Desk halted: {self.state.halt_reason}")

        if self.state.daily_pnl <= -self.state.daily_loss_limit:
            self.state.halted = True
            self.state.halt_reason = (
                f"Daily loss limit hit ({self.state.daily_pnl:,.0f} vs limit "
                f"-{self.state.daily_loss_limit:,.0f})")
            reasons.append(self.state.halt_reason)

        max_pos = int(self.cfg.get("risk.max_open_positions", 3))
        if self.state.open_positions >= max_pos:
            reasons.append(f"Max open positions reached ({self.state.open_positions}/{max_pos})")

        max_exposure = self._max_exposure()
        if self.state.exposure >= max_exposure:
            reasons.append(f"Exposure cap reached ({self.state.exposure:,.0f}/{max_exposure:,.0f})")

        if self._past_entry_cutoff():
            reasons.append(f"Past the no-new-entry cutoff "
                           f"({self.cfg.get('system.no_new_entry_after', '15:00')})")
        return reasons

    def _max_exposure(self) -> float:
        """Notional headroom.

        Leverage widens how much POSITION you may hold; it never widens how much
        you may LOSE. The per-trade risk budget is always a percentage of real
        capital, computed before this is ever consulted.
        """
        pct = float(self.cfg.get("risk.max_exposure_pct", 50.0)) / 100.0
        leverage = max(float(self.cfg.get("risk.intraday_leverage", 1.0)), 1.0)
        return self.state.capital * leverage * pct

    def _past_entry_cutoff(self) -> bool:
        cutoff = str(self.cfg.get("system.no_new_entry_after", "15:00"))
        try:
            h, m = (int(x) for x in cutoff.split(":"))
        except ValueError:
            return False
        now = datetime.now()
        return (now.hour, now.minute) >= (h, m)

    # ------------------------------------------------------------------ #
    # Core: build a sized, validated signal
    # ------------------------------------------------------------------ #
    def evaluate(
        self,
        ctx: MarketContext,
        bias: Bias,
        reports: list[AgentReport],
        composite_score: float,
        confirmations: list[str],
        rationale: str = "",
        counter_argument: str = "",
    ) -> TradeSignal:
        """Always returns a TradeSignal. Check `.status` — REJECTED carries reasons."""
        signal_id = f"SIG-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4].upper()}"
        reasons: list[str] = []

        instrument, entry, source_report = self._choose_instrument(ctx, bias)
        side = Side.BUY if bias == Bias.BULLISH else Side.SELL

        # Options are always BOUGHT — a bearish view buys a put, it doesn't sell a call.
        if instrument.instrument_type in {InstrumentType.CALL, InstrumentType.PUT}:
            side = Side.BUY

        stop_loss, target, sl_note = self._levels(ctx, bias, entry, instrument, source_report)

        # Record the underlying's spot and the leg's delta at entry so the outcome
        # tracker can mark an option position to market later.
        entry_spot = ctx.quote.last_price if ctx.quote else None
        entry_delta = None
        if source_report:
            leg = source_report.extra.get("suggested_leg") or {}
            entry_delta = leg.get("delta")

        signal = TradeSignal(
            id=signal_id, instrument=instrument, side=side,
            entry=round(entry, 2), stop_loss=round(stop_loss, 2), target=round(target, 2),
            entry_spot=entry_spot, entry_delta=entry_delta,
            bias=bias, composite_score=round(composite_score, 3),
            confirmations=confirmations, rationale=rationale,
            counter_argument=counter_argument, reports=reports, regime=ctx.regime,
        )

        # ---- desk-level gates ----
        reasons.extend(self.desk_checks())

        # ---- stop-loss sanity ----
        stop_points = abs(entry - stop_loss)
        if stop_points <= 0:
            reasons.append("Stop loss equals entry — invalidation is undefined")
        else:
            stop_pct = stop_points / entry * 100 if entry else 0.0
            min_pct = float(self.cfg.get("risk.min_stop_distance_pct", 0.15))
            max_pct = float(self.cfg.get("risk.max_stop_distance_pct", 3.0))
            # Option premiums are far more volatile; widen the band for them.
            if instrument.instrument_type in {InstrumentType.CALL, InstrumentType.PUT}:
                min_pct, max_pct = min_pct * 4, max_pct * 12
            if stop_pct < min_pct:
                reasons.append(f"Stop {stop_pct:.2f}% is too tight (min {min_pct}%) — noise will hit it")
            elif stop_pct > max_pct:
                reasons.append(f"Stop {stop_pct:.2f}% is too wide (max {max_pct}%) — invalidation ill-defined")

        # ---- risk:reward ----
        reward_points = abs(target - entry)
        rr = reward_points / stop_points if stop_points > 0 else 0.0
        min_rr = float(self.cfg.get("risk.min_risk_reward", 2.0))
        max_rr = float(self.cfg.get("risk.max_risk_reward", 10.0))
        if rr < min_rr:
            reasons.append(f"R:R {rr:.2f} below the {min_rr}:1 minimum — rejected")
        elif rr > max_rr:
            reasons.append(f"R:R {rr:.2f} implausibly high (>{max_rr}) — target is not credible")

        # ---- direction sanity ----
        if side == Side.BUY and stop_loss >= entry:
            reasons.append("Long with stop at or above entry")
        if side == Side.SELL and stop_loss <= entry:
            reasons.append("Short with stop at or below entry")

        # ---- position sizing ----
        risk_pct = min(float(self.cfg.get("risk.risk_per_trade_pct", 1.0)),
                       float(self.cfg.get("risk.max_risk_per_trade_pct", 2.0)))
        risk_amount = self.state.capital * risk_pct / 100.0

        quantity, lots = 0, 0
        if stop_points > 0:
            raw_qty = risk_amount / stop_points
            if max(instrument.lot_size, 1) > 1:
                lot_size = max(instrument.lot_size, 1)
                lots = int(math.floor(raw_qty / lot_size))
                quantity = lots * lot_size
                if lots < 1:
                    reasons.append(
                        f"Position sizes to 0 lots: risk budget {risk_amount:,.0f} / "
                        f"{stop_points:.2f} pts = {raw_qty:.1f} units, but one lot is "
                        f"{lot_size}. Capital is too small for this stop distance.")
            else:
                quantity = int(math.floor(raw_qty))
                lots = quantity
                if quantity < 1:
                    reasons.append("Position sizes to 0 shares — stop is too wide for the risk budget")


        # ---- capital caps TRIM the size, they don't veto the idea ----------
        # A tight stop naturally implies a large notional. A real desk cuts the
        # size to fit its exposure limit rather than passing on the trade; it
        # only passes when even the minimum tradeable size won't fit.
        lot_size = max(instrument.lot_size, 1)
        is_option = instrument.instrument_type in {InstrumentType.CALL, InstrumentType.PUT}

        caps: list[tuple[str, float]] = []
        max_exposure = self._max_exposure()
        caps.append(("exposure limit", max(max_exposure - self.state.exposure, 0.0)))
        if is_option:
            caps.append(("option premium cap", self.state.capital * float(
                self.cfg.get("risk.options_max_premium_pct", 25.0)) / 100.0))

        cap_note = ""
        for label, budget in caps:
            if entry <= 0:
                break
            affordable = budget / entry
            max_qty = (int(math.floor(affordable / lot_size)) * lot_size
                       if lot_size > 1 else int(math.floor(affordable)))
            if max_qty < quantity:
                if max_qty <= 0:
                    reasons.append(
                        f"Cannot fit even one {'lot' if lot_size > 1 else 'unit'} "
                        f"within the {label} (₹{budget:,.0f} available, "
                        f"₹{entry * lot_size:,.0f} needed)")
                    quantity = 0
                    break
                cap_note = (f"size trimmed from {quantity} to {max_qty} by the {label}")
                quantity = max_qty

        lots = quantity // lot_size if lot_size > 1 else quantity
        notional = quantity * entry
        if cap_note:
            log.info("%s: %s", ctx.symbol, cap_note)

        # ---- option premium richness ----
        if is_option:
            iv = (ctx.indicators or {}).get("derivatives", {}).get("atm_iv", 0.0)
            iv_cap = float(self.cfg.get("risk.reject_if_iv_percentile_above", 85.0))
            if iv and iv > iv_cap:
                reasons.append(f"ATM IV {iv:.1f}% above the {iv_cap}% ceiling — premium too rich")

        # ---- finalise ----
        if quantity <= 0 and not any("size" in r or "Cannot fit" in r or "0 lots" in r
                                     or "0 shares" in r for r in reasons):
            reasons.append("Position sizes to zero after applying capital limits")

        signal.quantity = quantity
        signal.lots = lots
        signal.risk_per_unit = round(stop_points, 2)
        signal.reward_per_unit = round(reward_points, 2)
        signal.total_risk = round(stop_points * quantity, 2)
        signal.risk_reward = round(rr, 2)
        signal.notional = round(notional, 2)
        signal.capital_at_risk_pct = round(
            signal.total_risk / self.state.capital * 100, 3) if self.state.capital else 0.0
        signal.rationale = (rationale + f" | Sizing: {sl_note}"
                            + (f" | {cap_note}" if cap_note else "")).strip(" |")

        if reasons:
            signal.status = SignalStatus.REJECTED
            signal.rejection_reasons = reasons
            log.info("REJECTED %s %s: %s", ctx.symbol, bias.value, reasons[0])
        else:
            signal.status = SignalStatus.APPROVED
            log.info("APPROVED %s", signal.alert_line())
        return signal

    # ------------------------------------------------------------------ #
    # Instrument & level selection
    # ------------------------------------------------------------------ #
    def _choose_instrument(self, ctx: MarketContext, bias: Bias
                           ) -> tuple[Instrument, float, AgentReport | None]:
        """Trade the option leg the derivatives analyst picked when there is one,
        otherwise the cash/underlying."""
        meta = self.cfg.instrument_meta(ctx.symbol)
        spot = ctx.quote.last_price if ctx.quote else 0.0

        deriv_report = None
        for r in (ctx.__dict__.get("_reports") or []):
            if r.agent_id == "derivatives":
                deriv_report = r
        leg = (deriv_report.extra.get("suggested_leg") if deriv_report else None)

        if leg and meta.get("fno") and ctx.option_chain:
            strike = leg["strike"]
            opt_type = leg["option_type"]
            expiry = leg.get("expiry", ctx.option_chain.expiry)
            tsym = self._option_tradingsymbol(meta.get("trading_symbol", ctx.symbol), expiry, strike, opt_type)
            instrument = Instrument(
                symbol=ctx.symbol, tradingsymbol=tsym,
                instrument_type=InstrumentType.CALL if opt_type == "CE" else InstrumentType.PUT,
                exchange="NFO", lot_size=int(meta.get("lot_size", 1)),
                strike=strike, expiry=expiry, tick_size=float(meta.get("tick_size", 0.05)),
            )
            return instrument, float(leg["ltp"]), deriv_report

        instrument = Instrument(
            symbol=ctx.symbol, tradingsymbol=meta.get("trading_symbol", ctx.symbol),
            instrument_type=InstrumentType.EQUITY,
            exchange=meta.get("exchange", "NSE"), lot_size=1,
            tick_size=float(meta.get("tick_size", 0.05)),
        )
        return instrument, spot, None

    @staticmethod
    def _option_tradingsymbol(underlying: str, expiry: str, strike: float, opt: str) -> str:
        """NSE weekly convention, e.g. NIFTY25JAN24500CE."""
        try:
            d = datetime.fromisoformat(expiry)
            return f"{underlying}{d:%y%b}".upper() + f"{int(strike)}{opt}"
        except Exception:
            return f"{underlying}{int(strike)}{opt}"

    def _levels(self, ctx: MarketContext, bias: Bias, entry: float,
                instrument: Instrument, source: AgentReport | None
                ) -> tuple[float, float, str]:
        """Stop and target.

        Preference order for the stop:
          1. a structural invalidation level an analyst actually named
          2. ATR-based distance
          3. a percentage fallback
        Target is then derived to satisfy the minimum R:R — we never pick a
        target first and reverse-engineer a stop to make the ratio look good.
        """
        min_rr = float(self.cfg.get("risk.min_risk_reward", 2.0))
        atr_mult = float(self.cfg.get("risk.atr_stop_multiplier", 1.5))
        primary = (ctx.indicators or {}).get("primary") or {}
        atr = float(primary.get("atr", 0.0))
        is_option = instrument.instrument_type in {InstrumentType.CALL, InstrumentType.PUT}

        # --- options: work in premium space ---
        if is_option:
            # A 35% premium stop with a 2:1 target is the standard intraday convention.
            stop = entry * 0.65
            target = entry * (1 + 0.35 * min_rr)
            return stop, target, f"option premium stop at -35% ({stop:.2f})"

        # --- cash/underlying ---
        structural = None
        for report in (ctx.__dict__.get("_reports") or []):
            if report.agent_id == "candlestick" and report.invalidation_level:
                structural = report.invalidation_level
                break
        if structural is None and source and source.invalidation_level:
            structural = source.invalidation_level

        note = ""
        if structural and self._structural_is_sane(structural, entry, bias, atr):
            stop = structural
            note = f"structural stop at {stop:.2f}"
        elif atr > 0:
            stop = entry - atr_mult * atr if bias == Bias.BULLISH else entry + atr_mult * atr
            note = f"ATR stop ({atr_mult}x ATR {atr:.2f})"
        else:
            pct = float(self.cfg.get("risk.max_stop_distance_pct", 3.0)) / 3
            stop = entry * (1 - pct / 100) if bias == Bias.BULLISH else entry * (1 + pct / 100)
            note = f"percentage fallback stop ({pct:.2f}%)"

        stop_points = abs(entry - stop)
        target = (entry + stop_points * min_rr if bias == Bias.BULLISH
                  else entry - stop_points * min_rr)
        return stop, target, f"{note}, target at {min_rr}R"

    @staticmethod
    def _structural_is_sane(level: float, entry: float, bias: Bias, atr: float) -> bool:
        """A structural stop is only usable if it is on the correct side of entry
        and not absurdly far away."""
        if bias == Bias.BULLISH and level >= entry:
            return False
        if bias == Bias.BEARISH and level <= entry:
            return False
        distance = abs(entry - level)
        if entry and distance / entry > 0.05:
            return False
        if atr > 0 and distance > 5 * atr:
            return False
        return True

    # ------------------------------------------------------------------ #
    # Position bookkeeping
    # ------------------------------------------------------------------ #
    def register_open(self, signal: TradeSignal) -> None:
        self.state.open_positions += 1
        self.state.trades_today += 1
        self.state.exposure += signal.notional

    def register_close(self, signal: TradeSignal, pnl: float) -> None:
        self.state.open_positions = max(0, self.state.open_positions - 1)
        self.state.realised_pnl += pnl
        self.state.exposure = max(0.0, self.state.exposure - signal.notional)
        if pnl >= 0:
            self.state.wins_today += 1
        else:
            self.state.losses_today += 1
        if self.state.daily_pnl <= -self.state.daily_loss_limit:
            self.state.halted = True
            self.state.halt_reason = (
                f"Daily loss limit breached ({self.state.daily_pnl:,.0f})")
            log.warning("DESK HALTED — %s", self.state.halt_reason)

    def set_unrealised(self, value: float) -> None:
        self.state.unrealised_pnl = value

    def snapshot(self) -> dict[str, Any]:
        self.roll_day_if_needed()
        risk_pct = min(float(self.cfg.get("risk.risk_per_trade_pct", 1.0)),
                       float(self.cfg.get("risk.max_risk_per_trade_pct", 2.0)))
        return {
            **self.state.model_dump(),
            "daily_pnl": round(self.state.daily_pnl, 2),
            "risk_per_trade": round(self.state.capital * risk_pct / 100, 2),
            "risk_per_trade_pct": risk_pct,
            "min_risk_reward": self.cfg.get("risk.min_risk_reward", 2.0),
            "remaining_loss_budget": round(
                max(0.0, self.state.daily_loss_limit + self.state.daily_pnl), 2),
            "max_exposure": round(self._max_exposure(), 2),
            "intraday_leverage": float(self.cfg.get("risk.intraday_leverage", 1.0)),
        }

    # ------------------------------------------------------------------ #
    # Calculator used by the dashboard's interactive sizing panel
    # ------------------------------------------------------------------ #
    def size_calculator(self, capital: float, risk_pct: float, entry: float,
                        stop_loss: float, lot_size: int = 1,
                        rr: float | None = None) -> dict[str, Any]:
        rr = rr or float(self.cfg.get("risk.min_risk_reward", 2.0))
        stop_points = abs(entry - stop_loss)
        risk_amount = capital * risk_pct / 100.0
        if stop_points <= 0:
            return {"error": "Stop loss must differ from entry"}

        raw_qty = risk_amount / stop_points
        lots = int(math.floor(raw_qty / lot_size)) if lot_size > 1 else int(math.floor(raw_qty))
        quantity = lots * lot_size if lot_size > 1 else lots
        direction = 1 if stop_loss < entry else -1
        target = entry + direction * stop_points * rr

        actual_risk = quantity * stop_points
        out = {
            "risk_amount": round(risk_amount, 2),
            "stop_points": round(stop_points, 2),
            "raw_quantity": round(raw_qty, 2),
            "lots": lots,
            "quantity": quantity,
            "actual_risk": round(actual_risk, 2),
            "actual_risk_pct": round(actual_risk / capital * 100, 3) if capital else 0,
            "target": round(target, 2),
            "reward": round(actual_risk * rr, 2),
            "risk_reward": rr,
            "notional": round(quantity * entry, 2),
            "notional_pct_of_capital": round(quantity * entry / capital * 100, 2) if capital else 0,
            # Only meaningful once there is a position: with quantity 0 there is
            # nothing at risk, so reporting a ruin count would be nonsense.
            "max_consecutive_losses_to_ruin": (
                int(capital / actual_risk) if actual_risk > 0 else None),
        }
        if quantity == 0:
            out["note"] = (
                f"Not tradeable: a {stop_points:.2f}-point stop on ₹{risk_amount:,.0f} "
                f"of risk sizes to {raw_qty:.1f} units, below one lot of {lot_size}. "
                f"Widen the capital, tighten the stop, or trade a smaller instrument.")
        return out
