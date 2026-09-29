"""Which option each pattern asks for — kept out of the strategies.

A strategy's job is to read the tape: where to get in, and where the idea is
wrong on the UNDERLYING. Which contract expresses that idea — its delta band
and its expiry window — is a derivatives question, so it lives here and is
read by the contract picker and the Derivatives & Flow agent, never by the
alpha engine.

The config (`strategies.candlestick_at_level.patterns.<key>`) wins over the
table below; the table is the fallback for a pattern the config omits.
"""
from __future__ import annotations

from typing import Any

# (delta band, DTE window) per pattern. A sharp reversal off a level and a
# four-candle structural turn are not the same bet and do not want the same
# contract.
DEFAULT_CONTRACT: dict[str, tuple[tuple[float, float], tuple[int, int]]] = {
    "Hammer":                    ((0.50, 0.65), (14, 30)),
    "Bullish Engulfing":         ((0.55, 0.70), (14, 30)),
    "Morning Star":              ((0.45, 0.55), (14, 30)),
    "Tweezer Bottom":            ((0.50, 0.65), (14, 30)),
    "Shooting Star":             ((0.50, 0.60), (14, 30)),
    "Bearish Engulfing":         ((0.55, 0.65), (14, 30)),
    "Evening Star":              ((0.45, 0.55), (14, 30)),
    "Tweezer Top":               ((0.50, 0.65), (14, 30)),
    "Bullish Three-Line Strike": ((0.65, 0.75), (30, 45)),
    "Bearish Three-Line Strike": ((0.65, 0.75), (30, 45)),
    "Three White Soldiers":      ((0.50, 0.60), (30, 45)),
    "Three Black Crows":         ((0.55, 0.65), (21, 35)),
    "Bullish Abandoned Baby":    ((0.50, 0.60), (14, 30)),
    "Bearish Abandoned Baby":    ((0.50, 0.60), (14, 30)),
    "Piercing Line":             ((0.50, 0.60), (14, 30)),
    "Dark Cloud Cover":          ((0.50, 0.60), (14, 30)),
    "Liquidity Sweep Rejection": ((0.55, 0.65), (14, 30)),
    "Double Rejection Top":      ((0.40, 0.50), (14, 30)),
    "Double Rejection Bottom":   ((0.40, 0.50), (14, 30)),
}

_PREFIX = "strategies.candlestick_at_level.patterns"


def key(pattern: str) -> str:
    """'Bullish Three-Line Strike' -> 'bullish_three_line_strike'."""
    return pattern.lower().replace(" ", "_").replace("-", "_")


def delta_band(cfg: Any, pattern: str) -> tuple[float, float]:
    """The delta band this pattern asks for.

    `contracts.primary_band` holds every pattern, call or put, to one primary
    tier (0.40-0.50); the over-budget ladder handles the rest.
    """
    primary = cfg.get("contracts.primary_band")
    if isinstance(primary, list | tuple) and len(primary) == 2:
        return float(primary[0]), float(primary[1])
    configured = cfg.get(f"{_PREFIX}.{key(pattern)}.delta")
    if isinstance(configured, list | tuple) and len(configured) == 2:
        return float(configured[0]), float(configured[1])
    return DEFAULT_CONTRACT.get(pattern, ((0.45, 0.60), (0, 0)))[0]


def dte_window(cfg: Any, pattern: str) -> tuple[int, int]:
    """How much time this pattern's thesis needs to play out."""
    configured = cfg.get(f"{_PREFIX}.{key(pattern)}.dte")
    if isinstance(configured, list | tuple) and len(configured) == 2:
        return int(configured[0]), int(configured[1])
    fallback = DEFAULT_CONTRACT.get(pattern, (None, (0, 0)))[1]
    if fallback != (0, 0):
        return fallback
    return (int(cfg.get("strategies.candlestick_at_level.min_dte", 14)),
            int(cfg.get("strategies.candlestick_at_level.max_dte", 30)))


def claimed_accuracy(cfg: Any, pattern: str) -> float:
    """The win rate this pattern is published as having, or 0."""
    try:
        return float(cfg.get(f"{_PREFIX}.{key(pattern)}.claimed_accuracy"))
    except (TypeError, ValueError):
        return 0.0
