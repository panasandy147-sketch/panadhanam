# Markets: India and the US

The desk runs one market at a time. Switch with the toggle in the top-left of
the dashboard; the accent colour and page tint change with it so a glance tells
you which desk you're on — saffron for India, blue for the US.

---

## What actually changes when you switch

Everything below is read from `config/markets/<code>.yaml`. Nothing is
hardcoded in Python.

| | 🇮🇳 India | 🇺🇸 United States |
|---|---|---|
| Session | 09:15–15:30 **IST** | 09:30–16:00 **ET** |
| No new entries after | 15:00 | 15:30 |
| Square-off | 15:15 | 15:45 |
| Currency | ₹ INR, lakh/crore grouping (`1,00,000`) | $ USD, thousands (`100,000`) |
| Universe | NIFTY, BANKNIFTY, FINNIFTY + 10 F&O stocks | SPY, QQQ, IWM + 10 large caps |
| Equity sizing | shares | shares |
| **Option sizing** | **exchange lots** (NIFTY = 75) | **100× contract multiplier** |
| Weekly expiry | **Thursday** | **Friday** |
| Strike ladder | NIFTY 50, BANKNIFTY 100 | SPY/QQQ $1, MSFT $5 |
| Option symbol | `NIFTY26SEP24500CE` | `SPY260925C00585000` (OCC) |
| News | Moneycontrol, ET, Mint, Business Standard | CNBC, MarketWatch, Yahoo Finance |
| Macro dashboard | GIFT Nifty, Brent, DXY, USD/INR, India VIX | ES, NQ, 10Y yield, VIX, WTI, Gold |
| VIX panic level | 20 | 25 |
| Brokers | paper, Zerodha, Upstox, Angel One | paper, **Alpaca** |
| Yahoo ticker | `RELIANCE.NS` | `AAPL` |

### The clock is the market's, not your PC's

Session checks run in the **market's** timezone. A machine in Mumbai watching US
hours correctly sees the session open at 19:00 IST, not 09:30 IST. The badge
next to the market switch shows the market's local time and session phase.

### Lots vs contracts — the one that bites

This is a genuine structural difference, not cosmetics:

- **India**: one NIFTY option *lot* is 75 units. You cannot buy 40. With
  ₹1,00,000 and a 50-point stop, the risk budget sizes to ~20 units — which is
  **zero lots**, so the trade is correctly refused.
- **US**: one option *contract* controls 100 shares, but equities trade in
  single shares. A $100,000 account can buy 99 shares of QQQ without difficulty.

A practical consequence: **on a small account the US market is more accessible
than Indian index F&O**, because you can take a position in single shares.

The dashboard labels quantity accordingly — `99 shares` for QQQ, `2 (2 lots)`
for FINNIFTY. It will never print "99 lots" for 99 shares, which would imply a
position 100× larger than you hold.

---

## Connecting a US broker

[Alpaca](https://app.alpaca.markets) is the easiest: free paper account with
real market data, no minimum.

```bash
# .env
ACTIVE_MARKET=US
BROKER=alpaca
ALPACA_API_KEY=...
ALPACA_API_SECRET=...
ALPACA_PAPER=true       # set false only when you mean real money
ALPACA_FEED=iex         # free tier; use sip if you pay for the full tape
```

No extra package needed — the adapter is plain REST over `httpx`.

**On the free IEX feed** you get real quotes and bars, but from a partial view
of the tape rather than the consolidated SIP feed. Prices are real; volume is
understated. That matters for the volume-surge checks in the technical agent —
a breakout may look thinner than it was.

---

## Switching is refused while positions are open

The new market's broker cannot manage the old market's positions — Zerodha
cannot square off AAPL. So a switch with open positions is refused with an
explanation rather than silently orphaning the trade. Close or square off first.

Switching also clears cached fundamentals, the opportunity scan and the replay,
because every one of them belonged to the other market.

---

## Adding a third market

Copy a profile and edit it:

```bash
cp config/markets/us.yaml config/markets/uk.yaml
```

Set `market.code: "UK"`, the session times, timezone, currency, universe, news
feeds, macro tickers and expiry conventions. Restart.

It appears in the switcher automatically. The only Python you'd need is a broker
adapter, if none of the existing ones serve that market — see the README's
broker section.

The agents, risk manager, scanner and learning loop are market-agnostic: they
read conventions through the profile and never learn which market they're on.
