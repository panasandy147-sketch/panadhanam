"""Backtest: replay the last 5 blocked setups through the debit-spread ladder.

The desk logs every setup it refuses. Five of them were refused for want of
an affordable contract; the backtest rebuilds each one's chain from the
historical underlying and asks today's picker what it would have done — and
what the position was worth at the close.
"""
from __future__ import annotations

import asyncio
import json
import math
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from panaoptions import backtest_spreads as bt
from panaoptions.ledger import store
from panaoptions.models import Candle

ET = ZoneInfo("America/New_York")
BASE = {"SPY": 500.0, "NVDA": 180.0}


class HistoryFeed:
    """A week of 5-minute bars (21-25 Sep 2026) and 40 daily bars.

    SPY drifts up through each session; NVDA drifts down."""

    async def connect(self):
        return True

    async def close(self):
        return None

    async def candles(self, symbol, interval="5m", include_prepost=False):
        base = BASE[symbol]
        drift = 0.0004 if symbol == "SPY" else -0.0005
        if interval == "1d":
            start = datetime(2026, 8, 17, 20, 0, tzinfo=UTC)
            out, price = [], base
            for i in range(40):
                price *= 1 + (0.012 if i % 2 else -0.011)          # ~18% realised vol
                out.append(Candle(ts=start + timedelta(days=i), open=price, high=price * 1.01,
                                  low=price * 0.99, close=price, volume=1e7))
            return out
        out = []
        for day in range(21, 26):
            price = base
            open_ = datetime(2026, 9, day, 13, 30, tzinfo=UTC)          # 09:30 ET
            for i in range(78):
                price *= 1 + drift
                out.append(Candle(ts=open_ + timedelta(minutes=5 * i), open=price,
                                  high=price * 1.001, low=price * 0.999, close=price,
                                  volume=5e5))
        return out


@pytest.fixture
def zcfg(cfg):
    cfg.data["contracts"].update(min_dte=0, max_dte=4, min_delta=0.40, max_delta=0.50,
                                 prefer_nearest_expiry=True, min_contract_price=0.10,
                                 max_contract_price=10.0, index_max_contract_price=10.0)
    cfg.data["session"]["force_exit_at"] = "15:45"
    return cfg


def _log_the_week():
    """What the desk wrote: five setups blocked on price, plus noise."""
    rows = [
        ("S1", datetime(2026, 9, 21, 10, 5), "SPY", "LONG", 60.0),
        ("S2", datetime(2026, 9, 22, 11, 0), "NVDA", "SHORT", 60.0),
        ("S3", datetime(2026, 9, 23, 10, 30), "SPY", "LONG", 50.0),
        ("S4", datetime(2026, 9, 24, 13, 15), "NVDA", "LONG", 60.0),
        ("S5", datetime(2026, 9, 25, 10, 0), "SPY", "LONG", 25.0),      # tiny budget
    ]
    for sid, ts, sym, side, budget in rows:
        store.save_signal_seen(
            sid, ts.replace(tzinfo=ET), sym, side, False,
            f"The 0.40-0.50 delta contract costs more, over the ${budget:,.0f} budget. "
            f"SKIPPED — hard risk failure", {"budget": budget})
    # Not blocked on price: must be ignored.
    store.save_signal_seen("N1", datetime(2026, 9, 23, 10, 0, tzinfo=ET), "SPY", "LONG",
                           False, "committee score 0.41 below 0.55")
    store.save_signal_seen("T1", datetime(2026, 9, 23, 10, 0, tzinfo=ET), "SPY", "LONG",
                           True, "taken")


def test_only_price_blocked_setups_are_replayed_oldest_first(zcfg):
    _log_the_week()
    items = bt.blocked_from_log(limit=5)
    assert [b.symbol for b in items] == ["SPY", "NVDA", "SPY", "NVDA", "SPY"]
    assert [b.budget for b in items] == [60.0, 60.0, 50.0, 60.0, 25.0]
    assert items[1].direction == "SHORT"


