# panaoptions

Intraday **US options** paper trading on a small account. A separate app in the
same repository as `panadhanam`, sharing no code with it: different
instruments, different risk model, different session rules.

**US only, by design.** The session clock, the 15-minute opening range, the
pre-market window and the option chains are all built around 09:30–16:00 ET.
It is not a market toggle — for Indian equities and F&O use `panadhanam`,
which follows both markets.

**Nothing here can place a real order.** There is no broker adapter in this
package, by design.

```
Pre-Market News & Catalyst Screener      RVOL > 1.5, gap >= 1.0%
            |
Multi-Timeframe Candle & Volume Engine   5m signal, 15m trend confirmation
            |
Options Contract Filter                  delta / IV / DTE / spread / price
            |
Risk Management & Paper Execution        $500 rules, circuit breaker, ledger
```

---

## Read this first: what a $500 account can actually buy

The price cap is now a sanity ceiling ($2,000 a contract), not position
sizing — sizing belongs in the risk manager, and a price cap doing risk
management is a rule in the wrong place that fails silently.

But raising the cap does not by itself make the mega-cap universe tradeable.
0.45–0.60 delta means *at the money*, one contract is **100 shares**, and:

| Symbol | Spot | ATM contract, ~10 DTE |
|---|---|---|
| AAPL | ~230 | ~$380 |
| AMZN | ~225 | ~$450 |
| AMD | ~160 | ~$480 |
| SPY | ~570 | ~$530 |
| NVDA | ~180 | ~$600 |
| MSFT | ~425 | ~$620 |
| TSLA | ~420 | ~$1,530 |

At 20% deployment, $500 gives a **$100 budget**. So the refusal simply moves
from the contract filter to the risk manager. Two ways to resolve it:

**A. Raise the capital.** About **$2,000** puts every name in reach at the
20% rule; $1,600 reaches the cheapest.

```bash
../.venv/Scripts/python.exe run.py --set PANAOPTIONS_CAPITAL=2000
```

That writes `.env`, which is untracked and survives a restart. An environment
variable exported in a shell lasts only until that shell closes — set it that
way and it reverts on the next launch, taking the desk back to refusing every
trade with no visible change in configuration.

At $2,000 the desk trades, with one thing to know: a $400 budget buys **one**
AAPL contract, and half a contract does not exist — so the position closes
whole at +40% and the scale-out never engages. `--check-config` says so, and
names the capital (~$3,200) that funds two.

**B. Keep $500 and trade names it can afford.** Swap
`universe.small_account_alternative` into `universe.symbols` — liquid tickers
whose ATM contracts run $15–$100:

```yaml
universe:
  symbols: ["IWM", "PLTR", "INTC", "SOFI", "HOOD", "F", "XLF", "GDX"]
```

Either way, check before the open:

```bash
python run.py --check-config       # can every rule hold at once?
python run.py --explain-contracts  # live chains, per symbol
```

`--check-config` is the guard against this whole class of problem. Every
setting here is individually sensible; the failures come from *combinations* —
a delta band implying a price the budget forbids, a stop so tight the daily
limit trips on the first loser. Each one produces the same symptom, a desk that
scans all morning and takes nothing, which is indistinguishable from a quiet
market. So the arithmetic runs at startup and on demand, and every finding
names the setting, the number, and the fix.

## The other number worth being precise about

The brief says "risk 15–20% per trade". Two different things get called risk:

| | |
|---|---|
| Capital **deployed** | the premium paid — 20% of the account, **$100** |
| Capital **at risk** | deployed × the 20% stop — **$20**, or **4%** |

4% per trade is roughly four times what a conventional desk risks, and five
consecutive losers is a 20% drawdown. That is a deliberate choice, not a
hidden one: both numbers print on every signal and in `--status`.

---

## Setup

```bash
cd panaoptions
./start.sh                     # Git Bash / macOS / Linux
start.bat                      # cmd, PowerShell, or double-click
```

Either one finds the right Python, checks the configuration, verifies the data
feed, starts the desk and opens the dashboard at **http://127.0.0.1:8100** —
refusing to start if a rule makes trading impossible, rather than running all
morning and taking nothing.

Port 8100, not 8000: panadhanam's dashboard already owns 8000, and the two can
run side by side. The skin is amber rather than blue on purpose — two desks
that look alike is how you read an options position as an equities one.

