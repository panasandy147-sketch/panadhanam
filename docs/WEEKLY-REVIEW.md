# The Weekend Review

Run it after Friday's close. It answers three questions the live dashboard cannot.

```
Dashboard → Weekly Review → Build this week's review → Download .md
```

Or from the command line / a browser:

```
GET  /api/journal/weekly?week=2026-09-21          the data
GET  /api/journal/weekly/download?format=md       the file
POST /api/journal/weekly/save                     write it into journal/weekly/
```

`week` is **any day inside the week you want** — the report snaps to that
Monday-to-Friday. Leave it blank for the current week. Asking on a Saturday or
Sunday gives you the week that just finished, which is the one you want.

---

## 1. What did I actually do?

Every trade the desk took, with its plan, its outcome and its R-multiple —
plus the scorecard:

| Metric | What it tells you |
|---|---|
| Total R | The week in risk units, not currency. The only number comparable across position sizes. |
| Clean execution | The share of trades that broke no rule. **This is the number that predicts next month.** |
| Discipline score | 1–10 per trade, on process only. A winner that broke a rule scores low. |
| Mistake Cost Index | Money lost **only** on rule-breaking trades. Clean losses are excluded deliberately — this isolates what indiscipline alone cost. |
| Avoidable share of losses | What fraction of the damage you did to yourself. |

## 2. Why did the desk take it?

For each trade: **every analyst's vote, score, confidence and reasoning at the
moment of entry**, what the CMIO weighed, and the counter-argument recorded
*before* the outcome was known.

This is recovered from the `agent_reports` table, which is written at decision
time. It is what the desk actually thought, not a story reconstructed
afterwards from a result you now know.

Abstentions are shown explicitly. An analyst with no data is not a quiet vote
of agreement, and the report never lets it read as one.

There is also an **analyst scorecard**: for each agent, the trades it actually
confirmed and how they went. That is how you find out whether the news desk is
earning its weight or just adding noise.

## 3. What should change?

A coach's read of the week: what worked, what indiscipline cost, and specific
rule changes with the evidence behind them.

With a local model configured (see [OLLAMA.md](OLLAMA.md)) this is the model's
work. Without one you get the deterministic version — the same numbers, read
out honestly. Either way it states plainly when the sample is too small to
conclude anything. **Four trades is not a trend.**

> **The coach advises; it never configures.** Nothing it proposes is applied.
> The risk desk stays deterministic — no model can change a stop, a position
> size, or the confirmation requirement. You read the suggestion and you decide.

---

## Mid-week is fine

The review builds any day of the week. It just says so:

> **This week is not finished.** These numbers are provisional and will change
> before Friday's close.

A progress check is useful. A half-week filed as the week's verdict is not.

## It writes itself on Friday

Once the week's last session closes, the engine writes the review to
`journal/weekly/` automatically — both formats, once per week:

```
journal/weekly/2026-09-21_to_2026-09-25.md      to read
journal/weekly/2026-09-21_to_2026-09-25.json    to diff against other weeks
```

`journal/` is committed to git on purpose. Your learning history should outlive
the database, the machine and this app.

```bash
git add journal/weekly && git commit -m "Weekly review" && git push
```

## Building it is the slow part

Asking a local model to read a week takes a minute or two on CPU. Untick
**"Ask the model for a strategy read"** (or pass `coach=false`) to get sections
1 and 2 instantly — those are pure database reads and never touch an LLM.

## An empty week

If the review says no trades were logged, check in this order:

1. Was the desk **armed**? Press *Start trading day* each morning — arming
   expires at square-off.
2. Is `AUTO_PLACE_ORDERS=true` in `.env`? Armed but alert-only places nothing.
3. Is capital large enough to size a position? At very small capital the board
   shows WATCH with *"cannot fit even one share"*.

A genuinely quiet week is possible. All three of those are more likely.
