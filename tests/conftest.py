import os
import tempfile
from pathlib import Path

import pytest

# Isolate the test DB from the real runtime one.
_tmp = tempfile.mkdtemp(prefix="panadhanam-test-")
os.environ.setdefault("LOG_LEVEL", "WARNING")

import app.core.config as config_mod  # noqa: E402

config_mod.DATA_DIR = Path(_tmp)

# Closed trades write markdown cards; without this the suite filled the repo's
# own journal/ with cards for trades that never happened.
import app.journal.store as journal_store  # noqa: E402

journal_store.JOURNAL_DIR = Path(_tmp) / "journal"


@pytest.fixture(autouse=True)
def pinned_simulator_clock(monkeypatch):
    """Freeze the paper broker's clock for every test.

    PaperBroker builds its synthetic tape anchored to `datetime.now()`, and the
    intraday volume smile is keyed to the time of day — so whether a breakout
    gets volume confirmation depends on what time the suite happens to run.
    That made an opportunity-board assertion pass in the evening and fail the
    next morning, with nothing in the diff to explain it.

    Pinning makes the tape reproducible. Only the simulator's clock is frozen;
    `app.core.clock`, which decides real session phases, is untouched.
    """
    import datetime as _dt

    import app.brokers.paper as paper

    class _Pinned(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            # Mid-session, so the bars look like a real trading day.
            return cls(2026, 9, 22, 14, 0, tzinfo=tz)

    monkeypatch.setattr(paper, "datetime", _Pinned)


@pytest.fixture
def cfg():
    from app.core.config import get_config
    c = get_config()
    c.reload()
    return c


@pytest.fixture
def candles():
    """A synthetic uptrend with a volume-backed breakout on the last bar."""
    from datetime import datetime, timedelta

    from app.core.models import Candle
    base = datetime(2025, 1, 2, 9, 15)
    out = []
    price = 100.0
    for i in range(80):
        price *= 1.002
        out.append(Candle(ts=base + timedelta(minutes=5 * i), open=price * 0.999,
                          high=price * 1.004, low=price * 0.996, close=price,
                          volume=100_000))
    # breakout bar
    price *= 1.02
    out.append(Candle(ts=base + timedelta(minutes=5 * 81), open=price * 0.985,
                      high=price * 1.002, low=price * 0.984, close=price,
                      volume=500_000))
    return out