def test_the_last_five_blocked_setups_through_the_spread_ladder(zcfg):
    _log_the_week()
    results = asyncio.run(bt.replay(bt.blocked_from_log(limit=5), HistoryFeed(), zcfg))
    summary = bt.summarise(results)

    assert summary["setups"] == 5 and summary["priced"] == 5
    for r in results:
        # Every one of them was genuinely blocked: the naked 0.40-0.50 contract
        # costs more than the budget the desk had.
        assert r.naked and r.naked_cost > r.budget, r
    spreads = [r for r in results if r.tier == "debit_spread"]
    assert summary["as_debit_spread"] == len(spreads) == 4
    assert [r.symbol for r in results if not r.filled] == ["SPY"]     # the $25 one
    for r in spreads:
        assert r.cost <= r.budget and len(r.legs) == 2 and r.max_profit > 0
        assert r.pnl is not None                                       # marked at the close
    nvda_short = next(r for r in results if r.direction == "SHORT")
    assert "P spread" in nvda_short.contract                           # bear put spread
    # SPY rose into each close, so its bull call spreads finished green;
    # NVDA fell, so its bear put spread did too and its bull call did not.
    by = {(r.symbol, r.direction, r.ts[:10]): r for r in spreads}
    assert by[("SPY", "LONG", "2026-09-21")].pnl > 0
    assert by[("NVDA", "SHORT", "2026-09-22")].pnl > 0
    assert by[("NVDA", "LONG", "2026-09-24")].pnl < 0
    assert summary["spread_winners"] == 3


def test_the_result_is_saved_for_the_weekend_review(zcfg):
    _log_the_week()
    results = asyncio.run(bt.replay(bt.blocked_from_log(limit=5), HistoryFeed(), zcfg))
    summary = bt.summarise(results)
    saved = bt.save(results, summary, zcfg, datetime(2026, 9, 26, 9, 0, tzinfo=ET))
    md = open(saved["markdown"], encoding="utf-8").read()
    assert "**4 of 5** setups would have filled (4 as a debit spread)" in md
    assert "Model prices" in md and "SKIPPED" in md
    data = json.loads(open(saved["json"], encoding="utf-8").read())
    assert data["summary"]["as_debit_spread"] == 4 and len(data["results"]) == 5


def test_any_symbol_on_recent_sessions(zcfg):
    items = bt.sessions_for(["SPY", "NVDA"], 3, zcfg,
                            datetime(2026, 9, 26, 9, 0, tzinfo=ET))
    assert [(b.symbol, b.ts.day) for b in items] == [
        ("NVDA", 23), ("SPY", 23), ("NVDA", 24), ("SPY", 24), ("NVDA", 25), ("SPY", 25)]
    results = asyncio.run(bt.replay(items, HistoryFeed(), zcfg, budget_now=120.0))
    assert all(r.spot for r in results)
    assert all(r.tier in {"primary", "debit_spread", "secondary_delta", ""} for r in results)


def test_a_session_older_than_the_intraday_history_says_so(zcfg):
    old = bt.Blocked(symbol="SPY", ts=datetime(2026, 6, 1, 10, 0, tzinfo=ET),
                     direction="LONG", budget=150.0)
    [r] = asyncio.run(bt.replay([old], HistoryFeed(), zcfg))
    assert not r.filled and "60 days" in r.note


def test_the_model_chain_is_priced_sensibly(zcfg):
    when = datetime(2026, 9, 23, 10, 0, tzinfo=ET)
    chain = bt.model_chain("SPY", 500.0, 0.18, when, zcfg, 100)
    calls = [c for c in chain if c.right.value == "CALL" and c.dte == 0]
    atm = min(calls, key=lambda c: abs(c.strike - 500))
    assert 0.45 <= atm.delta <= 0.55 and atm.estimated
    assert all(c.bid < c.ask for c in chain)
    # Deeper in the money costs more.
    assert min(calls, key=lambda c: c.strike).mid > atm.mid
    assert bt.realised_vol(asyncio.run(HistoryFeed().candles("SPY", "1d")),
                           when.date()) == pytest.approx(0.18, abs=0.05)
    assert math.isfinite(bt.value_at(atm, 501.0, 0.18, when + timedelta(hours=5), zcfg))


def test_run_py_has_the_command(zcfg, monkeypatch, capsys):
    import run

    _log_the_week()
    monkeypatch.setattr(run, "get_config", lambda *a, **k: zcfg)
    monkeypatch.setattr("panaoptions.data.provider.make_feed", lambda cfg: _Ctx())
    monkeypatch.setattr(run.clock, "now", lambda tz: datetime(2026, 9, 26, 9, 0, tzinfo=ET))
    assert asyncio.run(run._backtest_spreads(5, None, 5)) == 0
    out = capsys.readouterr().out
    assert "BACKTEST: the last 5 blocked setup(s)" in out
    assert "4 of 5" in out


class _Ctx(HistoryFeed):
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None
