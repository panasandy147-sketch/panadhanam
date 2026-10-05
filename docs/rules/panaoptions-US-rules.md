# panaoptions — US rules and strategies

> Snapshot of the live US rules page, 6 Oct 2026 (after the US screen trial). The Rules button in the app always shows the current version. **ON** = trading now; OFF = switched off for the US.

## The swing book (1-4 day options)

Beside the same-day strategies, in the same paper account, this desk holds a few positions overnight. One strategy, the one with evidence on two years of hourly bars in the US, India and gold: Larry Williams' volatility breakout, exited by his first-profitable-open rule.

- Hunted on every one of these, screen or no screen: **GLD, SPY, QQQ, AAPL, NVDA, TSLA, AMD, MSFT, AMZN, META, GOOGL**
- Entry: the first 5m bar today whose high reaches today's open + this share of yesterday's range (calls; puts: the low reaches the open minus it): **0.5 x range**
- Only with the trend: yesterday's close against the close this many sessions before: **20 days**
- Entries between: **09:45–15:30**
- Stop: today's open, on the underlying, checked every bar until the trade closes: **the open**
- Exit: the first later session that OPENS in profit (Larry Williams' bail-out): **on**
- …else closed at the end of this session (the entry day is 0): **4**
- Options: days to expiry (theta is small over a few days; never a shorter fallback): **21–45**
- Options: delta: **0.4–0.55**
- Options: the most a contract may cost a share: **$20.00**
- Swing positions held at once (their own slots, outside the same-day open and daily limits): **2**
- Premium backstop (the open is the real stop): **60.0%**
- Never the same-day exits: no square-off at the close, no scale-out or trail, no time stop, no breakeven tighten, no 1:3 room check: **—**
- Shared with the whole account: 2% risk a trade, the deployed ceiling, the daily circuit breaker: **—**
- On Auto: a swing position open when its market closes is parked in that market's book while the other market trades: **—**

## How a trade happens

Paper only. Every cycle the desk reads each symbol that passed the pre-market screen at the same moment, runs every strategy whose window is open, and buys the option contract for the first setup that triggers, as long as there is room under the position and capital limits. Everything is in New York time.

- Market (the US / India / Auto toggle in the header): **United States — 09:30–16:00 America/New_York, account in $**
- Desk profile: **zerodte — same-day options, 5-minute triggers**
- Symbols the desk watches: **SPY, QQQ, AAPL, NVDA, TSLA, AMD, MSFT, AMZN**
- Auto watchlist: only names whose options can be bought — a call AND a put near the money with a bid and a spread within 7.0%, and an expiry inside the desk's 0–4 day window (a monthly-only name is never listed; SPY, QQQ and held names are not checked): **on**
- Pre-market screen: relative volume at least: **1.2x the 20-day average, paced on the market's own session (09:30–16:00). An index with no volume in the feed (NIFTY, BANKNIFTY, FINNIFTY) is judged on the gap alone**
- Pre-market screen: gap from yesterday's close at least: **not required (US TRIAL from 6 Oct 2026)**
- Screen runs from, and re-runs while nothing passes, every: **09:00, every 1 min**
- First entry allowed at: **09:45**
- Last entry allowed at (the last strategy window's close): **15:45**
- Everything is closed at: **15:59**

## Who approves it

A triggered strategy is only a signal: symbol, direction, trigger, invalidation level on the stock, confidence. It knows nothing about money. Three agents vote on it, the CMIO combines the votes, and the Risk Gatekeeper checks the contract before any order exists. With Ollama running each agent also asks the local model and blends its view in; without it the rules score stands.

- Technical agent: 5m trigger vs the 15m trend; relative volume (trigger bar or session, whichever is higher) must be at least: **1.5x — hard veto below (not applied to an index with no volume in the feed)**
- Strategies the RVOL veto does not apply to (backtested without it, so they trade live as tested): **none**
- Midday RVOL: between these times (market clock) every volume gate asking for more asks for this instead — the Technical agent's veto and the strategies' volume multiples: **10:30–14:00: 1.2x**
- A live 5m bar with NO volume (0 or missing — feed lag, or an NSE index) is 'not measured', not 'quiet': **the volume gates are bypassed for it, and the log says so**
- Volume profile confluence on every signal: a call at the VAL or POC, or a put at the VAH or POC, scores +0.3; a thick High Volume Node straight ahead costs 0.3 within 1.0 ATR and is a veto within 0.25 ATR: **boost / penalty / veto**
- Extreme options flow (a contract at 10x+ its open interest): AGAINST the trade = strict veto (no calls into heavy put buying); WITH the trade = the RVOL gate relaxes to 1.3x. The Derivatives vote counts 2x meanwhile: **veto / override**
- Derivatives & Flow agent: the contract, put/call ratio, IV percentile (penalised above), unusual flow; no contract = veto: **IV ceiling 80th percentile, 0–1 DTE preferred**
- Macro & Sentiment agent: a high-impact headline against the trade (downgrade, guidance cut, investigation under a call; buyout, upgrade under a put) or index futures moving hard against it is a hard veto: **futures veto at 1.5%**
- CMIO: weighted score × strategy weight must reach, with no veto: **0.55 (weights technical 0.35, derivatives 0.35, macro 0.3)**
- Strategy weights (tuned by the Friday reflection): **pd_liquidity_sweep 1.00, orb_vwap 1.00, vwap_ema_pullback 1.00, liquidity_sweep 1.00, candlestick_at_level 1.00, va_rejection 1.00, lvn_acceleration 1.00, poc_bounce 1.00**
- Ollama second opinion: **on — blended at 40%, 20 s timeout**

## The strategies

Every condition listed must be true together. If two strategies trigger on the same symbol in the same cycle, the one listed first wins. Setups that do not trigger are still logged with the reason, so a quiet day can be told apart from a broken one.

### 0 · Previous Day Liquidity Sweep (failed breakout) — first in line — OFF
- Window: 09:45 – 15:00 ET
- Buy:
  - LONG_CALL: a 5m candle sweeps BELOW the previous-day low (PDL) by no more than 0.5%, and the very next candle closes back inside yesterday's range — a tweezer bottom or swing low at the PDL.
  - LONG_PUT: the mirror — a sweep ABOVE the previous-day high (PDH) within 0.5% and the next candle closes back inside, as a tweezer top or swing high.
  - Call open interest rising (a PDL sweep) / put open interest rising (a PDH sweep) — the F&O confluence filter.
  - The target must be at least 1:3 against the stop at the sweep wick — checked before the order.
- Wrong: A trade through the sweep candle's wick — the stop sits 2 ticks beyond it.
- Target: The day's VWAP or 3R, whichever is further. No time stop.

### 1 · Opening Range Breakout + VWAP — OFF
- Window: 09:45 – 15:00 ET
- Buy:
  - The 15-minute opening range (09:30–09:45 high and low) has formed.
  - A 5-minute candle CLOSES above the range high (calls) or below the range low (puts).
  - Price is on the same side of VWAP, and the 9 EMA is above the 21 EMA for calls (below for puts).
  - Volume on the break is at least 1.5x the average. Without it, the break is recorded as unbacked and not taken.
- Wrong: A 5-minute close back inside the opening range.
- Target: 1.5x the range height, measured from the broken edge.

### 2 · VWAP / 9-EMA Pullback — OFF
- Window: 10:00 – 15:00 ET
- Buy:
  - The 15-minute chart is in a stacked trend: price > 20 EMA > 50 EMA for calls, the reverse for puts (needs 50 bars of 15m history).
  - Price pulls back to the 9 EMA or VWAP (within 0.35 ATR).
  - A rejection candle prints at the line (engulfing, hammer or similar).
  - That candle closes beyond the previous candle's high (calls) or low (puts). The desk enters on the rejection and does not chase.
- Wrong: A 5-minute close on the wrong side of VWAP.
- Target: The desk's standard exits (below).

### 3 · Liquidity Sweep Reversal — **ON**
- Window: 09:45 – 14:30 ET
- Buy:
  - A 5-minute candle pokes below the pre-market low (or above the pre-market high), taking the stops resting there.
  - The NEXT candle closes back inside the pre-market range.
  - Price crosses VWAP in the new direction: above for calls, below for puts.
  - Volume is at least 1.5x the average.
- Wrong: Price goes back through the extreme of the sweep.
- Target: The desk's standard exits (below).

### 4 · Candlestick at a Key Level — **ON**
- Window: 09:45 – 15:00 ET
- Buy:
  - A reversal pattern on the 5m chart — only these: Hammer, Shooting Star, Bullish Engulfing, Bearish Engulfing, Tweezer Bottom, Tweezer Top, Double Rejection Top, Double Rejection Bottom (a hammer or shooting star that pierced the level and closed back counts as a liquidity sweep rejection).
  - Double rejection: the same high (put) or low (call) rejected twice, 2–8 bars apart, with price leaving the level by a full average bar in between.
  - It prints AT a level the market has turned at before, within 0.5 ATR: the previous-day high or low, a swing high or low, the pre-market extreme, the opening range edge, yesterday's close, VWAP or a moving average. VWAP, the averages and yesterday's close are resistance from below and support from above. A pattern anywhere else is ignored.
  - When the newest pattern is refused (its trend, its level, or its trigger not broken yet) the next pattern on the same bars is judged.
  - F&O confluence (strict): a call only where the pattern swept the previous-day LOW and closed back above it with call open interest rising; a put only where it tested the previous-day HIGH and closed back below it with put open interest rising. Unknown OI: block.
  - Price then takes out the pattern's trigger (the high of a hammer, the low of a shooting star) within 2 bars. The pattern alone is not the entry.
  - Each pattern picks its own contract (delta and days to expiry). See the table below.
- Wrong: Price breaks the structure the pattern formed at.
- Target: The desk's standard exits (below).

### 5 · Value Area Rejection (failed auction) — **ON**
- Window: 09:45 – 15:00 ET
- Buy:
  - Built on the session volume profile (RTH 09:30–16:00): the POC and the 70% value area (VAL–VAH), for the prior and the current session.
  - Puts: price pushes clearly above the VAH, fails to hold (a one-sided rejection wick or a bearish engulfing) and closes back inside value.
  - Calls: price drops to the VAL, tests it, and rejects with a hammer or bullish engulfing, closing back above it.
- Wrong: Beyond the poke's high (puts) or the test's low (calls).
- Target: The POC.

### 6 · LVN Pocket Acceleration — **ON**
- Window: 09:45 – 15:00 ET
- Buy:
  - A Low Volume Node is a price pocket inside the profile where almost nothing traded (≤ 0.3x the average bin) — price tends to travel through it fast.
  - After a 3-bar consolidation shelf, a 5-minute candle closes cleanly INTO the pocket on relative volume ≥ 1.5x.
  - Calls on an upside break, puts on a downside one.
- Wrong: Back inside the shelf it broke out of.
- Target: The far edge of the pocket.

### 7 · POC Magnet / Bounce — OFF
- Window: 10:00 – 15:00 ET
- Buy:
  - Price moves at least 1.0 ATR away from the POC, then comes back to retest it.
  - A clean rejection candle prints at the POC: calls when it came back down from above, puts when it came back up from below.
- Wrong: A close through the POC.
- Target: The swing it came back from.

### 8 · Volatility Breakout (Larry Williams, 1987 World Cup) — **ON**
- Window: 09:45 – 14:30 ET
- Buy:
  - The FIRST 5m close above today's open + 0.5 x yesterday's range (calls), or below the open minus the same (puts). A level crossed earlier in the day is not taken — that is a chase.
  - Price on the same side of VWAP, and the 9 EMA above the 21 EMA for calls (below for puts).
- Wrong: Back 1.0 of the way from the entry to today's open — the expansion failed.
- Target: 3.0R.

### 9 · SJK 50-200 — 50 / 200 EMA pullback continuation (the user's) — OFF
- Window: 09:45 – 15:15 ET
- Buy:
  - Calls: price above the 200 EMA; a pullback whose low comes within 0.15% of the 50 EMA (or through it) without a single close below the 200 EMA; then the FIRST 5m close above the swing high made before the pullback. Puts: the mirror, below the 200 EMA, breaking the swing low.
  - A swing high / low is a bar beyond the 3 bars on each side; the pullback swing at most 36 bars old. One trade per swing point.
- Wrong: Back through the pullback's swing low (calls) or high (puts) — the stop, on the underlying.
- Target: 1:2.5 of that risk, SOLD there. Judged at its own 1:2.5, not 1:3.

### 10 · SJK 9-15-21 — the 9 / 15 / 21 EMA fan (the user's) — OFF
- Window: 09:45 – 15:45 ET
- Buy:
  - Calls: the 9 EMA above the 15 above the 21 (purple, blue, grey on the chart). Puts: 21 above 15 above 9.
  - Not Sideways / Choppy: each EMA gap at least 0.02% of the price, the order changed at most 2 times in the last 12 bars, and the 21 EMA moving at least 0.05% the trade's way over them.
  - Entry: the close of the candle where that fan-out confirms, or a pullback whose low touches the 9/15 band, holds the 21 and closes back beyond the 9 (entry_mode: both). One trade per continuous alignment — the same fan-out never fires twice.
- Wrong: Beyond the recent swing low / high (3 bars each side, at most 24 bars old), else beyond the 21 EMA — the stop, on the underlying (stop_mode: swing).
- Target: 1:2.0 of that risk, SOLD there. Judged at its own 1:2.0, not 1:3.

### 11 · SJK 9/21 · VWAP · ADX (the user's) — OFF
- Window: 09:45 – 15:45 ET
- Buy:
  - Calls: the 9 EMA crosses above the 21 EMA with the close above the session VWAP; the NEXT candle closes above the 9 EMA and VWAP. Puts: the mirror. Entry on that confirmation close, never on the crossover candle.
  - ADX(14) above 20.0; not Sideways / Choppy (at most 2 crossings in 12 bars; EMAs, VWAP and price not inside 0.1%).
- Wrong: Through the crossover candle's low (calls) or high (puts).
- Target: 1:2.0 of that risk, SOLD there.

### 12 · sjk912RSi — 9/21 EMA + RSI crossover (the user's) — OFF
- Window: 09:45 – 15:45 ET
- Buy:
  - Calls: within 5 bars of the 9 EMA crossing above the 21, a green candle opening and closing above both EMAs with RSI(14) above 50.0. Puts: the mirror. One entry per crossover.
- Wrong: Through the lowest low (calls) / highest high (puts) of the 5 bars before the entry.
- Target: 1:2.0 of that risk, SOLD there.


## Previous-day F&O and reward to risk

Once a day after the screen the desk maps each watched symbol's previous session: high (PDH), low (PDL), close (PDC), total call and put open interest and their change, and the build-up — Long Buildup (price up, OI up), Short Buildup (down, up), Short Covering (up, down), Long Unwinding (down, down). US chains carry the previous close's OI, so the change is the previous session's; NSE updates it during the day.

- Map the previous day's F&O picture: **on**
- Reversal calls only at a PDL sweep with rising call OI; reversal puts only at a PDH test with rising put OI: **on — pd_liquidity_sweep, candlestick_at_level, liquidity_sweep**
- GO / NO-GO before any 5-minute candlestick pattern is read: LONG CALL only after a clean sweep below the PDL that closed back inside; LONG PUT only after a sweep above the PDH. No sweep: skipped — "No institutional sweep of previous day extremes.": **candlestick at level, liquidity sweep**
- Cached before the open, from: **09:00 (ET)**
- How a PDH / PDL is judged: a 5-minute candle sweeps through the level by no more than 0.5% and the very next candle closes back inside yesterday's range (the stop goes beyond that candle's wick): **sweep**
- When the chain carries no open interest: **block**
- Every trade's projected target is at least this multiple of the distance to its invalidation, with no previous-day, opening-range or pre-market level in the way: **1:3**
- Stop floor: the stop on the underlying sits at least the larger of this many 5m ATRs or this % of the price from the entry (a closer one is widened, and the target kept at 1:3 from it): **1.0 × ATR or 0.25%**
- Target snapping: with less than 1:3 of room to the nearest level but at least this much, the trade is taken with the target 2 ticks inside that level: **2.2R**
- Verified 1:3 — these must reach it with their OWN target (the POC for a value-area rejection, VWAP-or-3R for the sweep), not a projected one: **pd liquidity sweep, va rejection**

## Which contract it buys

Calls for a LONG_CALL setup, puts for a LONG_PUT one — both bought, never sold naked. Each trade is logged as executed outright, converted to a debit spread, or skipped by a hard risk gate (with which gate and why). A single option you buy — or, when that is over budget, a debit spread: buy the target-delta option and sell one further out of the money, so the most it can lose is what was paid.

- Days to expiry (strategies 1–3) — the NEAREST expiry first: 0DTE where the symbol lists one (SPY/QQQ/IWM daily; most stocks only on Friday), otherwise that week's: **0–4 days**
- Delta — the primary tier (strategies 1–3): **0.4–0.5**
- Bid/ask spread no wider than (judged on the rolling 1-minute volume-weighted spread, re-sampled inside the minute before refusing): **7% of the mid price**
- Opening window: for expiries within 4 days, the spread allowance widens from 09:30 to 10:15 (opening quotes are wide; after that, the normal limit): **off**
- Over budget, in order: the same delta with less time; a debit spread (buy the primary-tier option, sell a strike further out, reward:risk ≥ 0.8); then the secondary tier down to this delta, liquid contracts only (open interest ≥ 100 or volume ≥ 50). Nothing funded = skipped as a hard risk failure: **0.25**
- Single-leg grace: when the debit spread fails only because the short leg has no liquid, priced strike, buy the long leg outright if premium x lot fits this share of the budget: **100%**
- Unusual options flow (volume ≥ 3.0x open interest, ≥ 1000 contracts) passes the screen and scores for or against a setup in the Derivatives vote: **on**
- Contract price between: **$0.5 and $3.5 (×100 per contract)**

### 
- Pattern: Hammer
- Delta: 0.4–0.5
- Dte: 0–4 days

### 
- Pattern: Bullish Engulfing
- Delta: 0.4–0.5
- Dte: 0–4 days

### 
- Pattern: Tweezer Bottom
- Delta: 0.4–0.5
- Dte: 0–4 days

### 
- Pattern: Shooting Star
- Delta: 0.4–0.5
- Dte: 0–4 days

### 
- Pattern: Bearish Engulfing
- Delta: 0.4–0.5
- Dte: 0–4 days

### 
- Pattern: Tweezer Top
- Delta: 0.4–0.5
- Dte: 0–4 days

### 
- Pattern: Liquidity Sweep Rejection
- Delta: 0.4–0.5
- Dte: 0–4 days
- Claimed: 80% (published claim, unverified here)

### 
- Pattern: Double Rejection Top
- Delta: 0.4–0.5
- Dte: 0–4 days

### 
- Pattern: Double Rejection Bottom
- Delta: 0.4–0.5
- Dte: 0–4 days


## How much it buys

Two caps size every trade and the tighter wins: the premium paid as a share of the account, and what the trade loses at its stop. Then the tournament throttles: few positions, few trades a day, and a hard daily lockout.

- Account size: **$5,000**
- Premium per trade, at most: **30% = $1,500**
- Index ETFs (SPY, QQQ, DIA) — high-notional contracts, so a higher cap: **30% = $1,500**
- Risk Gatekeeper refuses a bid/ask spread wider than: **7% of the mid**
- All open trades together, at most: **60% = $3,000**
- Loss at the stop per trade, at most — the first exit to fire (delta x the distance to the underlying stop, or the premium backstop); with the premium cap above, the tighter one sizes it: **2% = $100**
- Open trades at once, at most: **2**
- Trades a day, at most (no over-trading): **3**
- No more entries after this many losing trades in a day: **off**
- Of those, the last ones are kept for the strategies with the best backtested edge (the last `run.py --backtest`, else the priority list: pd liquidity sweep, orb vwap); setups firing together are taken best edge first: **1**
- Circuit breaker: once the day's loss, closed plus open, reaches this, pending signals are cancelled, everything is sold, orders are refused and the desk is LOCKED OUT for the rest of the calendar day — a restart does not clear it: **3% = $150**
- History validation (python run.py --backtest): expectancy per trade at least, and max drawdown no more than: **0.5R · 5%**
- Simulated cost per contract, each side: **$0.02**

## When it sells

The first of these to happen closes the trade.

- Stop: the strategy's own invalidation on the STOCK decides (see each strategy's 'wrong if'). The option's price is not used, because an IV drop or a wide spread says nothing about whether the idea was wrong.: **underlying**
- Disaster backstop: out regardless if the option falls this far: **45%**
- Hold: no scale-out, no breakeven, no trail — the trade keeps its original stop on the UNDERLYING (and the premium backstop) until the square-off: **on (scale_out_r 0)**
- Morning trades that are green at this time get their stop moved to breakeven before the lunch slump: **10:45**
- Time limit on a trade that has not reached its first target: **none (time is what the contract bought)**
- Everything still open is sold at: **15:59**

## After the trade

Every closed trade is graded on process, not outcome. A winner that broke a rule grades low, and a loser that respected its invalidation grades high. The daily and weekly reviews are built from these grades.

- Grade each trade as it closes: **on**
- Friday reflection: after the close, the week's graded trades go to Ollama, which returns strategy weight adjustments; they are bounded and written to config/learned.yaml (also `python run.py --reflect`): **on — at least 5 trades, at most ±0.15 a week**
