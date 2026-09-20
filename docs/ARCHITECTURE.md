# Architecture notes

Why the system is shaped the way it is. Read this before making structural
changes.

---

## The data flow

```
scheduler.py  ─ every `cycle_seconds` during market hours
      │
      ├─ NewsCollector.fetch()     ─┐  fetched ONCE per cycle,
      ├─ MacroCollector.fetch()    ─┘  shared across all symbols
      │
      └─ for each symbol:
             MarketDataService.build_context()
                 ├─ broker.get_quote / get_candles / get_option_chain
                 ├─ indicators.ta.compute_all()       → EMA, VWAP, RSI, ATR, regime
                 ├─ indicators.patterns.scan()        → candlestick + structure
                 ├─ indicators.derivatives.analyse()  → PCR, Max Pain, OI buildup
                 └─ feedback.recall_for(symbol)       → past graded outcomes
                          │
                          ▼
             TradingDesk.run_cycle()  (LangGraph)
                 ├─ analysts   (parallel fan-out)
                 ├─ cmio       (synthesis + conflict resolution + vetoes)
                 ├─ risk_desk  (sizing + validation)   ← only if CMIO proceeds
                 └─ dispatcher (alert / order)         ← only if risk approves
                          │
                          ▼
             storage.db  →  OutcomeTracker  →  FeedbackLoop  →  new weights
```

---

## Five design decisions

### 1. The Risk Manager is not an agent

`app/agents/risk.py` has no LLM call and no prompt. It is arithmetic against
config. This is the single most important decision in the codebase.

An LLM sizing positions is an LLM that can be argued into a bigger one — by a
persuasive upstream agent, a prompt injection in a news headline, or its own
overconfidence. Risk logic must be the kind of thing you can read in one sitting
and unit-test exhaustively. It is, and it is (see `tests/test_risk.py`).

### 2. Abstention is not agreement

Every `AgentReport` carries `data_available`. When false, the CMIO excludes the
agent from the weighted vote entirely rather than counting a 0.0 score.

Counting a silent agent as neutral is a real failure mode: with four agents where
three have no data, a single mildly-positive score becomes "the desk is bullish".
The `abstained` list is surfaced in the decision and shown on the dashboard.

### 3. Two brains per agent

Each agent has a deterministic `analyse_rules()` and an optional Claude pass.

- The rule engine always runs, so the system works with no API key, and there is
  always a baseline to compare against.
- Claude sees the rule engine's verdict and is explicitly invited to overrule it.
- When the two disagree by more than 1.0 on a −1..+1 scale, confidence is
  multiplied by 0.6 and the disagreement is written into the rationale.

Neither brain is trusted unconditionally.

### 4. The registry is the extension seam

`app/core/registry.py` holds decorator-based registries for agents, brokers and
feeds, plus `autodiscover()` which imports every module in those packages so the
decorators run.

Consequences worth knowing:

- Adding a broker touches exactly one new file.
- A broker whose SDK isn't installed fails to import and is **skipped with an
  info log**, not fatal. That's why `kiteconnect` is optional.
- Agents listed in `agents.yaml` with no registered class log a warning and are
  skipped, rather than crashing the desk.

### 5. Config over code

Thresholds, prompts, weights and the watchlist are YAML. `Config.get()` takes
dotted paths with defaults, so a missing key degrades rather than raises, and
`POST /api/config/reload` re-reads everything and rebuilds the roster live.

Environment variables override YAML, so secrets and per-machine tuning stay out
of git.

---

## The paper broker is a real component

`app/brokers/paper.py` is not a stub. It generates a coherent synthetic market:
regime-persistent random walks, a U-shaped intraday volume smile, and a full
option chain with a volatility smile and OI concentrated near ATM and round
strikes.

**Its key invariant:** `self.price` is the single source of truth for a symbol.
History is generated *backward* from it and rescaled so the last close always
equals the current price. An earlier version walked the price forward on each
call, so the 5m chart, the quote and the option chain each ended up at a
different "spot" — the chart said 9,687 while the chain was priced at 23,265.
The rescale shape makes that class of bug impossible.

Index aliases (`NIFTY 50` ↔ `NIFTY`) are normalised through `canonical()` so seed
price, strike step and chain all agree on one instrument.

---

## Concurrency

- Analysts run concurrently via `asyncio.gather` with `return_exceptions=True`.
  A crashing agent becomes an abstaining report; it never takes the cycle down.
- Symbols are processed **sequentially** — deliberate. Brokers rate-limit hard,
  and a 13-symbol parallel burst gets you throttled or banned.
- Blocking broker SDKs (Kite, SmartAPI) are wrapped in `asyncio.to_thread`.
- SQLite uses WAL with thread-local connections.

---

## The event bus

`app/core/bus.py` is in-process pub/sub with a 300-event replay buffer, so a
dashboard opened mid-session immediately sees recent history instead of a blank
screen.

It is deliberately interface-compatible with Redis pub/sub: to scale to multiple
processes, reimplement `publish`/`subscribe` against Redis and change nothing
else.

---

## What is deliberately NOT here

- **No auto-trading by default.** Three switches, all off.
- **No tick-level data.** This is a 1-minute-and-slower system. HFT needs a
  different architecture entirely.
- **No portfolio optimisation.** Fixed fractional risk per trade, by design.
- **No proper backtester.** `scripts/backtest.py` is a threshold smoke test, and
  says so. A real backtest needs survivorship-bias-free data, realistic slippage
  and corporate-action handling.
- **No authentication on the API.** It binds to `127.0.0.1`. If you expose it,
  put it behind a reverse proxy with auth — and think hard about why you are
  exposing a trading system to the internet.
