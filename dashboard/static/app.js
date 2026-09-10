const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    ...opts,
  });
  if (res.status === 401) {
    showLogin();
    throw new Error("not authenticated");
  }
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || `HTTP ${res.status}`);
  }
  return res.status === 204 ? null : res.json();
}

function showLogin() {
  $("#login-screen").hidden = false;
  $("#app").hidden = true;
}

function showApp() {
  $("#login-screen").hidden = true;
  $("#app").hidden = false;
  refreshAll();
}

// --- Login ---

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#login-error").textContent = "";
  try {
    await api("/api/login", {
      method: "POST",
      body: JSON.stringify({
        username: $("#login-username").value,
        password: $("#login-password").value,
      }),
    });
    showApp();
  } catch (err) {
    $("#login-error").textContent = "Login failed. Check your username and password.";
  }
});

$("#logout-btn").addEventListener("click", async () => {
  await api("/api/logout", { method: "POST" });
  showLogin();
});

// --- Tabs ---

$$("nav.tabs button").forEach((btn) => {
  btn.addEventListener("click", () => {
    $$("nav.tabs button").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    ["activity", "scans", "summary", "rules", "logs"].forEach((tab) => {
      $(`#tab-${tab}`).hidden = tab !== btn.dataset.tab;
    });
  });
});

// --- Status pills ---

async function refreshStatus() {
  const s = await api("/api/status");
  const pnl = s.daily_pnl != null ? s.daily_pnl.toFixed(2) : "—";
  const equity = s.equity != null ? s.equity.toFixed(2) : "—";

  let pdtPill = "";
  if (s.day_trade_count != null && s.equity != null && s.equity < 25000) {
    const atLimit = s.day_trade_count >= 3;
    pdtPill = `<span class="pill ${atLimit ? "bad" : "warn"}" title="Account-wide PDT limit - shared with your other bot">
      Day trades (5d): ${s.day_trade_count}/3</span>`;
  }

  $("#status-pills").innerHTML = `
    <span class="pill ${s.mode === "live" ? "warn" : "ok"}">${s.mode.toUpperCase()}</span>
    <span class="pill ${s.broker_connected ? "ok" : "bad"}">${s.broker_connected ? "Alpaca Connected" : "Alpaca Disconnected"}</span>
    <span class="pill ${s.kill_switch ? "bad" : "ok"}">${s.kill_switch ? "Kill Switch ON" : "Trading Active"}</span>
    <span class="pill">Equity $${equity}</span>
    <span class="pill ${s.daily_pnl < 0 ? "bad" : "ok"}">PnL $${pnl}</span>
    <span class="pill">${s.open_positions} open</span>
    ${pdtPill}
  `;
}

// --- Activity tab ---

async function refreshActivity() {
  const [positions, trades] = await Promise.all([api("/api/positions"), api("/api/trades?limit=100")]);

  $("#positions-body").innerHTML = positions.map(p => `
    <tr><td>${p.symbol}</td><td>${p.qty}</td><td>${p.avg_price.toFixed(2)}</td>
    <td>${p.stop_loss ? p.stop_loss.toFixed(2) : "—"}</td><td>${p.take_profit ? p.take_profit.toFixed(2) : "—"}</td>
    <td>${fmtTime(p.opened_at)}</td></tr>
  `).join("") || `<tr><td colspan="6" class="muted">No open positions</td></tr>`;

  $("#trades-body").innerHTML = trades.map(t => `
    <tr><td>${fmtTime(t.ts)}</td><td>${t.symbol}</td>
    <td><span class="tag ${t.side === "BUY" ? "buy" : "sell"}">${t.side}</span></td>
    <td>${t.qty}</td><td>${t.price != null ? t.price.toFixed(2) : "—"}</td>
    <td>${t.status || ""}</td><td class="muted">${t.reason || ""}</td></tr>
  `).join("") || `<tr><td colspan="7" class="muted">No trades yet</td></tr>`;
}

// --- Scans tab ---

