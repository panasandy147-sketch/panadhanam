"""RSI(2) Swing Book (app/strategies/rsi2_swing.py): the indicators, the
entry and exit rules, the backtest, and the paper book's daily run."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.core.models import Candle, Quote
from app.strategies import rsi2_swing as m

ET = ZoneInfo("America/New_York")
P = {"rsi_max": 5.0, "trend_sma": 200, "exit_sma": 5, "max_hold_days": 10,
     "stop_pct": 25.0, "slots": 5, "fractional": True}


def uptrend_then_dip(n=260, dips=3):
    """A steady rise, then `dips` sharp down closes (still above SMA 200)."""
    closes = [100 + 0.2 * i for i in range(n)]
    for _ in range(dips):
        closes.append(closes[-1] * 0.97)
    return closes


def test_rsi_and_sma():
    assert m.rsi([1, 2, 3, 4, 5])[-1] == pytest.approx(100.0)
    assert m.rsi([5, 4, 3, 2, 1])[-1] == pytest.approx(0.0)
    assert m.rsi([1, 2])[1] is None
    assert m.sma([1, 2, 3, 4], 2) == [None, 1.5, 2.5, 3.5]


def test_the_entry_needs_the_uptrend_and_a_deep_two_day_dip():
    sig = m.entry_signal(uptrend_then_dip(), P)
    assert sig and sig["rsi2"] < 5 and sig["close"] > sig["sma200"]
    # One down day is not deep enough on the 2-period RSI…
    assert m.entry_signal(uptrend_then_dip(dips=0) + [uptrend_then_dip(dips=0)[-1] * 0.999], P) is None
    # …and below the 200-day average there is no long.
    falling = [300 - 0.5 * i for i in range(260)]
    assert m.entry_signal(falling, P) is None
    # Not enough history for the 200-day average.
    assert m.entry_signal(uptrend_then_dip(n=100), P) is None


def test_the_exits():
    base = [100.0] * 10
    assert m.exit_reason(base + [99.0], 99.0, 100.0, 0, P) is None          # entry day
    assert m.exit_reason(base + [101.0], 100.5, 100.0, 1, P) == ("sma5", 101.0)
    assert m.exit_reason(base + [99.0], 70.0, 100.0, 2, P) == ("stop", 75.0)
    assert m.exit_reason(base + [99.0], 98.0, 100.0, 10, P) == ("time", 99.0)
    assert m.exit_reason(base + [99.0], 98.0, 100.0, 9, P) is None
    assert m.exit_reason(base + [99.0], 70.0, 100.0, 2, {**P, "stop_pct": 0}) is None


def _candles(closes, start=date(2025, 1, 1)):
    out, d = [], start
    for c in closes:
        while d.weekday() >= 5:
            d += timedelta(days=1)
        out.append(Candle(ts=datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET),
                          open=c, high=c * 1.005, low=c * 0.995, close=c))
        d += timedelta(days=1)
    return out


def test_the_backtest_takes_the_dip_and_sells_the_bounce(cfg):
    closes = uptrend_then_dip() + [uptrend_then_dip()[-1] * 1.08, 200.0]
    out = m.backtest({"X": _candles(closes)}, cfg, "America/New_York", cost_pct=0.0)
    [t] = out["trades"]
    # In on the first 3% drop's close (RSI(2) already under 5), held through
    # the next two, out on the bounce's close above the 5-day average.
    assert t["entry"] == pytest.approx(closes[260]) and t["sessions"] == 3
    assert t["why"] == "sma5" and t["exit"] == pytest.approx(closes[263])
    assert t["ret"] == pytest.approx(closes[263] / closes[260] - 1)
    assert out["book"]["return_pct"] == pytest.approx(100 * t["ret"] / 5, abs=0.01)


def test_the_book_never_holds_more_than_its_slots():
    trades = [{"symbol": f"S{i}", "entry_day": "2025-01-02", "exit_day": "2025-01-05",
               "rsi2": float(i), "ret": 0.01, "sessions": 2} for i in range(8)]
    book = m.portfolio(trades, 5)
    assert [t["symbol"] for t in book["taken"]] == ["S0", "S1", "S2", "S3", "S4"]


# --------------------------------------------------------------------------- #
# The paper book
# --------------------------------------------------------------------------- #
class FakeBroker:
    def __init__(self, series, today):
        self.series, self.today = series, today

    async def get_candles(self, symbol, timeframe, count=200):
        closes = self.series.get(symbol)
        return _candles(closes, start=self.today - timedelta(days=int(len(closes) * 1.5)))[:-1] \
            if closes else []

    async def get_quote(self, symbol):
        closes = self.series.get(symbol)
        return Quote(symbol=symbol, last_price=closes[-1]) if closes else None


def _book(cfg, tmp_path, series, today):
    cfg.switch_market("US")
    b = m.Rsi2Book(FakeBroker(series, today), cfg, path=tmp_path / "book.json")
    return b


def test_the_daily_run_buys_then_sells_on_the_bounce(cfg, tmp_path, monkeypatch):
    from app.core.bus import bus

    async def quiet(*a, **k):
        return None
    monkeypatch.setattr(bus, "publish", quiet)
    monkeypatch.setattr(cfg, "watchlist", lambda: [{"symbol": "AAPL"}, {"symbol": "KO"}])
    dip = uptrend_then_dip()
    flat = [100.0] * 263                           # no setup
    day1 = datetime(2026, 10, 6, 15, 46, tzinfo=ET)
    try:
        book = _book(cfg, tmp_path, {"AAPL": dip, "KO": flat}, day1.date())
        assert book.due(day1) and not book.due(day1.replace(hour=15, minute=30))
        out = asyncio.run(book.maybe_run(day1))
        [pos] = out["entered"]
        assert pos["symbol"] == "AAPL" and pos["qty"] == pytest.approx(800 / dip[-1], abs=1e-3)
        assert pos["stop"] == pytest.approx(dip[-1] * 0.75, abs=1e-3)
        assert not book.due(day1)                    # once a day
        assert asyncio.run(book.maybe_run(day1)) is None

        # The next session closes above the 5-day average: sold.
        day2 = day1 + timedelta(days=1)
        book.broker = FakeBroker({"AAPL": dip + [dip[-1] * 1.08], "KO": flat + [100.0]},
                                 day2.date())
        out = asyncio.run(book.maybe_run(day2))
        [sold] = out["exited"]
        assert sold["why"] == "sma5" and sold["pnl"] > 0 and sold["ret_pct"] == pytest.approx(8.0)
        st = book.status()
        assert st["trades"] == 1 and st["win_rate"] == 100.0 and not st["open"]
        assert st["equity"] == pytest.approx(4000 + sold["pnl"], abs=0.01)
    finally:
        cfg.switch_market("IN")


def test_no_run_at_the_weekend_or_where_it_is_off(cfg, tmp_path):
    book = _book(cfg, tmp_path, {}, date(2026, 10, 10))
    try:
        assert not book.due(datetime(2026, 10, 10, 15, 50, tzinfo=ET))   # Saturday
    finally:
        cfg.switch_market("IN")
    assert not cfg.get("rsi2_swing.enabled")                              # India off
    assert not m.Rsi2Book(None, cfg, path=tmp_path / "x.json").due(
        datetime(2026, 10, 6, 15, 50, tzinfo=ET))


def test_the_us_runs_it_and_the_rules_page_says_so(cfg):
    from app.core import rules
    cfg.switch_market("US")
    try:
        assert cfg.get("rsi2_swing.enabled") is True
        assert cfg.get("rsi2_swing.stop_pct") == 25.0
        titles = [s["title"] for s in rules.build(cfg)["sections"]]
        assert any(t.startswith("RSI(2) Swing Book") for t in titles)
    finally:
        cfg.switch_market("IN")
    assert not any(t.startswith("RSI(2) Swing Book")
                   for t in (s["title"] for s in rules.build(cfg)["sections"]))
