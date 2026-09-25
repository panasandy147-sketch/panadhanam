"""The focus list: the best few names in each band, watched every minute.

Sixty names a minute is sixty chances to trade on the weakest readings of the
day. Instead, every `focus.rerank_minutes` the whole watchlist is scored the
same way the desk scores a trade — every analyst, then the CMIO's weighted
vote — and the top `focus.per_band` names of each band become the only ones
the desk cycles on and may trade. Re-ranking lets a name that wakes up during
the session take a slot from one that has gone quiet.

Open positions are not affected: the outcome tracker manages every open
trade whether or not its symbol is still on the focus list.
"""
from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

from app.core.bus import Topic, bus
from app.core.config import Config
from app.core.logging import get_logger

log = get_logger("analysis.focus")


class FocusList:
    def __init__(self, engine: Any, cfg: Config) -> None:
        self.engine = engine
        self.cfg = cfg
        self.bands: dict[str, list[dict[str, Any]]] = {}
        self.ranked = 0
        self.updated_at: datetime | None = None
        self._refreshed = 0.0          # monotonic
        self._market = ""

    # ------------------------------------------------------------------ #
    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("focus.enabled", True))

    @property
    def per_band(self) -> int:
        return max(int(self.cfg.get("focus.per_band", 5) or 5), 1)

    @property
    def rerank_seconds(self) -> float:
        return max(float(self.cfg.get("focus.rerank_minutes", 15) or 15), 1.0) * 60

    def stale(self) -> bool:
        return (not self.bands
                or self._market != self.cfg.active_market
                or time.monotonic() - self._refreshed >= self.rerank_seconds)

    def symbols(self) -> list[str]:
        return [row["symbol"] for rows in self.bands.values() for row in rows]

    # ------------------------------------------------------------------ #
    async def targets(self, cycle_id: str, news: list,
                      macro: Any) -> tuple[list[str], dict[str, Any]]:
        """The symbols to cycle on now, plus any contexts already fetched.

        When the list is re-ranked this cycle, the contexts built for the
        ranking are handed back so the focus names are not fetched twice.
        """
        watchlist = [w["symbol"] for w in self.cfg.watchlist()]
        if not self.enabled:
            return watchlist, {}
        contexts: dict[str, Any] = {}
        if self.stale():
            contexts = await self.refresh(cycle_id, news, macro)
        return self.symbols() or watchlist, contexts

    async def refresh(self, cycle_id: str, news: list, macro: Any) -> dict[str, Any]:
        items = self.cfg.watchlist()
        symbols = [w["symbol"] for w in items]
        contexts = await self.engine._prefetch(symbols, f"{cycle_id}-rank",
                                               news, macro)
        scored = await asyncio.gather(
            *(self._score(sym, ctx) for sym, ctx in contexts.items()))
        by_symbol = {row["symbol"]: row for row in scored if row}

        bands: dict[str, list[dict[str, Any]]] = {}
        for item in items:
            row = by_symbol.get(item["symbol"])
            if row:
                bands.setdefault(str(item.get("band") or "—"), []).append(row)
        for band, rows in bands.items():
            # A setup that passes the vote outranks one that merely leans;
            # within each, the stronger combined score wins.
            rows.sort(key=lambda r: (r["tradeable"], abs(r["score"])), reverse=True)
            bands[band] = rows[:self.per_band]

        self.bands = dict(sorted(bands.items()))
        self.ranked = len(by_symbol)
        self.updated_at = datetime.now(UTC)
        self._refreshed = time.monotonic()
        self._market = self.cfg.active_market
        log.info("focus list: %s", self.summary())
        await bus.publish(Topic.FOCUS, self.snapshot())
        keep = set(self.symbols())
        return {s: c for s, c in contexts.items() if s in keep}

    def _tradeable_at_all(self, symbol: str, ctx: Any) -> bool:
        """Can this name be traded this cycle, whatever the reading?

        An index (NIFTY) has no cash instrument — only its options, and only
        from a real chain. Without one it would hold a focus slot all session
        and be refused every minute.
        """
        meta = self.cfg.instrument_meta(symbol)
        if not meta.get("is_index") or meta.get("cash_tradeable"):
            return True
        chain = getattr(ctx, "option_chain", None)
        return bool(chain) and not getattr(chain, "synthetic", False)

    async def _score(self, symbol: str, ctx: Any) -> dict[str, Any] | None:
        """The desk's own read of a symbol, without placing or logging a trade."""
        if not self._tradeable_at_all(symbol, ctx):
            return None
        try:
            desk = self.engine.desk
            reports = (await desk._node_analysts({"context": ctx}))["reports"]
            vote = desk.cmio._weighted_vote(ctx, reports)
        except Exception as exc:
            log.debug("focus scoring failed for %s: %s", symbol, exc)
            return None
        bias = vote["bias"]
        return {
            "symbol": symbol,
            "score": round(float(vote["composite_score"]), 3),
            "bias": getattr(bias, "value", str(bias)),
            "tradeable": bool(vote["proceed"]),
            "why": ", ".join(vote.get("confirmations") or []) or "no strong reading",
        }

    # ------------------------------------------------------------------ #
    def summary(self) -> str:
        return "; ".join(f"{band}: {', '.join(r['symbol'] for r in rows)}"
                         for band, rows in self.bands.items()) or "empty"

    def snapshot(self) -> dict[str, Any]:
        nxt = None
        if self.updated_at:
            nxt = datetime.fromtimestamp(
                self.updated_at.timestamp() + self.rerank_seconds, UTC).isoformat()
        return {
            "enabled": self.enabled,
            "per_band": self.per_band,
            "rerank_minutes": self.rerank_seconds / 60,
            "ranked": self.ranked,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "next_rerank_at": nxt,
            "bands": self.bands,
        }
