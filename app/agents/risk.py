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

from app.agents import risk_manager as guard
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
        capital = float(self.cfg.get("risk.total_capital", 100_000))
        self.state = RiskState(capital=capital,
                               daily_loss_limit=guard.daily_loss_limit(self.cfg, capital))
        # Anti-stacking: a symbol that just closed a trade is blacklisted for
        # risk.reentry_cooldown_minutes, seeded from the database so a restart
        # does not clear it.
        self.cooldown = guard.CooldownBlacklist(
            float(self.cfg.get("risk.reentry_cooldown_minutes", 60) or 0),
            loader=self._last_exit)
        # Set each cycle by the engine from the news blackout rules; while it
        # holds, no new entry is allowed anywhere.
        self.blackout_reason: str = ""
        # Open P&L of each held position, by symbol — set by the outcome
        # tracker every poll. The "no averaging down" mandate reads it.
        self.position_pnl: dict[str, float] = {}
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
    def symbol_checks(self, symbol: str, order: str = "BASE_ENTRY") -> list[str]:
        """Reasons THIS symbol may not be traded now.

        `order` is BASE_ENTRY (a new position) or PYRAMID_ADD (a Standard
        Pyramid scale-in, +50% at +1R and +25% at +2R, to a position already
        held). "One position per symbol" and the re-entry cooldown stop BASE
        entries only; an add is still refused if the position is losing.

        A setup stays valid for several cycles, and nothing stopped the desk
        opening it again each time — LLY was held twice at once, and NFLX and
        AMZN were shorted, stopped, and shorted again half an hour later on
        the same idea.
        """
        from app.storage import db

        reasons: list[str] = []
        held = [r for r in db.open_signals() if r["symbol"] == symbol]
        # No averaging down: adding size to a position that is losing is
        # refused outright, whatever the per-symbol setting says.
        losing = self.position_pnl.get(symbol.upper())
        if held and losing is not None and losing < 0:
            cur = self.cfg.market.currency_symbol
            reasons.append(f"No averaging down: {symbol} is held in a drawdown "
                           f"({cur}{losing:,.2f} open) — size is never added to a loser")
        if order == "PYRAMID_ADD":
            return reasons
        if bool(self.cfg.get("risk.one_position_per_symbol", True)):
            if held:
                reasons.append(f"Already holding {symbol} — one position per symbol")
        # The configured minutes are read each time, so a live config change
        # (or a test) takes effect without rebuilding the blacklist.
        self.cooldown.minutes = float(self.cfg.get("risk.reentry_cooldown_minutes", 60) or 0)
        blocked = self.cooldown.reason(symbol)
        if blocked:
            reasons.append(blocked)
        return reasons

    # ------------------------------------------------------------------ #
    # Today's screened watchlist and the entry windows
    # ------------------------------------------------------------------ #
    def screener_checks(self, symbol: str, bias: Bias,
                        indicators: dict[str, Any],
                        composite_score: float = 0.0) -> list[str]:
        """Only today's screened names, only in their window:
        Band A 09:30-11:15, nothing 11:15-13:30, Band A/B VWAP pullbacks
        13:30-14:45 (IST; the US desk uses its own clock). A Band B name also
        trades only the side its close pointed to."""
        from app.analysis import pre_market_screener as scr
        if not bool(self.cfg.get("screener.enabled", False)):
            return []
        now = clock.market_now(str(self.cfg.get("system.timezone", "Asia/Kolkata")))
        entry = scr.todays(self.cfg, now.date())
        if entry is None:
            return ([f"No screened watchlist for today yet — the pre-market screener "
                     f"runs at {self.cfg.get('screener.run_at', '09:00')}; nothing is "
                     f"traded off-list"] if bool(self.cfg.get("screener.required", True))
                    else [])
        row = scr.band_of(entry, symbol)
        if row is None:
            listed = ", ".join(entry.get("symbols") or []) or "none today"
            return [f"{symbol} is not on today's screened watchlist ({listed})"]
        g = self.cfg.get
        spans = (f"{g('screener.windows.morning_from')}-{g('screener.windows.morning_to')}",
                 f"{g('screener.windows.morning_to')}-{g('screener.windows.afternoon_from')}",
                 f"{g('screener.windows.afternoon_from')}-{g('screener.windows.afternoon_to')}")
        where = scr.window(self.cfg, now)
        long = bias == Bias.BULLISH
        out: list[str] = []
        if where == "freeze":
            out.append(f"Midday freeze {spans[1]} — no new entries (the chop filter)")
        elif where == "closed":
            out.append(f"Outside the entry windows ({spans[0]} Band A; {spans[2]} "
                       f"Band A/B VWAP pullbacks)")
        elif where == "morning" and row["band"] != "A" and not self._band_b_promoted(
                bias, indicators, composite_score):
            out.append(f"{symbol} is Band B — Band B enters only in the afternoon "
                       f"VWAP-pullback window ({spans[2]}), unless it is trending "
                       f"its way with a composite of ±"
                       f"{g('screener.band_b_promotion.min_composite', 0.85)} or more "
                       f"(now {composite_score:+.2f}, "
                       f"{((indicators or {}).get('primary') or {}).get('regime', 'unknown')})")
        elif where == "afternoon" and not scr.is_vwap_pullback(
                indicators, long, float(g("screener.vwap_pullback_atr", 0.5))):
            out.append(f"Afternoon window ({spans[2]}) takes VWAP pullbacks only — "
                       f"{symbol} is not on the trend side of VWAP within "
                       f"{g('screener.vwap_pullback_atr', 0.5)} ATR of it")
        side = row.get("side", "BOTH")
        if (side == "LONG" and not long) or (side == "SHORT" and long):
            out.append(f"{symbol} is Band B {side.lower()}s-only (it closed at "
                       f"{row.get('close_location', 0):.0%} of yesterday's range)")
        return out

    # ------------------------------------------------------------------ #
    # General setups: the guards from the US week of 28 Sept 2026
    # ------------------------------------------------------------------ #
    def general_checks(self, bias: Bias, reports: list[AgentReport],
                       indicators: dict[str, Any]) -> list[str]:
        """Reasons a GENERAL setup (no named strategy) is refused; each guard
        is off until its setting is set (per market):

          consensus.general_min_analysts   at least N analysts voting the
                                           trade's way at +/-general_agree_min
          risk.rsi_guard                   no short below short_min RSI, no
                                           long above long_max
          screener.windows.general_to      no new general entry from this
                                           time (market clock)
        """
        g = self.cfg.get
        out: list[str] = []
        sign = 1 if bias == Bias.BULLISH else -1
        need = int(g("consensus.general_min_analysts", 0) or 0)
        if need > 1:
            floor = float(g("consensus.general_agree_min", 0.25))
            agree = sorted(r.agent_id for r in reports
                           if getattr(r, "data_available", True) and r.score * sign >= floor)
            if len(agree) < need:
                out.append(f"General setup needs {need} analysts at "
                           f"{'+' if sign > 0 else '-'}{floor:g} or stronger its way; "
                           f"{len(agree)} ({', '.join(agree) or 'none'}) — one analyst "
                           f"never trades alone without a named strategy")
        guard_cfg = g("risk.rsi_guard") or {}
        if guard_cfg.get("enabled", False):
            primary = (indicators or {}).get("primary") or {}
            rsi = primary.get("rsi")
            if rsi is not None:
                rsi = float(rsi)
                low, high = float(guard_cfg.get("short_min", 25)), float(
                    guard_cfg.get("long_max", 75))
                if sign < 0 and rsi < low:
                    out.append(f"RSI {rsi:.0f} is below {low:g} — no short into a "
                               f"stretched, oversold move (risk.rsi_guard)")
                elif sign > 0 and rsi > high:
                    out.append(f"RSI {rsi:.0f} is above {high:g} — no long into a "
                               f"stretched, overbought move (risk.rsi_guard)")
        cutoff = g("screener.windows.general_to")
        if cutoff and clock.past(str(g("system.timezone", "Asia/Kolkata")), str(cutoff)):
            out.append(f"Past {cutoff} — general setups take no new entries this late "
                       f"(screener.windows.general_to); the named strategies keep "
                       f"their own windows")
        return out

    # ------------------------------------------------------------------ #
    # The daily lockout
    # ------------------------------------------------------------------ #
    def lock(self, reason: str) -> None:
        """Halt for the rest of this market's calendar day — saved, so a
        restart (every git pull) does not clear it."""
        first = not self.state.halted
        self.state.halted = True
        self.state.halt_reason = reason
        if first:
            log.warning("DESK LOCKED OUT for the day — %s", reason)
        try:
            from app.storage import db
            db.save_lockout(self._day.isoformat(), str(self.cfg.active_market), reason)
        except Exception as exc:                          # noqa: BLE001 - never fatal
            log.warning("could not save the lockout: %s", exc)

    def restore_day(self) -> dict[str, Any]:
        """After a restart: today's trade count, realised P&L and any lockout
        for the ACTIVE market, from the database. Starting from zero let a
        restart hand the desk a fresh daily loss budget and trade count."""
        from zoneinfo import ZoneInfo

        from app.storage import db
        self.roll_day_if_needed()
        tz = ZoneInfo(str(self.cfg.get("system.timezone", "Asia/Kolkata")))
        u = self.cfg.universe or {}
        mine = ({i["symbol"] for i in (u.get("indices") or [])}
                | {i["symbol"] for i in (u.get("stocks") or [])})
        trades = wins = losses = 0
        realised = 0.0
        for r in db.recent_signals(limit=2000):
            if r.get("status") == "REJECTED" or (mine and r["symbol"] not in mine):
                continue
            try:
                ts = datetime.fromisoformat(str(r["ts"]).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                continue
            if ts.tzinfo is None:
                from datetime import UTC
                ts = ts.replace(tzinfo=UTC)
            if ts.astimezone(tz).date() != self._day:
                continue
            trades += 1
            if r.get("pnl") is not None:
                realised += float(r["pnl"])
                wins += 1 if float(r["pnl"]) >= 0 else 0
                losses += 1 if float(r["pnl"]) < 0 else 0
        self.state.trades_today = max(self.state.trades_today, trades)
        self.state.realised_pnl = realised
        self.state.wins_today, self.state.losses_today = wins, losses
        saved = db.lockout(self._day.isoformat(), str(self.cfg.active_market))
        if saved:
            self.state.halted, self.state.halt_reason = True, saved.get("reason") or "locked"
        elif guard.breaker_tripped(self.state.daily_pnl, self.state.daily_loss_limit):
            self.lock(f"Daily loss limit breached ({self.state.daily_pnl:,.0f})")
        return {"trades_today": trades, "realised_pnl": round(realised, 2),
                "locked": self.state.halted}

    def _band_b_promoted(self, bias: Bias, indicators: dict[str, Any],
                         composite_score: float) -> bool:
        """Dynamic Band B promotion: a Band B name may enter in the MORNING
        window when its regime trends its way (trending_up for a long,
        trending_down for a short) and the composite is at least
        screener.band_b_promotion.min_composite (0.85) that way."""
        g = self.cfg.get
        if not bool(g("screener.band_b_promotion.enabled", False)):
            return False
        regime = str(((indicators or {}).get("primary") or {}).get("regime") or "")
        floor = float(g("screener.band_b_promotion.min_composite", 0.85))
        if bias == Bias.BULLISH:
            return regime == "trending_up" and composite_score >= floor
        if bias == Bias.BEARISH:
            return regime == "trending_down" and composite_score <= -floor
        return False

    def approve_add(self, symbol: str, open_pnl: float, add_notional: float,
                    add_risk: float = 0.0) -> list[str]:
        """Reasons a pyramid ADD to a held position is refused ([] = approved).

        The mandate first: never add to a position in a drawdown. Then the
        desk's own limits still hold — a halted desk, the entry cutoff and
        the exposure ceiling apply to an add exactly as to a new trade.
        """
        self.roll_day_if_needed()
        reasons: list[str] = []
        cur = self.cfg.market.currency_symbol
        # The per-symbol rules for an ADD: "one position per symbol" does not
        # apply (the position is the point); no averaging down does.
        self.position_pnl[symbol.upper()] = open_pnl
        reasons.extend(r for r in self.symbol_checks(symbol, order="PYRAMID_ADD")
                       if not r.startswith("No averaging down"))
        if open_pnl < 0:
            reasons.append(f"No averaging down: {symbol} is in a drawdown "
                           f"({cur}{open_pnl:,.2f} open) — the add is refused")
        if self.state.halted:
            reasons.append(f"Desk halted: {self.state.halt_reason}")
        if self._past_entry_cutoff():
            reasons.append(f"Past the no-new-entry cutoff "
                           f"({self.cfg.get('system.no_new_entry_after', '15:00')})")
        room = self._max_exposure() - self.state.exposure
        if add_notional > room:
            reasons.append(f"Exposure cap: the add needs {cur}{add_notional:,.0f}, "
                           f"{cur}{max(room, 0):,.0f} of room left")
        return reasons

    def register_add(self, add_notional: float, risk_change: float) -> None:
        """Book a pyramid add: more exposure; the open risk follows the stop."""
        self.state.exposure += add_notional
        self.state.open_risk = max(0.0, self.state.open_risk + risk_change)

    @staticmethod
    def _last_exit(symbol: str) -> str | None:
        from app.storage import db
        return db.last_exit(symbol)

    def desk_checks(self) -> list[str]:
        """Reasons the desk is closed for new business, regardless of the setup."""
        self.roll_day_if_needed()
        reasons: list[str] = []

        if self.state.halted:
            reasons.append(f"Desk halted: {self.state.halt_reason}")

        if self.state.daily_pnl <= -self.state.daily_loss_limit:
            self.lock(f"Daily loss limit hit ({self.state.daily_pnl:,.0f} vs limit "
                      f"-{self.state.daily_loss_limit:,.0f})")
            reasons.append(self.state.halt_reason)

        max_daily = int(self.cfg.get("risk.max_daily_trades", 0) or 0)
        if max_daily and self.state.trades_today >= max_daily:
            reasons.append(f"Daily trade limit: {self.state.trades_today} of {max_daily} "
                           f"taken today — no more entries until tomorrow")

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

        sweep = self._sweep_report(reports or ctx.__dict__.get("_reports") or [], bias)
        if sweep is not None:
            stop_loss, target, sl_note, ustop = self._sweep_levels(
                ctx, bias, entry, instrument, source_report, sweep)
        else:
            stop_loss, target, sl_note, ustop = self._levels(ctx, bias, entry, instrument,
                                                             source_report)

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
            underlying_stop=round(ustop.level, 4) if ustop else None,
            underlying_stop_note=ustop.note if ustop else "",
            bias=bias, composite_score=round(composite_score, 3),
            confirmations=confirmations, rationale=rationale,
            counter_argument=counter_argument, reports=reports, regime=ctx.regime,
            setup=(sweep.extra or {}).get("setup", "") if sweep is not None else "",
        )

        # ---- the gold desk: an instrument with swing: true ----
        # Only the 1-4 day volatility breakout trades it (its evidence), the
        # morning screener does not apply (gold's ~1% daily range would fail
        # its 2% ATR floor every day), and the position is held overnight.
        swing = bool(self.cfg.instrument_meta(ctx.symbol).get("swing", False))
        if swing:
            from app.strategies.vol_breakout import SETUP_NAME as BREAKOUT
            signal.hold_overnight = True
            if signal.setup != BREAKOUT:
                reasons.append(f"{ctx.symbol} is on the gold desk: only the 1-4 day "
                               f"volatility breakout trades it")

        # ---- desk-level gates ----
        reasons.extend(self.desk_checks())
        reasons.extend(self.symbol_checks(ctx.symbol))
        if not swing:
            reasons.extend(self.screener_checks(ctx.symbol, bias, ctx.indicators or {},
                                                composite_score))
        # A general setup (no named strategy) answers to the weekly review's
        # guards; PD sweep, the volatility breakout and SJK 50-200 carry their own.
        if not swing and not signal.setup:
            reasons.extend(self.general_checks(
                bias, reports or ctx.__dict__.get("_reports") or [], ctx.indicators or {}))

        # ---- can this instrument actually be bought? ----
        if self._index_is_untradeable(self.cfg.instrument_meta(ctx.symbol), instrument):
            reasons.append(
                f"{ctx.symbol} is an index — there is no cash instrument to buy. "
                f"Trade it through an option or future; no option leg was "
                f"available this cycle (the derivatives analyst had no chain).")

        # ---- liquidity: the option's bid-ask spread ----
        leg = ((source_report.extra.get("suggested_leg") or {}) if source_report else {})
        if instrument.instrument_type in {InstrumentType.CALL, InstrumentType.PUT}:
            wide = guard.spread_rejection(self.cfg, leg.get("bid"), leg.get("ask"),
                                          instrument.tradingsymbol)
            if wide:
                reasons.append(wide)

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
            # A sweep's stop is hard-coded 2 ticks beyond the wick: the wick IS
            # the invalidation, so the "too tight" floor does not apply to it.
            if stop_pct < min_pct and sweep is None:
                reasons.append(f"Stop {stop_pct:.2f}% is too tight (min {min_pct}%) — noise will hit it")
            elif stop_pct > max_pct:
                reasons.append(f"Stop {stop_pct:.2f}% is too wide (max {max_pct}%) — invalidation ill-defined")

        # ---- previous-day F&O confluence (reversal trades) and the 1:3 road ----
        from app.agents import fno_confluence
        why = fno_confluence.confluence_reason(ctx, bias, reports, self.cfg)
        if why:
            reasons.append(f"F&O confluence: {why}")
        spot = ctx.quote.last_price if ctx.quote else entry
        u_entry, u_stop = ((spot, ustop.level) if ustop and instrument.instrument_type in
                           {InstrumentType.CALL, InstrumentType.PUT} else (entry, stop_loss))
        # A sweep's target is defined by the setup (VWAP or 3R): the far side
        # of yesterday's range is where a failed breakout rotates to, not a
        # wall, so the room check does not apply to it.
        snapped_r = None
        if sweep is None:
            tick = float(self.cfg.instrument_meta(ctx.symbol).get("tick_size", 0) or 0) or (
                0.05 if self.cfg.market.lot_based else 0.01)
            why, snapped, room = fno_confluence.room_snap(ctx, bias, u_entry, u_stop,
                                                          self.cfg, tick)
            if why:
                reasons.append(f"Reward:risk — {why}")
            elif snapped is not None:
                # 2.2-3R of room to the PDH/PDL: take the trade with the
                # target snapped just inside the level instead of refusing it.
                snapped_r = room
                if instrument.instrument_type in {InstrumentType.CALL, InstrumentType.PUT}:
                    target = entry + (entry - stop_loss) * room
                else:
                    target = snapped
                level = ((ctx.indicators or {}).get("previous_day") or {}).get(
                    "high" if bias == Bias.BULLISH else "low")
                signal.target = round(target, 2)
                signal.confirmations = list(signal.confirmations) + [
                    f"target snapped {int(self.cfg.get('risk.target_snap.ticks', 2))} "
                    f"ticks inside the previous-day {'high' if bias == Bias.BULLISH else 'low'} "
                    f"{level:,.2f} → {snapped:,.2f} ({room:.2f}R)"]

        # ---- risk:reward ----
        reward_points = abs(target - entry)
        rr = reward_points / stop_points if stop_points > 0 else 0.0
        min_rr = float(self.cfg.get("risk.min_risk_reward", 2.0))
        # The user's own-plan strategies are judged at their own reward:risk
        # (sjk50_200.rr 1:2.5, sjk_9_15_21.rr 1:2), not 1:3.
        from app.strategies import OWN_PLAN
        if signal.setup in OWN_PLAN:
            section, default_rr = OWN_PLAN[signal.setup]
            min_rr = float(self.cfg.get(f"{section}.rr", default_rr))
        if snapped_r is not None:
            min_rr = min(min_rr, float(self.cfg.get("risk.target_snap.min_r", 2.2)))
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
        min_size_note = ""
        if stop_points > 0:
            raw_qty = risk_amount / stop_points
            if unit_size > 1:
                lot_size = unit_size
                lots = int(math.floor(raw_qty / lot_size))
                quantity = lots * lot_size
                # One contract is the smallest position there is. When it risks
                # more than the per-trade budget but still no more than the
                # hard ceiling (risk.max_risk_per_trade_pct), take exactly one:
                # on a $4,000 account a 1% budget is $40, and a single SPY
                # contract with a structural stop routinely risks $50-$80.
                ceiling = self.state.capital * float(
                    self.cfg.get("risk.max_risk_per_trade_pct", 2.0)) / 100.0
                if lots < 1 and stop_points * lot_size <= ceiling:
                    lots, quantity = 1, lot_size
                    cur = self.cfg.market.currency_symbol
                    min_size_note = (
                        f"one {'lot' if self.cfg.market.lot_based else 'contract'} risks "
                        f"{cur}{stop_points * lot_size:,.2f} — over the "
                        f"{cur}{risk_amount:,.0f} budget, within the "
                        f"{cur}{ceiling:,.0f} hard ceiling; taking the minimum size")
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
            # 20% of capital per trade, flexed to 25% on SPY/QQQ/DIA, whose
            # near-the-money contracts cost $900-$1,000 on their own.
            cap_pct = guard.deployment_cap_pct(self.cfg, ctx.symbol)
            caps.append((f"{cap_pct:g}% capital deployment cap"
                         + (" (index flex)" if ctx.symbol.upper() in guard.index_symbols(self.cfg)
                            and cap_pct > float(self.cfg.get("risk.max_capital_deployed_pct", 20.0))
                            else ""),
                         guard.deployment_cap(self.cfg, self.state.capital, ctx.symbol)))

        cap_note = min_size_note
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
                ) -> tuple[float, float, str, guard.UnderlyingStop]:
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

        # The structural level: a volume-profile setup in this direction names
        # its own (beyond the poke, the test or the POC); otherwise the chart's.
        structural = None
        reports = ctx.__dict__.get("_reports") or []
        want = 1 if bias == Bias.BULLISH else -1
        for agent in ("volume_profile", "candlestick"):
            for report in reports:
                if report.agent_id == agent and report.invalidation_level and (
                        agent != "volume_profile" or report.score * want > 0):
                    structural = report.invalidation_level
                    break
            if structural is not None:
                break

        # --- options: the stop lives on the UNDERLYING ---------------------
        # Never a percentage of the premium: an IV wobble or a wide quote says
        # nothing about whether the idea was wrong. The stop is the stock's
        # 5m structure (swing ± 2 ticks, or 1.5x ATR), and the premium stop
        # recorded here is simply what the option marks at when the stock
        # gets there — so R, sizing and the exit all describe the same event.
        if is_option:
            spot = ctx.quote.last_price if ctx.quote else 0.0
            leg = (source.extra.get("suggested_leg") or {}) if source else {}
            bullish = bias == Bias.BULLISH
            meta = self.cfg.instrument_meta(ctx.symbol)
            ustop = guard.underlying_stop(
                self.cfg, spot=spot, bullish=bullish, atr=atr,
                candles_5m=(ctx.candles or {}).get("5m"),
                tick=float(meta.get("tick_size", 0.01) or 0.01),
                named_level=structural)
            is_call = instrument.instrument_type == InstrumentType.CALL
            stop = guard.premium_at_stop(entry, leg.get("delta"), spot, ustop.level, is_call)
            if stop >= entry:
                stop = entry * 0.5
            target = entry + (entry - stop) * min_rr
            return stop, target, (f"underlying stop: {ustop.note} (option marks "
                                  f"{stop:.2f} there), target at {min_rr}R"), ustop

        # --- cash/underlying ---
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

        # Never inside the market's own noise. A structural level two ticks
        # beyond a 5-minute candle can be 0.3% away (NFLX 71.03 -> 71.26) —
        # less than a normal bar's range — and gets hit by nothing but the
        # tape wiggling, twice in a morning. The stop goes at least
        # risk.min_stop_atr x ATR away; the size shrinks to keep the risk.
        floor, what = guard.stop_floor(self.cfg, entry, atr)
        if floor > 0 and abs(entry - stop) < floor:
            stop = entry - floor if bias == Bias.BULLISH else entry + floor
            note += f", widened to {what} — the level sat inside normal noise"

        stop_points = abs(entry - stop)
        target = (entry + stop_points * min_rr if bias == Bias.BULLISH
                  else entry - stop_points * min_rr)
        return stop, target, f"{note}, target at {min_rr}R", guard.UnderlyingStop(
            round(stop, 4), "structure" if structural else "atr", note)

    @staticmethod
    def _sweep_report(reports: list[AgentReport], bias: Bias) -> AgentReport | None:
        """The candlestick analyst's confirmed PD Liquidity Sweep in this
        trade's direction, if that is what this trade is."""
        from app.strategies import OWN_PLAN
        from app.strategies.pd_sweep import SETUP_NAME
        from app.strategies.vol_breakout import SETUP_NAME as BREAKOUT
        want = 1 if bias == Bias.BULLISH else -1
        for r in reports or []:
            if (r.agent_id == "candlestick"
                    and (r.extra or {}).get("setup") in (SETUP_NAME, BREAKOUT, *OWN_PLAN)
                    and r.score * want > 0 and r.invalidation_level):
                return r
        return None

    def _sweep_levels(self, ctx: MarketContext, bias: Bias, entry: float,
                      instrument: Instrument, source: AgentReport | None,
                      sweep: AgentReport) -> tuple[float, float, str, guard.UnderlyingStop]:
        """A sweep's stop and target, on the UNDERLYING:

          stop    exactly pd_sweep.stop_ticks (2) ticks beyond the sweep
                  candle's wick — no ATR widening, no swing substitute;
          target  the day's VWAP when it is at least pd_sweep.min_reward_risk
                  (3R) away, otherwise 3R.
        For an option the premium stop/target are what it marks at there.
        """
        meta = self.cfg.instrument_meta(ctx.symbol)
        # The listed tick, else the market's: ₹0.05 on NSE, $0.01 in the US.
        tick = float(meta.get("tick_size")
                     or (0.01 if str(getattr(self.cfg, "active_market", "IN")).upper() == "US"
                         else 0.05))
        from app.strategies import OWN_PLAN
        from app.strategies.vol_breakout import SETUP_NAME as BREAKOUT
        setup_name = (sweep.extra or {}).get("setup")
        own = OWN_PLAN.get(setup_name or "")
        breakout = setup_name == BREAKOUT or own is not None   # no VWAP target either
        if own:
            # SJK 50-200 / SJK 9-15-21: the stop AT the strategy's own level (the
            # pullback's swing; the swing or the 21 EMA) — <section>.stop_ticks
            # beyond it, 0 by default — and the target at its own 1:rr.
            section, default_rr = own
            ticks = int(self.cfg.get(f"{section}.stop_ticks", 0))
            need = float(self.cfg.get(f"{section}.rr", default_rr))
        elif breakout:
            # The volatility breakout: the same exact-level stop, beyond
            # today's open instead of a wick, and the desk's 3R target.
            ticks = int(self.cfg.get("vol_breakout.stop_ticks", 2))
            need = float(self.cfg.get("risk.min_risk_reward", 3.0))
        else:
            ticks = int(self.cfg.get("pd_sweep.stop_ticks", 2))
            need = float(self.cfg.get("pd_sweep.min_reward_risk", 3.0))
        long = bias == Bias.BULLISH
        wick = float(sweep.invalidation_level)
        u_stop = round(wick - ticks * tick if long else wick + ticks * tick, 4)
        is_option = instrument.instrument_type in {InstrumentType.CALL, InstrumentType.PUT}
        spot = (ctx.quote.last_price if ctx.quote else 0.0) if is_option else entry
        u_risk = abs(spot - u_stop)
        if u_risk <= 0 or (long and u_stop >= spot) or (not long and u_stop <= spot):
            # Price already back through the wick: not a trade; the stop-sanity
            # checks downstream refuse it with this note.
            return (entry, entry, f"sweep wick {wick:.2f} already breached",
                    guard.UnderlyingStop(u_stop, "sweep", "wick breached"))
        sign = 1.0 if long else -1.0
        three_r = spot + sign * need * u_risk
        vwap = 0.0 if breakout else float((sweep.extra or {}).get("vwap") or 0.0)
        vwap_r = (vwap - spot) * sign / u_risk if vwap else 0.0
        if vwap_r >= need:
            u_target, how = vwap, f"VWAP {vwap:.2f} ({vwap_r:.1f}R)"
        else:
            u_target, how = three_r, (f"{need:g}R ({three_r:.2f}; VWAP {vwap:.2f} is only "
                                      f"{max(vwap_r, 0):.1f}R)" if vwap else f"{need:g}R")
        rr = abs(u_target - spot) / u_risk
        note = ((f"{setup_name.split(' · ')[0]} stop at its own level {wick:.2f} → "
                 f"{u_stop:.2f}; target {how}") if own else
                (f"breakout stop {ticks} tick(s) beyond today's open {wick:.2f} → "
                 f"{u_stop:.2f}; target {how}") if breakout else
                (f"sweep stop {ticks} tick(s) beyond the wick {wick:.2f} → {u_stop:.2f}; "
                 f"target {how}; no time stop"))
        ustop = guard.UnderlyingStop(u_stop, "sweep", note)
        if is_option:
            leg = (source.extra.get("suggested_leg") or {}) if source else {}
            is_call = instrument.instrument_type == InstrumentType.CALL
            stop = guard.premium_at_stop(entry, leg.get("delta"), spot, u_stop, is_call)
            if stop >= entry:
                stop = entry * 0.5
            target = entry + (entry - stop) * rr
            return stop, target, note + f" (option marks {stop:.2f} at the stop)", ustop
        return u_stop, u_target, note, ustop

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

    def register_close(self, signal: TradeSignal, pnl: float,
                       closed_at: datetime | None = None) -> None:
        self.cooldown.add(signal.instrument.symbol, closed_at)
        self.state.open_positions = max(0, self.state.open_positions - 1)
        self.state.realised_pnl += pnl
        self.state.exposure = max(0.0, self.state.exposure - signal.notional)
        self.state.open_risk = max(0.0, self.state.open_risk - signal.total_risk)
        if pnl >= 0:
            self.state.wins_today += 1
        else:
            self.state.losses_today += 1
        if guard.breaker_tripped(self.state.daily_pnl, self.state.daily_loss_limit):
            self.lock(f"Daily loss limit breached ({self.state.daily_pnl:,.0f})")

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
        self.state.daily_loss_limit = guard.daily_loss_limit(self.cfg, float(capital))
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
            "cooldown_minutes": self.cooldown.minutes,
            "cooldown": self.cooldown.active(),
            "deployment_cap": guard.deployment_cap(self.cfg, self.state.capital, "_"),
            "index_deployment_cap": guard.deployment_cap(self.cfg, self.state.capital, "SPY"),
            "index_symbols": sorted(guard.index_symbols(self.cfg)),
            "max_spread_pct": float(self.cfg.get("risk.max_spread_pct_of_mid", 7.0)),
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
