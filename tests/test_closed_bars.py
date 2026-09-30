"""The analysts judge CLOSED intraday candles only: the feed's last bar is
still forming (Yahoo includes it), and a pattern or a volume surge read on a
minute of a five-minute bar is not the pattern the replay graded."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from app.core.models import Candle
from app.data.market import completed_bars

T0 = datetime(2026, 9, 30, 14, 0, tzinfo=UTC)


def _bars(n, step=5):
    return [Candle(ts=T0 + timedelta(minutes=step * i), open=1, high=1, low=1, close=1,
                   volume=1) for i in range(n)]


def test_a_forming_bar_is_dropped_and_a_closed_one_kept():
    bars = _bars(3)                                        # 14:00, 14:05, 14:10
    assert len(completed_bars(bars, "5m", T0 + timedelta(minutes=13))) == 2
    assert len(completed_bars(bars, "5m", T0 + timedelta(minutes=15))) == 3
    assert len(completed_bars(_bars(3, 15), "15m", T0 + timedelta(minutes=44))) == 2
    # Daily bars are left alone (the previous-day levels read completed days).
    assert len(completed_bars(bars, "1d", T0)) == 3
    assert completed_bars([], "5m", T0) == []


def test_the_context_carries_closed_bars_only(cfg, monkeypatch):
    from app.data.market import MarketDataService

    class Broker:
        supports_options = False

        async def get_quote(self, symbol):
            return None

        async def get_candles(self, symbol, tf, count):
            return _bars(40, 5 if tf == "5m" else 15)

    svc = MarketDataService(Broker(), cfg)
    now = datetime.now(UTC)
    # Shift so the last 5m bar started 2 minutes ago: still forming.
    shift = now - timedelta(minutes=2) - _bars(40)[-1].ts

    async def shifted(symbol, tf, count):
        return [b.model_copy(update={"ts": b.ts + shift}) for b in _bars(40)]

    monkeypatch.setattr(svc.broker, "get_candles", shifted)
    ctx = asyncio.run(svc.build_context("INFY", "t"))
    assert len(ctx.candles["5m"]) == 39
    cfg.settings["technical"]["completed_bars_only"] = False
    ctx = asyncio.run(svc.build_context("INFY", "t"))
    assert len(ctx.candles["5m"]) == 40
