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
feed, and starts the desk — refusing to start if a rule makes trading
impossible, rather than running all morning and taking nothing.

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

## The rules, as implemented

**Pre-market screen** — RVOL above 1.5 and a gap of at least ±1.0%. RVOL is
scaled by how much of the session has elapsed; an unscaled ratio calls every
morning quiet.

**Entry (calls)** — price above VWAP, 9 EMA above 20 EMA, a bullish engulfing
or hammer closing on above-average volume, and the 15-minute trend agreeing.
Puts are the mirror image.

**Contract** — 7–14 DTE, delta and price per the config, bid-ask spread ≤ 5%
of mid. Delta is computed with Black-Scholes from Yahoo's implied volatility,
because Yahoo does not serve greeks.

**Risk** — one trade at a time; 20% hard stop on the contract; scale 50% out at
+40% and move the stop to breakeven; +70% or a 9-EMA trail for the rest; a
daily circuit breaker at **10% of capital** that **latches** (winning it back
is exactly the impulse it exists to stop).

The breaker is a percentage, not a dollar figure. The brief states the rule as
"−$50, which is 10% of total capital" — $50 was the instance at $500, not the
rule. Pinned to dollars, raising capital to $2,000 would leave a limit smaller
than a single $80 stop-out, so the breaker would trip on the first loser and
the desk would quietly become one-trade-a-day.

**Session** — entries 09:35–10:30 ET only. Stops tighten to breakeven at
10:45. Everything is squared off by 15:45.

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
    data/    feed.py premarket.py greeks.py
    engine/  indicators.py patterns.py setups.py contracts.py
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
