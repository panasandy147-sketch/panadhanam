"""Unusual options activity: volume far above open interest."""
from __future__ import annotations

from datetime import datetime

import pytest

from panaoptions.engine import flow
from panaoptions.models import OptionContract, OptionRight


def _c(right, strike, volume, oi):
    return OptionContract(symbol="CCC", right=right, strike=strike,
                          expiry="2026-12-18", dte=84, bid=0.45, ask=0.50,
                          delta=0.3, volume=volume, open_interest=oi)


def test_the_ccc_sweep_is_unusual_and_bullish(cfg):
    """12,400 calls against 3,800 open interest — the morning's example."""
    chain = [_c(OptionRight.CALL, 7.5, 12_400, 3_800),
             _c(OptionRight.CALL, 10, 200, 5_000),
             _c(OptionRight.PUT, 7.5, 300, 2_000)]
    seen = flow.scan(chain, cfg)
    assert seen.found and seen.bias == 1
    assert "12,400 vs 3,800" in seen.headline()


def test_ordinary_volume_is_not_unusual(cfg):
    chain = [_c(OptionRight.CALL, 7.5, 2_000, 3_800),        # under 3x OI
             _c(OptionRight.PUT, 7.5, 900, 100)]              # under 1,000 lots
    assert not flow.scan(chain, cfg).found


def test_heavy_puts_read_bearish(cfg):
    chain = [_c(OptionRight.PUT, 33, 6_000, 1_000), _c(OptionRight.CALL, 35, 1_500, 400)]
    assert flow.scan(chain, cfg).bias == -1


@pytest.mark.asyncio
async def test_unusual_flow_passes_the_screen_without_a_gap(cfg):
    """No 1% gap, no 1.5x RVOL — but a sweep. That is a catalyst."""
    from zoneinfo import ZoneInfo

    from panaoptions.data.premarket import screen

    class _Feed:
        async def quote(self, symbol):
            return {"previous_close": 7.40, "last_price": 7.42}

        async def candles(self, symbol, tf, include_prepost=False):
            return []

        async def chain_for_window(self, symbol, spot, lo, hi):
            return [_c(OptionRight.CALL, 7.5, 12_400, 3_800)]

    original = list(cfg.data["universe"]["symbols"])
    cfg.data["universe"]["symbols"] = ["CCC"]
    try:
        now = datetime(2026, 9, 25, 9, 20, tzinfo=ZoneInfo("America/New_York"))
        [read] = await screen(_Feed(), cfg, now)
        assert read.passed
        assert any("unusual options activity" in r for r in read.reasons)
    finally:
        cfg.data["universe"]["symbols"] = original
