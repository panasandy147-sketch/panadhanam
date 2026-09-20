"""Live news collection from RSS + a deterministic lexicon scorer.

The lexicon scorer is the fallback when no LLM key is present, and it also acts
as a sanity prior for the LLM agent: if the lexicon and the model disagree
violently, the news agent lowers its own confidence rather than pick a winner.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.core.config import Config, get_config
from app.core.logging import get_logger
from app.core.models import NewsItem

log = get_logger("data.news")

# --------------------------------------------------------------------------- #
# Lexicon — weights are deliberately asymmetric: markets punish bad news harder
# than they reward good news.
# --------------------------------------------------------------------------- #
BULLISH = {
    "surge": 0.7, "soar": 0.8, "rally": 0.6, "jump": 0.6, "gain": 0.4, "rise": 0.35,
    "upgrade": 0.7, "beat": 0.65, "record high": 0.8, "outperform": 0.6, "bullish": 0.7,
    "profit": 0.45, "growth": 0.4, "expansion": 0.4, "order win": 0.75, "contract win": 0.75,
    "dividend": 0.35, "buyback": 0.6, "stake buy": 0.5, "approval": 0.5, "tie-up": 0.4,
    "acquisition": 0.45, "merger": 0.4, "stimulus": 0.6, "rate cut": 0.65, "inflow": 0.5,
    "raises guidance": 0.85, "strong demand": 0.6, "turnaround": 0.55, "multi-year high": 0.7,
}
BEARISH = {
    "plunge": -0.85, "crash": -0.95, "slump": -0.7, "fall": -0.4, "drop": -0.45,
    "decline": -0.45, "downgrade": -0.75, "miss": -0.7, "loss": -0.6, "bearish": -0.7,
    "probe": -0.6, "raid": -0.75, "fraud": -0.95, "scam": -0.95, "default": -0.9,
    "resign": -0.55, "lawsuit": -0.6, "penalty": -0.65, "fine": -0.5, "ban": -0.8,
    "recall": -0.6, "layoff": -0.5, "shutdown": -0.7, "war": -0.7, "sanction": -0.65,
    "rate hike": -0.55, "outflow": -0.5, "cuts guidance": -0.9, "weak demand": -0.6,
    "insolvency": -0.9, "downtrend": -0.5, "selloff": -0.75, "record low": -0.8,
}
# Words that mean "this is commentary, not news" — damp the score hard.
OPINION_MARKERS = ("should you buy", "here's why", "analysts say", "top picks",
                   "stocks to watch", "what to expect", "explained", "opinion")

HIGH_IMPACT = ("rbi", "fed", "sebi", "budget", "gdp", "inflation", "cpi", "repo rate",
               "election", "war", "crude oil", "results", "q1", "q2", "q3", "q4")


def lexicon_score(text: str) -> tuple[float, float, list[str]]:
    """Return (score -1..1, confidence 0..1, matched terms)."""
    low = text.lower()
    hits: list[str] = []
    total = 0.0
    for term, weight in {**BULLISH, **BEARISH}.items():
        if term in low:
            total += weight
            hits.append(term)
    if not hits:
        return 0.0, 0.1, []

    # Negation flip: "profit falls" shouldn't read as bullish.
    if re.search(r"\b(no|not|fails? to|without|denies|dismissed)\b", low):
        total *= -0.5

    score = max(-1.0, min(1.0, total / max(len(hits) ** 0.6, 1.0)))
    if any(m in low for m in OPINION_MARKERS):
        score *= 0.25
    confidence = min(1.0, 0.25 + 0.18 * len(hits))
    if any(k in low for k in HIGH_IMPACT):
        confidence = min(1.0, confidence + 0.2)
    return round(score, 3), round(confidence, 2), hits


def extract_symbols(text: str, universe: list[str]) -> list[str]:
    low = text.lower()
    found = []
    for sym in universe:
        token = sym.lower().replace(" ", "")
        if token in low.replace(" ", "") or re.search(rf"\b{re.escape(sym.lower())}\b", low):
            found.append(sym)
    return found


class NewsCollector:
    def __init__(self, cfg: Config | None = None) -> None:
        self.cfg = cfg or get_config()
        self._seen: set[str] = set()

    async def fetch(self) -> list[NewsItem]:
        sources = self.cfg.get("news.sources", []) or []
        tasks = [self._fetch_rss(s) for s in sources if s.get("type") == "rss"]
        if not tasks:
            return []
        results = await asyncio.gather(*tasks, return_exceptions=True)

        items: list[NewsItem] = []
        for res in results:
            if isinstance(res, Exception):
                log.debug("news source failed: %s", res)
                continue
            items.extend(res)

        # De-duplicate by normalised title and keep the window fresh.
        lookback = int(self.cfg.get("news.lookback_minutes", 180))
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=lookback)
        unique: list[NewsItem] = []
        for item in sorted(items, key=lambda i: i.published, reverse=True):
            key = re.sub(r"\W+", "", item.title.lower())[:80]
            if key in self._seen:
                continue
            published = item.published
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
            if published < cutoff:
                continue
            self._seen.add(key)
            unique.append(item)

        if len(self._seen) > 4000:
            self._seen = set(list(self._seen)[-2000:])

        max_items = int(self.cfg.get("news.max_items_per_cycle", 40))
        return unique[:max_items]

    async def _fetch_rss(self, source: dict[str, Any]) -> list[NewsItem]:
        import feedparser

        url = source.get("url", "")
        name = source.get("name", url)
        try:
            async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
                resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0 panadhanam/1.0"})
                if resp.status_code != 200:
                    return []
                parsed = await asyncio.to_thread(feedparser.parse, resp.content)
        except Exception as exc:
            log.debug("rss fetch failed %s: %s", name, exc)
            return []

        universe = [i["symbol"] for i in self.cfg.watchlist()]
        out: list[NewsItem] = []
        for entry in parsed.entries[:30]:
            title = getattr(entry, "title", "").strip()
            if not title:
                continue
            summary = re.sub(r"<[^>]+>", "", getattr(entry, "summary", ""))[:400]
            published = datetime.now(timezone.utc)
            if getattr(entry, "published_parsed", None):
                try:
                    published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
                except Exception:
                    pass

            blob = f"{title} {summary}"
            score, conf, _hits = lexicon_score(blob)
            out.append(NewsItem(
                title=title, source=name, url=getattr(entry, "link", ""),
                published=published, summary=summary,
                symbols=extract_symbols(blob, universe),
                impact=score, confidence=conf,
                decay_minutes=180 if any(k in blob.lower() for k in HIGH_IMPACT) else 60,
            ))
        return out

    def for_symbol(self, items: list[NewsItem], symbol: str) -> list[NewsItem]:
        """News tagged to this symbol, plus broad market news that moves everything."""
        specific = [i for i in items if symbol in i.symbols]
        market_wide = [i for i in items
                       if not i.symbols and abs(i.impact) >= 0.4][:6]
        return specific + market_wide
