# Alpaca paper trading (US stocks)

**Yes — Alpaca is already built in.** You don't need TradingView.

Alpaca gives you a free paper-trading account with **real market data**, a full
REST API, and $100,000 of simulated money. It is the right choice for what you
want to do.

---

## Why not TradingView?

TradingView's paper trading is **browser-only — there is no public API**. You
cannot connect a program to it. It's excellent for charting by hand, but this
system could not place, track or grade a single trade through it.

Alpaca is built for exactly this. Free, no minimum, real data, and the adapter
is already written and tested.

---

## Step 1 — Create the account

Go to **https://alpaca.markets** → Sign up.

You need email verification. **You do not need to fund anything or complete
brokerage onboarding to use paper trading** — the paper account exists straight
away.

## Step 2 — Get your paper API keys

1. In the dashboard, switch the toggle to **Paper Trading** (top-left).
   This matters: live and paper have *different* keys.
2. Go to **Home** → find the **API Keys** panel on the right.
3. Click **Generate New Key**.
4. Copy both values immediately — **the secret is shown only once**.

You'll have:
- **API Key ID** — starts with `PK` for paper
- **Secret Key** — a long string

> A key starting with `PK` is a paper key. `AK` is live. If yours starts with
> `AK`, you generated it on the wrong toggle.

## Step 3 — Put them in `.env`

```bash
ACTIVE_MARKET=US
BROKER=alpaca

ALPACA_API_KEY=PK................
ALPACA_API_SECRET=................................
ALPACA_PAPER=true        # keep this true
ALPACA_FEED=iex          # free tier
```

**No package to install** — the adapter is plain REST over `httpx`.

## Step 4 — Verify

```bash
python run.py --check-broker
```

You want:

```
  Configured : alpaca
  Connected  : alpaca
  Account    : SIMULATOR (paper)
  equity     : $100,000.00
  Data:
    SPY          $   585.42   20 candles, 42 option legs
  VERDICT: alpaca is connected and serving data.
           You can practice safely with this account.
```

If it says **FELL BACK**, your keys are wrong — the system refuses to pretend
it connected.

---

## Step 5 — Let it actually place practice orders

By default the desk is **alert-only**: it tells you about setups but sends
nothing. To have it trade your paper account:

```bash
# .env
AUTO_PLACE_ORDERS=true
```

Restart the app after changing it, then press **Start trading day** each
morning — the flag says orders are *allowed*, arming says they are allowed
*today*.

That's all a paper account needs. **`TRADING_MODE=live` and
`ENABLE_LIVE_ORDERS` are not required and should stay off** — those guard
real money, and Alpaca's paper endpoint is a simulator.

You can also set `execution.auto_place_orders` in `config/settings.yaml`, but
that file is tracked by git, so your edit will collide with the next `git pull`.
`.env` is per-machine and untracked, which is where a decision about your own
account belongs. When both are set, `.env` wins.

The dashboard header will show `SIMULATOR (paper)`, and every order log line
says so.

---

## Going to real money later

Three things must change together, deliberately:

```bash
ALPACA_PAPER=false          # different endpoint
ALPACA_API_KEY=AK...        # different keys (regenerate on the Live toggle)
TRADING_MODE=live
ENABLE_LIVE_ORDERS=true
```

`--check-broker` will then print `*** REAL MONEY ***` instead of `SIMULATOR`.
If it doesn't say that, you're still on paper.

Don't do this until your journal shows a positive Mistake Cost trend over
weeks, not days.

---

## What you get

| | Available |
|---|---|
| US stocks & ETFs | ✅ Real-time quotes, OHLCV candles |
| Options chains | ✅ With OI, IV and Greeks |
| Order types | ✅ Market, limit; day orders |
| Positions & P&L | ✅ From the broker |
| Paper account | ✅ $100,000 simulated |

### Limits worth knowing

- **The free feed is IEX**, a partial view of the tape. Prices are real but
  volume is understated — which matters for the volume-surge checks in the
  technical agent. Set `ALPACA_FEED=sip` if you subscribe to the full tape.
- **Options data needs enabling** on your account (Account → Configuration →
  Options). Without it, chains come back empty and the derivatives analyst
  correctly abstains.
- **No Indian markets.** Alpaca is US-only. Switch to the 🇮🇳 India desk and
  it falls back to paper + Yahoo/NSE data automatically.
- Paper fills are simulated by Alpaca and are optimistic — they don't model
  real queue position or slippage.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `FELL BACK` in the check | Wrong keys, or paper keys used against the live endpoint |
| `403 forbidden` | Key/secret mismatch — regenerate both together |
| No option legs | Enable options trading in your Alpaca account settings |
| Quotes but no candles | Free IEX feed has gaps outside market hours; normal |
| Orders rejected | Check buying power; the paper account starts at $100k |
