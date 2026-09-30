"""A day with no trade still has its reasons written down, and the three
things that kept the India desk from trading on 30 Sept: RVOL paced on the
US clock, NSE indices with no volume, and an exact 3R read as 2.9999R."""
from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from panaoptions.models import Candle, Direction, Indicators, PreMarketRead, Setup, SetupType

IST = ZoneInfo("Asia/Kolkata")
ET = ZoneInfo("America/New_York")


# --------------------------------------------------------------------------- #
# RVOL on the market's own clock, and indices without volume
# --------------------------------------------------------------------------- #
def test_rvol_is_paced_on_the_markets_own_session():
    from panaoptions.data import premarket as pm
    at = datetime(2026, 9, 30, 9, 25, tzinfo=IST)
    # NSE: 10 minutes into a 375-minute session — not "before the 09:30 open".
    assert pm._session_fraction(at, "09:15", "15:30") == 10 / 375
    assert pm._session_fraction(at) == 1 / 390               # the old US clock
    daily = [Candle(ts=at - timedelta(days=i), open=1, high=1, low=1, close=1,
                    volume=375_000) for i in range(21, 0, -1)]
    intraday = [Candle(ts=at - timedelta(minutes=5), open=1, high=1, low=1, close=1,
                       volume=10_000)]
    # 10,000 traded in 10 minutes of an average 1,000-a-minute day: 1.0x.
    assert pm.relative_volume(intraday, daily, at, 20, "09:15", "15:30") == 1.0


def test_an_index_with_no_volume_is_screened_on_the_gap_alone(cfg):
    from panaoptions.data import premarket as pm
    at = datetime(2026, 9, 30, 9, 40, tzinfo=IST)
    cfg.data["session"].update(market_open="09:15", market_close="15:30")

    class Feed:
        async def quote(self, s):
            return {"previous_close": 100.0, "last_price": 101.5 if s == "NIFTY" else 100.2}

        async def candles(self, s, tf, include_prepost=False):
            return [Candle(ts=at - timedelta(days=i), open=100, high=100, low=100,
                           close=100, volume=0) for i in range(5)]

        async def chain_for_window(self, *a):
            return []

    reads = {r.symbol: r for r in asyncio.run(pm.screen(Feed(), cfg, at,
                                                        ["NIFTY", "BANKNIFTY"]))}
    assert reads["NIFTY"].passed and reads["NIFTY"].rvol_unmeasured
    assert any("judged on the gap alone" in r for r in reads["NIFTY"].reasons)
    assert not reads["BANKNIFTY"].passed                 # a 0.2% gap is still too small


def test_the_technical_agent_does_not_veto_an_index_for_no_volume(cfg):
    from panaoptions import alpha
    from panaoptions.agents import technical
    cfg.data.setdefault("llm", {})["enabled"] = False
    setup = Setup(symbol="NIFTY", ts=datetime(2026, 9, 30, 10, 0, tzinfo=IST),
                  direction=Direction.LONG, strategy=SetupType.ORB_VWAP, pattern="x",
                  indicators=Indicators(close=100.0, rvol=0.0))
    signal = alpha.AlphaSignal("NIFTY", "LONG", 100.0, 99.0, 0.8, source="orb_vwap")
    bars = [Candle(ts=setup.ts - timedelta(minutes=5 * i), open=100, high=100.5,
                   low=99.5, close=100, volume=0) for i in range(30, 0, -1)]
    index = asyncio.run(technical.vote(signal, setup, bars, cfg))
    assert not index.veto and index.data["rvol_unmeasured"]
    traded = [b.model_copy(update={"volume": 1000.0}) for b in bars]
    stock = asyncio.run(technical.vote(signal, setup, traded, cfg))
    assert stock.veto and "below 1.5x" in stock.reasons[0]


