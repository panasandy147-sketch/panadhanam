"""Extreme options flow: when institutional positioning outranks the chart.

A contract trading at least `agents.derivatives.extreme_flow_ratio` (10x) its
open interest — IWM puts at 17.9x, 18.4x — is new money arriving in size with
a view. Three rules follow from it, and all three read the same `FlowRead`:

  Derivatives Confluence Override   extreme flow ON THE TRADE'S SIDE relaxes
                                    the Technical agent's relative-volume gate
                                    from 1.5x to `agents.technical.
                                    flow_confluence_min_rvol` (1.3x). A Tweezer
                                    Top at 1.41x spot volume under a 17.9x put
                                    sweep is participated — in the options.
  Derivatives Flow Conflict Veto    extreme flow AGAINST the trade is a strict
                                    veto: no calls into heavy institutional put
                                    buying, however clean the pattern. Neither
                                    the committee score nor the model can lift
                                    it.
  Derivative weight boost           while an extreme anomaly is on the tape the
                                    Derivatives agent's vote weight is
                                    multiplied by `agents.derivatives.
                                    extreme_flow_weight_multiplier` (2x).

Free feeds cannot see whether the volume traded at the ask or the bid, so
"put flow" means volume in puts — where the money went, not which side of it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from panaoptions.models import OptionContract, OptionRight


@dataclass(frozen=True)
class FlowRead:
    """The strongest extreme flow on the chain, if any."""
    bias: int                 # +1 calls, -1 puts, 0 none or both sides even
    ratio: float              # the largest volume / OI on the dominant side
    volume: int               # extreme volume on the dominant side
    label: str = ""           # the contract carrying the largest ratio

    @property
    def extreme(self) -> bool:
        return self.bias != 0

    @property
    def side(self) -> str:
        return {1: "call", -1: "put", 0: "no"}[self.bias]

    def line(self) -> str:
        if not self.extreme:
            return "no extreme options flow"
        return (f"extreme {self.side} flow: {self.label} traded {self.ratio:.1f}x its "
                f"open interest ({self.volume:,} contracts on the {self.side} side)")


NONE = FlowRead(0, 0.0, 0)


def _ratio(c: OptionContract) -> float:
    oi = int(c.open_interest or 0)
    return float(c.volume or 0) / oi if oi > 0 else float(c.volume or 0)


def read(chain: list[OptionContract], cfg: Any) -> FlowRead:
    """The extreme flow on `chain`: >= extreme_flow_ratio x OI and >= min volume."""
    threshold = float(cfg.get("agents.derivatives.extreme_flow_ratio", 10.0))
    floor = int(cfg.get("flow.min_volume", 1000))
    sides: dict[int, list[OptionContract]] = {1: [], -1: []}
    for c in chain or []:
        if int(c.volume or 0) >= floor and _ratio(c) >= threshold:
            sides[1 if c.right is OptionRight.CALL else -1].append(c)
    volume = {s: sum(int(c.volume or 0) for c in cs) for s, cs in sides.items()}
    if volume[1] == volume[-1]:
        return NONE
    bias = 1 if volume[1] > volume[-1] else -1
    top = max(sides[bias], key=_ratio)
    return FlowRead(bias, round(_ratio(top), 1), volume[bias], top.label)


def direction(signal: Any) -> int:
    return 1 if getattr(signal, "long", False) else -1


def rvol_floor(cfg: Any, signal: Any, flow: FlowRead) -> tuple[float, str]:
    """The Technical agent's RVOL gate: 1.5x, or 1.3x under agreeing extreme flow."""
    strict = float(cfg.get("agents.technical.min_rvol", 1.5))
    if flow.extreme and flow.bias == direction(signal):
        relaxed = float(cfg.get("agents.technical.flow_confluence_min_rvol", 1.3))
        return (min(strict, relaxed),
                f"Derivatives Confluence Override — {flow.line()}; RVOL gate "
                f"{strict:g}x → {min(strict, relaxed):g}x")
    return strict, ""


