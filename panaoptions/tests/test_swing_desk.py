"""The swing desk (profile swing): Larry Williams' volatility breakout held
1-4 days on 21-45 day options, exited at the first profitable open, the stop
on the underlying, or after swing.max_hold_days sessions."""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from panaoptions.engine import indicators as ta
from panaoptions.engine.strategies import ALL, VolatilityBreakout, evaluate_all, localise
from panaoptions.models import (
    Candle,
    Direction,
    ExitReason,
    OptionContract,
    OptionRight,
    PaperTrade,
    SessionLevels,
    SetupType,
)

ET = ZoneInfo("America/New_York")


def _swing(cfg):
    cfg.data["swing"] = {"enabled": True, "strategies": ["volatility_breakout"], "k": 0.5,
                         "trend_days": 20, "first_profitable_open": True,
                         "max_hold_days": 4, "min_dte": 21, "max_dte": 45}
    cfg.data["strategies"]["volatility_breakout"].update(enabled=True, room_check=False)
    return cfg


def _history(trend=+1.0, today=None):
    """25 prior sessions drifting `trend` a day, then today's bars."""
    out, day0 = [], datetime(2026, 8, 24, 13, 30, tzinfo=UTC)
    d, price = 0, 100.0
    while len(out) < 25 * 6:
        start = day0 + timedelta(days=d)
        d += 1
        if start.weekday() >= 5:
            continue
        for i in range(6):
            out.append(Candle(ts=start + timedelta(minutes=5 * i), open=price, high=price + 0.3,
                              low=price - 0.3, close=price, volume=1000))
        price += trend
    t0 = datetime(2026, 9, 29, 13, 30, tzinfo=UTC)
    for i, (o, h, lo, c) in enumerate(today or []):
        out.append(Candle(ts=t0 + timedelta(minutes=5 * i), open=o, high=h, low=lo, close=c,
                          volume=1000))
    return out


LEVELS = SessionLevels(previous_high=102.0, previous_low=98.0, previous_close=100.0)


def _eval(cfg, candles, levels=LEVELS):
    df5 = localise(ta.to_frame(candles), "America/New_York")
    return VolatilityBreakout(cfg).evaluate("SPY", df5, ta.resample(df5, "15min"), levels)


# open 100, range 4, k 0.5 -> the line 102; the 3rd bar's high reaches it
BREAK = [(100.0, 100.6, 99.8, 100.4), (100.4, 101.2, 100.2, 101.0),
         (101.0, 102.3, 100.9, 101.6)]


def test_the_swing_breakout_takes_the_first_bar_whose_high_reaches_the_level(cfg):
    s = _eval(_swing(cfg), _history(+0.5, BREAK))
    assert s.triggered and s.direction is Direction.LONG and s.key_level == 102.0
    assert s.underlying_support == 100.0                       # the stop: today's open
    assert (s.min_dte_override, s.max_dte_override) == (21, 45)
    assert "swing" in s.pattern and any("held overnight" in c for c in s.confirmations)


def test_only_with_the_20_day_trend(cfg):
    s = _eval(_swing(cfg), _history(-0.5, BREAK))
    assert not s.triggered


def test_a_level_already_reached_earlier_today_is_not_taken_again(cfg):
    later = BREAK + [(101.6, 102.5, 101.5, 102.2)]
    assert not _eval(_swing(cfg), _history(+0.5, later)).triggered


def test_the_swing_desk_runs_only_its_own_strategies(cfg):
    _swing(cfg)
    for key in ("orb_vwap", "vwap_ema_pullback", "pd_liquidity_sweep"):
        cfg.data["strategies"][key]["enabled"] = True
    _, attempts = evaluate_all("SPY", _history(+0.5, BREAK), LEVELS, cfg)
    assert {a.strategy for a in attempts} <= {SetupType.VOLATILITY_BREAKOUT}
    assert VolatilityBreakout in ALL


def test_the_profile_as_shipped(tmp_path, monkeypatch):
    from panaoptions import config as config_mod
    monkeypatch.setattr(config_mod, "ENV_PATH", tmp_path / "absent.env")
    for market in ("US", "IN"):
        c = config_mod.Config(market=market, profile="swing")
        assert c.get("swing.enabled") is True and c.get("swing.k") == 0.5
        assert c.get("swing.strategies") == ["volatility_breakout"]
        assert c.get("strategies.volatility_breakout.enabled") is True
    assert config_mod.Config().get("swing.enabled") in (None, False)


def test_a_second_desk_keeps_its_own_book_and_journal():
    code = ("from panaoptions import config; from panaoptions.journal import store; "
            "print(config.DATA_DIR.name, store.JOURNAL_DIR.name, config.LEARNED_PATH.name)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env={**os.environ, "PANAOPTIONS_DESK_DIR": "swing"},
                         cwd=os.path.dirname(os.path.dirname(__file__)))
    assert out.stdout.split() == ["swing", "swing", "learned-swing.yaml"], out.stderr


