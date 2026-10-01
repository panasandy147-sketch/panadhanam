"""Larry Williams' volatility breakout (1987 Robbins World Cup), panadhanam.

  * the FIRST closed 5m candle beyond today's open +/- k x yesterday's range,
    with VWAP and the 9/21 EMAs agreeing — a level crossed earlier is a chase;
  * scored by the candlestick analyst, invalidation at today's open;
  * stop exactly 2 ticks beyond the open, target 3R, no room check, no
    time stop;
  * on for the US, off for India (each on its own walk-forward).
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from app.agents.candlestick import CandlestickAgent
from app.agents.risk import RiskManager
from app.core.models import Bias, Candle, MarketContext, Quote, SignalStatus
from app.strategies import vol_breakout

TZ = "Asia/Kolkata"
TODAY = date(2026, 9, 29)
PREV = {"high": 1020.0, "low": 1000.0, "close": 1012.0}      # range 20, k 0.5 -> 10
PRIMARY = {"vwap": 1012.0, "ema9": 1015.0, "ema21": 1013.0, "atr": 4.0}


def _bars(closes, start=datetime(2026, 9, 29, 3, 45, tzinfo=UTC)):
    """Today's 5m bars from 09:15 IST, opening at 1010."""
    out, prev = [], 1010.0
    for i, c in enumerate(closes):
        out.append(Candle(ts=start + timedelta(minutes=5 * i), open=prev,
                          high=max(prev, c) + 0.5, low=min(prev, c) - 0.5, close=c,
                          volume=1000))
        prev = c
    return out


# open 1010, the line at 1020; the 8th bar (09:50) closes through it
CROSS_UP = [1011.0, 1012.5, 1014.0, 1015.5, 1017.0, 1018.0, 1019.0, 1021.5]
CROSS_DOWN = [1009.0, 1007.5, 1006.0, 1004.5, 1003.0, 1002.0, 1001.0, 998.5]


def _detect(cfg, closes, primary=None):
    return vol_breakout.detect(_bars(closes), PREV, {**PRIMARY, **(primary or {})},
                               TZ, TODAY, cfg)


@pytest.fixture
def on(cfg):
    cfg.settings.setdefault("vol_breakout", {})["enabled"] = True
    yield cfg
    cfg.settings["vol_breakout"]["enabled"] = False


# --------------------------------------------------------------------------- #
def test_the_first_close_above_open_plus_half_the_range_is_a_long(on):
    b = _detect(on, CROSS_UP)
    assert b and b.direction == 1 and b.level == 1020.0 and b.day_open == 1010.0
    assert "yesterday's range 20.00" in b.note


def test_the_mirror_below_is_a_short(on):
    b = _detect(on, CROSS_DOWN, {"vwap": 1008.0, "ema9": 1003.0, "ema21": 1005.0})
    assert b and b.direction == -1 and b.level == 1000.0


def test_a_level_crossed_earlier_is_a_chase(on):
    assert _detect(on, CROSS_UP[:-2] + [1021.0, 1022.5]) is None


def test_vwap_and_the_emas_must_agree(on):
    assert _detect(on, CROSS_UP, {"vwap": 1025.0}) is None
    assert _detect(on, CROSS_UP, {"ema9": 1012.0, "ema21": 1013.0}) is None


def test_nothing_in_the_first_half_hour(on):
    # the same cross on the 09:30 bar (4th) — inside the first 30 minutes
    assert _detect(on, [1014.0, 1017.0, 1019.0, 1021.0]) is None


def test_k_comes_from_the_config(on):
    on.settings["vol_breakout"]["k"] = 0.25                  # line at 1015
    try:
        b = _detect(on, [1011.0, 1012.0, 1013.0, 1013.5, 1014.0, 1014.5, 1014.8, 1015.6])
        assert b and b.level == 1015.0
    finally:
        on.settings["vol_breakout"]["k"] = 0.5