def conflict_veto(signal: Any, flow: FlowRead) -> str:
    """The strict veto when extreme flow opposes the trade, else ""."""
    if not flow.extreme or flow.bias != -direction(signal):
        return ""
    buying = "calls" if direction(signal) > 0 else "puts"
    return (f"Derivatives Flow Conflict Veto — {flow.line()}; no {buying} "
            f"into heavy institutional {flow.side} positioning")


def derivative_weight_multiplier(cfg: Any, flow: FlowRead) -> float:
    """How much more the Derivatives vote counts while extreme flow is present."""
    if not flow.extreme:
        return 1.0
    return max(1.0, float(cfg.get("agents.derivatives.extreme_flow_weight_multiplier", 2.0)))


# --------------------------------------------------------------------------- #
# Volume profile confluence — applied to EVERY signal by the Technical agent
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ProfileConfluence:
    """What the session volume profile says about one entry."""
    boost: float = 0.0          # added to the Technical score (may be negative)
    veto: str = ""              # a hard stop: buying straight into an HVN wall
    notes: tuple[str, ...] = ()
    profiles: dict[str, Any] | None = None


def profile_confluence(signal: Any, setup: Any, candles: list[Any],
                       cfg: Any) -> ProfileConfluence:
    """+alignment_boost (0.30) for an entry at a profile level in its favour —
    a call at the VAL or POC, a put at the VAH or POC — and a penalty or veto
    for one trading straight into a thick High Volume Node."""
    from panaoptions.indicators.volume_profile import session_profiles
    from panaoptions.strategies import volume_profile_strategies as vp

    if not candles:
        return ProfileConfluence()
    g = cfg.get
    profiles = session_profiles(
        candles, str(g("session.timezone", "America/New_York")),
        str(g("volume_profile.rth_open", "09:30")),
        str(g("volume_profile.rth_close", "16:00")), exclude_last=True,
        bins=int(g("volume_profile.bins", 40)),
        value_area_pct=float(g("volume_profile.value_area_pct", 0.70)),
        lvn_ratio=float(g("volume_profile.lvn_ratio", 0.30)),
        hvn_ratio=float(g("volume_profile.hvn_ratio", 1.5)))
    if not profiles:
        return ProfileConfluence()
    price = float(setup.indicators.close or signal.trigger_price)
    atr = float(setup.indicators.atr or 0.0)
    want = direction(signal)
    boost, notes, veto = 0.0, [], ""

    aligned = vp.level_alignment(price, want, profiles, atr,
                                 float(g("volume_profile.alignment_tolerance_atr", 0.25)))
    if aligned:
        gain = float(g("volume_profile.alignment_boost", 0.30))
        boost += gain
        notes.append(f"volume profile: {'call' if want > 0 else 'put'} at the {aligned} "
                     f"(+{gain:.2f})")

    target = float(setup.underlying_target) if getattr(setup, "underlying_target", 0) else None
    wall, zone = vp.hvn_wall(price, want, profiles, atr, target,
                             float(g("volume_profile.hvn_near_atr", 1.0)),
                             float(g("volume_profile.hvn_veto_atr", 0.25)))
    if wall and zone is not None:
        where = f"HVN wall {zone.low:.2f}-{zone.high:.2f}"
        if wall == "veto":
            veto = (f"buying {'calls' if want > 0 else 'puts'} straight into a thick "
                    f"{where}, {abs((zone.low if want > 0 else zone.high) - price):.2f} "
                    f"away — accepted inventory stalls the move")
        else:
            cost = float(g("volume_profile.hvn_penalty", 0.30))
            boost -= cost
            notes.append(f"volume profile: {where} within "
                         f"{g('volume_profile.hvn_near_atr', 1.0)} ATR ahead (-{cost:.2f})")
    return ProfileConfluence(round(boost, 3), veto, tuple(notes),
                             {k: p.to_dict() for k, p in profiles.items()})