# --------------------------------------------------------------------------- #
# An exact 3R is a 3R
# --------------------------------------------------------------------------- #
def test_a_sweep_target_of_exactly_3r_is_not_refused_for_rounding(cfg):
    from panaoptions.engine import reward
    from panaoptions.models import SessionLevels
    cfg.data["risk"]["own_target_strategies"] = ["pd_liquidity_sweep"]
    # FINNIFTY 30 Sept 09:30: risk 27.10, target rounded to 4 decimals.
    entry = 24636.5337
    stop = round(entry + 27.10, 4)
    target = round(entry - 3 * (stop - entry), 4)
    setup = Setup(symbol="FINNIFTY", ts=datetime(2026, 9, 30, 9, 30, tzinfo=IST),
                  direction=Direction.SHORT, strategy=SetupType.PD_LIQUIDITY_SWEEP,
                  pattern="PDH Sweep — Tweezer Top", indicators=Indicators(close=entry),
                  entry_trigger=entry, underlying_support=stop, underlying_target=target)
    got, rr, why, _ = reward.project(setup, SessionLevels(), cfg, sweep=True)
    assert not why and got == target
    # 2.9R is still refused.
    setup.underlying_target = round(entry - 2.9 * (stop - entry), 4)
    assert "needs a verified 1:3" in reward.project(setup, SessionLevels(), cfg,
                                                    sweep=True)[2]


# --------------------------------------------------------------------------- #
# The audit log on a day with no trade
# --------------------------------------------------------------------------- #
def _cfg_ist(cfg):
    return SimpleNamespace(timezone="Asia/Kolkata", market="IN", currency="₹",
                           get=cfg.get)


def test_refusals_the_screen_and_the_strategy_tally_are_in_the_audit(cfg, monkeypatch):
    from panaoptions import audit, clock
    from panaoptions.ledger import store
    day = date(2026, 9, 30)
    monkeypatch.setattr(clock, "now", lambda tz: datetime(2026, 9, 30, 10, 5, tzinfo=IST))
    c = _cfg_ist(cfg)

    # Nothing yet: the summary still gets written and says nobody was asked.
    audit.refresh_day(c, day)
    text = (audit.audit_dir() / "2026-09-30.md").read_text(encoding="utf-8")
    assert "0 trade(s)" in text and "No strategy was asked today" in text

    audit.record_screen(c, [
        PreMarketRead(symbol="TCS", gap_pct=2.5, rvol=1.6, passed=True,
                      reasons=["gap +2.50%", "RVOL 1.60"]),
        PreMarketRead(symbol="SBIN", gap_pct=0.1, rvol=0.9,
                      reasons=["gap +0.10% is inside the |1.0|% threshold"])],
        "1/2 passed — hunting TCS")
    audit.record_refusal(c, "TCS", "LONG_CALL", "committee",
                         "score 0.41 below 0.55", strategy="VWAP / 9-EMA Pullback",
                         pattern="Bullish Engulfing")
    attempt = SimpleNamespace(strategy=SetupType.CANDLESTICK_AT_LEVEL, triggered=False,
                              blockers=["No institutional sweep of previous day extremes."])
    fired = SimpleNamespace(strategy=SetupType.VWAP_EMA_PULLBACK, triggered=True, blockers=[])
    store.tally_checks("2026-09-30", [attempt, attempt, fired])

    audit.refresh_day(c, day)
    text = (audit.audit_dir() / "2026-09-30.md").read_text(encoding="utf-8")
    assert "## Pre-market screen" in text and "1/2 passed — hunting TCS" in text
    assert "SBIN: not passed" in text
    assert "## Fired but not taken" in text and "[committee] score 0.41" in text
    assert "## What each strategy saw" in text
    assert "No institutional sweep of previous day extremes. (2)" in text

    [d] = audit.by_day(day, day)
    assert d["buys"] == 0 and d["refused"] == 1 and d["screen"].startswith("1/2 passed")
    [row] = d["events"]
    assert row["event"] == "REFUSED" and row["execution"] == "NOT_TAKEN"
    assert row["reason"].startswith("[committee]")
    assert audit.by_trade(audit.entries(day)) == {}


