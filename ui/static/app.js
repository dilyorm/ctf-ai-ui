/* ─────────────────────────────────────────────────────────────────
   CTF Agent Dashboard  —  Frontend JS
   ───────────────────────────────────────────────────────────────── */

"use strict";

// ── State ──────────────────────────────────────────────────────────
const state = {
  challenges: {},
  selectedChallenge: null,
  costByModel: {},
  totalCost: 0,
  totalTokens: 0,
  wsConnected: false,
  logAutoScroll: true,
  filter: "all",
  runStatus: { running: false, stopped_challenges: [], priority_challenges: [], excluded_challenges: [] },
};

// ── DOM refs ───────────────────────────────────────────────────────
const $ = id => document.getElementById(id);

const challengeList    = $("challenge-list");
const challengeDetail  = $("challenge-detail");
const welcomeScreen    = $("welcome-screen");
const detailName       = $("detail-name");
const detailStatus     = $("detail-status");
const detailCategory   = $("detail-category");
const detailValue      = $("detail-value");
const flagBanner       = $("flag-banner");
const flagText         = $("flag-text");
const modelsGrid       = $("models-grid");
const logContainer     = $("log-container");
const logAutoScrollChk = $("log-autoscroll");
const valChallenges    = $("val-challenges");
const valSolved        = $("val-solved");
const valCost          = $("val-cost");
const ctfdBadge        = $("ctfd-badge");
const ctfdLabel        = $("ctfd-label");
const costTotal        = $("cost-total-display");
const modelCosts       = $("model-costs");
const wsStatus         = $("ws-status");
const wsLabel          = $("ws-label");
const btnSendMsg       = $("btn-send-msg");
const msgInput         = $("msg-input");
const msgStatus        = $("msg-status");
const btnCopyFlag      = $("btn-copy-flag");
const runStatusEl      = $("run-status");
const runMsg           = $("run-msg");
const btnRunStart      = $("btn-run-start");
const btnRunStop       = $("btn-run-stop");
const concurrencySlider = $("concurrency-slider");
const concurrencyVal   = $("concurrency-val");
const ctfSelector      = $("ctf-selector");
const noSubmitToggle   = $("no-submit-toggle");

// Per-challenge control buttons
const btnChStop     = $("btn-ch-stop");
const btnChPriority = $("btn-ch-priority");
const btnChExclude  = $("btn-ch-exclude");

// ── WebSocket ──────────────────────────────────────────────────────
let ws = null;
let wsReconnectTimer = null;

function connectWS() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);

  ws.onopen = () => {
    state.wsConnected = true;
    updateWSStatus("connected");
    if (wsReconnectTimer) { clearTimeout(wsReconnectTimer); wsReconnectTimer = null; }
  };

  ws.onmessage = evt => {
    try { handleEvent(JSON.parse(evt.data)); }
    catch (e) { console.error("WS parse error", e); }
  };

  ws.onclose = ws.onerror = () => {
    state.wsConnected = false;
    updateWSStatus("disconnected");
    wsReconnectTimer = setTimeout(connectWS, 3000);
  };
}

function updateWSStatus(status) {
  const dot = wsStatus.querySelector(".dot");
  dot.className = "dot " + status;
  wsLabel.textContent = status === "connected" ? "Live" : status === "connecting" ? "Connecting…" : "Disconnected";
}

// ── Event handler ──────────────────────────────────────────────────
function handleEvent(evt) {
  switch (evt.type) {
    case "snapshot":       applySnapshot(evt.data); break;
    case "challenge_new":
    case "challenge_update":
    case "challenge_started": upsertChallenge(evt.data); break;
    case "challenge_solved":  onChallengeSolved(evt.data); break;
    case "challenge_failed":  onChallengeFailed(evt.data); break;
    case "solver_update":     onSolverUpdate(evt.data); break;
    case "log_line":          onLogLine(evt.data); break;
    case "cost_update":       onCostUpdate(evt.data); break;
    case "ctfd_status":       onCTFdStatus(evt.data); break;
  }
}