`run.py --no-web` runs the desk with terminal output only.

**Use the virtual environment, not a bare `python`.** panadhanam's `.venv` one
level up already carries every package panaoptions needs; a bare `python` on
Windows finds the Microsoft Store build, which carries none of them. `run.py`
says so by name if you forget.

```bash
../.venv/Scripts/python.exe run.py --check-config    # Windows
../.venv/bin/python run.py --check-config            # macOS / Linux
```

The other commands, all through that same interpreter:

```bash
run.py --set PANAOPTIONS_CAPITAL=2000   # per-machine, survives a restart
run.py --check                          # is the data feed reachable?
run.py --screen                         # what passes the pre-market filter?
run.py --explain-contracts              # what your budget buys, live
run.py --check-llm                      # is the coach the model, or the rules?
run.py --report 30                      # the paper-trading record
```

No API key. Market data comes from Yahoo's public endpoints over plain
`httpx` — one less package to install than `yfinance`.

## Market data: Yahoo or Tradier

```yaml
data:
  provider: "yahoo"        # or "tradier"
  tradier_env: "sandbox"   # or "production"
```

```bash
python run.py --check                      # test the configured source
python run.py --provider tradier --check   # test the other one
```

| | **yahoo** | **cboe** | **tradier** |
|---|---|---|---|
| Signup | none | **none** | US brokerage account — SSN, phone |
| Charts | reliable | Yahoo's, which work | reliable |
| Option chains | different host; **often 401** | public delayed feed | first-class endpoint |
| Greeks | none — **estimated** here from IV | **from CBOE** | **from the exchange** |
| Delay | real-time-ish | ~15 min | 15 min sandbox, real-time funded |

**Start with `cboe`.** It needs no account of any kind, and it fixes the one
thing that is actually broken: Yahoo's chain endpoint. Charts stay on Yahoo,
which works.

```bash
python run.py --probe-sources
```

tries all three from your machine and tells you which answers — worth running
first, because the answer is genuinely machine-dependent. Yahoo's chain
endpoint returns 401 for some people and works for others, and a corporate
network or a country block can take out any of them.

The chain endpoint is the reason this choice exists. Yahoo serves options from
a different host and API path than charts, and that endpoint has become
unreliable: it can return `403` to every request while charts work perfectly,
which leaves the desk screening real prices, drawing real charts and firing
real setups — every one of which reports `no contract`. The dashboard now
carries a banner when that happens instead of letting it read as a quiet
market.

The greeks matter more than they sound. Every delta band in this config —
0.45–0.75 depending on the pattern — is a rule about a number. On Yahoo that
number is computed here from implied volatility, which is a reasonable
estimate and still an estimate. Tradier returns the delta the market is
actually using, so the bands mean what they say.

### Setting CBOE up

Nothing to sign up for, and no file to edit:

```bash
python run.py --set PANAOPTIONS_PROVIDER=cboe
python run.py --check
```

`--set` writes to `.env`, which survives a restart — an environment variable
exported in a shell lasts until that shell closes, which works once and then
silently reverts. `--probe-sources` prints this exact command for whichever
source it found working.

You can also set `data.provider` in `config/settings.yaml` if you would
rather have it in the shared file; the environment variable wins when both
are set, because which endpoint answers is a property of the machine. Charts continue to come from Yahoo; option
chains come from CBOE's public delayed-quotes feed, greeks included.

The endpoint is undocumented and could change. Every field is read
defensively and the parsing is tested against the recorded shape, so a change
fails a test and prints a clear message rather than producing a quiet day
with no trades. CBOE publishes chains only for the options it lists, so a
symbol it does not carry returns 404 and says so by name.

### Setting Tradier up

Worth knowing before you start: Tradier is a US broker, and their signup
opens a **brokerage account** — it asks for SSN, phone verification and the
rest of the KYC. That is a lot of identity to hand over in order to paper
trade, and `cboe` above needs none of it. Only go this way if you want
real-time data later.

1. Create an account at **developer.tradier.com** and generate an access
   token. (A funded brokerage account gives real-time data; the developer
   sandbox is free and serves delayed data with real chains.)
2. Put the token in `panaoptions/.env`:

   ```
   TRADIER_TOKEN=your-token-here
   ```