def test_the_desk_logs_a_repeated_refusal_once_per_window(cfg, monkeypatch):
    from panaoptions import audit
    from panaoptions.app import OptionsDesk
    from tests.test_app import FakeFeed
    desk = OptionsDesk(cfg=cfg, feed=FakeFeed())
    logged = []
    monkeypatch.setattr(audit, "record_refusal", lambda *a, **k: logged.append(a))
    setup = Setup(symbol="TCS", ts=datetime(2026, 9, 30, 10, 0, tzinfo=ET),
                  direction=Direction.LONG, strategy=SetupType.CANDLESTICK_AT_LEVEL,
                  pattern="Hammer", indicators=Indicators(close=2050.0))
    t0 = datetime(2026, 9, 30, 10, 0, tzinfo=ET)
    for minute in range(0, 45):
        desk._record_refusal("TCS", "LONG_CALL", setup, "F&O confluence",
                             f"no sweep of the PDL {2032 + minute * 0.05:.2f}",
                             t0 + timedelta(minutes=minute))
    assert len(logged) == 2                           # 10:00 and 10:30, not 45 times


def test_the_desk_writes_the_summary_during_the_session_and_after_the_close(cfg, monkeypatch):
    from panaoptions import audit
    from panaoptions.app import OptionsDesk
    from tests.test_app import FakeFeed
    desk = OptionsDesk(cfg=cfg, feed=FakeFeed())
    wrote = []
    monkeypatch.setattr(audit, "refresh_day", lambda c, d: wrote.append(d))
    t0 = datetime(2026, 9, 30, 10, 0, tzinfo=ET)
    desk._refresh_audit(t0)
    desk._refresh_audit(t0 + timedelta(minutes=2))             # throttled
    desk._refresh_audit(t0 + timedelta(minutes=6))
    desk._refresh_audit(t0 + timedelta(minutes=7))             # throttled: dirty
    assert len(wrote) == 2
    desk._refresh_audit(t0 + timedelta(hours=6), final=True)   # after the close
    desk._refresh_audit(t0 + timedelta(hours=7), final=True)   # nothing new
    assert len(wrote) == 3


def test_india_trades_the_sweep_alone_when_open_interest_cannot_be_read():
    """NSE's chain is refused and the estimated chain has no OI: on 30 Sept
    'OI unknown' refused all five fired setups. US chains carry OI: unchanged."""
    from panaoptions.config import Config
    india, us = Config(market="IN"), Config()
    assert india.get("fno.confluence.when_oi_unknown") == "allow"
    assert "pd_liquidity_sweep" in india.get("fno.confluence.strategies")
    assert us.get("fno.confluence.when_oi_unknown") == "block"


def test_symbol_by_symbol_the_data_figures_and_each_strategys_verdict(cfg, monkeypatch):
    from panaoptions import audit, clock
    from panaoptions.app import OptionsDesk
    from panaoptions.ledger import store
    from panaoptions.models import SessionLevels
    from tests.test_app import FakeFeed
    t0 = datetime(2026, 9, 30, 9, 30, tzinfo=ET)
    now = {"t": t0 + timedelta(minutes=40 * 5)}
    monkeypatch.setattr(clock, "now", lambda tz: now["t"])
    bars = [Candle(ts=t0 + timedelta(minutes=5 * i), open=100 + i * 0.01, high=100.5 + i * 0.01,
                   low=99.5 + i * 0.01, close=100.2 + i * 0.01, volume=1000 + i)
            for i in range(40)]
    levels = SessionLevels(previous_high=102.0, previous_low=98.0, previous_close=99.0,
                           opening_range_high=101.0, opening_range_low=99.0)
    quiet = Setup(symbol="SPY", ts=now["t"], strategy=SetupType.ORB_VWAP,
                  blockers=["no close beyond the opening range (101.00–99.00)"])
    fired = Setup(symbol="SPY", ts=now["t"], strategy=SetupType.PD_LIQUIDITY_SWEEP,
                  direction=Direction.LONG, pattern="PDL Sweep — Tweezer Bottom",
                  entry_trigger=100.6, underlying_support=99.9, underlying_target=102.7)
    day = now["t"].date()

    desk = OptionsDesk(cfg=cfg, feed=FakeFeed())
    for minute in range(3):              # a fired setup: one LOOK, not one a minute
        store.tally_checks(day.isoformat(), [quiet, fired], "SPY")
        desk._maybe_look("SPY", bars, levels, [quiet, fired], now["t"] + timedelta(minutes=minute))
    looks = [e for e in audit.entries(day) if e["event"] == "LOOK"]
    assert len(looks) == 1 and looks[0]["why_logged"] == "fired"
    lk = looks[0]
    assert lk["data"]["bars"] == 40 and lk["figures"]["close"] == 100.59
    assert lk["levels"]["previous_high"] == 102.0 and lk["figures"]["vwap"]
    assert audit.by_day(day, day)[0]["events"] == []          # not a buy/sell row

    text = audit.day_markdown(day)
    assert "## Symbol by symbol" in text and "### SPY" in text
    assert "| ORB + VWAP | 3 | 0 |" in text and "| PD Liquidity Sweep | 3 | 3 |" in text
    assert "(setup fired)" in text and "PDH/PDL 102.00/98.00" in text
    assert "OR 99.00–101.00" in text and "40 5m bars" in text
    assert "ORB + VWAP: no — no close beyond the opening range (101.00–99.00)" in text
    assert "PD Liquidity Sweep: **FIRED** LONG PDL Sweep — Tweezer Bottom — entry 100.60, " \
           "stop 99.90, target 102.70" in text


