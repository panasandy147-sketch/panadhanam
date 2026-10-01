"""The swing book, inside the 0DTE desk (swing.enabled) or alone (profile
swing): Larry Williams' volatility breakout held 1-4 days on 21-45 day
options, exited at the first profitable open, the stop on the underlying, or
after swing.max_hold_days sessions — beside the same-day strategies, with its
own slots, and parked in its own market's book across an Auto switch."""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from panaoptions.engine import indicators as ta
from panaoptions.engine.strategies import (
    ALL,
    SwingBreakout,
    VolatilityBreakout,
    evaluate_all,
    evaluate_swing,
    localise,
)
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
    cfg.data["swing"] = {"enabled": True, "only": False, "symbols": [], "max_open": 2,
                         "k": 0.5, "trend_days": 20, "from": "09:45", "to": "15:30",
                         "first_profitable_open": True, "max_hold_days": 4,
                         "min_dte": 21, "max_dte": 45, "min_delta": 0.40,
                         "max_delta": 0.55, "max_contract_price": 20.0,
                         "disaster_stop_pct": 60.0}
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
    return SwingBreakout(cfg).evaluate("SPY", df5, ta.resample(df5, "15min"), levels)


# open 100, range 4, k 0.5 -> the line 102; the 3rd bar's high reaches it
BREAK = [(100.0, 100.6, 99.8, 100.4), (100.4, 101.2, 100.2, 101.0),
         (101.0, 102.3, 100.9, 101.6)]


def test_the_swing_breakout_takes_the_first_bar_whose_high_reaches_the_level(cfg):
    s = _eval(_swing(cfg), _history(+0.5, BREAK))
    assert s.triggered and s.direction is Direction.LONG and s.key_level == 102.0
    assert s.underlying_support == 100.0                       # the stop: today's open
    assert (s.min_dte_override, s.max_dte_override) == (21, 45)
    assert s.swing and s.strategy is SetupType.SWING_BREAKOUT
    assert s.delta_band == (0.40, 0.55)
    assert "swing" in s.pattern and any("held overnight" in c for c in s.confirmations)


def test_only_with_the_20_day_trend(cfg):
    s = _eval(_swing(cfg), _history(-0.5, BREAK))
    assert not s.triggered


def test_a_level_already_reached_earlier_today_is_not_taken_again(cfg):
    later = BREAK + [(101.6, 102.5, 101.5, 102.2)]
    assert not _eval(_swing(cfg), _history(+0.5, later)).triggered


def test_the_swing_book_is_its_own_check_beside_the_day_strategies(cfg):
    _swing(cfg)
    # the same-day check never runs the swing breakout ...
    _, attempts = evaluate_all("SPY", _history(+0.5, BREAK), LEVELS, cfg)
    assert SetupType.SWING_BREAKOUT not in {a.strategy for a in attempts}
    assert SwingBreakout not in ALL and VolatilityBreakout in ALL
    # ... the swing book's own check does (in its window), and is off unless
    # swing.enabled
    assert evaluate_swing("SPY", _history(+0.5, BREAK), LEVELS, cfg) == (None, [])
    cfg.data["swing"]["from"] = "09:30"              # the test's bars are 09:30-09:40
    setup, [attempt] = evaluate_swing("SPY", _history(+0.5, BREAK), LEVELS, cfg)
    assert setup is attempt and setup.swing
    cfg.data["swing"]["enabled"] = False
    assert evaluate_swing("SPY", _history(+0.5, BREAK), LEVELS, cfg) == (None, [])


def test_the_day_breakout_is_not_a_swing_trade(cfg):
    _swing(cfg)
    df5 = localise(ta.to_frame(_history(+0.5, BREAK)), "America/New_York")
    s = VolatilityBreakout(cfg).evaluate("SPY", df5, ta.resample(df5, "15min"), LEVELS)
    assert not s.swing and s.min_dte_override == 0


