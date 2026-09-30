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


def test_every_unclosed_bar_goes_including_yahoos_live_point():
    t = T0 + timedelta(minutes=5)                    # 14:05 bucket forming at 14:08
    bars = _bars(1) + [Candle(ts=t, open=1, high=1, low=1, close=1),
                       Candle(ts=t + timedelta(minutes=3), open=1, high=1, low=1, close=1)]
    assert [b.ts for b in completed_bars(bars, "5m", t + timedelta(minutes=3, seconds=10))] \
        == [T0]


def test_a_stale_tape_is_emptied_in_market_hours():
    from app.data.market import stale_reason
    bars = _bars(1)                                  # closed 14:05
    assert stale_reason(bars, "5m", 3, T0 + timedelta(minutes=19)) == ""
    assert "data stale" in stale_reason(bars, "5m", 3, T0 + timedelta(minutes=25))


def test_panadhanam_candles_fall_back_to_the_second_host_then_1m():
    from app.data.feeds import yahoo

    t = int(T0.timestamp())

    def chart(stamps):
        n = len(stamps)
        return {"chart": {"result": [{"timestamp": stamps, "indicators": {"quote": [{
            "open": [1.0] * n, "high": [float(i + 2) for i in range(n)],
            "low": [0.5] * n, "close": [float(i + 1) for i in range(n)],
            "volume": [10] * n}]}}]}}

    class Resp:
        def __init__(self, payload):
            self.status_code = 200 if payload else 503
            self._p = payload

        def json(self):
            return self._p

    class Client:
        def __init__(self, answers):
            self.answers = answers

        async def get(self, url, params=None):
            host = "query1" if "query1" in url else "query2"
            return Resp(self.answers.get((host, params["interval"])))

    feed = yahoo.YahooFeed()
    feed._client = Client({("query2", "5m"): chart([t, t + 300])})
    assert len(asyncio.run(feed.get_candles("SPY", "5m"))) == 2
    assert feed.candle_source["SPY"] == "yahoo query2"
    feed._client = Client({("query1", "1m"): chart([t + 60 * i for i in range(10)])})
    bars = asyncio.run(feed.get_candles("SPY", "5m"))
    assert feed.candle_source["SPY"] == "rebuilt from 1m (5m)" and len(bars) == 2
    assert bars[0].high == 6.0 and bars[0].volume == 50