def test_the_decisions_view_moves_every_cycle_without_evicting_trades():
    """panadhanam shows each symbol's 'no trade: why' every minute; so does
    panaoptions now — in a rolling buffer the trades cannot be pushed out of."""
    from panaoptions.activity import MAX_ROLLING, ActivityLog
    log = ActivityLog(notable_limit=5)
    log.add("trade.open", "EXECUTED SPY call")
    for i in range(MAX_ROLLING + 50):
        log.add("cycle.done", f"SPY — no trade: ORB + VWAP: no close beyond the range {i}")
        log.add("setup.pass", "chatter")                 # still hidden
    shown = log.recent(1000, notable_only=True)
    kinds = [e["kind"] for e in shown]
    assert "trade.open" in kinds and "setup.pass" not in kinds
    assert kinds.count("cycle.done") == MAX_ROLLING
    assert shown[0]["detail"].endswith(str(MAX_ROLLING + 49))    # newest first
    assert log.notable_count == 1                                 # decisions, not checks


# --------------------------------------------------------------------------- #
# Zero-volume bars, midday RVOL, the 0.50% sweep band
# --------------------------------------------------------------------------- #
def test_a_live_bar_with_no_volume_bypasses_the_volume_gates():
    from panaoptions.engine import strategies as st
    lag = Indicators(close=100, volume=0, avg_volume=50_000, rvol=0.0)
    thin = Indicators(close=100, volume=20_000, avg_volume=50_000, rvol=0.4)
    assert st._volume_ok(lag, 1.5) and not st._volume_ok(thin, 1.5)
    assert "bypassed" in st._volume_note(lag, "volume 0x")
    assert st._volume_note(thin, "volume 0.4x") == "volume 0.4x"


def test_the_midday_window_lowers_the_volume_gates_to_1_2x(cfg):
    from panaoptions.engine.strategies import volume_multiple
    cfg.data["technical"]["midday_rvol"] = {"enabled": True, "from": "10:30",
                                            "to": "14:00", "min": 1.2}
    at = lambda h, m: datetime(2026, 9, 30, h, m, tzinfo=ET)    # noqa: E731
    assert volume_multiple(cfg, 1.5, at(10, 29)) == 1.5
    assert volume_multiple(cfg, 1.5, at(10, 30)) == 1.2
    assert volume_multiple(cfg, 1.5, at(13, 59)) == 1.2
    assert volume_multiple(cfg, 1.5, at(14, 0)) == 1.5
    assert volume_multiple(cfg, 1.0, at(12, 0)) == 1.0        # never raised


