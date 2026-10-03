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

### Keeping both desks up to date on their own

    ./auto_update.sh                   # leave it running in its own Git Bash window

When it starts, it does four things:
- starts Ollama (`ollama serve`) if it is installed but not running, and
  pulls the model (`OLLAMA_MODEL`, default `qwen2.5:7b`) only if it is
  missing;
- starts panadhanam (:8000) if it is not already running;
- opens its dashboard in the browser, this first time only;
- checks every 30 minutes that Ollama is still running.

Every 30 minutes (`--interval 900` for 15) it checks this branch on
GitHub:
- **Nothing new:** it does nothing.
- **New commits:** it runs `git pull`, then restarts the desk on the new
  code without opening another browser tab. The dashboard already open
  reconnects by itself, and the desk restores the day's trade count, open
  positions and any lockout.
- **A pull that fails** (for example a locally edited file): the desk keeps
  the code it has, and it tries again next time.

It only reads from GitHub: nothing is pushed, and nothing on this machine is
opened to the internet. Its own log is in `logs/auto_update.log`, and the
desk writes to `logs/panadhanam.log`. Closing its window stops the desk it
started.

**panaoptions** (the options desk, :8100) has its own repository since
2 Oct 2026: https://github.com/panasandy147-sketch/panaoptions. Clone it
beside this folder, run its `./setup.sh` once, and start it with its own
`./start.sh`; `git pull` in that folder updates it.

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

## Paper trading

Leave the app running with a broker configured and it trades on real prices
with simulated fills. It **arms itself when the session opens**, so you do not
have to be at the screen; **Start trading day** is there for the mornings you
want to start it by hand, or to restart after a Stop.

A real-money account is never auto-armed, and no setting permits it.

A few minutes after square-off the **Today** panel fills in by itself — signals,
trades, win rate, total R, P&L, and what the desk was waiting for on a day it
took nothing. Every trade is journalled as it closes, and the **Weekly Review**
panel builds the week from them. It also shows the audit log day by day: every
buy and sell with its reason, Monday to today. The week so far is saved to
`journal/weekly/<week>-<us|in>.md` after every session, so US and India each
have their own file.

Every buy in the day's audit (`journal/audit/<us|in>/YYYY-MM-DD.md`) carries a
**trade card**:
- the last `audit.card_candles` (6) closed candles with OHLCV, the trigger bar marked;
- PDH, PDL, PDC, the recent swing range and the PD sweep, if one fired;
- VWAP, the EMAs, ATR, RSI, volume against its average, the regime, and the
  patterns found on each timeframe;
- the stop and target in R, with the underlying's stop in ATR;
- the sizing arithmetic against the risk cap;
- the vote and the stop-and-target rules (with the strategy's own section when
  a PD sweep led), each with its live value.

The exit adds the result in R.

**The gold desk (held 1–4 days).** GOLDBEES (Nippon India gold ETF) in India,
GLD (SPDR Gold) in the US.
- **What it trades:** the shares, held overnight, on Larry Williams'
  volatility breakout.
- **Entry:** the first 5m bar whose high reaches today's open + 0.5 ×
  yesterday's range, with the 20-day trend.
- **Stop and target:** stop 2 ticks beyond today's open; target 3R.
- **Exit:** the first later session that is in profit at the open (Williams'
  bail-out), else the stop or target, else the square-off of the 4th session.
- **Exempt:** no morning screener (gold's ~1% day never passes the 2% ATR
  floor) and no other strategy.
- **Evidence** (two years of hourly bars, then the last 60 days of 5-minute
  bars):

  | Instrument | First year | Second year | Last 60 days |
  |---|---|---|---|
  | GOLDBEES | +0.43R | +0.58R | +0.37R |
  | GLD | +0.24R | +0.21R | +0.30R |

  Gold rose through 2025–26, which flatters a breakout system. Check it with
  panaoptions' `python run.py --swing-backtest` (its own repository).