3. Set the provider:

   ```yaml
   data:
     provider: "tradier"
     tradier_env: "sandbox"
   ```

4. `python run.py --check` — it fetches a real chain and reports how many
   contracts carry a usable delta.

A sandbox token does not work against production, and the reverse is also
true; `--check` says so rather than reporting a generic connection failure.
If `TRADIER_TOKEN` is missing the desk falls back to Yahoo **with an error in
the log**, never silently — running on a different data source than you
intended is worse than not starting.

### Delayed data and the scalp profile

Sandbox data is delayed by about 15 minutes. For the **default** profile —
5m/15m bars, 14–45 day contracts — that is tolerable: the setups it trades
develop over hours and the paper fills stay roughly honest.

For the **scalp** profile it is not. A 1-minute desk buying same-day contracts
on prices from a quarter of an hour ago is not testing the strategy, it is
testing a fiction. Use real-time data for that profile or treat its results as
meaningless.

## The watchlist

The **Watchlist** box at the top of the dashboard takes comma-separated
tickers — `QQQ, SPY, AAPL, NVDA` — and every enabled strategy then runs
against every one of them, each cycle, taking paper trades whenever the rules
agree. Commas, spaces and newlines all work, because people paste from all
three. Press Enter or **Scan these**; **Reset to config** goes back to
`universe.symbols`.

The change applies on the next cycle, not the next restart, and it is saved to
`data/watchlist.json` so it survives one. The panel header says whether the
desk is running your list or the shipped one.

Two limits, both deliberate:

* **20 symbols.** Each one costs several feed requests every cycle. Past this
  the desk spends its time waiting on Yahoo rather than deciding, which looks
  exactly like being broken.
* **It chooses what to LOOK at, never what to buy.** Choosing what to watch
  and choosing what to trade are different powers, and only the first one
  belongs to whoever last used the dashboard. Every rule still has to agree
  before a position is opened, and dropping a symbol does not liquidate an
  open position in it — that trade was taken under rules that still apply and
  its exit is already defined.

### How often it checks

Every **60 seconds** by default, and all of your symbols **at the same
instant** — gathered concurrently, not fetched one after another. Sequentially,
five symbols is five round trips to Yahoo laid end to end, during which the
first symbol's chart goes stale while the last is still being read; a cycle
should judge one moment, not a smear of five. Change the interval with
`--interval`:

```bash
python run.py --profile scalp --interval 30
```

One cycle, in order: manage any open position → run the pre-market screen if
it is due → then, inside the entry window and only when there is room, judge
every symbol that passed, in turn, against every enabled strategy.

For `QQQ, SPY, AAPL` that is **3 data requests per cycle** in steady state
(one candle series per symbol), plus one option-chain request per open
position, plus 15 on the first cycle of the day when the screen runs. About
390 cycles in a session.

Two things about the cadence are worth knowing:

* **The interval is the period, not the gap.** The loop sleeps the remainder
  of the interval rather than the whole of it after finishing. Sleeping the
  full amount would make the real period `work + interval` — nearer 65
  seconds than 60 with three symbols — and the desk would drift a bar further
  behind every cycle while still claiming to run every minute. If a cycle
  ever overruns the interval it writes `slow.cycle` to the activity log and
  says to use fewer symbols or a longer interval, because a desk quietly
  running at half its stated rate looks exactly like one that is fine.
* **A full desk stops hunting.** While `risk.max_open_trades` positions are
  open, no symbol is scanned for new entries until one closes — the desk
  manages what it has instead. It logs `hunt.skip` saying so, and the Live
  Candidate panel clears rather than leaving the last symbol on screen as
  though it were still being judged.
* **A cycle fills every free slot.** If three symbols set up in the same
  minute and there is room for three, all three are taken in that cycle.
  Stopping after the first would make `max_open_trades` a limit the desk
  could only approach one cycle at a time, and the other two setups would
  simply be missed.

### Notifications

The activity-log toolbar has **Notify me when a trade is taken** — a desktop
notification the moment a paper trade opens or closes. It is off until you ask
for it, asks the browser's permission on the first tick, and says so plainly
if the browser has notifications blocked rather than leaving a ticked box that
does nothing.

Only opens and closes. Alerting on every setup considered would be a
notification every few seconds on a 1-minute desk across three symbols, and
would be switched off within the hour. Opening the page does not replay the
day as a burst of alerts — the first poll seeds what has already happened.

