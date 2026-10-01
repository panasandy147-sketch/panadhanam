"""Refused setups are followed to the close, so a day with no trades still
says what each gate cost and saved — and the coach's advice is never applied."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from panaoptions.learning import missed
from panaoptions.models import Candle

TZ = "America/New_York"


def _bars(path, start=datetime(2026, 9, 29, 14, 0, tzinfo=UTC)):
    """5m bars (UTC; 14:00 UTC = 10:00 ET) through (low, high, close) points."""
    return [Candle(ts=start + timedelta(minutes=5 * i), open=c, high=h, low=lo,
                   close=c, volume=1000) for i, (lo, h, c) in enumerate(path)]


def _event(**kw):
    base = {"event": "REFUSED", "market_time": "2026-09-29 10:00:00 EDT",
            "symbol": "SPY", "side": "LONG_CALL", "strategy": "PD Liquidity Sweep",
            "gate": "committee", "entry": 100.0, "stop": 99.0, "target": 103.0}
    return {**base, **kw}


def test_one_setup_counts_once_and_gathers_every_gate_that_refused_it():
    rows = missed.setups([_event(), _event(market_time="2026-09-29 10:30:00 EDT",
                                           gate="contract"),
                          _event(stop=None), _event(event="LOOK")])
    assert len(rows) == 1
    assert rows[0]["gate"] == "committee" and rows[0]["gates"] == ["committee", "contract"]


def test_target_first_is_its_planned_r_and_stop_first_is_minus_one():
    s = missed.setups([_event()])[0]
    win = missed.follow(s, _bars([(99.8, 100.5, 100.2), (100.5, 103.2, 103.0)]), TZ)
    assert win["outcome"] == "target" and win["r"] == 3.0 and win["at"] == "10:05"
    loss = missed.follow(s, _bars([(99.8, 100.5, 100.2), (98.9, 100.4, 99.0)]), TZ)
    assert loss["outcome"] == "stop" and loss["r"] == -1.0
    # A bar touching both counts as the stop: the cautious reading.
    both = missed.follow(s, _bars([(99.8, 100.1, 100.0), (98.5, 103.5, 101.0)]), TZ)
    assert both["outcome"] == "stop"


def test_bars_before_the_refusal_do_not_count_and_open_trades_mark_at_the_close():
    s = missed.setups([_event()])[0]
    early = datetime(2026, 9, 29, 13, 0, tzinfo=UTC)          # 09:00 ET
    bars = _bars([(90.0, 110.0, 100.0)], start=early) + _bars(
        [(99.9, 100.6, 100.5), (100.2, 101.2, 101.0)])
    r = missed.follow(s, bars, TZ)
    assert r["outcome"] == "open at close" and r["r"] == 1.0


def test_puts_walk_the_other_way():
    s = missed.setups([_event(side="LONG_PUT", entry=100.0, stop=101.0, target=97.0)])[0]
    r = missed.follow(s, _bars([(99.5, 100.2, 99.8), (96.9, 99.9, 97.0)]), TZ)
    assert r["outcome"] == "target" and r["r"] == 3.0


def test_by_gate_totals_what_each_gate_cost_and_saved():
    rows = [{"gate": "committee", "outcome": "target", "r": 3.0},
            {"gate": "committee", "outcome": "stop", "r": -1.0},
            {"gate": "contract", "outcome": "open at close", "r": 0.2},
            {"gate": "contract", "outcome": "no data", "r": None}]
    g = missed.by_gate(rows)
    assert g["committee"] == {"setups": 2, "would_win": 1, "would_lose": 1, "flat": 0,
                              "r": 2.0}
    assert g["contract"]["setups"] == 1 and g["contract"]["flat"] == 1


def test_the_coach_answer_is_parsed_defensively_and_capped_at_three():
    text = ('```json\n{"summary": "committee cost 2R", "suggestions": ['
            + ",".join('{"setting": "agents.approve_threshold", "change": "0.50", '
                       '"why": "3 of 4 refused won"}' for _ in range(5)) + "]}```")
    c = missed.parse_coach(text)
    assert c["summary"] == "committee cost 2R" and len(c["suggestions"]) == 3
    assert missed.parse_coach("no json here") is None
    assert missed.parse_coach(None) is None


def test_the_review_lands_in_the_day_audit_and_changes_no_setting(cfg, monkeypatch):
    from panaoptions import audit, clock

    now = datetime(2026, 9, 29, 16, 5, tzinfo=UTC).astimezone()
    monkeypatch.setattr(clock, "now", lambda tz: now)
    audit._write(cfg, _event())

    class Feed:
        async def candles(self, symbol, tf):
            return _bars([(99.8, 100.5, 100.2), (100.5, 103.2, 103.0)])

    async def fake_coach(cfg, day, events, record):
        return {"summary": "the committee refused a 3R winner",
                "suggestions": [{"setting": "agents.approve_threshold",
                                 "change": "test 0.50", "why": "1 of 1 won"}]}

    monkeypatch.setattr(missed, "coach", fake_coach)
    before = dict(cfg.data["agents"])
    day = now.date()
    record = asyncio.run(missed.review(cfg, day, Feed()))
    assert record["by_gate"]["committee"]["r"] == 3.0
    assert cfg.data["agents"] == before, "advice is written down, never applied"
    text = audit.day_markdown(day)
    assert "What the refused setups did next" in text
    assert "| committee | 1 | 1 | 0 | 0 | +3.00 |" in text
    assert "Coach (Ollama) — advice only, never applied" in text


def test_the_coach_stays_silent_when_the_model_is_off(cfg):
    # conftest sets PANAOPTIONS_AGENT_LLM=off
    out = asyncio.run(missed.coach(cfg, datetime(2026, 9, 29).date(), [],
                                   {"by_gate": {}, "setups": []}))
    assert out is None


def test_the_friday_reflection_tunes_the_pd_sweep_too():
    from panaoptions.learning import reflect
    assert "pd_liquidity_sweep" in reflect.STRATEGIES
