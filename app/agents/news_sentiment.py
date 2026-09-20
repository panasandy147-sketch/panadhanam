"""News & Sentiment Lead — triages live headlines for tradeable impact."""
from __future__ import annotations

from datetime import UTC, datetime

from app.agents.base import BaseAgent
from app.core.models import AgentReport, Evidence, MarketContext
from app.core.registry import register_agent


@register_agent("news_sentiment")
class NewsSentimentAgent(BaseAgent):
    agent_id = "news_sentiment"

    def analyse_rules(self, ctx: MarketContext) -> AgentReport:
        if not ctx.news:
            return AgentReport(agent_id=self.agent_id, symbol=ctx.symbol,
                               data_available=False,
                               rationale="No news in the lookback window.")

        now = datetime.now(UTC)
        weighted_sum = 0.0
        weight_total = 0.0
        evidence: list[Evidence] = []
        high_impact: list[str] = []

        for item in ctx.news:
            published = item.published
            if published.tzinfo is None:
                published = published.replace(tzinfo=UTC)
            age_min = max((now - published).total_seconds() / 60.0, 0.0)

            # Time decay: a 3-hour-old headline is mostly priced in.
            decay = max(0.0, 1.0 - age_min / max(item.decay_minutes, 1))
            # Symbol-specific news outweighs generic market news.
            specificity = 1.0 if ctx.symbol in item.symbols else 0.45
            weight = decay * specificity * max(item.confidence, 0.1)

            weighted_sum += item.impact * weight
            weight_total += weight

            if abs(item.impact) >= self.cfg.get("news.high_impact_threshold", 0.7):
                high_impact.append(item.title[:110])
            if abs(item.impact) >= 0.35 and len(evidence) < 6:
                evidence.append(Evidence(
                    label=f"{item.source} ({int(age_min)}m ago)",
                    value=f"{item.title[:130]} → {item.impact:+.2f}",
                    weight=round(weight, 3)))

        score = weighted_sum / weight_total if weight_total else 0.0
        score = max(-1.0, min(1.0, score))

        # Confidence tracks corroboration: one headline is an anecdote.
        corroboration = min(1.0, len(ctx.news) / 8.0)
        confidence = min(1.0, 0.2 + corroboration * 0.4 + abs(score) * 0.35)

        rationale = (f"Sentiment {score:+.2f} across {len(ctx.news)} items "
                     f"({sum(1 for i in ctx.news if ctx.symbol in i.symbols)} symbol-specific).")
        if high_impact:
            rationale += f" High-impact: {high_impact[0]}"

        return AgentReport(
            agent_id=self.agent_id, symbol=ctx.symbol,
            bias=self._bias_from_score(score, 0.2), score=round(score, 3),
            confidence=round(confidence, 2), rationale=rationale, evidence=evidence,
            extra={"item_count": len(ctx.news), "high_impact": high_impact,
                   "has_high_impact": bool(high_impact)},
        )

    def llm_payload(self, ctx: MarketContext, baseline: AgentReport) -> str | None:
        if not ctx.news:
            return None
        now = datetime.now(UTC)
        items = []
        for item in ctx.news[:25]:
            published = item.published
            if published.tzinfo is None:
                published = published.replace(tzinfo=UTC)
            items.append({
                "age_min": int((now - published).total_seconds() / 60),
                "src": item.source,
                "title": item.title[:160],
                "symbols": item.symbols,
                "lexicon_score": item.impact,
            })
        return (
            f"Symbol under review: {ctx.symbol}\n"
            f"Current price: {ctx.quote.last_price if ctx.quote else 'n/a'} "
            f"({ctx.quote.change_pct if ctx.quote else 0:+.2f}% today)\n\n"
            f"HEADLINES (newest first, lexicon_score is a crude keyword prior):\n"
            f"{self._compact(items, 4000)}\n\n"
            f"Score the NET tradeable impact on {ctx.symbol} for the next 1-3 hours.\n"
            f"Discount anything already reflected in today's {ctx.quote.change_pct if ctx.quote else 0:+.2f}% move.\n"
            f"Opinion and 'stocks to watch' pieces are not news — score them near zero.\n"
            f"My keyword engine said {baseline.score:+.3f}; it cannot tell new information "
            f"from recycled commentary, so overrule it where that matters."
        )
