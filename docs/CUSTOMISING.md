# Customising the system

This file is the answer to "I want to change X — where do I go?"

The design goal: **you should be able to describe a change in one sentence and
have it be a small, local edit.** If a change you want requires touching more
than two files, that's a design bug — open an issue.

---

## 1. Change a threshold

Everything tunable is in `config/settings.yaml`. Edit it, then:

```bash
curl -X POST http://127.0.0.1:8000/api/config/reload
```

No restart. The agent roster is rebuilt and the CMIO picks up new weights.

### The knobs that matter most

```yaml
risk:
  risk_per_trade_pct: 1.0      # ← the single most important number here
  min_risk_reward: 2.0         # reject anything paying less than 2:1
  max_daily_loss_pct: 3.0      # halt for the day
  max_open_positions: 3
  intraday_leverage: 1.0       # your broker's MIS multiplier

consensus:
  min_confirmations: 2         # raise to 3 for fewer, higher-quality signals
  min_composite_score: 0.35    # lower to 0.2 to see more marginal setups
  conflict_policy: flat        # flat | side_with_technicals | side_with_macro
```

**Getting no signals at all?** That is usually correct behaviour, not a bug —
most 5-minute windows contain no good trade. To confirm the pipeline is alive,
temporarily set `min_composite_score: 0.15` and `min_confirmations: 1`, watch
signals appear, then put them back.

---

## 2. Re-prompt an agent

Agent prompts live in `config/agents.yaml`, not in Python. The system prompt is
assembled from `role` + `goal` + `focus` + `rules`.

```yaml
candlestick:
  role: >
    You read multi-timeframe price action for Indian intraday instruments.
  focus:
    - "Multi-timeframe alignment (1m, 5m, 15m, daily)"
    - "Opening Range Breakout in the first 30 minutes"   # ← added
  rules:
    - "A breakout on below-average volume must be scored down, not up."
    - "Never take a counter-trend trade in the first 15 minutes."   # ← added
```

Reload, done. No code change.

---

## 3. Add an agent

**Step 1** — declare it in `config/agents.yaml`:

```yaml
delivery_volume:
  id: delivery_volume
  name: "Delivery Percentage Analyst"
  module: "app.agents.delivery_volume"
  enabled: true
  role: >
    You analyse delivery-to-traded-quantity ratios to separate genuine
    accumulation from intraday churn.
  goal: >
    Score whether the current move is backed by real delivery-based buying.
  rules:
    - "A price rise on low delivery percentage is speculative — score it down."
  max_tokens: 1500
```

**Step 2** — create `app/agents/delivery_volume.py`:

```python
from app.agents.base import BaseAgent
from app.core.models import AgentReport, Evidence, MarketContext
from app.core.registry import register_agent


@register_agent("delivery_volume")
class DeliveryVolumeAgent(BaseAgent):
    agent_id = "delivery_volume"

    def analyse_rules(self, ctx: MarketContext) -> AgentReport:
        # MUST NOT raise. If you can't judge, abstain:
        # (put your own numbers on ctx.indicators from a collector in app/data/)
        pct = (ctx.indicators or {}).get("delivery_pct")
        if pct is None:
            return AgentReport(agent_id=self.agent_id, symbol=ctx.symbol,
                               data_available=False,
                               rationale="No delivery data for this symbol.")
        score = 0.5 if pct > 60 else (-0.3 if pct < 30 else 0.0)
        return AgentReport(
            agent_id=self.agent_id, symbol=ctx.symbol,
            bias=self._bias_from_score(score), score=score, confidence=0.6,
            rationale=f"Delivery {pct:.0f}% — "
                      f"{'genuine accumulation' if score > 0 else 'intraday churn'}.",
            evidence=[Evidence(label="Delivery %", value=f"{pct:.0f}%")],
        )

    def llm_payload(self, ctx, baseline) -> str | None:
        return None   # deterministic is enough here
```

**Step 3** — give it a weight in `settings.yaml`:

```yaml
weights:
  delivery_volume: 0.6
```

Reload. It now votes, appears on the dashboard, and gets graded by the learning
loop like everyone else.

### The one rule for agents

`analyse_rules()` **must never raise and must never fake data**. If you can't
judge, return `data_available=False`. The CMIO treats that as abstention. An
agent that returns a confident 0.0 instead is actively harmful — it dilutes the
vote while looking like participation.

---

## 4. Add a broker

See the README's broker section. Implement `BrokerAdapter`, add
`@register_broker("name")`, set `BROKER=name`. The factory auto-discovers it and
falls back to paper if it can't authenticate.

An adapter whose SDK isn't installed is **skipped at import**, not crashed on —
that's why `pip install kiteconnect` is optional.

---

## 5. Add a data source

News sources are pure config — any RSS feed works:

```yaml
news:
  sources:
    - name: "NSE Announcements"
      type: rss
      url: "https://www.nseindia.com/api/..."
```

For a non-RSS source, add a collector in `app/data/` and call it from
`TradingEngine.run_cycle()` in `app/scheduler.py`.

---

## 6. Change the schedule

```yaml
system:
  cycle_seconds: 60            # how often the desk thinks
  premarket_scan_time: "08:45"
  no_new_entry_after: "15:00"
  square_off_time: "15:15"
```

Lower `cycle_seconds` for faster reaction and higher LLM cost. Below ~30s you'll
hit broker rate limits.

---

## 7. Swap LangGraph for CrewAI / AutoGen

Only `app/agents/graph.py` knows about LangGraph. It already contains an
equivalent built-in orchestrator (`_run_builtin`) used when LangGraph isn't
installed, which is your template: keep the same four node functions
(`_node_analysts`, `_node_cmio`, `_node_risk`, `_node_dispatch`) and rewire them
in whatever framework you prefer. Nothing else in the codebase imports LangGraph.

---

## 8. Tune the learning loop

```yaml
learning:
  enabled: true
  alpha: 0.08                      # learning rate — raise to adapt faster, and noisier
  min_samples_before_update: 15    # don't re-weight on a handful of trades
  weight_floor: 0.2
  weight_ceiling: 2.0
  recall_examples: 8               # past outcomes fed into agent prompts
```

Set `enabled: false` to freeze weights at whatever `weights:` says — useful when
you're changing something else and want the rest held still.

Reset the learned weights entirely:

```sql
-- sqlite3 data/runtime/panadhanam.db
DELETE FROM agent_performance;
```

---

## 9. Alerts to phone / Slack / Telegram

```yaml
alerts:
  telegram_bot_token: "123456:ABC..."
  telegram_chat_id: "987654321"
  webhook_url: "https://hooks.slack.com/services/..."
```

Alerting failures are logged and swallowed — a dead webhook never breaks a cycle.
