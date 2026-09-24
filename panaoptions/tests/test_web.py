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


def test_the_strategies_endpoint_lists_them_all_with_their_windows(client):
    body = client.get("/api/strategies").json()
    names = [s["name"] for s in body["strategies"]]

    assert len(names) == 4
    assert "ORB + VWAP" in names
    assert "Candlestick at a Key Level" in names
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


# --------------------------------------------------------------------------- #
# The live candidate panel
# --------------------------------------------------------------------------- #
def test_the_candidate_endpoint_is_honest_about_having_nothing(client):
    """An idle desk and a broken one must not look the same.

    Returning an empty object here would render as a blank panel, which reads
    exactly like a panel that failed to load.
    """
    body = client.get("/api/candidate").json()
    assert body["candidate"] is None
    assert body["scanning"] == ""
    assert "phase" in body and body["watchlist"] == []


def test_the_candidate_endpoint_carries_the_whole_case(client):
    client.desk.scanning = "AAPL"
    client.desk.candidate = {
        "symbol": "AAPL", "right": "CALL", "direction": "LONG",
        "pattern": "Hammer", "key_level": 97.9, "key_level_source": "swing low",
        "entry_trigger": 99.0, "invalidation": 97.6,
        "delta_band": [0.50, 0.65], "reasoning": ["**Buy a CALL** on AAPL."],
        "confirmations": ["Hammer on the 15m close"], "taken": False,
    }
    body = client.get("/api/candidate").json()
    assert body["scanning"] == "AAPL"
    found = body["candidate"]
    # The why, not just the what — the reasoning is the part you can learn from.
    assert found["reasoning"] and found["right"] == "CALL"
    assert found["key_level_source"] == "swing low"
    assert found["entry_trigger"] == 99.0 and found["invalidation"] == 97.6


def test_a_candidate_says_whether_the_desk_actually_took_it(client):
    # "We saw this" and "we bought this" are different claims, and the panel
    # must not blur them.
    client.desk.candidate = {"symbol": "AAPL", "taken": False}
    assert client.get("/api/candidate").json()["candidate"]["taken"] is False
    client.desk.candidate = {"symbol": "AAPL", "taken": True,
                             "trade_id": "PT-1", "quantity": 1}
    body = client.get("/api/candidate").json()["candidate"]
    assert body["taken"] is True and body["trade_id"] == "PT-1"


def test_the_chart_endpoint_speaks_the_chart_library_dialect(client,
                                                             monkeypatch):
    from datetime import UTC, datetime

    from panaoptions.models import Candle

    bars = [Candle(ts=datetime(2026, 9, 23, 13, 30 + 5 * i, tzinfo=UTC),
                   open=100 + i, high=101 + i, low=99 + i, close=100.5 + i,
                   volume=1000.0) for i in range(4)]

    async def _bars(symbol, interval="5m", include_prepost=False):
        return bars

    monkeypatch.setattr(client.desk.feed, "candles", _bars)
    body = client.get("/api/candles/aapl").json()
    assert body["symbol"] == "AAPL"
    # Lightweight-charts wants epoch seconds, not an ISO string.
    first = body["candles"][0]
    assert isinstance(first["time"], int)
    assert first["time"] == int(bars[0].ts.timestamp())
    assert {"open", "high", "low", "close", "volume"} <= set(first)


