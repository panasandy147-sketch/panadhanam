/* panaoptions dashboard — read-only.
   Every panel answers one question, and the configuration panel comes first
   because a desk that is scanning and taking nothing is indistinguishable
   from a quiet market until something says which it is. */

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let CURRENCY = "$";
const money = (n) => `${CURRENCY}${Number(n || 0).toLocaleString("en-US",
  { minimumFractionDigits: 0, maximumFractionDigits: 0 })}`;
const num = (n, d = 2) => Number(n || 0).toFixed(d);
const sign = (n) => (Number(n) > 0 ? "pos" : Number(n) < 0 ? "neg" : "");

async function getJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${url} → ${res.status}`);
  return res.json();
}

/* ------------------------------------------------------------------ */
function renderConfig(c) {
  const blockers = c.blockers || [];
  const warnings = c.warnings || [];

  if (!blockers.length && !warnings.length) {
    $("config-body").innerHTML =
      `<div class="note"><b>Every rule can hold at once.</b>
       Nothing in the configuration will stop a trade.</div>`;
    return;
  }

  const one = (f, level) => `
    <div class="note ${level}">
      <b>${level === "crit" ? "BLOCKER" : "Warning"} — ${esc(f.setting)}</b>
      ${esc(f.problem)}
      <div class="fix">${esc(f.fix)}</div>
      ${f.command ? `<pre>${esc(f.command)}</pre>` : ""}
    </div>`;

  $("config-body").innerHTML =
    blockers.map((f) => one(f, "crit")).join("") +
    warnings.map((f) => one(f, "warn")).join("") +
    (blockers.length
      ? `<div class="empty">The desk keeps scanning, but no setup can become a
         trade while a blocker stands. Run the command above, then restart.</div>`
      : "");
}

function renderAccount(s) {
  const c = s.config || {};
  const r = s.risk || {};
  $("account-tiles").innerHTML = `
    <div class="tile"><div class="k">Capital</div>
      <div class="v">${money(c.capital)}</div></div>
    <div class="tile"><div class="k">Deployed per trade</div>
      <div class="v">${money(c.deployed_per_trade)}</div>
      <div class="sub">${num(c.deployed_pct, 0)}% of the account</div></div>
    <div class="tile"><div class="k">At risk per trade</div>
      <div class="v">${money(c.risk_per_trade)}</div>
      <div class="sub">${num(c.risk_per_trade_pct, 1)}% behind the stop</div></div>
    <div class="tile"><div class="k">Day P&amp;L</div>
      <div class="v ${sign(r.realised_pnl)}">${money(r.realised_pnl)}</div>
      <div class="sub">${r.wins || 0}W / ${r.losses || 0}L</div></div>
    <div class="tile"><div class="k">Room before halt</div>
      <div class="v">${money(r.remaining_loss_budget)}</div>
      <div class="sub">limit ${money(r.daily_loss_limit)}</div></div>
    <div class="tile"><div class="k">Entry window</div>
      <div class="v" style="font-size:15px">${esc(s.session?.entry_open)}–${esc(s.session?.entry_close)}</div>
      <div class="sub">square off ${esc(s.session?.force_exit_at)}</div></div>`;
}

function renderScreen(d) {
  const reads = d.reads || [];
  if (!reads.length) {
    $("screen-body").innerHTML =
      `<div class="empty">Not run yet today. It fires once, before the entry
       window opens.</div>`;
    return;
  }
  const t = d.thresholds || {};
  const rows = reads
    .slice()
    .sort((a, b) => (b.passed - a.passed) || (Math.abs(b.gap_pct) - Math.abs(a.gap_pct)))
    .map((r) => `
      <tr>
        <td>${esc(r.symbol)}</td>
        <td class="num">${num(r.last_price)}</td>
        <td class="num ${sign(r.gap_pct)}">${r.gap_pct >= 0 ? "+" : ""}${num(r.gap_pct)}%</td>
        <td class="num">${num(r.rvol)}</td>
        <td><span class="pill ${r.passed ? "pass" : "skip"}">${r.passed ? "pass" : "skip"}</span></td>
      </tr>`).join("");

  $("screen-body").innerHTML = `
    <table>
      <thead><tr><th>Symbol</th><th class="num">Last</th><th class="num">Gap</th>
      <th class="num">RVOL</th><th>Verdict</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <div class="empty">Needs RVOL ≥ ${t.min_rvol} and a gap of at least
      ±${t.min_gap_pct}%.</div>`;
}

function renderContracts(d) {
  const rows = (d.rows || []).map((r) => `
    <tr>
      <td>${esc(r.symbol)}</td>
      <td class="num">${num(r.spot, 0)}</td>
      <td class="num">${money(r.atm_cost)}</td>
      <td><span class="pill ${r.affordable ? "pass" : "skip"}">${r.affordable ? "fits" : "too dear"}</span></td>
    </tr>`).join("");

  $("contracts-body").innerHTML = `
    <table>
      <thead><tr><th>Symbol</th><th class="num">Spot</th>
      <th class="num">ATM contract</th><th>Budget</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <div class="empty">At ${d.delta_band?.[0]}–${d.delta_band?.[1]} delta — at the
      money — against a ${money(d.budget)} budget. One contract is 100 shares,
      which is why these cost what they do. Estimates; use
      <code>--explain-contracts</code> for live chains.</div>`;
}

function renderOpen(s) {
  const open = s.open_trades || [];
  if (!open.length) {
    $("open-body").innerHTML = `<div class="empty">Flat.</div>`;
    return;
  }
  $("open-body").innerHTML = `
    <table>
      <thead><tr><th>Contract</th><th>Side</th><th class="num">Qty</th>
      <th class="num">Entry</th><th class="num">Stop</th><th class="num">TP1</th>
      <th class="num">TP2</th><th class="num">Banked</th></tr></thead>
      <tbody>${open.map((t) => `
        <tr>
          <td>${esc(t.contract_label)}</td>
          <td class="${t.direction === "LONG" ? "pos" : "neg"}">${esc(t.direction)}</td>
          <td class="num">${t.remaining}</td>
          <td class="num">${num(t.entry_price)}</td>
          <td class="num neg">${num(t.stop_price)}</td>
          <td class="num pos">${num(t.target_1)}</td>
          <td class="num pos">${num(t.target_2)}</td>
          <td class="num ${sign(t.realised_pnl)}">${money(t.realised_pnl)}</td>
        </tr>`).join("")}</tbody>
    </table>`;
}

function renderLedger(d) {
  const s = d.stats || {};
  $("ledger-tiles").innerHTML = `
    <div class="tile"><div class="k">Trades</div><div class="v">${s.trades || 0}</div></div>
    <div class="tile"><div class="k">Win rate</div><div class="v">${num(s.win_rate, 0)}%</div></div>
    <div class="tile"><div class="k">Total P&amp;L</div>
      <div class="v ${sign(s.total_pnl)}">${money(s.total_pnl)}</div></div>
    <div class="tile"><div class="k">Avg win</div><div class="v pos">${money(s.avg_win)}</div></div>
    <div class="tile"><div class="k">Avg loss</div><div class="v neg">${money(s.avg_loss)}</div></div>
    <div class="tile"><div class="k">Expectancy</div>
      <div class="v ${sign(s.expectancy)}">${money(s.expectancy)}</div>
      <div class="sub">per trade</div></div>`;

  const rows = (d.trades || []).slice(0, 25).map((t) => `
    <tr>
      <td>${esc(t.session_date)}</td>
      <td>${esc(t.symbol)}</td>
      <td>${esc(t.contract)}</td>
      <td>${esc(t.exit_reason || "")}</td>
      <td class="num ${sign(t.realised_pnl)}">${money(t.realised_pnl)}</td>
    </tr>`).join("");

  $("trades-body").innerHTML = rows
    ? `<table><thead><tr><th>Date</th><th>Symbol</th><th>Contract</th>
       <th>Exit</th><th class="num">P&amp;L</th></tr></thead>
       <tbody>${rows}</tbody></table>`
    : `<div class="empty">No closed trades in the last ${d.days} days.</div>`;
}

function renderRejections(d) {
  const entries = Object.entries(d.rejections || {});
  if (!entries.length) {
    $("rejections-body").innerHTML =
      `<div class="empty">Nothing passed over yet.</div>`;
    return;
  }
  $("rejections-body").innerHTML = `
    <table>
      <thead><tr><th class="num">Count</th><th>Reason the setup did not become a trade</th></tr></thead>
      <tbody>${entries.slice(0, 12).map(([reason, count]) => `
        <tr><td class="num">${count}</td><td>${esc(reason)}</td></tr>`).join("")}</tbody>
    </table>
    <div class="empty">If one reason dominates, that is the rule to look at —
      a selective rule and an impossible one look the same from the outside.</div>`;
}

/* ====================================================================== */
/* Live candidate: the chart, and the case for the trade                  */
/* ====================================================================== */
let candChart = null;
let candSeries = {};
let lastFlashed = null;

function ema(values, period) {
  const k = 2 / (period + 1);
  let prev = values[0];
  return values.map((v, i) => (prev = i ? v * k + prev * (1 - k) : v));
}

function initCandChart() {
  const el = $("cand-chart");
  if (!el || candChart || typeof LightweightCharts === "undefined") return;
  candChart = LightweightCharts.createChart(el, {
    layout: { background: { color: "transparent" }, textColor: "#8b949e",
              fontSize: 10 },
    grid: { vertLines: { color: "#20272f" }, horzLines: { color: "#20272f" } },
    rightPriceScale: { borderColor: "#283039" },
    timeScale: { borderColor: "#283039", timeVisible: true, secondsVisible: false },
    crosshair: { mode: 0 },
    height: 320,
  });
  candSeries.candles = candChart.addCandlestickSeries({
    upColor: "#3fb950", downColor: "#f85149",
    borderUpColor: "#3fb950", borderDownColor: "#f85149",
    wickUpColor: "#3fb950", wickDownColor: "#f85149",
  });
  const line = (color, width) =>
    candChart.addLineSeries({ color, lineWidth: width, priceLineVisible: false,
                              lastValueVisible: false });
  candSeries.ema9 = line("#4c8dff", 1);
  candSeries.ema21 = line("#c77dff", 1);
  candSeries.ema50 = line("#8b949e", 1);
  candSeries.vwap = line("#d29922", 2);
  new ResizeObserver(() => candChart.applyOptions({ width: el.clientWidth }))
    .observe(el);
}

async function loadCandChart(symbol) {
  if (!symbol) return;
  initCandChart();
  if (!candChart) return;

  let d;
  try {
    d = await getJSON(`/api/candles/${encodeURIComponent(symbol)}?timeframe=5m`);
  } catch { return; }

  // Sorted and de-duplicated: the charting library silently blanks on a
  // non-monotonic series, which looks like "no data" rather than bad data.
  const seen = new Set();
  const bars = (d.candles || [])
    .filter((c) => (seen.has(c.time) ? false : seen.add(c.time)))
    .sort((a, b) => a.time - b.time);
  if (!bars.length) return;

  candSeries.candles.setData(bars);
  const closes = bars.map((c) => c.close);
  const times = bars.map((c) => c.time);
  const asLine = (arr) => times.map((t, i) => ({ time: t, value: arr[i] }));
  candSeries.ema9.setData(asLine(ema(closes, 9)));
  candSeries.ema21.setData(asLine(ema(closes, 21)));
  candSeries.ema50.setData(asLine(ema(closes, 50)));

  // Session VWAP, reset each day — the same line the strategies lean on.
  let pv = 0, vol = 0, day = null;
  candSeries.vwap.setData(bars.map((c) => {
    const d2 = new Date(c.time * 1000).toDateString();
    if (d2 !== day) { pv = 0; vol = 0; day = d2; }
    const typical = (c.high + c.low + c.close) / 3;
    const v = c.volume || 1;
    pv += typical * v; vol += v;
    return { time: c.time, value: pv / vol };
  }));
  candChart.timeScale().fitContent();

  $("cand-symbol").textContent = `${d.symbol} · 5m`;
  const age = Math.round((Date.now() - bars[bars.length - 1].time * 1000) / 60000);
  const stamp = new Date(bars[bars.length - 1].time * 1000)
    .toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  $("cand-freshness").textContent = age <= 6
    ? `live · last bar ${stamp}` : `last bar ${stamp} (${age}m ago)`;
}

function renderCandidate(d) {
  const c = d.candidate;
  $("cand-meta").textContent = d.scanning
    ? `scanning ${d.scanning}` : (d.watchlist || []).join(", ");

  if (!c) {
    $("cand-why").innerHTML = `<div class="empty">Nothing set up yet.
      ${d.watchlist?.length
        ? `Watching ${esc(d.watchlist.join(", "))}.`
        : "Waiting for the pre-market screen."}</div>`;
    if (d.scanning) loadCandChart(d.scanning);
    return;
  }

  const call = c.right === "CALL";
  $("cand-why").innerHTML = `
    <div class="side ${call ? "call" : "put"}">
      BUY ${esc(c.right)} · ${esc(c.symbol)}
      ${c.taken ? `<span class="pill pass">taken</span>` : ""}
    </div>
    <div class="levels">
      <div><div class="k">Trigger</div>${fmtPrice(c.entry_trigger)}</div>
      <div><div class="k">${esc(c.key_level_source || "level")}</div>${fmtPrice(c.key_level)}</div>
      <div><div class="k">Invalidation</div>${fmtPrice(c.invalidation)}</div>
    </div>
    ${(c.reasoning || []).map((line) => `<p>${bold(line)}</p>`).join("")}
    ${c.delta_band ? `<p style="color:var(--muted)">Contract: delta
      ${c.delta_band[0]} – ${c.delta_band[1]}.</p>` : ""}
    ${c.taken && c.contract ? `<p style="color:var(--muted)">Filled:
      <b>${esc(c.contract)}</b> x${c.quantity} at ${fmtPrice(c.entry)}.</p>`
      : `<p style="color:var(--muted)">Not filled — see the activity log for
         why.</p>`}`;

  loadCandChart(c.symbol);
  flashCandidate(c);
}

const fmtPrice = (n) => (n ? Number(n).toFixed(2) : "—");
/* The reasoning arrives with **bold** markers; render those and escape the
   rest, so a symbol or a note can never inject markup. */
const bold = (line) => esc(line).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");

function flashCandidate(c) {
  const key = `${c.symbol}-${c.ts}`;
  if (lastFlashed === key) return;      // flash a signal once, not every poll
  lastFlashed = key;

  const el = $("s-candidate");
  el.classList.remove("flash-call", "flash-put");
  void el.offsetWidth;                   // restart the animation
  el.classList.add(c.right === "CALL" ? "flash-call" : "flash-put");
  setTimeout(() => el.classList.remove("flash-call", "flash-put"), 15000);
}

async function refreshCandidate() {
  try {
    renderCandidate(await getJSON("/api/candidate"));
  } catch {
    /* the next tick retries */
  }
}

/* ------------------------------------------------------------------ */
function renderStrategies(d) {
  $("strat-meta").textContent = d.market_time || "";
  $("strategies-body").innerHTML = `
    <table>
      <thead><tr><th>Strategy</th><th>Window (ET)</th><th>Status</th></tr></thead>
      <tbody>${(d.strategies || []).map((s) => `
        <tr>
          <td>${esc(s.name)}</td>
          <td class="num">${esc(s.from)} – ${esc(s.to)}</td>
          <td><span class="pill ${s.live ? "pass" : "skip"}">${
            !s.enabled ? "off" : s.live ? "live now" : "outside window"}</span></td>
        </tr>`).join("")}</tbody>
    </table>
    <div class="empty">Each runs only inside its own window. They all skip
      09:30–09:45, where spreads are widest and the first prints are noise.</div>`;
}

function renderJournal(d) {
  const s = d.stats || {};
  const c = d.coach || {};
  $("journal-meta").textContent =
    `last ${d.days} days · cards by ${c.writing_cards ? esc(c.model) : "the rules"}`;

  // A coach that is switched on but not answering looks exactly like one that
  // is working, unless the page says so.
  $("journal-coach").innerHTML = (c.configured && !c.writing_cards)
    ? `<div class="note warn"><b>${esc(c.model)} is configured but not
       answering.</b> ${esc(c.down_reason || "")} Cards are still being written
       and graded — by the rules rather than the model. Check with
       <code>run.py --check-llm</code>.</div>`
    : "";
  $("journal-tiles").innerHTML = `
    <div class="tile"><div class="k">Graded</div><div class="v">${s.total || 0}</div></div>
    <div class="tile"><div class="k">Clean execution</div>
      <div class="v">${num(s.clean_pct, 0)}%</div>
      <div class="sub">no rule broken</div></div>
    <div class="tile"><div class="k">Discipline</div>
      <div class="v">${num(s.avg_execution_score, 1)}/10</div></div>
    <div class="tile"><div class="k">P&amp;L</div>
      <div class="v ${sign(s.total_pnl)}">${money(s.total_pnl)}</div></div>
    <div class="tile"><div class="k">Lost to rule breaks</div>
      <div class="v neg">${money(s.mistake_cost)}</div>
      <div class="sub">${num(s.avoidable_loss_pct, 0)}% of losses avoidable</div></div>
    <div class="tile"><div class="k">Bad wins</div>
      <div class="v ${(s.by_verdict || {}).BAD_WIN ? "neg" : ""}">${
        (s.by_verdict || {}).BAD_WIN || 0}</div>
      <div class="sub">paid, but broke a rule</div></div>`;

  const byStrategy = Object.entries(s.by_strategy || {});
  const strategies = byStrategy.length ? `
    <div style="padding:10px 14px 0" class="k">Which strategy is paying</div>
    <table>
      <thead><tr><th>Strategy</th><th class="num">Trades</th><th class="num">Win rate</th>
      <th class="num">P&amp;L</th><th class="num">Discipline</th></tr></thead>
      <tbody>${byStrategy
        .sort((a, b) => b[1].total_pnl - a[1].total_pnl)
        .map(([name, v]) => `
          <tr><td>${esc(name)}</td>
            <td class="num">${v.count}</td>
            <td class="num">${num(v.win_rate, 0)}%</td>
            <td class="num ${sign(v.total_pnl)}">${money(v.total_pnl)}</td>
            <td class="num">${num(v.avg_score, 1)}/10</td></tr>`).join("")}
      </tbody>
    </table>` : "";

  const rows = (d.entries || []).slice(0, 20).map((e) => `
    <tr>
      <td>${esc((e.ts || "").slice(0, 10))}</td>
      <td>${esc(e.symbol)}</td>
      <td>${esc(e.strategy)}</td>
      <td>${esc(e.exit_reason)}</td>
      <td class="num ${sign(e.pnl)}">${money(e.pnl)}</td>
      <td><span class="verdict ${esc(e.verdict)}">${esc(e.verdict)}</span></td>
      <td class="num">${e.execution_score}/10</td>
    </tr>`).join("");

  $("journal-body").innerHTML = strategies + (rows
    ? `<div style="padding:10px 14px 0" class="k">Graded trades</div>
       <table><thead><tr><th>Date</th><th>Symbol</th><th>Strategy</th>
       <th>Exit</th><th class="num">P&amp;L</th><th>Verdict</th>
       <th class="num">Score</th></tr></thead><tbody>${rows}</tbody></table>
       <div class="empty">Process over outcome. <b>BAD_WIN</b> means it paid
         while breaking a rule — the P&amp;L is rewarding a habit that will
         eventually cost you.</div>`
    : `<div class="empty">No graded trades yet. Every closed trade is graded
        automatically the moment it closes.</div>`);
}

async function buildReview(period) {
  const daily = period === "day";
  const btn = $(daily ? "btn-daily" : "btn-weekly");
  const status = $(daily ? "daily-status" : "weekly-status");
  const body = daily ? "daily-body" : "weekly-body";
  const coach = $(daily ? "d-coach" : "w-coach").checked;

  btn.disabled = true;
  status.textContent = coach
    ? "Reading and asking the model…" : "Reading…";
  try {
    const d = await getJSON(`/api/${daily ? "daily" : "weekly"}?coach=${coach}`);
    renderReview(d, body);
    $(daily ? "btn-daily-md" : "btn-weekly-md").disabled = false;
    status.textContent = "";
  } catch (e) {
    $(body).innerHTML = `<div class="note crit">${esc(e.message)}</div>`;
    status.textContent = "";
  } finally {
    btn.disabled = false;
  }
}

function renderReview(r, target) {
  const s = r.stats || {};
  const daily = r.period === "day";
  const c = r.coach;
  const list = (xs) => (xs || []).map((x) => `<li>${esc(x)}</li>`).join("");
  const provisional = r.complete ? "" :
    `<div class="note warn"><b>This ${daily ? "session is not over" : "week is not finished"}.</b>
     These numbers are provisional.</div>`;

  if (!s.total) {
    $(target).innerHTML = `${provisional}
      <div class="empty">No graded trades ${daily
        ? `on ${esc(r.week_start)}`
        : `in ${esc(r.week_start)} – ${esc(r.week_end)}`}.</div>`;
    return;
  }

  $(target).innerHTML = `${provisional}
    <div class="tiles">
      <div class="tile"><div class="k">Trades</div><div class="v">${s.total}</div></div>
      <div class="tile"><div class="k">Win rate</div><div class="v">${num(s.win_rate, 0)}%</div></div>
      <div class="tile"><div class="k">P&amp;L</div>
        <div class="v ${sign(s.total_pnl)}">${money(s.total_pnl)}</div></div>
      <div class="tile"><div class="k">Clean</div><div class="v">${num(s.clean_pct, 0)}%</div></div>
      <div class="tile"><div class="k">Discipline</div>
        <div class="v">${num(s.avg_execution_score, 1)}/10</div></div>
    </div>
    ${c ? `
      <div style="padding:12px 14px">
        <p style="margin:0 0 8px"><b>${esc(c.headline)}</b></p>
        ${c.what_worked?.length ? `<div class="k">What worked</div><ul>${list(c.what_worked)}</ul>` : ""}
        ${c.what_cost_money?.length ? `<div class="k">What cost money</div><ul>${list(c.what_cost_money)}</ul>` : ""}
        ${c.rule_changes?.length ? `
          <div class="k">Proposed changes</div>
          <table><thead><tr><th>Rule</th><th>Change</th><th>Evidence</th></tr></thead>
          <tbody>${c.rule_changes.map((x) => `<tr><td>${esc(x.rule)}</td>
            <td>${esc(x.change)}</td><td>${esc(x.why)}</td></tr>`).join("")}</tbody></table>
          <p style="font-size:11.5px;color:var(--muted)">Suggestions only —
            <b>nothing here has been applied</b>. No model can change a stop, a
            size, or a strategy&#39;s window.</p>` : ""}
        <div class="k" style="margin-top:8px">Focus next week</div>
        <p style="margin:4px 0">${esc(c.focus_next_week)}</p>
        <p style="font-size:11px;color:var(--muted);margin-top:8px">
          Graded by ${esc(c.generated_by)}.</p>
      </div>` : ""}`;
}

