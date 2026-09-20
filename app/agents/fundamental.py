"""Fundamental & Quality Filter — a veto gate, never a direction signal.

Runs pre-market by default. Indices skip it entirely (an index has no P/E to
screen), which is why `data_available=False` and abstention matter so much here.
"""
from __future__ import annotations

from app.agents.base import BaseAgent
from app.core.models import AgentReport, Evidence, MarketContext
from app.core.registry import register_agent


@register_agent("fundamental")
class FundamentalAgent(BaseAgent):
    agent_id = "fundamental"

    def analyse_rules(self, ctx: MarketContext) -> AgentReport:
        cfg = self.cfg.get("fundamental", {}) or {}
        if not cfg.get("enabled", True):
            return AgentReport(agent_id=self.agent_id, symbol=ctx.symbol,
                               data_available=False, rationale="Fundamental filter disabled.")

        meta = self.cfg.instrument_meta(ctx.symbol)
        if meta.get("is_index"):
            return AgentReport(agent_id=self.agent_id, symbol=ctx.symbol,
                               data_available=False,
                               rationale="Index — fundamental screening not applicable.",
                               extra={"eligible": True, "reason": "index bypasses the filter"})

        f = ctx.fundamentals
        if not f:
            return AgentReport(agent_id=self.agent_id, symbol=ctx.symbol,
                               data_available=False,
                               rationale="No fundamental data retrieved — cannot verify quality.",
                               extra={"eligible": False, "reason": "data unavailable"})

        passes: list[str] = []
        fails: list[str] = []
        evidence: list[Evidence] = []

        def check(ok: bool | None, label: str, detail: str) -> None:
            if ok is None:
                return
            (passes if ok else fails).append(label)
            evidence.append(Evidence(label=label, value=detail, weight=1.0 if ok else -1.0))

        # ROE
        if f.roe is not None:
            min_roe = cfg.get("min_roe", 20.0)
            check(f.roe >= min_roe, "ROE", f"ROE {f.roe:.1f}% vs required {min_roe}%")

        # P/E — vs industry if we have it, else an absolute ceiling
        if f.pe is not None:
            if f.industry_pe:
                ratio = cfg.get("max_pe_vs_industry_ratio", 1.2)
                check(f.pe <= f.industry_pe * ratio,
                      "P/E vs industry",
                      f"P/E {f.pe:.1f} vs industry {f.industry_pe:.1f} (max {ratio}x)")
            else:
                cap = cfg.get("max_absolute_pe", 60.0)
                check(f.pe <= cap, "P/E", f"P/E {f.pe:.1f} vs cap {cap} (no industry benchmark)")

        # EPS
        if cfg.get("require_positive_eps", True) and f.eps is not None:
            check(f.eps > 0, "EPS", f"EPS {f.eps:.2f}")

        # Profit growth
        if f.profit_growth_pct is not None:
            min_g = cfg.get("min_profit_growth_pct", 5.0)
            check(f.profit_growth_pct >= min_g,
                  "Profit growth", f"Growth {f.profit_growth_pct:.1f}% vs required {min_g}%")

        # Leverage — Yahoo reports D/E as a percentage
        if f.debt_to_equity is not None:
            max_de = cfg.get("max_debt_to_equity", 1.5)
            de = f.debt_to_equity / 100.0 if f.debt_to_equity > 10 else f.debt_to_equity
            check(de <= max_de, "Debt/Equity", f"D/E {de:.2f} vs max {max_de}")

        # Liquidity — the one that actually gets people trapped intraday
        if f.avg_volume is not None:
            min_vol = cfg.get("min_avg_volume", 500_000)
            check(f.avg_volume >= min_vol,
                  "Liquidity", f"Avg volume {f.avg_volume:,.0f} vs required {min_vol:,.0f}")

        total = len(passes) + len(fails)
        quality = len(passes) / total if total else 0.0
        # A liquidity failure is disqualifying on its own — you cannot exit a thin name.
        eligible = quality >= 0.6 and "Liquidity" not in fails

        return AgentReport(
            agent_id=self.agent_id, symbol=ctx.symbol,
            bias=self._bias_from_score(0.0),   # never directional, by design
            score=0.0,
            confidence=round(quality, 2),
            rationale=(f"Quality {quality:.0%} ({len(passes)}/{total} checks). "
                       f"{'ELIGIBLE' if eligible else 'NOT ELIGIBLE'}."
                       + (f" Failed: {', '.join(fails)}." if fails else "")),
            evidence=evidence,
            extra={"eligible": eligible, "quality_score": round(quality, 3),
                   "passes": passes, "fails": fails},
        )

    def llm_payload(self, ctx: MarketContext, baseline: AgentReport) -> str | None:
        # Deterministic screening is strictly better than a model here — the
        # thresholds are explicit and auditable. No LLM call.
        return None
