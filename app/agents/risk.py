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

from app.core import clock
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
        # Set each cycle by the engine from the news blackout rules; while it
        # holds, no new entry is allowed anywhere.
        self.blackout_reason: str = ""
        self._day = clock.market_now(
            str(self.cfg.get("system.timezone", "Asia/Kolkata"))).date()

    # ------------------------------------------------------------------ #
    # Day boundary
    # ------------------------------------------------------------------ #
    def roll_day_if_needed(self) -> None:
        today = clock.market_now(
            str(self.cfg.get("system.timezone", "Asia/Kolkata"))).date()
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

        heat_pct = float(self.cfg.get("risk.max_portfolio_heat_pct", 0) or 0)
        if heat_pct > 0 and self.state.open_risk >= self.state.capital * heat_pct / 100.0:
            reasons.append(f"Portfolio heat cap reached ({self.state.open_risk:,.0f} at "
                           f"risk, cap {heat_pct:g}% of capital)")

        if self.blackout_reason:
            reasons.append(self.blackout_reason)

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
        return clock.past(str(self.cfg.get("system.timezone", "Asia/Kolkata")),
                          str(self.cfg.get("system.no_new_entry_after", "15:00")))

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

        # ---- can this instrument actually be bought? ----
        if self._index_is_untradeable(self.cfg.instrument_meta(ctx.symbol), instrument):
            reasons.append(
                f"{ctx.symbol} is an index — there is no cash instrument to buy. "
                f"Trade it through an option or future; no option leg was "
                f"available this cycle (the derivatives analyst had no chain).")

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
        # Compared at the precision it is printed at. The desk places its own
        # target at exactly min_rr, and float error or rounding the prices to
        # the paisa/cent lands it a hair under — "R:R 1.50 below the 1.5:1
        # minimum" rejected the very target the desk had just set.
        if round(rr, 2) < min_rr:
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

        # How many underlying units one tradeable unit represents.
        #   India equity : 1        India option : exchange lot (e.g. 75)
        #   US equity    : 1        US option    : 100 (contract multiplier)
        is_option_leg = instrument.instrument_type in {InstrumentType.CALL,
                                                       InstrumentType.PUT}
        unit_size = max(instrument.lot_size, 1)
        if is_option_leg and not self.cfg.market.lot_based:
            unit_size = max(self.cfg.market.contract_multiplier, 1)

        quantity, lots = 0, 0
        if stop_points > 0:
            raw_qty = risk_amount / stop_points
            if unit_size > 1:
                lot_size = unit_size
                lots = int(math.floor(raw_qty / lot_size))
                quantity = lots * lot_size
                if lots < 1:
                    unit_word = "lot" if self.cfg.market.lot_based else "contract"
                    cur = self.cfg.market.currency_symbol
                    needed = stop_points * lot_size * 100.0 / max(risk_pct, 0.01)
                    reasons.append(
                        f"Position sizes to 0 {unit_word}s: {cur}{risk_amount:,.0f} "
                        f"of risk / {cur}{stop_points:,.2f} stop = {raw_qty:.1f} units, "
                        f"but one {unit_word} is {lot_size}. One {unit_word} needs "
                        f"about {cur}{needed:,.0f} of capital at {risk_pct:.1f}% risk — "
                        f"you have {cur}{self.state.capital:,.0f}.")
            else:
                quantity = int(math.floor(raw_qty))
                lots = quantity
                if quantity < 1:
                    # Say exactly what would have to change. "Too wide" alone
                    # sends people widening their risk instead of noticing the
                    # account is simply too small for this instrument.
                    cur = self.cfg.market.currency_symbol
                    needed = stop_points * 100.0 / max(risk_pct, 0.01)
                    reasons.append(
                        f"Position sizes to 0 shares: {cur}{risk_amount:,.2f} of risk / "
                        f"{cur}{stop_points:,.2f} stop = {raw_qty:.2f} shares. "
                        f"One share needs about {cur}{needed:,.0f} of capital at "
                        f"{risk_pct:.1f}% risk — you have {cur}{self.state.capital:,.0f}.")


        # ---- capital caps TRIM the size, they don't veto the idea ----------
        # A tight stop naturally implies a large notional. A real desk cuts the
        # size to fit its exposure limit rather than passing on the trade; it
        # only passes when even the minimum tradeable size won't fit.
        is_option = is_option_leg
        lot_size = unit_size

        caps: list[tuple[str, float]] = []
        max_exposure = self._max_exposure()
        caps.append(("exposure limit", max(max_exposure - self.state.exposure, 0.0)))
        # Each position gets at most its share of the exposure. Without this a
        # tight stop sized the first trade into nearly the whole limit — one
        # QQQ entry took $333k of $400k — and "five positions" meant two.
        slots = int(self.cfg.get("risk.max_open_positions", 3) or 1)
        if bool(self.cfg.get("risk.split_exposure_across_positions", True)) and slots > 1:
            caps.append(("per-position share of exposure", max_exposure / slots))
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
                    unit_word = ("lot" if (lot_size > 1 and self.cfg.market.lot_based)
                                 else "contract" if lot_size > 1 else "unit")
                    cur = self.cfg.market.currency_symbol
                    reasons.append(
                        f"Cannot fit even one {unit_word} within the {label} "
                        f"({cur}{budget:,.0f} available, "
                        f"{cur}{entry * lot_size:,.0f} needed)")
                    quantity = 0
                    break
                cap_note = (f"size trimmed from {quantity} to {max_qty} by the {label}")
                quantity = max_qty

        # ---- portfolio heat: total open risk, whatever the position count ----
        # Five positions at 1% each is 5% at risk the moment one headline moves
        # correlated names through their stops together. The heat cap bounds
        # the sum; a new trade takes what room is left, or waits.
        heat_pct = float(self.cfg.get("risk.max_portfolio_heat_pct", 0) or 0)
        if heat_pct > 0 and quantity > 0 and stop_points > 0:
            room = self.state.capital * heat_pct / 100.0 - self.state.open_risk
            fits = room / stop_points
            max_qty = (int(math.floor(fits / lot_size)) * lot_size
                       if lot_size > 1 else int(math.floor(fits)))
            if max_qty < quantity:
                cur = self.cfg.market.currency_symbol
                if max_qty <= 0:
                    reasons.append(
                        f"Portfolio heat cap: {cur}{self.state.open_risk:,.0f} already at "
                        f"risk across open positions, cap {heat_pct:g}% = "
                        f"{cur}{self.state.capital * heat_pct / 100:,.0f}")
                    quantity = 0
                else:
                    cap_note = (f"size trimmed from {quantity} to {max_qty} by the "
                                f"{heat_pct:g}% portfolio heat cap")
                    quantity = max_qty

        lots = quantity // lot_size if lot_size > 1 else quantity
        notional = quantity * entry
        if cap_note:
            log.info("%s: %s", ctx.symbol, cap_note)

        # ---- option premium richness ----
        if is_option:
            rich = self._iv_too_rich(ctx)
            if rich:
                reasons.append(rich)
            floor = int(self.cfg.get("derivatives.min_days_to_expiry", 3))
            try:
                expiry_day = datetime.fromisoformat(str(instrument.expiry)[:10]).date()
                dte = (expiry_day - clock.market_now(str(self.cfg.get(
                    "system.timezone", "Asia/Kolkata"))).date()).days
            except (TypeError, ValueError):
                dte = None
            if dte is not None and dte < floor:
                reasons.append(f"Option expires in {dte} day(s) — under the {floor}-day "
                               f"floor; intraday theta and an IV crush would eat it")

        # ---- finalise ----
        if quantity <= 0 and not any("size" in r or "Cannot fit" in r or "0 lots" in r
                                     or "0 shares" in r for r in reasons):
            reasons.append("Position sizes to zero after applying capital limits")

        signal.quantity = quantity
        signal.lots = lots
        signal.unit_size = unit_size
        signal.unit_label = (
            "lot" if (unit_size > 1 and self.cfg.market.lot_based)
            else "contract" if unit_size > 1
            else "share")
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
            tsym = self._option_tradingsymbol(
                meta.get("trading_symbol", ctx.symbol), expiry, strike, opt_type,
                market=self.cfg.active_market)
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
    def _index_is_untradeable(meta: dict[str, Any], instrument: Instrument) -> bool:
        """An index has no cash instrument.

        You cannot buy NIFTY or FINNIFTY at spot — only its futures or options.
        US index exposure goes through ETFs (SPY, QQQ), which ARE ordinary
        shares, so this only applies to a true index with no option leg chosen.
        """
        if not meta.get("is_index"):
            return False
        if meta.get("cash_tradeable"):
            return False          # an ETF: SPY/QQQ are ordinary shares
        return instrument.instrument_type == InstrumentType.EQUITY

    @staticmethod
    def _option_tradingsymbol(underlying: str, expiry: str, strike: float,
                              opt: str, market: str = "IN") -> str:
        """Each market names its option contracts differently.

        NSE  : NIFTY25JAN24500CE
        OCC  : SPY  260320C00585000   (root, YYMMDD, C/P, strike x1000 in 8 chars)
        """
        try:
            d = datetime.fromisoformat(expiry)
        except (TypeError, ValueError):
            return f"{underlying}{int(strike)}{opt}"

        if market.upper() == "US":
            cp = "C" if opt.upper() in {"CE", "C", "CALL"} else "P"
            strike_part = f"{int(round(strike * 1000)):08d}"
            return f"{underlying.upper():<6}{d:%y%m%d}{cp}{strike_part}".replace(" ", "")
        return f"{underlying}{d:%y%b}".upper() + f"{int(strike)}{opt}"

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
            # Sit a couple of ticks BEYOND the level, not on it. A stop resting
            # exactly at support is in the queue with everyone else's, which is
            # precisely where a sweep goes looking for liquidity.
            tick = float(instrument.tick_size or 0.05)
            ticks = int(self.cfg.get("risk.structural_stop_ticks", 2))
            offset = tick * ticks
            stop = (structural - offset if bias == Bias.BULLISH
                    else structural + offset)
            note = (f"structural stop {ticks} tick(s) beyond {structural:.2f} "
                    f"→ {stop:.2f}")
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
        self.state.open_risk += signal.total_risk

    def _iv_too_rich(self, ctx: MarketContext) -> str:
        """Is the option expensive enough that an IV drop would crush it?

        IV RANK: where today's ATM IV sits in this symbol's own range over the
        past year of samples. It needs history, so until
        `risk.iv_rank_min_samples` days are recorded the stand-in is IV
        against the stock's realised volatility: paying well above what the
        stock actually moves is buying the premium before it deflates.
        """
        iv = float((ctx.indicators or {}).get("derivatives", {}).get("atm_iv", 0.0) or 0)
        if iv <= 0:
            return ""
        cap = float(self.cfg.get("risk.reject_if_iv_rank_above", 80.0))
        need = int(self.cfg.get("risk.iv_rank_min_samples", 20))
        try:
            from app.storage import db
            history = db.iv_history(ctx.symbol)
        except Exception:
            history = []
        if len(history) >= need:
            low, high = min(history), max(history)
            rank = 100.0 * (iv - low) / (high - low) if high > low else 50.0
            if rank > cap:
                return (f"IV rank {rank:.0f} is above {cap:g} (ATM IV {iv:.1f}% vs a "
                        f"{low:.1f}-{high:.1f}% range over {len(history)} days) — "
                        f"premium too rich, an IV drop would crush it")
            return ""

        closes = [c.close for c in (ctx.candles or {}).get("1d", [])][-21:]
        if len(closes) >= 10:
            rets = [math.log(b / a) for a, b in zip(closes, closes[1:], strict=False) if a > 0 and b > 0]
            if len(rets) >= 5:
                mean = sum(rets) / len(rets)
                hv = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)) \
                    * math.sqrt(252) * 100
                ratio = float(self.cfg.get("risk.iv_over_realised_max", 1.5))
                if hv > 0 and iv > hv * ratio:
                    return (f"ATM IV {iv:.1f}% is {iv / hv:.1f}x the stock's realised "
                            f"{hv:.1f}% (limit {ratio:g}x) — premium too rich; IV rank "
                            f"takes over once {need} days of IV are recorded")
        return ""

    def restore_open(self, rows: list[dict[str, Any]]) -> int:
        """Count positions still open in the database after a restart.

        The desk restarts on every `git pull`. Starting from zero, it thought
        nothing was open while the outcome tracker was still managing trades,
        so the position limit, exposure and heat caps all had full room.
        """
        import json

        restored = 0
        for row in rows:
            try:
                signal = TradeSignal.model_validate(json.loads(row.get("payload") or "{}"))
            except Exception:
                continue
            self.state.open_positions += 1
            self.state.exposure += signal.notional
            self.state.open_risk += signal.total_risk
            restored += 1
        if restored:
            log.info("restored %d open position(s) from the database", restored)
        return restored

    def register_close(self, signal: TradeSignal, pnl: float) -> None:
        self.state.open_positions = max(0, self.state.open_positions - 1)
        self.state.realised_pnl += pnl
        self.state.exposure = max(0.0, self.state.exposure - signal.notional)
        self.state.open_risk = max(0.0, self.state.open_risk - signal.total_risk)
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

    def set_capital(self, capital: float) -> dict[str, Any]:
        """Change the account size the desk sizes against.

        Everything derived from capital — the per-trade risk budget, the daily
        loss limit, the exposure ceiling — is recomputed here. Refused while
        positions are open: those were sized against the OLD capital, so
        changing it underneath them would misstate how much is actually at risk.
        """
        if capital <= 0:
            return {"ok": False, "reason": "Capital must be greater than zero"}
        if self.state.open_positions > 0:
            return {
                "ok": False,
                "reason": (f"{self.state.open_positions} position(s) are open and "
                           f"were sized against the current capital. Close them "
                           f"before changing the account size."),
            }

        previous = self.state.capital
        self.state.capital = float(capital)
        self.state.daily_loss_limit = (
            float(capital) * float(self.cfg.get("risk.max_daily_loss_pct", 3.0)) / 100.0)
        # Keep the live config in step so a restart-free reload agrees.
        self.cfg.settings.setdefault("risk", {})["total_capital"] = float(capital)

        log.info("capital changed: %.2f → %.2f (risk/trade %.2f, daily limit %.2f)",
                 previous, capital,
                 capital * float(self.cfg.get("risk.risk_per_trade_pct", 1.0)) / 100,
                 self.state.daily_loss_limit)
        return {"ok": True, "previous": previous, "capital": float(capital),
                "snapshot": self.snapshot()}

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
            "currency": self.cfg.market.currency_symbol,
            "currency_code": self.cfg.market.currency_code,
            "market": self.cfg.active_market,
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
