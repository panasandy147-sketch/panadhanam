# panadhanam — US rules and strategies

> Snapshot of the live US rules page, 6 Oct 2026 (after the US screen trial). The Rules button in the app always shows the current version. **ON** = trading now; OFF = switched off for the US.

## How a trade happens

Paper trading. Every cycle, each symbol on the watchlist goes through the same five steps, and a trade is placed only when every step says yes. Times are in the market's own zone (America/New_York).

- A full cycle runs every: **60 s**
- Band A (21): SPY, QQQ, GLD, IWM, AAPL, MSFT, NVDA, AMZN, META, GOOGL, TSLA, AMD, JPM, XOM, AVGO, NFLX, LLY, UNH, V, MA, COST: **scanned**
- Band B (20): ORCL, CRM, ADBE, INTC, QCOM, MU, BAC, WFC, GS, HD, WMT, KO, PEP, DIS, BA, CAT, CVX, PFE, NKE, MRK: **scanned**
- Band C (20): PLTR, COIN, UBER, SHOP, SNOW, SOFI, HOOD, RIVN, MARA, ROKU, DKNG, CRWD, PANW, ARM, SMCI, AFRM, NET, DDOG, ABNB, RBLX: **scanned**
- Of those, only the top of each band is watched and traded, ranked by the desk's own vote and re-ranked every 15 min: **top 5 per band**
- Market opens: **09:30**
- No new trades after: **15:45**
- Everything still open is sold at: **15:59**
- A general setup (no named strategy) needs this many analysts voting its way at ±0.25: **2**
- A general setup: no short below this RSI, no long above: **25 / 75**
- No more entries after this many losing trades in a day: **off**
- No new general setup after (named strategies keep their own windows): **off**
- Paper account arms itself at the open: **yes**

- Five analysts each read the symbol and vote a score from -1 (strongly bearish) to +1 (strongly bullish).
- The chief (CMIO) combines the votes and decides whether there is enough agreement to act at all.
- The risk manager places the stop and target and sizes the position, then checks it against the account limits.
- If the trading day is armed, the order goes to the paper broker. A paper account arms itself at the open.
- The outcome tracker watches the price and sells at the stop, the target, the time stop or the square-off, whichever comes first.

## The analysts (who votes)

An analyst with no data this cycle (no option chain, no news in the window) abstains rather than voting zero, so it neither helps nor hurts. The weights are how much each vote counts in the combined score.

### Candlestick & technicals
- Weight: weight 1.0
- Reads:
  - EMA stack on the primary timeframe: 9 > 21 > 50 is +0.25, the reverse is -0.25.
  - Price above VWAP is +0.15, below is -0.15.
  - Candlestick patterns (engulfing, hammer, shooting star, morning/evening star, tweezer bottom/top, double rejection bottom/top, inside bar, flag…), each adding its own weight. A double rejection is the same high or low rejected twice, 2–8 bars apart, with a real pullback between.
  - Higher timeframes agreeing adds up to ±0.2. Disagreement is flagged as a conflict.
  - Volume above 1.8x the average confirms the move. Thin volume adds nothing.
  - RSI overbought pulls a long down (-0.12). Oversold pulls a short down.
  - Names the structural level that would prove the idea wrong. The stop is placed beyond it.

### Volume profile
- Weight: weight 1.0
- Reads:
  - Builds each session's volume by price (regular hours): the POC, the 70% value area (VAL–VAH), and low/high volume nodes, for the prior and the current session.
  - Value Area Rejection: a failed poke above the VAH closing back inside (put), or a held VAL test with a bullish candle (call); target the POC.
  - LVN Pocket Acceleration: a close from a shelf into a volume pocket on RVOL ≥ 1.5x — price crosses thin volume fast; target the pocket's far edge.
  - POC Bounce: back to the POC after a 1.0-ATR move away, rejected there.
  - Can lead a trade, and names its own structural stop.
  - On every trade: an entry at a VAL/POC (long) or VAH/POC (short) adds 0.3 to the composite; a thick volume node straight ahead subtracts 0.3 within 1.0 ATR and vetoes within 0.25 ATR.

