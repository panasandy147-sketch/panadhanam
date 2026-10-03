"""Seeded random 5-minute tapes for the strategy engines' tests: drifting
legs of 40 bars with noise, so real crossovers, pullbacks and chop occur.
Deterministic: the same seed is always the same tape."""
from __future__ import annotations

import random
from datetime import datetime, timedelta

from app.core.models import Candle


def tape(seed: int, n: int = 200, start: datetime = datetime(2026, 10, 5, 9, 15)):
    rnd = random.Random(seed)
    px, drift = [100.0], 0.0
    for i in range(n):
        if i % 40 == 0:
            drift = rnd.choice([-0.12, -0.06, 0.0, 0.06, 0.12])
        px.append(px[-1] + drift + rnd.gauss(0, 0.12))
    out = []
    for i, x in enumerate(px[1:], 1):
        o = px[i - 1]
        out.append(Candle(
            ts=start + timedelta(minutes=5 * i), open=o,
            high=max(o, x) + abs(rnd.gauss(0, 0.05)), low=min(o, x) - abs(rnd.gauss(0, 0.05)),
            close=x, volume=1000 + rnd.random() * 500))
    return out
