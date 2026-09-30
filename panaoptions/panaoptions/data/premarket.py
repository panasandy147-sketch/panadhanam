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


def _minutes(hhmm: str) -> int:
    h, _, m = str(hhmm).partition(":")
    return int(h) * 60 + int(m or 0)


def _session_fraction(now_et: datetime, market_open: str = "09:30",
                      market_close: str = "16:00") -> float:
    """How much of the market's own session (09:30-16:00 in New York, 09:15-
    15:30 on NSE) has elapsed, clamped to (0, 1]. On the wrong clock an NSE
    stock at 09:25 IST looked 5 minutes BEFORE the open, so a few minutes of
    trade read as 15x its normal day."""
    start, end = _minutes(market_open), _minutes(market_close)
    length = max(end - start, 1)
    minutes = now_et.hour * 60 + now_et.minute - start
    return max(min(minutes / length, 1.0), 1 / length)


def relative_volume(intraday: list[Candle], daily: list[Candle],
                    now_et: datetime, lookback: int = 20,
                    market_open: str = "09:30", market_close: str = "16:00") -> float:
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

    expected = (sum(history) / len(history)) * _session_fraction(now_et, market_open,
                                                                  market_close)
    return round(traded / expected, 3) if expected else 0.0


def has_volume(daily: list[Candle]) -> bool:
    """False for a symbol whose feed carries no volume at all — the NSE
    indices on Yahoo (NIFTY, BANKNIFTY, FINNIFTY) print 0 on every bar."""
    return any(float(c.volume or 0) > 0 for c in daily or [])


def gap_pct(previous_close: float, open_or_last: float) -> float:
    if not previous_close:
        return 0.0
    return round((open_or_last - previous_close) / previous_close * 100, 3)


async def screen(feed, cfg, now_et: datetime,
                 symbols: list[str] | None = None) -> list[PreMarketRead]:
    """Run the screen over the whole universe (or `symbols`), concurrently."""
    symbols = list(symbols) if symbols else cfg.symbols
    min_rvol = float(cfg.get("premarket.min_rvol", 1.5))
    min_gap = float(cfg.get("premarket.min_gap_pct", 1.0))
    lookback = int(cfg.get("technical.volume_lookback", 20))
    session = (str(cfg.get("session.market_open", "09:30")),
               str(cfg.get("session.market_close", "16:00")))

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
        read.rvol = relative_volume(intraday, daily, now_et, lookback, *session)

        gap_ok = abs(read.gap_pct) >= min_gap
        rvol_ok = read.rvol >= min_rvol
        # An index with no volume in the feed cannot show RVOL: 0.00x is "not
        # measured", not "quiet". It is judged on the gap alone.
        no_volume = not has_volume(daily) and not has_volume(intraday)
        if no_volume:
            read.rvol_unmeasured = True

        if gap_ok:
            read.reasons.append(f"gap {read.gap_pct:+.2f}% (need |{min_gap}|%)")
        else:
            read.reasons.append(
                f"gap {read.gap_pct:+.2f}% is inside the |{min_gap}|% threshold")
        if no_volume:
            read.reasons.append("RVOL not measurable (the feed carries no volume for "
                                "this index) — judged on the gap alone")
        elif rvol_ok:
            read.reasons.append(f"RVOL {read.rvol:.2f} (need {min_rvol})")
        else:
            read.reasons.append(f"RVOL {read.rvol:.2f} is below {min_rvol}")

        read.passed = gap_ok and (rvol_ok or no_volume)
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
