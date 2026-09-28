"""Volume Profile Analyst — where the session's volume says price turns or runs.

Builds the prior and current Regular Trading Hours profiles (POC, the 70%
value area, low and high volume nodes) from the 5-minute tape and runs the
three volume-profile strategies (app/strategies/volume_profile_strategies.py):

  Value Area Rejection    a failed auction at the VAH (bearish → a put) or a
                          held VAL test (bullish → a call), target the POC
  LVN Pocket Acceleration a close from a shelf into a volume pocket on
                          RVOL >= 1.5x, target the pocket's far edge
  POC Magnet / Bounce     back to the POC after a 1-ATR move away, rejected

A trigger is a directional vote with its own invalidation level, which the
risk desk uses as the structural stop. No trigger is silence (score 0), not
a vote against — the profile levels still go into `extra` for the CMIO's
confluence check on whatever else trades.
"""
from __future__ import annotations

from app.agents.base import BaseAgent
from app.core.models import AgentReport, Evidence, MarketContext
from app.core.registry import register_agent
from app.strategies import volume_profile_strategies as vp

# How strongly each setup votes. The failed auction and the pocket break are
# the cleaner reads; a POC bounce is a mean-reversion bet inside value.
_CONVICTION = {"va_rejection": 0.60, "lvn_acceleration": 0.65, "poc_bounce": 0.50}


@register_agent("volume_profile")
class VolumeProfileAgent(BaseAgent):
    agent_id = "volume_profile"

    def analyse_rules(self, ctx: MarketContext) -> AgentReport:
        tf = str(self.cfg.get("technical.primary_timeframe", "5m"))
        candles = (ctx.candles or {}).get(tf) or []
        atr = float(((ctx.indicators or {}).get("primary") or {}).get("atr", 0.0) or 0.0)
        if not candles:
            return AgentReport(agent_id=self.agent_id, symbol=ctx.symbol,
                               data_available=False,
                               rationale="No intraday candles to build a volume profile from.")

        found, profiles = vp.evaluate(candles, self.cfg, atr)
        if not profiles:
            return AgentReport(agent_id=self.agent_id, symbol=ctx.symbol,
                               data_available=False,
                               rationale="No regular-hours session to build a profile from.")

        levels = {k: p.to_dict() for k, p in profiles.items()}
        summary = "; ".join(f"{k} POC {p.poc:.2f}, value {p.val:.2f}-{p.vah:.2f}"
                            for k, p in profiles.items())
        if found is None:
            return AgentReport(
                agent_id=self.agent_id, symbol=ctx.symbol, score=0.0, confidence=0.3,
                rationale=f"No volume-profile trigger. {summary}.",
                extra={"profiles": levels, "setup": None})

        score = found.direction * _CONVICTION.get(found.strategy, 0.5)
        evidence = [Evidence(label=found.name, value=c, weight=round(abs(score) / 3, 3))
                    for c in found.confirmations]
        return AgentReport(
            agent_id=self.agent_id, symbol=ctx.symbol,
            bias=self._bias_from_score(score), score=round(score, 3),
            confidence=0.75,
            rationale=(f"{found.name} at the {found.level_name} ({found.level:.2f}) → "
                       f"{found.right}, target {found.target:.2f}, wrong beyond "
                       f"{found.invalidation:.2f}. {summary}."),
            evidence=evidence,
            invalidation_level=round(found.invalidation, 4),
            suggested_entry=round(found.trigger, 4),
            suggested_target=round(found.target, 4),
            extra={"profiles": levels, "setup": found.strategy, "setup_name": found.name,
                   "level": found.level, "level_name": found.level_name},
        )
