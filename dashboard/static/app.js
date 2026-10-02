/* ==========================================================================
   panadhanam dashboard client
   One WebSocket carries every live event; REST fills in history on load.

   The page is deliberately small: account, the log of what the desk decided
   and why, what is open, what was signalled, and the reviews. One chart
   only, the live candidate's: the symbol the desk is on, with the entry,
   stop and target of the newest trade it judged — the desk still trades from
   its analysts, not from what a person reads off a candle. The
   desk scans, cycles and arms itself — the controls that did those by hand
   are gone from the page (the API still has them), because a button pressed
   by accident on a desk that runs itself is a state nobody chose.
   ========================================================================== */
"use strict";

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
   (1,00,000) and prints ₹; the US groups in thousands and prints $. */
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
const signed = (n, d = 2) => (n >= 0 ? "+" : "") + fmt(n, d);
const signClass = (n) => (n > 0 ? "pos" : n < 0 ? "neg" : "neutral-ink");
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const state = {
  signals: [],
  status: null,
  market: null,
  tradingDay: null,
  switching: false,
  weekly: null,
  log: { all: [], decisions: [] },
  lastPass: {},          // symbol -> the last "no trade" reason logged
};

/* ====================================================================== */
/* Market time                                                            */
/* ====================================================================== */
/* The browser's clock is wherever the viewer is, not the market's. Every
   time on this page is in the active market's own timezone. */
const marketTz = () => state.market?.timezone || "Asia/Kolkata";
const tzLabel = () => ({ IN: "IST", US: "ET" }[state.market?.code] || "");

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
  logEvent(topic, data, ts);

  switch (topic) {
    case "system.status":       applyStatus(data); break;
    case "signal.approved":
    case "signal.proposed":
    case "signal.rejected":     upsertSignal(data); break;
    case "risk.state":          renderRisk(data); break;
    case "market.switched":     adoptMarket(data.market); break;
    case "trading_day.state":   renderTradingDay(data); break;
    case "trading_day.summary":
      // A summary for another market (India's close, arriving just before
      // the desk moves to the US) is not today's review for this screen.
      if (!data?.market || data.market === state.market?.code) {
        renderDayReport(data, "after square-off");
      }
      break;
    case "news.blackout":
      return { text: d.reason || "news blackout", level: d.active ? "bad" : "" };
    case "focus.updated":       renderFocus(data); break;
    case "position.update":
      loadPositions();
      loadRecord();
      loadHistory();
      notifyTrade(data);
      break;
  }
}

/* ====================================================================== */
/* Market switching                                                       */
/* ====================================================================== */
function applyMarket(profile) {
  if (!profile) return;
  state.market = profile;
  document.documentElement.dataset.market = profile.code || "IN";
  document.querySelectorAll("#market-switch button").forEach((b) => {
    b.setAttribute("aria-pressed", String(b.dataset.market === profile.code));
  });
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
    $("market-clock").textContent = `${p.flag || ""} ${t} ${tzLabel()}${phase}`;
  } catch {
    $("market-clock").textContent = p.code || "";
  }
}

async function loadMarkets() {
  const res = await fetch("/api/markets");
  if (!res.ok) return;
  applyMarket((await res.json()).profile);
}

/* Rebuild the page around a new active market. Shared by the switch button
   and by the desk following the session clock — an automatic switch happens
   while nobody is watching, so stale Indian signals under a US header are
   exactly the confusion to avoid. */
async function adoptMarket(profile) {
  applyMarket(profile);
  state.signals = [];
  $("signals").innerHTML =
    `<div class="empty">No signals yet for ${esc(profile.name)}.</div>`;
  $("s-today").hidden = true;

  await Promise.all([loadPositions(), loadStatus(), loadTradingDay(), loadFocus(),
                     loadRecord()]);
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
      $("banners").innerHTML = `<div class="banner crit"><b>Market not switched.</b>
        ${esc(d.detail || d.reason || "could not switch market")}</div>`;
      return;
    }
    await adoptMarket(d.profile);
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

  // Three states, not two. A paper broker with auto_place_orders on really
  // does place orders — simulated ones — and calling that "alert-only" told
  // people nothing would be sent while the desk was filling positions.
  const live = s.live_orders && s.auto_place_orders;
  const simulating = !live && s.auto_place_orders;
  const badge = $("mode-badge");
  badge.textContent = live ? "LIVE ORDERS"
    : simulating ? "PAPER TRADING ON" : "ALERTS ONLY — no orders";
  badge.className = "badge " + (live ? "live" : simulating ? "ok" : "paper");
  badge.title = live
    ? "Approved signals become REAL orders. Real money is at risk."
    : simulating
      ? "Approved signals become simulated orders on the paper broker."
      : "The desk analyses and alerts but places nothing. start.sh turns "
        + "paper trading on for a paper account; restart it.";

  if (s.day_summary && (!s.day_summary.market
                        || s.day_summary.market === state.market?.code)) {
    renderDayReport(s.day_summary, "after square-off");
  }
  if (s.risk) renderRisk(s.risk);
  renderBanners(s);
}

function renderBanners(s) {
  const out = [];

  // The most consequential thing on the page: are these real prices?
  const ds = s.data_source;
  if (ds && ds.simulated) {
    out.push(`<div class="banner crit">
      <b>These are NOT real market prices.</b> No data feed could connect, so the
      system is generating a synthetic market. Every price, signal and trade on
      this screen refers to nothing real. Check your internet connection and
      restart.</div>`);
  } else if (ds?.synthetic_chain) {
    // Prices and chains come from different places; one can be real while the
    // other is invented, and the F&O numbers drive decisions.
    out.push(`<div class="banner warn">
      <b>Option chain is SIMULATED.</b> Prices are real, but no feed could serve
      an option chain, so Open Interest, PCR and Max Pain are generated.</div>`);
  }
  if (s.risk?.halted) {
    out.push(`<div class="banner crit"><b>Desk halted.</b> ${esc(s.risk.halt_reason)}
      No new positions today.</div>`);
  }
  if (s.live_orders && s.auto_place_orders) {
    out.push(`<div class="banner crit"><b>Live order placement is ON.</b>
      Real orders will be sent to ${esc(s.desk?.broker)}. Real money is at risk.</div>`);
  }
  if (!s.auto_place_orders && !(ds && ds.simulated)) {
    // The one setting that turns a paper desk into a spectator, stated where
    // it cannot be missed rather than in a tooltip.
    out.push(`<div class="banner warn"><b>Paper trading is OFF.</b> The desk
      is approving signals and placing nothing. Restart with
      <code>./start.sh</code> — it turns simulated orders on for a paper
      account and never touches a real-money one.</div>`);
  }
  if (s.desk?.llm_degraded) {
    out.push(`<div class="banner warn">
      <b>${esc(s.desk.reasoning_label)} is configured but not responding.</b>
      ${esc(s.desk.llm_degraded)}. The desk is running on its rule engines
      meanwhile — signals and risk are unaffected.</div>`);
  }
  $("banners").innerHTML = out.join("");
}