# --------------------------------------------------------------------------- #
def _trade(opened, entry=102.0, stop=100.0, swing=True):
    return PaperTrade(id="PT-SWING", signal_id="S", symbol="SPY", direction=Direction.LONG,
                      contract_label="SPY 2026-10-30 102C", opened_at=opened, quantity=1,
                      entry_price=3.0, stop_price=1.2, target_1=9.0, target_2=12.0,
                      underlying_support=stop, strategy=SetupType.VOLATILITY_BREAKOUT,
                      remaining=1, underlying_entry=entry, risk_r=abs(entry - stop),
                      hold_overnight=swing)


def _desk(cfg, monkeypatch, now):
    from panaoptions import clock
    from panaoptions.app import OptionsDesk
    from tests.test_app import FakeFeed
    monkeypatch.setattr(clock, "now", lambda tz: now)
    desk = OptionsDesk(cfg=_swing(cfg), feed=FakeFeed())

    async def price(trade):
        return 3.5
    monkeypatch.setattr(desk, "_contract_price", price)
    return desk


def _bars(day, first_open):
    t0 = datetime(day.year, day.month, day.day, 13, 30, tzinfo=UTC)
    return [Candle(ts=t0, open=first_open, high=first_open + 0.5, low=first_open - 0.5,
                   close=first_open, volume=1000)]


def test_the_first_later_open_in_profit_sells(cfg, monkeypatch):
    now = datetime(2026, 9, 30, 9, 40, tzinfo=ET)
    desk = _desk(cfg, monkeypatch, now)
    trade = _trade(datetime(2026, 9, 29, 10, 0, tzinfo=ET))
    desk.ledger.open_trades[trade.id] = trade
    fill = desk._first_profitable_open(trade, _bars(now, 102.6), 3.5, now)
    assert fill and not trade.is_open
    assert trade.exit_reason is ExitReason.FIRST_PROFITABLE_OPEN


def test_an_open_not_in_profit_keeps_holding_and_is_judged_once(cfg, monkeypatch):
    now = datetime(2026, 9, 30, 9, 40, tzinfo=ET)
    desk = _desk(cfg, monkeypatch, now)
    trade = _trade(datetime(2026, 9, 29, 10, 0, tzinfo=ET))
    desk.ledger.open_trades[trade.id] = trade
    assert desk._first_profitable_open(trade, _bars(now, 101.5), 3.5, now) is None
    assert trade.is_open and trade.fpo_checked == "2026-09-30"
    # later the same day it trades higher: only the OPEN counts
    assert desk._first_profitable_open(trade, _bars(now, 103.0), 3.5, now) is None
    # nor is the entry day's own open ever judged
    same_day = _trade(datetime(2026, 9, 30, 9, 50, tzinfo=ET))
    assert desk._first_profitable_open(same_day, _bars(now, 103.0), 3.5, now) is None


def test_the_close_keeps_a_swing_trade_until_its_hold_limit(cfg, monkeypatch):
    import asyncio
    now = datetime(2026, 9, 30, 16, 0, tzinfo=ET)
    desk = _desk(cfg, monkeypatch, now)
    held = _trade(datetime(2026, 9, 29, 10, 0, tzinfo=ET))           # 1 session
    old = _trade(datetime(2026, 9, 24, 10, 0, tzinfo=ET))            # 4 sessions
    old.id, old.contract_label = "PT-OLD", "SPY 2026-10-30 101C"
    day = _trade(datetime(2026, 9, 30, 10, 0, tzinfo=ET), swing=False)
    day.id, day.contract_label = "PT-DAY", "SPY 2026-10-02 102C"
    for t in (held, old, day):
        desk.ledger.open_trades[t.id] = t
    asyncio.run(desk._square_off(now))
    assert held.is_open
    assert old.exit_reason is ExitReason.TIME_EXIT
    assert day.exit_reason is ExitReason.DAY_END


def test_a_swing_trade_never_scales_out_intraday(cfg):
    from panaoptions.ledger.paper import PaperLedger
    from panaoptions.risk.guardrails import RiskManager
    cfg.data["risk"].update(exit_style="r_multiple", scale_out_r=1.5)
    ledger = PaperLedger(cfg, RiskManager(cfg))
    trade = _trade(datetime(2026, 9, 29, 10, 0, tzinfo=ET))
    trade.quantity = trade.remaining = 4
    ledger.open_trades[trade.id] = trade
    fills = ledger.mark(trade.id, 5.0, 106.0, datetime(2026, 9, 29, 11, 0, tzinfo=ET))
    assert fills == [] and trade.remaining == 4 and not trade.breakeven_armed


