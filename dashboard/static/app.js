/* ==========================================================================
   panadhanam dashboard client
   One WebSocket carries every live event; REST fills in history on load.
   ========================================================================== */
"use strict";

const AGENT_COLORS = {
  candlestick:    "var(--series-1)",
  derivatives:    "var(--series-2)",
  news_sentiment: "var(--series-3)",
  macro_flow:     "var(--series-4)",
  fundamental:    "var(--series-5)",
  cmio:           "var(--series-6)",
};
const AGENT_LABELS = {
  candlestick:    "Candlestick & Technical",
  derivatives:    "Options & Futures (F&O)",
  news_sentiment: "News & Sentiment",
  macro_flow:     "Macro & FII/DII Flow",
  fundamental:    "Fundamental Filter",
  cmio:           "Chief Market Intelligence Officer",
};

const $ = (id) => document.getElementById(id);

/* Number formatting follows the active market: India groups in lakh/crore
   (1,00,000) and prints ₹; the US groups in thousands (100,000) and prints $.
   Everything money-shaped goes through money(), so switching markets never
   leaves a stale currency symbol on screen. */
const locale = () => state.market?.currency?.locale || "en-IN";
const cur = () => state.market?.currency?.symbol || "₹";

const fmt = (n, d = 2) =>
  n === null || n === undefined || Number.isNaN(n)
    ? "—"
    : Number(n).toLocaleString(locale(), { minimumFractionDigits: d, maximumFractionDigits: d });
const fmtInt = (n) =>
  n === null || n === undefined ? "—" : Number(n).toLocaleString(locale());
const money = (n, d = 0) =>
  n === null || n === undefined || Number.isNaN(n)
    ? "—"
    : cur() + Number(n).toLocaleString(locale(),
        { minimumFractionDigits: d, maximumFractionDigits: d });
const signClass = (n) => (n > 0 ? "pos" : n < 0 ? "neg" : "neutral-ink");
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const state = {
  agents: {},          // agent_id -> latest report
  currentSymbol: null,
  signals: [],
  news: [],
  status: null,
  market: null,
  switching: false,
  chart: null,
  series: {},
  timeframe: "5m",
  paused: false,
};