/* ====================================================================== */
/* Account                                                                */
/* ====================================================================== */
function renderRisk(r) {
  if (!r) return;
  const used = Math.min(Math.max(-r.daily_pnl, 0) / (r.daily_loss_limit || 1), 1);
  const meterClass = used > 0.85 ? "critical" : used > 0.6 ? "serious" : used > 0.35 ? "warn" : "";

  $("risk-stats").innerHTML = `
    <div class="stat">
      <div class="label">Capital <button class="tiny-edit" id="btn-edit-capital"
            title="Change the account size the desk sizes against">edit</button></div>
      <div class="value">${money(Math.round(r.capital))}</div>
      <div class="sub">${fmt(r.risk_per_trade_pct, 1)}% risked per trade</div>
    </div>
    <div class="stat">
      <div class="label">Day P&amp;L</div>
      <div class="value ${signClass(r.daily_pnl)}">${r.daily_pnl >= 0 ? "+" : ""}${money(Math.round(r.daily_pnl))}</div>
      <div class="sub">realised ${money(Math.round(r.realised_pnl))} · open ${money(Math.round(r.unrealised_pnl))}</div>
    </div>
    <div class="stat">
      <div class="label">Open positions</div>
      <div class="value">${r.open_positions}</div>
      <div class="sub">${r.trades_today} today · ${r.wins_today}W / ${r.losses_today}L</div>
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
      <div class="label">Desk</div>
      <div class="value ${r.halted ? "neg" : "pos"}">${r.halted ? "HALTED" : "ACTIVE"}</div>
      <div class="sub">exposure ${money(Math.round(r.exposure))}</div>
    </div>`;
  $("risk-updated").textContent = new Date().toLocaleTimeString(locale());
  const editBtn = $("btn-edit-capital");
  if (editBtn) editBtn.onclick = promptCapital;
}

async function promptCapital() {
  const current = state.status?.risk?.capital ?? 100000;
  const raw = window.prompt(
    `Account size to size positions against (${cur()}).\n\n` +
    `Every position size is derived from this. In paper mode nothing real ` +
    `is traded either way.`, String(Math.round(current)));
  if (raw === null) return;
  const value = Number(String(raw).replace(/[^0-9.]/g, ""));
  if (!value || value <= 0) { alert("Enter a number greater than zero."); return; }

  const res = await fetch("/api/risk/capital", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ capital: value }),
  });
  const d = await res.json();
  if (!res.ok) { alert(d.detail || "Could not change capital."); return; }
  renderRisk(d.snapshot);
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

/* US equities trade in single shares — printing "99 lots" for 99 shares of QQQ
   implies a 100x bigger position than you hold. */
function qtyLabel(t) {
  const q = fmtInt(t.quantity);
  if (!t.unit_size || t.unit_size <= 1) return `${q} shares`;
  const word = t.unit_label || "lot";
  return `${q} (${fmtInt(t.lots)} ${word}${t.lots === 1 ? "" : "s"})`;
}

function buildAlertLine(s) {
  const i = s.instrument || {};
  const leg = i.strike ? `${i.symbol} ${Math.round(i.strike)} ${i.instrument_type}` : i.tradingsymbol;
  return `[${i.symbol} | ${leg}] [${s.side}] [ENTRY ${fmt(s.entry)}] ` +
         `[SL ${fmt(s.stop_loss)}] [TGT ${fmt(s.target)} (${fmt(s.risk_reward, 1)}R)]`;
}

/* Why it was bought and how it sells — the server writes the sentences
   (app/core/explain.py), so the panel, the day review and the notification
   all say the same thing. */
