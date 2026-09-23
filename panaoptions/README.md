# panaoptions

Intraday US **options** paper trading on a small account. A separate app in the
same repository as `panadhanam`, sharing no code with it: different
instruments, different risk model, different session rules.

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
run.py --report 30                      # the paper-trading record
```

No API key. Market data comes from Yahoo's public endpoints over plain
`httpx` — one less package to install than `yfinance`.

## The three strategies

Every entry is tagged with the strategy that produced it, so the journal can
answer *which of these actually pays* rather than lumping them together. All
three skip **09:30–09:45**, where spreads are widest and the first prints are
noise.

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

Windows, volume multiples and enable flags are all in
`config/settings.yaml` under `strategies:`.

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

### The weekend review

**Weekly review → Build this week's review** gives you the week with the
question that matters: *which of the three strategies is actually working.*
Per-strategy win rate, P&L and average discipline, the money lost specifically
to rule breaks (clean losses excluded — those are the cost of an edge), and a
coach's read.

With Ollama running (`journal.use_llm: true`) the coach's prose is the model's.
Without it you get the deterministic version — the same numbers, read honestly.
Either way the model **never decides a verdict, a score, a stop or a size**;
those stay deterministic, because a model that has read a profitable trade is
very good at finding reasons it was fine.

It writes itself to `journal/weekly/` once Friday's session closes.

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
    engine/  strategies.py levels.py   the three entry rules
    journal/ grade.py weekly.py        the learning loop
    web/     server.py static/    the dashboard (read-only)
    data/    feed.py premarket.py greeks.py
    engine/  indicators.py patterns.py contracts.py
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
