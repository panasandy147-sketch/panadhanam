import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _desk_files_elsewhere(tmp_path, monkeypatch, request):
    """Never let a test write into the desk's own data folder.

    The ledger store and the watchlist copy DATA_DIR when they are imported,
    so patching config.DATA_DIR alone did not reach them: tests appended rows
    to the real data/trades.csv and a browser test saved a watchlist over the
    real one — fake trades in someone's paper record, and a watchlist they
    never chose.
    """
    from panaoptions import config as config_mod
    from panaoptions import watchlist
    from panaoptions.ledger import store

    data = tmp_path / "desk-data"
    data.mkdir(exist_ok=True)
    monkeypatch.setattr(store, "DATA_DIR", data)
    monkeypatch.setattr(watchlist, "STORE", data / "watchlist.json")
    # The connection is cached per process: without a fresh one, every test
    # after the first reads and writes the FIRST test's database, and journal
    # rows from one test turn up in the next one's weekly numbers.
    # The browser suite runs one live server across its tests and keeps its
    # own database for the whole session, so it is left alone.
    if not request.module.__name__.endswith("test_browser_panels"):
        monkeypatch.setattr(store, "_conn", None)
    # What this machine's Friday reflection learned is not the shipped config.
    monkeypatch.setattr(config_mod, "LEARNED_PATH", tmp_path / "learned.yaml")
    # The committee votes by rules alone in tests: no test may depend on
    # whether an Ollama happens to be running on the machine.
    monkeypatch.setenv("PANAOPTIONS_AGENT_LLM", "off")
    # Nor on Yahoo's screeners: the auto watchlist is off unless a test
    # turns it on (and gives it fake sources).
    monkeypatch.setenv("PANAOPTIONS_AUTO_WATCHLIST", "off")
    # Every test starts on the US, whatever this machine's toggle says.
    from panaoptions import markets
    monkeypatch.delenv("PANAOPTIONS_MARKET", raising=False)
    monkeypatch.delenv("PANAOPTIONS_CAPITAL_IN", raising=False)
    monkeypatch.setattr(markets, "_active", "US")
    monkeypatch.setattr(markets, "_home", {})
    monkeypatch.setattr(config_mod, "DATA_DIR", data)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """The shipped config, isolated from this machine.

    A real .env in the working copy must not reach the tests. If it does, the
    suite passes or fails according to a file that is not in the repository —
    so it would pass here and fail in CI, or worse, pass in CI and hide a
    break that only shows up on the developer's machine.
    """
    from panaoptions import config as config_mod

    monkeypatch.setattr(config_mod, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config_mod, "ENV_PATH", tmp_path / "absent.env")
    for key in ("PANAOPTIONS_CAPITAL", "PANAOPTIONS_PROVIDER",
                "DISCORD_WEBHOOK_URL", "TELEGRAM_BOT_TOKEN",
                "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(key, raising=False)

    cfg = config_mod.Config()
    # A fixed, deliberately small account. Twenty-odd tests assert exact
    # dollar figures — position sizes, the daily limit, what the budget can
    # buy — and pinning the baseline here keeps a change to the SHIPPED
    # capital from rewriting all of them. Tests about the shipped figure read
    # it from the file instead; see test_preflight.
    cfg.data["account"]["starting_capital"] = 500.0
    # The same for the throttles: the loop tests predate the tournament
    # rules (2% risk a trade, 2 open, 4 a day, 3% breaker) and assert figures
    # under the old ones. tests/test_throttles.py tests the shipped rules.
    cfg.data["risk"].update({"max_risk_per_trade_pct": 0, "max_daily_trades": 0,
                             "max_open_trades": 3, "daily_loss_limit_pct": 10.0,
                             # the premium targets the ledger tests are written
                             # against; tests/test_exit_plan.py tests r_multiple
                             "exit_style": "auto",
                             # the 20% / 25% / 45% budgets the sizing tests are
                             # written against ($5,000 at 30% since 30 Sept is
                             # tested in test_throttles and test_preflight)
                             "max_capital_deployed_pct": 20.0,
                             "index_max_capital_deployed_pct": 25.0,
                             "max_total_deployed_pct": 45.0})
    # The midday RVOL window (1.2x from 10:30): the RVOL tests are written
    # against the 1.5x gate; tests/test_no_trade_audit.py tests the window.
    cfg.data["technical"]["midday_rvol"] = {"enabled": False}
    # Likewise the previous-day go/no-go on the candlestick strategies;
    # tests/test_go_no_go.py tests it.
    cfg.data["fno"].setdefault("go_no_go", {})["enabled"] = False
    # The loop tests' tapes are made up and not on the desk's clock (a 09:50
    # cycle reads bars up to 10:15): the closed-bar and stale-tape rules are
    # off for them; tests/test_no_trade_audit.py tests both.
    cfg.data["technical"].update(completed_bars_only=False, stale_after_bars=0)
    return cfg


@pytest.fixture(autouse=True)
def _journal_elsewhere(tmp_path, monkeypatch):
    """Never let a test write into the repository's journal.

    `journal/` is committed on purpose — it is the learning history. A suite
    that appends synthetic cards to it poisons exactly the record the weekly
    review reads, and the damage looks like real trading rather than a test.
    """
    from panaoptions.journal import store as journal_store
    from panaoptions.journal import weekly as journal_weekly

    monkeypatch.setattr(journal_store, "JOURNAL_DIR", tmp_path / "journal")
    monkeypatch.setattr(journal_weekly, "DAILY_DIR", tmp_path / "journal" / "daily")
    monkeypatch.setattr(journal_weekly, "WEEKLY_DIR", tmp_path / "journal" / "weekly")


@pytest.fixture
def bars():
    """A clean 5-minute uptrend: 40 bars, steadily higher, even volume."""
    from datetime import datetime, timedelta

    from panaoptions.models import Candle
    base = datetime(2026, 9, 22, 9, 30)
    out, price = [], 100.0
    for i in range(40):
        price *= 1.002
        out.append(Candle(ts=base + timedelta(minutes=5 * i), open=price * 0.999,
                          high=price * 1.003, low=price * 0.997, close=price,
                          volume=1000.0))
    return out


@pytest.fixture
def shipped(tmp_path, monkeypatch):
    """settings.yaml exactly as it ships, with no .env override in sight.

    The `cfg` fixture pins a small account so arithmetic assertions stay
    still. This one is the opposite: it is for asserting things about the
    figures the project actually ships with, so a developer's own .env — or
    the absence of one — cannot change the answer.
    """
    from panaoptions import config as config_mod

    monkeypatch.setattr(config_mod, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config_mod, "ENV_PATH", tmp_path / "absent.env")
    for key in ("PANAOPTIONS_CAPITAL", "PANAOPTIONS_PROVIDER"):
        monkeypatch.delenv(key, raising=False)
    return config_mod.Config()
