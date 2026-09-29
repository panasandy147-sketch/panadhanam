"""The auto watchlist: the day's top 10 before the open, re-checked hourly.

The promises under test:
  * built before the open, refreshed every hour, never at the weekend or
    after the stop time;
  * a symbol with an open position is never replaced — once its trade has
    closed it can be, but only at the NEXT hourly refresh;
  * the list is never emptied, whatever the sources do;
  * a list typed on the dashboard turns auto off, and the button turns it on;
  * India ranks only names with a known lot size.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest

from panaoptions import auto_watchlist as aw
from panaoptions import clock, markets, watchlist
from panaoptions.app import OptionsDesk
from panaoptions.models import Candle

ET = ZoneInfo("America/New_York")
IST = ZoneInfo("Asia/Kolkata")


def _at(hh, mm, day=23):
    return datetime(2026, 9, day, hh, mm, tzinfo=ET)          # Wed 23 Sep 2026


def cand(symbol, change=1.0, rvol=1.0, rating=2.0, price=100.0, avg=10_000_000,
         sources=("most_actives",), kind="EQUITY", cap=1e11):
    return aw.Candidate(symbol=symbol, price=price, change_pct=change,
                        volume=avg * rvol, avg_volume=avg, rating=rating,
                        rating_text="Buy", market_cap=cap, quote_type=kind,
                        sources=set(sources))


class FakeDiscovery:
    """Stands in for Yahoo: returns whatever the test put in `names`."""

    def __init__(self, names=None, fail=False):
        self.names = dict(names or {})
        self.fail = fail
        self.calls = 0
        self.status = {}

    async def candidates(self, cfg, pool):
        self.calls += 1
        if self.fail:
            raise RuntimeError("Yahoo said no")
        self.status = {"most_actives": f"{len(self.names)} names"}
        # Fresh copies: scoring writes into them.
        return {s: aw.Candidate(**{**c.__dict__, "sources": set(c.sources),
                                   "reasons": []})
                for s, c in self.names.items()}

    async def close(self):
        return None


class QuietFeed:
    """No charts, no chains: the watchlist is all that is being tested."""

    def __init__(self, daily=None):
        self.daily = daily or {}

    async def connect(self):
        return True

    async def close(self):
        return None

    async def quote(self, symbol):
        return None

    async def candles(self, symbol, interval="5m", include_prepost=False):
        return self.daily.get(symbol, []) if interval == "1d" else []

    async def expiries(self, symbol):
        return []

    async def chain_for_window(self, symbol, spot, min_dte, max_dte):
        return []


def _market(n=14):
    """Fourteen names, strongest first (big move, heavy volume)."""
    names = {f"S{i:02d}": cand(f"S{i:02d}", change=4.0 - i * 0.25, rvol=3.0 - i * 0.15)
             for i in range(n)}
    names["SPY"] = cand("SPY", change=0.2, rvol=1.0, rating=None, kind="ETF", avg=60e6)
    names["QQQ"] = cand("QQQ", change=0.3, rvol=1.0, rating=None, kind="ETF", avg=40e6)
    return names


@pytest.fixture
def on(monkeypatch):
    monkeypatch.delenv("PANAOPTIONS_AUTO_WATCHLIST", raising=False)


@pytest.fixture
def desk(cfg, on, monkeypatch, tmp_path):
    from panaoptions.ledger import store
    monkeypatch.setattr(store, "db_path", lambda: tmp_path / "t.db")
    d = OptionsDesk(cfg=cfg, feed=QuietFeed())
    d.discovery = FakeDiscovery(_market())
    return d


def _clock(monkeypatch, when):
    monkeypatch.setattr(clock, "now", lambda tz: when.astimezone(ZoneInfo(tz)))


# --------------------------------------------------------------------------- #
# Reading Yahoo
# --------------------------------------------------------------------------- #
def test_a_yahoo_quote_is_read_with_its_analyst_rating():
    c = aw.parse_quote({"symbol": "nvda", "regularMarketPrice": 120.5,
                        "regularMarketChangePercent": 2.4, "regularMarketVolume": 300e6,
                        "averageDailyVolume10Day": 200e6, "marketCap": 3e12,
                        "averageAnalystRating": "1.4 - Strong Buy",
                        "quoteType": "EQUITY", "marketState": "REGULAR"}, source="trending")
    assert (c.symbol, c.price, c.change_pct, c.rating, c.rating_text) == (
        "NVDA", 120.5, 2.4, 1.4, "Strong Buy")
    assert c.avg_volume == 200e6 and c.sources == {"trending"} and not c.premarket


def test_before_the_open_the_pre_market_move_is_the_move():
    c = aw.parse_quote({"symbol": "AMD", "marketState": "PRE", "regularMarketPrice": 150,
                        "regularMarketChangePercent": -0.5, "preMarketPrice": 156,
                        "preMarketChangePercent": 4.0})
    assert c.premarket and c.change_pct == 4.0 and c.price == 156


@pytest.mark.parametrize("raw,expected", [
    ("2.1 - Buy", (2.1, "Buy")), ("4.6 - Sell", (4.6, "Sell")), (1.0, (1.0, "")),
    ("", (None, "")), (None, (None, "")), ("n/a", (None, "")), ("9 - Huh", (None, "")),
])
def test_analyst_ratings_parse_or_are_left_out(raw, expected):
    assert aw.parse_rating(raw) == expected


# --------------------------------------------------------------------------- #
# Scoring and filters
# --------------------------------------------------------------------------- #
def test_a_big_move_on_heavy_volume_outranks_a_quiet_name(cfg):
    hot, quiet = cand("HOT", change=3.5, rvol=2.5), cand("DULL", change=0.1, rvol=0.6)
    assert aw.score(hot, cfg) > aw.score(quiet, cfg) + 0.2
    assert any("RVOL" in r for r in hot.reasons)


def test_analysts_count_for_more_when_they_agree_with_the_move(cfg):
    agree = cand("A", change=2.0, rating=1.5)          # Strong Buy, stock rising
    against = cand("B", change=-2.0, rating=1.5)       # Strong Buy, stock falling
    assert aw.score(agree, cfg) > aw.score(against, cfg)
    assert "with the move" in " ".join(agree.reasons)
    assert "against the move" in " ".join(against.reasons)


@pytest.mark.parametrize("c,why", [
    (cand("PENNY", price=3.0), "under"),
    (cand("DEAR", price=900.0), "over"),
    (cand("THIN", avg=200_000), "average volume"),
    (cand("SMOL", cap=1e9), "market cap"),
    (cand("BTC-USD", kind="CRYPTOCURRENCY"), "not a stock"),
    (aw.Candidate(symbol="NOQ"), "no quote"),
])
def test_names_the_desk_cannot_trade_well_are_refused(cfg, c, why):
    assert why in aw.eligible(c, cfg)


def test_a_liquid_etf_is_allowed(cfg):
    assert aw.eligible(cand("SPY", kind="ETF", rating=None, cap=0), cfg) == ""


# --------------------------------------------------------------------------- #
# Choosing the list
# --------------------------------------------------------------------------- #
def _ranked(cfg, names):
    ok, _ = aw.rank(names, cfg, _at(8, 45), None)
    return ok


def test_the_morning_build_is_the_top_ten_with_the_pins_first(cfg):
    sel = aw.select(["AAPL", "MSFT"], _ranked(cfg, _market()), [], cfg, fresh=True)
    assert len(sel.symbols) == 10
    assert sel.symbols[:2] == ["SPY", "QQQ"]
    assert sel.symbols[2:] == [f"S{i:02d}" for i in range(8)]
    assert sel.removed == ["AAPL", "MSFT"]


def test_an_hourly_refresh_only_swaps_for_a_clearly_better_name(cfg):
    current = ["SPY", "QQQ"] + [f"S{i:02d}" for i in range(8)]
    names = _market()
    # S08 is only a little better than the weakest incumbent: no churn.
    assert not aw.select(current, _ranked(cfg, names), [], cfg).changed
    # A name that is far stronger than the weakest: swapped in.
    names["ROCKET"] = cand("ROCKET", change=6.0, rvol=4.0, rating=1.2,
                           sources=("most_actives", "day_gainers", "trending"))
    sel = aw.select(current, _ranked(cfg, names), [], cfg)
    assert sel.added == ["ROCKET"] and sel.removed == ["S07"]
    assert len(sel.symbols) == 10


def test_an_open_position_is_never_replaced(cfg):
    current = ["SPY", "QQQ", "WEAK"] + [f"S{i:02d}" for i in range(7)]
    names = _market()
    names["WEAK"] = cand("WEAK", change=0.0, rvol=0.2, rating=3.0)
    for fresh in (True, False):
        sel = aw.select(current, _ranked(cfg, names), ["WEAK"], cfg, fresh=fresh)
        assert "WEAK" in sel.symbols and sel.held == ["WEAK"]
        assert len(sel.symbols) == 10


def test_an_open_position_stays_even_when_it_no_longer_qualifies(cfg):
    names = _market()
    names["GONE"] = cand("GONE", price=2.0)             # now a penny stock
    ok, refused = aw.rank(names, cfg, _at(10, 0), None)
    assert "GONE" in refused
    sel = aw.select(["SPY", "QQQ", "GONE"], ok, ["GONE"], cfg, judged=set(names))
    assert "GONE" in sel.symbols


def test_the_list_is_never_emptied_when_every_source_fails(cfg):
    current = ["SPY", "QQQ", "NVDA"]
    sel = aw.select(current, {}, [], cfg, fresh=True)
    assert sel.symbols == current and not sel.changed
    # Nothing in force either: the base list, never nothing.
    sel = aw.select([], {}, [], cfg, fresh=True, fallback=["SPY", "QQQ", "IWM"])
    assert sel.symbols == ["SPY", "QQQ", "IWM"]


def test_a_name_refused_is_dropped_but_a_name_with_no_quote_keeps_its_place(cfg):
    names = _market()
    names["FELL"] = cand("FELL", price=4.0)
    ok, _ = aw.rank(names, cfg, _at(10, 0), None)
    current = ["SPY", "QQQ", "FELL", "NODATA"]
    sel = aw.select(current, ok, [], cfg, judged=set(names))
    assert "FELL" not in sel.symbols and "NODATA" in sel.symbols


# --------------------------------------------------------------------------- #
# When
# --------------------------------------------------------------------------- #
def test_the_schedule_before_the_open_then_hourly(cfg):
    assert aw.due(cfg, _at(8, 30), {}) == ""
    assert aw.due(cfg, _at(8, 45), {}) == "morning"
    done = {"day": "2026-09-23", "last_refresh": _at(8, 45).isoformat()}
    assert aw.due(cfg, _at(9, 30), done) == ""
    assert aw.due(cfg, _at(9, 45), done) == "hourly"
    assert aw.due(cfg, _at(15, 0), {**done, "last_refresh": _at(13, 45).isoformat()}) == ""
    saturday = datetime(2026, 9, 26, 10, 0, tzinfo=ET)
    assert aw.due(cfg, saturday, {}) == ""


# --------------------------------------------------------------------------- #
# On the desk
# --------------------------------------------------------------------------- #
async def test_the_desk_builds_the_list_before_the_open(desk, monkeypatch):
    _clock(monkeypatch, _at(8, 30))
    await desk.cycle()
    assert desk.discovery.calls == 0                   # too early

    _clock(monkeypatch, _at(8, 45))
    await desk.cycle()
    assert desk.discovery.calls == 1
    assert desk.cfg.symbols[:2] == ["SPY", "QQQ"] and len(desk.cfg.symbols) == 10
    assert "S00" in desk.cfg.symbols

    state = aw.load_state()
    assert state["mode"] == "auto" and state["kind"] == "morning"
    assert state["symbols"] == desk.cfg.symbols
    # Not saved as a typed list: auto stays in charge.
    assert watchlist.load() == [] and aw.mode(desk.cfg) == "auto"
    # Logged for the weekly review.
    from panaoptions.journal import store as journal_store
    log = journal_store.JOURNAL_DIR / "watchlist" / "2026-09-23.jsonl"
    entry = json.loads(log.read_text().splitlines()[-1])
    assert entry["kind"] == "morning" and entry["symbols"] == desk.cfg.symbols
    assert any(e["kind"] == "watchlist.auto" for e in desk.activity.recent(20))


async def test_the_desk_re_ranks_every_hour_not_every_minute(desk, monkeypatch):
    for hh, mm in ((8, 45), (9, 0), (9, 30), (9, 44)):
        await desk._maybe_refresh_watchlist(_at(hh, mm))
    assert desk.discovery.calls == 1
    await desk._maybe_refresh_watchlist(_at(9, 45))
    assert desk.discovery.calls == 2
    await desk._maybe_refresh_watchlist(_at(10, 45))
    assert desk.discovery.calls == 3


async def test_a_traded_symbol_is_replaced_only_at_the_next_hour_after_closing(desk):
    names = desk.discovery.names
    await desk._maybe_refresh_watchlist(_at(8, 45))
    assert "S07" in desk.cfg.symbols

    # A trade on S07; then S07 goes quiet and a far stronger name appears.
    desk.ledger.open_trades["T1"] = SimpleNamespace(symbol="S07")
    names["S07"] = cand("S07", change=0.0, rvol=0.3, rating=3.0)
    names["ROCKET"] = cand("ROCKET", change=6.0, rvol=4.0, rating=1.2,
                           sources=("most_actives", "day_gainers", "trending"))
    await desk._maybe_refresh_watchlist(_at(9, 45))
    assert "S07" in desk.cfg.symbols                   # position open: kept
    assert "ROCKET" in desk.cfg.symbols                # took the next-weakest slot

    # The trade closes at 10:05. Nothing changes until the next refresh…
    del desk.ledger.open_trades["T1"]
    await desk._maybe_refresh_watchlist(_at(10, 5))
    assert "S07" in desk.cfg.symbols
    # …and at 10:45 it is replaced like any other weak name.
    names["NEWCO"] = cand("NEWCO", change=5.5, rvol=3.5, rating=1.3,
                          sources=("most_actives", "trending"))
    await desk._maybe_refresh_watchlist(_at(10, 45))
    assert "S07" not in desk.cfg.symbols and "NEWCO" in desk.cfg.symbols


async def test_when_every_source_fails_the_list_stays_and_it_retries_soon(desk):
    await desk._maybe_refresh_watchlist(_at(8, 45))
    before = list(desk.cfg.symbols)
    desk.discovery.fail = True
    await desk._maybe_refresh_watchlist(_at(9, 45))
    assert desk.cfg.symbols == before
    events = desk.activity.recent(20)
    assert any("no source answered" in e["detail"] for e in events)
    # Retried in ten minutes, not an hour.
    calls = desk.discovery.calls
    await desk._maybe_refresh_watchlist(_at(9, 50))
    assert desk.discovery.calls == calls
    await desk._maybe_refresh_watchlist(_at(9, 55))
    assert desk.discovery.calls == calls + 1


async def test_a_typed_list_turns_auto_off_and_the_button_turns_it_on(desk):
    await desk._maybe_refresh_watchlist(_at(8, 45))
    desk.set_universe(["AAPL", "MSFT"])
    assert aw.mode(desk.cfg) == "custom"
    calls = desk.discovery.calls
    await desk._maybe_refresh_watchlist(_at(10, 45))
    assert desk.discovery.calls == calls and desk.cfg.symbols == ["AAPL", "MSFT"]

    status = await desk.set_auto_watchlist(True)
    assert status["source"] == "auto" and status["symbols"][:2] == ["SPY", "QQQ"]
    assert len(status["symbols"]) == 10 and status["auto"]["table"]

    status = await desk.set_auto_watchlist(False)
    assert status["source"] == "config"


async def test_the_auto_list_survives_a_restart(desk, cfg):
    await desk._maybe_refresh_watchlist(_at(8, 45))
    chosen = list(desk.cfg.symbols)
    cfg.reload()
    again = OptionsDesk(cfg=cfg, feed=QuietFeed())
    assert again.cfg.symbols == chosen


async def test_a_newcomer_is_screened_straight_away_when_the_day_is_screened(
        desk, monkeypatch):
    seen = []

    async def _screen(feed, cfg, now, symbols=None):
        seen.append(list(symbols or cfg.symbols))
        return []

    monkeypatch.setattr("panaoptions.app.screen", _screen)
    await desk._maybe_refresh_watchlist(_at(8, 45))
    desk._screened_on = "2026-09-23"
    desk.discovery.names["ROCKET"] = cand(
        "ROCKET", change=6.0, rvol=4.0, rating=1.2,
        sources=("most_actives", "day_gainers", "trending"))
    await desk._maybe_refresh_watchlist(_at(9, 45))
    assert seen == [["ROCKET"]]


async def test_switched_off_means_no_requests_at_all(cfg, monkeypatch):
    monkeypatch.setenv("PANAOPTIONS_AUTO_WATCHLIST", "off")
    d = OptionsDesk(cfg=cfg, feed=QuietFeed())
    d.discovery = FakeDiscovery(_market())
    await d._maybe_refresh_watchlist(_at(8, 45))
    assert d.discovery.calls == 0 and aw.mode(cfg) == "config"
    with pytest.raises(ValueError):
        await d.set_auto_watchlist(True)


async def test_a_pool_name_without_a_quote_is_priced_from_its_chart(cfg):
    day = datetime(2026, 9, 1, tzinfo=UTC)
    daily = [Candle(ts=day + timedelta(days=i), open=100, high=101, low=99,
                    close=100 + i, volume=5e6) for i in range(12)]
    found, status = await aw.gather(FakeDiscovery({}), QuietFeed({"IWM": daily}),
                                    cfg, ["IWM"], _at(9, 0))
    assert found["IWM"].price == 111 and round(found["IWM"].change_pct, 2) == 0.91
    assert "chart" in status


# --------------------------------------------------------------------------- #
# India
# --------------------------------------------------------------------------- #
@pytest.fixture
def india(tmp_path, monkeypatch, on):
    from panaoptions import config as config_mod
    monkeypatch.setattr(config_mod, "ENV_PATH", tmp_path / "absent.env")
    yield config_mod.Config(market="IN")
    markets.activate("US")


def test_india_ranks_only_names_with_a_known_lot_size(india):
    pool, restrict = aw.pool_for(india, [], [])
    assert restrict and "RELIANCE" in restrict and "NIFTY" in restrict
    assert pool[:2] == ["NIFTY", "BANKNIFTY"]
    names = {s: cand(s, price=1500, cap=0) for s in restrict}
    names["ZOMATO"] = cand("ZOMATO", change=9.0, rvol=5.0, sources=("trending",))
    ok, refused = aw.rank(names, india, datetime(2026, 9, 23, 9, 0, tzinfo=IST), restrict)
    assert "ZOMATO" not in ok and "lot size" in refused["ZOMATO"]
    sel = aw.select([], ok, [], india, fresh=True)
    assert sel.symbols[:2] == ["NIFTY", "BANKNIFTY"] and len(sel.symbols) == 10


def test_india_builds_at_nine_ist(india):
    assert aw.due(india, datetime(2026, 9, 23, 8, 55, tzinfo=IST), {}) == ""
    assert aw.due(india, datetime(2026, 9, 23, 9, 0, tzinfo=IST), {}) == "morning"


# --------------------------------------------------------------------------- #
# Yahoo, over a fake network
# --------------------------------------------------------------------------- #
def _yahoo(refuse=()):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "fc.yahoo.com" in url:
            return httpx.Response(404, headers={"set-cookie": "A3=x; Path=/"})
        if "getcrumb" in url:
            return httpx.Response(200, text="abc123")
        if "screener" in url:
            scr = request.url.params.get("scrIds")
            if scr in refuse:
                return httpx.Response(500)
            assert request.url.params.get("crumb") == "abc123"
            quotes = {"most_actives": [{"symbol": "NVDA", "regularMarketPrice": 120,
                                        "regularMarketChangePercent": 3.0,
                                        "averageAnalystRating": "1.3 - Strong Buy"}],
                      "day_gainers": [{"symbol": "NVDA", "regularMarketPrice": 120}],
                      "day_losers": [{"symbol": "INTC", "regularMarketPrice": 20}]}[scr]
            return httpx.Response(200, json={"finance": {"result": [{"quotes": quotes}]}})
        if "trending" in url:
            return httpx.Response(200, json={"finance": {"result": [{"quotes": [
                {"symbol": "PLTR"}, {"symbol": "RELIANCE.NS"}, {"symbol": "^NSEI"}]}]}})
        if "/v7/finance/quote" in url:
            names = request.url.params.get("symbols").split(",")
            return httpx.Response(200, json={"quoteResponse": {"result": [
                {"symbol": n, "regularMarketPrice": 50.0, "averageAnalystRating": "2.0 - Buy"}
                for n in names]}})
        return httpx.Response(404)
    return httpx.MockTransport(handler)


async def test_yahoo_lists_and_quotes_are_pooled_and_one_refusal_sinks_nothing(cfg):
    src = aw.YahooDiscovery(transport=_yahoo(refuse=("day_losers",)))
    try:
        found = await src.candidates(cfg, ["SPY"])
    finally:
        await src.close()
    assert found["NVDA"].sources == {"most_actives", "day_gainers"}
    assert found["NVDA"].rating == 1.3
    assert found["PLTR"].has_quote and found["PLTR"].sources == {"trending"}
    assert found["SPY"].has_quote
    assert "INTC" not in found and src.status["day_losers"] == "HTTP 500"


async def test_india_names_come_back_as_desk_symbols(india):
    src = aw.YahooDiscovery(transport=_yahoo())
    try:
        found = await src.candidates(india, ["NIFTY", "TCS"])
    finally:
        await src.close()
    assert {"NIFTY", "RELIANCE", "TCS"} <= set(found)
    assert "trending" in found["RELIANCE"].sources and "trending" in found["NIFTY"].sources
    assert not any(s.endswith(".NS") or s.startswith("^") for s in found)


# --------------------------------------------------------------------------- #
# The dashboard
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(desk, cfg, monkeypatch):
    from fastapi.testclient import TestClient

    from panaoptions.web import server

    monkeypatch.setattr(server, "get_config", lambda: cfg)

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(desk, "start", _noop)
    with TestClient(server.create_app(desk)) as c:
        yield c


def test_the_auto_button_ranks_and_the_panel_shows_why(client, monkeypatch):
    _clock(monkeypatch, _at(9, 50))
    body = client.post("/api/watchlist/auto", json={"enabled": True}).json()
    assert body["source"] == "auto" and len(body["symbols"]) == 10
    got = client.get("/api/watchlist").json()
    assert got["auto"]["on"] and got["auto"]["refresh_minutes"] == 60
    assert got["auto"]["next_refresh"] == "10:50"
    assert got["auto"]["table"][0]["reasons"]
    # Typing a list takes over; the reset goes back to settings.yaml.
    client.post("/api/watchlist", json={"symbols": "AAPL, MSFT"})
    assert client.get("/api/watchlist").json()["source"] == "custom"
    assert client.post("/api/watchlist/reset").json()["source"] == "config"


def test_the_auto_button_says_so_when_it_is_switched_off(client, monkeypatch):
    monkeypatch.setenv("PANAOPTIONS_AUTO_WATCHLIST", "off")
    res = client.post("/api/watchlist/auto", json={"enabled": True})
    assert res.status_code == 409 and "switched off" in res.json()["detail"]