function renderActivity(d) {
  const events = d.events || [];
  $("activity-meta").textContent = events.length
    ? `${events.length} of the last ${d.count}` : "";
  $("activity-body").innerHTML = events.length
    ? events.map((e) => `
        <div class="log-row ${esc(e.level)}">
          <span class="t">${esc(e.time)}</span>
          <span class="k">${esc(e.kind)}</span>
          <span class="d">${esc(e.detail)}</span>
        </div>`).join("")
    : `<div class="empty">Nothing yet. The desk logs every cycle, every
       symbol it judges, and the reason it passed.</div>`;
}

/* The log is the one panel worth polling faster — it is what tells you the
   desk is alive between trades. */
async function refreshActivity() {
  try {
    renderActivity(await getJSON("/api/activity?limit=60"));
  } catch {
    /* the next tick will retry; a log gap is not worth an error banner */
  }
}

/* ------------------------------------------------------------------ */
async function refresh() {
  try {
    const s = await getJSON("/api/status");
    CURRENCY = s.config?.currency || "$";

    $("b-phase").textContent = s.phase || "—";
    $("b-clock").textContent = s.market_time || "—";
    $("b-capital").textContent = money(s.config?.capital);

    const halted = s.risk?.halted;
    $("b-halt").hidden = !halted;
    if (halted) $("b-halt").title = s.risk.halt_reason || "";

    renderConfig(s.config || {});
    renderAccount(s);
    renderOpen(s);

    const [screen, trades, contracts, strategies, journal] = await Promise.all([
      getJSON("/api/screen"), getJSON("/api/trades?days=30"),
      getJSON("/api/contracts"), getJSON("/api/strategies"),
      getJSON("/api/journal?days=30"),
    ]);
    renderScreen(screen);
    renderLedger(trades);
    renderRejections(trades);
    renderContracts(contracts);
    renderStrategies(strategies);
    renderJournal(journal);
  } catch (e) {
    $("config-body").innerHTML =
      `<div class="note crit"><b>Cannot reach the desk</b>${esc(e.message)}</div>`;
  }
}

$("btn-weekly").onclick = () => buildReview("week");
$("btn-weekly-md").onclick = () => {
  window.location.href = "/api/weekly/download?format=md";
};
$("btn-daily").onclick = () => buildReview("day");
$("btn-daily-md").onclick = () => {
  window.location.href = "/api/daily/download?format=md";
};

refresh();
refreshActivity();
refreshCandidate();
setInterval(refresh, 15000);
setInterval(refreshActivity, 5000);
// The candidate and its chart are what you actually watch, so they lead.
setInterval(refreshCandidate, 5000);
