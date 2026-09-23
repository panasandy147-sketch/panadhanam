"""The dashboard. Read-only by design: no endpoint opens or closes a position.

A trading decision belongs to the rules engine, not to whoever last clicked a
button, so the absence of those endpoints is a tested property rather than an
omission.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from panaoptions.app import OptionsDesk


class _SilentFeed:
    async def connect(self):
        return True

    async def close(self):
        return None

    async def quote(self, symbol):
        return None

    async def candles(self, symbol, interval="5m", include_prepost=False):
        return []

    async def expiries(self, symbol):
        return []

    async def chain_for_window(self, symbol, spot, min_dte, max_dte):
        return []


@pytest.fixture
def client(cfg, monkeypatch, tmp_path):
    from panaoptions.ledger import store
    from panaoptions.web import server

    monkeypatch.setattr(store, "_conn", None)
    monkeypatch.setattr(store, "db_path", lambda: tmp_path / "web.db")
    monkeypatch.setattr(server, "get_config", lambda: cfg)

    desk = OptionsDesk(cfg=cfg, feed=_SilentFeed())
    # The app starts the desk loop on startup; the TestClient context manager
    # would run it for real, so keep it inert.
    monkeypatch.setattr(desk, "start", lambda *a, **k: _noop())
    app = server.create_app(desk)
    with TestClient(app) as c:
        c.desk = desk
        yield c


async def _noop():
    return None


# --------------------------------------------------------------------------- #
def test_the_page_loads(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "panaoptions" in res.text
    assert "paper only" in res.text.lower()


def test_the_footer_says_no_order_can_be_placed(client):
    assert "no broker adapter" in client.get("/").text


@pytest.mark.parametrize("path", [
    "/api/status", "/api/config-check", "/api/screen",
    "/api/trades", "/api/contracts",
])
def test_every_endpoint_answers(client, path):
    assert client.get(path).status_code == 200


# --------------------------------------------------------------------------- #
def test_status_carries_the_configuration_verdict(client):
    body = client.get("/api/status").json()

    assert body["paper_only"] is True
    assert "config" in body
    assert body["config"]["capital"] == 500.0
    assert body["config"]["risk_per_trade_pct"] == 4.0


def test_a_blocker_reaches_the_page_with_its_command(client):
    # The whole reason the dashboard exists: a desk taking nothing looks like
    # a quiet market, and this panel is what tells them apart.
    body = client.get("/api/config-check").json()

    assert body["blockers"], "the shipped $500 config cannot buy an ATM contract"
    blocker = body["blockers"][0]
    assert blocker["setting"] and blocker["problem"] and blocker["fix"]
    assert "PANAOPTIONS_CAPITAL" in blocker["command"]


def test_a_workable_configuration_reports_no_blockers(client, cfg):
    cfg.data["account"]["starting_capital"] = 2000.0
    assert client.get("/api/config-check").json()["blockers"] == []


def test_the_contracts_panel_says_which_names_are_affordable(client):
    body = client.get("/api/contracts").json()

    assert body["budget"] == 100.0             # 20% of $500
    assert body["rows"]
    assert not any(r["affordable"] for r in body["rows"]), \
        "nothing in this universe fits a $100 budget at the money"

    tsla = next(r for r in body["rows"] if r["symbol"] == "TSLA")
    assert tsla["atm_cost"] > 1000


def test_the_trades_panel_includes_why_setups_were_passed_over(client):
    body = client.get("/api/trades?days=7").json()
    assert body["days"] == 7
    assert "rejections" in body, \
        "on a desk taking nothing, this is the panel that explains it"
    assert "stats" in body


# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path,method", [
    ("/api/order", "post"), ("/api/trade", "post"), ("/api/close", "post"),
    ("/api/status", "post"),
])
def test_nothing_on_the_dashboard_can_place_or_close_a_trade(client, path, method):
    res = getattr(client, method)(path)
    assert res.status_code in (404, 405), \
        f"{method.upper()} {path} must not exist — the rules engine decides, not a button"


# --------------------------------------------------------------------------- #
# The feed tracks whether it is connected, so --check does not have to open a
# second client to find out — which printed every failure twice.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_the_feed_reports_whether_it_connected(monkeypatch):
    import httpx

    from panaoptions.data.feed import YahooFeed

    class _Response:
        status_code = 200

        def json(self):
            return {"chart": {"result": [{"meta": {}}]}}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def get(self, *a, **k):
            return _Response()

        async def aclose(self):
            pass

    monkeypatch.setattr(httpx, "AsyncClient", _Client)

    feed = YahooFeed()
    assert feed.connected is False
    await feed.connect()
    assert feed.connected is True
    await feed.close()
    assert feed.connected is False, "a closed feed must not read as connected"


@pytest.mark.asyncio
async def test_a_failed_connection_does_not_read_as_connected(monkeypatch):
    import httpx

    from panaoptions.data.feed import YahooFeed

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def get(self, *a, **k):
            raise OSError("network is unreachable")

        async def aclose(self):
            pass

    monkeypatch.setattr(httpx, "AsyncClient", _Client)

    feed = YahooFeed()
    assert await feed.connect() is False
    assert feed.connected is False


# --------------------------------------------------------------------------- #
# A `git pull` must actually change what people see.
# --------------------------------------------------------------------------- #
def test_the_page_is_never_cached_and_its_assets_are_stamped(client):
    res = client.get("/")
    assert "no-store" in res.headers.get("cache-control", "")
    for asset in ("app.js", "styles.css"):
        assert f"/static/{asset}?v=" in res.text


def test_a_stamped_asset_may_be_cached_hard(client):
    stamped = client.get("/").text.split("/static/app.js?v=")[1].split('"')[0]
    res = client.get(f"/static/app.js?v={stamped}")
    assert res.status_code == 200
    assert "immutable" in res.headers.get("cache-control", "")


def test_a_missing_asset_does_not_break_the_page(monkeypatch, tmp_path):
    from panaoptions.web import server
    monkeypatch.setattr(server, "STATIC", tmp_path)
    assert server._asset_version("nope.js") == "0"


# --------------------------------------------------------------------------- #
# "Configured" and "working" must not look the same on the page.
# --------------------------------------------------------------------------- #
def test_the_journal_endpoint_says_who_is_writing_the_cards(client, cfg):
    cfg.data["journal"]["use_llm"] = True
    from panaoptions.ml import llm
    llm.health.mark_up()

    coach = client.get("/api/journal").json()["coach"]
    assert coach["configured"] is True
    assert coach["writing_cards"] is True
    assert coach["model"]


def test_a_coach_that_is_on_but_down_reports_itself(client, cfg):
    cfg.data["journal"]["use_llm"] = True
    from panaoptions.ml import llm
    llm.health.mark_down("Ollama is not reachable at http://127.0.0.1:11434")
    try:
        coach = client.get("/api/journal").json()["coach"]
        assert coach["configured"] is True
        assert coach["writing_cards"] is False, \
            "on and answering are different things"
        assert "not reachable" in coach["down_reason"]
    finally:
        llm.health.mark_up()


def test_the_strategies_endpoint_lists_all_three_with_their_windows(client):
    body = client.get("/api/strategies").json()
    names = [s["name"] for s in body["strategies"]]

    assert len(names) == 3
    assert "ORB + VWAP" in names
    for strategy in body["strategies"]:
        assert strategy["from"] < strategy["to"]
        assert strategy["from"] >= "09:45", \
            "every strategy skips the opening chop"


# --------------------------------------------------------------------------- #
# The activity log: the difference between a working desk and a hung one.
# --------------------------------------------------------------------------- #
def test_the_activity_endpoint_returns_newest_first(client):
    client.desk.activity.add("cycle.start", "open")
    client.desk.activity.add("screen.done", "2/8 passed", level="good")

    body = client.get("/api/activity").json()
    assert body["count"] == 2
    assert body["events"][0]["kind"] == "screen.done", "a log reads newest first"
    assert body["events"][0]["level"] == "good"
    assert "time" in body["events"][0]


def test_the_log_is_bounded_so_a_long_session_cannot_grow_without_limit(client):
    from panaoptions.activity import ActivityLog

    log = ActivityLog(limit=5)
    for i in range(20):
        log.add("cycle.start", str(i))

    assert len(log) == 5
    assert log.recent()[0]["detail"] == "19", "the newest survives, not the oldest"


def test_an_empty_log_is_not_an_error(client):
    client.desk.activity.clear()
    body = client.get("/api/activity").json()
    assert body["events"] == []
    assert body["count"] == 0
