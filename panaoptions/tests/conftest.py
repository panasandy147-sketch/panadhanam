import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    from panaoptions import config as config_mod
    monkeypatch.setattr(config_mod, "DATA_DIR", tmp_path / "data")
    c = config_mod.Config()
    return c


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