For alerts that reach you away from the machine, fill in
`notify.discord_webhook_url` or the Telegram pair in `config/settings.yaml`;
blank means no webhook call is ever made.

The dashboard prints the cadence under the entry window: the interval, how
many symbols, and how long the last cycle actually took.

### Watching several names at once

`risk.max_open_trades` is **3** on both profiles, so a watchlist is a desk
rather than a queue: every symbol is judged each cycle and up to three
positions can be live at once, in different names.

More than one open position exposes a rule that had been invisible:
`max_capital_deployed_pct` is a **per-trade** cap, so three trades at 20% each
satisfy it every time and deploy 60% of the account. There is now a ceiling on
the total as well — `risk.max_total_deployed_pct` — which falls back to the
per-trade budget when it is not set rather than to "no limit", so an upgrade
cannot silently remove it. The refusal names the number:

```
$2,000.00 is already at work across 2 position(s), and the ceiling is
30% of $10,000.00 ($3,000.00). One QQQ 741C costs $1,180.00 and there
is $1,000.00 of room.
```

And `--check-config` states what holding the maximum actually costs, because
"three positions" is an abstraction until it is a dollar figure and the
per-trade risk number everybody reads is for one trade:

```
[warning] risk.max_open_trades
    Up to 3 positions at once, $900 deployed (45% of the account). If all 3
    hit the 45% backstop together — and correlated names do — that is -$405,
    20% of the account. In practice the daily loss limit halts the desk at
    -$200 (10%) before it gets there.
```

Lower `risk.max_total_deployed_pct` to cap the total, or
`risk.max_open_trades` to hold fewer at once.

## Profiles: which desk are you running?

```bash
python run.py --profiles                        # what exists
python run.py --profile scalp --check-config    # what it changes
python run.py --profile scalp                   # run it
```

There are two desks in here, and they answer the same tape differently.

| | **default** | **scalp** |
|---|---|---|
| Bars | 5m, patterns on 15m | 1m, patterns on 5m |
| Expiries | 7–45 DTE | 0–1 DTE |
| Screen | gap ≥ 1%, RVOL ≥ 1.5 | no catalyst filter |
| Universe | 8 large caps | QQQ, SPY, IWM |
| Entries | 09:35–15:00 | 09:40–15:30 |
| Deployed per trade | 20% | 10% |
| Disaster stop | 45% | 35% |
| Max hold | no limit | 20 minutes |

**They are different machines, not settings.** The default desk is looking for
a catalyst — a 1% gap on real volume — and then holds a multi-week contract
through intraday noise. On a quiet index ETF grinding through a $3 range it
correctly does nothing, because the move it is built to trade is not there.

The scalp profile trades exactly that tape. A $1.10 push on QQQ is noise to
the first desk and the entire trade to the second.

A profile is deep-merged over `config/settings.yaml`, so anything it does not
mention keeps its default. An unknown profile name is an error rather than a
silent fall back to the default — running one desk while believing you
selected the other is the most expensive way this could fail — and the
profile in force is printed by `--check-config` and shown on the dashboard.

### If the desk took nothing today

Work down the gates in order; each one is visible somewhere:

0. **The clock.** Every strategy skips **09:30–09:45**, where spreads are
   widest and the first prints are noise, so nothing can trade before 09:45
   however many symbols passed the screen. The Strategies panel says which
   state each one is in — `live now`, `opens in 4 min`, `done for today` —
   rather than a flat "outside window", which is equally true at 09:41 and at
   16:30 and means the opposite thing. The activity log says it too.

   Note that a strategy's window is checked against the **last bar's** time,
   not the wall clock: the rule is about the candle being judged, and at
   09:46 with the last completed 5m bar at 09:40 the opening range is not
   final yet.
1. **The pre-market screen.** Nothing that fails it is ever looked at again.
   `python run.py --screen` shows every symbol with its gap, its RVOL and the
   reason. A quiet index ETF fails the default screen on most days by design:
   QQQ closing −0.89% is inside the ±1% threshold, so it is never scanned,
   and no strategy rule downstream ever runs.
2. **The bar size.** Patterns are read on 15m by default. A reversal on a
   1-minute chart is not visible to it and never will be — that is the scalp
   profile's job.
3. **The entry window.** Shown on the dashboard and in `--check-config`. It
   runs until the last enabled strategy shuts.
