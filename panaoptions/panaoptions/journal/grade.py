"""Grade a closed trade. Deterministic first, LLM only for the prose.

Every mistake tag is derived from the ledger — the fills, the levels, the
clock — so the grade does not depend on anyone admitting to anything. The
optional model writes the card's wording; it never decides the verdict or the
score, because a model that has read a profitable trade tends to find reasons
it was fine.
"""
from __future__ import annotations

from datetime import time

from panaoptions.journal.models import Card, JournalEntry, Mistake, Verdict
from panaoptions.logging import get_logger
from panaoptions.models import ExitReason, PaperTrade, SetupType

log = get_logger("journal.grade")

# What each mistake costs the discipline score, out of 10.
_PENALTY = {
    Mistake.STOP_NOT_HONOURED: 5,
    Mistake.IGNORED_INVALIDATION: 4,
    Mistake.TRADED_WHILE_HALTED: 5,
    Mistake.OVERSIZED: 3,
    Mistake.OUTSIDE_WINDOW: 2,
    Mistake.NO_STRATEGY_TAG: 2,
    Mistake.CHASED: 2,
    Mistake.EXITED_EARLY: 2,
    Mistake.HELD_TO_THE_BELL: 1,
}


def detect(trade: PaperTrade, cfg) -> list[Mistake]:
    """Every rule this trade broke, read off the ledger."""
    found: list[Mistake] = []

    if trade.strategy is SetupType.OTHER:
        found.append(Mistake.NO_STRATEGY_TAG)

    # The desk always honours its own stop, so this only fires if something
    # moved it the wrong way — worth catching loudly if it ever does.
    #
    # The tolerance is the SLIPPAGE, not a percentage. A flat 5% would flag
    # every honest stop fill on a cheap contract: $0.02 of slippage is 4.5% of
    # a $0.44 stop but only 0.4% of a $5.00 one, so a percentage tolerance
    # means the check is strict or lax depending on the contract's price.
    if trade.exit_reason is ExitReason.STOP and trade.stop_price > 0:
        worst = min((f.price for f in trade.fills if f.quantity < 0),
                    default=trade.stop_price)
        slippage = float(cfg.get("risk.slippage_per_contract", 0.02))
        if worst < trade.stop_price - slippage * 1.5:
            found.append(Mistake.STOP_NOT_HONOURED)

    if trade.exit_reason is ExitReason.DAY_END:
        # Squared off by the clock rather than by a rule. Not a sin, but the
        # trade never resolved on its own terms.
        found.append(Mistake.HELD_TO_THE_BELL)

    deployed = trade.entry_price * trade.quantity * cfg.multiplier
    limit = cfg.capital * float(cfg.get("risk.max_capital_deployed_pct", 20)) / 100
    if deployed > limit * 1.01:
        found.append(Mistake.OVERSIZED)

    window = _entry_window(trade, cfg)
    if window and not _inside(trade, window):
        found.append(Mistake.OUTSIDE_WINDOW)

    # A winner cut before either exit rule fired is a decision, not a plan.
    if (trade.realised_pnl > 0 and trade.exit_reason
            in {ExitReason.TIME_EXIT, ExitReason.CIRCUIT_BREAKER}):
        found.append(Mistake.EXITED_EARLY)

    return found


def _entry_window(trade: PaperTrade, cfg) -> tuple[time, time] | None:
    for key, name in (("orb_vwap", SetupType.ORB_VWAP),
                      ("vwap_ema_pullback", SetupType.VWAP_EMA_PULLBACK),
                      ("liquidity_sweep", SetupType.LIQUIDITY_SWEEP)):
        if trade.strategy is not name:
            continue
        opens = cfg.get(f"strategies.{key}.from")
        closes = cfg.get(f"strategies.{key}.to")
        if not (opens and closes):
            return None
        return _parse(str(opens)), _parse(str(closes))
    return None


def _parse(value: str) -> time:
    hour, _, minute = value.partition(":")
    return time(int(hour), int(minute or 0))


def _inside(trade: PaperTrade, window: tuple[time, time]) -> bool:
    from zoneinfo import ZoneInfo

    stamp = trade.opened_at
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone(ZoneInfo("America/New_York"))
    return window[0] <= stamp.time() < window[1]


# --------------------------------------------------------------------------- #
def score(mistakes: list[Mistake]) -> int:
    """1-10 on discipline alone. P&L does not appear in this calculation."""
    return max(1, 10 - sum(_PENALTY.get(m, 1) for m in mistakes))


def verdict(pnl: float, mistakes: list[Mistake]) -> Verdict:
    clean = not mistakes
    if pnl >= 0:
        return Verdict.GOOD_WIN if clean else Verdict.BAD_WIN
    return Verdict.GOOD_LOSS if clean else Verdict.BAD_LOSS


