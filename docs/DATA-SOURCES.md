# Where the prices come from

The system uses **real market data by default**, from public sources that need
no API key and no signup.

---

## The feeds

| Source | Used for | Markets | Key needed |
|---|---|---|---|
| **NSE India** (`nseindia.com`) | Official option chain — real OI, OI change, IV per strike; index spot | 🇮🇳 India | No |
| **Yahoo Finance** | Quotes, OHLCV candles (1m/5m/15m/1d), US option chains, macro, fundamentals | Both | No |
| **Stooq** | Daily OHLCV only — the fallback when Yahoo is blocked or throttled | Both | No |

They're layered. For India the system asks NSE first (only the exchange serves
genuine Open Interest), then falls back to Yahoo for candles and quotes. For the
US, Yahoo serves everything.

### Why NSE matters for F&O

The **OI change** per strike is what separates a long buildup from short
covering. Yahoo doesn't publish it; the exchange does. Without it, PCR and
Max Pain still work, but the buildup classification is guesswork.

NSE blocks some networks and most non-Indian IPs. If it can't connect, the log
says so and the system falls back to Yahoo — you keep real prices, you lose the
OI-change read.

---

## Am I actually getting real data? Check it in five seconds

```bash
python run.py --check-data
```

It probes every feed, fetches a real quote and candle, and prints what came
back — or tells you plainly that nothing connected and the dashboard is showing
a synthetic market. Exit code 0 means real data, 1 means none.

```
  [OK ] Yahoo Finance: CONNECTED
        NIFTY 50      ₹   24,412.15  (+0.34%)   as of 2026-09-18T10:02
        RELIANCE      ₹    2,938.60  (-0.21%)   as of 2026-09-18T10:02
```

## Checking what you're actually looking at

**The banner at the top of the dashboard tells you, every time:**

> 🟢 **Real market data** via nse, yahoo · simulated fills (paper)

> 🔴 **These are NOT real market prices.** No data feed could connect…

The second one means every number on screen came from a random-walk generator.
It is not a degraded version of the market — it refers to nothing.

The opportunity board and the replay panel carry the same label independently,
so you can't read a stale one by accident.

---

## Market closed? You get the last traded price

That is the correct thing to show. When NSE or NYSE is shut, these endpoints
return the closing print and the day's OHLCV. Trend analysis, the opportunity
board and the replay all work on it.

What you **cannot** get outside market hours is a live intraday move — because
there isn't one. A weekend scan tells you how things closed and what the setups
looked like at the close, which is exactly what you want when planning Monday.

---

## Real data with simulated execution

The default configuration is:

```
prices     → real (NSE + Yahoo)
execution  → simulated (paper broker)
```

This is the right setup for evaluating the system. You see genuine market
behaviour; no order ever reaches a broker. The header shows
`PAPER / ALERT-ONLY` whenever this is the case.

To place real orders you still need a broker (Zerodha / Upstox / Angel One /
Alpaca) and the three explicit switches described in the README.

---

## Configuration

```yaml
# config/settings.yaml
data:
  use_real_data: true    # false only for offline tinkering
  use_nse: true          # India's official chain; falls back to Yahoo
  quote_cache_seconds: 20
```

Set `use_real_data: false` if you want the synthetic market back — for demos, or
for working on a plane.

---

## Limits worth knowing

- **Yahoo intraday history is capped.** 1-minute candles go back ~7 days,
  5/15-minute ~60 days. Daily goes back years. The replay respects this.
- **Yahoo is unofficial.** It is a public endpoint, not a supported API, and it
  can change or rate-limit. Treat it as good enough for analysis, not as
  infrastructure you'd build a fund on.
- **NSE rate-limits.** Responses are cached for 30 seconds and the session
  cookies are refreshed automatically.
- **No tick data anywhere.** The finest resolution is one minute. This system
  is not built for anything faster.
- **Corporate actions are not adjusted for** in intraday candles. A split or
  special dividend will look like a gap.

If a feed fails mid-session the agents receive no data and **abstain** — they
do not guess. That is why the `data_available` flag exists.