### Options & futures
- Weight: weight 1.0
- Reads:
  - Abstains when the option chain is simulated (no feed served one), instead of voting on invented open interest.
  - Open-interest build-up is the main read, ±0.35: a fresh long or short build-up (short covering counts for less, 0.22).
  - Put/call ratio above 1.2 is bullish, below 0.7 is bearish (±0.2).
  - Pull towards max pain, IV skew and OI walls add small amounts.
  - Picks the option leg to trade when the symbol has options: ATM, 3–7 days to expiry.

### News
- Weight: weight 0.8
- Reads:
  - Scores recent headlines for this symbol, newer ones counting more.
  - News polarity beyond ±0.6 against the trade is an absolute veto — nothing overrides it (on).

### Macro & flows
- Weight: weight 0.7
- Reads:
  - The session backdrop: global indices and futures, VIX, dollar, yields, crude, and FII/DII cash flows in India.

### Fundamental filter
- Weight: veto only
- Reads:
  - Never votes a direction. A stock that fails the quality screen (for example, debt/equity too high) is vetoed whatever the others say. Indices and ETFs skip it.


## The vote (when there is enough agreement)

This is the gate most setups stop at. Both numbers must be met together. The paper profile is active on entries (one strong price-based analyst is enough) and strict on risk.

- Analysts agreeing, at least (each voting ±0.25 or stronger in the trade's direction; the fundamental filter does not count): **1**
- Combined weighted score, at least (±): **0.25**
- When analysts disagree: **the chart wins against news or macro, but never trades against the options & futures read; with no real chain the chart may trade alone**
- An analyst scoring under this has nothing to say, and is left out of the average rather than watering it down: **0.1**
- No counter-trend trades: no long below VWAP or into a falling 15-minute trend, no short above VWAP or into a rising one — except a volume-profile setup in the trade's direction (a call at the VAL is counter to the move by design): **on**
- At least one of these must agree (news or macro alone never trades): **candlestick, derivatives, volume_profile**
- 2-analyst quorum: the candlestick trigger at ±0.35 or stronger AND one of volume_profile, derivatives, macro_flow, news_sentiment at ±0.25 the same way: **off (backtest: it would have left 5 of 4,085 India and 0 of 3,687 US setups)**
- Macro must agree with the direction: **no**
- The language model may change or veto the vote (it still writes the journal and the weekly coach either way): **no — the rules decide**
- News blackout: no new entries this many minutes before and after a macro release (FOMC and other scheduled events, or a fresh headline naming one): **15 / 15 min**
- High-impact news against the trade vetoes it: **yes**

## Strategy: Previous Day Liquidity Sweep (failed breakout)

Stops rest just beyond yesterday's high and low. When price runs them and fails, the breakout traders are trapped and their exit fuels the move back. US and India, intraday.

- The trigger: the latest candle pierces the previous-day high (PDH) or low (PDL) and CLOSES BACK INSIDE yesterday's range, on: **5m or 15m**
- PDH sweep → SHORT (a put) only if the sweep candle is a Shooting Star or a Bearish Engulfing: **confirmation**
- PDL sweep → LONG (a call) only if it is a Hammer or a Bullish Engulfing: **confirmation**
- The candlestick analyst's score for a confirmed sweep: **1.0**
- Stop: exactly this many ticks beyond the sweep candle's wick (₹0.05 ticks on NSE, $0.01 in the US) — no ATR widening: **2**
- Target: the day's VWAP, or this many R — whichever is further: **3.0R**
- No time stop: it runs to the target, the stop, or the intraday square-off: **15:59**
- The trend filter stands aside for it (a PDH sweep short sits above VWAP by design): **on**
- Also needs rising call (PDL) / put (PDH) open interest: **no**

## The gold desk (held 1-4 days): GLD

Gold is traded on its own rules: the shares (not options), held overnight, on Larry Williams' volatility breakout — the strongest result of every test (two years of hourly bars and the last 60 days of 5-minute bars all positive). No morning screener and no other strategy on it.

- Entry: the first 5m bar today whose high reaches today's open + this share of yesterday's range (a long; a short: the low reaches the open minus it): **0.5 x range**
- Only with the trend: yesterday's close against the close this many sessions before: **20 days**
- Stop: 2 ticks beyond today's open; target: **3.0R**
- Exit: the first later session that is in profit at the open (Williams' bail-out): **on**
- …else the square-off of this session (the entry day is 0): **4**

## RSI(2) Swing Book (Larry Connors, held 1-10 days) — US TRIAL from 5 Oct 2026

A separate paper book with its own capital and ledger, on daily bars: buy a stock in an uptrend after a sharp short dip, sell on the first bounce. Not the intraday desk's limits, circuit breaker or records, and nothing is sent to a broker. Backtest Jul 2017 - Oct 2026 on the US watchlist, with the 25% stop: 67% wins, +58.6% (2017-22) and +127.4% (2022-26), max drawdown 14.6%.

- Runs once a day from this time (market time) to the close, the live price standing in for today's close: **15:45**
- Its own paper capital: **4000**
- Positions at once (each 1/N of the book's equity): **5**
- Entry: the close above its N-day average…: **200 days**
- …and the 2-period RSI below (the lowest first): **5**
- Exit: the first close above the N-day average: **5 days**
- …or the close of this session after the entry: **10**
- Emergency stop below the entry (Connors uses none; a 5% stop cut the tested return, and 25% beat 15% in both halves): **25%**

## Strategy: Volatility Breakout (Larry Williams, 1987 World Cup)

Yesterday's range says how far price can travel today. Once it has moved a set share of that range away from today's open, the day's expansion is under way.

- The trigger: the FIRST closed 5m candle beyond today's open plus (long) or minus (short) this share of yesterday's range — a level crossed earlier is a chase, not taken: **0.5 x range**
- Price on the same side of VWAP, the 9 EMA over the 21 for a long (under for a short): **required**
- Entries from this long after the open, until: **30 min – 15:45**
- The candlestick analyst's score for it: **0.9**
- Stop: exactly this many ticks beyond today's open — back there and the expansion has failed: **2**
- Target, with no room check against the previous-day high/low and no time stop: **3.0R**

## Strategy: SJK 50-200 — 50 / 200 EMA pullback continuation (the user's)

A trial from 2 Oct 2026. Trade WITH the 200 EMA trend, after a pullback to the 50 EMA, on the break of the swing made before the pullback. One trade per swing point.

- Long: price above the slow EMA; short: below it: **200 EMA (5m close)**
- The pullback's low (long) / high (short) comes within this much of the fast EMA, or through it: **0.15% of the 50 EMA**
- ...without a single 5m close beyond the slow EMA: **required**
- The trigger: the FIRST 5m close through the swing high (long) / low (short) made before the pullback — once per swing: **first close only**
- A swing high / low: beyond this many bars on each side: **3**
- The pullback's swing at most this many 5m bars old: **36**
- Entries between (market time): **09:45 – 15:45**
- Stop: AT the pullback's swing low (long) / high (short): **0 tick(s) beyond**
- Target, sold there — and the reward:risk it is judged at (not the desk's 1:3): **1:2.5**
- Stop to breakeven at this R (0 = off), then trailed this far behind the best R (0 = off): **0R / 0R**
- The candlestick analyst's score for it: **0.9**

## Strategy: SJK 9-15-21 — the 9 / 15 / 21 EMA Master (the user's, v2)

A trial. Trade the fan-out of three EMAs — 9 (purple), 15 (blue), 21 (grey) — in order AND all sloping the same way; stand aside when CHOPPY/SIDEWAYS. At most one breakout and one pullback trade per alignment cycle.

- Long: 9 > 15 > 21, all rising; short: 21 > 15 > 9, all falling: **9 / 15 / 21**
- CHOPPY when |EMA9 - EMA21| is under this x ATR(14): **0.35**
- ...or the 9 and 21 cross this often in the last N bars: **2 in 12**
- Trigger A — the bar turning into valid alignment, closing in the top part of its range (bottom for a short): **60% of the range**
- Trigger B — a pullback into the ribbon (low to the 15 EMA, close above the 21) closing up above the 9 EMA: **on**
- Triggers in use: **both**
- Entries between (market time): **09:45 – 15:45**
- Stop: min(lowest low of the last 5 bars, EMA 21) minus this x ATR (the mirror for a short): **0.2**
- Target, sold there: **1:2.0**

## Strategy: SJK 9/21 · VWAP · ADX (the user's confluence model, v2)

A trial. The 9 EMA crossing the 21, confirmed by the NEXT candle (else the setup expires), with a rising, directional ADX and the price on VWAP's side but not over-extended from it.

- The confirmation candle closes beyond the 9 EMA, the 21 EMA and the session VWAP (reset each day), the trade's way: **required**
- ADX above this AND rising, with the trade's DI leading: **20.0 (ADX 14)**
- No chasing: the close at most this x ATR(14) from VWAP: **2.5**
- CHOPPY: ADX at or under the threshold, |EMA9 - EMA21| under this x ATR, or VWAP between the two EMAs: **0.25**
- Entries between (market time): **09:45 – 15:45**
- Stop: beyond the crossover and confirmation candles by 2 ticks, at least this x ATR away: **1.0**
- Target 1, sold there (the backtest also runs the half-and-runner variant): **1:2.0**

## Strategy: sjk912RSi — 9/21 EMA + RSI crossover (the user's, v2)

A trial; the same rules for TradingView in pine/sjk912RSi.pine. One trade per crossover cycle.

- Buy within this many bars of the 9 EMA crossing above the 21 (still above); sell the mirror: **3**
- The candle closes beyond both EMAs, its low (high) clear of the 21 EMA, and closes the trade's way: **required**
- RSI band — longs / shorts: **50.0–68.0 / 32.0–50.0**
- Not before this many bars into the session: **5**
- Entries between (market time): **09:45 – 15:45**
- Stop: the lowest low (highest high) of 5 bars, at least this x ATR away: **0.5**
- Target, sold there: **1:2.0**

## Previous-day F&O confluence

Each symbol's previous session is mapped every day: high (PDH), low (PDL), close (PDC), call and put open interest and their change since the previous close, and the build-up — Long Buildup (price up, OI up), Short Buildup (down, up), Short Covering (up, down), Long Unwinding (down, down). It is stored for the review.

- A trade driven by a reversal pattern is taken only at the previous day's level: bullish after a sweep-and-reject of the PDL with call OI rising; bearish after a test-and-reject of the PDH with put OI rising: **on**
- By regime — rangebound (and volatile): the reversal's extreme within this of the PDL (longs) / PDH (shorts), closed back inside, with the OI rule below: **0.25%, in the last 3 bars**
- By regime — trending_up longs / trending_down shorts: no PDH/PDL needed; the pattern must form on a pullback within this of the intraday VWAP, the session POC or the 9/20 EMA, closing back on the trend side (no OI condition). Against the trend: the rangebound rule: **0.3%**
- When the chain has no real open interest: **block**
- The target needs open road: refused when the previous-day high (long) or low (short) sits inside the 1:3.0 target: **on**
- Target snapping: with less than 1:3.0 of room but at least this much, the trade is approved and the target snapped 2 ticks inside the PDH (long) / PDL (short); less room is refused: **2.2R**

## What it buys, and the stop and target

The option leg the options analyst picked when the symbol has options and a chain is available, otherwise the stock itself. An index with no option leg is not traded, because an index cannot be bought at spot.

- Stop: beyond the level the candlestick analyst named, by this many ticks (a structural stop): **2**
- …if that level is on the wrong side, more than 5% away, or more than 5 ATR away, the stop is this many ATR instead: **1.5 × ATR(14)**
- …and never closer than the larger of these (a level inside a normal bar's range is hit by noise alone); the target is 3R from the widened stop and the size shrinks to keep the 1% risk: **1.5 × ATR or 0.75% of the price**
- Stop distance must be between: **0.15% and 3% of the price (×4 and ×12 for options)**
- Target: reward at least this many times the risk: **3.0 : 1**
- …and not more than (implausible targets are refused): **10.0 : 1**
- Options: expiry between (0–2 day options are never bought — intraday theta and an IV crush eat them): **3–7 days**
- Options: refuse when IV rank (today's IV in this symbol's own past-year range) is above — until 20 days of IV are recorded, when IV is over 1.5x the stock's realised volatility: **80.0**

## How much it buys

Size is set by risk: how much is lost if the stop is hit. The other caps can only make a position smaller, never larger.

- Account size — each market has its own (US in dollars, India in rupees): **$4,000**
- Quantity = capital × risk % ÷ (entry − stop), then trimmed by the caps below: **by the stop, not fixed**
- Risked per trade (entry to stop × quantity): **1% = $40**
- Hard ceiling on risk per trade: **2%**
- All open positions together, notional at most: **100% of capital × 4.0 leverage**
- Each position, notional at most (so one tight stop cannot crowd out the rest): **total ÷ max positions**
- Option premium per trade, at most (capital deployment cap): **20% = $800**
- Index flex: high-notional index ETFs (SPY, QQQ, DIA) may deploy up to: **25% = $1,000**
- Option bid-ask spread, at most: **7% of the mid**
- Option stop: on the UNDERLYING — 5m swing low/high over the last 6 bars ± 2 ticks, else 1.5x ATR; never the premium: **underlying**
- Open positions at once, at most: **2**
- Trades a day, at most (the over-trading throttle): **4**
- Anti-stacking: one position per stock; a stock that closes a trade goes on the cooldown blacklist for: **on, 60 min**
- A held symbol is not re-scanned for a new entry: the position manager runs its stop, target and pyramid adds. 'One position per symbol' blocks BASE entries only — pyramid adds are allowed: **on**
- Portfolio heat: total lost if EVERY open position hit its stop together, at most (a new trade takes what room is left): **4% = $160**
- Daily circuit breaker: at this loss (closed + open) every position is closed at market and the desk is LOCKED OUT for the rest of the calendar day — a restart does not clear it: **3% = $120**

## Today's watchlist: the pre-market screener (Band A / B)

Before the open, from YESTERDAY's daily bars across the index universe: ATR% (14-day ATR / close), RVOL (yesterday's volume / 20-day average), NR7, inside day, and where it closed in its range. Only the names it picks are traded that day — nothing off the list, and no list means no entries.

- Runs at (market clock): **09:00**
- Band A (top 5): ATR% ≥ 1.0, RVOL ≥ 1.2, and NR7 or an inside day; ranked by RVOL: **both sides**
- Band B (next 5): ATR% ≥ 1.0, RVOL ≥ 1.5, and a close in the top 20% of the range (longs only) or the bottom 20% (shorts only): **one side**
- Morning window: Band A entries only: **09:45–11:30**
- Band B promotion: a Band B name may enter in the morning window when its regime trends its way (trending_up long / trending_down short) and its composite is at least ± this: **0.85**
- Midday freeze: no new entries (the chop filter): **11:30–13:30**
- Afternoon window: Band A/B VWAP pullbacks only (trend side, within 0.5 ATR of VWAP): **13:30–15:45**
- Trades a day (open or closed), then no new signals: **4**
- Everything still open is squared off at: **15:59**

## The Standard Pyramid (Base-50-25)

Add to winners, never to losers. The base is sized at 1R (risk per trade / stop distance). After Level 1 the worst case is -0.5R; after Level 2 +0.75R is locked. An add is also refused when the desk is halted, past the entry cutoff or over the exposure cap — the stop still steps up. Results are measured in the BASE trade's R.

- No averaging down: any add, or any new order on a symbol already held, is refused while that position is in a drawdown: **always**
- Level 1: at +1R open profit, ADD 50% of the base quantity, and move the stop for the WHOLE position to the base entry price: **+1R → +50%**
- Level 2: at +2R open profit, ADD 25% of the base quantity, and move the stop for the WHOLE position to the Level 1 fill price: **+2R → +25%**
- One take-profit for the whole (175%) position, from the BASE entry; the average entry is re-blended after each add: **+3R**

## When it sells

Checked every cycle. The first of these to happen closes the trade.

- Stop hit: sold at the stop (a planned loss of 1R): **−1R**
- Target hit: sold at the target — the pyramid's single target, the whole position: **+3R from the base entry**
- Time stop, for Mean Reversion setups only: no bounce within: **30 min**
- Square-off: anything still open is sold at: **15:59**

## After the trade

Every closed trade is graded in the journal on process, not outcome: a winner that broke a rule is a bad trade, and a loser that honoured its stop is a good one. The agents' weights are nudged over time by how each analyst's votes turned out.
