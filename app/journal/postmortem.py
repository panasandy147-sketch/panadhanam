"""The Post-Mortem Mistake Card Engine.

Evaluates a completed trade on PROCESS, not profit, and produces a standard
Mistake Card. Uses Claude when a key is present; otherwise a deterministic
rule engine produces the same card shape, so the journal never goes dark.

The rule engine is not a toy fallback: most violations (moved stop, oversized,
chased entry, held past the time stop) are detectable from the recorded
numbers alone, without any judgement call.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.agents.base import get_llm_client
from app.core.config import Config, get_config
from app.core.logging import get_logger
from app.journal.models import JournalEntry, MistakeCard, MistakeTag, TradeVerdict

log = get_logger("journal.postmortem")


class _CardSchema(BaseModel):
    """What Claude must return. Narrow, so nothing free-form slips through."""
    execution_score: int = Field(description="1-10 based STRICTLY on rule discipline, not PnL")
    core_violation: str = Field(description="The exact error, or 'None' if the trade was clean")
    entry_analysis: str = Field(description="Was price sufficiently stretched? Volume confirmed?")
    exit_discipline: str = Field(description="Was the stop honoured mechanically? Target taken?")
    root_cause: str = Field(description="The behavioural bias behind the error")
    corrective_protocol: str = Field(description="ONE specific, measurable checklist rule")
    detected_mistakes: list[str] = Field(
        default_factory=list,
        description="Mistake tags you can justify from the data")


class PostMortemEngine:
    def __init__(self, cfg: Config | None = None) -> None:
        self.cfg = cfg or get_config()

    @property
    def system_prompt(self) -> str:
        """Editable in config/agents.yaml under `post_mortem.role`."""
        spec = self.cfg.agent("post_mortem")
        if spec.get("role"):
            parts = [spec["role"]]
            if spec.get("rules"):
                parts.append("\nHard rules:")
                parts += [f"- {r}" for r in spec["rules"]]
            return "\n".join(parts)
        return _DEFAULT_PROMPT

    # ------------------------------------------------------------------ #
    async def build(self, entry: JournalEntry) -> MistakeCard:
        entry.compute()
        detected = self.detect_mistakes(entry)
        # Anything the maths proves is added to whatever the trader self-reported.
        for tag in detected:
            if tag not in entry.mistakes:
                entry.mistakes.append(tag)
        entry.compute()          # re-derive the verdict with the full mistake list

        if self.cfg.llm_enabled:
            try:
                card = await self._llm_card(entry)
                if card:
                    return card
            except Exception as exc:
                log.warning("LLM post-mortem failed (%s) — using the rule engine", exc)
        return self.rule_card(entry)

    # ------------------------------------------------------------------ #
    # Deterministic detection: things the numbers prove, not opinions
    # ------------------------------------------------------------------ #
    def detect_mistakes(self, entry: JournalEntry) -> list[MistakeTag]:
        found: list[MistakeTag] = []
        ctx = entry.context or {}

        # --- stop was widened ---
        actual_stop = ctx.get("actual_stop")
        if actual_stop is not None and entry.planned_stop:
            long = entry.side.upper() == "BUY"
            widened = (actual_stop < entry.planned_stop if long
                       else actual_stop > entry.planned_stop)
            if widened:
                found.append(MistakeTag.MOVED_STOP)
                entry.stop_honoured = False

        # --- the loss exceeded the planned risk: the stop was not honoured ---
        if entry.initial_risk > 0 and entry.pnl < 0:
            if abs(entry.pnl) > entry.initial_risk * 1.15:
                entry.stop_honoured = False
                if MistakeTag.MOVED_STOP not in found:
                    found.append(MistakeTag.MOVED_STOP)

        # --- oversized against the configured risk limit ---
        capital = float(ctx.get("capital") or self.cfg.get("risk.total_capital", 0) or 0)
        max_pct = float(self.cfg.get("risk.max_risk_per_trade_pct", 2.0))
        if capital and entry.initial_risk:
            risked_pct = entry.initial_risk / capital * 100
            if risked_pct > max_pct * 1.05:
                found.append(MistakeTag.SIZED_TOO_LARGE)

        # --- chased the entry ---
        if entry.planned_entry and entry.actual_entry is not None:
            long = entry.side.upper() == "BUY"
            adverse = ((entry.actual_entry - entry.planned_entry) if long
                       else (entry.planned_entry - entry.actual_entry))
            stop_points = abs(entry.planned_entry - entry.planned_stop) or 1.0
            # Paying more than a quarter of the stop distance to get in is a chase,
            # not slippage: it hands away 25% of the trade's edge at the door.
            if adverse > stop_points * 0.25:
                found.append(MistakeTag.CHASED_PRICE)

        # --- exited well before the target for no stated reason ---
        if (entry.actual_exit is not None and entry.pnl > 0
                and entry.planned_target and entry.initial_risk):
            target_r = abs(entry.planned_target - entry.planned_entry) / \
                max(abs(entry.planned_entry - entry.planned_stop), 1e-9)
            if entry.r_multiple < target_r * 0.5 and not ctx.get("time_stop_hit"):
                found.append(MistakeTag.EXITED_EARLY)

        # --- held past the time stop ---
        time_stop = float(self.cfg.get("risk.time_stop_minutes", 0) or 0)
        if (time_stop and entry.hold_minutes > time_stop
                and entry.setup.value == "Mean Reversion" and entry.pnl <= 0):
            found.append(MistakeTag.HELD_PAST_TIME_STOP)

        # --- traded against the market with no volume confirmation ---
        if ctx.get("volume_surge") is not None:
            try:
                if float(ctx["volume_surge"]) < 1.0:
                    found.append(MistakeTag.NO_VOLUME_CONFIRM)
            except (TypeError, ValueError):
                pass

        return found

    # ------------------------------------------------------------------ #
    def rule_card(self, entry: JournalEntry) -> MistakeCard:
        """A full card with no LLM. Blunt, specific, and always available."""
        score = 10
        score -= 3 * sum(1 for m in entry.mistakes
                         if m in {MistakeTag.MOVED_STOP, MistakeTag.SIZED_TOO_LARGE})
        score -= 2 * sum(1 for m in entry.mistakes
                         if m in {MistakeTag.CHASED_PRICE, MistakeTag.REVENGE_TRADED})
        score -= 1 * sum(1 for m in entry.mistakes
                         if m in {MistakeTag.EXITED_EARLY, MistakeTag.IGNORED_MARKET,
                                  MistakeTag.NO_VOLUME_CONFIRM,
                                  MistakeTag.HELD_PAST_TIME_STOP})
        score = max(1, min(10, score))

        violation = entry.mistakes[0].value if entry.mistakes else "None — clean execution"

        ctx = entry.context or {}
        stretch = ctx.get("distance_from_ma_pct")
        vol = ctx.get("volume_surge")
        entry_bits = []
        if stretch is not None:
            entry_bits.append(f"{stretch:+.2f}% from the moving average at entry")
        if vol is not None:
            entry_bits.append(f"volume {vol:.1f}x average "
                              f"({'confirmed' if vol >= 1.5 else 'NOT confirmed'})")
        if entry.slippage:
            entry_bits.append(f"slippage {entry.slippage:+.2f} vs the planned entry")
        entry_analysis = "; ".join(entry_bits) or "No entry context was recorded."

        if entry.stop_honoured:
            exit_bits = [f"Stop honoured. Held {entry.hold_minutes:.0f} min"]
        else:
            exit_bits = [f"STOP NOT HONOURED — loss of {abs(entry.pnl):,.0f} "
                         f"against a planned risk of {entry.initial_risk:,.0f}"]
        exit_bits.append(f"realised {entry.r_multiple:+.2f}R")
        exit_discipline = ". ".join(exit_bits) + "."

        root_cause = _ROOT_CAUSE.get(
            entry.mistakes[0] if entry.mistakes else None,
            "No behavioural error detected. The plan was followed; the outcome "
            "is the market's business, not a verdict on the process.")

        corrective = _CORRECTIVE.get(
            entry.mistakes[0] if entry.mistakes else None,
            "Keep doing exactly this. Log the next ten trades the same way and "
            "check the hit rate, not the last result.")

        return MistakeCard(
            trade_id=entry.id,
            ticker_setup=f"{entry.symbol} | {entry.setup.value}",
            execution_score=score,
            core_violation=violation,
            entry_analysis=entry_analysis,
            exit_discipline=exit_discipline,
            root_cause=root_cause,
            corrective_protocol=corrective,
            verdict=entry.verdict or TradeVerdict.GOOD_LOSS,
            r_multiple=entry.r_multiple,
            generated_by="rules",
        )

    # ------------------------------------------------------------------ #
    async def _llm_card(self, entry: JournalEntry) -> MistakeCard | None:
        client = await get_llm_client(self.cfg)
        if client is None:
            return None

        payload = {
            "symbol": entry.symbol,
            "instrument": entry.instrument,
            "setup": entry.setup.value,
            "side": entry.side,
            "planned": {"entry": entry.planned_entry, "stop": entry.planned_stop,
                        "target": entry.planned_target,
                        "quantity": entry.planned_quantity},
            "actual": {"entry": entry.actual_entry, "exit": entry.actual_exit,
                       "quantity": entry.actual_quantity},
            "slippage": entry.slippage,
            "hold_minutes": entry.hold_minutes,
            "pnl": entry.pnl,
            "initial_risk": entry.initial_risk,
            "r_multiple": entry.r_multiple,
            "stop_honoured": entry.stop_honoured,
            "mistakes_detected_by_rules": [m.value for m in entry.mistakes],
            "market_context": entry.context,
            "trader_notes": entry.notes,
        }

        import json
        prompt = (
            f"TRADE TO EVALUATE:\n{json.dumps(payload, default=str, indent=2)}\n\n"
            f"My deterministic checks already flagged: "
            f"{[m.value for m in entry.mistakes] or 'nothing'}.\n"
            f"Those are arithmetic facts — do not contradict them. Add anything "
            f"they cannot see, and judge the quality of the decision itself.\n\n"
            f"Score execution on DISCIPLINE ONLY. A profitable trade that broke "
            f"a rule scores low. A losing trade that honoured its stop scores high."
        )

        response = await client.messages.parse(
            model=self.cfg.llm_model,
            max_tokens=2000,
            system=self.system_prompt,
            thinking={"type": "adaptive"},
            output_config={"effort": self.cfg.llm_effort},
            messages=[{"role": "user", "content": prompt}],
            output_format=_CardSchema,
        )
        d = response.parsed_output
        if d is None:
            return None

        return MistakeCard(
            trade_id=entry.id,
            ticker_setup=f"{entry.symbol} | {entry.setup.value}",
            execution_score=max(1, min(10, int(d.execution_score))),
            core_violation=d.core_violation,
            entry_analysis=d.entry_analysis,
            exit_discipline=d.exit_discipline,
            root_cause=d.root_cause,
            corrective_protocol=d.corrective_protocol,
            verdict=entry.verdict or TradeVerdict.GOOD_LOSS,
            r_multiple=entry.r_multiple,
            generated_by="claude",
        )


_ROOT_CAUSE: dict[Any, str] = {
    MistakeTag.MOVED_STOP:
        "Sunk cost fallacy. Widening a stop converts a defined, survivable loss "
        "into an open-ended one, purely to avoid admitting the thesis was wrong. "
        "This is the single fastest way to end an account.",
    MistakeTag.CHASED_PRICE:
        "FOMO. Paying up after the move has begun destroys the risk:reward the "
        "setup was built on — the stop distance stays the same while the reward "
        "shrinks.",
    MistakeTag.EXITED_EARLY:
        "Disposition effect: the urge to realise a gain early while letting "
        "losses run. It caps your winners at less than your losers, which no "
        "hit rate can survive.",
    MistakeTag.SIZED_TOO_LARGE:
        "Overconfidence, usually after a winning streak. Size is the one "
        "variable that can end you in a single trade, however good the setup.",
    MistakeTag.REVENGE_TRADED:
        "Loss aversion driving action. The market has no memory of your last "
        "trade; taking a worse setup to 'win it back' compounds the damage.",
    MistakeTag.IGNORED_MARKET:
        "Confirmation bias — reading the chart you wanted while the index said "
        "otherwise. Most single-stock moves are the market's move.",
    MistakeTag.NO_VOLUME_CONFIRM:
        "Pattern-matching without evidence. A level without volume behind it is "
        "a drawing, not a decision by real buyers.",
    MistakeTag.HELD_PAST_TIME_STOP:
        "Hope substituting for a thesis. A mean-reversion trade that has not "
        "bounced is telling you supply is still in control.",
}

_CORRECTIVE: dict[Any, str] = {
    MistakeTag.MOVED_STOP:
        "Place the stop as a resting order with the broker at entry. Once it is "
        "live, you may only move it in the profitable direction. No exceptions.",
    MistakeTag.CHASED_PRICE:
        "Enter with a limit order at your planned price. If it is not filled "
        "within 2 minutes, the trade is cancelled. You do not get to pay up.",
    MistakeTag.EXITED_EARLY:
        "Scale out mechanically: half at 1R with the stop to breakeven, the "
        "remainder runs to target. Manual early exits require a written reason "
        "in the journal before the order is sent.",
    MistakeTag.SIZED_TOO_LARGE:
        "Size is computed by the risk calculator before entry and is not "
        "adjustable afterwards. If the calculator says zero, the trade does not "
        "happen at this account size.",
    MistakeTag.REVENGE_TRADED:
        "After any loss, no new position for 30 minutes. After two consecutive "
        "losses, the desk is closed for the day.",
    MistakeTag.IGNORED_MARKET:
        "Add a pre-entry check: the index must not be moving against your "
        "direction by more than 0.3% on the day. Log the index level with every "
        "entry.",
    MistakeTag.NO_VOLUME_CONFIRM:
        "No entry unless relative volume is at least 1.5x the 20-period average "
        "on the entry bar. Record the figure in the journal at entry.",
    MistakeTag.HELD_PAST_TIME_STOP:
        "Set a hard timer at entry. If a mean-reversion trade has not moved in "
        "your favour within the configured time stop, exit at market.",
}


_DEFAULT_PROMPT = """You are an uncompromising, professional trading risk officer and \
performance coach specializing in mean-reversion and intraday price-action strategies \
(in the style of Takashi Kotegawa / BNF).

Your goal is to evaluate the user's completed trade, determine whether it was a \
"good loss" (flawless execution of an edge that failed) or a "bad trade" (rule \
violation), and output a structured Mistake Card.

Guidelines:
1. Distinguish between Process and Outcome: a winning trade that broke the rules is a \
POOR trade. A losing trade that followed the stop-loss plan is an ACCEPTABLE loss.
2. Identify root causes: emotional leaks (chasing, premature exits, expanding stops, \
lack of volume or moving-average confirmation).
3. Be candid, direct and actionable. Do not soften a verdict because the trade made \
money, and do not manufacture a fault where the process was sound.
4. The corrective protocol must be ONE specific, measurable rule that could be checked \
off before the next entry — not general advice."""
