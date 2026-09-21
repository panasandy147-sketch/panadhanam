# Practice Day

> **This is a REPLAY of a past session, not live trading.**
>
> It does **not** run during market hours waiting for signals. It takes a day
> that already happened and fast-forwards through it so you can get reps.
>
> For live paper trading during market hours, see
> [Live paper trading](#live-paper-trading-the-other-thing) at the bottom —
> that is the normal engine, and it is already running whenever the app is open.

Replays a **real** trading session bar by bar, running the full agent desk at
each step, and streams it to the dashboard as if it were happening live.

The point is reps. A day takes minutes instead of hours, so you can run a
fortnight of sessions in an afternoon — including at the weekend, when the
market is shut and you actually have time.

---

## Running one

1. Open the **Practice Day** panel.
2. Leave the date blank for the most recent session, or pick a weekday.
3. Choose a speed:

| Speed | A full day takes | Use it for |
|---|---|---|
| 12x | ~30 min | Watching a day unfold, bar by bar |
| **60x** | **~6 min** | **The default — a session over coffee** |
| 180x | ~2 min | Working through several days |
| 600x | seconds | Batch practice |

4. **▶ Start practice day**.

You can **Pause** mid-session to study a setup, and change speed while running.

---

## No lookahead

At bar N the agents see bars 0 to N and nothing else. Indicators, patterns and
the regime are all recomputed from that window alone.

The engine knows how the day ended. The agents never do. That's what makes the
result worth anything.

Two more honesty rules:

- A bar that touches **both** your stop and your target is scored as a **loss**.
  Without tick data the order is unknowable, and optimism there would flatter
  every session you ever run.
- Anything still open at the close is **squared off** at the last price, exactly
  as the real desk would.

---

## What the desk can and cannot see in a replay

The desk will not fire a signal on fewer than `consensus.min_confirmations`
(default **2**) independent analysts. So a replay is only useful if it can put
more than one analyst in the room. Here is exactly what it rebuilds:

| Analyst | In a replay | Why |
|---|---|---|
| **Candlestick & Technical** | ✅ votes | Recomputed from bars 0..N |
| **Macro & Flow** | ✅ votes | The macro tape (futures, VIX, yields, crude, dollar) is re-fetched **for the replayed day** and served as it printed at each bar |
| **News & Sentiment** | ❌ abstains | Headlines for a past session are not retrievable from the RSS feeds |
| **Options & Futures** | ❌ abstains | Historical option chains (OI, IV) are not available without a paid feed |
| **Fundamental Filter** | ❌ abstains | It screens on today's filings, which the replayed day had not seen |

The macro tape is fetched for the replayed date specifically — serving
*today's* VIX against last Tuesday's chart would leak the answer into the
question, which is the one thing a replay must never do.

So a replay runs on **two voices where live trading has up to five**. That
means:

- Replay is **stricter** than live. A setup that trades live may not clear the
  bar here.
- A quiet replay is **not** evidence of a quiet market. The panel headed
  *"Analysts that could not vote in this replay"* names every silent seat so
  you can tell the two apart.

If the macro tape will not load — a firewall, an offline laptop — the macro
analyst abstains too, leaving **one** voice, and the desk then cannot trade at
all by construction. You will see `macro_flow` in the same panel saying so. Set
`practice.rebuild_macro: false` in `config/settings.yaml` if you want to skip
the fetch deliberately; it will not change the arithmetic, only the wait.

---

## Reading the result

| Metric | What it tells you |
|---|---|
| Signals taken | How many setups cleared every gate |
| Win rate | Of the closed trades |
| **Total R** | The sum of outcomes in units of risk — the headline |
| P&L | Total R × your per-trade risk |

**"Why the desk stayed out"** is the part most people skip and shouldn't. A day
with few trades is normal — most bars contain no valid setup. That panel tells
you what the desk was waiting for:

```
WHY THE DESK STAYED OUT (976 passes)
  • 976×  Not enough confirmations
```

If one reason dominates every session, that's your configuration talking. Three
common readings:

- **Not enough confirmations** — your analysts are abstaining. Usually a data
  gap: no option chain means the derivatives analyst can't vote. Check
  `--check-data`.
- **Position sizes to zero** — your capital is too small for the instruments in
  your watchlist at the current stop distances.
- **Conviction below threshold** — genuinely quiet tape, or
  `consensus.min_composite_score` is set high.

---

## Grading yourself

**Log to journal** sends every closed practice trade through the post-mortem
engine. You get a Mistake Card per trade, written to `journal/cards/`, and your
Mistake Cost Index updates.

This is the loop that actually teaches: practise a day, grade it, read what went
wrong, change one thing, practise another day.

---

## A two-week programme

Roughly what a fortnight of deliberate practice looks like:

| Days | Focus |
|---|---|
| 1–3 | Run days at 60x. Don't change any settings. Learn what the desk does. |
| 4–6 | Read "why the desk stayed out" each time. Fix data gaps first, not thresholds. |
| 7–9 | Change **one** threshold, run three days, compare Total R. Revert if worse. |
| 10–12 | Log everything to the journal. Look at the Mistake Cost Index, not the P&L. |
| 13–14 | Run fresh days you haven't seen. If Total R holds up, the settings are real. |

Commit `journal/` as you go — that history is the record of what you learned.

---

## Things worth knowing

- Practice **pauses the live cycle** so the two don't fight over the same risk
  desk, and hands it back when you stop.
- It is **refused while you have open positions**, for the same reason.
- It uses the same capital and risk settings as live. Change capital first if
  you want to practise at a different size.
- Intraday history is limited by your data feed. Yahoo gives roughly 60 days of
  5-minute bars, so pick a date inside that window.
- Practice trades **never reach a broker**, whatever your execution settings.


---

## Live paper trading (the other thing)

If what you want is *"be trading when the US market opens tomorrow"*, that is
not this button — it is the **normal engine**, which runs automatically whenever
the app is open.

### How to set it up

```bash
# .env
ACTIVE_MARKET=US
BROKER=alpaca
ALPACA_API_KEY=PK...
ALPACA_API_SECRET=...
ALPACA_PAPER=true
```

```yaml
# config/settings.yaml
execution:
  auto_place_orders: true     # the only switch a paper account needs
```

Then just leave the app running. At 09:30 ET it starts cycling every
60 seconds: the analysts run, the CMIO synthesises, the risk desk sizes, and an
approved signal becomes a simulated order in your Alpaca paper account.

### What is tracked, automatically

| Where | What |
|---|---|
| **Risk & Capital** tiles | Day P&L, realised vs open, wins/losses, room before halt |
| **Signals** panel | Every signal, with rejections and their reasons |
| **Open Positions** | Live positions with entry, stop and target |
| **Trade Journal** | **Every closed trade is graded automatically** and written to `journal/cards/` |

That last one matters: when a live trade hits its stop or target, the system
writes a Mistake Card for it without you having to remember. Your Mistake Cost
Index and R-multiple history build up on their own.

Turn it off with `journal.auto_log_live_trades: false` if you'd rather log by
hand.

### The two are complementary

- **Practice Day** — many reps, fast, on days that already happened.
- **Live paper** — one day at a time, in real time, with real order flow.

Use Practice Day to learn the system and tune thresholds. Use live paper to see
whether it holds up when you can't skip ahead.
