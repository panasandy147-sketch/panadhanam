"""A day with no trade still has its reasons written down, and the three
things that kept the India desk from trading on 30 Sept: RVOL paced on the
US clock, NSE indices with no volume, and an exact 3R read as 2.9999R."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
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
