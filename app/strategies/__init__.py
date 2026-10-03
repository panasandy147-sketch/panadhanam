"""Rule-based strategy detectors the analysts run (pure: bars in, signals out)."""
from __future__ import annotations

# SJK 50-200 was called "SJK 1" until 5 Oct 2026 (the user's rename).
LEGACY_SJK1 = "SJK 1 · 50/200 EMA Pullback"

# The user's strategies that bring their OWN plan: the stop where the
# strategy puts it (<section>.stop_ticks beyond), the target at their own
# 1:rr (<section>.rr, judged at that, not the desk's 1:3), no time stop, and
# optional breakeven / trail (<section>.breakeven_r / .trail_r).
#   setup name -> (settings section, default rr)
OWN_PLAN: dict[str, tuple[str, float]] = {
    "SJK 50-200 · EMA Pullback": ("sjk50_200", 2.5),
    # Its name until 5 Oct 2026 — trades recorded then still find their plan.
    LEGACY_SJK1: ("sjk50_200", 2.5),
    "SJK 9-15-21 · EMA Fan": ("sjk_9_15_21", 2.0),
    "SJK 9/21 · VWAP · ADX": ("sjk912_vwapadx", 2.0),
    "sjk912RSi · 9/21 EMA + RSI": ("sjk912rsi", 2.0),
}