def test_the_technical_agent_uses_the_midday_gate_and_ignores_a_lagging_bar(cfg):
    from panaoptions import alpha
    from panaoptions.agents import technical
    cfg.data.setdefault("llm", {})["enabled"] = False
    cfg.data["technical"]["midday_rvol"] = {"enabled": True, "from": "10:30",
                                            "to": "14:00", "min": 1.2}
    signal = alpha.AlphaSignal("SPY", "LONG", 100.0, 99.0, 0.8, source="orb_vwap")

    def vote(hh, rvol, last_volume):
        ts = datetime(2026, 9, 30, hh, 0, tzinfo=ET)
        setup = Setup(symbol="SPY", ts=ts, direction=Direction.LONG,
                      strategy=SetupType.ORB_VWAP, pattern="x",
                      indicators=Indicators(close=100.0, rvol=rvol))
        bars = [Candle(ts=ts - timedelta(minutes=5 * i), open=100, high=100.5, low=99.5,
                       close=100, volume=1000) for i in range(30, 0, -1)]
        bars[-1] = bars[-1].model_copy(update={"volume": last_volume})
        return asyncio.run(technical.vote(signal, setup, bars, cfg))

    assert vote(10, 1.3, 1000).veto                      # 1.3x < 1.5x in the morning
    noon = vote(12, 1.3, 1000)
    assert not noon.veto and "midday RVOL gate 1.5x → 1.2x" in " ".join(noon.reasons)
    lag = vote(10, 0.0, 0)                               # the live bar not reported yet
    assert not lag.veto and "feed lag" in " ".join(lag.reasons)


def test_the_shipped_sweep_band_is_half_a_percent_everywhere(tmp_path, monkeypatch):
    from panaoptions import config as config_mod
    monkeypatch.setattr(config_mod, "ENV_PATH", tmp_path / "absent.env")
    c = config_mod.Config()
    assert c.get("strategies.pd_liquidity_sweep.proximity_pct") == 0.50
    assert c.get("fno.confluence.proximity_pct") == 0.50
    assert c.get("technical.midday_rvol.min") == 1.2


def test_the_desk_judges_closed_candles_only(cfg, monkeypatch):
    """The feed's last 5m bar is still forming: it is dropped for the hunt."""
    from panaoptions.app import OptionsDesk, completed_bars
    t0 = datetime(2026, 9, 30, 10, 0, tzinfo=ET)
    bars = [Candle(ts=t0 + timedelta(minutes=5 * i), open=1, high=1, low=1, close=1,
                   volume=1) for i in range(3)]                       # 10:00, 10:05, 10:10
    assert len(completed_bars(bars, "5m", t0 + timedelta(minutes=14))) == 2   # 10:10 forming
    assert len(completed_bars(bars, "5m", t0 + timedelta(minutes=15))) == 3   # closed at 10:15
    assert len(completed_bars(bars, "1m", t0 + timedelta(minutes=10, seconds=30))) == 2

    from tests.test_app import FakeFeed
    cfg.data["technical"].update(completed_bars_only=True, stale_after_bars=0)
    desk = OptionsDesk(cfg=cfg, feed=FakeFeed())

    async def candles(symbol, tf="5m", **k):
        return bars

    async def levels(symbol, now):
        return None

    monkeypatch.setattr(desk.feed, "candles", candles)
    monkeypatch.setattr(desk, "_levels_for", levels)
    got, _ = asyncio.run(desk._tape("SPY", t0 + timedelta(minutes=12)))
    assert len(got) == 2
    cfg.data["technical"]["completed_bars_only"] = False
    got, _ = asyncio.run(desk._tape("SPY", t0 + timedelta(minutes=12)))
    assert len(got) == 3


