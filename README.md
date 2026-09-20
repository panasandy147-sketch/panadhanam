# panadhanam — Multi-Agent Stock & F&O Trading Intelligence

A hedge-fund-style multi-agent pipeline for Indian equity and F&O intraday
analysis, with **deterministic, non-negotiable risk controls** and a real-time
dashboard.

It runs on your PC. It uses **real market data out of the box** — NSE India's
official option chain and Yahoo Finance, neither of which needs an API key —
with **simulated execution**, so nothing reaches a broker until you explicitly
turn that on. Claude-powered reasoning and live orders are each one environment
variable away.

> **This is analysis and decision-support software, not investment advice.**
> It ships in paper / alert-only mode. Placing real orders requires three
> separate switches to be turned on deliberately. Read [Going live](#going-live)
> before you risk money.

---

## Quick start

```bash
git clone https://github.com/panasandy147-sketch/panadhanam.git
cd panadhanam
bash setup.sh
```

Then start it with the line the setup script prints at the end, and open
**http://127.0.0.1:8000**.

`setup.sh` works in **Git Bash on Windows**, macOS and Linux. It finds your
Python, builds the virtualenv, installs everything and creates `.env`.

### On Windows

Git Bash (the `MINGW64` terminal) runs `bash setup.sh` fine.
From **cmd.exe or PowerShell** — or by just double-clicking it — use:

```
setup.bat
```

Windows puts virtualenv executables in `Scripts\`, not `bin/`, so afterwards run:

```bash
.venv/Scripts/python.exe run.py          # Git Bash
.venv\Scripts\python.exe run.py          # cmd / PowerShell
```

### Doing it by hand

```bash
python3 -m venv .venv                    # Windows: py -3 -m venv .venv
source .venv/bin/activate                # Git Bash:  source .venv/Scripts/activate
                                         # cmd:       .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
python run.py
```

### With `make`

`make` is **not** installed on Windows by default — that is what `setup.sh` is
for. If you do have it (macOS, Linux, or Windows via Chocolatey/scoop):

```bash
make install && make run
```

---

Whichever route you take, you'll see the full desk running against a synthetic
market: five analysts scoring, the CMIO synthesising, the Risk Manager sizing and
rejecting, and the dashboard updating live over a WebSocket. **No API keys
required.**

---

## Two markets: India and the US

Toggle between 🇮🇳 **India (NSE)** and 🇺🇸 **US (NYSE/NASDAQ)** from the top-left
of the dashboard. The theme changes with it — saffron for India, blue for the US
— so you always know which desk you're on.

Switching swaps the session hours and **timezone**, the currency and its number
grouping (`₹1,00,000` vs `$100,000`), the watchlist, the news feeds, the macro
dashboard, the strike ladders, the expiry weekday (Thursday vs Friday) and the
available brokers.

The structural difference that matters: **Indian options trade in exchange lots
(NIFTY = 75), US options use a 100× contract multiplier while equities trade in
single shares.** The risk manager handles both, so a ₹1,00,000 account correctly
refuses a NIFTY lot it cannot afford while a $100,000 account happily buys 99
shares of QQQ.

See **[docs/MARKETS.md](docs/MARKETS.md)** for the full comparison and how to add
a third market (one YAML file).

---

## Trade journal & post-mortem

Every completed trade gets a **Mistake Card**: graded 1-10 on *discipline, not
profit*, with the root cause and one measurable corrective rule.

The verdict is the point — **a winning trade that broke your rules is a BAD WIN**,
and it is the most dangerous outcome because it teaches you to break the rule
again. The engine detects a widened stop, a chased entry, an oversized position
or a blown time stop from the numbers alone, whether or not you own up to them.

Two metrics drive it: **R-multiple realisation** (P&L ÷ initial risk) and the
**Mistake Cost Index** — the money lost specifically to rule violations, with
clean losses excluded because those are the price of having an edge.

Cards are written to `journal/` as markdown and **committed to git**, so your
learning history outlives the database. See [docs/JOURNAL.md](docs/JOURNAL.md).

---

## Which trades should I take?

Click **Scan watchlist** on the dashboard. Every symbol is ranked into
**low / medium / high risk** buckets with the full trade laid out — entry, stop,
target, quantity, rupee risk — and every card says either **TRADEABLE** (cleared
every gate) or **WATCH** plus the exact reason it didn't.

Then **Replay last 5 sessions** shows what the rules would have caught recently,
graded in R-multiples, with its own limitations printed alongside.

**New here? [docs/BUTTONS.md](docs/BUTTONS.md) explains every control and when
to click it.** Then read
**[docs/HOW-TO-READ-IT.md](docs/HOW-TO-READ-IT.md) before acting on any of it.** It explains what the tiers mean (risk of a messy exit, not size of the
prize), why a WATCH is not a weak buy, and what the replay honestly cannot tell
you.

> **Check the banner at the top of the dashboard.** Green means real market
> data; red means the feeds could not connect and the prices are synthetic.
> See [docs/DATA-SOURCES.md](docs/DATA-SOURCES.md).

---

## The architecture

```
                    [ Chief Market Intelligence Officer (CMIO) ]
                                       │  synthesises, resolves conflicts
        ┌──────────────────────────────┴──────────────────────────────┐
        │                                                             │
[ Market News & Macro Lead ]                      [ Quantitative & Technical Lead ]
  ├── News Sentiment Analyst                        ├── Candlestick & Pattern Analyst
  ├── Geopolitical / Macro Risk Analyst             ├── Greeks & Open Interest Analyst
  └── FII / DII Flow Analyst                        └── Breakout & Volume Analyst
        │                                                             │
        └──────────────────────────────┬──────────────────────────────┘
                                       │
                            [ Execution & Risk Desk ]
                              ├── Risk Manager  (strict 1–2% SL, 1:2 R:R)
                              └── Dispatcher    (dashboard alert / broker order)
```

Orchestrated with **LangGraph**. Analysts fan out in parallel, the CMIO gates on
consensus, and the Risk Manager has the final word.

### The two rules that shape everything

**1. The Risk Manager is not an LLM.** It is plain Python arithmetic against
`config/settings.yaml` (`app/agents/risk.py`). No prompt can talk it into a
bigger position. Every limit is unit-tested.

**2. No data means abstain, never agree.** An analyst with no feed returns
`data_available=False` and is *excluded* from the vote. A silent agent must never
be counted as a confirming one — that is how naive ensembles manufacture false
confidence.

---

## What each agent does

| Agent | Reads | Produces |
|---|---|---|
| **Candlestick & Technical** | 1m/5m/15m/1d candles, EMA 9/21/50, VWAP, RSI, ATR, patterns, volume | Score −1..+1 + the price where the structure breaks |
| **Options & Futures (F&O)** | Live option chain: OI buildup, PCR, Max Pain, IV, Greeks | Score + the exact strike to trade |
| **News & Sentiment** | RSS from Moneycontrol, ET, Mint, Business Standard, Google News | Impact score with time decay |
| **Macro & FII/DII Flow** | GIFT Nifty, US futures, Brent, DXY, USD/INR, India VIX | Session risk appetite |
| **Fundamental Filter** | ROE, P/E vs industry, EPS, profit growth, D/E, liquidity | Pass / fail eligibility (a **veto**, never a direction) |
| **CMIO** | Every report above | One bias + confirmations + the strongest counter-argument |
| **Risk Manager** | The CMIO's candidate | Sized, validated signal — or a rejection with reasons |

Every agent works two ways: a **deterministic rule engine** (always runs) and an
optional **Claude pass** that reviews the rule engine's verdict and can overrule
it. When the two disagree sharply, confidence is cut rather than one being
blindly trusted.

---

## Risk controls (enforced, not suggested)

| Control | Default | Where |
|---|---|---|
| Risk per trade | 1% of capital (hard ceiling 2%) | `risk.risk_per_trade_pct` |
| Position size | `floor(risk_budget / stop_points)`, lot-rounded **down** | `risk.py` |
| Minimum R:R | 1:2, rejected below | `risk.min_risk_reward` |
| Daily loss limit | 3% → desk halts, manual resume only | `risk.max_daily_loss_pct` |
| Max open positions | 3 | `risk.max_open_positions` |
| Stop-loss sanity | rejected if too tight (noise) or too wide (ill-defined) | `risk.min/max_stop_distance_pct` |
| Exposure cap | 50% of capital × leverage | `risk.max_exposure_pct` |
| No late entries | no new positions after 15:00 | `system.no_new_entry_after` |
| Confirmations | ≥ 2 **independent** signals required | `consensus.min_confirmations` |

Capital caps **trim** position size rather than veto a good idea; the trade is
only rejected when not even one lot fits. Leverage widens how much you may
*hold* — never how much you may *lose*.

**Sizing identity**

```
risk_amount = capital × risk_pct / 100
stop_points = |entry − stop_loss|
quantity    = floor(risk_amount / stop_points)     → lot-rounded down for F&O
risk_reward = |target − entry| / stop_points       → must be ≥ 2.0
```

---

## Connecting a broker

Set `BROKER=` in `.env` and add that broker's credentials.

| Broker | `BROKER=` | Auth | Notes |
|---|---|---|---|
| **Paper** (default) | `paper` | none | Simulated market + fills. Zero setup. |
| **Zerodha Kite** | `zerodha` | daily login | `pip install kiteconnect`, then `python -m scripts.kite_login` |
| **Upstox** | `upstox` | daily login | `python -m scripts.upstox_login` |
| **Angel One** | `angelone` | fully automatic (TOTP) | `pip install smartapi-python pyotp` — no daily ritual |
| **Alpaca** (US) | `alpaca` | API key | Free paper account with real data. No SDK needed. |

**If a broker fails to authenticate the system falls back to paper mode and says
so loudly.** A failed login never becomes a live trade.

### Adding a broker not on the list

Implement 5 methods and add one decorator — nothing else in the codebase changes:

```python
# app/brokers/fyers.py
from app.brokers.base import BrokerAdapter, OrderResult
from app.core.registry import register_broker

@register_broker("fyers")
class FyersBroker(BrokerAdapter):
    name = "fyers"
    async def connect(self) -> bool: ...
    async def get_quote(self, symbol): ...
    async def get_candles(self, symbol, timeframe, count=200): ...
    async def get_option_chain(self, underlying, expiry=None): ...
    async def place_order(self, instrument, side, quantity, price, **kw): ...
```

Then `BROKER=fyers`. The registry auto-discovers it. An adapter whose SDK isn't
installed is skipped, not crashed on.

---

## Changing how it behaves

**Almost nothing is hardcoded.** These files are the intended editing surface,
and `POST /api/config/reload` applies changes without a restart:

| File | Controls |
|---|---|
| `config/settings.yaml` | every threshold: risk, consensus, indicators, IV limits, learning rate, risk-tier bands |
| `config/agents.yaml` | the agent roster, their **prompts**, focus areas and hard rules |
| `config/markets/*.yaml` | per-market: session, timezone, currency, universe, news, macro, expiry and strike conventions |
| `config/universe.yaml` | legacy single-market watchlist (market profiles take precedence) |

Some worked examples:

| You want | Change |
|---|---|
| Change the account size | Capital tile → `edit` on the dashboard, or `risk.total_capital` |
| Risk 0.5% instead of 1% | `risk.risk_per_trade_pct: 0.5` |
| Demand 1:3 reward | `risk.min_risk_reward: 3.0` |
| Require 3 confirmations | `consensus.min_confirmations: 3` |
| Trade only Bank Nifty | trim `config/universe.yaml` |
| Trust the F&O analyst more | `weights.derivatives: 1.5` |
| Reword an analyst's mandate | edit its `role` / `focus` / `rules` in `agents.yaml` |
| Use your broker's MIS margin | `risk.intraday_leverage: 5.0` |

### Adding a new agent

1. Add a block to `config/agents.yaml` (id, name, role, goal, focus, rules).
2. Create `app/agents/<id>.py`:

```python
@register_agent("twitter_sentiment")
class TwitterAgent(BaseAgent):
    agent_id = "twitter_sentiment"

    def analyse_rules(self, ctx) -> AgentReport:
        ...   # deterministic fallback — must never raise

    def llm_payload(self, ctx, baseline) -> str | None:
        ...   # optional: what to ask Claude
```

The graph picks it up on the next reload. It joins the vote, gets weighted by the
learning loop and appears on the dashboard automatically.

---

## How it learns

The system grades its own calls and re-weights the agents that make them.

1. Every approved signal is tracked to its stop, target, or square-off.
2. The realised **R-multiple** grades each agent that took a directional stance.
   Abstentions are neither rewarded nor punished; credit scales with how strongly
   the agent committed.
3. Weights update by EWMA, **separately per market regime** (`trending_up`,
   `trending_down`, `rangebound`, `volatile`) — so a setup that only works in
   trends isn't punished for a chop day.
4. Weights are clamped to `[0.2, 2.0]`: a hot streak can't let one agent
   dominate, a cold streak can't silence it.
5. Recent graded outcomes are fed back into agent prompts, so the Claude pass
   literally sees how its last calls on that symbol turned out.

Watch it on the dashboard's **Agent Scorecard**, or `GET /api/learning/scorecard`.

Tune in `settings.yaml` under `learning:` — set `enabled: false` to freeze weights.

---

## Enabling AI reasoning (free or paid)

Two options. **Both are optional** — the agents always have deterministic rule
engines, and the risk manager never uses an LLM in any configuration.

### Free: a model on your own PC

No login, no signup, no API key, no cost. See **[docs/OLLAMA.md](docs/OLLAMA.md)**.

```bash
# 1. install from https://ollama.com/download
ollama pull qwen2.5:7b
# 2. in .env:
#    LLM_PROVIDER=ollama
#    OLLAMA_MODEL=qwen2.5:7b
python run.py --check-llm
```

Slower and shallower than Claude, and entirely private — nothing leaves your
machine.

### Paid: Claude

```bash
# .env
ANTHROPIC_API_KEY=sk-ant-...
LLM_MODEL=claude-opus-5
LLM_EFFORT=medium        # low | medium | high | xhigh | max
```

Agents then return **schema-validated** verdicts (`client.messages.parse`), so a
malformed response is a caught exception, never a mis-parsed trade. Without a
key everything still runs on the rule engines — the dashboard tells you which
mode you're in.

Cost control: `LLM_EFFORT=low` and a shorter watchlist cut token spend sharply;
`system.cycle_seconds` controls how often the desk thinks.

Both providers implement the same interface, so you can switch at any time —
Ollama while practising, Claude when it matters.

---

## Going live

Real orders require **all three** switches. This friction is deliberate.

```yaml
# config/settings.yaml
execution:
  auto_place_orders: true
```
```bash
# .env
TRADING_MODE=live
ENABLE_LIVE_ORDERS=true
```

Before you do:

- Run in paper mode for **weeks**, not hours. Check the scorecard's hit rate.
- Run `python -m scripts.backtest --symbol RELIANCE` to sanity-check thresholds.
- Start with `risk_per_trade_pct: 0.5` and `max_open_positions: 1`.
- Confirm your lot sizes in `config/universe.yaml` — NSE revises them.
- Know that the daily halt is a floor, not a guarantee: gaps can exceed a stop.

---

## Command line

On Windows use `.venv/Scripts/python.exe` in place of `python` below
(or activate the venv first).

```bash
python run.py                                  # server + dashboard
python run.py --cycle                          # one analysis cycle, print, exit
python run.py --premarket                      # fundamental + macro scan
python run.py --size 100000 1 24500 24400 75   # position sizing calculator
python -m scripts.backtest --symbol RELIANCE   # replay the rules over history
                                               # (the dashboard's Replay panel
                                               #  does this for the whole list)
make test                                      # 60 tests
make lint
```

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/status` | engine, desk, risk state |
| `POST /api/cycle/run` | run a cycle on demand |
| `POST /api/config/reload` | apply YAML changes with no restart |
| `GET /api/signals` | signal history with rejection reasons |
| `POST /api/risk/calculate` | position sizing |
| `GET /api/markets` | active market, all profiles, market clock |
| `POST /api/markets/{code}` | switch the desk to IN or US |
| `GET /api/opportunities` | top N setups per risk tier (cached) |
| `POST /api/opportunities/scan` | force a fresh watchlist scan |
| `GET /api/replay?days=5` | replay the rules over recent sessions |
| `GET /api/learning/scorecard` | per-agent hit rate, avg R, weight |
| `GET /api/market/{symbol}/chain` | option chain + PCR / Max Pain / IV |
| `WS /ws` | live event stream |

Interactive docs at `/docs`.

## Docker

```bash
docker compose up --build      # → http://localhost:8000
```

`config/` and `data/` are mounted, so thresholds and trade history survive rebuilds.

---

## Project layout

```
app/
  agents/      candlestick, derivatives, news, macro, fundamental,
               cmio (synthesis), risk (deterministic), dispatcher, graph (LangGraph)
  brokers/     base ABC + paper, zerodha, upstox, angelone, alpaca + factory
config/markets/  per-market profiles (india.yaml, us.yaml)
  data/        market context builder, news RSS, macro feeds
  indicators/  ta.py, patterns.py, derivatives.py (Black-Scholes, PCR, Max Pain)
  analysis/    opportunity board (risk tiering), historical replay
  learning/    outcome tracking, EWMA agent re-weighting
  core/        config, market profiles, market clock, models, bus, registry
  api/         REST + WebSocket
config/        settings.yaml, agents.yaml, universe.yaml  ← your editing surface
dashboard/     single-page dashboard (no build step)
tests/         60 tests, risk logic covered hardest
```

## Troubleshooting

| Symptom | Cause |
|---|---|
| Red "NOT real market prices" banner | no data feed connected. Check internet/firewall and restart. Real data needs no key. |
| NSE unreachable, Yahoo works | NSE blocks many non-Indian IPs. You keep real prices but lose the per-strike OI-change read. |
| "No news / macro unavailable" | outbound HTTPS blocked. Agents correctly abstain. |
| Everything says NEUTRAL | working as intended — 2 confirmations + score ≥ 0.35 required. Lower `consensus.min_composite_score` to see more. |
| Low-risk tier is empty | also normal — it means nothing currently has aligned timeframes AND a clean stop AND cheap premium. Retune `opportunities.low_risk_max_points` if your tolerance differs. |
| Replay shows negative expectancy | on `BROKER=paper` that is expected: a random walk has no edge. On real data it means the configuration needs work — don't trade it. |
| "Position sizes to 0 lots/shares" | capital too small for that stop distance. The message names the capital you'd need. |
| Broker won't connect | check `.env`; Kite/Upstox tokens expire **daily**. |
| Market switch refused | you have open positions. The new broker can't manage them — close first. |
| US prices look thin | Alpaca's free IEX feed is a partial tape. Volume is understated; set `ALPACA_FEED=sip` if you pay for it. |
| Chart empty | re-add `dashboard/static/vendor-lightweight-charts.js`. |

## License & disclaimer

MIT. Provided as-is, with no warranty. Trading in equities and derivatives
carries substantial risk of loss. Nothing here is investment advice. You alone
are responsible for any orders this software places on your behalf.

TradingView Lightweight Charts™ is vendored under Apache-2.0.
