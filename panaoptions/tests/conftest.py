import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


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