# --------------------------------------------------------------------------- #
def _ctx(cfg, closes=CROSS_UP, prev=None):
    bars = _bars(closes)
    close = bars[-1].close
    primary = {**PRIMARY, "last_close": close, "above_vwap": close > PRIMARY["vwap"],
               "high": close * 1.01, "low": close * 0.99, "patterns": []}
    found = vol_breakout.detect(bars, PREV, primary, TZ, TODAY, cfg)
    ctx = MarketContext(symbol="RELIANCE", cycle_id="t", candles={"5m": bars},
                        quote=Quote(symbol="RELIANCE", last_price=close))
    ctx.indicators = {"primary": primary, "previous_day": prev or PREV,
                      "vol_breakout": found.to_dict() if found else None,
                      "by_timeframe": {}}
    ctx.__dict__["_reports"] = []
    return ctx


def test_the_analyst_scores_it_and_names_todays_open(on):
    report = CandlestickAgent(on).analyse_rules(_ctx(on))
    assert report.score == pytest.approx(0.9) and report.bias == Bias.BULLISH
    assert report.extra["setup"] == "Volatility Breakout"
    assert report.invalidation_level == 1010.0


def test_switched_off_it_is_an_ordinary_chart_read(cfg):
    ctx = _ctx(cfg)
    ctx.indicators["vol_breakout"] = {"direction": 1, "day_open": 1010.0}
    report = CandlestickAgent(cfg).analyse_rules(ctx)
    assert (report.extra or {}).get("setup") != "Volatility Breakout"


@pytest.fixture
def rm(on):
    m = RiskManager(on)
    m.cfg.settings["system"]["no_new_entry_after"] = "23:59"
    m.cfg.settings["risk"]["reentry_cooldown_minutes"] = 0
    m.set_capital(100_000)
    return m


def test_the_stop_is_two_ticks_beyond_the_open_and_the_target_3r(rm, on):
    ctx = _ctx(on)
    reports = [CandlestickAgent(on).analyse_rules(ctx)]
    sig = rm.evaluate(ctx, Bias.BULLISH, reports, 0.8, ["Volatility Breakout"])
    tick = float(on.instrument_meta("RELIANCE").get("tick_size") or 0.05)
    assert sig.stop_loss == pytest.approx(1010.0 - 2 * tick)
    risk = sig.entry - sig.stop_loss
    assert sig.target == pytest.approx(sig.entry + 3 * risk, abs=0.02)
    assert sig.setup == "Volatility Breakout"
    assert "beyond today's open" in sig.rationale


def test_a_breakout_is_not_refused_for_room_to_the_previous_high(rm, on):
    # the PDH just ahead (0.3R of room): an ordinary trade is refused for it
    ctx = _ctx(on, prev=dict(PREV, high=1025.0))
    reports = [CandlestickAgent(on).analyse_rules(ctx)]
    sig = rm.evaluate(ctx, Bias.BULLISH, reports, 0.8, ["Volatility Breakout"])
    assert not any("Reward:risk" in r or "previous-day" in r for r in sig.rejection_reasons)
    # (other suites may hold RELIANCE in the shared paper book: the one-per-
    # symbol rule is theirs, not this test's)
    assert all("Already holding" in r for r in sig.rejection_reasons) or \
        sig.status == SignalStatus.APPROVED, sig.rejection_reasons


def test_a_breakout_is_never_time_stopped(cfg):
    assert "Volatility Breakout" in cfg.get("risk.no_time_stop_setups")


def test_on_for_the_us_off_for_india(cfg):
    cfg.switch_market("US")
    try:
        assert cfg.get("vol_breakout.enabled") is True and cfg.get("vol_breakout.k") == 0.5
        assert cfg.get("screener.band_a.min_atr_pct") == 1.0
        assert cfg.get("screener.band_b.min_atr_pct") == 1.0
    finally:
        cfg.switch_market("IN")
    assert cfg.get("vol_breakout.enabled") is False
    assert cfg.get("screener.band_a.min_atr_pct") == 2.0
