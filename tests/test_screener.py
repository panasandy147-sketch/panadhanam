"""The pre-market screener (Band A/B from yesterday's EOD bars), the gate that
trades only today's list, and the IST entry windows."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.analysis import pre_market_screener as scr
from app.core.models import Bias

IST = ZoneInfo("Asia/Kolkata")
TODAY = date(2026, 9, 30)


def _daily(n=30, close=100.0, rng=4.0, vol=1_000_000, last=None, day2=None):
    """n completed daily bars ending YESTERDAY; `last`/`day2` override the last
    two (high, low, close, volume)."""
    out = []
    start = datetime(2026, 8, 1, tzinfo=IST)
    for i in range(n):
        h, lo, c, v = close + rng / 2, close - rng / 2, close, vol
        out.append(SimpleNamespace(ts=start + timedelta(days=i), open=c, high=h, low=lo,
                                   close=c, volume=v))
    for idx, over in ((-2, day2), (-1, last)):
        if over:
            h, lo, c, v = over
            out[idx] = SimpleNamespace(ts=out[idx].ts, open=c, high=h, low=lo, close=c,
                                       volume=v)
    # Relabel so the last bar is yesterday.
    shift = (datetime(2026, 9, 29, tzinfo=IST) - out[-1].ts)
    for b in out:
        b.ts = b.ts + shift
    return out


@pytest.fixture
def scfg(cfg):
    cfg.switch_market("IN")
    for s in (cfg._base_settings, cfg.settings):
        s.setdefault("screener", {})["enabled"] = True
    return cfg


# --------------------------------------------------------------------------- #
# The metrics
# --------------------------------------------------------------------------- #
def test_the_five_metrics():
    bars = _daily(last=(101.0, 99.0, 100.8, 1_500_000))      # narrow, closes near the high
    m = scr.metrics(bars)
    assert m["atr_pct"] == pytest.approx(3.87, abs=0.05)       # ~4-point ranges on 100
    assert m["rvol"] == pytest.approx(1.5, abs=0.01)
    assert m["is_nr7"] is True                                 # 2 < each prior 4
    assert m["is_inside_day"] is True                          # 101 < 102 and 99 > 98
    assert m["close_location"] == pytest.approx(0.9, abs=0.001)
    assert scr.metrics(bars[:10]) is None                      # not enough history


def test_bands_rank_and_filter():
    rows = {
        "A1": {"atr_pct": 2.5, "rvol": 2.0, "is_nr7": True, "is_inside_day": False,
               "close_location": 0.5},
        "A2": {"atr_pct": 2.1, "rvol": 1.3, "is_nr7": False, "is_inside_day": True,
               "close_location": 0.5},
        "LOWATR": {"atr_pct": 1.5, "rvol": 3.0, "is_nr7": True, "is_inside_day": True,
                   "close_location": 0.95},
        "BLONG": {"atr_pct": 3.0, "rvol": 1.8, "is_nr7": False, "is_inside_day": False,
                  "close_location": 0.85},
        "BSHORT": {"atr_pct": 3.0, "rvol": 1.6, "is_nr7": False, "is_inside_day": False,
                   "close_location": 0.10},
        "MIDCLOSE": {"atr_pct": 3.0, "rvol": 2.5, "is_nr7": False, "is_inside_day": False,
                     "close_location": 0.5},
    }
    from app.core.config import get_config
    got = scr.bands(get_config(), rows)
    assert [r["symbol"] for r in got["A"]] == ["A1", "A2"]           # by RVOL
    assert [(r["symbol"], r["side"]) for r in got["B"]] == [("BLONG", "LONG"),
                                                           ("BSHORT", "SHORT")]


@pytest.mark.asyncio
async def test_run_uses_yesterdays_bars_and_saves_todays_list(scfg):
    today_bar = SimpleNamespace(ts=datetime(2026, 9, 30, tzinfo=IST), open=100, high=150,
                                low=50, close=100, volume=99_000_000)   # a partial today

    class Broker:
        async def get_candles(self, sym, tf, count):
            if sym == "HDFCBANK":
                return _daily(last=(101.0, 99.0, 100.8, 1_500_000)) + [today_bar]
            return _daily()

    entry = await scr.run(scfg, Broker(), TODAY, symbols=["HDFCBANK", "INFY"])
    assert [r["symbol"] for r in entry["band_a"]] == ["HDFCBANK"]
    assert entry["band_a"][0]["rvol"] == pytest.approx(1.5, abs=0.01)   # not today's bar
    assert scr.todays(scfg, TODAY)["symbols"] == ["HDFCBANK"]
    assert scr.todays(scfg, TODAY + timedelta(days=1)) is None


# --------------------------------------------------------------------------- #
# The gate and the windows
# --------------------------------------------------------------------------- #
def _listed(scfg):
    scr.save({"date": TODAY.isoformat(), "market": "IN", "symbols": ["HDFCBANK", "SBIN"],
              "band_a": [{"symbol": "HDFCBANK", "band": "A", "side": "BOTH"}],
              "band_b": [{"symbol": "SBIN", "band": "B", "side": "LONG",
                          "close_location": 0.9}]})


def _at(monkeypatch, hh, mm):
    from app.core import clock
    monkeypatch.setattr(clock, "market_now",
                        lambda tz: datetime(2026, 9, 30, hh, mm, tzinfo=ZoneInfo(tz)))


@pytest.fixture
def rm(scfg, monkeypatch):
    from app.agents.risk import RiskManager
    _listed(scfg)
    return RiskManager(scfg)


PULLBACK = {"primary": {"last_close": 100.2, "vwap": 100.0, "atr": 1.0}}
EXTENDED = {"primary": {"last_close": 103.0, "vwap": 100.0, "atr": 1.0}}


def test_off_list_symbols_are_refused(rm, monkeypatch):
    _at(monkeypatch, 10, 0)
    assert any("not on today's screened watchlist" in r
               for r in rm.screener_checks("TCS", Bias.BULLISH, PULLBACK))
    assert rm.screener_checks("HDFCBANK", Bias.BULLISH, EXTENDED) == []


def test_no_list_means_no_trades(scfg, monkeypatch):
    from app.agents.risk import RiskManager
    scr.save({"date": "2026-09-29", "market": "IN", "symbols": ["HDFCBANK"],
              "band_a": [], "band_b": []})                     # yesterday's
    _at(monkeypatch, 10, 0)
    assert any("No screened watchlist for today" in r
               for r in RiskManager(scfg).screener_checks("HDFCBANK", Bias.BULLISH, {}))


@pytest.mark.parametrize("hh, mm, sym, bias, ind, blocked", [
    (9, 45, "HDFCBANK", Bias.BULLISH, EXTENDED, None),             # Band A, morning
    (9, 45, "SBIN", Bias.BULLISH, PULLBACK, "Band B enters only"),  # B waits for 13:30
    (12, 0, "HDFCBANK", Bias.BULLISH, PULLBACK, "Midday freeze"),
    (14, 0, "HDFCBANK", Bias.BULLISH, EXTENDED, "VWAP pullbacks only"),
    (14, 0, "SBIN", Bias.BULLISH, PULLBACK, None),                   # B long pullback
    (14, 0, "SBIN", Bias.BEARISH, {"primary": {"last_close": 99.8, "vwap": 100.0,
                                               "atr": 1.0}}, "longs-only"),
    (15, 0, "HDFCBANK", Bias.BULLISH, PULLBACK, "Outside the entry windows"),
])
def test_the_ist_windows(rm, monkeypatch, hh, mm, sym, bias, ind, blocked):
    _at(monkeypatch, hh, mm)
    reasons = rm.screener_checks(sym, bias, ind)
    if blocked is None:
        assert reasons == []
    else:
        assert any(blocked in r for r in reasons), reasons


def test_the_windows_are_the_ist_ones(scfg):
    assert scr.window(scfg, datetime(2026, 9, 30, 11, 14, tzinfo=IST)) == "morning"
    assert scr.window(scfg, datetime(2026, 9, 30, 11, 15, tzinfo=IST)) == "freeze"
    assert scr.window(scfg, datetime(2026, 9, 30, 13, 30, tzinfo=IST)) == "afternoon"
    assert scr.window(scfg, datetime(2026, 9, 30, 14, 45, tzinfo=IST)) == "closed"
    assert scfg.get("system.square_off_time") == "15:15"


@pytest.mark.asyncio
async def test_the_engine_cycles_only_the_list_and_stops_at_four(scfg, monkeypatch):
    from app.brokers.paper import PaperBroker
    from app.scheduler import TradingEngine
    _listed(scfg)
    _at(monkeypatch, 10, 0)
    broker = PaperBroker(config={"total_capital": 350_000})
    await broker.connect()
    eng = TradingEngine(broker, scfg)
    assert eng._screened_targets() == ["HDFCBANK", "SBIN"]
    eng.risk.state.trades_today = 4
    assert eng._screened_targets() == []


def test_the_rules_page_explains_the_screener(scfg):
    import json

    from app.core.rules import build
    text = json.dumps(build(scfg))
    assert "Band A" in text and "Midday freeze" in text and "11:15" in text
    assert "13:30" in text and "14:45" in text and "15:15" in text