// ── Snapshot ───────────────────────────────────────────────────────
function applySnapshot(data) {
  state.challenges = data.challenges || {};
  state.totalCost = data.total_cost || 0;
  state.totalTokens = data.total_tokens || 0;
  state.costByModel = data.cost_summary || {};

  if (data.ctfd_status) onCTFdStatus(data.ctfd_status);
  onCostUpdate({ total_cost: state.totalCost, total_tokens: state.totalTokens, by_model: state.costByModel });

  if (data.logs) {
    Object.entries(data.logs).forEach(([ch, lines]) => {
      state.challenges[ch] = state.challenges[ch] || { name: ch };
      state.challenges[ch]._logs = lines;
    });
  }

  renderChallengeList();
  if (state.selectedChallenge && state.challenges[state.selectedChallenge]) {
    renderChallengeDetail(state.challenges[state.selectedChallenge]);
  }
}

// ── Challenge helpers ──────────────────────────────────────────────
function upsertChallenge(data) {
  const name = data.name;
  if (!name) return;
  const existing = state.challenges[name] || {};
  state.challenges[name] = Object.assign({}, existing, data);
  if (!state.challenges[name]._logs) state.challenges[name]._logs = [];
  renderChallengeList();
  if (state.selectedChallenge === name) renderChallengeDetail(state.challenges[name]);
  updateHeaderStats();
}

function onChallengeSolved(data) {
  const name = data.name;
  if (!name) return;
  state.challenges[name] = Object.assign(state.challenges[name] || { name }, data, { status: "solved" });
  renderChallengeList();
  if (state.selectedChallenge === name) renderChallengeDetail(state.challenges[name]);
  updateHeaderStats();
}

function onChallengeFailed(data) {
  const name = data.name;
  if (!name) return;
  if (state.challenges[name]) state.challenges[name].status = "failed";
  renderChallengeList();
  if (state.selectedChallenge === name) renderChallengeDetail(state.challenges[name]);
}

function onSolverUpdate(data) {
  const name = data.challenge;
  if (!name) return;
  const ch = state.challenges[name] || { name, status: "running", models: {} };
  state.challenges[name] = ch;
  ch.models = ch.models || {};
  ch.models[data.model] = {
    status: data.status || "running",
    steps: data.steps || 0,
    cost: data.cost || 0,
    findings: data.findings || "",
  };
  if (state.selectedChallenge === name) updateModelsGrid(ch);
}

function onLogLine(data) {
  const name = data.challenge;
  if (!name) return;
  if (!state.challenges[name]) state.challenges[name] = { name, _logs: [] };
  if (!state.challenges[name]._logs) state.challenges[name]._logs = [];
  const line = { ts: Date.now() / 1000, model: data.model, text: data.text, level: data.level || "info" };
  state.challenges[name]._logs.push(line);
  if (state.challenges[name]._logs.length > 500) state.challenges[name]._logs.shift();
  if (state.selectedChallenge === name) appendLogLine(line);
}

function onCostUpdate(data) {
  if (data.total_cost !== undefined) state.totalCost = data.total_cost;
  if (data.total_tokens !== undefined) state.totalTokens = data.total_tokens;
  if (data.by_model) state.costByModel = data.by_model;
  if (costTotal) costTotal.textContent = "$" + state.totalCost.toFixed(4);
  if (valCost) valCost.textContent = "$" + state.totalCost.toFixed(2);
  renderModelCosts();
}

function onCTFdStatus(data) {
  if (!ctfdBadge) return;
  const connected = data.connected;
  ctfdBadge.className = "ctfd-badge " + (connected ? "connected" : "disconnected");
  ctfdLabel.textContent = "CTFd " + (connected ? "Connected" : "Disconnected");
}