4. **The contract.** `--check-config` says whether the budget can buy what
   the rules ask for; `--explain-contracts` prices the live chain and lists
   every rejection with its reason.
5. **The activity log**, at the bottom of the dashboard. `hunt.skip`,
   `setup.pass`, `contract.none` and `risk.refused` each say which gate
   stopped the trade. A desk that is scanning and taking nothing and a desk
   that is hung look identical from an empty position list; this is the
   difference.

   It has two tiers, and on the scalp profile that matters. Scanning chatter
   runs at about thirteen events a minute — three symbols against four
   strategies, every cycle — so the live window holds roughly twenty minutes
   of a six-and-a-half-hour session. **Trades and decisions only** (ticked by
   default) reads a separate buffer that the chatter cannot evict, so a trade
   taken at 13:10 is still on screen at 15:30. Untick it to watch the desk
   scanning and confirm it is alive between trades.

### A note on 0-DTE

The scalp profile buys same-day contracts, which changes three things the
default profile's rules assume:

* **Theta stops being a slow leak.** A same-day contract loses its whole
  extrinsic value by the close whatever the underlying does, so a call that
  is correct but takes forty minutes to be right can still expire worthless.
  That is what `risk.max_hold_minutes` is for: a scalp that has not reached
  its first target in twenty minutes is wrong even though nothing has broken.
  It does not apply once the first target is banked, because the stop is at
  breakeven by then and the runner is the one position worth room.
* **Gamma runs the position.** The +120%-in-six-minutes moves are real. So is
  −100% in the same six minutes from the same size. The profile deploys half
  as much per trade and halts the day twice as early.
* **Published win rates are even less transferable.** Those figures are from
  daily bars. Applying them to a 1-minute chart on a 0-DTE contract is two
  extrapolations stacked, which is why the journal measures them here.

## The four strategies

Every entry is tagged with the strategy that produced it, so the journal can
answer *which of these actually pays* rather than lumping them together. They
all skip **09:30–09:45**, where spreads are widest and the first prints are
noise.

The desk hunts from `session.entry_open` until **the last enabled strategy
shuts** — not until `session.entry_close`, which is only the floor. Closing the
window earlier than a strategy's own window would leave that strategy enabled,
in window by its own reckoning, and never once asked. `--check-config` warns if
a window runs past the square-off, where it would be clipped.

### 1. Opening Range Breakout + VWAP — 09:45 to 11:00

A 5m close beyond the **15-minute opening range**, with price the right side
of VWAP, 9 EMA the right side of the 21, and volume ≥ 1.5× the 20-bar average.

*Invalidation:* a 5m close back inside the range. *Target:* 1.5× the range
height.

### 2. VWAP / 9-EMA Pullback — 10:00 to 13:30

15m chart in a stacked trend (price > 20 EMA > 50 EMA, or the inverse); on the
5m, price pulls back to the 9 EMA or VWAP and prints a rejection candle that
takes out the previous candle's high (or low).

*Invalidation:* a 5m close on the wrong side of VWAP.

### 3. Liquidity Sweep Reversal — 09:45 to 12:00

Price pokes through the **pre-market low**, takes the stops resting under it,
then reclaims the level on the next 5m close and crosses VWAP on high volume.
The trade is that the breakout buyers are trapped.

*Invalidation:* a 5m close back through the sweep wick.

### 4. Candlestick at a Key Level — 09:45 to 15:00

Thirteen reversal and continuation patterns on the **15m** chart.

*Single and two-candle:* Hammer, Bullish Engulfing, Morning Star, Tweezer
Bottom, Piercing Line for calls; Shooting Star, Bearish Engulfing, Evening
Star, Tweezer Top, Dark Cloud Cover for puts.

*Multi-candle structures:* Three-Line Strike, Three Black Crows, Three White
Soldiers, Abandoned Baby, and the Liquidity Sweep Rejection.

The pattern is the smaller half of the rule. Three gates have to clear:

1. **Location.** It must print at a level the market has already turned at — a
   swing high or low, the pre-market extreme, an opening-range boundary,
   yesterday's close, VWAP or a moving average. A hammer in the middle of a
   range is a bar with a wick, and trading it is how people conclude
   candlesticks do not work. "At the level" is measured in **ATR**
   (`level_tolerance_atr`, default 0.5) rather than percent, so it means the
   same thing on a quiet stock and a volatile one. The panel names the level
   it found; a pattern with no level is refused in writing.
