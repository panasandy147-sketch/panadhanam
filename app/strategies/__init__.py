"""Rule-based strategy detectors the analysts run (pure: bars in, signals out)."""
from __future__ import annotations

# The user's strategies that bring their OWN plan: the stop where the
# strategy puts it (<section>.stop_ticks beyond), the target at their own
# 1:rr (<section>.rr, judged at that, not the desk's 1:3), no time stop, and
# optional breakeven / trail (<section>.breakeven_r / .trail_r).
#   setup name -> (settings section, default rr)
OWN_PLAN: dict[str, tuple[str, float]] = {
    "SJK 1 · 50/200 EMA Pullback": ("sjk1", 2.5),
    "SJK 9-15-21 · EMA Fan": ("sjk_9_15_21", 2.0),
}
