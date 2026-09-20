import os
import tempfile
from pathlib import Path

import pytest

# Isolate the test DB from the real runtime one.
_tmp = tempfile.mkdtemp(prefix="panadhanam-test-")
os.environ.setdefault("LOG_LEVEL", "WARNING")

import app.core.config as config_mod  # noqa: E402

config_mod.DATA_DIR = Path(_tmp)


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