// ── Challenge list ─────────────────────────────────────────────────
function renderChallengeList() {
  if (!challengeList) return;
  const items = Object.values(state.challenges);
  // Put priority challenges first, then sort by status
  const stopped = new Set(state.runStatus.stopped_challenges || []);
  const priority = new Set(state.runStatus.priority_challenges || []);

  const filtered = state.filter === "all" ? items : items.filter(c => c.status === state.filter);
  filtered.sort((a, b) => {
    const pa = priority.has(a.name) ? -1 : 0;
    const pb = priority.has(b.name) ? -1 : 0;
    if (pa !== pb) return pa - pb;
    const order = { running: 0, solved: 1, pending: 2, stopped: 3, excluded: 4, failed: 5 };

    const excluded = new Set(state.runStatus.excluded_challenges || []);
    const sa = excluded.has(a.name) ? "excluded" : (stopped.has(a.name) ? "stopped" : (a.status || "pending"));
    const sb = excluded.has(b.name) ? "excluded" : (stopped.has(b.name) ? "stopped" : (b.status || "pending"));
    return (order[sa] ?? 99) - (order[sb] ?? 99) || (a.name || "").localeCompare(b.name || "");
  });

  if (filtered.length === 0) {
    challengeList.innerHTML = '<div class="empty-state">No challenges match this filter.</div>';
    return;
  }

  const excluded = new Set(state.runStatus.excluded_challenges || []);

  challengeList.innerHTML = filtered.map(ch => {
    const isStopped = stopped.has(ch.name);
    const isPriority = priority.has(ch.name);
    const isExcluded = excluded.has(ch.name);
    const effectiveStatus = isExcluded ? "excluded" : (isStopped ? "stopped" : (ch.status || "pending"));
    const badges = [
      isPriority ? '<span class="ch-badge priority">▲</span>' : "",
      isExcluded ? '<span class="ch-badge excluded">✕</span>' : "",
      isStopped  ? '<span class="ch-badge stopped">⏹</span>'  : "",
    ].join("");
    return `
      <div class="challenge-item${state.selectedChallenge === ch.name ? " active" : ""}${isStopped ? " ch-stopped" : ""}${isExcluded ? " ch-excluded" : ""}" data-name="${escHtml(ch.name)}">
        <div class="ch-status-dot ${effectiveStatus}"></div>
        <div class="ch-info">
          <div class="ch-name">${escHtml(ch.name)}${badges}</div>
          <div class="ch-meta">${escHtml(ch.category || "")}${ch.flag ? " · " + escHtml(ch.flag) : ""}</div>
        </div>
        <div class="ch-pts">${ch.value ? ch.value + "pt" : ""}</div>
      </div>
    `;
  }).join("");

  challengeList.querySelectorAll(".challenge-item").forEach(el => {
    el.addEventListener("click", () => selectChallenge(el.dataset.name));
  });
}

function updateHeaderStats() {
  const all = Object.values(state.challenges);
  if (valChallenges) valChallenges.textContent = all.length;
  if (valSolved) valSolved.textContent = all.filter(c => c.status === "solved").length;
}

// ── Challenge detail ───────────────────────────────────────────────
function selectChallenge(name) {
  state.selectedChallenge = name;
  renderChallengeList();
  const ch = state.challenges[name];
  if (ch) {
    welcomeScreen.style.display = "none";
    challengeDetail.style.display = "flex";
    renderChallengeDetail(ch);
    updateChallengeControlButtons(name);
  }
}

function renderChallengeDetail(ch) {
  detailName.textContent = ch.name;
  // If run controls say it's stopped/excluded, surface that even if the last
  // backend-emitted status was "running".
  const name = ch.name;
  const stoppedSet = new Set(state.runStatus.stopped_challenges || []);
  const excludedSet = new Set(state.runStatus.excluded_challenges || []);
  const effectiveStatus = excludedSet.has(name)
    ? "excluded"
    : (stoppedSet.has(name) ? "stopped" : (ch.status || "pending"));

  detailStatus.textContent = effectiveStatus;
  detailStatus.className = "status-badge " + effectiveStatus;
  detailCategory.textContent = ch.category || "";
  detailValue.textContent = ch.value ? ch.value + " pts" : "";

  if (ch.flag && ch.status === "solved") {
    flagBanner.style.display = "flex";
    flagText.textContent = ch.flag;
  } else {
    flagBanner.style.display = "none";
  }

  updateModelsGrid(ch);
  renderLogs(ch);
}