let currentScans = [];
let scansSort = { key: "ts", dir: "desc" };

async function refreshScans() {
  currentScans = await api("/api/scans?limit=150");
  renderScans();
}

function renderScans() {
  const filterVal = $("#scans-filter").value;
  let rows = currentScans.filter((s) => {
    if (filterVal === "passed") return !!s.passed;
    if (filterVal === "filtered") return !s.passed;
    return true;
  });

  const { key, dir } = scansSort;
  const mul = dir === "asc" ? 1 : -1;
  rows = [...rows].sort((a, b) => {
    let av = a[key], bv = b[key];
    if (key === "passed") { av = av ? 1 : 0; bv = bv ? 1 : 0; }
    if (av == null) return bv == null ? 0 : 1;
    if (bv == null) return -1;
    if (typeof av === "string") return av.localeCompare(bv) * mul;
    return (av - bv) * mul;
  });

  $("#scans-body").innerHTML = rows.map(s => `
    <tr><td>${fmtTime(s.ts)}</td><td>${s.symbol}</td><td>${s.gap_pct != null ? s.gap_pct.toFixed(2) : "—"}</td>
    <td>${s.price != null ? s.price.toFixed(2) : "—"}</td><td>${s.volume != null ? Math.round(s.volume).toLocaleString() : "—"}</td>
    <td><span class="tag ${s.passed ? "pass" : "fail"}">${s.passed ? "PASSED" : "filtered"}</span></td>
    <td class="muted">${s.notes || ""}</td></tr>
  `).join("") || `<tr><td colspan="7" class="muted">No scans match this filter</td></tr>`;

  $$('#tab-scans th[data-sort]').forEach((th) => {
    const active = th.dataset.sort === scansSort.key;
    th.classList.toggle("sorted-asc", active && scansSort.dir === "asc");
    th.classList.toggle("sorted-desc", active && scansSort.dir === "desc");
  });
}

$("#scans-filter").addEventListener("change", renderScans);

$$('#tab-scans th[data-sort]').forEach((th) => {
  th.addEventListener("click", () => {
    const key = th.dataset.sort;
    if (scansSort.key === key) {
      scansSort.dir = scansSort.dir === "asc" ? "desc" : "asc";
    } else {
      scansSort = { key, dir: key === "ts" ? "desc" : "asc" };
    }
    renderScans();
  });
});

// --- Summary tab ---

let allTrades = [];

async function refreshSummary() {
  allTrades = await api("/api/trades?limit=500");
  renderSummary();
}

function renderSummary() {
  const range = $("#summary-range").value;
  const todayStr = new Date().toISOString().slice(0, 10);
  const rows = allTrades.filter((t) => range === "all" || (t.ts || "").slice(0, 10) === todayStr);

  const buys = rows.filter((t) => t.side === "BUY");
  const sells = rows.filter((t) => t.side === "SELL");

  const dollarValue = (t) => (t.price != null ? t.qty * t.price : null);
  const buyTotal = buys.reduce((sum, t) => sum + (dollarValue(t) || 0), 0);
  const sellTotal = sells.reduce((sum, t) => sum + (dollarValue(t) || 0), 0);
  const pnlKnown = sells.filter((t) => t.pnl != null);
  const pnlTotal = pnlKnown.reduce((sum, t) => sum + t.pnl, 0);

  $("#summary-totals").innerHTML = `
    <span class="pill">Bought: ${buys.length} trade(s), $${buyTotal.toFixed(2)}</span>
    <span class="pill">Sold: ${sells.length} trade(s), $${sellTotal.toFixed(2)}</span>
    <span class="pill ${pnlTotal < 0 ? "bad" : "ok"}">Realized PnL: $${pnlTotal.toFixed(2)}${pnlKnown.length < sells.length ? " (partial - some exits have no recorded fill price)" : ""}</span>
  `;

  $("#summary-bought-body").innerHTML = buys.map(t => `
    <tr><td>${fmtTime(t.ts)}</td><td>${t.symbol}</td><td>${t.qty}</td>
    <td>${t.price != null ? t.price.toFixed(2) : "—"}</td>
    <td>${dollarValue(t) != null ? dollarValue(t).toFixed(2) : "—"}</td>
    <td>${t.status || ""}</td></tr>
  `).join("") || `<tr><td colspan="6" class="muted">No buys in this range</td></tr>`;

  $("#summary-sold-body").innerHTML = sells.map(t => `
    <tr><td>${fmtTime(t.ts)}</td><td>${t.symbol}</td><td>${t.qty}</td>
    <td>${t.price != null ? t.price.toFixed(2) : "—"}</td>
    <td>${dollarValue(t) != null ? dollarValue(t).toFixed(2) : "—"}</td>
    <td class="${t.pnl > 0 ? "pnl-pos" : (t.pnl < 0 ? "pnl-neg" : "")}">${t.pnl != null ? (t.pnl >= 0 ? "+" : "") + t.pnl.toFixed(2) : "—"}</td>
    <td>${t.status || ""}</td></tr>
  `).join("") || `<tr><td colspan="7" class="muted">No sells in this range</td></tr>`;
}

