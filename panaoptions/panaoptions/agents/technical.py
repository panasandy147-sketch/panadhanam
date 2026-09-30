"""Agent 1 — Alpha / Technical: does the chart back the signal?

Checks the 5-minute trigger against the 15-minute trend, and that the move
has participation: relative volume of at least 1.5x (`agents.technical.
min_rvol`) is a hard gate. RVOL is read two ways and the higher counts — the
trigger bar's volume against its recent average, and the session's volume
against the 20-day average at this time from the pre-market screen — because
a breakout on a name trading 3x its normal session volume is participated
even when the single trigger bar is ordinary.
"""
from __future__ import annotations

from typing import Any

from panaoptions.agents import consensus
from panaoptions.agents.base import AgentVote, ask, blend, clamp
from panaoptions.alpha import AlphaSignal
from panaoptions.engine import indicators as ta
from panaoptions.models import Candle, Setup

NAME = "Technical"


def trend_15m(candles: list[Candle]) -> int:
    """+1 up, -1 down, 0 flat, from the 15m close against its EMAs.

    20/50 EMA once there are 50 bars, 9/20 before that, so the first hours of
    a session still get a read.
    """
    if len(candles) < 21:
        return 0
    df15 = ta.resample(ta.to_frame(candles), "15min")
    close = df15["close"]
    if len(close) < 20:
        return 0
    fast, slow = (20, 50) if len(close) >= 50 else (9, 20)
    price = float(close.iloc[-1])
    ema_fast = float(ta.ema(close, fast).iloc[-1])
    ema_slow = float(ta.ema(close, slow).iloc[-1])
    if price > ema_fast > ema_slow:
        return 1
    if price < ema_fast < ema_slow:
        return -1
    return 0


async def vote(signal: AlphaSignal, setup: Setup, candles: list[Candle],
               cfg: Any, screen_rvol: float = 0.0,
               flow: consensus.FlowRead = consensus.NONE) -> AgentVote:
    """Score the signal on chart evidence.

    The one thing it takes from the options side is the Derivatives
    Confluence Override: extreme flow (>= 10x OI) on the trade's own side
    relaxes the RVOL gate from 1.5x to 1.3x (agents/consensus.py).
    """
    min_rvol, override = consensus.rvol_floor(cfg, signal, flow)
    bar_rvol = float(setup.indicators.rvol or 0.0)
    rvol = max(bar_rvol, float(screen_rvol or 0.0))
    want = 1 if signal.long else -1
    trend = trend_15m(candles)

    result = AgentVote(agent=NAME, score=signal.confidence_score,
                       data={"rvol": round(rvol, 2), "bar_rvol": round(bar_rvol, 2),
                             "screen_rvol": round(float(screen_rvol or 0), 2),
                             "trend_15m": trend})
    unmeasured = bool(candles) and not any(float(c.volume or 0) > 0 for c in candles)
    if unmeasured:
        # The NSE indices carry no volume on the feed: 0.00x is "not
        # measured", not "quiet", and vetoing on it barred NIFTY, BANKNIFTY
        # and FINNIFTY options for good.
        result.data["rvol_unmeasured"] = True
        result.reasons.append("RVOL not measurable (no volume in the feed for this "
                              "index) — the volume gate does not apply")
    elif rvol < min_rvol:
        result.veto = True
        result.reasons.append(
            f"relative volume {rvol:.2f}x is below {min_rvol:g}x (bar "
            f"{bar_rvol:.2f}x, session {float(screen_rvol or 0):.2f}x)")
    else:
        result.reasons.append(f"RVOL {rvol:.2f}x ≥ {min_rvol:g}x")
    if override:
        result.reasons.append(override)
        result.data["rvol_override"] = override

    # Volume profile confluence: +0.30 at a VAL/POC (calls) or VAH/POC (puts);
    # a thick HVN straight ahead costs points, or vetoes when it is right there.
    profile = consensus.profile_confluence(signal, setup, candles, cfg)
    result.score += profile.boost
    result.reasons.extend(profile.notes)
    if profile.veto:
        result.veto = True
        result.reasons.insert(0, f"volume profile veto: {profile.veto}")
    if profile.profiles:
        result.data["volume_profile"] = profile.profiles

    if trend == want:
        result.score += 0.10
        result.reasons.append("15m trend agrees")
    elif trend == -want:
        result.score -= 0.20
        result.reasons.append("15m trend opposes the 5m trigger")
    else:
        result.reasons.append("15m trend flat")
    result.reasons.append(f"{setup.strategy.value}: {setup.pattern}")
    result.score = round(clamp(result.score), 3)

    opinion = await ask(cfg, NAME,
                        "Judge whether the 5m/15m chart evidence supports "
                        "buying the option in this direction.",
                        {"signal": signal.to_dict(), "strategy": setup.strategy.value,
                         "pattern": setup.pattern,
                         "confirmations": setup.confirmations,
                         "rvol": result.data["rvol"],
                         "trend_15m": {1: "up", -1: "down", 0: "flat"}[trend]})
    return blend(result, opinion, cfg)