function whyHtml(s, open = false) {
  const w = s.why || {};
  if (!w.headline && !w.plan && !s.exit_reason) return "";
  const items = [
    w.confirmations?.length
      ? `<li><b>Analysts agreeing:</b> ${w.confirmations.map(esc).join(" · ")}</li>` : "",
    w.vote ? `<li><b>Vote:</b> ${esc(w.vote)}</li>` : "",
    w.stop_basis ? `<li><b>Stop and size:</b> ${esc(w.stop_basis)}</li>` : "",
    w.plan ? `<li><b>Plan:</b> ${esc(w.plan)}</li>` : "",
    w.counter_argument ? `<li><b>What would make it wrong:</b> ${esc(w.counter_argument)}</li>` : "",
  ].join("");
  return `<details class="why"${open ? " open" : ""}>
      <summary>Why it bought${w.headline ? ` — ${esc(w.headline)}` : ""}</summary>
      ${items ? `<ul>${items}</ul>` : ""}
    </details>
    ${s.exit_reason ? `<div class="exit-why"><b>${
      /^Still held/.test(s.exit_reason) ? "How it sells" : "Why it sold"}:</b> ${esc(s.exit_reason)}</div>` : ""}`;
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
          <span>score <b>${signed(s.composite_score)}</b></span>
          <span>${esc(s.regime || "")}</span>
        </div>
        ${confirmations ? `<div style="margin-top:6px">${confirmations}</div>` : ""}
        ${rejected
          ? `<div class="note"><b>Rejected:</b> ${esc((s.rejection_reasons || []).join(" · "))}</div>`
          : whyHtml(s) || `<div class="note">${esc((s.rationale || "").slice(0, 260))}</div>`}
      </div>`;
  }).join("");
}

/* ====================================================================== */
/* Positions                                                              */
/* ====================================================================== */
async function loadPositions() {
  const res = await fetch("/api/positions");
  if (!res.ok) return;
  const { open_signals } = await res.json();
  $("positions-meta").textContent = open_signals.length
    ? `${open_signals.length} open` : "";
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
    </tr>
    <tr class="why-row"><td colspan="7">${whyHtml(p)}</td></tr>`).join("")}</tbody></table>`;
}

/* ====================================================================== */
/* Trading day                                                            */
/* ====================================================================== */
function renderTradingDay(td) {
  if (!td) return;
  state.tradingDay = td;
  const badge = $("td-badge");
  if (td.armed) {
    badge.textContent = td.orders_will_be_placed
      ? `ARMED${td.armed_by === "auto" ? " (auto)" : ""} · trading`
      : "ARMED · alerts only";
    badge.className = "badge " + (td.is_paper_account ? "ok" : "live");
    badge.title = td.orders_will_be_placed
      ? `Approved signals become orders on ${td.broker}.`
      : "Armed, but paper trading is off — restart with ./start.sh.";
  } else {
    badge.textContent = td.disarm_reason ? `not armed — ${td.disarm_reason}` : "not armed";
    badge.className = "badge";
    badge.title = td.auto_arm
      ? "A paper account arms itself when the session opens."
      : "";
  }
  // The start button is for the exception, so it only exists when needed.
  $("btn-td-start").hidden = td.armed || td.halted;
}

async function loadTradingDay() {
  const res = await fetch("/api/trading-day/status");
  if (res.ok) renderTradingDay(await res.json());
}

async function startTradingDay() {
  const res = await fetch("/api/trading-day/start", { method: "POST" });
  const d = await res.json();
  if (!res.ok) {
    $("banners").insertAdjacentHTML("afterbegin",
      `<div class="banner crit"><b>Not started.</b> ${esc(d.detail)}</div>`);
    return;
  }
  renderTradingDay(d.status);
}

/* ====================================================================== */
/* Today's review                                                         */
/* ====================================================================== */
/* One renderer for the day, whether asked for mid-session or published after
   square-off. A day with no trades is the commonest outcome and the more
   useful one to read — provided it says what the desk was waiting for. */
function renderDayReport(r, when) {
  if (!r) return;
  const box = $("s-today");
  box.hidden = false;
  $("today-meta").textContent =
    [r.date, r.market, r.broker, when].filter(Boolean).join(" · ");

  const rows = (r.trades || []).map((t) => `
    <tr>
      <td>${esc(t.time)}</td>
      <td>${esc(t.symbol)}</td>
      <td class="${t.side === "BUY" ? "pos" : "neg"}">${esc(t.side)}</td>
      <td class="num">${fmt(t.entry)}</td>
      <td class="num neg">${t.stop != null ? fmt(t.stop) : "—"}</td>
      <td class="num pos">${t.target != null ? fmt(t.target) : "—"}</td>
      <td class="num">${t.exit ? fmt(t.exit) : "—"}</td>
      <td>${esc(t.status)}</td>
      <td class="num ${signClass(t.r_multiple)}">${t.r_multiple != null
        ? signed(t.r_multiple) + "R" : "open"}</td>
      <td class="num ${signClass(t.pnl)}">${t.pnl != null ? money(Math.round(t.pnl)) : "—"}</td>
    </tr>
    <tr class="why-row"><td colspan="10">${whyHtml(t)}</td></tr>`).join("");

  const why = (r.top_rejections || []).map((x) =>
    `<li><b>${fmtInt(x.count)}×</b> ${esc(x.reason)}</li>`).join("");

  $("today-body").innerHTML = `
    <div class="calc-out">
      <div><div class="k">Symbols judged</div><div class="v">${fmtInt(r.symbols_judged || 0)}</div></div>
      <div><div class="k">Signals</div><div class="v">${fmtInt(r.signals_generated)}</div></div>
      <div><div class="k">Trades taken</div><div class="v">${fmtInt(r.trades_taken)}</div></div>
      <div><div class="k">Closed</div><div class="v">${fmtInt(r.trades_closed)}</div>
           <div class="k" style="margin-top:3px">${fmtInt(r.still_open)} still open</div></div>
      <div><div class="k">Win rate</div><div class="v">${fmt(r.win_rate, 0)}%</div></div>
      <div><div class="k">Total R</div>
           <div class="v ${signClass(r.total_r)}">${signed(r.total_r)}R</div></div>
      <div><div class="k">P&amp;L</div>
           <div class="v ${signClass(r.pnl)}">${money(Math.round(r.pnl))}</div></div>
    </div>
    ${rows ? `<table><thead><tr>
      <th>Time</th><th>Symbol</th><th>Side</th><th class="num">Entry</th>
      <th class="num">Stop</th><th class="num">Target</th><th class="num">Exit</th>
      <th>Status</th><th class="num">R</th><th class="num">P&amp;L</th>
      </tr></thead><tbody>${rows}</tbody></table>`
      : `<div class="empty">No trades taken today.</div>`}
    ${why ? `<div class="callout">
      <div class="k">What the desk was waiting for</div><ul>${why}</ul></div>` : ""}`;
}

async function showDayReport() {
  const btn = $("btn-day-review");
  btn.disabled = true;
  try {
    const res = await fetch("/api/trading-day/report");
    if (!res.ok) return;
    renderDayReport(await res.json(), "so far");
    $("s-today").scrollIntoView({ behavior: "smooth", block: "start" });
  } finally {
    btn.disabled = false;
  }
}

/* ====================================================================== */
/* Weekly review                                                          */
/* ====================================================================== */
async function buildWeekly() {
  const btn = $("btn-weekly");
  const box = $("s-weekly");
  box.hidden = false;
  btn.disabled = true;
  $("weekly-status").textContent =
    "Reading the week and asking the model… a local model takes a minute.";
  box.scrollIntoView({ behavior: "smooth", block: "start" });
  try {
    const res = await fetch("/api/journal/weekly?coach=true");
    if (!res.ok) throw new Error((await res.json()).detail || "Could not build it.");
    renderWeekly(await res.json());
    $("weekly-status").textContent = "";
  } catch (e) {
    $("weekly-body").innerHTML =
      `<div class="banner crit" style="margin:12px 14px">${esc(e.message)}</div>`;
    $("weekly-status").textContent = "";
  } finally {
    btn.disabled = false;
  }
}

function renderWeekly(r) {
  state.weekly = r;
  for (const id of ["btn-weekly-md", "btn-weekly-json", "btn-weekly-save"]) {
    $(id).disabled = false;
  }
  $("weekly-meta").textContent = `${r.week_start} → ${r.week_end} · ${r.market}`;

  const s = r.stats || {};
  // Mid-week numbers are real but partial; saying so is the difference between
  // a progress check and a verdict filed under the wrong week.
  const provisional = r.complete ? "" : `
    <div class="banner warn" style="margin:12px 14px">
      <b>This week is not finished.</b> These numbers are provisional.</div>`;

  const audit = renderAuditDays(r);
  if (!r.trades?.length) {
    $("weekly-body").innerHTML = `${provisional}
      <div class="empty">No trades were graded this week yet. Check the
      Activity Log for what the desk approved and why nothing filled.</div>
      ${audit}`;
    return;
  }

  const trades = r.trades.map((t) => {
    const votes = (t.votes || [])
      .filter((v) => v.data_available)
      .sort((a, b) => Math.abs(b.score || 0) - Math.abs(a.score || 0))
      .map((v) => `<li><b>${esc(AGENT_LABELS[v.agent] || v.agent)}</b>
          <span class="${signClass(v.score)}">${signed(v.score)}</span>
          (${fmt((v.confidence || 0) * 100, 0)}% sure) — ${esc(v.rationale)}</li>`).join("");
    return `
      <details class="weekly-trade">
        <summary>
          <b>${esc(t.symbol)} ${esc(t.side)}</b> · ${esc(t.setup || "Other")}
          <span class="${signClass(t.r_multiple)}">${signed(t.r_multiple)}R</span>
          · ${esc(t.verdict || "ungraded")} · ${t.execution_score ?? "—"}/10
        </summary>
        <div style="padding:8px 4px 2px">
          <div style="font-size:12px;margin-bottom:6px">
            Entry ${fmt(t.planned_entry)} · stop ${fmt(t.planned_stop)} ·
            target ${fmt(t.planned_target)} · exited ${fmt(t.actual_exit)} ·
            ${fmtInt(t.quantity)} × · ${money(Math.round(t.pnl || 0))}
          </div>
          ${t.mistakes?.length
            ? `<div class="banner warn" style="margin:6px 0">Mistakes: ${esc(t.mistakes.join(", "))}</div>` : ""}
          ${t.verdict === "BAD_WIN"
            ? `<div class="banner crit" style="margin:6px 0">Made money <b>while breaking a rule</b>.</div>` : ""}
          ${votes ? `<div class="k">What each analyst said at entry</div><ul>${votes}</ul>` : ""}
          ${t.counter_argument ? `<p style="font-size:12px"><b>Counter-argument at entry:</b>
             ${esc(t.counter_argument)}</p>` : ""}
        </div>
      </details>`;
  }).join("");

  const c = r.coach;
  const list = (xs) => (xs || []).map((x) => `<li>${esc(x)}</li>`).join("");
  const coach = !c ? "" : `
    <div class="callout">
      <div class="k">The coach's read (${esc(c.generated_by)})</div>
      <p style="margin:6px 0"><b>${esc(c.headline)}</b></p>
      ${c.what_worked?.length ? `<div class="k">What worked</div><ul>${list(c.what_worked)}</ul>` : ""}
      ${c.what_cost_money?.length ? `<div class="k">What cost money</div><ul>${list(c.what_cost_money)}</ul>` : ""}
      ${c.rule_changes?.length ? `
        <div class="k">Proposed rule changes — suggestions only, nothing applied</div>
        <table><thead><tr><th>Rule</th><th>Change</th><th>Evidence</th></tr></thead>
        <tbody>${c.rule_changes.map((x) => `
          <tr><td>${esc(x.rule)}</td><td>${esc(x.change)}</td><td>${esc(x.why)}</td></tr>`).join("")}
        </tbody></table>` : ""}
      <div class="k" style="margin-top:8px">Focus next week</div>
      <p style="margin:4px 0">${esc(c.focus_next_week)}</p>
    </div>`;

  $("weekly-body").innerHTML = `${provisional}
    <div class="calc-out">
      <div><div class="k">Trades</div><div class="v">${fmtInt(s.total)}</div></div>
      <div><div class="k">Win rate</div><div class="v">${fmt(s.win_rate, 0)}%</div></div>
      <div><div class="k">Total R</div>
           <div class="v ${signClass(s.total_r)}">${signed(s.total_r)}R</div></div>
      <div><div class="k">Clean execution</div><div class="v">${fmt(s.clean_pct, 0)}%</div></div>
      <div><div class="k">Discipline</div><div class="v">${fmt(s.avg_execution_score, 1)}/10</div></div>
      <div><div class="k">Mistake Cost</div>
           <div class="v neg">${money(Math.round(s.mistake_cost_index || 0))}</div></div>
    </div>
    <div style="padding:10px 14px 0" class="k">Every trade, and why the desk took it</div>
    <div style="padding:4px 14px 10px">${trades}</div>
    ${audit}
    ${coach}`;
}

/* The week's audit log, date by date: every buy and sell with its reason.
   Mid-week it runs Monday to today. */
function renderAuditDays(r) {
  const days = r.audit_days || [];
  if (!days.length) {
    return `<div class="empty" id="weekly-audit">No buys or sells in the audit
      log yet this week.</div>`;
  }
  return `<div id="weekly-audit" style="padding:10px 14px">
    <div class="k">Audit log — day by day</div>
    ${days.map((d) => `
      <details class="weekly-day" ${d === days[days.length - 1] ? "open" : ""}>
        <summary><b>${esc(d.weekday)} ${esc(d.date)}</b> · ${d.buys} buy(s),
          ${d.sells} sell(s) · <span class="${signClass(d.pnl)}">${money(d.pnl, 2)}</span>
          · ${d.wins}W / ${d.losses}L</summary>
        <table><thead><tr><th>Time</th><th>Event</th><th>Instrument</th>
          <th class="num">Qty</th><th class="num">Price</th><th>Stop / Target</th>
          <th class="num">P&amp;L</th><th>Why</th></tr></thead>
        <tbody>${d.events.map((e) => `<tr>
          <td>${esc(e.time)}</td><td>${esc(e.event)}</td><td>${esc(e.instrument)}</td>
          <td class="num">${e.quantity ?? ""}</td>
          <td class="num">${e.price == null ? "—" : fmt(e.price)}</td>
          <td>${e.stop == null ? "—" : `${fmt(e.stop)} / ${fmt(e.target)}`}</td>
          <td class="num ${e.pnl == null ? "" : signClass(e.pnl)}">${
            e.pnl == null ? "—" : money(e.pnl, 2)}</td>
          <td style="font-size:12px">${esc(e.reason)}</td></tr>`).join("")}
        </tbody></table>
      </details>`).join("")}
  </div>`;
}

function downloadWeekly(format) {
  window.location.href = `/api/journal/weekly/download?format=${format}`;
}

async function saveWeekly() {
  $("weekly-status").textContent = "Writing to journal/weekly/…";
  const res = await fetch("/api/journal/weekly/save", { method: "POST" });
  const d = await res.json();
  $("weekly-status").textContent = res.ok
    ? `Saved ${d.label}. Commit journal/weekly/ to keep it.`
    : (d.detail || "Could not save it.");
}

/* ====================================================================== */
/* Activity log                                                           */
/* ====================================================================== */
/* Two tiers. The desk emits an event per analyst per symbol per cycle, so a
   single log fills with that chatter in minutes and pushes the trades — the
   thing anyone opens the log to find — off the bottom. Decisions are kept in
   their own list that the chatter cannot evict. */
const DECISION_TOPICS = new Set([
  "signal.approved", "signal.rejected", "position.update", "focus.updated",
  "news.blackout",
  "trading_day.state", "trading_day.summary", "market.switched",
  "premarket.scan", "system.error",
]);
const LOG_LIMIT = { all: 200, decisions: 400 };

function describe(topic, d) {
  d = d || {};
  switch (topic) {
    case "signal.approved":
      return { text: `APPROVED ${d.alert_line || buildAlertLine(d)}`, level: "good" };
    case "signal.rejected": {
      const sym = d.instrument?.symbol || d.symbol || "";
      const why = (d.rejection_reasons || [])[0] || "rejected by risk";
      return { text: `rejected ${sym} — ${why}`, level: "" };
    }
    case "position.update":
      if (d.event === "opened") {
        return { text: `PAPER ${d.side} ${fmtInt(d.quantity)} ${d.tradingsymbol || d.symbol}`
          + ` @ ${fmt(d.entry)} · SL ${fmt(d.stop_loss)} · TGT ${fmt(d.target)}`
          + (d.why?.headline ? ` — ${d.why.headline}` : ""), level: "good" };
      }
      if (d.event === "closed") {
        return { text: `CLOSED ${d.symbol} ${d.side || ""} @ ${fmt(d.exit_price)} · `
          + `${d.status} · ${signed(d.r_multiple)}R · ${money(Math.round(d.pnl))}`
          + (d.exit_reason ? ` — ${d.exit_reason}` : ""),
                 level: d.pnl >= 0 ? "good" : "bad" };
      }
      if (d.event === "order_failed") {
        return { text: `order FAILED ${d.symbol}: ${d.order?.message || ""}`, level: "bad" };
      }
      return { text: `position update ${d.signal_id || ""}`, level: "" };
    case "trading_day.state":
      return { text: d.armed ? `armed${d.armed_by === "auto" ? " automatically" : ""} · `
          + `${d.orders_will_be_placed ? "trading" : "alerts only"}`
        : `not armed${d.disarm_reason ? " — " + d.disarm_reason : ""}`, level: "" };
    case "trading_day.summary":
      return { text: `day closed · ${fmtInt(d.trades_taken)} trades · `
          + `${money(Math.round(d.pnl || 0))}`, level: "" };
    case "market.switched":
      return { text: `now trading ${d.market?.name || d.market?.code || ""}`, level: "" };
    case "focus.updated":
      return { text: `watching the top ${d.per_band} of each band (from ${d.ranked}) — `
          + Object.entries(d.bands || {}).map(([b, rows]) =>
            `${b}: ${rows.map((r) => r.symbol).join(", ")}`).join(" · "), level: "" };
    case "premarket.scan":
      return { text: "pre-market scan done", level: "" };
    case "system.error":
      return { text: d.error || JSON.stringify(d).slice(0, 160), level: "bad" };
    case "agent.report":
      return { text: `${d.agent_id} ${d.symbol} ${signed(d.score ?? 0)}`, level: "" };
    case "cycle.done": {
      if (d.proceed) {
        return { text: `${d.symbol} → ${d.bias} (${signed(d.composite_score ?? 0)}) · to risk`, level: "" };
      }
      const why = (d.rationale || "").split("Not proceeding: ")[1] || "no edge";
      const agreeing = (d.confirmations || []).join(", ");
      return { text: `${d.symbol} — no trade: ${why.replace(/\.$/, "")}`
          + (agreeing ? ` (agreeing: ${agreeing})` : ""), level: "" };
    }
    case "cycle.start":
      return { text: `judging ${d.symbol}`, level: "" };
    default:
      return { text: "", level: "" };
  }
}

/* The CMIO saying "no" is the commonest decision of all, and it was the one
   the decisions log never showed — a quiet day looked like a desk that was
   not running. Each symbol's "no" is shown once, and again only when the
   reason changes, so a minute-by-minute cycle does not bury the trades. */
function isNewPass(topic, d) {
  if (topic !== "cycle.done" || !d || d.proceed) return false;
  const reason = ((d.rationale || "").split("Not proceeding: ")[1] || "")
    .replace(/[-+]?\d+\.\d+/g, "#");         // ignore score wiggle
  if (state.lastPass[d.symbol] === reason) return false;
  state.lastPass[d.symbol] = reason;
  return true;
}

function logEvent(topic, data, ts) {
  const { text, level } = describe(topic, data);
  const item = { time: ts ? new Date(ts) : new Date(), topic, text, level };
  const push = (list, max) => { list.unshift(item); if (list.length > max) list.length = max; };
  push(state.log.all, LOG_LIMIT.all);
  if (DECISION_TOPICS.has(topic) || isNewPass(topic, data)) {
    push(state.log.decisions, LOG_LIMIT.decisions);
  }
  renderLog();
}

function renderLog() {
  const decisionsOnly = $("log-decisions").checked;
  const list = decisionsOnly ? state.log.decisions : state.log.all;
  $("log-meta").textContent = decisionsOnly
    ? `${state.log.decisions.length} decisions since this page opened`
    : `last ${state.log.all.length} events`;
  $("log-hint").textContent = decisionsOnly
    ? "Trades, approvals, rejections and the reasons."
    : "Everything, including every analyst's vote each cycle.";
  $("log").innerHTML = list.length
    ? list.map((e) => `
        <div class="log ${e.level === "bad" ? "err" : e.level === "good" ? "good" : ""}">
          <time>${e.time.toLocaleTimeString("en-GB", { timeZone: marketTz(),
                    hour: "2-digit", minute: "2-digit", second: "2-digit" })}</time>
          <span class="topic">${esc(e.topic)}</span><span>${esc(e.text)}</span>
        </div>`).join("")
    : `<div class="empty">${decisionsOnly
        ? "No trades or decisions yet since this page opened. Untick the box to watch the desk working."
        : "Waiting for the desk…"}</div>`;
}

/* ====================================================================== */
/* Notifications                                                          */
/* ====================================================================== */
/* A desktop notification when a paper trade is opened or closed — and only
   then. Notifying on every signal considered would fire every few seconds
   and be switched off within the hour. The WebSocket only carries new
   events, so opening the page can never replay the day as a burst. */
function notifyWanted() {
  try { return localStorage.getItem("panadhanam.notify") === "1"; } catch { return false; }
}

async function toggleNotify(on) {
  if (!on) {
    try { localStorage.setItem("panadhanam.notify", "0"); } catch { /* ignore */ }
    return;
  }
  if (!("Notification" in window)) {
    $("log-hint").textContent = "This browser has no notification support.";
    $("notify-trades").checked = false;
    return;
  }
  let granted = Notification.permission === "granted";
  if (!granted && Notification.permission !== "denied") {
    granted = (await Notification.requestPermission()) === "granted";
  }
  if (!granted) {
    // Say so rather than leaving a ticked box that does nothing.
    $("log-hint").textContent =
      "Notifications are blocked for this page in your browser settings.";
    $("notify-trades").checked = false;
    return;
  }
  try { localStorage.setItem("panadhanam.notify", "1"); } catch { /* ignore */ }
}

function notifyTrade(d) {
  if (!d || !["opened", "closed"].includes(d.event)) return;
  if (!notifyWanted() || typeof Notification === "undefined"
      || Notification.permission !== "granted") return;
  const opened = d.event === "opened";
  const title = opened
    ? `Paper trade: ${d.side} ${d.tradingsymbol || d.symbol}`
    : `Closed ${d.symbol}: ${money(Math.round(d.pnl))}`;
  const body = opened
    ? `${fmtInt(d.quantity)} @ ${fmt(d.entry)} · SL ${fmt(d.stop_loss)} · TGT ${fmt(d.target)}`
      + (d.why?.headline ? `\n${d.why.headline}` : "")
    : d.exit_reason || `${d.status} · ${signed(d.r_multiple)}R at ${fmt(d.exit_price)}`;
  try {
    new Notification(title, { body, tag: `${d.event}-${d.signal_id}` });
  } catch { /* a notification failing is never worth breaking the page */ }
}

/* ====================================================================== */
/* Paper record                                                           */
/* ====================================================================== */
function renderRecord(r) {
  if (!r) return;
  const cur = r.currency || "";
  const m = (v) => `${v < 0 ? "−" : ""}${cur}${fmtInt(Math.abs(Math.round(v || 0)))}`;
  const tile = (k, v, sub = "", cls = "") => `<div class="stat">
      <div class="label">${k}</div><div class="value ${cls}">${v}</div>
      ${sub ? `<div class="sub">${sub}</div>` : ""}</div>`;
  const c = r.current || {};
  const today = r.period === "day";
  $("record-meta").textContent = `${today ? `today · ${r.date || ""}` : `last ${r.days} days`}`
    + " · filled paper trades only";
  $("record-current").innerHTML = `<b>${today ? "Today" : `Last ${r.days} days`}:</b>
    ${fmtInt(r.closed)} closed · ${fmt(r.win_rate, 0)}% win (${r.wins}W / ${r.losses}L) ·
    <span class="${signClass(r.total_pnl)}">${m(r.total_pnl)} (${signed(r.total_r)}R)</span>
    ${r.open_now ? ` · ${r.open_now} open` : ""}${c.version ? ` · code ${esc(c.version)}` : ""}.
    ${today ? `Saved at the close to <code>journal/daily/${esc(r.date || "")}-record.md</code>
    for the weekly review; every buy and sell, with its reasons, is in the
    <a href="/api/audit" target="_blank" rel="noopener">audit log</a>.` : ""}`;
  $("record-stats").innerHTML = [
    tile("Open now", fmtInt(r.open_now), r.open_symbols.join(", ") || "flat"),
    tile("Closed", fmtInt(r.closed),
         `${r.exits.target} target · ${r.exits.stop} stop · ${r.exits.time} time`),
    tile("Win rate", `${fmt(r.win_rate, 0)}%`,
         `${r.wins}W / ${r.losses}L${r.flat ? ` / ${r.flat} flat` : ""}`),
    tile("Total P&amp;L", m(r.total_pnl), `${signed(r.total_r)}R`, signClass(r.total_pnl)),
    tile("Avg win", m(r.avg_win), "", "pos"),
    tile("Avg loss", m(r.avg_loss), "", "neg"),
    tile("Expectancy", m(r.expectancy), `per trade · ${signed(r.avg_r)}R`,
         signClass(r.expectancy)),
  ].join("");
  $("record-trades").innerHTML = r.trades.length ? `
    <table><thead><tr><th>Closed</th><th>Symbol</th><th>Side</th>
      <th class="num">Qty</th><th class="num">Entry</th><th class="num">Exit</th>
      <th class="num">R</th><th class="num">P&amp;L</th><th>Why it sold</th></tr></thead><tbody>
    ${r.trades.map((t) => `<tr>
      <td>${t.closed ? new Date(t.closed).toLocaleString("en-GB", { timeZone: marketTz(),
          day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : "—"}</td>
      <td>${esc(t.symbol)}</td>
      <td class="${t.side === "BUY" ? "pos" : "neg"}">${esc(t.side)}</td>
      <td class="num">${fmtInt(t.quantity)}</td>
      <td class="num">${fmt(t.entry)}</td><td class="num">${fmt(t.exit)}</td>
      <td class="num ${signClass(t.r_multiple)}">${signed(t.r_multiple)}R</td>
      <td class="num ${signClass(t.pnl)}">${m(t.pnl)}</td>
      <td class="why-cell">${esc(t.exit_reason)}</td></tr>`).join("")}
    </tbody></table>`
    : `<div class="empty">No paper trades closed ${today ? "today" : `in the last ${r.days} days`}${
        r.alerts_not_traded ? ` — ${r.alerts_not_traded} alert(s) on unarmed days were
        followed but never bought` : ""}.</div>`;
}

async function loadRecord() {
  try {
    const res = await fetch("/api/paper-record?period=day");
    if (res.ok) renderRecord(await res.json());
  } catch { /* the next poll fills it in */ }
}

/* ====================================================================== */
/* Focus list                                                             */
/* ====================================================================== */
function renderFocus(f) {
  if (!f) return;
  const bands = Object.entries(f.bands || {});
  const when = (iso) => iso ? new Date(iso).toLocaleTimeString("en-GB",
    { timeZone: marketTz(), hour: "2-digit", minute: "2-digit" }) : "—";
  $("focus-meta").textContent = f.enabled
    ? `top ${f.per_band} of each band · ${f.ranked} ranked at ${when(f.updated_at)} ${tzLabel()}`
      + ` · next ${when(f.next_rerank_at)}`
    : "off — every name is scanned";
  if (!bands.length) return;
  $("focus-body").innerHTML = bands.map(([band, rows]) => `
    <div class="focus-band"><span class="focus-label">Band ${esc(band)}</span>
      ${rows.map((r) => `<span class="pill ${r.score > 0 ? "up" : r.score < 0 ? "down" : ""}${
        r.tradeable ? " ready" : ""}" title="${esc(r.why)}">${esc(r.symbol)} ${signed(r.score)}${
        r.tradeable ? " ●" : ""}</span>`).join("")}
    </div>`).join("") + `<div class="focus-note">● passes the vote now · hover a name for
      the reading behind it</div>`;
}

async function loadFocus() {
  try {
    const res = await fetch("/api/focus");
    if (res.ok) renderFocus(await res.json());
  } catch { /* the next event or poll fills it in */ }
}

/* ====================================================================== */
/* Live candidate                                                         */
/* ====================================================================== */
/* The symbol the desk is on, as a 5m chart with the lines the strategies
   lean on, and the newest trade the risk desk judged — taken or refused —
   with its entry, stop and target drawn on the price axis. */
let candChart = null;
const candSeries = {};
let candLines = [];
let candChartFor = "";
let candFlashed = "";
let candTz = "Asia/Kolkata";

const _candParts = (epoch, opts) =>
  new Intl.DateTimeFormat("en-GB", { timeZone: candTz, ...opts })
    .format(new Date(epoch * 1000));
/* Keyed on the EXCHANGE's day and clock: watching New York from India, the
   viewer's own day rolls over mid-session and the VWAP would reset there. */
const candDay = (t) => _candParts(t, { year: "numeric", month: "2-digit", day: "2-digit" });
const candClock = (t) => _candParts(t, { hour: "2-digit", minute: "2-digit", hour12: false });

function candEma(values, n) {
  const k = 2 / (n + 1);
  let prev = 0;
  return values.map((v, i) => (prev = i ? v * k + prev * (1 - k) : v));
}

function initCandChart() {
  const el = $("cand-chart");
  if (!el || candChart || typeof LightweightCharts === "undefined") return;
  const css = getComputedStyle(document.documentElement);
  const muted = css.getPropertyValue("--text-muted").trim() || "#898781";
  const grid = css.getPropertyValue("--grid").trim() || "#2c2c2a";
  candChart = LightweightCharts.createChart(el, {
    layout: { background: { color: "transparent" }, textColor: muted, fontSize: 10 },
    grid: { vertLines: { color: grid }, horzLines: { color: grid } },
    rightPriceScale: { borderColor: grid },
    timeScale: { borderColor: grid, timeVisible: true, secondsVisible: false,
                 tickMarkFormatter: (t) => candClock(t) },
    localization: { timeFormatter: (t) => `${candClock(t)} ${tzLabel()}` },
    crosshair: { mode: 0 },
    height: 320,
  });
  candSeries.candles = candChart.addCandlestickSeries({
    upColor: "#0ca30c", downColor: "#d03b3b", borderUpColor: "#0ca30c",
    borderDownColor: "#d03b3b", wickUpColor: "#0ca30c", wickDownColor: "#d03b3b",
    // The price axis stretches to the entry, stop and target, so a target
    // above today's range is still on screen.
    autoscaleInfoProvider: (base) => {
      const r = base();
      if (!r || !candLevels.length) return r;
      r.priceRange.minValue = Math.min(r.priceRange.minValue, ...candLevels);
      r.priceRange.maxValue = Math.max(r.priceRange.maxValue, ...candLevels);
      return r;
    },
  });
  const line = (color, width) => candChart.addLineSeries(
    { color, lineWidth: width, priceLineVisible: false, lastValueVisible: false });
  candSeries.ema9 = line("#4c8dff", 1);
  candSeries.ema21 = line("#c77dff", 1);
  candSeries.ema50 = line("#8b949e", 1);
  candSeries.ema200 = line("#2dd4bf", 2);
  candSeries.vwap = line("#d29922", 2);
  new ResizeObserver(() => candChart.applyOptions({ width: el.clientWidth })).observe(el);
}

/* Entry, stop and target as dashed lines — on the UNDERLYING only: an
   option's premiums do not belong on the stock's price axis. */
let candLevels = [];

function drawCandLevels(c, bars = []) {
  if (!candSeries.candles) return;
  candLines.forEach((l) => candSeries.candles.removePriceLine(l));
  candLines = [];
  candSeries.candles.setMarkers([]);
  candLevels = c ? [c.spot, c.underlying_stop, c.underlying_target]
    .filter((x) => x).map(Number) : [];
  if (!c) return;
  const add = (price, color, title) => {
    if (!price) return;
    candLines.push(candSeries.candles.createPriceLine({
      price: Number(price), color, lineWidth: 1, lineStyle: 2,
      axisLabelVisible: true, title }));
  };
  add(c.spot, "#8b949e", "entry");
  add(c.underlying_stop, "#d03b3b", "stop");
  add(c.underlying_target, "#0ca30c", "target");
  // An arrow on the bar the trade was judged on.
  const at = Math.floor(new Date(c.ts).getTime() / 1000);
  const bar = [...bars].reverse().find((b) => b.time <= at);
  if (bar) {
    const long = c.direction === "LONG";
    candSeries.candles.setMarkers([{ time: bar.time,
      position: long ? "belowBar" : "aboveBar", color: long ? "#0ca30c" : "#d03b3b",
      shape: long ? "arrowUp" : "arrowDown", text: c.action }]);
  }
}

async function loadCandChart(symbol, c) {
  if (!symbol) return;
  initCandChart();
  if (!candChart) return;
  let d;
  try {
    const res = await fetch(`/api/market/${encodeURIComponent(symbol)}/candles` +
                            "?timeframe=5m&session=true&warmup=600");
    if (!res.ok) return;
    d = await res.json();
  } catch { return; }
  if (d.timezone) candTz = d.timezone;
  // Sorted and de-duplicated: the library blanks on a non-monotonic series.
  const clean = (list) => {
    const seen = new Set();
    return (list || []).filter((x) => (seen.has(x.time) ? false : seen.add(x.time)))
      .sort((a, b) => a.time - b.time);
  };
  const bars = clean(d.candles);
  if (!bars.length) return;
  // Today is drawn; the bars before it make the lines right (an EMA 200
  // started from nine bars of today is not an EMA 200).
  const all = clean([...(d.warmup || []), ...bars]);
  const first = bars[0].time;
  const closes = all.map((x) => x.close);
  const asLine = (arr) => all.map((x, i) => ({ time: x.time, value: arr[i] }))
    .filter((p) => p.time >= first);
  candSeries.candles.setData(bars);
  candSeries.ema9.setData(asLine(candEma(closes, 9)));
  candSeries.ema21.setData(asLine(candEma(closes, 21)));
  candSeries.ema50.setData(asLine(candEma(closes, 50)));
  candSeries.ema200.setData(asLine(candEma(closes, 200)));
  let pv = 0, vol = 0, day = null;
  candSeries.vwap.setData(asLine(all.map((x) => {
    const dd = candDay(x.time);
    if (dd !== day) { pv = 0; vol = 0; day = dd; }
    const v = x.volume || 1;
    pv += ((x.high + x.low + x.close) / 3) * v; vol += v;
    return pv / vol;
  })));
  drawCandLevels(c, bars);
  if (candChartFor !== symbol) candChart.timeScale().fitContent();
  candChartFor = symbol;
  $("cand-symbol").textContent = `${symbol} · 5m · ${d.session_only ? "today" : "recent"}`;
  const last = bars[bars.length - 1].time;
  const age = Math.round((Date.now() - last * 1000) / 60000);
  $("cand-freshness").textContent = age <= 6
    ? `live · last bar ${candClock(last)} ${tzLabel()}`
    : `last bar ${candClock(last)} ${tzLabel()} (${age}m ago)`;
}

function renderCandidate(d) {
  const c = d.candidate;
  const scanning = d.scanning || [];
  $("cand-meta").textContent = scanning.length
    ? `checking ${scanning.slice(0, 8).join(", ")}${scanning.length > 8 ? ` +${scanning.length - 8}` : ""}`
    : "between cycles";
  if (!c) {
    $("cand-why").innerHTML = `<div class="empty">No trade put to the risk desk yet
      today.${scanning.length ? ` Checking ${esc(scanning.join(", "))}.` : ""}</div>`;
    if (scanning.length) loadCandChart(scanning[0], null);
    return;
  }
  const long = c.direction === "LONG";
  const when = new Date(c.ts).toLocaleTimeString("en-GB",
    { timeZone: marketTz(), hour: "2-digit", minute: "2-digit" });
  const status = c.taken
    ? `<span class="pill up">taken</span>`
    : `<span class="pill down">refused</span>`;
  const levels = c.option
    ? [["Entry (premium)", c.entry], ["Stop (premium)", c.stop], ["Target (premium)", c.target]]
    : [["Entry", c.entry], ["Stop", c.stop], ["Target", c.target]];
  $("cand-why").innerHTML = `
    <div class="side ${long ? "long" : "short"}">${esc(c.action)} · ${esc(c.symbol)} ${status}</div>
    <div class="levels">${levels.map(([k, v]) =>
      `<div><div class="k">${esc(k)}</div>${fmt(v)}</div>`).join("")}</div>
    <p><b>${esc(c.instrument)}</b>${c.setup ? ` · <b>${esc(c.setup)}</b>` : ""}
      · reward:risk <b>${fmt(c.risk_reward, 1)}</b> · ${esc(when)} ${esc(tzLabel())}
      ${c.hold_overnight ? " · held overnight (1-4 days)" : ""}</p>
    ${c.option && c.underlying_stop ? `<p>Underlying stop <b>${fmt(c.underlying_stop)}</b>
      (spot at entry ${fmt(c.spot)}).</p>` : ""}
    ${c.rationale ? `<p><b>The case.</b> ${esc(c.rationale)}</p>` : ""}
    ${(c.confirmations || []).length ? `<ul>${c.confirmations.map((x) =>
      `<li>${esc(x)}</li>`).join("")}</ul>` : ""}
    <p><b>What kills it.</b> ${esc(c.stop_note || `price through the stop ${fmt(c.underlying_stop || c.stop)}`)}</p>
    ${c.counter ? `<p><b>Against it.</b> ${esc(c.counter)}</p>` : ""}
    ${c.taken
      ? `<p>Bought ${fmtInt(c.quantity)} ${esc(c.unit_label || "unit")}${
          c.quantity === 1 || /s$/.test(c.unit_label || "") ? "" : "s"}.</p>`
      : `<p><b>Not taken:</b> ${esc(c.refused || "refused by the risk desk")}</p>`}`;
  loadCandChart(c.symbol, c);
  const key = `${c.symbol}-${c.ts}`;
  if (candFlashed !== key) {
    candFlashed = key;
    const el = $("s-candidate");
    el.classList.remove("flash-long", "flash-short");
    void el.offsetWidth;
    el.classList.add(long ? "flash-long" : "flash-short");
    setTimeout(() => el.classList.remove("flash-long", "flash-short"), 15000);
  }
}

async function loadCandidate() {
  try {
    const res = await fetch("/api/candidate");
    if (res.ok) renderCandidate(await res.json());
  } catch { /* the next poll fills it in */ }
}

/* ====================================================================== */
/* Rules pop-up                                                           */
/* ====================================================================== */
/* Read beside the desk, not instead of it. /api/rules writes the text from
   the live config for the active market, so the numbers are the ones in
   force — a document kept by hand would have drifted by the first edit. */
function renderRules(doc) {
  $("rules-meta").textContent = `${doc.market_name} · live config`;
  const row = (r) => `<tr><td>${esc(r.text)}</td><td class="rv">${esc(r.value)}</td>
    <td class="rs"><code>${esc(r.setting)}</code></td></tr>`;
  $("rules-body").innerHTML = doc.sections.map((sec) => {
    const steps = sec.steps?.length
      ? `<ol>${sec.steps.map((x) => `<li>${esc(x)}</li>`).join("")}</ol>` : "";
    const analysts = (sec.analysts || []).map((a) => `
      <div class="rule-block"><h4>${esc(a.name)} <span class="count">${esc(a.weight)}</span></h4>
        <ul>${a.reads.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div>`).join("");
    const rules = sec.rules?.length
      ? `<table class="rules-table"><thead><tr><th>Rule</th><th>Now</th>
         <th class="rs">Setting</th></tr></thead><tbody>${sec.rules.map(row).join("")}
         </tbody></table>` : "";
    return `<section class="rules-sec"><h3>${esc(sec.title)}</h3>
      ${sec.intro ? `<p>${esc(sec.intro)}</p>` : ""}${steps}${analysts}${rules}</section>`;
  }).join("") + `<p class="rules-foot">To change a rule, name the setting in the
    right-hand column and the new value.</p>`;
}

async function openRules() {
  const dlg = $("rules-dialog");
  if (!dlg.open) dlg.showModal();
  try {
    const res = await fetch("/api/rules");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    renderRules(await res.json());
  } catch (e) {
    $("rules-body").innerHTML = `<div class="empty">Could not load the rules: ${esc(e.message)}</div>`;
  }
}

function closeRules() {
  $("rules-dialog").close();
}

/* ====================================================================== */
/* Boot                                                                   */
/* ====================================================================== */
async function loadHistory() {
  const res = await fetch("/api/signals?limit=40");
  if (!res.ok) return;
  const { signals } = await res.json();
  const safeParse = (s) => { try { return JSON.parse(s || "[]"); } catch { return []; } };
  state.signals = signals.map((row) => {
    let payload = {};
    try { payload = JSON.parse(row.payload || "{}"); } catch { /* ignore */ }
    return { ...payload, ...row,
             confirmations: safeParse(row.confirmations),
             rejection_reasons: safeParse(row.rejection_reasons) };
  });
  renderSignals();
}

function bind() {
  $("btn-theme").onclick = () => {
    const root = document.documentElement;
    root.dataset.theme = root.dataset.theme === "light" ? "dark" : "light";
    try { localStorage.setItem("theme", root.dataset.theme); } catch { /* ignore */ }
  };
  $("market-switch").onclick = (e) => {
    const btn = e.target.closest("button[data-market]");
    if (btn) switchMarket(btn.dataset.market);
  };
  $("btn-td-start").onclick = startTradingDay;
  $("btn-day-review").onclick = showDayReport;
  $("btn-rules").onclick = (e) => { e.preventDefault(); openRules(); };
  $("rules-close").onclick = closeRules;
  $("rules-dialog").addEventListener("click", (e) => {
    if (e.target === $("rules-dialog")) closeRules();    // the backdrop
  });
  $("rules-dialog").addEventListener("close", () => {
    if (location.hash === "#rules") history.replaceState(null, "", location.pathname);
  });
  if (location.hash === "#rules") openRules();
  window.addEventListener("hashchange", () => {
    if (location.hash === "#rules") openRules();
  });
  $("btn-weekly").onclick = buildWeekly;
  $("btn-weekly-md").onclick = () => downloadWeekly("md");
  $("btn-weekly-json").onclick = () => downloadWeekly("json");
  $("btn-weekly-save").onclick = saveWeekly;
  $("log-decisions").onchange = renderLog;
  $("notify-trades").checked = notifyWanted();
  $("notify-trades").onchange = (e) => toggleNotify(e.target.checked);
}

(async function boot() {
  try {
    const saved = localStorage.getItem("theme");
    if (saved) document.documentElement.dataset.theme = saved;
  } catch { /* ignore */ }

  bind();
  renderLog();
  await loadMarkets();       // currency, timezone and theme before first render
  await Promise.all([loadHistory(), loadStatus(), loadPositions(), loadTradingDay(),
                     loadFocus(), loadRecord(), loadCandidate()]);
  connect();
  setInterval(loadPositions, 30_000);
  setInterval(loadFocus, 60_000);
  setInterval(loadCandidate, 15_000);
  setInterval(loadRecord, 60_000);
  setInterval(renderMarketClock, 15_000);
  setInterval(loadTradingDay, 30_000);
})();
