"""The gold desk: GOLDBEES (India) and GLD (US) held 1-4 days on Larry
Williams' volatility breakout — no screener, no other strategy, out at the
first session that opens in profit, the stop or target, or after
swing.max_hold_days sessions."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.core.models import (
    Bias,
    Candle,
    Instrument,
    InstrumentType,
    Quote,
    Side,
    SignalStatus,
    TradeSignal,
)
from app.strategies import vol_breakout

TZ = "Asia/Kolkata"
IST = ZoneInfo(TZ)
TODAY = date(2026, 9, 29)
PREV = {"high": 122.0, "low": 118.0, "close": 120.0}       # range 4, k 0.5 -> 2


def _daily(trend=+0.3):
    t0 = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)
    out, price, d = [], 110.0, 0
    while len(out) < 30:
        ts = t0 + timedelta(days=d)
        d += 1
        if ts.weekday() >= 5:
            continue
        out.append(Candle(ts=ts, open=price, high=price + 1, low=price - 1, close=price,
                          volume=1000))
        price += trend
    return out


def _bars(rows, start=datetime(2026, 9, 29, 3, 45, tzinfo=UTC)):       # 09:15 IST
    return [Candle(ts=start + timedelta(minutes=5 * i), open=o, high=h, low=lo, close=c,
                   volume=1000) for i, (o, h, lo, c) in enumerate(rows)]


# open 120 -> the line 122; the 3rd bar's high reaches it (09:25)
CROSS = [(120.0, 120.6, 119.8, 120.4), (120.4, 121.2, 120.2, 121.0),
         (121.0, 122.3, 120.9, 121.6)]


def test_the_gold_swing_takes_the_first_bar_whose_high_reaches_the_level(cfg):
    b = vol_breakout.detect(_bars(CROSS), PREV, {"vwap": 125.0, "ema9": 1, "ema21": 2},
                            TZ, TODAY, cfg, swing=True, daily=_daily())
    # VWAP and the EMAs are against it: the swing mode does not ask them
    assert b and b.direction == 1 and b.level == 122.0 and b.day_open == 120.0
    assert "1-4 day swing" in b.note


def test_only_with_the_20_day_trend_and_only_the_first_cross(cfg):
    assert vol_breakout.detect(_bars(CROSS), PREV, {}, TZ, TODAY, cfg, swing=True,
                               daily=_daily(-0.3)) is None
    later = CROSS + [(121.6, 122.6, 121.5, 122.4)]
    assert vol_breakout.detect(_bars(later), PREV, {}, TZ, TODAY, cfg, swing=True,
                               daily=_daily()) is None


def test_gold_is_on_both_markets_and_never_screened(cfg):
    cfg.switch_market("IN")
    assert cfg.instrument_meta("GOLDBEES").get("swing") is True
    assert cfg.instrument_meta("GOLDBEES").get("fno") is False
    cfg.switch_market("US")
    try:
        assert cfg.instrument_meta("GLD").get("swing") is True
        assert cfg.get("swing.k") == 0.3            # GLD: 0.3 since 6 Oct 2026
    finally:
        cfg.switch_market("IN")
    assert cfg.get("swing.max_hold_days") == 4 and cfg.get("swing.k") == 0.5


# --------------------------------------------------------------------------- #
def _ctx(cfg, breakout=True):
    from app.core.models import MarketContext
    bars = _bars(CROSS)
    found = vol_breakout.detect(bars, PREV, {}, TZ, TODAY, cfg, swing=True, daily=_daily())
    ctx = MarketContext(symbol="GOLDBEES", cycle_id="t", candles={"5m": bars},
                        quote=Quote(symbol="GOLDBEES", last_price=bars[-1].close))
    ctx.indicators = {"primary": {"last_close": bars[-1].close, "atr": 0.4, "vwap": 120.5,
                                  "above_vwap": True, "high": 122.5, "low": 119.5,
                                  "patterns": []},
                      "previous_day": PREV, "by_timeframe": {},
                      "vol_breakout": ({**found.to_dict(), "swing": True}
                                       if breakout and found else None)}
    ctx.__dict__["_reports"] = []
    return ctx


@pytest.fixture
def rm(cfg):
    from app.agents.risk import RiskManager
    cfg.switch_market("IN")
    m = RiskManager(cfg)
    m.cfg.settings["system"]["no_new_entry_after"] = "23:59"
    m.cfg.settings["risk"]["reentry_cooldown_minutes"] = 0
    m.set_capital(350_000)
    return m


def test_a_gold_breakout_is_taken_held_overnight_and_skips_the_screener(rm, cfg):
    from app.agents.candlestick import CandlestickAgent
    ctx = _ctx(cfg)
    report = CandlestickAgent(cfg).analyse_rules(ctx)
    assert report.extra["setup"] == "Volatility Breakout"
    sig = rm.evaluate(ctx, Bias.BULLISH, [report], 0.8, ["Volatility Breakout"])
    assert sig.hold_overnight is True
    assert sig.stop_loss == pytest.approx(120.0 - 2 * 0.01)
    assert not any("screened watchlist" in r for r in sig.rejection_reasons), \
        sig.rejection_reasons
    assert not any("gold desk" in r for r in sig.rejection_reasons)


def test_gold_trades_nothing_but_the_breakout(rm, cfg):
    from app.agents.candlestick import CandlestickAgent
    ctx = _ctx(cfg, breakout=False)
    report = CandlestickAgent(cfg).analyse_rules(ctx)
    sig = rm.evaluate(ctx, Bias.BULLISH, [report], 0.8, ["chart"])
    assert any("only the 1-4 day volatility breakout" in r for r in sig.rejection_reasons)


# --------------------------------------------------------------------------- #
class _Broker:
    def __init__(self, last):
        self.last = last

    async def get_quote(self, symbol):
        return Quote(symbol=symbol, last_price=self.last, bid=self.last - 0.01,
                     ask=self.last + 0.01)


def _open_gold(opened: datetime, entry=122.0, stop=119.99, target=128.0):
    from app.storage import db
    sig = TradeSignal(id=f"SIG-GOLD-{opened:%d%H%M}", ts=opened,
                      instrument=Instrument(symbol="GOLDBEES", tradingsymbol="GOLDBEES",
                                            instrument_type=InstrumentType.EQUITY),
                      side=Side.BUY, entry=entry, stop_loss=stop, target=target,
                      quantity=100, status=SignalStatus.OPEN, hold_overnight=True,
                      setup="Volatility Breakout")
    db.save_signal(sig)
    return sig.id


def _tracker(cfg, monkeypatch, now, last):
    from app.core import clock
    from app.learning.outcomes import OutcomeTracker
    cfg.switch_market("IN")
    monkeypatch.setattr(clock, "market_now", lambda tz: now)
    # These trades are made up: they must not reach the shared test journal,
    # whose analytics other tests check to the rupee.
    monkeypatch.setitem(cfg.settings.setdefault("journal", {}), "auto_log_live_trades",
                        False)
    return OutcomeTracker(_Broker(last), cfg)


@pytest.mark.asyncio
async def test_the_square_off_does_not_close_a_gold_swing_before_its_limit(cfg, monkeypatch):
    sid = _open_gold(datetime(2026, 9, 29, 4, 0, tzinfo=UTC))            # 09:30 IST Mon
    now = datetime(2026, 9, 29, 15, 20, tzinfo=IST)                      # past 15:15
    closed = await _tracker(cfg, monkeypatch, now, 121.0).poll()
    assert sid not in [c["signal_id"] for c in closed]


@pytest.mark.asyncio
async def test_the_first_later_session_in_profit_sells_once(cfg, monkeypatch):
    sid = _open_gold(datetime(2026, 9, 28, 4, 0, tzinfo=UTC))
    now = datetime(2026, 9, 29, 9, 20, tzinfo=IST)                       # after 09:15
    tracker = _tracker(cfg, monkeypatch, now, 123.0)
    closed = await tracker.poll()
    [c] = [c for c in closed if c["signal_id"] == sid]
    assert c["status"] == "CLOSED_TIME" and c["exit_detail"] == "first_profitable_open"
    assert "first session that opened in profit" in c["exit_reason"]


@pytest.mark.asyncio
async def test_an_open_not_in_profit_holds_and_is_judged_once_a_day(cfg, monkeypatch):
    sid = _open_gold(datetime(2026, 9, 25, 4, 0, tzinfo=UTC), entry=125.0, stop=119.99,
                     target=140.0)
    now = datetime(2026, 9, 29, 9, 20, tzinfo=IST)
    tracker = _tracker(cfg, monkeypatch, now, 124.0)
    assert sid not in [c["signal_id"] for c in await tracker.poll()]
    tracker.broker.last = 126.0                      # later the same day, in profit
    assert sid not in [c["signal_id"] for c in await tracker.poll()]


@pytest.mark.asyncio
async def test_after_four_sessions_the_square_off_closes_it(cfg, monkeypatch):
    sid = _open_gold(datetime(2026, 9, 23, 4, 0, tzinfo=UTC), entry=125.0, stop=119.99,
                     target=140.0)                                       # Wed
    now = datetime(2026, 9, 29, 15, 26, tzinfo=IST)                      # Tue: 4 sessions
    tracker = _tracker(cfg, monkeypatch, now, 124.0)
    tracker.__dict__["_fpo_judged"] = {(sid, "2026-09-29")}
    [c] = [c for c in await tracker.poll() if c["signal_id"] == sid]
    assert c["exit_detail"] == "square_off"


def test_the_scan_always_includes_gold(cfg):
    from app.scheduler import TradingEngine
    eng = TradingEngine.__new__(TradingEngine)
    eng.cfg = cfg
    cfg.switch_market("IN")

    class _Risk:
        class state:
            trades_today = 0
    eng.risk = _Risk
    assert "GOLDBEES" in eng._screened_targets()
