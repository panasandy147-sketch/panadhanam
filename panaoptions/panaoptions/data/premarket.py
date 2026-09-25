"""Pre-market screen: relative volume and a catalyst gap.

RVOL here is the session's volume so far against the same symbol's recent
average for a full day, scaled to how much of the session has elapsed. The
scaling matters: 10% of yesterday's volume is enormous at 09:40 and dismal at
15:50, and an unscaled ratio would call every morning quiet.
"""
from __future__ import annotations

import asyncio
from datetime import datetime

from panaoptions.engine import indicators as ta
from panaoptions.logging import get_logger
from panaoptions.models import Candle, PreMarketRead

log = get_logger("premarket")


def _session_fraction(now_et: datetime) -> float:
    """How much of the 09:30-16:00 session has elapsed, clamped to (0, 1]."""
    minutes = (now_et.hour - 9) * 60 + now_et.minute - 30
    return max(min(minutes / 390.0, 1.0), 1 / 390.0)


def relative_volume(intraday: list[Candle], daily: list[Candle],
                    now_et: datetime, lookback: int = 20) -> float:
    """Today's pace against the average day, adjusted for the time of day."""
    if not intraday or len(daily) < 2:
        return 0.0

    today = now_et.date()
    traded = sum(c.volume for c in intraday if c.ts.astimezone(now_et.tzinfo).date() == today)
    if not traded:
        return 0.0

    history = [c.volume for c in daily[:-1] if c.volume > 0][-lookback:]
    if not history:
        return 0.0

    expected = (sum(history) / len(history)) * _session_fraction(now_et)
    return round(traded / expected, 3) if expected else 0.0


def gap_pct(previous_close: float, open_or_last: float) -> float:
    if not previous_close:
        return 0.0
    return round((open_or_last - previous_close) / previous_close * 100, 3)


async def screen(feed, cfg, now_et: datetime) -> list[PreMarketRead]:
    """Run the screen over the whole universe, concurrently."""
    symbols = cfg.symbols
    min_rvol = float(cfg.get("premarket.min_rvol", 1.5))
    min_gap = float(cfg.get("premarket.min_gap_pct", 1.0))
    lookback = int(cfg.get("technical.volume_lookback", 20))

    async def one(symbol: str) -> PreMarketRead:
        quote, intraday, daily = await asyncio.gather(
            feed.quote(symbol),
            feed.candles(symbol, "5m", include_prepost=True),
            feed.candles(symbol, "1d"),
            return_exceptions=True)

        read = PreMarketRead(symbol=symbol)
        if isinstance(quote, Exception) or not quote:
            read.reasons.append("no quote available")
            return read
        if isinstance(intraday, Exception):
            intraday = []
        if isinstance(daily, Exception):
            daily = []

        previous_close = float(quote.get("previous_close") or 0.0)
        last = float(quote.get("pre_market_price")
                     or quote.get("last_price") or 0.0)
        read.previous_close = previous_close
        read.last_price = last

        if not previous_close or not last:
            read.reasons.append("no previous close to measure a gap against")
            return read

        read.gap_pct = gap_pct(previous_close, last)
        read.rvol = relative_volume(intraday, daily, now_et, lookback)

        gap_ok = abs(read.gap_pct) >= min_gap
        rvol_ok = read.rvol >= min_rvol

        if gap_ok:
            read.reasons.append(f"gap {read.gap_pct:+.2f}% (need |{min_gap}|%)")
        else:
            read.reasons.append(
                f"gap {read.gap_pct:+.2f}% is inside the |{min_gap}|% threshold")
        if rvol_ok:
            read.reasons.append(f"RVOL {read.rvol:.2f} (need {min_rvol})")
        else:
            read.reasons.append(f"RVOL {read.rvol:.2f} is below {min_rvol}")

        read.passed = gap_ok and rvol_ok
        if not read.passed and bool(cfg.get("flow.pass_screen", True)):
            # Unusual options activity is a catalyst of its own: new positions
            # opened in size, often before the stock gaps or wakes up.
            try:
                from panaoptions.engine import flow as flow_mod
                chain = await feed.chain_for_window(
                    symbol, last, 0, int(cfg.get("flow.max_dte", 60)))
                seen = flow_mod.scan(chain, cfg)
                if seen.found:
                    read.passed = True
                    read.reasons.append(f"passed on {seen.headline()}")
            except Exception as exc:          # a flow check never breaks a screen
                log.debug("%s: flow check failed: %s", symbol, exc)
        return read

    reads = await asyncio.gather(*[one(s) for s in symbols])
    passed = [r.symbol for r in reads if r.passed]
    log.info("pre-market screen: %d/%d passed%s", len(passed), len(symbols),
             f" ({', '.join(passed)})" if passed else "")
    return list(reads)
