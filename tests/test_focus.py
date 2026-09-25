"""The focus list: the top of each band is what the desk watches and trades."""
from __future__ import annotations

import pytest

from app.brokers.paper import PaperBroker
from app.core.bus import Topic, bus
from app.scheduler import TradingEngine


@pytest.fixture
async def engine(cfg):
    cfg.switch_market("US")
    broker = PaperBroker(config={"total_capital": 100_000})
    await broker.connect()
    eng = TradingEngine(broker, cfg)

    async def _nothing():
        return []

    async def _no_macro():
        return None

    eng.news.fetch = _nothing
    eng.macro.fetch = _no_macro
    yield eng
    cfg.switch_market("IN")


@pytest.mark.asyncio
async def test_the_top_of_each_band_is_kept(engine, cfg):
    targets, ready = await engine.focus.targets("t", [], None)
    per = int(cfg.get("focus.per_band"))
    assert set(engine.focus.bands) == {"A", "B", "C"}
    assert all(len(rows) == per for rows in engine.focus.bands.values())
    assert len(targets) == 3 * per
    # The ranking's own contexts are reused, not fetched twice.
    assert set(ready) == set(targets)
    for rows in engine.focus.bands.values():
        keys = [(r["tradeable"], abs(r["score"])) for r in rows]
        assert keys == sorted(keys, reverse=True)


@pytest.mark.asyncio
async def test_the_list_is_kept_until_the_re_rank_is_due(engine, cfg):
    await engine.focus.targets("t", [], None)
    first = engine.focus.updated_at
    await engine.focus.targets("t2", [], None)
    assert engine.focus.updated_at == first           # not re-ranked every minute
    engine.focus._refreshed -= engine.focus.rerank_seconds
    await engine.focus.targets("t3", [], None)
    assert engine.focus.updated_at != first


@pytest.mark.asyncio
async def test_a_cycle_only_judges_the_focus_names(engine, cfg):
    results = await engine.run_cycle()
    assert {r["symbol"] for r in results} == set(engine.focus.symbols())
    assert len(results) == 3 * int(cfg.get("focus.per_band"))


@pytest.mark.asyncio
async def test_ranking_does_not_flood_the_log_with_no_trade_lines(engine, monkeypatch):
    """Ranking 60 names must not publish 60 CMIO decisions; it publishes one
    focus update."""
    seen: list[str] = []
    publish = bus.publish

    async def _record(topic, payload):
        seen.append(topic)
        await publish(topic, payload)

    monkeypatch.setattr(bus, "publish", _record)
    await engine.focus.refresh("t", [], None)
    assert seen.count(Topic.CYCLE_DONE) == 0
    assert seen.count(Topic.FOCUS) == 1


@pytest.mark.asyncio
async def test_off_means_every_name_is_scanned(engine, cfg):
    cfg.settings["focus"]["enabled"] = False
    try:
        targets, _ = await engine.focus.targets("t", [], None)
        assert len(targets) == len(cfg.watchlist())
    finally:
        cfg.settings["focus"]["enabled"] = True


@pytest.mark.asyncio
async def test_an_index_without_a_real_chain_does_not_take_a_slot(cfg):
    """NIFTY has no cash instrument. With no real option chain it would sit
    at the top of band A all session and be refused every minute."""
    cfg.switch_market("IN")
    broker = PaperBroker(config={"total_capital": 100_000})
    await broker.connect()
    eng = TradingEngine(broker, cfg)

    async def _nothing():
        return []

    eng.news.fetch = _nothing
    await eng.focus.refresh("t", [], None)
    watched = set(eng.focus.symbols())
    assert not watched & {"NIFTY 50", "NIFTY BANK", "FINNIFTY"}
    assert len(eng.focus.bands["A"]) == int(cfg.get("focus.per_band"))