def test_the_chart_endpoint_returns_the_most_recent_bars(client, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from panaoptions.models import Candle

    base = datetime(2026, 9, 23, 13, 30, tzinfo=UTC)
    bars = [Candle(ts=base + timedelta(minutes=5 * i), open=100, high=101,
                   low=99, close=100, volume=1.0) for i in range(300)]

    async def _bars(symbol, interval="5m", include_prepost=False):
        return bars

    monkeypatch.setattr(client.desk.feed, "candles", _bars)
    body = client.get("/api/candles/AAPL?count=50").json()
    assert len(body["candles"]) == 50
    assert body["candles"][-1]["time"] == int(bars[-1].ts.timestamp())


def test_a_symbol_with_no_tape_is_an_empty_chart_not_an_error(client):
    body = client.get("/api/candles/AAPL").json()
    assert body["candles"] == []


def test_the_panel_is_on_the_page_and_the_chart_library_is_local(client):
    page = client.get("/").text
    assert 's-candidate' in page
    # The library is vendored: a dashboard that needs a CDN is a dashboard
    # that goes blank on a train.
    assert "vendor-lightweight-charts.js" in page
    assert "cdn." not in page
    assert client.get("/static/vendor-lightweight-charts.js").status_code == 200


def test_every_asset_is_stamped_not_a_hand_kept_list(client):
    """A vendored library upgraded in place must not stay cached for a year.

    Stamping only the files somebody remembered to list is how a `git pull`
    updates the code and leaves the browser running the old one.
    """
    import re

    page = client.get("/").text
    unstamped = re.findall(r'/static/([\w.-]+\.(?:js|css))(?!\?v=)', page)
    assert not unstamped, f"not cache-busted: {unstamped}"
    assert "vendor-lightweight-charts.js?v=" in page


def test_the_dashboard_names_the_profile_it_is_running(client):
    """Running the scalp desk while reading a screen that says nothing about
    it is the kind of confusion that costs money."""
    body = client.get("/api/status").json()
    assert body["profile"] == "default"
    assert body["session"]["timeframe"] == "5m"
    assert body["session"]["dte"] == "7-14"


def test_the_activity_log_can_drop_the_scanning_chatter(client):
    """On a 1-minute desk the full stream holds about twenty minutes.

    Thirteen events a minute evict a 13:10 trade by 13:33 — by its own desk's
    scanning noise — so anyone looking in the afternoon would see a wall of
    "no setup" and conclude nothing had happened all day.
    """
    log = client.desk.activity
    log.add("trade.open", "QQQ 739C x2")
    for i in range(500):                       # a few hours of chatter
        log.add("setup.pass", f"QQQ no setup {i}")

    full = client.get("/api/activity?limit=120").json()
    assert all(e["kind"] == "setup.pass" for e in full["events"]), \
        "the trade should have rolled out of the live window"

    decisions = client.get("/api/activity?limit=120&decisions=true").json()
    kinds = [e["kind"] for e in decisions["events"]]
    assert "trade.open" in kinds, "a decision must survive the chatter"
    assert "setup.pass" not in kinds
    assert decisions["decisions"] >= 1


def test_a_decision_is_still_reachable_from_the_merged_stream(client):
    """With less chatter the full stream should still carry it, so the
    default view is not lying by omission on a slow day."""
    log = client.desk.activity
    log.add("trade.open", "QQQ 739C x2")
    for i in range(20):
        log.add("setup.pass", f"QQQ no setup {i}")
    kinds = [e["kind"] for e in client.get("/api/activity?limit=120").json()["events"]]
    assert "trade.open" in kinds


# --------------------------------------------------------------------------- #
# The watchlist endpoint
# --------------------------------------------------------------------------- #
@pytest.fixture
def _watch_elsewhere(tmp_path, monkeypatch):
    from panaoptions import watchlist as wl

    monkeypatch.setattr(wl, "STORE", tmp_path / "watchlist.json")
    return wl


def test_the_watchlist_says_where_the_symbols_came_from(client,
                                                        _watch_elsewhere):
    body = client.get("/api/watchlist").json()
    assert body["source"] == "config"
    assert body["symbols"] == client.desk.cfg.symbols

    client.post("/api/watchlist", json={"symbols": "QQQ, SPY"})
    after = client.get("/api/watchlist").json()
    assert after["source"] == "custom" and after["symbols"] == ["QQQ", "SPY"]


def test_a_bad_list_comes_back_as_a_message_not_a_stack_trace(client,
                                                              _watch_elsewhere):
    res = client.post("/api/watchlist", json={"symbols": "QQQ, ticker!"})
    assert res.status_code == 400
    assert "TICKER!" in res.json()["detail"]
    # And nothing changed.
    assert client.get("/api/watchlist").json()["source"] == "config"


def test_setting_the_watchlist_cannot_open_a_position(client, _watch_elsewhere):
    """The line this feature must not cross.

    Choosing what to look at is not choosing what to buy. Every rule still
    has to agree, and the dashboard has no way to make one.
    """
    before = dict(client.desk.ledger.open_trades)
    client.post("/api/watchlist", json={"symbols": "QQQ, SPY, AAPL"})
    assert client.desk.ledger.open_trades == before


def test_a_watchlist_change_does_not_touch_an_open_position(client,
                                                            _watch_elsewhere):
    """Dropping a symbol stops NEW trades in it. It does not liquidate.

    The open trade was taken under rules that still apply and its exit is
    already defined; closing it because somebody edited a text box would be a
    trading decision made by the dashboard.
    """
    client.post("/api/watchlist", json={"symbols": "QQQ"})
    desk = client.desk
    desk.ledger.open_trades["PT-X"] = object()
    client.post("/api/watchlist", json={"symbols": "SPY"})
    assert "PT-X" in desk.ledger.open_trades


def test_the_reset_button_goes_back_to_the_config(client, _watch_elsewhere):
    client.post("/api/watchlist", json={"symbols": "QQQ"})
    body = client.post("/api/watchlist/reset").json()
    assert body["source"] == "config"
    assert client.get("/api/watchlist").json()["source"] == "config"


def test_the_box_is_on_the_page(client):
    page = client.get("/").text
    assert 'id="watch-input"' in page
    assert "cannot open or close a position" in page


def test_the_page_offers_a_notification_toggle(client):
    """Notifications are opt-in and only for trades.

    Alerting on every setup considered would be a notification every few
    seconds on a 1-minute desk across three symbols, and would be switched
    off within the hour.
    """
    page = client.get("/").text
    assert 'id="notify-trades"' in page
    js = client.get("/static/app.js").text
    assert '"trade.open"' in js and '"trade.exit"' in js
    assert "setup.pass" not in js.split("NOTIFY_KINDS")[1][:200]
    # Opening the page must not replay the day as a burst of alerts.
    assert "notifySeeded" in js


def test_the_record_shows_a_position_the_moment_it_is_bought(client):
    """Counting only closed trades left "Trades 0" on screen while a paper
    trade was live, which reads as "nothing was bought"."""
    body = client.get("/api/trades").json()
    assert body["open"] == []

    from datetime import UTC, datetime

    from panaoptions.models import Direction, PaperTrade, SetupType

    client.desk.ledger.open_trades["PT-1"] = PaperTrade(
        id="PT-1", signal_id="S1", symbol="AVGO", direction=Direction.SHORT,
        contract_label="AVGO 2026-10-16 350P",
        opened_at=datetime(2026, 9, 24, 13, 50, tzinfo=UTC),
        quantity=2, entry_price=3.20, stop_price=1.80, target_1=4.48,
        target_2=5.44, remaining=2, strategy=SetupType.CANDLESTICK_AT_LEVEL)

    body = client.get("/api/trades").json()
    assert len(body["open"]) == 1
    held = body["open"][0]
    assert held["symbol"] == "AVGO" and held["remaining"] == 2
    assert held["entry_price"] == 3.20


def test_the_status_says_whether_option_chains_are_available(client):
    """Charts and chains are different endpoints and fail independently.

    A desk whose charts work can still be unable to price a single contract,
    and from the outside that is indistinguishable from a quiet market.
    """
    body = client.get("/api/status").json()
    assert "feed" in body
    assert "options_available" in body["feed"]


def test_the_page_can_warn_that_chains_are_down(client):
    page = client.get("/").text
    assert 'id="feed-health"' in page
    js = client.get("/static/app.js").text
    assert "Option chains are not available" in js


def test_the_dashboard_names_the_live_data_source(client):
    """Which source is serving changes what the numbers mean.

    Yahoo's delta is a Black-Scholes estimate, CBOE's and Tradier's are
    quoted; CBOE is delayed and Yahoo is not. After an automatic fallback the
    live source is not the configured one, so the page must name the live
    one — and say whether it is delayed and whether its greeks are real.
    """
    feed = client.get("/api/status").json()["feed"]
    assert feed["provider"] in ("auto", "yahoo", "cboe", "tradier")
    assert feed["configured"] in ("auto", "yahoo", "cboe", "tradier")
    assert "delayed" in feed and "greeks" in feed
    assert feed["note"]


def test_check_distinguishes_no_feed_from_no_chains():
    """start.sh keys off these, and only one of them is fatal.

    A desk that can screen, chart and show WHY nothing is being bought is
    worth having on screen; refusing to start would hide the banner that
    explains it.
    """
    import run

    assert run.CHECK_OK == 0
    assert run.CHECK_NO_FEED == 1
    assert run.CHECK_NO_CHAINS == 2


# --------------------------------------------------------------------------- #
# The candidate chart shows one session
# --------------------------------------------------------------------------- #
def _three_days(monkeypatch, client):
    """Three sessions of 5m bars, as the feed returns them."""
    from datetime import UTC, datetime, timedelta

    from panaoptions.models import Candle

    bars = []
    for day in (22, 23, 24):
        bell = datetime(2026, 9, day, 13, 30, tzinfo=UTC)   # 09:30 ET
        bars += [Candle(ts=bell + timedelta(minutes=5 * i), open=100, high=101,
                        low=99, close=100.5, volume=1000.0) for i in range(78)]

    async def _bars(symbol, interval="5m", include_prepost=False):
        return bars

    monkeypatch.setattr(client.desk.feed, "candles", _bars)
    return bars


def test_the_chart_shows_only_the_latest_session(client, monkeypatch):
    """Three days of 5m bars squeeze today into the right-hand third.

    The pattern fired on today's tape, so today's tape is what the panel
    should be showing.
    """
    _three_days(monkeypatch, client)
    body = client.get("/api/candles/AAPL").json()
    assert body["session_only"] is True

    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    days = {datetime.fromtimestamp(c["time"], tz=UTC)
            .astimezone(ZoneInfo("America/New_York")).date()
            for c in body["candles"]}
    assert len(days) == 1
    assert next(iter(days)).day == 24


def test_the_full_history_is_still_available_on_request(client, monkeypatch):
    _three_days(monkeypatch, client)
    body = client.get("/api/candles/AAPL?session=false").json()
    assert body["session_only"] is False
    assert len(body["candles"]) > 78


def test_today_is_the_last_bars_date_not_the_wall_clock(client, monkeypatch):
    """Before the open, after the close and at a weekend, the panel should
    still show a complete last session rather than an empty chart."""
    _three_days(monkeypatch, client)
    body = client.get("/api/candles/AAPL").json()
    assert body["candles"], "an out-of-hours chart must not come back empty"


def test_the_chart_is_told_which_clock_the_session_is_on(client, monkeypatch):
    """The library renders epochs in UTC and the browser is wherever the
    viewer is. Neither is the market's clock."""
    _three_days(monkeypatch, client)
    assert client.get("/api/candles/AAPL").json()["timezone"] == \
        "America/New_York"

    js = client.get("/static/app.js").text
    assert "tickMarkFormatter" in js
    # The VWAP must reset on the exchange's day, not the viewer's: from India
    # the local day rolls over at 00:30 ET, mid-session.
    assert "exchangeDay(c.time)" in js
    # The old local-clock version, which must not come back.
    assert "new Date(c.time * 1000).toDateString()" not in js


def test_the_chart_gets_warm_up_bars_for_its_lines(client, monkeypatch):
    """An EMA 50 started from nine bars of today is not an EMA 50, so the
    bars before the session come back separately for the lines to use."""
    _three_days(monkeypatch, client)
    body = client.get("/api/candles/AAPL?warmup=60").json()
    assert len(body["warmup"]) == 60
    assert body["warmup"][-1]["time"] < body["candles"][0]["time"]
