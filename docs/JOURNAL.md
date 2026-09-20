# Trade journal & the Mistake Card engine

The journal exists to separate **process from outcome**. A winning trade that
broke your rules is a bad trade. A losing trade that honoured its stop is an
acceptable one. Until you grade yourself that way, a run of luck is
indistinguishable from skill.

---

## The four verdicts

| Verdict | Meaning |
|---|---|
| ✅ **GOOD WIN** | Followed the plan, made money. Repeat it. |
| 🟦 **GOOD LOSS** | Followed the plan, the edge failed. This is the cost of business — nothing to fix. |
| ⚠️ **BAD WIN** | Broke a rule and got paid anyway. **The most dangerous outcome**, because it teaches you to break the rule again. |
| ❌ **BAD LOSS** | Broke a rule and paid for it. |

Execution is scored **1–10 on discipline only**. Profit never raises the score
and a loss never lowers it.

---

## Logging a trade

Fill the form in the dashboard's **Trade Journal & Post-Mortem** panel. The
setup tag is mandatory: Mean Reversion, Breakout, Sector Laggard, News Momentum,
or Other. An untagged trade teaches you nothing later.

You can tick mistakes you already know you made. **The engine detects the rest
from the numbers**, whether or not you own up to them:

| Detected automatically | How |
|---|---|
| **Moved Stop Loss Further** | You recorded a stop worse than planned, or the loss exceeded planned risk by >15% |
| **Chased Price (FOMO)** | Your entry was worse than planned by more than 25% of the stop distance |
| **Sized Too Large** | Risk exceeded `max_risk_per_trade_pct` of capital |
| **Exited Before Target** | Closed a winner at under half the planned R — unless the time stop fired |
| **Held Past Time Stop** | A mean-reversion trade held beyond `time_stop_minutes` and still losing |
| **No Volume Confirmation** | Relative volume below 1.0x on the entry bar |

Self-reported only: Revenge Traded, Ignored Broader Market Trend.

---

## The two numbers that matter

**R-multiple realisation** = realised P&L ÷ initial risk. A trade sized to risk
₹1,000 that made ₹2,000 is +2R. This makes trades comparable across position
sizes and instruments.

**Mistake Cost Index** = the money lost *specifically on trades that broke a
rule*. Losses from clean setups are excluded, because those are the price of
having an edge at all. The index isolates what indiscipline alone costs you.

Alongside it, **avoidable losses %** tells you what share of everything you've
lost was self-inflicted. If that number is high, your problem is behaviour, not
strategy.

---

## Where it is saved

Two copies, on purpose:

- **SQLite** (`data/runtime/panadhanam.db`) — queryable, drives the analytics.
- **Markdown** (`journal/`) — committed to git.
  - `journal/cards/YYYY-MM-DD-TRADE-ID.md` — one post-mortem per trade
  - `journal/README.md` — the scorecard and full trade log, rendered on GitHub

The markdown is the durable artefact. If the database is lost, your learning
history survives in plain text.

**Commit it:**

```bash
git add journal/
git commit -m "journal: trades through $(date +%F)"
git push
```

Click **Export to git** in the panel first to regenerate `journal/README.md`.

---

## The coach

With `ANTHROPIC_API_KEY` set, each card is written by Claude acting as an
uncompromising risk officer in the style of Takashi Kotegawa (BNF). The prompt
lives in `config/agents.yaml` under `post_mortem` — edit it to change how hard
your coach is on you.

**Without a key you still get a full card** from the deterministic engine. Most
violations are arithmetic, not judgement: a widened stop, an oversized position
and a chased entry are all provable from the recorded numbers. The engine's
root-cause explanations and corrective protocols are written per mistake type.

The deterministic checks are **authoritative** — the LLM is instructed never to
contradict them, only to add what they cannot see.

---

## Risk architecture this supports

Configured in `config/settings.yaml`:

```yaml
risk:
  risk_per_trade_pct: 1.0        # 0.5 is safer
  structural_stop_ticks: 2       # stop sits BEYOND the level, not on it
  prefer_structural_stop: true
  time_stop_minutes: 30
  time_stop_setups: ["Mean Reversion"]
```

**Structural stops.** The stop is placed 1–2 ticks *beyond* the support level or
panic wick where buyers actually stepped in — not at a round percentage. A stop
resting exactly on a round number is queued with everyone else's, which is
precisely where a liquidity sweep goes looking.

**Time stops.** A mean-reversion entry is a bet that buyers are absorbing
supply. If it hasn't bounced within the window, the bet is simply wrong — a dead
bounce means supply is still in control. The tracker exits at market rather than
waiting to pay the full price stop. A trade that *is* working is left alone; the
timer only kills dead ones.
