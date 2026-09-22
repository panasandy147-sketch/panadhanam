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

## Read this first: the budget and the delta band cannot both hold

The specification asks for contracts at **0.45–0.60 delta** costing
**$60–$100**. Those two rules describe an empty set, and no amount of waiting
will produce a trade.

0.45–0.60 delta means *at the money*. One contract is **100 shares**. So an
at-the-money option 7–14 days out costs roughly:

| Symbol | Spot | ATM contract, ~10 DTE |
|---|---|---|
| SPY | ~570 | **~$530** |
| AAPL | ~230 | **~$380** |
| AMD | ~160 | **~$480** |
| NVDA | ~180 | **~$600** |
| TSLA | ~420 | **~$1,500** |

A $100 budget buys roughly **0.10–0.20 delta** — well out of the money.

The filter is implemented exactly as specified and will correctly return
nothing. What it will *not* do is return nothing silently: every rejection is
counted by cause, and the reason names the real price.

```bash
python run.py --explain-contracts     # live prices, per symbol, right now
```

Your three options, in the order we would recommend them:

1. **Raise `contracts.max_contract_price`** to what ATM actually costs and
   paper-trade the strategy as designed. You are learning the rules; the
   account size is the constraint, not the rules.
2. **Trade cheaper underlyings** where ATM fits $100.
3. **Lower `contracts.min_delta`** to ~0.15 and accept OTM lottery tickets.
   Theta and the spread will take most of the edge. Not recommended.

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
pip install -r requirements.txt
python run.py --check          # is the data feed reachable?
python run.py --screen         # what passes the pre-market filter?
python run.py --explain-contracts
python run.py                  # start the desk
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
−$50 daily circuit breaker that **latches** (winning it back is exactly the
impulse it exists to stop).

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
