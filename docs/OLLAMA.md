# Free local AI with Ollama

**There is no login, no signup, no account and no API key.** Ollama is a program
that runs on your PC and serves a model on `localhost`. Nothing leaves your
machine — your trades and your journal are never sent anywhere.

Total cost: ₹0 / $0, forever.

---

## Step 1 — Install Ollama

**Windows:** download and run the installer from **https://ollama.com/download**

It installs as a background service and starts automatically. Nothing else to configure.

*(macOS: same link. Linux: `curl -fsSL https://ollama.com/install.sh | sh`)*

### Check it installed

Open a **new** terminal (Git Bash, PowerShell or cmd):

```bash
ollama --version
```

If that prints a version, you're done with step 1. If it says "command not
found", close every terminal and open a fresh one — the installer adds Ollama to
your PATH and existing windows don't see it.

---

## Step 2 — Download a model

This is the one slow step: a few GB over your internet connection, once.

```bash
ollama pull qwen2.5:7b
```

**Which model?** Pick by your RAM — this is the only thing that matters:

| Your RAM | Model | Size | Notes |
|---|---|---|---|
| 8 GB | `qwen2.5:3b` | ~2 GB | Works, but weaker at structured output |
| **16 GB** | **`qwen2.5:7b`** | **~4.7 GB** | **Start here — the best balance** |
| 32 GB+ | `qwen2.5:14b` | ~9 GB | Noticeably better judgement |
| 32 GB+ | `llama3.1:8b` | ~4.7 GB | Good alternative if you prefer Llama |

I've defaulted the config to `qwen2.5:7b` because Qwen follows JSON schemas more
reliably than similarly-sized alternatives, and this system asks every agent for
structured output.

### Check the model is there

```bash
ollama list
```

---

## Step 3 — Point the system at it

Add these two lines to your `.env`:

```bash
LLM_PROVIDER=ollama
OLLAMA_MODEL=qwen2.5:7b
```

That's the whole configuration.

---

## Step 4 — Verify it works

```bash
python run.py --check-llm
```

You want:

```
  Provider : ollama
  Host     : http://127.0.0.1:11434
  Model    : qwen2.5:7b
  Installed: qwen2.5:7b

  Sending a test prompt (a local model may take a minute)…

  [OK] Round trip succeeded: 'pong'

  VERDICT: ollama (qwen2.5:7b, local) is working. Agents will use it.
```

Then restart the dashboard. The header badge changes from `RULE-BASED` to
`ollama (qwen2.5:7b, local)`.

---

## What you actually get

| | Rule engines only | + Ollama (free) | + Claude (paid) |
|---|---|---|---|
| Signals & risk | ✅ Full | ✅ Full | ✅ Full |
| Agent reasoning | Deterministic | Local judgement | Best judgement |
| Mistake Cards | Template per mistake | Written for your trade | Sharpest coaching |
| News reading | Keyword lexicon | Understands context | Best |
| Cost | Free | Free | Per token |
| Privacy | Total | Total | Sent to Anthropic |
| Speed | Instant | 10–60s per cycle | 3–10s |

**The risk manager never uses an LLM in any configuration.** Position sizing and
stops stay deterministic Python whichever provider you pick — that is
deliberate, and the reason no prompt can talk the system into a bigger position.

---

## Honest expectations

A 7B model on your PC is **not** Claude. Specifically:

- It is **slower**. Expect 10–60 seconds per analysis cycle on CPU, much faster
  with a GPU. Raise `system.cycle_seconds` in `config/settings.yaml` to 120 or
  180 so cycles don't queue up.
- It **sometimes fumbles the schema**. The system sends the JSON Schema with
  every request to constrain it, validates what comes back, and retries once
  with the error fed back. If it still fails, that agent falls back to its rule
  engine for that cycle. You lose a little judgement, never correctness.
- Its **market reasoning is shallower**. It will not spot the subtle conflict
  between an OI buildup and a macro print the way Claude does.

For learning your own discipline — which is what the journal is for — a local
model is genuinely good enough. The Mistake Card's *detection* is arithmetic
anyway; the model only writes the explanation.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `Ollama is not running` | Run `ollama serve` in a terminal, or restart your PC (it's a service) |
| `no model called 'x'` | `ollama pull qwen2.5:7b` — the error names the exact command |
| Very slow | Normal on CPU. Use a smaller model or raise `cycle_seconds` |
| `could not produce schema-valid output` | Your model is too small. Try `qwen2.5:14b` |
| Out of memory | Use `qwen2.5:3b`, or close other applications |

Run `python run.py --check-llm` after any change — it tells you exactly what's
wrong rather than making you guess.

---

## Switching back to Claude

```bash
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
```

Both providers implement the same interface, so nothing else changes. You can
switch freely — use Ollama while practising, Claude when it matters.
