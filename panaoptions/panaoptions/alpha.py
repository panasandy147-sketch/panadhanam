"""The alpha engine: what the tape says, and nothing else.

Every strategy and every candlestick evaluator ends here as a plain signal
dictionary with exactly five keys:

    symbol              the underlying
    direction           "LONG" (a call) or "SHORT" (a put)
    trigger_price       the underlying price that makes it an entry
    invalidation_level  the underlying price that proves it wrong — the stop
    confidence_score    0.0-1.0, how much the tape agrees with itself

No account balance, no contract, no chain and no broker connection is read
anywhere on this path. Capital, spreads and delta bands are the Risk
Gatekeeper's business (risk/gatekeeper.py), and it sees a signal only after
the alpha engine has produced it. Keeping the two apart is what lets a
strategy be judged on whether it reads the market well, separately from
whether the account could afford the contract that day.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, TypedDict

import pandas as pd

from panaoptions.engine import patterns
from panaoptions.logging import get_logger
from panaoptions.models import Candle, Direction, SessionLevels, Setup

log = get_logger("alpha")

SIGNAL_KEYS: tuple[str, ...] = ("symbol", "direction", "trigger_price",
                                "invalidation_level", "confidence_score")


class SignalDict(TypedDict):
    """The one shape that leaves the alpha engine."""
    symbol: str
    direction: str
    trigger_price: float
    invalidation_level: float
    confidence_score: float


@dataclass(frozen=True)
class AlphaSignal:
    """A signal plus where it came from.

    `to_dict()` is the contract with the rest of the desk and carries only the
    five signal keys. `source` and `pattern` ride alongside so the CMIO can
    apply a learned per-strategy weight and the journal can say which rule
    fired — they are provenance, not inputs to any decision about money.
    """
    symbol: str
    direction: str
    trigger_price: float
    invalidation_level: float
    confidence_score: float
    source: str = ""
    pattern: str = ""
    # The projected target on the underlying (engine/reward.py). Not one of
    # the five keys: the risk desk reads it for the 1:3 gate.
    target_price: float = 0.0

    def to_dict(self) -> SignalDict:
        return SignalDict(symbol=self.symbol, direction=self.direction,
                          trigger_price=round(self.trigger_price, 4),
                          invalidation_level=round(self.invalidation_level, 4),
                          confidence_score=round(self.confidence_score, 3))

    @property
    def long(self) -> bool:
        return self.direction == Direction.LONG.value


# --------------------------------------------------------------------------- #
def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def validate(signal: Mapping[str, Any]) -> list[str]:
    """What is wrong with a signal dictionary, or [] when it is well formed.

    A long must have its invalidation BELOW the trigger and a short above it;
    anything else is a stop on the wrong side of the entry.
    """
    problems: list[str] = []
    missing = [k for k in SIGNAL_KEYS if k not in signal]
    if missing:
        return [f"missing {', '.join(missing)}"]
    extra = sorted(set(signal) - set(SIGNAL_KEYS))
    if extra:
        problems.append(f"unexpected keys {', '.join(extra)}")
    direction = str(signal["direction"])
    if direction not in (Direction.LONG.value, Direction.SHORT.value):
        problems.append(f"direction {direction!r} is not LONG or SHORT")
    try:
        trigger = float(signal["trigger_price"])
        stop = float(signal["invalidation_level"])
        score = float(signal["confidence_score"])
    except (TypeError, ValueError):
        return problems + ["prices and score must be numbers"]
    if trigger <= 0 or stop <= 0:
        problems.append("trigger and invalidation must be positive prices")
    elif direction == Direction.LONG.value and stop >= trigger:
        problems.append(f"a long's invalidation {stop:.2f} is not below its "
                        f"trigger {trigger:.2f}")
    elif direction == Direction.SHORT.value and stop <= trigger:
        problems.append(f"a short's invalidation {stop:.2f} is not above its "
                        f"trigger {trigger:.2f}")
    if not 0.0 <= score <= 1.0:
        problems.append(f"confidence {score} is outside 0-1")
    return problems


def confidence(setup: Setup) -> float:
    """How strongly the tape agrees with itself, from the setup's own evidence.

    0.45 for a bare trigger, +0.08 for each independent confirmation (up to
    four), +0.05 when the trend agrees. A published accuracy, when the pattern
    has one, is blended in at a fifth — it is somebody else's daily-bar study,
    so it nudges rather than decides.
    """
    score = 0.45 + 0.08 * min(len(setup.confirmations), 4)
    if setup.trend_aligned:
        score += 0.05
    if setup.claimed_accuracy:
        score = 0.8 * score + 0.2 * setup.claimed_accuracy
    return round(_clamp(score), 3)


def from_setup(setup: Setup) -> AlphaSignal | None:
    """A triggered setup as a signal, or None if it is not a usable one."""
    if not setup.triggered:
        return None
    trigger = setup.entry_trigger or setup.indicators.close
    signal = AlphaSignal(
        symbol=setup.symbol, direction=setup.direction.value,
        trigger_price=float(trigger),
        invalidation_level=float(setup.underlying_support),
        confidence_score=confidence(setup),
        source=setup.strategy.name.lower(), pattern=setup.pattern,
        target_price=float(setup.underlying_target or 0.0))
    problems = validate(signal.to_dict())
    if problems:
        log.info("%s %s dropped: %s", setup.symbol, setup.strategy.value,
                 "; ".join(problems))
        return None
    return signal


def evaluate(symbol: str, candles: list[Candle], levels: SessionLevels,
             cfg: Any) -> tuple[AlphaSignal | None, Setup | None, list[Setup]]:
    """Run the strategies and return (signal, winning setup, all attempts).

    `cfg` is read only for strategy windows and thresholds; nothing about
    capital or contracts. The Setup comes back too because it carries the
    written case for the trade, which the dashboard and journal show.
    """
    from panaoptions.engine import strategies

    setup, attempts = strategies.evaluate_all(symbol, candles, levels, cfg)
    signal = from_setup(setup) if setup is not None else None
    return signal, setup, attempts


def pattern_signals(symbol: str, frame: pd.DataFrame,
                    allowed: set[str] | None = None) -> list[SignalDict]:
    """Every candlestick pattern completing on the last bar, as signals.

    The raw evaluators, with no level or trend gate — those belong to the
    Candlestick-at-a-Level strategy. Multi-bar structures score higher than a
    single wick because they carry more confirmation.
    """
    out: list[SignalDict] = []
    for hit in patterns.detect_all(frame):
        if allowed is not None and hit.name not in allowed:
            continue
        signal = AlphaSignal(
            symbol=symbol,
            direction=(Direction.LONG if hit.bullish else Direction.SHORT).value,
            trigger_price=hit.trigger, invalidation_level=hit.invalidation,
            confidence_score=round(_clamp(0.5 + 0.08 * (hit.bars - 1)), 3),
            source="pattern", pattern=hit.name)
        if not validate(signal.to_dict()):
            out.append(signal.to_dict())
    return out
