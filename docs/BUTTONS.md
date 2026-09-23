# Every button, and when to click it

Left to right, top to bottom.

---

## Header

### 🇮🇳 India / 🇺🇸 US — market switch
Switches the entire desk: session hours, timezone, currency, watchlist, news
feeds, macro dashboard, strike ladders, expiry day and available brokers.

**Click when:** you want to work the other market.
**Refused when:** you have open positions — the new market's broker cannot
manage them. Close or square off first.

### `US 12:40 · WEEKEND` — market clock
Not a button. Shows the time **in the market's own timezone** and the session
phase. On a PC in India watching US markets, this is the number that matters,
not your system clock.

Phases: `closed` → `premarket` → `open` → `postmarket` → `weekend`.

### `● LIVE` — websocket status
Green and pulsing means the dashboard is connected. If it says `reconnecting`,
the server stopped.

### `PAPER / ALERT-ONLY` — the safety indicator
**Check this before you do anything.** Two possible states:

- **PAPER / ALERT-ONLY** — nothing reaches a broker. Signals are alerts only.
- **LIVE ORDERS** (red) — real orders will be placed. Real money.

### `WEEKEND` / `BROKER: PAPER` / `RULE-BASED`
Status only. Respectively: session phase, which broker is connected, and
whether agents are using Claude or their deterministic rule engines.

---

## Action buttons

### `Run cycle`
Runs **one** full analysis pass over the whole watchlist right now: fetches
data, runs all five analysts, the CMIO synthesises, the risk desk sizes.

**Click when:** you want an immediate read instead of waiting for the next
automatic cycle. Useful outside market hours to see the machinery work.
**Takes:** a few seconds on rules, longer with Claude enabled.

### `Pre-market scan`
Runs the fundamental quality filter across the stock universe and pulls the
global macro read. Produces the eligible/screened-out list for the day.

**Click when:** before the open — around 08:45. It runs automatically at the
configured time; this is the manual trigger.

### `Pause` / `Resume`
Stops the automatic cycle loop. Open positions are still tracked and graded;
only **new** analysis stops.

**Click when:** you want the screen to stop changing while you read something,
or you're mid-configuration.

### `◐` — light/dark theme
Cosmetic. Your choice is remembered.

---

## Risk & Capital panel

### `edit` on the Capital tile
Changes the account size every position is sized against.

**Click when:** your real account size changes, or you're testing at a
different size.
**Refused when:** positions are open — they were sized against the old capital,
so changing it underneath them would misstate your actual risk.

Changing capital immediately rescales the per-trade risk budget, the daily loss
limit and the exposure ceiling. **It does not move any money.** In paper mode
nothing is traded either way.

### The other tiles
Read-only. `Room before halt` is the one to watch — when it hits zero the desk
stops taking new trades for the day.

---

## Trade Opportunities

### `Scan watchlist`
Analyses every symbol and sorts them into low / medium / high risk buckets with
full trade details.

**Click when:** you want to know what's set up right now. Before the open, and
whenever you're deciding what to look at.

Each card is marked:
- **TRADEABLE** — cleared every gate. This is a real candidate.
- **WATCH** — the ⚠ line says exactly what's blocking it.

---

## Price Action

### Symbol dropdown, and `1m` `5m` `15m` `1D`
Changes what the chart shows. Purely a view — it does not change what the
agents analyse (they always read every timeframe).

---

## Historical Replay

### `Replay last N sessions` + the dropdown
Walks historical candles bar by bar and grades every setup that would have
fired, in R-multiples.

**Click when:** after the close, or when you've changed thresholds and want to
know whether the change helps.

**Read the expectancy first.** Negative expectancy means the current
configuration did not work over that window. That is a reason to change the
configuration, not to trade it anyway.

---

## A normal day

| Time | Action |
|---|---|
| 08:45 | `Pre-market scan` |
| Just before open | `Scan watchlist` |
| Through the session | Let it cycle. Watch for **TRADEABLE** cards |
| 15:00 (IN) / 15:30 (US) | Entry cutoff — no new positions by design |
| After close | `Replay last 5 sessions`, check the Agent Scorecard |

---

## The two things that actually protect you

1. **`PAPER / ALERT-ONLY` in the header.** While it says this, nothing reaches a
   broker regardless of what any panel shows.
2. **The data-source badge on the opportunity board.** While it says
   `SIMULATED DATA`, the prices are from a random-walk generator and refer to
   nothing real.

Setting a small capital does **not** make live trading safer — it only makes
position sizes smaller or zero. Paper mode is what makes it safe.

---

## Weekly Review

| Button | What it does | When to press it |
|---|---|---|
| **Build this week's review** | Reads the week's journal and the decision log, then (optionally) asks the model for a strategy read. | After Friday's close. Mid-week works and is labelled provisional. |
| **Week** (date box) | Any day inside the week you want. Blank = this week. | Reviewing an earlier week. |
| **Ask the model for a strategy read** | Untick to skip the LLM pass. Sections 1 and 2 are pure database reads and appear instantly; a local model takes a minute or two. | Untick when you just want the numbers. |
| **Download .md** | The report as a markdown file. | To keep, print, or read outside the app. |
| **Download .json** | The same data, structured. | To compare weeks or feed something else. |
| **Save to repo** | Writes it to `journal/weekly/` so you can commit it. | You want the history in git. The engine also does this automatically once the week closes. |

Each trade expands to show **what every analyst said at the moment of entry** —
its score, its confidence and its reasoning — plus the counter-argument the
desk recorded before the outcome was known. Abstentions are listed too: an
analyst with no data was never a quiet vote of agreement.

Full details in [WEEKLY-REVIEW.md](WEEKLY-REVIEW.md).


---

## Trading Day

| Button | What it does | When to press it |
|---|---|---|
| **▶ Start trading day** | Arms order placement for **today only**. The desk already arms itself at the open, so this is for starting by hand or restarting after a Stop. | Any time during the session. |
| **Stop** | Disarms immediately. The desk keeps analysing and alerting; it just stops sending orders. A Stop is not undone by the next auto-arm. | When you want it to stop trading but keep watching. |
| **Day report** | The session so far, on demand. | Any time — the **Today** panel publishes the same thing by itself after square-off. |

The badge beside them says which of three states you are in:

| Badge | Meaning |
|---|---|
| `ALERT ONLY (no orders)` | `AUTO_PLACE_ORDERS` is off. Nothing is sent anywhere. Not the default — something has turned it off. |
| `PAPER ORDERS ON` | Approved signals become **simulated** orders, on a day you have armed. |
| `LIVE ORDERS` | Real orders. Real money. |