def test_the_config_as_shipped(tmp_path, monkeypatch):
    from panaoptions import config as config_mod
    monkeypatch.setattr(config_mod, "ENV_PATH", tmp_path / "absent.env")
    # the 0DTE desk runs the swing book beside its day strategies
    us = config_mod.Config(market="US", profile="zerodte")
    assert us.get("swing.enabled") is True and not us.get("swing.only")
    assert us.get("swing.symbols")[0] == "GLD" and us.get("swing.max_contract_price") == 20.0
    assert us.get("contracts.max_contract_price") == 3.50        # the day desk's own
    india = config_mod.Config(market="IN", profile="zerodte")
    assert india.get("swing.enabled") is True and "NIFTY" in india.get("swing.symbols")
    assert india.get("swing.max_contract_price") == 400.0
    assert (india.get("swing.min_dte"), india.get("swing.max_dte")) == (15, 50)
    # the swing profile: the book alone
    for market in ("US", "IN"):
        c = config_mod.Config(market=market, profile="swing")
        assert c.get("swing.enabled") is True and c.get("swing.only") is True
    # the other desks: off
    monkeypatch.delenv("PANAOPTIONS_PROFILE", raising=False)
    assert config_mod.Config(market="US").get("swing.enabled") is False
    assert config_mod.Config(market="US", profile="scalp").get("swing.enabled") is False


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


def test_the_fill_is_flagged_for_overnight_only_from_a_swing_signal(cfg):
    from panaoptions.ledger.paper import PaperLedger
    from panaoptions.models import Signal
    from panaoptions.risk.guardrails import RiskManager
    c = OptionContract(symbol="SPY", right=OptionRight.CALL, strike=102, expiry="2026-10-30",
                       dte=31, bid=2.9, ask=3.1, delta=0.5)
    sig = Signal(id="S", ts=datetime(2026, 9, 29, 10, 0, tzinfo=ET), symbol="SPY",
                 direction=Direction.LONG, contract=c, quantity=1, entry_price=3.0,
                 stop_price=1.2, target_1=4, target_2=5, underlying_at_entry=102,
                 underlying_support=100)
    risk = RiskManager(cfg)
    ledger = PaperLedger(_swing(cfg), risk)
    assert not ledger.open(sig).hold_overnight
    assert risk.state.trades_taken == 1 and risk.state.swing_open == 0
    swing_sig = sig.model_copy(update={"id": "S2", "hold_overnight": True})
    assert ledger.open(swing_sig).hold_overnight
    # the swing entry has its own slots: not one of the day's trades
    assert risk.state.trades_taken == 1
    assert risk.state.open_trades == 2 and risk.state.swing_open == 1