2. **The trigger.** A pattern is not an entry. Price has to take out the
   pattern's trigger — the high of a hammer, the low of a shooting star —
   before anything is bought. That break happens on a *later* candle, so the
   pattern is allowed to be up to `trigger_within_bars` (2) back.
3. **Participation.** Volume on the pattern, against the 20-bar average.

*Invalidation:* a close back through the candle that made the signal — below
the hammer's low, under the morning star's low, above the shooting star's
high. The stop is that level on the **underlying**, not a percentage of the
premium.

#### The multi-candle structures

Six more patterns, all of which are defined by what they **interrupt** — three
long red candles after a rally is distribution, the same three mid-range is
noise with a story attached. Each one is refused in writing when the run-in
does not match (`trend_lookback`, default 10 bars).

| Pattern | Side | Delta | DTE | Stop anchor |
|---|---|---|---|---|
| Bullish / Bearish Three-Line Strike | CALL / PUT | 0.65–0.75 | 30–45 | Candle 4's low (high) |
| Three Black Crows | PUT | 0.55–0.65 | 21–35 | Candle 2's high |
| Three White Soldiers | CALL | 0.50–0.60 | 30–45 | Midpoint of candle 2 |
| Bullish / Bearish Abandoned Baby | CALL / PUT | 0.50–0.60 | 14–30 | The isolated doji's low (high) |
| Piercing Line / Dark Cloud Cover | CALL / PUT | 0.50–0.60 | 14–30 | The reversing candle's far wick |
| Liquidity Sweep Rejection | CALL / PUT | 0.55–0.65 | 14–30 | Beyond the tip of the sweep wick |

**Three-Line Strike** — three consecutive lower closes, then one wide candle
that opens at or below the third's low and closes above the *first* candle's
open. One session undoing three; everyone short through the run is offside at
once. Because candle 4 is wide, it asks for delta in the money and real time.

**Three Black Crows / Three White Soldiers** — three long bodies, each opening
inside the previous one's body and closing near its extreme. The near-the-low
test matters: three red closes with long lower wicks is buyers showing up
every session, which is the opposite of what the pattern claims, so it is
rejected.

**Abandoned Baby** — an island reversal. The gaps *are* the pattern: the doji
must not overlap the candle on either side. Without both gaps it is a Morning
Star, which is a weaker signal with a different stop, so the overlap test is
strict and the two are never logged as one.

**Piercing Line / Dark Cloud Cover** — a close back past the midpoint of the
previous body. The textbook asks the second candle to gap; intraday bars gap
only at the open, so a strict gap rule would make this fire once a day at
09:30 and never again. Opening beyond the previous close carries the same
meaning. A close past the *whole* body is an engulfing — a different pattern
with a different stop — and is reported as one.

**Liquidity Sweep Rejection** — not a separate detector. It is what a Hammer or
Shooting Star *is* when its wick pushes through the level and the candle closes
back inside: the stops resting beyond the level were filled first, so the trade
is that whoever got filled out there is now offside. It earns its own name, its
own contract and a stop beyond the wick tip rather than at the level.

*Contract:* each pattern asks for its own delta **and** its own expiry, because
a sharp reversal off a level and a four-candle structural turn are not the same
bet. A four-candle reversal is a multi-session move and dies on theta at 7
days; handing it the intraday default would buy the right thesis with the wrong
contract. The bands are in `config/settings.yaml` under
`strategies.candlestick_at_level.patterns`.

#### About those published win rates

The config records a `claimed_accuracy` for the patterns that have one —
0.84 for the Three-Line Strike, 0.78 for Three Black Crows, and so on. Three
things are true about those numbers and the app is built to keep all three
visible:

1. **They are measured on daily bars**, mostly on individual equities. This
   desk reads a **15-minute intraday tape**. A figure from one is a hypothesis
   about the other, not a result.
2. **The headline figure is usually a different question.** Bulkowski's ~84%
   for the three-line strike is how often it *reverses*, which is not the same
   as how often a trade on it pays after spread, slippage and theta. His own
   ranked performance tables put it well down the list.
3. **The figure is usually cited for the bearish pattern**, and mirrored onto
   the bullish one on the assumption that the market is symmetrical. It is not.