$("#summary-range").addEventListener("change", renderSummary);

// --- Logs tab ---

async function refreshLogs() {
  const logs = await api("/api/logs?limit=300");
  $("#logs-body").innerHTML = logs.map(l => `
    <div class="log-line log-level-${l.level}">[${fmtTime(l.ts)}] ${l.level} ${l.source}: ${l.message}</div>
  `).join("") || `<div class="muted">No log entries yet</div>`;
}

// --- Rules tab ---

function ruleFieldMap(rules) {
  return {
    "#rule-mode": rules.mode,
    "#rule-kill-switch": rules.kill_switch,
    "#sched-premarket": rules.schedule.premarket_scan_time,
    "#sched-window-start": rules.schedule.trading_window_start,
    "#sched-window-end": rules.schedule.trading_window_end,
    "#sched-eod": rules.schedule.eod_summary_time,
    "#sched-interval": rules.schedule.cycle_interval_minutes,
    "#sched-tz": rules.schedule.timezone,
    "#gm-enabled": rules.strategies.gap_momentum.enabled,
    "#gm-min-gap": rules.strategies.gap_momentum.min_gap_pct,
    "#gm-max-gap": rules.strategies.gap_momentum.max_gap_pct,
    "#gm-min-price": rules.strategies.gap_momentum.min_price,
    "#gm-max-price": rules.strategies.gap_momentum.max_price,
    "#gm-min-vol": rules.strategies.gap_momentum.min_avg_volume,
    "#bo-enabled": rules.strategies.breakout.enabled,
    "#bo-lookback": rules.strategies.breakout.lookback_days,
    "#bo-pct": rules.strategies.breakout.breakout_pct,
    "#risk-allocation": rules.risk.capital_allocation_usd,
    "#risk-size": rules.risk.position_size_pct,
    "#risk-maxpos": rules.risk.max_open_positions,
    "#risk-sl": rules.risk.stop_loss_pct,
    "#risk-tp": rules.risk.take_profit_pct,
    "#risk-maxloss": rules.risk.max_daily_loss_pct,
    "#notif-telegram": rules.notifications.telegram_enabled,
    "#notif-trade": rules.notifications.notify_on_trade,
    "#notif-error": rules.notifications.notify_on_error,
    "#notif-eod": rules.notifications.notify_eod_summary,
  };
}

let currentRules = null;

async function refreshRules() {
  currentRules = await api("/api/rules");
  const map = ruleFieldMap(currentRules);
  for (const [sel, val] of Object.entries(map)) {
    const el = $(sel);
    if (!el) continue;
    if (el.type === "checkbox") el.checked = !!val;
    else el.value = (val === null || val === undefined) ? "" : val;
  }
}

