"""Agent 3 — Macro & Sentiment: is anything outside the chart against it?

Two inputs, both read through the data feed:

  headlines   the symbol's recent news (Yahoo search). A HIGH-IMPACT catalyst
              against the trade — a downgrade, a guidance cut, an
              investigation, a bankruptcy filing under a call; a buyout or an
              upgrade under a put — is a hard veto. The keyword list is the
              floor; Ollama, when running, reads the same headlines and may
              veto on one the keywords missed. It may not veto on no news.
  futures     ES and NQ against their prior settle. A tape moving hard the
              other way (`agents.macro.futures_veto_pct`, 1.5%) is a veto;
              a milder lean moves the score.

A feed that serves no headlines or futures (the offline test feed, Tradier)
leaves this agent neutral rather than blocking the desk.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta
from typing import Any

from panaoptions.agents.base import AgentVote, ask, blend, clamp
from panaoptions.alpha import AlphaSignal
from panaoptions.logging import get_logger

log = get_logger("agents.macro")

NAME = "Macro"

NEGATIVE = [re.compile(p, re.I) for p in (
    r"\bdowngrad(e|es|ed|ing)\b", r"\bbankrupt(cy)?\b", r"\bchapter 11\b",
    r"\bfraud\b", r"\binvestigation\b", r"\bprobe\b", r"\bsubpoena",
    r"\b(sec|doj|ftc) (sues|charges|charged)\b", r"\brecall(s|ed)?\b",
    r"\b(cuts?|lowers?|slashes|withdraws?|pulls?) (its |full[- ]year |annual )?"
    r"(guidance|outlook|forecast)\b", r"\bguidance cut\b", r"\bprofit warning\b",
    r"\bmiss(es|ed)? (estimates|expectations|forecasts)\b",
    r"\btrading halt(ed)?\b", r"\bdelist", r"\bclass action\b", r"\bdata breach\b",
    r"\b(ceo|cfo) (resigns|steps down|ousted|fired|departs)\b",
    r"\bfda (rejects|rejection|declines|refuses)\b", r"\bplung(e|es|ed|ing)\b",
    r"\bshort[- ]seller report\b",
)]
POSITIVE = [re.compile(p, re.I) for p in (
    r"\bupgrad(e|es|ed|ing)\b", r"\bbeats? (estimates|expectations|forecasts)\b",
    r"\b(raises?|lifts?|boosts?) (its |full[- ]year |annual )?(guidance|outlook|forecast)\b",
    r"\b(to acquire|acquisition of|buyout|takeover bid|agrees to be acquired)\b",
    r"\bfda approv", r"\brecord (revenue|profit|quarter)\b", r"\bsoar(s|ed|ing)?\b",
    r"\bsurg(e|es|ed|ing)\b",
)]

_NEWS_TTL = 600.0
_news_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def classify(title: str) -> int:
    """-1 high-impact negative, +1 high-impact positive, 0 neither or both."""
    neg = any(p.search(title) for p in NEGATIVE)
    pos = any(p.search(title) for p in POSITIVE)
    return 0 if neg == pos else (-1 if neg else 1)


async def headlines(feed: Any, symbol: str, now: datetime,
                    hours: float) -> list[dict[str, Any]]:
    """Headlines from the last `hours`, cached ten minutes per symbol."""
    source = getattr(feed, "news", None)
    if source is None:
        return []
    cached = _news_cache.get(symbol)
    if cached and time.monotonic() - cached[0] < _NEWS_TTL:
        items = cached[1]
    else:
        try:
            items = list(await source(symbol))
        except Exception as exc:                      # noqa: BLE001
            log.debug("news for %s failed: %s", symbol, exc)
            items = []
        _news_cache[symbol] = (time.monotonic(), items)
    cutoff = now - timedelta(hours=hours)
    return [n for n in items
            if n.get("published") is None or n["published"] >= cutoff]


async def futures_move(feed: Any, symbols: list[str]) -> float | None:
    """The average move of the index futures, in percent, or None."""
    source = getattr(feed, "futures_change", None)
    if source is None:
        return None
    moves = []
    for sym in symbols:
        try:
            move = await source(sym)
        except Exception:                             # noqa: BLE001
            move = None
        if move is not None:
            moves.append(float(move))
    return round(sum(moves) / len(moves), 2) if moves else None


async def vote(signal: AlphaSignal, feed: Any, cfg: Any,
               now: datetime) -> AgentVote:
    """Score the world outside the chart; veto on a catalyst against it."""
    result = AgentVote(agent=NAME, score=0.60)
    want = 1 if signal.long else -1
    hours = float(cfg.get("agents.macro.lookback_hours", 18))
    news = await headlines(feed, signal.symbol, now, hours)

    against = [n["title"] for n in news if classify(n["title"]) == -want]
    behind = [n["title"] for n in news if classify(n["title"]) == want]
    if against:
        result.veto = True
        result.reasons.append(f"high-impact catalyst against the trade: “{against[0]}”")
    elif behind:
        result.score += 0.15
        result.reasons.append(f"catalyst behind the trade: “{behind[0]}”")
    elif news:
        result.reasons.append(f"{len(news)} headline(s), none high-impact")
    else:
        result.reasons.append("no headlines in the lookback")

    futures = list(cfg.get("agents.macro.futures", ["ES=F", "NQ=F"]) or [])
    move = await futures_move(feed, futures)
    veto_pct = float(cfg.get("agents.macro.futures_veto_pct", 1.5))
    if move is not None:
        leaning = move * want
        if leaning <= -veto_pct:
            result.veto = True
            result.reasons.append(f"index futures {move:+.2f}% — hard against the trade")
        elif leaning <= -0.5:
            result.score -= 0.15
            result.reasons.append(f"index futures {move:+.2f}% lean against")
        elif leaning >= 0.5:
            result.score += 0.10
            result.reasons.append(f"index futures {move:+.2f}% agree")
        else:
            result.reasons.append(f"index futures {move:+.2f}% flat")

    result.score = round(clamp(result.score), 3)
    result.data = {"headlines": [n["title"] for n in news[:8]],
                   "futures_move_pct": move}
    if news:
        opinion = await ask(cfg, NAME,
                            "Read the headlines and futures. Veto ONLY for a "
                            "high-impact catalyst against the trade direction "
                            "(for a LONG: downgrade, guidance cut, legal or "
                            "regulatory action; for a SHORT: buyout, upgrade, "
                            "beat-and-raise).",
                            {"symbol": signal.symbol, "direction": signal.direction,
                             **result.data})
        result = blend(result, opinion, cfg, veto_allowed=True)
    return result