def test_the_watchlist_keeps_only_names_whose_options_can_be_bought(cfg):
    from panaoptions import auto_watchlist as aw
    from panaoptions.models import OptionContract, OptionRight

    def k(sym, right, bid, ask, delta=0.45):
        return OptionContract(symbol=sym, right=right, strike=20, expiry="2026-10-09",
                              dte=9, bid=bid, ask=ask, delta=delta)

    class Feed:
        async def chain_for_window(self, sym, spot, lo, hi):
            if sym == "THIN":          # 0.20 x 0.40: 67% wide
                return [k(sym, OptionRight.CALL, 0.20, 0.40), k(sym, OptionRight.PUT, 0.20, 0.40)]
            if sym == "CALLS":         # liquid calls, no liquid put
                return [k(sym, OptionRight.CALL, 1.00, 1.04), k(sym, OptionRight.PUT, 0, 0.5)]
            if sym == "DOWN":
                raise RuntimeError("chain source down")
            return [k(sym, OptionRight.CALL, 1.00, 1.04), k(sym, OptionRight.PUT, 0.98, 1.02, -0.45)]

    ranked = {s: aw.Candidate(symbol=s, price=20.0, score=10 - i)
              for i, s in enumerate(["GOOD", "THIN", "CALLS", "DOWN", "SPY", "HELD"])}
    kept, thin = asyncio.run(aw.drop_illiquid(Feed(), cfg, ranked, ["HELD"], "2026-09-30"))
    # SPY (pinned) and HELD (held) are never checked; DOWN's chain failed.
    assert set(kept) == {"GOOD", "DOWN", "SPY", "HELD"}
    assert "no call and put" in thin["THIN"] and "(tightest 67%)" in thin["THIN"]
    assert "no put" in thin["CALLS"]


def test_every_unclosed_bar_is_dropped_including_yahoos_live_point():
    """At 15:33 Yahoo ends the chart with the forming 15:30 bucket AND a live
    point stamped 15:33: both go, the last closed bar is the 15:25 one."""
    from panaoptions.app import completed_bars
    t = datetime(2026, 9, 30, 15, 25, tzinfo=ET)
    bars = [Candle(ts=t, open=1, high=1, low=1, close=1),
            Candle(ts=t + timedelta(minutes=5), open=1, high=1, low=1, close=1),
            Candle(ts=t + timedelta(minutes=8), open=1, high=1, low=1, close=1)]
    got = completed_bars(bars, "5m", t + timedelta(minutes=8, seconds=20))
    assert [b.ts for b in got] == [t]


def test_a_stale_tape_is_not_traded():
    from panaoptions.app import stale_reason
    t = datetime(2026, 9, 30, 11, 0, tzinfo=ET)
    bars = [Candle(ts=t, open=1, high=1, low=1, close=1)]        # closed 11:05
    assert stale_reason(bars, "5m", t + timedelta(minutes=19), 3) == ""
    why = stale_reason(bars, "5m", t + timedelta(minutes=25), 3)
    assert "data stale" in why and "20 min ago" in why
    assert stale_reason(bars, "5m", t + timedelta(hours=5), 0) == ""   # guard off


def test_candles_fall_back_to_the_second_host_then_to_1m_rebuilt():
    from panaoptions.data import feed as feed_mod

    t = int(datetime(2026, 9, 30, 14, 0, tzinfo=UTC).timestamp())

    def chart(stamps):
        n = len(stamps)
        return {"chart": {"result": [{"timestamp": stamps, "indicators": {"quote": [{
            "open": [1.0] * n, "high": [float(i + 2) for i in range(n)],
            "low": [0.5] * n, "close": [float(i + 1) for i in range(n)],
            "volume": [10] * n}]}}]}}

    class Feed(feed_mod.YahooFeed):
        def __init__(self, answers):
            super().__init__()
            self._client = object()
            self.answers = answers

        async def _get(self, url, **params):
            host = "query1" if "query1" in url else "query2"
            return self.answers.get((host, params.get("interval")))

    five = chart([t, t + 300])
    f = Feed({("query2", "5m"): five})
    assert len(asyncio.run(f.candles("SPY", "5m"))) == 2
    assert f.candle_source["SPY"] == "yahoo query2"

    ones = chart([t + 60 * i for i in range(10)])                   # 14:00-14:09
    f = Feed({("query1", "1m"): ones})
    bars = asyncio.run(f.candles("SPY", "5m"))
    assert f.candle_source["SPY"] == "rebuilt from 1m (5m)"
    assert len(bars) == 2 and bars[0].high == 6.0 and bars[0].close == 5.0
    assert bars[0].volume == 50 and bars[1].open == 1.0

    f = Feed({})
    assert asyncio.run(f.candles("SPY", "5m")) == []
    assert f.candle_source["SPY"].startswith("none")
