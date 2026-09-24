"""Options & Futures Derivatives Analyst — the option chain read."""
from __future__ import annotations

from app.agents.base import BaseAgent
from app.core.models import AgentReport, Evidence, MarketContext
from app.core.registry import register_agent
from app.indicators.derivatives import select_strike


@register_agent("derivatives")
class DerivativesAgent(BaseAgent):
    agent_id = "derivatives"

    def analyse_rules(self, ctx: MarketContext) -> AgentReport:
        deriv = (ctx.indicators or {}).get("derivatives")
        if not deriv or not ctx.option_chain:
            return AgentReport(agent_id=self.agent_id, symbol=ctx.symbol,
                               data_available=False,
                               rationale="No option chain available for this underlying.")

        # A generated chain alongside REAL prices is invented OI and PCR next
        # to a real tape — the banner says so, and the analyst must not vote
        # on it as if it were the market. In the all-synthetic demo market it
        # is consistent with everything else, so it still votes there.
        if ctx.option_chain.synthetic and bool(self.cfg.get("data.use_real_data", True)):
            return AgentReport(agent_id=self.agent_id, symbol=ctx.symbol,
                               data_available=False,
                               rationale="Option chain is simulated (no feed served "
                                         "one) — abstaining rather than voting on "
                                         "invented open interest.")

        cfg = self.cfg.get("derivatives", {}) or {}
        score = 0.0
        evidence: list[Evidence] = []

        # --- 1. OI buildup is the primary read -------------------------------
        buildup_dir = deriv.get("buildup_direction", 0)
        if buildup_dir:
            weight = 0.35
            # Short covering is a weaker, more fragile signal than a fresh long build.
            if deriv.get("buildup") == "SHORT_COVERING":
                weight = 0.22
            score += buildup_dir * weight
            evidence.append(Evidence(label=deriv.get("buildup", "OI"),
                                     value=deriv.get("buildup_note", ""), weight=weight))

        # --- 2. PCR ----------------------------------------------------------
        pcr_dir = deriv.get("pcr_direction", 0)
        if pcr_dir:
            score += pcr_dir * 0.2
            evidence.append(Evidence(label="PCR", value=deriv.get("pcr_signal", ""), weight=0.2))

        # --- 3. Max pain gravity ---------------------------------------------
        mp_dist = deriv.get("max_pain_distance_pct", 0.0)
        if abs(mp_dist) > 1.0:
            # Price above max pain tends to get dragged down into expiry, and vice versa.
            pull = -0.15 if mp_dist > 0 else 0.15
            score += pull
            evidence.append(Evidence(
                label="Max Pain",
                value=f"Spot is {mp_dist:+.2f}% from max pain {deriv.get('max_pain')} — "
                      f"expiry gravity {'downward' if mp_dist > 0 else 'upward'}",
                weight=0.15))

        # --- 4. IV regime -----------------------------------------------------
        atm_iv = deriv.get("atm_iv", 0.0)
        avg_iv = deriv.get("avg_iv", 0.0)
        iv_spike_threshold = cfg.get("iv_spike_pct", 20.0)
        iv_warning = ""
        if atm_iv and atm_iv > iv_spike_threshold * 1.5:
            score *= 0.8
            iv_warning = (f"ATM IV {atm_iv:.1f}% is elevated — option buying is expensive here; "
                          f"prefer ITM or a spread.")
            evidence.append(Evidence(label="IV", value=iv_warning, weight=0.0))
        elif atm_iv:
            evidence.append(Evidence(label="IV", value=f"ATM IV {atm_iv:.1f}%, avg {avg_iv:.1f}%", weight=0.05))

        # --- 5. IV skew (put bid = fear) --------------------------------------
        skew = deriv.get("iv_skew", 0.0)
        if abs(skew) > 2.0:
            score += -0.1 if skew > 0 else 0.1
            evidence.append(Evidence(
                label="IV skew",
                value=f"Skew {skew:+.2f}% — {'puts bid, downside fear' if skew > 0 else 'calls bid, upside chase'}",
                weight=0.1))

        # --- 6. OI walls as levels --------------------------------------------
        walls = deriv.get("oi_walls", {})
        resistance = (walls.get("resistance") or [{}])[0].get("strike")
        support = (walls.get("support") or [{}])[0].get("strike")
        if resistance and support:
            evidence.append(Evidence(
                label="OI walls",
                value=f"Heaviest call OI at {resistance} (resistance), put OI at {support} (support)",
                weight=0.1))

        score = max(-1.0, min(1.0, score))

        # Which strike would we actually trade?
        suggested_leg = None
        if abs(score) >= 0.2:
            direction = 1 if score > 0 else -1
            moneyness = cfg.get("preferred_moneyness", "ATM")
            if atm_iv and atm_iv > iv_spike_threshold * 1.5:
                moneyness = "ITM"    # high IV: buy intrinsic, not premium
            leg = select_strike(ctx.option_chain, direction, moneyness)
            if leg:
                suggested_leg = {
                    "strike": leg.strike, "option_type": leg.option_type,
                    "ltp": leg.ltp, "iv": round(leg.iv * 100, 2),
                    "delta": leg.delta, "theta": leg.theta,
                    "moneyness": moneyness, "expiry": ctx.option_chain.expiry,
                }
                evidence.append(Evidence(
                    label="Strike selection",
                    value=f"{int(leg.strike)} {leg.option_type} @ {leg.ltp:.2f} "
                          f"(delta {leg.delta}, {moneyness})", weight=0.0))

        confidence = min(1.0, 0.3 + 0.1 * len(evidence) + abs(score) * 0.3)
        return AgentReport(
            agent_id=self.agent_id, symbol=ctx.symbol,
            bias=self._bias_from_score(score), score=round(score, 3),
            confidence=round(confidence, 2),
            rationale=(f"F&O read {score:+.2f}: {deriv.get('buildup')} with "
                       f"PCR {deriv.get('pcr_oi')}, max pain {deriv.get('max_pain')}, "
                       f"ATM IV {atm_iv:.1f}%. {iv_warning}").strip(),
            evidence=evidence,
            invalidation_level=support if score > 0 else resistance,
            extra={"derivatives": deriv, "suggested_leg": suggested_leg,
                   "support_wall": support, "resistance_wall": resistance},
        )

    def llm_payload(self, ctx: MarketContext, baseline: AgentReport) -> str | None:
        deriv = (ctx.indicators or {}).get("derivatives")
        if not deriv:
            return None
        chain = ctx.option_chain
        atm = chain.atm_strike() if chain else 0
        near = []
        if chain:
            for leg in chain.legs:
                if abs(leg.strike - atm) <= 3 * 50:
                    near.append({"k": leg.strike, "t": leg.option_type, "ltp": round(leg.ltp, 2),
                                 "oi": int(leg.oi), "doi": int(leg.oi_change),
                                 "iv": round(leg.iv * 100, 1)})
        return (
            f"Underlying: {ctx.symbol}  Spot: {deriv.get('spot')}  "
            f"Expiry: {deriv.get('expiry')}\n"
            f"Spot change today: {ctx.quote.change_pct if ctx.quote else 0:+.2f}%\n\n"
            f"CHAIN METRICS:\n{self._compact(deriv)}\n\n"
            f"NEAR-ATM LEGS (k=strike, doi=OI change):\n{self._compact(near, 2500)}\n\n"
            f"My rule engine scored {baseline.score:+.3f} ({baseline.bias.value}) and would "
            f"trade {baseline.extra.get('suggested_leg')}.\n"
            f"Judge whether option writers actually support this move, and whether that "
            f"strike is the right one given IV and time to expiry."
            f"{self._recall_block(ctx)}"
        )