function updateChallengeControlButtons(name) {
  if (!btnChStop || !btnChPriority || !btnChExclude) return;
  const stopped  = new Set(state.runStatus.stopped_challenges || []);
  const priority = new Set(state.runStatus.priority_challenges || []);
  const excluded = new Set(state.runStatus.excluded_challenges || []);
  const isStopped  = stopped.has(name);
  const isPriority = priority.has(name);
  const isExcluded = excluded.has(name);

  btnChStop.innerHTML     = isStopped  ? '<span class="ctrl-icon">▶</span> Resume'   : '<span class="ctrl-icon">⏹</span> Stop';
  btnChStop.classList.toggle("active", isStopped);
  btnChPriority.innerHTML = isPriority ? '<span class="ctrl-icon">⬆</span> Deprioritize' : '<span class="ctrl-icon">⬆</span> Priority';
  btnChPriority.classList.toggle("active", isPriority);

  btnChExclude.innerHTML = isExcluded
    ? '<span class="ctrl-icon">↩</span> Unexclude'
    : '<span class="ctrl-icon">✕</span> Exclude';
  btnChExclude.classList.toggle("active", isExcluded);
}

function updateModelsGrid(ch) {
  if (state.selectedChallenge !== ch.name || !modelsGrid) return;
  const models = ch.models || {};
  const specs = ch.model_specs || ch.models_list || Object.keys(models);
  const allSpecs = [...new Set([...specs, ...Object.keys(models)])];

  if (allSpecs.length === 0) {
    modelsGrid.innerHTML = '<div class="empty-state">No models running yet.</div>';
    return;
  }

  modelsGrid.innerHTML = allSpecs.map(spec => {
    const info = models[spec] || {};
    const status = info.status || (ch.winner_model === spec ? "won" : "pending");
    const isWinner = ch.winner_model === spec || status === "won";
    const cardClass = isWinner ? "won" : status === "running" ? "running" : status === "failed" ? "failed" : "";
    const statusIcon = isWinner ? "🏆" : status === "running" ? "⚙" : status === "failed" ? "✗" : "○";
    return `
      <div class="model-card ${cardClass}">
        <div class="model-name">${escHtml(spec)}</div>
        <div class="model-status-row">
          <span>${statusIcon}</span>
          <span class="model-status">${escHtml(status)}</span>
        </div>
        ${info.steps ? `<div class="model-stats">${info.steps} steps${info.cost ? " · $" + info.cost.toFixed(4) : ""}</div>` : ""}
        ${info.findings ? `<div class="model-findings">${escHtml(info.findings.substring(0, 200))}</div>` : ""}
      </div>
    `;
  }).join("");
}

// ── Logs ───────────────────────────────────────────────────────────
function renderLogs(ch) {
  if (!logContainer) return;
  logContainer.innerHTML = "";
  (ch._logs || []).forEach(line => appendLogLine(line, false));
  if (state.logAutoScroll) logContainer.scrollTop = logContainer.scrollHeight;
}

function appendLogLine(line, doScroll = true) {
  if (!logContainer) return;
  if (state.selectedChallenge !== (line.challenge || state.selectedChallenge)) return;
  const ts = new Date(line.ts * 1000).toISOString().substr(11, 8);
  const el = document.createElement("div");
  el.className = "log-line " + (line.level || "info");
  el.innerHTML = `
    <span class="log-ts">${ts}</span>
    ${line.model ? `<span class="log-model">${escHtml(line.model.split("/").pop())}</span>` : ""}
    <span class="log-text">${escHtml(line.text)}</span>
  `;
  logContainer.appendChild(el);
  if (doScroll && state.logAutoScroll) logContainer.scrollTop = logContainer.scrollHeight;
}