function collectRulesFromForm() {
  const r = JSON.parse(JSON.stringify(currentRules));
  r.mode = $("#rule-mode").value;
  r.kill_switch = $("#rule-kill-switch").checked;
  r.schedule.premarket_scan_time = $("#sched-premarket").value;
  r.schedule.trading_window_start = $("#sched-window-start").value;
  r.schedule.trading_window_end = $("#sched-window-end").value;
  r.schedule.eod_summary_time = $("#sched-eod").value;
  r.schedule.cycle_interval_minutes = Number($("#sched-interval").value);
  r.schedule.timezone = $("#sched-tz").value;
  r.strategies.gap_momentum.enabled = $("#gm-enabled").checked;
  r.strategies.gap_momentum.min_gap_pct = Number($("#gm-min-gap").value);
  r.strategies.gap_momentum.max_gap_pct = Number($("#gm-max-gap").value);
  r.strategies.gap_momentum.min_price = Number($("#gm-min-price").value);
  r.strategies.gap_momentum.max_price = Number($("#gm-max-price").value);
  r.strategies.gap_momentum.min_avg_volume = Number($("#gm-min-vol").value);
  r.strategies.breakout.enabled = $("#bo-enabled").checked;
  r.strategies.breakout.lookback_days = Number($("#bo-lookback").value);
  r.strategies.breakout.breakout_pct = Number($("#bo-pct").value);
  const allocationRaw = $("#risk-allocation").value;
  r.risk.capital_allocation_usd = allocationRaw === "" ? null : Number(allocationRaw);
  r.risk.position_size_pct = Number($("#risk-size").value);
  r.risk.max_open_positions = Number($("#risk-maxpos").value);
  r.risk.stop_loss_pct = Number($("#risk-sl").value);
  r.risk.take_profit_pct = Number($("#risk-tp").value);
  r.risk.max_daily_loss_pct = Number($("#risk-maxloss").value);
  r.notifications.telegram_enabled = $("#notif-telegram").checked;
  r.notifications.notify_on_trade = $("#notif-trade").checked;
  r.notifications.notify_on_error = $("#notif-error").checked;
  r.notifications.notify_eod_summary = $("#notif-eod").checked;
  return r;
}

$("#save-rules-btn").addEventListener("click", async () => {
  const msg = $("#save-msg");
  try {
    const updated = collectRulesFromForm();
    currentRules = await api("/api/rules", { method: "PUT", body: JSON.stringify(updated) });
    msg.textContent = "Saved.";
    msg.className = "save-msg ok";
    refreshStatus();
  } catch (err) {
    msg.textContent = "Save failed: " + err.message;
    msg.className = "save-msg err";
  }
});

$("#kill-switch-btn").addEventListener("click", async () => {
  const next = !$("#rule-kill-switch").checked;
  await api("/api/kill-switch", { method: "POST", body: JSON.stringify({ enabled: next }) });
  await refreshRules();
  await refreshStatus();
});

$("#live-mode-btn").addEventListener("click", async () => {
  const goingLive = $("#rule-mode").value !== "live";
  if (goingLive) {
    const sure = confirm(
      "This will switch the bot to LIVE trading with real money, if the server's " +
      "ALLOW_LIVE_TRADING setting also permits it. Are you sure?"
    );
    if (!sure) return;
  }
  try {
    await api("/api/mode", {
      method: "POST",
      body: JSON.stringify({ mode: goingLive ? "live" : "paper", confirm: goingLive ? "CONFIRM" : undefined }),
    });
    await refreshRules();
    await refreshStatus();
  } catch (err) {
    alert("Could not change mode: " + err.message);
  }
});

// --- Helpers ---

function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString();
}

async function refreshAll() {
  try {
    await Promise.all([refreshStatus(), refreshActivity(), refreshScans(), refreshSummary(), refreshRules(), refreshLogs()]);
  } catch (err) {
    console.error(err);
  }
}

setInterval(() => {
  if (!$("#app").hidden) {
    refreshStatus();
    refreshActivity();
  }
}, 30000);

// --- Boot: check if already logged in ---

(async () => {
  try {
    await api("/api/status");
    showApp();
  } catch {
    showLogin();
  }
})();