So the number is carried as a **claim, never as a fact**. Nothing in the risk
or sizing path reads it — there is a test that proves a 0.99 claim and a 0.00
claim size the identical position — and the journal prints it beside what the
pattern actually did here:

```
| Pattern                    | Trades | Yours | Published | Gap      | Total   |
| Bullish Three-Line Strike  | 12     | 50%   | 84%       | -34 pts  | -180.00 |
| Hammer                     | 4      | —     | —         | —        |  +60.00 |
```

`Yours` stays blank until there are at least 10 trades, because three trades
against an 84% claim is not evidence either way and printing "33%" beside it
invites exactly the wrong conclusion.

#### Can this account actually buy them?

Per-pattern contract selection has a trap in it: **the patterns with the best
published numbers ask for the most expensive contracts.** A 0.70-delta call at
45 DTE is a different instrument at a different price from the 0.50-delta
default, and on a small account the highest-conviction setup on the list is the
one most likely to fire and find nothing it can buy.

`run.py --check-config` prices every pattern's band against the universe and
says so:

```
[warning] strategies.candlestick_at_level.patterns
    15 of 17 patterns ask for a contract no name in the universe offers
    inside the $400 per-trade budget, so they can fire and never be filled
```

On a $2,000 account that is most of them, with the large-cap universe. Swapping
`universe.small_account_alternative` into `universe.symbols` clears it.

Windows, volume multiples and enable flags are all in
`config/settings.yaml` under `strategies:`.

## The live candidate panel

The dashboard's widest panel answers three questions the position list cannot:

* **Which name is being judged right now** — the header reads
  `LIVE CANDIDATE SCANNING <SYMBOL>`, so a working desk and a hung one look
  different even on a day that takes nothing.
* **Why this is a call or a put** — the case is written in plain language on
  the right: the pattern, the level it formed at, the trigger price, what
  would kill it, and the contract the pattern wants. That is the part you can
  learn from; a P&L number on its own teaches nothing.
* **What the chart actually looked like** — a live 5m candle chart of that
  symbol on the left, with 9/21/50 EMAs and VWAP, refreshed while the page is
  open and labelled with the age of the last bar.

A new signal **flashes the panel for 15 seconds** — green for a call, red for
a put — then settles back rather than leaving a tinted card behind. Anyone with
`prefers-reduced-motion` set gets one steady tint instead of a pulse.

The panel also says whether the desk **took** the trade or only saw it. "We saw
this" and "we bought this" are different claims and the panel does not blur
them. When it is taken, the fill is shown under the reasoning — entry, contract
and size — and it is paper, always: there is no broker adapter in this app.

## What "wrong" means: the underlying, not the premium

`risk.stop_mode: underlying` (the default) makes the **strategy's own
invalidation level** the exit. A fixed −20% on the contract is at the mercy of
an implied-volatility shift or a wide spread and says nothing about whether the
trade was wrong; if the thesis breaks the option is sold whether it is down 8%
or 22%.

The percentage stop survives as a **disaster backstop** (`disaster_stop_pct`,
45%) — wide enough that the underlying level normally fires first, but still
there for a gap or a collapse in the option itself.

## One contract cannot be halved

`risk.exit_style: auto` notices when a position is a single contract, where
"exit 50% at +40%" quietly becomes "exit everything at +40%" and the runner
never exists. For those it switches to:

1. **+35%** → stop moves to breakeven, and you get a notification
2. then **hold** until a 5m candle closes on the far side of the 9 EMA

No profit target at all: the exit is the trend ending, which is what lets one
contract still catch a runner. Two or more contracts scale out as before.

## What gets recorded

Every closed trade goes to `data/panaoptions.db` and `data/trades.csv` the
moment it closes — and so does **every setup that did not become a trade**,
with its reason. The rejected ones are the more useful half: they tell you
whether a rule is selective or simply impossible.

```bash
python run.py --report 30
```

With no trades yet, the report prints the rejection tally instead. If one
reason dominates, that is the rule to look at.

## The learning loop

Every closed trade is graded **the moment it closes**, on process rather than
outcome — a winning trade that broke a rule is a bad trade, and a losing trade
that honoured its invalidation is an acceptable one. Grading on P&L teaches the
opposite of what you want, because the market pays out on bad decisions often
enough to make them feel right.

