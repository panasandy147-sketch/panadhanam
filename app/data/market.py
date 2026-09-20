"""Context builder — assembles everything the analysts need for one symbol.

This is the only place that talks to the broker for data. Agents never fetch;
they receive a fully-populated `MarketContext`. That separation is what makes
agents trivially testable (hand them a fixture, assert the report).
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from app.brokers.base import BrokerAdapter
from app.core.config import Config, get_config
from app.core.logging import get_logger
from app.core.models import (Fundamentals, MacroSnapshot, MarketContext,
                             NewsItem, Regime)
from app.indicators import patterns as pattern_mod
from app.indicators import ta
from app.indicators.derivatives import analyse as analyse_chain
from app.indicators.derivatives import enrich_chain

log = get_logger("data.market")


class MarketDataService:
    def __init__(self, broker: BrokerAdapter, cfg: Config | None = None) -> None:
        self.broker = broker
        self.cfg = cfg or get_config()

    async def build_context(
        self,
        symbol: str,
        cycle_id: str,
        news: list[NewsItem] | None = None,
        macro: MacroSnapshot | None = None,
        fundamentals: Fundamentals | None = None,
        recall: list[dict[str, Any]] | None = None,
    ) -> MarketContext:
        tech = self.cfg.get("technical", {}) or {}
        timeframes: list[str] = tech.get("timeframes", ["5m", "15m"])

        quote_task = self.broker.get_quote(symbol)
        candle_tasks = {tf: self.broker.get_candles(symbol, tf, 200) for tf in timeframes}
        chain_task = self.broker.get_option_chain(symbol) if self.broker.supports_options else None

        quote = await quote_task
        candle_results = await asyncio.gather(*candle_tasks.values(), return_exceptions=True)
        candles = {}
        for tf, res in zip(candle_tasks.keys(), candle_results):
            candles[tf] = [] if isinstance(res, Exception) else res

        chain = None
        if chain_task is not None:
            try:
                chain = await chain_task
            except Exception as exc:
                log.debug("chain fetch failed for %s: %s", symbol, exc)

        ctx = MarketContext(
            symbol=symbol, cycle_id=cycle_id, quote=quote, candles=candles,
            news=news or [], macro=macro, fundamentals=fundamentals,
            recall=recall or [],
        )

        ctx.indicators = self._compute_indicators(candles, tech)
        regime = ctx.indicators.get("primary", {}).get("regime")
        ctx.regime = Regime(regime) if regime in {r.value for r in Regime} else None

        if chain:
            ctx.option_chain = self._prepare_chain(chain, quote)
            ctx.indicators["derivatives"] = analyse_chain(
                ctx.option_chain,
                quote.change_pct if quote else 0.0,
                self.cfg.get("derivatives", {}) or {},
            )
        return ctx

    def _compute_indicators(self, candles: dict[str, list], tech: dict) -> dict[str, Any]:
        primary_tf = tech.get("primary_timeframe", "5m")
        out: dict[str, Any] = {"by_timeframe": {}}

        for tf, series in candles.items():
            if not series:
                continue
            df = ta.candles_to_df(series)
            if df.empty:
                continue
            snapshot = ta.compute_all(df, tech)
            snapshot["patterns"] = pattern_mod.scan(df, tech.get("patterns_enabled"))
            out["by_timeframe"][tf] = snapshot

        primary = out["by_timeframe"].get(primary_tf)
        if primary is None and out["by_timeframe"]:
            primary = next(iter(out["by_timeframe"].values()))
        out["primary"] = primary or {}
        out["primary_timeframe"] = primary_tf
        out["mtf_alignment"] = self._mtf_alignment(out["by_timeframe"])
        return out

    @staticmethod
    def _mtf_alignment(by_tf: dict[str, dict]) -> dict[str, Any]:
        """Do the timeframes agree? Disagreement is the single most common reason
        an intraday setup fails, so it gets surfaced explicitly."""
        votes = []
        for tf, snap in by_tf.items():
            if snap.get("ema_stacked_bull"):
                votes.append((tf, 1))
            elif snap.get("ema_stacked_bear"):
                votes.append((tf, -1))
            else:
                votes.append((tf, 0))
        if not votes:
            return {"aligned": False, "direction": 0, "detail": {}}
        signs = [v for _, v in votes]
        bull, bear = signs.count(1), signs.count(-1)
        direction = 1 if bull > bear and bull >= 2 else (-1 if bear > bull and bear >= 2 else 0)
        return {
            "aligned": direction != 0 and (bull == 0 or bear == 0),
            "direction": direction,
            "bull_timeframes": bull,
            "bear_timeframes": bear,
            "detail": {tf: v for tf, v in votes},
        }

    def _prepare_chain(self, chain, quote):
        deriv = self.cfg.get("derivatives", {}) or {}
        try:
            expiry_date = datetime.fromisoformat(chain.expiry).date()
            dte = max((expiry_date - datetime.now().date()).days, 1)
        except Exception:
            dte = 7
        if quote and quote.last_price:
            chain.spot = quote.last_price
        return enrich_chain(chain, dte, deriv.get("risk_free_rate", 0.065))


async def fetch_fundamentals(symbol: str) -> Fundamentals | None:
    """Fundamental data via Yahoo's quoteSummary (no key required).

    Returns None on any failure — the fundamental agent then abstains rather
    than passing a stock it could not actually verify.
    """
    import httpx

    # Indian equities need a ".NS" suffix on Yahoo; US tickers take none.
    from app.core.config import get_config
    suffix = get_config().market.yahoo_suffix
    url = ("https://query2.finance.yahoo.com/v10/finance/quoteSummary/"
           f"{symbol}{suffix}")
    modules = "defaultKeyStatistics,financialData,summaryDetail"
    try:
        async with httpx.AsyncClient(timeout=12.0, follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0"}) as c:
            r = await c.get(url, params={"modules": modules})
            if r.status_code != 200:
                return None
            result = r.json()["quoteSummary"]["result"][0]
    except Exception:
        return None

    def _v(node: dict, key: str) -> float | None:
        item = (node or {}).get(key)
        if isinstance(item, dict):
            return item.get("raw")
        return item if isinstance(item, (int, float)) else None

    fin = result.get("financialData", {})
    stats = result.get("defaultKeyStatistics", {})
    detail = result.get("summaryDetail", {})

    roe = _v(fin, "returnOnEquity")
    growth = _v(fin, "earningsGrowth")
    return Fundamentals(
        symbol=symbol,
        pe=_v(detail, "trailingPE") or _v(stats, "forwardPE"),
        roe=round(roe * 100, 2) if roe is not None else None,
        eps=_v(stats, "trailingEps"),
        profit_growth_pct=round(growth * 100, 2) if growth is not None else None,
        debt_to_equity=_v(fin, "debtToEquity"),
        avg_volume=_v(detail, "averageVolume"),
    )