def test_the_rules_page_describes_the_swing_desk(cfg):
    from panaoptions import rules
    sections = rules.build(_swing(cfg))["sections"]
    assert sections[0]["title"] == "The swing book (1-4 day options)"
    assert "Beside the same-day strategies" in sections[0]["intro"]
    cfg.data["swing"]["enabled"] = False
    assert "The swing book (1-4 day options)" not in [
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


def test_the_hunt_reads_the_screened_names_and_the_whole_swing_list(cfg, monkeypatch):
    import asyncio

    from panaoptions.models import PreMarketRead
    now = datetime(2026, 9, 30, 10, 30, tzinfo=ET)
    desk = _desk(cfg, monkeypatch, now)
    cfg.data["swing"]["symbols"] = ["GLD", "SPY"]
    desk.screened = [PreMarketRead(symbol="TSLA", passed=True),
                     PreMarketRead(symbol="AAPL", passed=False)]
    seen = []

    async def tape(symbol, now):
        seen.append(symbol)
        raise RuntimeError("no tape in this test")   # read, then skipped
    monkeypatch.setattr(desk, "_tape", tape)
    asyncio.run(desk._hunt(now))
    assert sorted(seen) == ["GLD", "SPY", "TSLA"]
    # the swing book alone: the screen's names are not read
    cfg.data["swing"]["only"] = True
    seen.clear()
    asyncio.run(desk._hunt(now))
    assert sorted(seen) == ["GLD", "SPY"]


def test_full_day_slots_still_leave_the_swing_book_hunting(cfg, monkeypatch):
    import asyncio

    from panaoptions.models import PreMarketRead
    now = datetime(2026, 9, 30, 10, 30, tzinfo=ET)
    desk = _desk(cfg, monkeypatch, now)
    cfg.data["risk"]["max_open_trades"] = 1
    cfg.data["swing"]["symbols"] = ["GLD"]
    day = _trade(datetime(2026, 9, 30, 10, 0, tzinfo=ET), swing=False)
    desk.ledger.open_trades[day.id] = day
    desk.screened = [PreMarketRead(symbol="TSLA", passed=True)]
    seen = []

    async def tape(symbol, now):
        seen.append(symbol)
        raise RuntimeError("no tape in this test")
    monkeypatch.setattr(desk, "_tape", tape)
    asyncio.run(desk._hunt(now))
    assert seen == ["GLD"]
    # and a full swing book leaves the day desk hunting
    desk.ledger.open_trades.clear()
    for n in range(2):
        t = _trade(datetime(2026, 9, 29, 10, 0, tzinfo=ET))
        t.id = f"PT-S{n}"
        desk.ledger.open_trades[t.id] = t
    seen.clear()
    asyncio.run(desk._hunt(now))
    assert seen == ["TSLA"]


def _contract(dte, mid, delta=0.5):
    return OptionContract(symbol="GLD", right=OptionRight.CALL, strike=350,
                          expiry=f"dte{dte}", dte=dte, bid=mid - 0.02, ask=mid + 0.02,
                          delta=delta, volume=500, open_interest=5000)


def test_a_swing_setup_takes_a_21_45_day_contract_at_its_own_price_ceiling(cfg):
    from panaoptions.engine import contracts
    from panaoptions.models import Setup
    _swing(cfg)
    cfg.data["contracts"].update(max_contract_price=3.50, min_dte=0, max_dte=4)
    setup = Setup(symbol="GLD", ts=datetime(2026, 9, 30, 10, 0, tzinfo=ET),
                  strategy=SetupType.SWING_BREAKOUT,
                  direction=Direction.LONG, swing=True, min_dte_override=21,
                  max_dte_override=45, delta_band=(0.40, 0.55))
    chain = [_contract(0, 1.20), _contract(30, 7.50)]
    found = contracts.choose("GLD", chain, Direction.LONG, cfg, setup, budget=2000)
    assert found.chosen is not None and found.chosen.dte == 30
    # over budget: never a shorter-dated contract for a position held overnight
    found = contracts.choose("GLD", chain, Direction.LONG, cfg, setup, budget=300)
    assert found.chosen is None or found.chosen.dte >= 21


def test_sizing_gives_the_swing_book_its_own_slots_and_backstop(cfg):
    from panaoptions.models import Indicators, Setup
    from panaoptions.risk.guardrails import RiskManager
    _swing(cfg)
    cfg.data["risk"].update(max_open_trades=1, max_daily_trades=1, stop_mode="underlying")
    risk = RiskManager(cfg)
    risk.capital = 10_000.0
    risk.state.open_trades, risk.state.trades_taken = 1, 1       # the day desk is full
    setup = Setup(symbol="GLD", ts=datetime(2026, 9, 30, 10, 0, tzinfo=ET),
                  strategy=SetupType.SWING_BREAKOUT,
                  direction=Direction.LONG, swing=True,
                  indicators=Indicators(close=352.0), underlying_support=351.5)
    c = _contract(30, 2.00)
    signal, why = risk.size(setup, c, "S", datetime(2026, 9, 30, 10, 0, tzinfo=ET))
    assert signal is not None, why
    assert signal.hold_overnight and signal.stop_price == round(2.00 * 0.4, 2)
    risk.state.open_trades, risk.state.swing_open = 3, 2          # the swing book full
    signal, why = risk.size(setup, c, "S", datetime(2026, 9, 30, 10, 0, tzinfo=ET))
    assert signal is None and "Swing book full" in why


def test_a_swing_trade_is_not_tightened_or_timed_out(cfg, monkeypatch):
    import asyncio
    now = datetime(2026, 9, 30, 11, 0, tzinfo=ET)
    desk = _desk(cfg, monkeypatch, now)
    cfg.data["risk"]["max_hold_minutes"] = 30
    trade = _trade(datetime(2026, 9, 30, 9, 50, tzinfo=ET))
    trade.last_price = 4.0
    desk.ledger.open_trades[trade.id] = trade
    asyncio.run(desk._maybe_tighten(now))
    assert not trade.breakeven_armed and trade.stop_price == 1.2
    fills = desk.ledger.mark(trade.id, 3.5, 102.5, now)
    assert fills == [] and trade.is_open


def test_auto_parks_swing_positions_in_their_market_and_brings_them_back(
        cfg, monkeypatch, tmp_path):
    import asyncio

    from panaoptions import markets
    from panaoptions.ledger import store
    now = datetime(2026, 9, 30, 20, 0, tzinfo=ET)       # US closed
    desk = _desk(cfg, monkeypatch, now)
    monkeypatch.setattr(markets, "in_hours", lambda code, now=None: False)
    books: dict[str, list] = {"US": [], "IN": []}
    monkeypatch.setattr(store, "save_open_book",
                        lambda trades: books.__setitem__(desk.cfg.market, list(trades)))
    monkeypatch.setattr(store, "load_open_book",
                        lambda: list(books.get(desk.cfg.market, [])))
    monkeypatch.setattr(store, "init", lambda: None)
    trade = _trade(datetime(2026, 9, 30, 10, 0, tzinfo=ET))
    desk.ledger.open_trades[trade.id] = trade
    rebuilt = []

    def rebuild(code, factory):
        rebuilt.append(code)
        desk.cfg.market = code
        desk.ledger.open_trades = {}
    monkeypatch.setattr(desk, "_rebuild", rebuild)
    out = asyncio.run(desk.switch_market("IN"))
    assert out["switched"] and rebuilt == ["IN"]
    assert not desk.ledger.open_trades and [t.id for t in books["US"]] == [trade.id]
    out = asyncio.run(desk.switch_market("US"))
    assert out["switched"] and list(desk.ledger.open_trades) == [trade.id]
    # a same-day position still blocks the switch
    day = _trade(datetime(2026, 9, 30, 10, 0, tzinfo=ET), swing=False)
    day.id = "PT-DAY"
    desk.ledger.open_trades[day.id] = day
    assert not asyncio.run(desk.switch_market("IN"))["switched"]


def test_the_daily_limit_is_said_once_a_day_and_the_swing_book_keeps_hunting(
        cfg, monkeypatch):
    import asyncio
    now = datetime(2026, 9, 30, 10, 30, tzinfo=ET)
    desk = _desk(cfg, monkeypatch, now)
    cfg.data["risk"]["max_daily_trades"] = 3
    cfg.data["swing"]["symbols"] = ["GLD"]
    desk.risk.state.trades_taken = 3
    seen = []

    async def tape(symbol, now):
        seen.append(symbol)
        raise RuntimeError("no tape in this test")
    monkeypatch.setattr(desk, "_tape", tape)
    for _ in range(3):
        asyncio.run(desk._hunt(now))
    said = [e for e in desk.activity.recent(200)
            if "the daily limit" in str(getattr(e, "message", e))]
    assert len(said) == 1 and "swing book keeps hunting" in str(said[0])
    assert seen == ["GLD"] * 3
