"""Candlestick & Technical Analyst — multi-timeframe price action."""
from __future__ import annotations

from app.agents.base import BaseAgent
from app.core.models import AgentReport, Evidence, MarketContext
from app.core.registry import register_agent


@register_agent("candlestick")
class CandlestickAgent(BaseAgent):
    agent_id = "candlestick"

    def analyse_rules(self, ctx: MarketContext) -> AgentReport:
        ind = ctx.indicators or {}
        primary = ind.get("primary") or {}
        if not primary:
            return AgentReport(agent_id=self.agent_id, symbol=ctx.symbol,
                               data_available=False,
                               rationale="No candle data available for this symbol.")

        # --- The Previous Day Liquidity Sweep, confirmed by its reversal
        # candle, is a complete setup on its own: scored 1.0 (a liquidity trap
        # is the highest-probability read this analyst makes).
        sweep = ind.get("pd_sweep") or {}
        if sweep.get("confirmed") and bool(self.cfg.get("pd_sweep.enabled", True)):
            return self._sweep_report(ctx, primary, sweep)
        # --- Larry Williams' volatility breakout: a complete setup too.
        breakout = ind.get("vol_breakout") or {}
        if breakout and bool(self.cfg.get("vol_breakout.enabled", False)):
            return self._breakout_report(ctx, primary, breakout)

        score = 0.0
        evidence: list[Evidence] = []
        price = primary.get("last_close", 0.0)
        if sweep:
            evidence.append(Evidence(label="PD sweep (not approved)",
                                     value=sweep.get("note", ""), weight=0.0))

        # --- 1. EMA structure -------------------------------------------------
        if primary.get("ema_stacked_bull"):
            score += 0.25
            evidence.append(Evidence(label="EMA stack", value="9 > 21 > 50 — bullish alignment", weight=0.25))
        elif primary.get("ema_stacked_bear"):
            score -= 0.25
            evidence.append(Evidence(label="EMA stack", value="9 < 21 < 50 — bearish alignment", weight=0.25))

        # --- 2. VWAP ----------------------------------------------------------
        vwap = primary.get("vwap", 0.0)
        if vwap:
            if primary.get("above_vwap"):
                score += 0.15
                evidence.append(Evidence(label="VWAP", value=f"Price {price:.2f} above VWAP {vwap:.2f}", weight=0.15))
            else:
                score -= 0.15
                evidence.append(Evidence(label="VWAP", value=f"Price {price:.2f} below VWAP {vwap:.2f}", weight=0.15))

        # --- 3. Patterns ------------------------------------------------------
        patterns = primary.get("patterns") or []
        for pat in patterns:
            contribution = pat["direction"] * pat["strength"] * 0.3
            score += contribution
            if pat["direction"] != 0:
                evidence.append(Evidence(label=pat["name"], value=pat["note"],
                                         weight=round(abs(contribution), 3)))

        # --- 4. Multi-timeframe agreement ------------------------------------
        mtf = ind.get("mtf_alignment") or {}
        if mtf.get("aligned"):
            score += 0.2 * mtf.get("direction", 0)
            evidence.append(Evidence(
                label="MTF alignment",
                value=f"{mtf.get('bull_timeframes',0)} bullish vs "
                      f"{mtf.get('bear_timeframes',0)} bearish timeframes — aligned",
                weight=0.2))
        elif mtf.get("bull_timeframes") and mtf.get("bear_timeframes"):
            score *= 0.7
            evidence.append(Evidence(label="MTF conflict",
                                     value="Timeframes disagree — conviction reduced", weight=0.0))

        # --- 5. Volume --------------------------------------------------------
        surge = primary.get("volume_surge", 1.0)
        vol_mult = self.cfg.get("technical.volume_surge_multiplier", 1.8)
        if surge >= vol_mult:
            score *= 1.15
            evidence.append(Evidence(label="Volume", value=f"{surge:.1f}x average — participation confirms", weight=0.1))
        elif surge < 0.7:
            score *= 0.75
            evidence.append(Evidence(label="Volume", value=f"Only {surge:.1f}x average — thin, unreliable", weight=0.0))

        # --- 6. RSI extremes (fade, don't chase) ------------------------------
        rsi = primary.get("rsi", 50.0)
        if rsi > 78:
            score -= 0.12
            evidence.append(Evidence(label="RSI", value=f"{rsi:.0f} — overbought, poor entry for a fresh long", weight=0.12))
        elif rsi < 22:
            score += 0.12
            evidence.append(Evidence(label="RSI", value=f"{rsi:.0f} — oversold, poor entry for a fresh short", weight=0.12))

        score = max(-1.0, min(1.0, score))

        # Invalidation = the structural swing level, not a round number.
        swing_high = primary.get("high", price)
        swing_low = primary.get("low", price)
        atr = primary.get("atr", 0.0)
        if score > 0:
            invalidation = min(swing_low, price - 1.5 * atr) if atr else swing_low
            target = price + 2.5 * atr if atr else swing_high
        elif score < 0:
            invalidation = max(swing_high, price + 1.5 * atr) if atr else swing_high
            target = price - 2.5 * atr if atr else swing_low
        else:
            invalidation, target = None, None

        confidence = min(1.0, 0.25 + 0.12 * len(evidence) + abs(score) * 0.35)
        return AgentReport(
            agent_id=self.agent_id, symbol=ctx.symbol,
            bias=self._bias_from_score(score), score=round(score, 3),
            confidence=round(confidence, 2),
            rationale=self._summarise(primary, patterns, mtf, score),
            evidence=evidence,
            invalidation_level=round(invalidation, 2) if invalidation else None,
            suggested_entry=price,
            suggested_target=round(target, 2) if target else None,
            extra={"rsi": rsi, "atr": atr, "regime": primary.get("regime"),
                   "volume_surge": surge, "patterns": [p["name"] for p in patterns]},
        )

    def _sweep_report(self, ctx: MarketContext, primary: dict, sweep: dict) -> AgentReport:
        from app.strategies.pd_sweep import SETUP_NAME
        direction = int(sweep.get("direction") or 0)
        score = float(self.cfg.get("pd_sweep.score", 1.0)) * direction
        price = float(primary.get("last_close") or sweep.get("close") or 0.0)
        vwap = float(primary.get("vwap") or 0.0)
        pattern = str(sweep.get("pattern", "")).replace("_", " ")
        side = "high (PDH)" if sweep.get("side") == "PDH" else "low (PDL)"
        return AgentReport(
            agent_id=self.agent_id, symbol=ctx.symbol,
            bias=self._bias_from_score(score), score=round(score, 3),
            confidence=0.95,
            rationale=(f"{SETUP_NAME}: {sweep.get('note', '')}. Trapped "
                       f"{'buyers' if direction < 0 else 'sellers'} above/below the "
                       f"previous-day {side} fuel the move back — target VWAP "
                       f"{vwap:,.2f} or 3R."),
            evidence=[Evidence(label=SETUP_NAME, value=sweep.get("note", ""), weight=1.0),
                      Evidence(label=pattern.title() or "Reversal",
                               value=f"on the {sweep.get('timeframe')} close", weight=0.0)],
            # The risk desk places the stop exactly 2 ticks beyond this wick.
            invalidation_level=round(float(sweep.get("wick") or 0.0), 4),
            suggested_entry=price,
            suggested_target=round(vwap, 2) if vwap else None,
            extra={"setup": SETUP_NAME, "sweep": sweep, "patterns": [sweep.get("pattern")],
                   "atr": primary.get("atr", 0.0), "vwap": vwap},
        )

    def _breakout_report(self, ctx: MarketContext, primary: dict, breakout: dict) -> AgentReport:
        from app.strategies.vol_breakout import SETUP_NAME
        direction = int(breakout.get("direction") or 0)
        score = float(self.cfg.get("vol_breakout.score", 0.9)) * direction
        price = float(primary.get("last_close") or breakout.get("close") or 0.0)
        return AgentReport(
            agent_id=self.agent_id, symbol=ctx.symbol,
            bias=self._bias_from_score(score), score=round(score, 3), confidence=0.9,
            rationale=(f"{SETUP_NAME}: {breakout.get('note', '')}. The day's range "
                       f"expansion is under way — wrong if back at today's open "
                       f"{float(breakout.get('day_open') or 0):,.2f}; target 3R."),
            evidence=[Evidence(label=SETUP_NAME, value=breakout.get("note", ""), weight=1.0)],
            # The risk desk places the stop exactly 2 ticks beyond today's open.
            invalidation_level=round(float(breakout.get("day_open") or 0.0), 4),
            suggested_entry=price,
            extra={"setup": SETUP_NAME, "breakout": breakout,
                   "patterns": ["volatility_breakout"],
                   "atr": primary.get("atr", 0.0), "vwap": primary.get("vwap", 0.0)},
        )

    @staticmethod
    def _summarise(primary: dict, patterns: list, mtf: dict, score: float) -> str:
        bits = [f"{primary.get('regime', 'unknown')} regime"]
        if patterns:
            bits.append("patterns: " + ", ".join(p["name"] for p in patterns[:3]))
        bits.append(f"RSI {primary.get('rsi', 50):.0f}")
        bits.append("above VWAP" if primary.get("above_vwap") else "below VWAP")
        if mtf.get("aligned"):
            bits.append("timeframes aligned")
        return f"Technical read {score:+.2f}: " + "; ".join(bits) + "."

    def llm_payload(self, ctx: MarketContext, baseline: AgentReport) -> str | None:
        ind = ctx.indicators or {}
        by_tf = {tf: {k: v for k, v in snap.items() if k != "patterns"}
                 for tf, snap in (ind.get("by_timeframe") or {}).items()}
        patterns = (ind.get("primary") or {}).get("patterns", [])
        return (
            f"Symbol: {ctx.symbol}\n"
            f"Spot: {ctx.quote.last_price if ctx.quote else 'n/a'} "
            f"({ctx.quote.change_pct if ctx.quote else 0:+.2f}%)\n\n"
            f"INDICATORS BY TIMEFRAME:\n{self._compact(by_tf)}\n\n"
            f"PATTERNS DETECTED:\n{self._compact(patterns)}\n\n"
            f"MULTI-TIMEFRAME ALIGNMENT:\n{self._compact(ind.get('mtf_alignment'))}\n\n"
            f"My rule engine scored this {baseline.score:+.3f} ({baseline.bias.value}). "
            f"Review it. Agree or overrule it, and say why.\n"
            f"Set invalidation_level to the exact price where this structure breaks."
            f"{self._recall_block(ctx)}"
        )