**SJK 50-200 — 50 / 200 EMA pullback (the user's strategy).** Both markets, a trial
from 2 Oct 2026 (`sjk50_200:` in `config/settings.yaml`; `app/strategies/sjk50_200.py`).
- **Long:** price above the 200 EMA (5m close); a pullback whose low comes
  within 0.15% of the 50 EMA without a close below the 200; then the
  **first** 5m close above the swing high made before the pullback.
- **Short:** the mirror.
- **Stop:** AT the pullback's swing. **Target:** 1:2.5 (`sjk50_200.rr`), sold
  there, and judged at 1:2.5 rather than the desk's 1:3.
- **Options:** breakeven at +1.5R and a trail (`sjk50_200.breakeven_r`,
  `sjk50_200.trail_r`), both off by default. One trade per swing point.

**Volatility Breakout (Larry Williams, 1987 World Cup).** US only, intraday.
- **Trigger:** the first closed 5m candle beyond today's open ± 0.5 × yesterday's
  range, with price on the same side of VWAP and the 9 EMA over (or under) the 21.
- **Stop:** 2 ticks beyond today's open. **Target:** 3R, with no room check
  and no time stop.
- **Why the US only:** on the 20-session walk-forward (to 30 Sept 2026), the
  breakout plus a 1.0% screener ATR floor took the US from +2.83% to +5.26%
  in the earlier ten sessions and from +1.04% to +4.73% in the judged ten,
  with the same drawdown. No setting beat India's rules in both halves.
  Switch it with `vol_breakout.enabled` in `config/markets/<market>.yaml`.

**Previous Day Liquidity Sweep (failed breakout).** US and India, intraday.
- **Trigger:** a 5m or 15m candle pierces the previous-day high or low and
  closes back inside yesterday's range.
- **Confirmation:** a PDH sweep is a short only if that candle is a Shooting
  Star or Bearish Engulfing. A PDL sweep is a long only if it is a Hammer or
  Bullish Engulfing. A confirmed sweep scores **1.0** with the candlestick
  analyst.
- **Stop:** exactly 2 ticks beyond the sweep candle's wick.
- **Target:** VWAP or 3R, whichever is further.
- **Exit:** no time stop. It runs to the target, the stop, or the square-off.
- **Filters:** the trend filter stands aside for it.

The journal grades it as its own setup, "PD Liquidity Sweep".

**Previous-day F&O and 1:3.** Each symbol's previous session is mapped
every cycle and stored once a day (`fno_daily`):
- **Levels:** high (PDH), low (PDL) and close (PDC).
- **Open interest:** call and put open interest, and its change since the
  previous close.
- **Build-up:** Long Buildup, Short Buildup, Short Covering or Long
  Unwinding.

A trade driven by a reversal pattern (tweezer bottom or top, double
rejection, hammer, engulfing, star) depends on the tape's regime
(`fno_confluence.regime_rules`, since 30 Sept 2026):
- **Rangebound (or volatile), or against the trend:** only at the previous
  day's level — within **0.25%** of it (`rangebound_proximity_pct`):
  - **Bullish:** after the PDL is swept and rejected, with call open
    interest rising.
  - **Bearish:** after the PDH is tested and rejected, with put open
    interest rising.

  With no real chain, `fno_confluence.when_oi_unknown: block` refuses it.
- **With the trend** (a long in `trending_up`, a short in `trending_down`):
  no PDH/PDL needed. The pattern must form on a pullback within **0.30%**
  (`trend_pullback_pct`) of the intraday VWAP, the session POC or the 9/20
  EMA, with the close back on the trend side.

The target is set at **1:3** (`risk.min_reward_risk`). When the previous-day
high (long) or low (short) sits inside it:
- **2.2R to 3R of room** (`risk.target_snap`): the trade is taken, with the
  target snapped 2 ticks inside that level.
- **Under 2.2R:** refused.

A symbol already held is not re-scanned for a new entry
(`system.skip_held_symbols`): the position manager runs its stop, target and
Standard Pyramid adds. "One position per symbol" stops new BASE entries
only; pyramid adds (+50% at +1R, +25% at +2R) go ahead, never onto a loser.

**Band B promotion** (`screener.band_b_promotion`): a Band B name may enter
in the morning window when its regime trends its way and its composite score
is ±0.85 or stronger.

See [docs/PAPER-TRADING.md](docs/PAPER-TRADING.md).

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
| **Volume Profile** | Session volume by price (RTH): POC, 70% value area, low/high volume nodes, prior + current session | Value Area Rejection, LVN Pocket Acceleration or POC Bounce — score + its structural stop |
| **Options & Futures (F&O)** | Live option chain: OI buildup, PCR, Max Pain, IV, Greeks | Score + the exact strike to trade |
| **News & Sentiment** | RSS from Moneycontrol, ET, Mint, Business Standard, Google News | Impact score with time decay |
| **Macro & FII/DII Flow** | GIFT Nifty, US futures, Brent, DXY, USD/INR, India VIX | Session risk appetite |
| **Fundamental Filter** | ROE, P/E vs industry, EPS, profit growth, D/E, liquidity | Pass / fail eligibility (a **veto**, never a direction) |
| **CMIO** | Every report above | One bias + confirmations + the strongest counter-argument. Every trade also gets the volume-profile check: +0.30 at a VAL/POC (long) or VAH/POC (short); a thick HVN straight ahead costs 0.30, or vetoes within 0.25 ATR |
| **Risk Manager** | The CMIO's candidate | Sized, validated signal — or a rejection with reasons |

Every agent works two ways: a **deterministic rule engine** (always runs) and an
optional **Claude pass** that reviews the rule engine's verdict and can overrule
it. When the two disagree sharply, confidence is cut rather than one being
blindly trusted.

---

## Risk controls (enforced, not suggested)

| Control | Default | Where |
|---|---|---|
| Account | Per market: US $4,000, India ₹3,50,000 — so following the session into India never sizes on "4,000 rupees" | `TOTAL_CAPITAL_US` / `TOTAL_CAPITAL_IN` in .env, or Capital → edit on that market; India's default in `config/markets/india.yaml` |
| Audit log | every buy and sell with its reasons, per market | `journal/audit/us/`, `journal/audit/in/`; day records `journal/daily/<date>-<market>-record.md` |
| Risk per trade | 1% of capital (hard ceiling 2%; one contract may use the ceiling) | `risk.risk_per_trade_pct` |
| Position size | `floor(risk_budget / stop_points)`, lot-rounded **down** | `risk.py` |
| Minimum R:R | 1:1.5, rejected below | `risk.min_risk_reward` |
| Daily circuit breaker | 10% ($400), realised + open → flat, halted, day disarmed | `risk.max_daily_loss_pct` |
| Anti-stacking cooldown | a symbol that closes a trade is blacklisted for 60 min | `risk.reentry_cooldown_minutes` |
| Option premium per trade | 20% ($800); **25% ($1,000) on SPY/QQQ/DIA** | `risk.max_capital_deployed_pct` / `index_max_capital_deployed_pct` |
| Option spread | refused above 7% of mid | `risk.max_spread_pct_of_mid` |
| Option stop | on the **underlying**: 5m swing low/high ± 2 ticks, else 1.5× ATR — never the premium | `risk.swing_lookback_bars`, `atr_stop_multiplier` |
| Stop floor | never closer than max(1.5× ATR, 0.75% of the price); the target is 3R from the widened stop and the size shrinks to keep 1% risk | `risk.min_stop_atr`, `min_stop_pct` |
| 2-analyst quorum | candlestick ±0.35 AND one of volume profile / derivatives / macro / news ±0.25 the same way — built, **off** (it would have left 5 of 4,085 India setups, 0 of 3,687 US) | `consensus.quorum.enabled` |
| News veto | polarity beyond ±0.60 against the trade = absolute veto | `consensus.news_veto_polarity` |
| Max open positions | 5 | `risk.max_open_positions` |
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
| **Alpaca** (US) | `alpaca` | API key | Free paper account, real data, $100k simulated. See [docs/ALPACA.md](docs/ALPACA.md) |

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

**The Friday Ollama review** (`scripts/ollama_feedback.py`). After Friday's
close — or on the next start, if the desk was off — the week's graded trades
(GOOD_WIN / GOOD_LOSS / BAD_WIN / BAD_LOSS, discipline score, R, and which
analysts voted for or against each) go to the local Ollama model as JSON. Its
answer is parsed defensively, bounded to ±0.15 a week within 0.25–1.5, and
written to `config/strategy_weights.json` (git-ignored); the CMIO multiplies
those into the vote weights above. Every run is recorded in
`journal/reflections/<week>.json`. Run it by hand with
`python run.py --feedback` (or `python -m scripts.ollama_feedback --dry-run`).
Delete `config/strategy_weights.json` to go back to the shipped weights.

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

```bash
# .env  — all three, and .env is untracked so a git pull cannot undo them
AUTO_PLACE_ORDERS=true
TRADING_MODE=live
ENABLE_LIVE_ORDERS=true
```

Plus a fourth, taken fresh each morning: press **Start trading day**. Arming
lasts for that one session and expires at square-off, so a flag left on from
last week can never trade today's market on its own.

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

### Starting it

| You are in | Run |
|---|---|
| Git Bash (MINGW64) | `./start.sh` |
| cmd / PowerShell / File Explorer | `start.bat` (or double-click it) |
| macOS / Linux | `./start.sh` |

Either one pulls, installs, runs the three checks, starts the server and opens
your browser. The leading `./` matters in bash: it does not search the current
directory, so a bare `start.sh` is *"command not found"*.

To run pieces by hand, always use the virtual environment's Python
(`.venv\Scripts\python.exe` on Windows, `.venv/bin/python` elsewhere). A bare
`python` on Windows finds the Microsoft Store build, which has none of this
project's packages — that is what `No module named 'pydantic'` means.

```bash
python run.py                                  # server + dashboard
python run.py --cycle                          # one analysis cycle, print, exit
python run.py --premarket                      # fundamental + macro scan
python run.py --size 100000 1 24500 24400 75   # position sizing calculator
python run.py --check-broker                   # is my broker connected? real money?
python run.py --check-llm                      # is my AI working?
python run.py --check-data                     # am I getting REAL prices?
python run.py --set TOTAL_CAPITAL=10000        # change a setting in .env
python -m scripts.backtest --symbol RELIANCE   # replay the rules over history
                                               # (the dashboard's Replay panel
                                               #  does this for the whole list)
make test                                      # the full suite
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
