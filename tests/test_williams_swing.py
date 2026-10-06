"""Williams-Crabel Swing Book (app/strategies/williams_swing.py): the NR day
and trend setup, the breakout trigger, the backtest's exits, and the live
paper book: a fresh entry, the first profitable open, the stop, a missed
(stale) trigger, and where it is switched on."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.core.models import Candle
from app.strategies import williams_swing as m

ET = ZoneInfo("America/New_York")
P = {"k": 0.3, "nr": 4, "trend_days": 20, "max_hold_days": 4, "risk_pct": 1.0,
     "slots": 5, "max_position_pct": 25.0, "min_risk_pct": 0.2, "fresh_bars": 2,
     "fractional": True}


def daily(n=30, start=date(2026, 8, 1), rising=True, last_range=1.0, other_range=3.0):
    out, d = [], start
    for i in range(n):
        while d.weekday() >= 5:
            d += timedelta(days=1)
        c = 100 + (i if rising else -i) * 0.5
        rng = last_range if i == n - 1 else other_range
        out.append(Candle(ts=datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET),
                          open=c, high=c + rng / 2, low=c - rng / 2, close=c))
        d += timedelta(days=1)
    return out


def test_the_setup_needs_a_narrow_day_and_a_trend():
    s = m.day_setup(daily(), 120.0, P)
    assert s and s["up"] == pytest.approx(120.3) and s["dn"] == pytest.approx(119.7)
    assert s["stop"] == 120.0 and s["slope"] > 0
    assert m.day_setup(daily(last_range=5.0), 120.0, P) is None       # not an NR4 day
    assert m.day_setup(daily(last_range=5.0), 120.0, {**P, "nr": 0})  # NR off
    assert m.day_setup(daily(n=10), 120.0, P) is None                 # too little history


def test_the_trigger_takes_the_trend_side_only():
    s = {"up": 101.0, "dn": 99.0, "slope": 1.0, "stop": 100.0}
    bars = [{"o": 100.0, "h": 100.5, "l": 98.5, "c": 99.0},       # below dn, but the trend is up
            {"o": 100.5, "h": 101.4, "l": 100.4, "c": 101.2}]
    assert m.trigger(bars, s) == (True, 101.0, 1)
    gap = [{"o": 101.8, "h": 102.0, "l": 101.5, "c": 101.9}]
    assert m.trigger(gap, s) == (True, 101.8, 0)                  # gapped through: the open
    assert m.trigger(bars, {**s, "slope": -1.0}) == (False, 99.0, 0)


def _hour(day, hour, o, h, lo, c):
    return {"d": day, "t": f"{day}T{hour:02d}", "o": o, "h": h, "l": lo, "c": c}


def test_the_backtest_exits_at_the_first_profitable_open():
    days = [f"2026-09-{d:02d}" for d in (1, 2, 3, 4, 8)]
    dl = [{"d": f"2026-08-{i:02d}", "o": 90 + i * 0.3, "h": 91.5 + i * 0.3, "l": 88.5 + i * 0.3,
           "c": 90 + i * 0.3} for i in range(1, 29)]
    dl[-1].update(h=dl[-1]["c"] + 0.5, l=dl[-1]["c"] - 0.5)          # NR4 the day before
    dl += [{"d": d, "o": 100, "h": 101, "l": 99, "c": 100} for d in days]
    hourly = [_hour(days[0], 10, 100.0, 100.2, 99.9, 100.1),
              _hour(days[0], 11, 100.1, 100.6, 100.05, 100.5),     # through 100.3: long
              _hour(days[1], 10, 100.9, 101.0, 100.8, 100.9)]      # opens above the entry
    [t] = m.symbol_trades("X", dl, hourly, P)
    assert t["long"] and t["entry"] == pytest.approx(100.3)
    assert t["why"] == "first_profitable_open" and t["exit"] == 100.9
    assert t["r"] == pytest.approx((100.9 - 100.3) / 0.3)


def test_the_book_never_holds_more_than_its_slots():
    tr = [{"symbol": f"S{i}", "day": "2026-09-01", "exit_day": "2026-09-03", "t": f"{i}",
           "entry": 100.0, "risk": 1.0, "r": 1.0} for i in range(8)]
    assert m.book(tr, P)["trades"] == 5


# --------------------------------------------------------------------------- #
# The live paper book
# --------------------------------------------------------------------------- #
class Broker:
    def __init__(self, daily_bars, five):
        self.daily_bars, self.five = daily_bars, five

    async def get_candles(self, symbol, tf, count=200):
        return self.daily_bars.get(symbol, []) if tf == "1d" else self.five.get(symbol, [])


def five(day, bars):
    out = []
    t = datetime(day.year, day.month, day.day, 9, 30, tzinfo=ET)
    for o, h, lo, c in bars:
        out.append(Candle(ts=t, open=o, high=h, low=lo, close=c))
        t += timedelta(minutes=5)
    return out


def _book(cfg, tmp_path, broker):
    cfg.switch_market("US")
    return m.WilliamsBook(broker, cfg, path=tmp_path / "w.json")


@pytest.fixture
def quiet_bus(monkeypatch):
    from app.core.bus import bus

    async def nothing(*a, **k):
        return None
    monkeypatch.setattr(bus, "publish", nothing)


def test_a_fresh_breakout_is_bought_and_sold_at_the_next_profitable_open(
        cfg, tmp_path, monkeypatch, quiet_bus):
    monkeypatch.setattr(cfg, "watchlist", lambda: [{"symbol": "AAPL"}])
    d1, d2 = date(2026, 10, 6), date(2026, 10, 7)
    hist = daily(start=date(2026, 8, 20))                     # NR4, rising, ends 2 Oct
    today = five(d1, [(120.0, 120.1, 119.95, 120.05), (120.05, 120.4, 120.0, 120.35)])
    book = _book(cfg, tmp_path, Broker({"AAPL": hist}, {"AAPL": today}))
    try:
        out = asyncio.run(book.run(datetime(2026, 10, 6, 9, 41, tzinfo=ET)))
        [pos] = out["entered"]
        assert pos["side"] == "LONG" and pos["entry"] == pytest.approx(120.3)
        assert pos["stop"] == 120.0
        # 1% of $4,000 = $40 at risk over $0.30 -> 133 shares, capped at 25% = $1,000.
        assert pos["qty"] == pytest.approx(1000 / 120.3, abs=1e-3)
        book.broker = Broker({"AAPL": hist + [hist[-1]]},
                             {"AAPL": five(d2, [(121.0, 121.2, 120.9, 121.1)])})
        out = asyncio.run(book.run(datetime(2026, 10, 7, 9, 36, tzinfo=ET)))
        [sold] = out["exited"]
        assert sold["why"] == "first_profitable_open" and sold["exit"] == 121.0
        assert sold["pnl"] > 0 and book.status()["trades"] == 1
    finally:
        cfg.switch_market("IN")


def test_the_stop_at_the_open_and_a_stale_trigger(cfg, tmp_path, monkeypatch, quiet_bus):
    monkeypatch.setattr(cfg, "watchlist", lambda: [{"symbol": "AAPL"}, {"symbol": "MSFT"}])
    d1 = date(2026, 10, 6)
    hist = daily(start=date(2026, 8, 20))
    bars = five(d1, [(120.0, 120.1, 119.95, 120.05), (120.05, 120.4, 120.0, 120.35)])
    stale = five(d1, [(120.0, 120.4, 119.95, 120.35)] + [(120.3, 120.35, 120.25, 120.3)] * 4)
    book = _book(cfg, tmp_path, Broker({"AAPL": hist, "MSFT": hist},
                                       {"AAPL": bars, "MSFT": stale}))
    try:
        out = asyncio.run(book.run(datetime(2026, 10, 6, 9, 56, tzinfo=ET)))
        assert [p["symbol"] for p in out["entered"]] == ["AAPL"]      # MSFT ran 4 bars ago
        book.broker.five["AAPL"] = bars + [
            Candle(ts=bars[-1].ts + timedelta(minutes=5), open=120.3, high=120.3,
                   low=119.9, close=119.95)]
        out = asyncio.run(book.run(datetime(2026, 10, 6, 10, 6, tzinfo=ET)))
        [sold] = out["exited"]
        assert sold["why"] == "stop" and sold["exit"] == 120.0 and sold["r"] == pytest.approx(-1.0)
        # Not bought again the same day.
        assert asyncio.run(book.run(datetime(2026, 10, 6, 10, 16, tzinfo=ET)))["entered"] == []
    finally:
        cfg.switch_market("IN")


def test_us_trial_on_india_off_and_the_rules_page(cfg):
    from app.core import rules
    cfg.switch_market("US")
    try:
        assert cfg.get("williams_swing.enabled") is True
        assert cfg.get("williams_swing.k") == 0.3 and cfg.get("williams_swing.nr") == 4
        titles = [s["title"] for s in rules.build(cfg)["sections"]]
        assert any(t.startswith("Williams-Crabel Swing Book") for t in titles)
    finally:
        cfg.switch_market("IN")
    assert not cfg.get("williams_swing.enabled")
    assert asyncio.run(m.WilliamsBook(None, cfg).maybe_run(
        datetime(2026, 10, 6, 10, 0, tzinfo=ET))) is None


def test_longs_only_skips_a_falling_trend():
    """India's cash market cannot carry a short overnight (williams_swing.longs_only)."""
    down = daily(rising=False)
    assert m.day_setup(down, 120.0, P)["slope"] < 0
    assert m.day_setup(down, 120.0, {**P, "longs_only": True}) is None
