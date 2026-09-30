"""Context builder — assembles everything the analysts need for one symbol.

This is the only place that talks to the broker for data. Agents never fetch;
they receive a fully-populated `MarketContext`. That separation is what makes
agents trivially testable (hand them a fixture, assert the report).
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from app.brokers.base import BrokerAdapter
from app.core.config import Config, get_config
from app.core.logging import get_logger
from app.core.models import (Fundamentals, MacroSnapshot, MarketContext,
                             NewsItem, Regime)
from app.indicators import patterns as pattern_mod
from app.indicators import ta
from app.indicators.derivatives import analyse as analyse_chain

# (symbol, day) -> the F&O picture last stored, so it is written once a day
# and again only when it changes.
_FNO_SAVED: dict[tuple[str, str], dict] = {}
from app.indicators.derivatives import enrich_chain

log = get_logger("data.market")


def completed_bars(bars: list, timeframe: str, now: datetime | None = None) -> list:
    """Drop a last INTRADAY bar that has not closed yet (its start plus the
    timeframe is still in the future). Daily and longer bars are kept: the
    previous-day levels already read completed days only."""
    unit = {"m": 1, "h": 60}.get(str(timeframe)[-1:], 0)
    try:
        minutes = int(str(timeframe)[:-1]) * unit
    except ValueError:
        minutes = 0
    if not minutes or not bars:
        return bars
    now = now or datetime.now(timezone.utc)
    # Every bar that has not closed goes — Yahoo can end a chart with the
    # forming bucket AND a live point stamped with the current minute.
    out = list(bars)
    while out:
        last = out[-1].ts
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if last + timedelta(minutes=minutes) <= now:
            break
        out.pop()
    return out


def stale_reason(bars: list, timeframe: str, max_bars: float,
                 now: datetime | None = None) -> str:
    """'' when the last closed intraday bar is recent, else why it is stale."""
    unit = {"m": 1, "h": 60}.get(str(timeframe)[-1:], 0)
    try:
        minutes = int(str(timeframe)[:-1]) * unit
    except ValueError:
        minutes = 0
    if not bars or not minutes or max_bars <= 0:
        return ""
    now = now or datetime.now(timezone.utc)
    end = bars[-1].ts
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    age = (now - (end + timedelta(minutes=minutes))).total_seconds() / 60
    if age > max_bars * minutes:
        return (f"data stale: the last closed {timeframe} bar ended {age:.0f} min ago "
                f"(more than {max_bars:g} bars) — not traded on")
    return ""


class MarketDataService:
    def __init__(self, broker: BrokerAdapter, cfg: Config | None = None) -> None:
        self.broker = broker
        self.cfg = cfg or get_config()
        # (symbol, timeframe) -> (monotonic time fetched, candles)
        self._slow: dict[tuple[str, str], tuple[float, list]] = {}

    # Daily and 15m bars barely move between one-minute cycles. Re-fetching
    # them for 60 symbols every minute is ~120 needless requests a minute to
    # a free feed that throttles; 1m and 5m are always fetched fresh.
    _DEFAULT_TTL = {"15m": 120, "30m": 240, "1h": 300, "1d": 900}

    def _dte(self, expiry: str) -> int | None:
        from app.core import clock
        try:
            day = datetime.fromisoformat(str(expiry)[:10]).date()
        except ValueError:
            return None
        today = clock.market_now(str(self.cfg.get("system.timezone",
                                                  "Asia/Kolkata"))).date()
        return (day - today).days

    async def _chain_in_window(self, symbol: str):
        """The chain for an expiry inside derivatives.min/max_days_to_expiry.

        Asking for "the" chain returns the NEAREST expiry — on expiry day that
        is a 0DTE contract, which a 15-minute pause bleeds to nothing. The
        window was in the config and never applied. Now: the nearest expiry
        inside it, or failing that the nearest beyond its floor; never one
        under the floor.
        """
        lo = int(self.cfg.get("derivatives.min_days_to_expiry", 3))
        hi = int(self.cfg.get("derivatives.max_days_to_expiry", 7))
        try:
            expiries = await self.broker.get_expiries(symbol)
        except Exception:
            expiries = []
        dated = sorted((d, e) for e in expiries or []
                       if (d := self._dte(e)) is not None and d >= lo)
        if not dated:
            return None
        inside = [e for d, e in dated if d <= hi]
        return await self.broker.get_option_chain(symbol, inside[0] if inside
                                                  else dated[0][1])

    async def _candles(self, symbol: str, tf: str) -> list:
        ttl = float((self.cfg.get("technical.cache_seconds") or self._DEFAULT_TTL)
                    .get(tf, 0) or 0)
        key = (symbol, tf)
        hit = self._slow.get(key)
        if ttl and hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
        bars = await self.broker.get_candles(symbol, tf, 200)
        if ttl and bars:
            self._slow[key] = (time.monotonic(), bars)
        return bars

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
        candle_tasks = {tf: self._candles(symbol, tf) for tf in timeframes}
        # Only names marked F&O have a chain worth asking for. Asking NSE for
        # forty cash-only names a minute is how a feed gets rate-limited.
        fno = bool(self.cfg.instrument_meta(symbol).get("fno", True))
        chain_task = (self._chain_in_window(symbol)
                      if self.broker.supports_options and fno else None)

        quote = await quote_task
        candle_results = await asyncio.gather(*candle_tasks.values(), return_exceptions=True)
        candles = {}
        for tf, res in zip(candle_tasks.keys(), candle_results):
            candles[tf] = [] if isinstance(res, Exception) else res
        # CLOSED intraday bars only (technical.completed_bars_only): the feed's
        # last bar is still forming, and a pattern, a volume surge or a
        # sweep's reclaim judged on it is judged on a minute of a five-minute
        # candle. The replay always used closed bars. Exits follow the quote.
        # (The paper broker's own synthetic market is not on the market clock:
        # neither rule applies to it — only to a real feed's candles.)
        synthetic = (getattr(self.broker, "name", "") == "paper"
                     and not getattr(self.broker, "data_source", None))
        if bool(tech.get("completed_bars_only", True)) and not synthetic:
            candles = {tf: completed_bars(bars, tf) for tf, bars in candles.items()}
        # A tape that stopped updating mid-session is not traded on: an
        # intraday timeframe whose last closed bar ended more than
        # technical.stale_after_bars bars ago (in market hours) is emptied, so
        # the analysts see no data rather than an old picture.
        stale_bars = float(tech.get("stale_after_bars", 3) or 0)
        if stale_bars > 0 and not synthetic and self._in_session():
            for tf, bars in candles.items():
                why = stale_reason(bars, tf, stale_bars)
                if why:
                    log.warning("%s %s: %s", symbol, tf, why)
                    candles[tf] = []

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
            # Keep a daily ATM IV sample from REAL chains, so IV rank can be
            # measured against this symbol's own history.
            atm_iv = ctx.indicators["derivatives"].get("atm_iv")
            if atm_iv and not getattr(ctx.option_chain, "synthetic", False):
                try:
                    from app.core import clock
                    from app.storage import db
                    day = clock.market_now(str(self.cfg.get(
                        "system.timezone", "Asia/Kolkata"))).date().isoformat()
                    db.record_iv(symbol, day, float(atm_iv))
                except Exception as exc:
                    log.debug("IV sample not recorded for %s: %s", symbol, exc)

        # The previous day's F&O picture: PDH/PDL/PDC, OI and its change,
        # the build-up. Stored once a day per symbol for the review.
        try:
            from app.agents import fno_confluence
            from app.core import clock
            tz = str(self.cfg.get("system.timezone", "Asia/Kolkata"))
            today = clock.market_now(tz).date()
            prev = fno_confluence.previous_day(candles, tz, today)
            ctx.indicators["previous_day"] = prev
            # The Previous Day Liquidity Sweep trigger: price pierced the PDH
            # or PDL and the 5m/15m candle closed back inside the range.
            if prev and bool(self.cfg.get("pd_sweep.enabled", True)):
                from app.strategies import pd_sweep
                sweep = pd_sweep.detect(candles, prev, tz, today, tuple(
                    self.cfg.get("pd_sweep.timeframes") or ("5m", "15m")))
                ctx.indicators["pd_sweep"] = sweep.to_dict() if sweep else None
            if prev:
                pic = fno_confluence.picture(symbol, prev, ctx.indicators.get("derivatives"))
                ctx.indicators["fno_picture"] = pic
                key = (symbol, today.isoformat())
                if _FNO_SAVED.get(key) != pic:
                    from app.storage import db
                    db.record_fno_day(symbol, today.isoformat(), pic)
                    _FNO_SAVED[key] = pic
        except Exception as exc:                        # noqa: BLE001
            log.debug("F&O picture not built for %s: %s", symbol, exc)
        return ctx

    def _in_session(self) -> bool:
        """Market hours on the market's own clock, from 10 minutes after the
        open (the first bars need time to print) to the close."""
        from app.core import clock
        now = clock.market_now(str(self.cfg.get("system.timezone", "Asia/Kolkata")))
        if now.weekday() >= 5:
            return False

        def hm(v: str) -> int:
            h, _, m = str(v).partition(":")
            return int(h) * 60 + int(m or 0)
        t = now.hour * 60 + now.minute
        return (hm(self.cfg.get("system.market_open", "09:15")) + 10 <= t
                < hm(self.cfg.get("system.market_close", "15:30")))

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
