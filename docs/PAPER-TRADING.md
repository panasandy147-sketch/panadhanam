# Paper Trading

Real prices, simulated fills, no money at risk and no broker account needed
beyond a free simulator. This is the normal way to run the desk.

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

```bash
# .env  (untracked, so a git pull will not undo it)
AUTO_PLACE_ORDERS=true       # the only switch a paper account needs
```

Restart the app, then press **Start trading day**. Until you do, the badge
reads `ARMED · alerts only` or `not armed` and nothing is sent — arming is a
decision taken each morning and it expires at square-off.

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


---

## It arms itself

`trading_day.auto_arm_on_open: true` (the default) arms the desk when the
session opens, so it can act on what it finds without anyone at the screen.
**Start trading day** still works and still arms immediately — use it on the
mornings you want to start it yourself, or to restart after a Stop.

Arming still expires at square-off and does not survive a restart, and a
**Stop** you press is not undone by the next auto-arm.

> **Real-money accounts are never auto-armed**, and there is no setting that
> permits it. Committing real capital is a decision a person takes each
> morning, not one a config file takes overnight. On a real account you press
> the button.

## The day's summary, on screen

A few minutes after square-off the **Today** panel fills in by itself:
signals generated, trades taken, closed, win rate, total R, P&L, and the
trade-by-trade table.

A day with no trades still publishes, with *what the desk was waiting for*.
That is the more common outcome and the more useful one to read — a quiet
market and a rule that never passes look identical without it.

Set the delay with `trading_day.summary_after_square_off_minutes`.

## At the end of the week

Every trade is journalled as it closes, graded on process rather than P&L.
The **Weekly Review** panel builds the week from those entries — each trade
with the reasoning behind it, which analyst earned its weight, the Mistake
Cost Index, and a coach's read. See [WEEKLY-REVIEW.md](WEEKLY-REVIEW.md).