def build_entry(trade: PaperTrade, cfg) -> JournalEntry:
    mistakes = detect(trade, cfg)
    cost = trade.entry_price * trade.quantity * cfg.multiplier
    exits = [f for f in trade.fills if f.quantity < 0]
    hold = ((trade.closed_at - trade.opened_at).total_seconds() / 60
            if trade.closed_at else 0.0)

    return JournalEntry(
        id=f"J-{trade.id}",
        trade_id=trade.id,
        ts=trade.closed_at or trade.opened_at,
        symbol=trade.symbol,
        contract=trade.contract_label,
        strategy=trade.strategy,
        pattern=trade.pattern,
        claimed_accuracy=trade.claimed_accuracy,
        direction=trade.direction.value,
        entry_price=trade.entry_price,
        exit_price=exits[-1].price if exits else 0.0,
        stop_price=trade.stop_price,
        quantity=trade.quantity,
        pnl=trade.realised_pnl,
        return_pct=round(trade.realised_pnl / cost * 100, 2) if cost else 0.0,
        hold_minutes=round(hold, 1),
        exit_reason=trade.exit_reason.value if trade.exit_reason else "",
        underlying_support=trade.underlying_support,
        invalidation_note=trade.invalidation_note,
        mistakes=mistakes,
        verdict=verdict(trade.realised_pnl, mistakes),
        execution_score=score(mistakes),
    )


# --------------------------------------------------------------------------- #
_SYSTEM = (
    "You are a trading coach reviewing ONE options trade on a small paper "
    "account. You grade PROCESS, not outcome: a profitable trade that broke a "
    "rule is a bad trade, and a losing trade that honoured its invalidation is "
    "an acceptable one. The verdict and the discipline score are already "
    "decided and are not yours to change — write the explanation, in plain "
    "language, citing the numbers you were given. Never suggest a wider stop, "
    "a bigger position, or removing a confirmation."
)


async def build_card(entry: JournalEntry, cfg) -> Card:
    """The written post-mortem. Falls back to a rules-written card."""
    card = _rule_card(entry, cfg)
    if not bool(cfg.get("journal.use_llm", False)):
        return card

    try:
        from pydantic import BaseModel, Field

        from panaoptions.ml.llm import structured_complete

        class _Prose(BaseModel):
            what_happened: str = Field(description="Two sentences on the trade")
            core_violation: str = Field(description="The error, or 'None'")
            what_to_repeat: str = Field(description="What was done well")

        prose = await structured_complete(
            system=_SYSTEM, prompt=_prompt(entry), schema=_Prose, cfg=cfg)
        if prose is None:
            return card
        card.what_happened = prose.what_happened
        card.core_violation = prose.core_violation
        card.what_to_repeat = prose.what_to_repeat
        card.generated_by = str(cfg.get("journal.llm_label", "a local model"))
    except Exception as exc:                     # noqa: BLE001 - never fatal
        log.debug("card prose unavailable, keeping the rules version: %s", exc)
    return card


def _prompt(entry: JournalEntry) -> str:
    return "\n".join([
        f"{entry.symbol} {entry.direction} — {entry.contract}",
        f"Strategy: {entry.strategy.value}",
        f"Entry {entry.entry_price:.2f} x{entry.quantity}, "
        f"exit {entry.exit_price:.2f} ({entry.exit_reason})",
        f"Held {entry.hold_minutes:.0f} minutes. "
        f"P&L {entry.pnl:+,.2f} ({entry.return_pct:+.1f}%)",
        f"Invalidation was: {entry.invalidation_note or 'not recorded'}",
        f"Verdict: {entry.verdict.value if entry.verdict else '—'} "
        f"(discipline {entry.execution_score}/10)",
        f"Rules broken: {', '.join(m.value for m in entry.mistakes) or 'none'}",
    ])


def _rule_card(entry: JournalEntry, cfg) -> Card:
    currency = str(cfg.get("account.currency", "$"))
    won = entry.pnl >= 0
    happened = (
        f"{entry.strategy.value} on {entry.symbol}. Entered at "
        f"{entry.entry_price:.2f}, exited at {entry.exit_price:.2f} on "
        f"{entry.exit_reason or 'no recorded reason'} after "
        f"{entry.hold_minutes:.0f} minutes, for {currency}{entry.pnl:+,.2f} "
        f"({entry.return_pct:+.1f}%).")

    if entry.mistakes:
        violation = "; ".join(m.value for m in entry.mistakes)
        repeat = ("Nothing to repeat here — fix the rule break first."
                  if not won else
                  "The read was right. The execution was not, and the money "
                  "is hiding that.")
    else:
        violation = "None."
        repeat = (f"Clean execution: the entry matched {entry.strategy.value} "
                  f"and the exit came from a rule"
                  + (f" ({entry.invalidation_note})" if entry.invalidation_note else "")
                  + ". Repeat this regardless of what it paid.")

    return Card(
        trade_id=entry.trade_id, ts=entry.ts,
        headline=f"{entry.symbol} {entry.direction} — {entry.strategy.value}",
        strategy=entry.strategy,
        verdict=entry.verdict or Verdict.GOOD_LOSS,
        execution_score=entry.execution_score,
        pnl=entry.pnl,
        what_happened=happened, core_violation=violation, what_to_repeat=repeat,
    )