// ── Cost ───────────────────────────────────────────────────────────
function renderModelCosts() {
  if (!modelCosts) return;
  const entries = Object.entries(state.costByModel || {});
  if (entries.length === 0) {
    modelCosts.innerHTML = '<div class="empty-state-sm">No usage yet</div>';
    return;
  }
  entries.sort(([, a], [, b]) => (b.cost_usd || 0) - (a.cost_usd || 0));
  modelCosts.innerHTML = entries.map(([model, info]) => {
    const short = model.split("/").slice(-1)[0];
    const cost = (info.cost_usd || info.cost || 0).toFixed(4);
    return `
      <div class="model-cost-row">
        <span class="model-cost-name" title="${escHtml(model)}">${escHtml(short)}</span>
        <span class="model-cost-val">$${cost}</span>
      </div>
    `;
  }).join("");
}

// ── Operator message ───────────────────────────────────────────────
if (btnSendMsg) {
  btnSendMsg.addEventListener("click", async () => {
    const msg = msgInput.value.trim();
    if (!msg) return;
    setStatus("msg-status", "Sending…", null);
    try {
      const res = await fetch("/api/message", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: msg }),
      });
      const data = await res.json();
      if (data.ok) {
        setStatus("msg-status", "Sent!", true);
        msgInput.value = "";
      } else {
        setStatus("msg-status", data.error || "Failed", false);
      }
    } catch {
      setStatus("msg-status", "Network error", false);
    }
  });
}

// ── Run controls ───────────────────────────────────────────────────
if (concurrencySlider) {
  concurrencySlider.addEventListener("input", () => {
    concurrencyVal.textContent = concurrencySlider.value;
  });
}

async function refreshRunStatus() {
  if (!runStatusEl) return;
  try {
    const res = await fetch("/api/run/status");
    const data = await res.json();
    if (!data.ok) return;
    const st = data.status || {};
    state.runStatus = st;
    runStatusEl.textContent = st.running ? "running" : "stopped";
    runStatusEl.style.color = st.running ? "var(--green)" : "var(--text3)";
    // Update challenge control buttons if a challenge is selected
    if (state.selectedChallenge) updateChallengeControlButtons(state.selectedChallenge);
    // Update sidebar badges
    renderChallengeList();
  } catch {
    if (runStatusEl) runStatusEl.textContent = "unknown";
  }
}

async function runStart() {
  setStatus("run-msg", "Starting…", null);
  const ctfId = ctfSelector ? ctfSelector.value : "";
  const maxConcurrent = concurrencySlider ? parseInt(concurrencySlider.value) : 10;
  const noSubmit = noSubmitToggle ? noSubmitToggle.checked : false;

  if (!ctfId) {
    setStatus("run-msg", "Select a CTF instance first (Manage CTFs)", false);
    return;
  }

  try {
    const res = await fetch("/api/run/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ctf_id: ctfId ? parseInt(ctfId) : undefined,
        coordinator: "claude",
        max_concurrent_challenges: maxConcurrent,
        no_submit: noSubmit,
      }),
    });
    const data = await res.json();
    if (data.ok) {
      setStatus("run-msg", "Started", true);
    } else {
      setStatus("run-msg", data.error || "Failed", false);
    }
  } catch {
    setStatus("run-msg", "Network error", false);
  }
  refreshRunStatus();
}

async function runStop() {
  setStatus("run-msg", "Stopping…", null);
  try {
    const res = await fetch("/api/run/stop", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      // Force-stop so a stale run from another session/user can be stopped.
      body: JSON.stringify({ force: true }),
    });
    const data = await res.json();
    setStatus("run-msg", data.ok ? (data.stopped ? "Stopped" : "Not running") : data.error || "Failed", data.ok);
  } catch {
    setStatus("run-msg", "Network error", false);
  }
  refreshRunStatus();
}

if (btnRunStart) btnRunStart.addEventListener("click", runStart);
if (btnRunStop)  btnRunStop.addEventListener("click", runStop);