Four verdicts, and the dangerous one is not the losses:

| Verdict | Meaning |
|---|---|
| `GOOD_WIN` | Paid, and followed the rules |
| `GOOD_LOSS` | Lost, and followed the rules — the cost of having an edge |
| `BAD_WIN` | **Paid while breaking a rule.** The P&L is reinforcing the habit that will eventually cost you |
| `BAD_LOSS` | Lost, and broke a rule |

Nine mistake tags, all detected from the ledger — nothing depends on you owning
up to anything: no strategy tag, held past the invalidation, stop not honoured,
exited early, oversized, entered outside the window, chased, traded while
halted, held to the forced close.

Each trade gets a **card** in `journal/cards/` as markdown, and the whole thing
is committed to git so the history outlives the database.

### Two reviews: daily and weekly

**Today's review** writes itself once the session closes, to `journal/daily/`.
It answers *how did I execute* — every trade, the rules broken, the money lost
to indiscipline, and a coach's read.

It deliberately **refuses to judge a strategy**. One session of two or three
trades says nothing about whether ORB beats the pullback, and letting a good
day read as proof is how a fluke becomes a rule. That question belongs to:

### The weekend review

**Weekly review → Build this week's review** gives you the week with the
question that matters: *which of the strategies is actually working.*
Per-strategy win rate, P&L and average discipline, the money lost specifically
to rule breaks (clean losses excluded — those are the cost of an edge), and a
coach's read.

`journal.use_llm` is **on by default** and safe to leave on: if Ollama is not
running the cards fall back to the rules-written version, the failure is logged
once rather than per trade, and the grading is unaffected.

That safety is exactly why it needs checking — "configured" and "working" look
identical otherwise:

```bash
python run.py --check-llm
```

It names the model, says which version you are getting, and on a failure prints
the command that fixes it (including the models you *do* have installed, if the
configured one is missing). The dashboard's **Learning** panel says the same
thing in its header: *cards by qwen2.5:7b* or *cards by the rules*.

Either way the model **never decides a verdict, a score, a stop or a size** —
those stay deterministic, because a model that has read a profitable trade is
very good at finding reasons it was fine.

It writes itself to `journal/weekly/` once Friday's session closes, and
compares the strategies head to head — which is the comparison that
needs a sample rather than a session.

Both panels have a **Build** button for looking before the close, and download
as `.md` or `.json`. Turn either off with `journal.auto_daily_review` /
`journal.auto_weekly_review`.

## The optional ML filter

```bash
pip install -r requirements-ml.txt
python run.py --train
```

XGBoost with **isotonic calibration** (the strategy gates on "probability above
65%", so the number has to actually mean 65%), trained **walk-forward** — each
test window strictly after the training window that produced it. A random split
would put Tuesday afternoon in training and Tuesday morning in test, and every
metric would be a fiction.

The label is a triple barrier: +1.5 ATR within 6 bars before −1.0 ATR. The path
is walked bar by bar, not compared against the window maximum, and a bar that
touches both barriers scores as a **loss**.

`--train` prints per-fold AUC and the precision at your threshold. **Read the
precision.** If it is not meaningfully above the base rate, the model has not
learned anything and should stay off.

Set `ml.enabled: true` to use it. It is a **veto** — it can stop a trade the
rules found, never start one they did not.

`shap` gives per-trade attribution; without it you get model-wide importance.

## Alerts

Set `DISCORD_WEBHOOK_URL`, or `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`, in
the environment. Blank means nothing is sent and nothing is attempted. A
webhook failure never interrupts a trading cycle.

## Layout

```
panaoptions/
  run.py                  every command
  config/settings.yaml    every rule
  panaoptions/
    clock.py              session windows, in New York time
    preflight.py          can every rule hold at once?
    engine/  strategies.py levels.py   the four entry rules
    engine/  patterns.py                the seven reversal patterns
    journal/ grade.py weekly.py        the learning loop
    web/     server.py static/    the dashboard (read-only)
    data/    feed.py premarket.py greeks.py
    engine/  indicators.py contracts.py
    risk/    guardrails.py
    ledger/  paper.py store.py
    notify/  webhook.py
    ml/      features.py labels.py train.py predict.py explain.py
  tests/
```

## Running the tests

```bash
cd panaoptions
python -m pytest -q
```