def test_a_new_day_keeps_the_carried_positions_on_the_books(cfg, monkeypatch):
    import asyncio
    now = datetime(2026, 9, 30, 9, 0, tzinfo=ET)
    desk = _desk(cfg, monkeypatch, now)
    trade = _trade(datetime(2026, 9, 29, 10, 0, tzinfo=ET))
    desk.ledger.open_trades[trade.id] = trade
    desk.risk.state.date = "2026-09-29"
    asyncio.run(desk.cycle())
    assert desk.risk.state.date == "2026-09-30"
    assert desk.risk.state.open_trades == 1 and desk.risk.state.deployed > 0


def test_the_fill_is_flagged_for_overnight_only_on_the_swing_desk(cfg):
    from panaoptions.ledger.paper import PaperLedger
    from panaoptions.models import Signal
    from panaoptions.risk.guardrails import RiskManager
    c = OptionContract(symbol="SPY", right=OptionRight.CALL, strike=102, expiry="2026-10-30",
                       dte=31, bid=2.9, ask=3.1, delta=0.5)
    sig = Signal(id="S", ts=datetime(2026, 9, 29, 10, 0, tzinfo=ET), symbol="SPY",
                 direction=Direction.LONG, contract=c, quantity=1, entry_price=3.0,
                 stop_price=1.2, target_1=4, target_2=5, underlying_at_entry=102,
                 underlying_support=100)
    assert not PaperLedger(cfg, RiskManager(cfg)).open(sig).hold_overnight
    assert PaperLedger(_swing(cfg), RiskManager(cfg)).open(sig).hold_overnight


def test_the_rules_page_describes_the_swing_desk(cfg):
    from panaoptions import rules
    titles = [s["title"] for s in rules.build(_swing(cfg))["sections"]]
    assert titles[0] == "The swing desk (1-4 day options)"
    cfg.data["swing"]["enabled"] = False
    assert "The swing desk (1-4 day options)" not in [
        s["title"] for s in rules.build(cfg)["sections"]]


def test_the_backtest_walks_entry_stop_and_the_first_profitable_open():
    from panaoptions import swing_backtest as sb

    def bar(d, hh, o, h, lo, c):
        return {"ts": datetime(2026, 9, d, hh), "d": f"2026-09-{d:02d}", "o": o, "h": h,
                "l": lo, "c": c}
    daily = [{"d": f"2026-08-{i:02d}", "o": 90 + i * 0.3, "h": 91 + i * 0.3,
              "l": 89 + i * 0.3, "c": 90 + i * 0.3} for i in range(1, 30)]
    daily += [{"d": "2026-09-01", "o": 99, "h": 102, "l": 98, "c": 100},   # range 4
              {"d": "2026-09-02", "o": 100, "h": 103, "l": 99.5, "c": 102.5},
              {"d": "2026-09-03", "o": 103, "h": 104, "l": 102.5, "c": 103.5}]
    intraday = [bar(2, 10, 100, 101, 99.8, 100.8), bar(2, 11, 100.8, 102.4, 100.6, 102.2),
                bar(2, 12, 102.2, 103, 101.9, 102.5),
                bar(3, 10, 103, 104, 102.5, 103.5)]                   # opens in profit
    [t] = sb.run(daily, intraday, k=0.5, trend=True)
    assert t["long"] and t["how"] == "FIRST_PROFITABLE_OPEN" and t["held"] == 1
    assert round(t["ur"], 2) == 0.5          # entry 102, stop 100, out at 103
    assert t["or"] is not None and t["or"] > 0


def test_the_swing_desk_hunts_its_whole_list_without_the_screen(cfg, monkeypatch):
    import asyncio

    from panaoptions.models import PreMarketRead
    now = datetime(2026, 9, 30, 10, 30, tzinfo=ET)
    desk = _desk(cfg, monkeypatch, now)
    cfg.data["universe"]["symbols"] = ["SPY", "QQQ", "AAPL"]
    desk.screened = [PreMarketRead(symbol=s, passed=False) for s in ("SPY", "QQQ", "AAPL")]
    seen = []

    async def tape(symbol, now):
        seen.append(symbol)
        raise RuntimeError("no tape in this test")   # read, then skipped
    monkeypatch.setattr(desk, "_tape", tape)
    asyncio.run(desk._hunt(now))
    assert sorted(seen) == ["AAPL", "QQQ", "SPY"]
    # with the screen asked for, nothing that failed it is read
    cfg.data["swing"]["use_screen"] = True
    seen.clear()
    asyncio.run(desk._hunt(now))
    assert seen == []
