"""Risk-tiered opportunity board.

Answers the question "what should I actually look at right now?" by scanning the
whole watchlist and sorting candidates into LOW / MEDIUM / HIGH risk buckets.

The tier is NOT a guess about how much money you'll make — it is a structural
assessment of how much can go wrong:

    low     tight, liquid, multi-timeframe agreement, cheap options, clear stop
    medium  a normal setup with one or two things working against it
    high    thin confirmation, expensive premium, volatile tape, or a wide stop

A LOW-risk idea is not a better trade than a HIGH-risk one. It is a trade whose
failure mode is better understood and more survivable. Position sizing already
equalises the rupee risk; the tier tells you how likely the stop is to be a
clean, honest exit rather than a gap.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.core.config import Config, get_config
from app.core.logging import get_logger
from app.core.models import (
    Bias,
    CycleResult,
    InstrumentType,
    MarketContext,
    SignalStatus,
    TradeSignal,
)

log = get_logger("analysis.opportunities")

TIERS = ("low", "medium", "high")


@dataclass
class RiskFactor:
    """One thing that makes a setup safer or riskier, and by how much."""
    label: str
    detail: str
    points: int          # positive = riskier, negative = safer


@dataclass
class Opportunity:
    symbol: str
    bias: Bias                        # the desk's official verdict
    lean: Bias                        # which way the evidence points, even when
                                      # the desk's neutral band says "don't trade"
    conviction: float                 # |composite score| 0..1
    tier: str
    risk_points: int
    actionable: bool                  # passed every gate → tradeable now
    blocked_reasons: list[str] = field(default_factory=list)
    signal: TradeSignal | None = None
    factors: list[RiskFactor] = field(default_factory=list)
    confirmations: list[str] = field(default_factory=list)
    rationale: str = ""
    counter_argument: str = ""
    regime: str = ""

    def to_dict(self) -> dict[str, Any]:
        s = self.signal
        return {
            "symbol": self.symbol,
            "bias": self.bias.value,
            "lean": self.lean.value,
            "conviction": round(self.conviction, 3),
            "tier": self.tier,
            "risk_points": self.risk_points,
            "actionable": self.actionable,
            "blocked_reasons": self.blocked_reasons,
            "blocked_reason": self.blocked_reasons[0] if self.blocked_reasons else "",
            "regime": self.regime,
            "confirmations": self.confirmations,
            "rationale": self.rationale,
            "counter_argument": self.counter_argument,
            "factors": [{"label": f.label, "detail": f.detail, "points": f.points}
                        for f in self.factors],
            "trade": None if s is None else {
                "instrument": s.instrument.tradingsymbol,
                "instrument_type": s.instrument.instrument_type.value,
                "strike": s.instrument.strike,
                "expiry": s.instrument.expiry,
                "side": s.side.value,
                "entry": s.entry,
                "stop_loss": s.stop_loss,
                "target": s.target,
                "quantity": s.quantity,
                "lots": s.lots,
                "unit_size": s.unit_size,
                "unit_label": s.unit_label,
                "risk_reward": s.risk_reward,
                "total_risk": s.total_risk,
                "capital_at_risk_pct": s.capital_at_risk_pct,
                "notional": s.notional,
                "alert": s.alert_line(),
            },
        }


class OpportunityScanner:
    """Scans the watchlist and ranks what it finds."""

    def __init__(self, engine: Any, cfg: Config | None = None) -> None:
        self.engine = engine
        self.cfg = cfg or get_config()
        self.last_scan: dict[str, Any] = {}

    # ------------------------------------------------------------------ #
    async def scan(self, symbols: list[str] | None = None,
                   per_tier: int | None = None) -> dict[str, Any]:
        """Run the full desk across the watchlist and bucket the results."""
        targets = symbols or [w["symbol"] for w in self.cfg.watchlist()]
        per_tier = per_tier or int(self.cfg.get("opportunities.per_tier", 5))
        cycle_id = f"SCAN-{datetime.now():%H%M%S}"

        news, macro = await self._shared_data()

        opportunities: list[Opportunity] = []
        for symbol in targets:
            try:
                opp = await self._evaluate(symbol, cycle_id, news, macro)
                if opp:
                    opportunities.append(opp)
            except Exception as exc:
                log.warning("scan failed for %s: %s", symbol, exc)

        buckets: dict[str, list[dict[str, Any]]] = {t: [] for t in TIERS}
        for opp in opportunities:
            buckets[opp.tier].append(opp)

        # Rank inside each tier: tradeable first, then conviction, then R:R.
        for tier in TIERS:
            ranked = sorted(
                buckets[tier],
                key=lambda o: (o.actionable,
                               o.conviction,
                               o.signal.risk_reward if o.signal else 0.0),
                reverse=True)
            buckets[tier] = [o.to_dict() for o in ranked[:per_tier]]

        self.last_scan = {
            "ts": datetime.now().isoformat(),
            "data_source": self._provenance(),
            "scanned": len(targets),
            "found": len(opportunities),
            "actionable": sum(1 for o in opportunities if o.actionable),
            "tiers": buckets,
            "market_phase": self.engine.session_phase(),
        }
        return self.last_scan

    # ------------------------------------------------------------------ #
    async def _shared_data(self) -> tuple[list, Any]:
        news, macro = await asyncio.gather(
            self.engine.news.fetch(), self.engine.macro.fetch(),
            return_exceptions=True)
        if isinstance(news, Exception):
            news = []
        if isinstance(macro, Exception):
            macro = None
        return news, macro

    async def _evaluate(self, symbol: str, cycle_id: str,
                        news: list, macro: Any) -> Opportunity | None:
        symbol_news = self.engine.news.for_symbol(news, symbol) if news else []
        ctx = await self.engine.data.build_context(
            symbol=symbol, cycle_id=cycle_id, news=symbol_news, macro=macro,
            fundamentals=self.engine._fundamentals.get(symbol),
            recall=self.engine.feedback.recall_for(symbol),
        )

        # Evaluate only. A scan is a question; on an armed paper day the full
        # cycle would have placed an order for every tradeable setup it found.
        result: CycleResult = await self.engine.desk.run_cycle(
            ctx, cycle_id=f"{cycle_id}-{symbol}", dispatch=False)

        floor = float(self.cfg.get("opportunities.min_conviction", 0.15))

        # Nothing directional at all — not worth a slot on the board.
        if result.bias == Bias.NEUTRAL and abs(result.composite_score) < floor:
            return None

        lean = result.bias
        if lean == Bias.NEUTRAL and abs(result.composite_score) >= floor:
            lean = Bias.BULLISH if result.composite_score > 0 else Bias.BEARISH

        signal = result.signal
        blocked: list[str] = []
        if signal is None:
            # Build the hypothetical trade anyway so the board can show what the
            # setup WOULD be, and why it is not tradeable right now.
            signal, blocked = self._hypothetical(ctx, result, lean)

        factors = self._risk_factors(ctx, result, signal)
        points = sum(f.points for f in factors)
        tier = self._tier_for(points)

        decision = getattr(result, "_decision", {}) or {}
        return Opportunity(
            symbol=symbol,
            bias=result.bias,
            lean=lean,
            conviction=abs(result.composite_score),
            tier=tier,
            risk_points=points,
            actionable=(result.signal is not None
                        and result.signal.status == SignalStatus.APPROVED),
            blocked_reasons=self._prioritise(blocked, result),
            signal=signal,
            factors=factors,
            confirmations=(signal.confirmations if signal else []),
            rationale=(signal.rationale if signal else decision.get("rationale", "")),
            counter_argument=(signal.counter_argument if signal else ""),
            regime=ctx.regime.value if ctx.regime else "unknown",
        )

    @staticmethod
    def _prioritise(blocked: list[str], result: CycleResult) -> list[str]:
        """Order the blockers so the most informative one leads.

        "Past the 15:00 cutoff" resolves itself tomorrow morning; "capital is too
        small for one lot" does not. Showing the clock first would hide the fact
        that the trade is structurally untakeable at this account size.
        """
        reasons = list(blocked)
        if not reasons and result.rejected:
            reasons = list(result.rejected)
        if not reasons and result.bias == Bias.NEUTRAL:
            reasons = ["Below the desk's conviction threshold — watch, don't trade"]

        def rank(reason: str) -> int:
            low = reason.lower()
            if "lot" in low or "sizes to" in low or "cannot fit" in low:
                return 0      # structural: this account can't take the trade
            if "risk" in low or "r:r" in low or "stop" in low:
                return 1      # the setup itself is poor
            if "halt" in low or "loss limit" in low or "open positions" in low:
                return 2      # desk state
            if "cutoff" in low or "market" in low:
                return 4      # just the clock — least informative
            return 3

        return sorted(reasons, key=rank)

    def _hypothetical(self, ctx: MarketContext, result: CycleResult,
                      lean: Bias) -> tuple[TradeSignal | None, list[str]]:
        """Size the trade the desk *would* take, ignoring desk-level gates.

        This is what lets the board say "here is the setup, and here is exactly
        why you can't take it" instead of silently hiding it.
        """
        if lean == Bias.NEUTRAL:
            return None, ["No directional bias"]
        ctx.__dict__.setdefault("_reports", result.reports)
        try:
            signal = self.engine.risk.evaluate(
                ctx=ctx, bias=lean, reports=result.reports,
                composite_score=result.composite_score,
                confirmations=[f"{r.agent_id}: {r.bias.value.lower()}"
                               for r in result.reports
                               if r.data_available and abs(r.score) >= 0.25],
                rationale="Scanner projection — not a desk-approved signal.",
            )
        except Exception as exc:
            return None, [f"could not size: {exc}"]
        return signal, list(signal.rejection_reasons)

    def _tier_for(self, points: int) -> str:
        """Thresholds live in settings.yaml like everything else, so you can
        retune what "low risk" means for your own tolerance."""
        low_max = int(self.cfg.get("opportunities.low_risk_max_points", 1))
        med_max = int(self.cfg.get("opportunities.medium_risk_max_points", 4))
        if points <= low_max:
            return "low"
        return "medium" if points <= med_max else "high"

    # ------------------------------------------------------------------ #
    def _risk_factors(self, ctx: MarketContext, result: CycleResult,
                      signal: TradeSignal | None) -> list[RiskFactor]:
        """Explainable tiering. Every point is attributable to a named reason."""
        f: list[RiskFactor] = []
        primary = (ctx.indicators or {}).get("primary") or {}
        deriv = (ctx.indicators or {}).get("derivatives") or {}
        meta = self.cfg.instrument_meta(ctx.symbol)

        # --- instrument ------------------------------------------------
        if signal:
            itype = signal.instrument.instrument_type
            if itype == InstrumentType.EQUITY:
                f.append(RiskFactor("Instrument", "Cash equity — no time decay", 0))
            else:
                strike = signal.instrument.strike or 0
                spot = ctx.quote.last_price if ctx.quote else strike
                itm = (itype == InstrumentType.CALL and strike < spot) or \
                      (itype == InstrumentType.PUT and strike > spot)
                if itm:
                    f.append(RiskFactor("Instrument", "ITM option — mostly intrinsic value", 1))
                elif abs(strike - spot) / max(spot, 1) < 0.005:
                    f.append(RiskFactor("Instrument", "ATM option — premium decays daily", 2))
                else:
                    f.append(RiskFactor("Instrument", "OTM option — can expire worthless", 3))

        # --- liquidity -------------------------------------------------
        if meta.get("is_index"):
            f.append(RiskFactor("Liquidity", "Index — deepest book, easiest exit", -1))

        # --- multi-timeframe agreement --------------------------------
        mtf = (ctx.indicators or {}).get("mtf_alignment") or {}
        if mtf.get("aligned"):
            f.append(RiskFactor("Timeframes", "1m/5m/15m/daily agree", -1))
        elif mtf.get("bull_timeframes") and mtf.get("bear_timeframes"):
            f.append(RiskFactor("Timeframes", "Timeframes disagree — whipsaw risk", 2))

        # --- regime ----------------------------------------------------
        regime = primary.get("regime", "unknown")
        if regime == "volatile":
            f.append(RiskFactor("Regime", "Volatile tape — stops get hit on noise", 2))
        elif regime == "rangebound":
            f.append(RiskFactor("Regime", "Rangebound — breakouts often fail", 1))
        elif regime in {"trending_up", "trending_down"}:
            f.append(RiskFactor("Regime", f"Clean {regime.replace('_', ' ')}", 0))

        # --- stop quality ----------------------------------------------
        if signal and signal.entry:
            stop_pct = abs(signal.entry - signal.stop_loss) / signal.entry * 100
            atr = primary.get("atr", 0.0)
            stop_pts = abs(signal.entry - signal.stop_loss)
            if atr and stop_pts < 0.5 * atr:
                f.append(RiskFactor("Stop", f"Stop is only {stop_pts/atr:.1f}x ATR — "
                                            f"normal noise will hit it", 2))
            elif stop_pct > 2.0:
                f.append(RiskFactor("Stop", f"Wide {stop_pct:.1f}% stop — "
                                            f"forces a small position", 1))
            else:
                f.append(RiskFactor("Stop", f"{stop_pct:.2f}% stop, "
                                            f"{stop_pts/atr:.1f}x ATR" if atr
                                            else f"{stop_pct:.2f}% stop", 0))

        # --- option premium richness -----------------------------------
        atm_iv = deriv.get("atm_iv", 0.0)
        if atm_iv:
            if atm_iv > 25:
                f.append(RiskFactor("IV", f"ATM IV {atm_iv:.0f}% — premium is expensive", 2))
            elif atm_iv > 18:
                f.append(RiskFactor("IV", f"ATM IV {atm_iv:.0f}% — slightly rich", 1))
            else:
                f.append(RiskFactor("IV", f"ATM IV {atm_iv:.0f}% — reasonably priced", 0))

        # --- confirmation depth ----------------------------------------
        confirming = [r for r in result.reports
                      if r.data_available and abs(r.score) >= 0.25]
        if len(confirming) >= 3:
            f.append(RiskFactor("Confirmation", f"{len(confirming)} analysts agree", -1))
        elif len(confirming) < 2:
            f.append(RiskFactor("Confirmation",
                                f"Only {len(confirming)} analyst(s) — thin evidence", 2))

        abstained = [r.agent_id for r in result.reports if not r.data_available]
        if len(abstained) >= 3:
            f.append(RiskFactor("Blind spots",
                                f"{len(abstained)} analysts had no data "
                                f"({', '.join(abstained[:3])})", 1))

        # --- conviction -------------------------------------------------
        if abs(result.composite_score) >= 0.6:
            f.append(RiskFactor("Conviction",
                                f"Strong composite {result.composite_score:+.2f}", -1))

        # --- expiry proximity -------------------------------------------
        if signal and signal.instrument.expiry:
            try:
                dte = (datetime.fromisoformat(signal.instrument.expiry).date()
                       - datetime.now().date()).days
                if dte <= 1:
                    f.append(RiskFactor("Expiry", f"{dte} day(s) to expiry — "
                                                  f"theta is brutal", 3))
                elif dte <= 3:
                    f.append(RiskFactor("Expiry", f"{dte} days to expiry", 1))
            except Exception:
                pass

        return f

    # ------------------------------------------------------------------ #
    def _provenance(self) -> dict[str, Any]:
        from app.data.feeds.stack import describe_data_source
        out = describe_data_source(self.engine.broker)
        if out["simulated"]:
            out["label"] = "SIMULATED DATA — these are not real market prices"
        out["llm"] = self.cfg.llm_label
        return out
