# How do I know what to trade?

The short version: **the system never tells you to trade something. It tells you
what it found, how much can go wrong, and why it did or didn't clear the desk's
rules.** You make the call.

Here is how to read each part.

---

## 1. Start with the Trade Opportunities board

Click **Scan watchlist**. Every symbol is analysed and sorted into three buckets.

### The tiers are about risk, not reward

| Tier | What it means |
|---|---|
| **Low risk** | Liquid instrument, timeframes agree, clean structural stop, cheap premium |
| **Medium risk** | A normal setup with one or two things working against it |
| **High risk** | Thin confirmation, expensive option premium, volatile tape, or a wide stop |

A LOW-risk idea is **not** a better trade than a HIGH-risk one. Position sizing
already equalises the rupee at stake — every trade risks the same 1% of capital.
The tier tells you how likely the stop is to be a **clean, honest exit** rather
than a gap or a noise-triggered whipsaw.

If you're new to this, trade the low tier until your scorecard says you're
consistently right. The high tier is where the fastest moves and the fastest
losses both live.

### Every card shows its own reasoning

```
INFY          ▲ LONG    conviction 0.35 · trending_up          [WATCH]
  Instrument       INFY26SEP1860CE
  Entry            23.57
  Stop loss        15.32
  Target (2.0R)    40.07
  Quantity         0
  Risk             ₹0 (0.00%)
⚠ Position sizes to 0 lots: risk budget 1,000 / 8.25 pts = 121.2 units,
  but one lot is 400. Capital is too small for this stop distance.

  +1 Instrument   +2 Timeframes   0 Regime   +1 Stop   +1 IV   +1 Blind spots
```

- **▲ LONG / ▼ SHORT** — the direction the evidence points.
- **conviction** — how strongly the analysts agree (0 to 1).
- **TRADEABLE vs WATCH** — this is the one that matters.
- **The ⚠ line** — exactly why a WATCH item isn't tradeable. It leads with the
  structural problem, not the clock, so "you can't afford one lot" is never
  hidden behind "the market is closed".
- **The coloured chips** — every risk point, attributable to a named reason.
  Red adds risk, green removes it. Hover for the detail.

### TRADEABLE means it cleared every gate

- At least 2 independent confirmations
- Composite score past the threshold
- Risk:reward of at least 1:2
- A stop that isn't absurdly tight or absurdly wide
- Position sizes to at least one lot
- The desk isn't halted, isn't full, and isn't past the entry cutoff

Anything less and it's a WATCH: worth understanding, not worth an order.

---

## 2. Read the counter-argument before the rationale

Every approved signal carries an **Against:** line — the strongest case for the
opposite view. This is deliberate. The failure mode of any signal system is
reading only the part that agrees with you.

If the counter-argument is "macro and news abstained this cycle, so the call
rests on technicals alone", that is telling you the desk is half-blind right now.

---

## 3. The alert format

```
[NIFTY | NIFTY 24500 CE] [BUY] [ENTRY 120.50] [SL 78.30] [TGT 205.00 (2.0R)] [breakout, long buildup]
```

`[underlying | instrument] [side] [entry] [stop] [target (R:R)] [confirmations]`

The stop is not advisory. It's the number the position was sized from — if you
widen it after entry, you are no longer risking 1%, and the maths the whole
system rests on is void.

---

## 4. Historical Replay — "what would this have caught?"

Pick a window and hit **Replay**. It walks real candles bar by bar, rebuilds the
analysis using only data available at that bar, and grades every setup that
fired.

Read it in this order:

1. **Expectancy** — positive or negative. This is the headline.
2. **Total R** — the sum of all outcomes in units of risk. +10R on a 1% risk
   setting is roughly +10% on capital, before costs.
3. **Win rate vs break-even.** At 2:1 R:R you need **33.4%** to break even. A 45%
   win rate at 2:1 is a good system; a 60% win rate at 1:1 is not.
4. **The worst trades table.** Read it. It shows you what the system gets wrong,
   which is more useful than what it gets right.

### What the replay honestly cannot tell you

It says so itself on screen, but to be explicit:

- It replays the **deterministic rule engines only** — no LLM reasoning.
- It assumes fills exactly at stop/target with **zero slippage or brokerage**.
  Real results are worse.
- When one candle touches both stop and target, it scores it as a **loss**,
  because without tick data nobody knows which came first.
- It is a **threshold sanity check, not a tradeable backtest.** No
  survivorship-bias-free universe, no corporate actions.

A negative expectancy on the replay is a reason not to trade the configuration.
A positive one is not permission to trade it.

---

## 5. The data-source badge — check it every time

Both panels carry a badge:

> **SIMULATED DATA — these are not real market prices**

That appears whenever `BROKER=paper`. The paper broker generates a synthetic
market so you can see the whole system work without credentials. Those prices are
**invented**. Any "top gainer" from that data is a property of a random number
generator, not of the market.

When you connect a real broker the badge changes to `Live data via zerodha`
(or upstox / angelone), and only then do the numbers refer to reality.

### Getting real data

```bash
# .env
BROKER=zerodha
KITE_API_KEY=...
KITE_API_SECRET=...
```
```bash
pip install kiteconnect
python -m scripts.kite_login          # then follow the two steps it prints
```

Restart. The badge should now say `Live data via zerodha`. The replay will then
report genuine last-week results, and the board will rank real setups.

---

## 6. A sane daily routine

| When | What |
|---|---|
| **08:45** | `Pre-market scan` — fundamental filter + global macro read |
| **09:10** | `Scan watchlist` — see what's set up before the open |
| **09:15–15:00** | Let it cycle. TRADEABLE cards and the Signals panel are your alerts |
| **15:00** | Entry cutoff — no new positions, by design |
| **15:15** | Square-off time |
| **After close** | `Replay last 5 sessions` and check the Agent Scorecard |

---

## 7. What the system will not do for you

- It will not tell you a trade is going to work.
- It will not account for your existing positions elsewhere.
- It will not stop you overriding the stop it sized the position from.
- It cannot see a gap coming. The daily loss limit is a floor on **intraday**
  drawdown, not a guarantee.

Run it in paper mode for weeks. Watch the Agent Scorecard's hit rate. If the
replay expectancy is negative on your configuration, the answer is to change the
configuration — not to trade it anyway and hope.