// ── Per-challenge controls ──────────────────────────────────────────
async function challengeControl(endpoint) {
  const name = state.selectedChallenge;
  if (!name) return;
  try {
    const res = await fetch(`/api/run/challenge/${encodeURIComponent(name)}/${endpoint}`, { method: "POST" });
    const data = await res.json();
    if (data.ok) {
      // Update local state
      const stopped  = new Set(state.runStatus.stopped_challenges  || []);
      const priority = new Set(state.runStatus.priority_challenges || []);
      const excluded = new Set(state.runStatus.excluded_challenges || []);
      if (endpoint === "stop") {
        data.stopped ? stopped.add(name) : stopped.delete(name);
        state.runStatus.stopped_challenges = [...stopped];
      } else if (endpoint === "priority") {
        data.priority ? priority.add(name) : priority.delete(name);
        state.runStatus.priority_challenges = [...priority];
      } else if (endpoint === "exclude") {
        if (data.excluded) {
          excluded.add(name);
          stopped.add(name);
        } else {
          excluded.delete(name);
        }
        state.runStatus.excluded_challenges = [...excluded];
        state.runStatus.stopped_challenges = [...stopped];
      }
      updateChallengeControlButtons(name);
      renderChallengeList();
    }
  } catch { /* ignore */ }
}

if (btnChStop)     btnChStop.addEventListener("click",     () => challengeControl("stop"));
if (btnChPriority) btnChPriority.addEventListener("click", () => challengeControl("priority"));
if (btnChExclude)  btnChExclude.addEventListener("click",  async (e) => {
  if (e) e.preventDefault();
  // Avoid moving focus to the operator message textarea when clicking controls.
  if (e) e.stopPropagation();
  const name = state.selectedChallenge;
  if (!name) return;
  if (!confirm(`Exclude "${name}" from this run? It won't be auto-spawned again.`)) return;
  await challengeControl("exclude");
});

// ── Challenge filters ──────────────────────────────────────────────
document.querySelectorAll(".filter-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".filter-btn").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    state.filter = btn.dataset.filter;
    renderChallengeList();
  });
});

// ── Log auto-scroll ────────────────────────────────────────────────
if (logAutoScrollChk) {
  logAutoScrollChk.addEventListener("change", () => {
    state.logAutoScroll = logAutoScrollChk.checked;
  });
}

// ── Copy flag ──────────────────────────────────────────────────────
if (btnCopyFlag) {
  btnCopyFlag.addEventListener("click", () => {
    navigator.clipboard.writeText(flagText.textContent).then(() => {
      btnCopyFlag.textContent = "Copied!";
      setTimeout(() => { btnCopyFlag.textContent = "Copy"; }, 2000);
    });
  });
}

// ── Utility ───────────────────────────────────────────────────────
function escHtml(str) {
  if (!str) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function setStatus(id, msg, ok) {
  const el = $(id);
  if (!el) return;
  el.textContent = msg;
  el.className = "msg-status" + (ok === true ? " ok" : ok === false ? " err" : "");
  if (ok !== null) setTimeout(() => { el.textContent = ""; el.className = "msg-status"; }, 4000);
}

// ── Init ───────────────────────────────────────────────────────────
function init() {
  connectWS();
  updateWSStatus("connecting");
  refreshRunStatus();
  setInterval(refreshRunStatus, 5000);

  // Read ctf_id from URL query param and pre-select
  const params = new URLSearchParams(location.search);
  const ctfParam = params.get("ctf_id");
  if (ctfParam && ctfSelector) {
    ctfSelector.value = ctfParam;
  }

  // Fallback poll when WS is disconnected
  setInterval(async () => {
    if (!state.wsConnected) {
      try {
        const res = await fetch("/api/status");
        const data = await res.json();
        applySnapshot({
          challenges: data.challenges,
          total_cost: data.cost?.total_usd || 0,
          total_tokens: data.cost?.total_tokens || 0,
          cost_summary: data.cost?.by_model || {},
          ctfd_status: data.ctfd,
        });
      } catch { /* ignore */ }
    }
  }, 10000);
}

document.addEventListener("DOMContentLoaded", init);