/* ====================================================================== */
/* WebSocket                                                              */
/* ====================================================================== */
function connect() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${location.host}/ws`);

  ws.onopen = () => {
    $("conn-dot").className = "dot live pulse";
    $("conn-text").textContent = "live";
    $("conn").classList.add("ok");
  };
  ws.onclose = () => {
    $("conn-dot").className = "dot off";
    $("conn-text").textContent = "reconnecting";
    $("conn").classList.remove("ok");
    setTimeout(connect, 2500);
  };
  ws.onerror = () => ws.close();
  ws.onmessage = (ev) => {
    try { handle(JSON.parse(ev.data)); } catch (e) { console.warn("bad event", e); }
  };
}

function handle(event) {
  const { topic, data, ts } = event;
  logLine(topic, data, ts);

  switch (topic) {
    case "system.status":       applyStatus(data); break;
    case "cycle.start":         state.currentSymbol = data.symbol;
                                $("agent-symbol").textContent = data.symbol;
                                state.agents = {}; renderAgents(); break;
    case "agent.report":        state.agents[data.agent_id] = data; renderAgents(); break;
    case "cycle.done":          state.agents.cmio = {
                                  agent_id: "cmio", bias: data.bias,
                                  score: data.composite_score, confidence: 1,
                                  rationale: data.rationale, data_available: true,
                                }; renderAgents(); break;
    case "signal.approved":
    case "signal.proposed":
    case "signal.rejected":     upsertSignal(data); break;
    case "risk.state":          renderRisk(data); break;
    case "news.item":           addNews(data); break;
    case "macro.update":        renderMacro(data); break;
    case "learning.update":     loadScorecard(); break;
    case "market.switched":     applyMarket(data.market); break;
    case "position.update":     loadPositions(); break;
  }
}


/* ====================================================================== */
/* Market switching                                                       */
/* ====================================================================== */
function applyMarket(profile) {
  if (!profile) return;
  state.market = profile;

  // Drives the theme: accent colour and page tint per market.
  document.documentElement.dataset.market = profile.code || "IN";

  document.querySelectorAll("#market-switch button").forEach((b) => {
    b.setAttribute("aria-pressed", String(b.dataset.market === profile.code));
  });

  const capCur = $("cap-cur");
  if (capCur) capCur.textContent = profile.currency?.symbol || "₹";

  const s = profile.session || {};
  document.title = `panadhanam — ${profile.name || profile.code}`;
  $("market-clock").title =
    `${profile.name}: ${s.market_open}–${s.market_close} ${profile.timezone}`;
  renderMarketClock();
}

function renderMarketClock() {
  const p = state.market;
  if (!p || !p.timezone) return;
  try {
    const t = new Date().toLocaleTimeString("en-GB", {
      timeZone: p.timezone, hour: "2-digit", minute: "2-digit",
    });
    const phase = state.status?.phase ? ` · ${state.status.phase}` : "";
    $("market-clock").textContent = `${p.flag || ""} ${t}${phase}`;
  } catch {
    $("market-clock").textContent = p.code || "";
  }
}

async function loadMarkets() {
  const res = await fetch("/api/markets");
  if (!res.ok) return;
  const d = await res.json();
  applyMarket(d.profile);
}

async function switchMarket(code) {
  if (state.switching || code === state.market?.code) return;
  state.switching = true;
  const buttons = [...document.querySelectorAll("#market-switch button")];
  buttons.forEach((b) => (b.disabled = true));

  try {
    const res = await fetch(`/api/markets/${code}`, { method: "POST" });
    const d = await res.json();

    if (!res.ok || d.switched === false) {
      // The commonest refusal is open positions — switching would orphan them.
      const why = d.detail || d.reason || "could not switch market";
      $("banners").innerHTML =
        `<div class="banner crit"><b>Market not switched.</b> ${esc(why)}</div>`;
      return;
    }

    applyMarket(d.profile);

    // Everything on screen belonged to the old market — clear it, don't let
    // stale Indian signals sit under a US header.
    state.agents = {};
    state.signals = [];
    state.news = [];
    $("agents").innerHTML = `<div class="empty">Waiting for the first ${esc(d.profile.name)} cycle…</div>`;
    $("signals").innerHTML = `<div class="empty">No signals yet for ${esc(d.profile.name)}.</div>`;
    $("news").innerHTML = `<div class="empty">No headlines yet.</div>`;
    $("opp-tiers").innerHTML =
      `<div class="empty" style="grid-column:1/-1">Hit <b>Scan watchlist</b> to rank ${esc(d.profile.name)} symbols.</div>`;
    $("replay-body").innerHTML = `<div class="empty">Run a replay for ${esc(d.profile.name)}.</div>`;
    $("macro").innerHTML = `<div class="empty">Macro feed not yet loaded.</div>`;

    await loadWatchlist();
    initChart();
    await Promise.all([loadChart(), loadPositions(), loadScorecard(),
                       loadStatus(), runCalc()]);
  } finally {
    state.switching = false;
    buttons.forEach((b) => (b.disabled = false));
  }
}

async function loadStatus() {
  const res = await fetch("/api/status");
  if (res.ok) applyStatus(await res.json());
}

/* ====================================================================== */
/* Header / status                                                        */
/* ====================================================================== */
function applyStatus(s) {
  state.status = s;
  if (s.market && s.market.code !== state.market?.code) applyMarket(s.market);
  renderMarketClock();
  const live = s.live_orders && s.auto_place_orders;
  const modeBadge = $("mode-badge");
  modeBadge.textContent = live ? "LIVE ORDERS" : "PAPER / ALERT-ONLY";
  modeBadge.className = "badge " + (live ? "live" : "paper");

  $("phase-badge").textContent = s.phase;
  $("phase-badge").className = "badge " + (s.phase === "open" ? "ok" : "");
  $("broker-badge").textContent = "broker: " + (s.desk?.broker ?? "—");
  $("broker-badge").title = state.market
    ? `Brokers available for ${state.market.name}: ${(state.market.brokers || []).join(", ")}`
    : "";
  $("brain-badge").textContent =
    s.desk?.reasoning === "claude" ? `claude (${s.desk.model})` : "rule-based";

  state.paused = s.paused;
  $("btn-pause").textContent = s.paused ? "Resume" : "Pause";

  if (s.risk) renderRisk(s.risk);
  renderBanners(s);
}

function renderBanners(s) {
  const out = [];
  if (s.risk?.halted) {
    out.push(`<div class="banner crit"><b>Desk halted.</b> ${esc(s.risk.halt_reason)}
      No new positions will be opened. Clear it deliberately from the API
      (<code>POST /api/risk/resume</code>) once you have reviewed the day.</div>`);
  }
  if (s.live_orders && s.auto_place_orders) {
    out.push(`<div class="banner crit"><b>Live order placement is ON.</b>
      Real orders will be sent to ${esc(s.desk?.broker)}. Real money is at risk.</div>`);
  }
  if (s.desk?.reasoning !== "claude") {
    out.push(`<div class="banner warn">Agents are running on their deterministic rule
      engines. Add <code>ANTHROPIC_API_KEY</code> to <code>.env</code> to enable
      Claude reasoning and the learning-from-context features.</div>`);
  }
  $("banners").innerHTML = out.join("");
}

/* ====================================================================== */
/* Risk tiles                                                             */
/* ====================================================================== */
function renderRisk(r) {
  const used = Math.min(Math.max(-r.daily_pnl, 0) / (r.daily_loss_limit || 1), 1);
  const meterClass = used > 0.85 ? "critical" : used > 0.6 ? "serious" : used > 0.35 ? "warn" : "";

  $("risk-stats").innerHTML = `
    <div class="stat">
      <div class="label">Capital</div>
      <div class="value">${money(Math.round(r.capital))}</div>
      <div class="sub">${fmt(r.risk_per_trade_pct, 1)}% risked per trade</div>
    </div>
    <div class="stat">
      <div class="label">Day P&amp;L</div>
      <div class="value ${signClass(r.daily_pnl)}">${r.daily_pnl >= 0 ? "+" : ""}${money(Math.round(r.daily_pnl))}</div>
      <div class="sub">realised ${money(Math.round(r.realised_pnl))} · open ${money(Math.round(r.unrealised_pnl))}</div>
    </div>
    <div class="stat">
      <div class="label">Room before halt</div>
      <div class="value">${money(Math.round(r.remaining_loss_budget))}</div>
      <div class="sub">halts at −${money(Math.round(r.daily_loss_limit))} on the day</div>
      <div class="meter" role="img"
           aria-label="${(used * 100).toFixed(0)}% of the daily loss limit used">
        <span class="${meterClass}" style="width:${(used * 100).toFixed(1)}%"></span>
      </div>
    </div>
    <div class="stat">
      <div class="label">Risk per trade</div>
      <div class="value">${money(Math.round(r.risk_per_trade))}</div>
      <div class="sub">min R:R ${fmt(r.min_risk_reward, 1)}:1</div>
    </div>
    <div class="stat">
      <div class="label">Open positions</div>
      <div class="value">${r.open_positions}</div>
      <div class="sub">${r.trades_today} today · ${r.wins_today}W / ${r.losses_today}L</div>
    </div>
    <div class="stat">
      <div class="label">Desk status</div>
      <div class="value ${r.halted ? "neg" : "pos"}">${r.halted ? "HALTED" : "ACTIVE"}</div>
      <div class="sub">exposure ${money(Math.round(r.exposure))}</div>
    </div>`;
  $("risk-updated").textContent = new Date().toLocaleTimeString("en-IN");
}

/* ====================================================================== */
/* Agents                                                                 */
/* ====================================================================== */
function renderAgents() {
  const order = ["candlestick", "derivatives", "news_sentiment", "macro_flow",
                 "fundamental", "cmio"];
  const items = order.filter((id) => state.agents[id]);
  if (!items.length) return;

  $("agents").innerHTML = items.map((id) => {
    const r = state.agents[id];
    const score = Number(r.score ?? 0);
    const width = Math.min(Math.abs(score), 1) * 50;
    const abstained = r.data_available === false;
    const confidence = r.confidence !== undefined ? ` · ${Math.round(r.confidence * 100)}% conf` : "";

    return `
      <div class="agent ${abstained ? "abstain" : ""}">
        <span class="swatch" style="background:${AGENT_COLORS[id] || "var(--text-muted)"}"></span>
        <div class="name">${esc(AGENT_LABELS[id] || id)}
          <small>${abstained ? "abstained — no data" : esc(r.bias || "")}${abstained ? "" : confidence}</small>
        </div>
        <div class="score ${signClass(score)}">${score >= 0 ? "+" : ""}${fmt(score, 2)}</div>
        <div class="divbar" role="img"
             aria-label="${esc(AGENT_LABELS[id] || id)} score ${fmt(score, 2)}">
          <i class="${score >= 0 ? "up" : "down"}" style="width:${width}%"></i>
        </div>
        <div class="why">${esc((r.rationale || "").slice(0, 230))}</div>
      </div>`;
  }).join("");
}

/* ====================================================================== */
/* Signals                                                                */
/* ====================================================================== */
function upsertSignal(sig) {
  state.signals = state.signals.filter((s) => s.id !== sig.id);
  state.signals.unshift(sig);
  state.signals = state.signals.slice(0, 60);
  renderSignals();
}

function renderSignals() {
  if (!state.signals.length) return;
  $("signal-count").textContent = `${state.signals.length} recent`;

  $("signals").innerHTML = state.signals.map((s) => {
    const rejected = s.status === "REJECTED";
    const cls = rejected ? "rejected" : s.side === "BUY" ? "buy" : "sell";
    const line = s.alert_line || buildAlertLine(s);
    const confirmations = (s.confirmations || [])
      .map((c) => `<span class="pill ${s.bias === "BULLISH" ? "up" : "down"}">${esc(c)}</span>`).join("");

    return `
      <div class="signal ${cls}">
        <div class="line">${rejected ? "✕ " : "▸ "}${esc(line)}</div>
        <div class="meta">
          <span>qty <b>${qtyLabel(s)}</b></span>
          <span>R:R <b>${fmt(s.risk_reward, 2)}</b></span>
          <span>risk <b>${money(Math.round(s.total_risk))}</b> (${fmt(s.capital_at_risk_pct, 2)}%)</span>
          <span>score <b>${s.composite_score >= 0 ? "+" : ""}${fmt(s.composite_score, 2)}</b></span>
          <span>${esc(s.regime || "")}</span>
        </div>
        ${confirmations ? `<div style="margin-top:6px">${confirmations}</div>` : ""}
        ${rejected
          ? `<div class="note"><b>Rejected:</b> ${esc((s.rejection_reasons || []).join(" · "))}</div>`
          : `<div class="note">${esc((s.rationale || "").slice(0, 260))}</div>`}
        ${s.counter_argument && !rejected
          ? `<div class="counter"><b>Counter-argument:</b> ${esc(s.counter_argument.slice(0, 220))}</div>` : ""}
      </div>`;
  }).join("");
}

function buildAlertLine(s) {
  const i = s.instrument || {};
  const leg = i.strike ? `${i.symbol} ${Math.round(i.strike)} ${i.instrument_type}` : i.tradingsymbol;
  return `[${i.symbol} | ${leg}] [${s.side}] [ENTRY ${fmt(s.entry)}] ` +
         `[SL ${fmt(s.stop_loss)}] [TGT ${fmt(s.target)} (${fmt(s.risk_reward, 1)}R)]`;
}

/* ====================================================================== */
/* News & macro                                                           */
/* ====================================================================== */
function addNews(item) {
  state.news.unshift(item);
  state.news = state.news.slice(0, 40);
  renderNews();
}

function renderNews() {
  if (!state.news.length) return;
  $("news-count").textContent = `${state.news.length} items`;
  $("news").innerHTML = state.news.map((n) => `
    <div class="news-item">
      <div class="impact ${signClass(n.impact)}">${n.impact >= 0 ? "+" : ""}${fmt(n.impact, 2)}</div>
      <div>
        <div class="headline">${esc(n.title)}</div>
        <div class="src">${esc(n.source)}${(n.symbols || []).length ? " · " + n.symbols.map(esc).join(", ") : ""}</div>
      </div>
    </div>`).join("");
}

function renderMacro(m) {
  const notes = (m.notes || []).map((n) => `<li style="margin-bottom:5px">${esc(n)}</li>`).join("");
  const rows = Object.entries(m.changes_pct || {}).map(([k, v]) => `
    <tr>
      <td>${esc(k.replace(/_/g, " "))}</td>
      <td class="num">${fmt((m.values || {})[k], 2)}</td>
      <td class="num ${signClass(v)}">${v >= 0 ? "+" : ""}${fmt(v, 2)}%</td>
    </tr>`).join("");

  $("macro").innerHTML = rows
    ? `<table><thead><tr><th>Market</th><th class="num">Level</th><th class="num">Change</th></tr></thead>
       <tbody>${rows}</tbody></table>
       ${notes ? `<ul style="margin:12px 0 0;padding-left:18px;font-size:12px;color:var(--text-secondary)">${notes}</ul>` : ""}`
    : `<div class="empty">Macro feed unavailable. Check outbound network access.</div>`;
}

/* ====================================================================== */
/* Chart                                                                  */
/* ====================================================================== */
function ink(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function initChart() {
  const el = $("chart");
  el.innerHTML = "";
  if (typeof LightweightCharts === "undefined") {
    // Everything else on this page still works without the chart library.
    el.innerHTML = `<div class="empty">Chart library unavailable.
      Re-add <code>dashboard/static/vendor-lightweight-charts.js</code>
      or allow access to the CDN.</div>`;
    state.chart = null;
    return;
  }
  state.chart = LightweightCharts.createChart(el, {
    layout: { background: { color: "transparent" }, textColor: ink("--text-muted"), fontSize: 11 },
    grid: { vertLines: { color: ink("--grid") }, horzLines: { color: ink("--grid") } },
    rightPriceScale: { borderColor: ink("--baseline") },
    timeScale: { borderColor: ink("--baseline"), timeVisible: true, secondsVisible: false },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    autoSize: true,
  });

  state.series.candles = state.chart.addCandlestickSeries({
    upColor: ink("--good"), downColor: ink("--critical"),
    borderUpColor: ink("--good"), borderDownColor: ink("--critical"),
    wickUpColor: ink("--good"), wickDownColor: ink("--critical"),
  });
  const line = (color, width = 2) =>
    state.chart.addLineSeries({ color, lineWidth: width, priceLineVisible: false,
                                lastValueVisible: false, crosshairMarkerVisible: false });
  state.series.ema9  = line(ink("--series-1"));
  state.series.ema21 = line(ink("--series-2"));
  state.series.ema50 = line(ink("--series-4"));
  state.series.vwap  = line(ink("--series-6"));
}

function ema(values, period) {
  const k = 2 / (period + 1);
  let prev = values[0];
  return values.map((v, i) => (i === 0 ? (prev = v) : (prev = v * k + prev * (1 - k))));
}

async function loadChart() {
  const symbol = $("chart-symbol").value;
  if (!symbol) return;
  const res = await fetch(`/api/market/${encodeURIComponent(symbol)}/candles?timeframe=${state.timeframe}&count=250`);
  if (!res.ok) return;
  const { candles } = await res.json();
  if (!candles.length) return;
  if (!state.chart) { loadChain(symbol); return; }

  state.series.candles.setData(candles);

  const closes = candles.map((c) => c.close);
  const times = candles.map((c) => c.time);
  const asLine = (arr) => times.map((t, i) => ({ time: t, value: arr[i] }));
  state.series.ema9.setData(asLine(ema(closes, 9)));
  state.series.ema21.setData(asLine(ema(closes, 21)));
  state.series.ema50.setData(asLine(ema(closes, 50)));

  // Session VWAP — resets each calendar day, matching the backend.
  let cumPV = 0, cumV = 0, lastDay = null;
  const vwap = candles.map((c) => {
    const day = new Date(c.time * 1000).toDateString();
    if (day !== lastDay) { cumPV = 0; cumV = 0; lastDay = day; }
    const typical = (c.high + c.low + c.close) / 3;
    const vol = c.volume || 1;
    cumPV += typical * vol; cumV += vol;
    return { time: c.time, value: cumPV / cumV };
  });
  state.series.vwap.setData(vwap);
  state.chart.timeScale().fitContent();

  loadChain(symbol);
}

/* ====================================================================== */
/* Option chain                                                           */
/* ====================================================================== */
async function loadChain(symbol) {
  const res = await fetch(`/api/market/${encodeURIComponent(symbol)}/chain`);
  if (!res.ok) {
    $("chain-metrics").innerHTML = `<div class="empty">No option chain for ${esc(symbol)}.</div>`;
    $("chain-table").innerHTML = "";
    $("chain-meta").textContent = "";
    return;
  }
  const { chain, metrics } = await res.json();
  $("chain-meta").textContent = `${chain.underlying} · exp ${chain.expiry}`;

  const pcrDir = metrics.pcr_direction;
  $("chain-metrics").innerHTML = `
    <div class="calc-out">
      <div><div class="k">PCR (OI)</div><div class="v ${pcrDir > 0 ? "pos" : pcrDir < 0 ? "neg" : ""}">${fmt(metrics.pcr_oi, 2)}</div></div>
      <div><div class="k">Max pain</div><div class="v">${fmtInt(metrics.max_pain)}</div></div>
      <div><div class="k">ATM IV</div><div class="v">${fmt(metrics.atm_iv, 1)}%</div></div>
      <div><div class="k">OI buildup</div><div class="v" style="font-size:12px">${esc(metrics.buildup)}</div></div>
    </div>
    <p style="font-size:11.5px;color:var(--text-muted);margin:10px 0 0">${esc(metrics.buildup_note)}</p>`;

  const atm = metrics.atm_strike;
  const byStrike = {};
  for (const leg of chain.legs) {
    byStrike[leg.strike] = byStrike[leg.strike] || {};
    byStrike[leg.strike][leg.option_type] = leg;
  }
  const strikes = Object.keys(byStrike).map(Number).sort((a, b) => a - b)
    .filter((s) => Math.abs(s - atm) <= (strikes_step(chain) * 6));
  const maxOI = Math.max(...chain.legs.map((l) => l.oi), 1);

  $("chain-table").innerHTML = `
    <table class="chain">
      <thead><tr>
        <th class="num">CE OI</th><th class="num">CE LTP</th>
        <th style="text-align:center">Strike</th>
        <th class="num">PE LTP</th><th class="num">PE OI</th>
      </tr></thead>
      <tbody>${strikes.map((s) => {
        const ce = byStrike[s].CE || {}, pe = byStrike[s].PE || {};
        return `<tr class="${s === atm ? "atm" : ""}">
          <td class="num">${fmtInt(Math.round(ce.oi || 0))}
            <span class="oi-bar" style="width:${((ce.oi || 0) / maxOI * 100).toFixed(0)}%"></span></td>
          <td class="num">${fmt(ce.ltp, 2)}</td>
          <td class="strike">${fmtInt(s)}</td>
          <td class="num">${fmt(pe.ltp, 2)}</td>
          <td class="num">${fmtInt(Math.round(pe.oi || 0))}
            <span class="oi-bar pe" style="width:${((pe.oi || 0) / maxOI * 100).toFixed(0)}%"></span></td>
        </tr>`;
      }).join("")}</tbody>
    </table>`;
}

function strikes_step(chain) {
  const s = [...new Set(chain.legs.map((l) => l.strike))].sort((a, b) => a - b);
  let step = Infinity;
  for (let i = 1; i < s.length; i++) step = Math.min(step, s[i] - s[i - 1]);
  return Number.isFinite(step) && step > 0 ? step : 50;
}


/* ====================================================================== */
/* Opportunity board                                                      */
/* ====================================================================== */
const TIER_LABEL = {
  low:    "Low risk",
  medium: "Medium risk",
  high:   "High risk",
};
const TIER_BLURB = {
  low:    "Liquid, aligned timeframes, clean stop",
  medium: "A normal setup with one or two things against it",
  high:   "Thin evidence, rich premium, or a volatile tape",
};

function renderOpportunities(d) {
  const src = d.data_source || {};
  const badge = $("opp-source");
  badge.textContent = src.label || "";
  badge.className = "badge " + (src.simulated ? "sim" : "ok");

  $("opp-meta").textContent =
    `${d.scanned} scanned · ${d.found} with a directional lean · ${d.actionable} tradeable now`;

  $("opp-tiers").innerHTML = ["low", "medium", "high"].map((tier) => {
    const items = (d.tiers && d.tiers[tier]) || [];
    return `
      <div class="tier ${tier}">
        <header>${TIER_LABEL[tier]}<span class="n">${items.length}</span></header>
        ${items.length
          ? items.map((o) => oppCard(o)).join("")
          : `<div class="empty" style="padding:18px 14px">
               Nothing in this bucket right now.<br>
               <span style="font-size:11px">${esc(TIER_BLURB[tier])}</span>
             </div>`}
      </div>`;
  }).join("");
}

/* US equities trade in single shares — printing "99 lots" for 99 shares of QQQ
   is not just noise, it implies a 100x bigger position than you hold. */
function qtyLabel(t) {
  const q = fmtInt(t.quantity);
  if (!t.unit_size || t.unit_size <= 1) return `${q} shares`;
  const word = t.unit_label || "lot";
  return `${q} (${fmtInt(t.lots)} ${word}${t.lots === 1 ? "" : "s"})`;
}

function oppCard(o) {
  const t = o.trade;
  const bull = o.lean === "BULLISH";
  const factors = (o.factors || []).map((f) => {
    const cls = f.points > 0 ? "bad" : f.points < 0 ? "good" : "";
    return `<span class="factor ${cls}" title="${esc(f.detail)}">
              ${f.points > 0 ? "+" : ""}${f.points} ${esc(f.label)}
            </span>`;
  }).join("");

  return `
    <div class="opp">
      <div class="head">
        <span class="sym">${esc(o.symbol)}</span>
        <span class="lean ${bull ? "bull" : "bear"}">${bull ? "▲ LONG" : "▼ SHORT"}</span>
        <span style="font-size:11px;color:var(--text-muted)">
          conviction ${fmt(o.conviction, 2)} · ${esc(o.regime)}</span>
        <span class="state ${o.actionable ? "go" : "wait"}">
          ${o.actionable ? "TRADEABLE" : "WATCH"}</span>
      </div>

      ${t ? `
        <div class="trade">
          <div class="row"><span>Instrument</span><b>${esc(t.instrument)}</b></div>
          <div class="row"><span>Entry</span><b>${fmt(t.entry)}</b></div>
          <div class="row"><span>Stop loss</span><b class="neg">${fmt(t.stop_loss)}</b></div>
          <div class="row"><span>Target (${fmt(t.risk_reward, 1)}R)</span><b class="pos">${fmt(t.target)}</b></div>
          <div class="row"><span>Quantity</span><b>${qtyLabel(t)}</b></div>
          <div class="row"><span>Risk</span><b>${money(Math.round(t.total_risk))} (${fmt(t.capital_at_risk_pct, 2)}%)</b></div>
        </div>` : ""}

      ${!o.actionable && o.blocked_reason
        ? `<div class="blocked">⚠ ${esc(o.blocked_reason)}</div>` : ""}

      ${o.counter_argument
        ? `<div style="font-size:11px;color:var(--text-muted);margin-top:5px;line-height:1.45">
             <b style="color:var(--text-secondary)">Against:</b> ${esc(o.counter_argument.slice(0, 150))}
           </div>` : ""}

      <div class="factors">${factors}</div>
    </div>`;
}

async function loadOpportunities(refresh = false) {
  const btn = $("btn-scan");
  if (refresh) { btn.disabled = true; btn.textContent = "Scanning…"; }
  try {
    const res = await fetch(refresh ? "/api/opportunities/scan" : "/api/opportunities",
                            { method: refresh ? "POST" : "GET" });
    if (res.ok) renderOpportunities(await res.json());
  } finally {
    btn.disabled = false; btn.textContent = "Scan watchlist";
  }
}

/* ====================================================================== */
/* Historical replay                                                      */
/* ====================================================================== */
function renderReplay(d) {
  const src = d.data_source || {};
  const badge = $("replay-source");
  badge.textContent = src.label || "";
  badge.className = "badge " + (src.simulated ? "sim" : "ok");

  const t = d.totals || {};
  $("replay-meta").textContent =
    `${d.window?.from ?? ""} → ${d.window?.to ?? ""} · ${d.window?.timeframe ?? ""}`;

  const best = (d.best_trades || []).slice(0, 10);
  const worst = (d.worst_trades || []).slice(0, 5);
  const rows = (list) => list.map((x) => `
    <tr>
      <td>${esc(x.symbol)}</td>
      <td class="${x.side === "BUY" ? "pos" : "neg"}">${esc(x.side)}</td>
      <td class="num">${fmt(x.entry)}</td>
      <td class="num">${fmt(x.exit)}</td>
      <td class="num ${signClass(x.r_multiple)}"><b>${x.r_multiple >= 0 ? "+" : ""}${fmt(x.r_multiple, 2)}R</b></td>
      <td>${esc(x.setup)}</td>
      <td class="num" title="bars held to exit">${fmtInt(x.bars_held)}</td>
    </tr>`).join("");

  $("replay-body").innerHTML = `
    <div class="replay-totals">
      <div><div class="k">Setups fired</div><div class="v">${fmtInt(t.trades)}</div></div>
      <div><div class="k">Win rate</div><div class="v">${fmt(t.win_rate, 1)}%</div></div>
      <div><div class="k">Total R</div>
           <div class="v ${signClass(t.total_r)}">${t.total_r >= 0 ? "+" : ""}${fmt(t.total_r, 1)}R</div></div>
      <div><div class="k">Avg R / trade</div>
           <div class="v ${signClass(t.avg_r)}">${t.avg_r >= 0 ? "+" : ""}${fmt(t.avg_r, 3)}</div></div>
      <div><div class="k">Expectancy</div>
           <div class="v ${t.expectancy === "positive" ? "pos" : "neg"}"
                style="font-size:14px">${esc((t.expectancy || "").toUpperCase())}</div></div>
      <div><div class="k">Break-even needs</div><div class="v" style="font-size:14px">33.4%</div>
           <div class="k" style="margin-top:3px">at 2:1 R:R</div></div>
    </div>

    ${best.length ? `
      <table style="margin-top:2px">
        <thead><tr><th colspan="7" style="color:var(--good)">Best setups in the window</th></tr>
        <tr><th>Symbol</th><th>Side</th><th class="num">Entry</th><th class="num">Exit</th>
            <th class="num">Result</th><th>Setup</th><th class="num">Bars</th></tr></thead>
        <tbody>${rows(best)}</tbody>
      </table>` : `<div class="empty">No setups fired in this window.</div>`}

    ${worst.length ? `
      <table>
        <thead><tr><th colspan="7" style="color:var(--critical)">Worst setups — read these too</th></tr>
        <tr><th>Symbol</th><th>Side</th><th class="num">Entry</th><th class="num">Exit</th>
            <th class="num">Result</th><th>Setup</th><th class="num">Bars</th></tr></thead>
        <tbody>${rows(worst)}</tbody>
      </table>` : ""}

    ${(d.by_symbol || []).length ? `
      <table>
        <thead><tr><th colspan="6">Breakdown by symbol</th></tr>
        <tr><th>Symbol</th><th class="num">Setups</th><th class="num">Wins</th>
            <th class="num">Win rate</th><th class="num">Total R</th>
            <th class="num">Avg R</th></tr></thead>
        <tbody>${d.by_symbol.map((sy) => `
          <tr>
            <td>${esc(sy.symbol)}</td>
            <td class="num">${fmtInt(sy.trades)}</td>
            <td class="num">${fmtInt(sy.wins)}</td>
            <td class="num">${fmt(sy.win_rate, 1)}%</td>
            <td class="num ${signClass(sy.total_r)}"><b>${sy.total_r >= 0 ? "+" : ""}${fmt(sy.total_r, 1)}R</b></td>
            <td class="num ${signClass(sy.avg_r)}">${sy.avg_r >= 0 ? "+" : ""}${fmt(sy.avg_r, 3)}</td>
          </tr>`).join("")}</tbody>
      </table>` : ""}

    <div class="caveats">
      <b>Read this before trusting the numbers above:</b>
      <ul>${(d.caveats || []).map((c) => `<li>${esc(c)}</li>`).join("")}</ul>
    </div>`;
}

async function loadReplay(refresh = false) {
  const btn = $("btn-replay");
  const days = $("replay-days").value;
  if (refresh) { btn.disabled = true; btn.textContent = "Replaying…"; }
  try {
    const url = refresh
      ? `/api/replay/run?days=${days}`
      : `/api/replay?days=${days}`;
    const res = await fetch(url, { method: refresh ? "POST" : "GET" });
    if (res.ok) renderReplay(await res.json());
  } finally {
    btn.disabled = false; btn.textContent = `Replay last ${days} sessions`;
  }
}

/* ====================================================================== */
/* Positions & scorecard                                                  */
/* ====================================================================== */
async function loadPositions() {
  const res = await fetch("/api/positions");
  if (!res.ok) return;
  const { open_signals } = await res.json();
  if (!open_signals.length) {
    $("positions").innerHTML = `<div class="empty">No open positions.</div>`;
    return;
  }
  $("positions").innerHTML = `
    <table><thead><tr>
      <th>Instrument</th><th>Side</th><th class="num">Entry</th>
      <th class="num">SL</th><th class="num">Target</th><th class="num">Qty</th><th class="num">Risk</th>
    </tr></thead><tbody>
    ${open_signals.map((p) => `<tr>
      <td>${esc(p.tradingsymbol)}</td>
      <td class="${p.side === "BUY" ? "pos" : "neg"}">${esc(p.side)}</td>
      <td class="num">${fmt(p.entry)}</td>
      <td class="num neg">${fmt(p.stop_loss)}</td>
      <td class="num pos">${fmt(p.target)}</td>
      <td class="num">${fmtInt(p.quantity)}</td>
      <td class="num">${money(Math.round(p.total_risk))}</td>
    </tr>`).join("")}</tbody></table>`;
}

async function loadScorecard() {
  const res = await fetch("/api/learning/scorecard");
  if (!res.ok) return;
  const { agents } = await res.json();
  const ids = Object.keys(agents || {});
  if (!ids.length) return;

  $("scorecard").innerHTML = `
    <table><thead><tr>
      <th>Agent</th><th class="num">Calls</th><th class="num">Hit rate</th>
      <th class="num">Avg R</th><th class="num">Weight</th>
    </tr></thead><tbody>
    ${ids.map((id) => {
      const a = agents[id];
      return `<tr>
        <td><span class="swatch" style="display:inline-block;width:8px;height:8px;border-radius:2px;
             margin-right:7px;background:${AGENT_COLORS[id] || "var(--text-muted)"}"></span>${esc(AGENT_LABELS[id] || id)}</td>
        <td class="num">${fmtInt(a.samples || 0)}</td>
        <td class="num">${a.hit_rate ? (a.hit_rate * 100).toFixed(0) + "%" : "—"}</td>
        <td class="num ${signClass(a.avg_r)}">${a.avg_r !== undefined ? fmt(a.avg_r, 2) : "—"}</td>
        <td class="num"><b>${fmt(a.weight, 2)}</b></td>
      </tr>`;
    }).join("")}</tbody></table>`;
}

/* ====================================================================== */
/* Calculator                                                             */
/* ====================================================================== */
let calcTimer = null;
async function runCalc() {
  const body = {
    capital: +$("c-capital").value,
    risk_pct: +$("c-risk").value,
    entry: +$("c-entry").value,
    stop_loss: +$("c-stop").value,
    lot_size: +$("c-lot").value || 1,
    risk_reward: +$("c-rr").value,
  };
  const res = await fetch("/api/risk/calculate", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const r = await res.json();
  if (r.error) {
    $("calc-out").innerHTML = `<div><div class="k">Error</div><div class="v neg" style="font-size:12px">${esc(r.error)}</div></div>`;
    return;
  }
  const overRisk = r.actual_risk_pct > 2.0;
  $("calc-out").innerHTML = `
    <div><div class="k">Quantity</div><div class="v">${fmtInt(r.quantity)}</div></div>
    <div><div class="k">Lots</div><div class="v">${fmtInt(r.lots)}</div></div>
    <div><div class="k">Stop points</div><div class="v">${fmt(r.stop_points)}</div></div>
    <div><div class="k">Capital at risk</div><div class="v ${overRisk ? "neg" : ""}">${money(Math.round(r.actual_risk))}</div></div>
    <div><div class="k">% of capital</div><div class="v ${overRisk ? "neg" : "pos"}">${fmt(r.actual_risk_pct, 2)}%</div></div>
    <div><div class="k">Target</div><div class="v pos">${fmt(r.target)}</div></div>
    <div><div class="k">Reward at ${fmt(r.risk_reward, 1)}R</div><div class="v pos">${money(Math.round(r.reward))}</div></div>
    <div><div class="k">Notional</div><div class="v">${money(Math.round(r.notional))}</div>
         <div class="k" style="margin-top:3px">${fmt(r.notional_pct_of_capital, 1)}% of capital</div></div>
    <div><div class="k">Losses to ruin</div>
         <div class="v">${r.max_consecutive_losses_to_ruin === null ? "—" : fmtInt(r.max_consecutive_losses_to_ruin)}</div>
         <div class="k" style="margin-top:3px">consecutive full stops</div></div>
    ${r.note ? `<div style="grid-column:1/-1"><div class="k">Why zero</div>
         <div style="font-size:12px;color:var(--warning);line-height:1.5">${esc(r.note)}</div></div>` : ""}`;
}

/* ====================================================================== */
/* Boot                                                                   */
/* ====================================================================== */
async function loadWatchlist() {
  const res = await fetch("/api/watchlist");
  const { watchlist } = await res.json();
  $("chart-symbol").innerHTML = watchlist
    .map((w) => `<option value="${esc(w.symbol)}">${esc(w.symbol)}</option>`).join("");
}

async function loadHistory() {
  const [sigRes, statRes] = await Promise.all([
    fetch("/api/signals?limit=40"),
    fetch("/api/status"),
  ]);
  if (sigRes.ok) {
    const { signals } = await sigRes.json();
    state.signals = signals.map((row) => {
      let payload = {};
      try { payload = JSON.parse(row.payload || "{}"); } catch {}
      return { ...payload, ...row,
               confirmations: safeParse(row.confirmations),
               rejection_reasons: safeParse(row.rejection_reasons) };
    });
    renderSignals();
  }
  if (statRes.ok) applyStatus(await statRes.json());
}

const safeParse = (s) => { try { return JSON.parse(s || "[]"); } catch { return []; } };

function logLine(topic, data, ts) {
  const box = $("log");
  const el = document.createElement("div");
  el.className = "log" + (topic === "system.error" ? " err" : "");
  let summary = "";
  if (topic === "agent.report") summary = `${data.agent_id} ${data.symbol} ${data.score >= 0 ? "+" : ""}${fmt(data.score, 2)}`;
  else if (topic === "cycle.done") summary = `${data.symbol} → ${data.bias} (${fmt(data.composite_score, 2)})`;
  else if (topic.startsWith("signal")) summary = data.id || "";
  else if (topic === "news.item") summary = (data.title || "").slice(0, 70);
  else summary = "";
  el.innerHTML = `<time>${new Date(ts).toLocaleTimeString("en-IN")}</time>
                  <span class="topic">${esc(topic)}</span><span>${esc(summary)}</span>`;
  box.prepend(el);
  while (box.children.length > 200) box.lastChild.remove();
}

function bind() {
  $("btn-cycle").onclick = async (e) => {
    e.target.disabled = true; e.target.textContent = "Running…";
    await fetch("/api/cycle/run", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: "{}" });
    e.target.disabled = false; e.target.textContent = "Run cycle";
    loadPositions(); loadScorecard(); loadOpportunities(false);
  };
  $("btn-premarket").onclick = async (e) => {
    e.target.disabled = true;
    await fetch("/api/premarket/scan", { method: "POST" });
    e.target.disabled = false;
  };
  $("btn-pause").onclick = async () => {
    const next = !state.paused;
    const r = await fetch(`/api/engine/pause?paused=${next}`, { method: "POST" });
    const d = await r.json();
    state.paused = d.paused;
    $("btn-pause").textContent = d.paused ? "Resume" : "Pause";
  };
  $("btn-theme").onclick = () => {
    const root = document.documentElement;
    root.dataset.theme = root.dataset.theme === "light" ? "dark" : "light";
    try { localStorage.setItem("theme", root.dataset.theme); } catch {}
    initChart(); loadChart();
  };
  $("market-switch").onclick = (e) => {
    const btn = e.target.closest("button[data-market]");
    if (btn) switchMarket(btn.dataset.market);
  };
  $("btn-scan").onclick = () => loadOpportunities(true);
  $("btn-replay").onclick = () => loadReplay(true);
  $("replay-days").onchange = () => {
    $("btn-replay").textContent = `Replay last ${$("replay-days").value} sessions`;
  };
  $("chart-symbol").onchange = loadChart;
  $("tf-group").onclick = (e) => {
    const btn = e.target.closest("button[data-tf]");
    if (!btn) return;
    [...$("tf-group").children].forEach((b) => b.setAttribute("aria-pressed", b === btn));
    state.timeframe = btn.dataset.tf;
    loadChart();
  };
  for (const id of ["c-capital", "c-risk", "c-entry", "c-stop", "c-lot", "c-rr"]) {
    $(id).oninput = () => { clearTimeout(calcTimer); calcTimer = setTimeout(runCalc, 220); };
  }
}

(async function boot() {
  try {
    const saved = localStorage.getItem("theme");
    if (saved) document.documentElement.dataset.theme = saved;
  } catch {}

  bind();
  await loadMarkets();       // currency + theme must be set before first render
  initChart();
  await loadWatchlist();
  await Promise.all([loadHistory(), loadChart(), loadPositions(), loadScorecard(), runCalc()]);
  connect();
  setInterval(loadPositions, 30_000);
  setInterval(renderMarketClock, 15_000);
})();
