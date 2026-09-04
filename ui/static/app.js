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

// Shared utilities live in common.js (escHtml, toast, confirmDialog, setBusy).
const toast = window.toast;
const confirmDialog = window.confirmDialog;
const setBusy = window.setBusy;
const escHtml = window.escHtml;

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
    case "agent_intervention": onIntervention(evt.data); break;
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
  if (Array.isArray(data.interventions)) {
    state.interventions = data.interventions;
    renderInterventions();
  }
  if (state.view === "fleet") renderFleet();
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
        <div class="model-controls" data-spec="${escHtml(spec)}">
          <button class="agent-btn" data-act="message" title="Send instruction">✎</button>
          <button class="agent-btn" data-act="${info.paused ? "resume" : "pause"}" title="${info.paused ? "Resume" : "Pause"}">${info.paused ? "▶" : "⏸"}</button>
          <button class="agent-btn" data-act="restart" title="Restart agent">↻</button>
          <button class="agent-btn" data-act="swap_account" title="Swap to another account">⇄</button>
          <button class="agent-btn" data-act="swap_model" title="Swap model">≡</button>
          <button class="agent-btn" data-act="stop" title="Stop agent">⏹</button>
          <label class="agent-btn" title="Attach context file">📎<input type="file" data-act="context" hidden></label>
        </div>
      </div>
    `;
  }).join("");
  wireAgentControls();
}

let _agentControlsWired = false;
function wireAgentControls() {
  if (_agentControlsWired || !modelsGrid) return;
  _agentControlsWired = true;

  async function post(action, spec, extra) {
    await fetch(`/api/run/agent/${action}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ challenge: state.selectedChallenge, model_spec: spec, ...extra }),
    });
  }

  modelsGrid.addEventListener("click", async (e) => {
    const btn = e.target.closest(".agent-btn[data-act]");
    if (!btn) return;
    const act = btn.dataset.act;
    if (act === "context") return; // handled by file input change
    const spec = btn.closest(".model-controls")?.dataset.spec;
    if (!spec) return;
    if (act === "message") {
      const text = prompt(`Instruction for ${spec.split("/").pop()}:`);
      if (text) await post("message", spec, { text });
    } else if (act === "swap_model") {
      const newSpec = prompt(`Swap ${spec} to which model spec?\n(e.g. claude-sdk/claude-opus-4-6/max, codex/gpt-5.4, copilot/gpt-5)`, spec);
      if (newSpec && newSpec !== spec) await post("swap_model", spec, { new_spec: newSpec.trim() });
    } else {
      await post(act, spec, {}); // stop / pause / resume / restart / swap_account (rotate)
    }
  });

  modelsGrid.addEventListener("change", async (e) => {
    const input = e.target;
    if (input.dataset?.act !== "context" || !input.files?.length) return;
    const spec = input.closest(".model-controls")?.dataset.spec;
    const fd = new FormData();
    fd.append("challenge", state.selectedChallenge);
    fd.append("model_spec", spec);
    fd.append("file", input.files[0]);
    await fetch("/api/run/agent/context", { method: "POST", body: fd });
    input.value = "";
  });
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
async function sendOperatorMessage() {
  const msg = msgInput.value.trim();
  if (!msg) { toast("Type a message first", "warn"); return; }
  const release = setBusy(btnSendMsg, true, "Sending…");
  try {
    const res = await fetch("/api/message", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: msg }),
    });
    const data = await res.json();
    if (data.ok) {
      toast("Hint sent to coordinator.", "success");
      msgInput.value = "";
    } else {
      toast(data.error || "Send failed", "error");
    }
  } catch {
    toast("Network error.", "error");
  } finally {
    release();
  }
}

if (btnSendMsg) btnSendMsg.addEventListener("click", sendOperatorMessage);
if (msgInput)   msgInput.addEventListener("keydown", e => {
  if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
    e.preventDefault();
    sendOperatorMessage();
  }
});

// ── Run controls ───────────────────────────────────────────────────
if (concurrencySlider) {
  concurrencySlider.addEventListener("input", () => {
    concurrencyVal.textContent = concurrencySlider.value;
  });
}

function formatDuration(secs) {
  secs = Math.max(0, Math.floor(secs));
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  const s = secs % 60;
  return h ? `${h}h ${m}m` : m ? `${m}m ${s}s` : `${s}s`;
}

async function refreshRunStatus() {
  if (!runStatusEl) return;
  try {
    const res = await fetch("/api/run/status");
    const data = await res.json();
    if (!data.ok) return;
    const st = data.status || {};
    state.runStatus = st;
    runStatusEl.textContent = st.running ? "running" : "idle";
    runStatusEl.style.color = st.running ? "var(--green)" : "var(--text3)";

    if (btnRunStart) btnRunStart.disabled = !!st.running;
    if (btnRunStop)  btnRunStop.disabled  = !st.running;

    const meta = $("run-meta");
    if (meta) {
      if (st.running && st.started_at) {
        const elapsed = (Date.now() - new Date(st.started_at).getTime()) / 1000;
        meta.className = "run-meta";
        meta.textContent = `Up for ${formatDuration(elapsed)}`;
      } else if (st.last_error) {
        meta.className = "run-meta error";
        meta.textContent = `Last error: ${st.last_error.slice(0, 80)}`;
      } else if (st.started_at) {
        meta.className = "run-meta";
        meta.textContent = "Stopped";
      } else {
        meta.className = "run-meta";
        meta.textContent = "";
      }
    }

    if (state.selectedChallenge) updateChallengeControlButtons(state.selectedChallenge);
    renderChallengeList();
  } catch {
    if (runStatusEl) runStatusEl.textContent = "unknown";
  }
}

async function runStart() {
  const ctfId = ctfSelector ? ctfSelector.value : "";
  const maxConcurrent = concurrencySlider ? parseInt(concurrencySlider.value) : 10;
  const noSubmit = noSubmitToggle ? noSubmitToggle.checked : false;

  if (!ctfId) {
    toast("Select a CTF instance first (open Manage CTFs).", "warn");
    return;
  }

  const release = setBusy(btnRunStart, true, "Starting…");
  try {
    const res = await fetch("/api/run/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ctf_id: parseInt(ctfId),
        coordinator: "claude",
        max_concurrent_challenges: maxConcurrent,
        no_submit: noSubmit,
        autopilot: state.autopilot !== false,
      }),
    });
    const data = await res.json();
    if (data.ok) toast("Solver started.", "success");
    else         toast(data.error || "Failed to start", "error", { duration: 6000 });
  } catch {
    toast("Network error while starting.", "error");
  } finally {
    release();
    refreshRunStatus();
  }
}

async function runStop({ skipConfirm = false } = {}) {
  if (!skipConfirm) {
    const chCount = Object.values(state.challenges).filter(c => c.status === "running").length;
    const ok = await confirmDialog({
      title: "Stop the running solver?",
      body: chCount
        ? `This will cancel the coordinator and kill <strong>${chCount}</strong> running challenge${chCount === 1 ? "" : "s"}. Solved flags are kept.`
        : "This will cancel the coordinator. Solved flags are kept.",
      confirmText: "Stop solver",
      cancelText: "Keep running",
      icon: "■",
      danger: true,
    });
    if (!ok) return;
  }

  const release = setBusy(btnRunStop, true, "Stopping…");
  try {
    const res = await fetch("/api/run/stop", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ force: true }),
    });
    const data = await res.json();
    if (!data.ok)             toast(data.error || "Stop failed", "error");
    else if (data.stopped)    toast("Solver stopped.", "success");
    else                       toast("No active run to stop.", "info");
  } catch {
    toast("Network error while stopping.", "error");
  } finally {
    release();
    refreshRunStatus();
  }
}

if (btnRunStart) btnRunStart.addEventListener("click", () => runStart());
if (btnRunStop)  btnRunStop.addEventListener("click",  () => runStop());

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
  if (e) { e.preventDefault(); e.stopPropagation(); }
  const name = state.selectedChallenge;
  if (!name) return;
  const excluded = new Set(state.runStatus.excluded_challenges || []);
  if (excluded.has(name)) {
    await challengeControl("exclude");
    toast(`"${name}" un-excluded`, "info");
    return;
  }
  const ok = await confirmDialog({
    title: "Exclude this challenge?",
    body: `Exclude <strong>${escHtml(name)}</strong> from this run? It won't be auto-spawned again until the run is restarted.`,
    confirmText: "Exclude",
    cancelText: "Cancel",
    icon: "✕",
    danger: true,
  });
  if (!ok) return;
  await challengeControl("exclude");
  toast(`"${name}" excluded`, "info");
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
  btnCopyFlag.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(flagText.textContent);
      btnCopyFlag.textContent = "Copied!";
      toast("Flag copied", "success", { duration: 1500 });
      setTimeout(() => { btnCopyFlag.textContent = "Copy"; }, 2000);
    } catch {
      toast("Clipboard unavailable", "error");
    }
  });
}

// ── Mobile drawer toggles ──────────────────────────────────────────
function setupDrawer(triggerSel, panelSel) {
  const trigger = document.querySelector(triggerSel);
  const panel = document.querySelector(panelSel);
  if (!trigger || !panel) return;
  trigger.addEventListener("click", e => {
    e.stopPropagation();
    const others = document.querySelectorAll(".sidebar.open, .right-panel.open");
    others.forEach(o => { if (o !== panel) o.classList.remove("open"); });
    panel.classList.toggle("open");
    updateDrawerOverlay();
  });
}

function updateDrawerOverlay() {
  let overlay = document.getElementById("drawer-overlay");
  const anyOpen = document.querySelector(".sidebar.open, .right-panel.open");
  if (!overlay) {
    overlay = document.createElement("div");
    overlay.id = "drawer-overlay";
    overlay.className = "drawer-overlay";
    overlay.addEventListener("click", () => {
      document.querySelectorAll(".sidebar.open, .right-panel.open").forEach(p => p.classList.remove("open"));
      overlay.classList.remove("open");
    });
    document.body.appendChild(overlay);
  }
  overlay.classList.toggle("open", !!anyOpen);
}

// (Top nav toggle + Esc closes drawers handled in common.js)

// ── Init ───────────────────────────────────────────────────────────
async function refreshRunModelSummary() {
  const countEl = document.getElementById("run-model-count");
  const listEl  = document.getElementById("run-model-list");
  if (!countEl || !listEl) return;
  try {
    const res = await fetch("/api/models");
    const data = await res.json();
    const enabled = (data.enabled || []);
    const isDefault = !!data.default;
    countEl.textContent = enabled.length || "0";
    if (!enabled.length) {
      listEl.innerHTML = `<em style="color:var(--text3)">no models selected — falls back to <code>claude-sdk/claude-opus-4-7/max</code></em>`;
      return;
    }
    listEl.innerHTML = enabled
      .map(s => `<span style="display:block">${isDefault ? "⊘ " : "✓ "}${s}</span>`)
      .join("");
    if (isDefault) {
      listEl.innerHTML += `<em style="color:var(--text3);display:block;margin-top:4px;">↑ default — pick explicit models in <a href="/settings#models" style="color:var(--accent)">Settings → Models</a></em>`;
    }
  } catch {
    listEl.textContent = "(failed to load model selection)";
  }
}

function init() {
  connectWS();
  updateWSStatus("connecting");
  refreshRunStatus();
  refreshRunModelSummary();
  setInterval(refreshRunStatus, 5000);
  setInterval(refreshRunModelSummary, 10000);

  setupDrawer("#sidebar-toggle", ".sidebar");
  setupDrawer("#right-panel-toggle", ".right-panel");
  setupAutopilot();
  setupFleetNav();

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

// ── Provider colors / initials (fleet + interventions) ─────────────
const PROV_COLOR = {
  "claude-sdk": "#b4713a", claude: "#b4713a",
  codex: "#10a37f", copilot: "#6e40c9", google: "#4285f4",
  grok: "#111827", kimi: "#16a34a", antigravity: "#4285f4",
  bedrock: "#ff9900", azure: "#0078d4", zen: "#8b5cf6",
};
function specProvider(spec) { return (spec || "").split("/")[0]; }
function provColor(spec) { return PROV_COLOR[specProvider(spec)] || "var(--accent2)"; }
function provAb(spec) {
  const p = specProvider(spec);
  const map = { "claude-sdk": "CL", claude: "CL", codex: "GX", copilot: "CP",
    google: "GM", grok: "GK", kimi: "KM", antigravity: "AG", bedrock: "BR", azure: "AZ", zen: "ZN" };
  return map[p] || p.slice(0, 2).toUpperCase();
}

// ── Intervention log ("who steered the agents") ────────────────────
function initials(name) {
  const s = (name || "?").trim();
  const parts = s.split(/[\s@._-]+/).filter(Boolean);
  return ((parts[0]?.[0] || "?") + (parts[1]?.[0] || "")).toUpperCase();
}
function actorColor(name) {
  let h = 0; for (const c of (name || "")) h = (h * 31 + c.charCodeAt(0)) & 0xffff;
  const hues = [265, 210, 168, 300, 24, 190]; return `oklch(0.62 0.15 ${hues[h % hues.length]})`;
}
function relTime(ts) {
  const s = Math.max(0, Math.floor(Date.now() / 1000 - (ts || 0)));
  if (s < 60) return s + "s"; if (s < 3600) return Math.floor(s / 60) + "m";
  if (s < 86400) return Math.floor(s / 3600) + "h"; return Math.floor(s / 86400) + "d";
}
function onIntervention(d) {
  state.interventions = state.interventions || [];
  state.interventions.unshift(d);
  if (state.interventions.length > 200) state.interventions.pop();
  renderInterventions();
}
function renderInterventions() {
  const el = $("intervention-list");
  if (!el) return;
  const items = state.interventions || [];
  if (!items.length) {
    el.innerHTML = '<div class="empty-state-sm">No interventions yet. Message an agent to steer it.</div>';
    return;
  }
  el.innerHTML = "";
  items.slice(0, 40).forEach(i => {
    const actor = i.actor || "operator";
    const target = i.model && i.model !== "coordinator"
      ? `${i.challenge || ""}${i.challenge ? "/" : ""}${(i.model || "").split("/").slice(0, 2).join("/")}`
      : (i.challenge ? i.challenge : "coordinator");
    const isMsg = (i.action || "message") === "message";
    const div = document.createElement("div");
    div.className = "intv";
    div.innerHTML =
      `<span class="intv-av" style="background:${actorColor(actor)}">${escHtml(initials(actor))}</span>` +
      `<div class="intv-body"><div class="intv-head">` +
      `<span class="intv-actor">${escHtml(actor)}</span>` +
      `<span class="intv-target">${escHtml(target)}</span>` +
      `<span class="intv-time">${relTime(i.ts)}</span></div>` +
      (isMsg && i.text
        ? `<div class="intv-text">${escHtml(i.text)}</div>`
        : `<div class="intv-action">${escHtml(i.action || "steered")}</div>`) +
      `</div>`;
    el.appendChild(div);
  });
}

// ── Autopilot toggle ───────────────────────────────────────────────
function setupAutopilot() {
  const sw = $("autopilot-switch");
  if (!sw) return;
  let on = true;
  try { on = localStorage.getItem("fr_autopilot") !== "0"; } catch { /* ignore */ }
  state.autopilot = on;
  const apply = () => {
    sw.classList.toggle("on", state.autopilot);
    sw.setAttribute("aria-checked", String(state.autopilot));
  };
  apply();
  const toggle = () => {
    state.autopilot = !state.autopilot;
    try { localStorage.setItem("fr_autopilot", state.autopilot ? "1" : "0"); } catch { /* ignore */ }
    apply();
    toast(state.autopilot ? "Autopilot on — agents run autonomously." : "Autopilot off — start challenges manually.", "info");
  };
  sw.addEventListener("click", toggle);
  sw.addEventListener("keydown", e => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); toggle(); } });
}

// ── Fleet view (coordinator → swarms → solvers) ────────────────────
function setupFleetNav() {
  const navFleet = $("nav-fleet");
  const navCmd = $("nav-command");
  const fleetView = $("fleet-view");
  const detail = challengeDetail;
  const welcome = welcomeScreen;
  if (!navFleet || !fleetView) return;
  state.view = "command";
  navFleet.addEventListener("click", e => {
    e.preventDefault();
    state.view = "fleet";
    navFleet.classList.add("active"); if (navCmd) navCmd.classList.remove("active");
    if (welcome) welcome.style.display = "none";
    if (detail) detail.style.display = "none";
    fleetView.style.display = "flex";
    fleetView.style.flexDirection = "column";
    fleetView.style.flex = "1";
    fleetView.style.minHeight = "0";
    renderFleet();
  });
  if (navCmd) navCmd.addEventListener("click", e => {
    // Let the real "/" navigation happen only if not already here; otherwise switch view.
    if (state.view === "fleet") {
      e.preventDefault();
      state.view = "command";
      navCmd.classList.add("active"); navFleet.classList.remove("active");
      fleetView.style.display = "none";
      if (state.selectedChallenge) { if (detail) detail.style.display = "flex"; }
      else if (welcome) welcome.style.display = "flex";
    }
  });
}
function renderFleet() {
  const tree = $("fleet-tree");
  const countEl = $("fleet-count");
  if (!tree) return;
  const challenges = Object.values(state.challenges || {});
  const active = challenges.filter(c => c.status === "running" || (c.models && Object.keys(c.models).length));
  let solverCount = 0;
  active.forEach(c => { solverCount += c.models ? Object.keys(c.models).length : 0; });
  if (countEl) countEl.textContent = `${solverCount} solver${solverCount === 1 ? "" : "s"}`;

  tree.innerHTML = "";
  const rootRow = document.createElement("div");
  rootRow.className = "tnode-row";
  rootRow.innerHTML =
    `<span class="twist open">▸</span><span class="tnode-ic" style="background:var(--accent)">CO</span>` +
    `<span class="tnode-name">coordinator</span><span class="tnode-sub">${active.length} swarm${active.length === 1 ? "" : "s"}</span>`;
  tree.appendChild(rootRow);

  if (!active.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state-sm";
    empty.style.padding = "14px 12px";
    empty.textContent = "No live swarms. Start a run to populate the fleet.";
    tree.appendChild(empty);
    return;
  }

  const kidsWrap = document.createElement("div");
  kidsWrap.className = "tkids";
  active.forEach(c => {
    const chWrap = document.createElement("div");
    chWrap.className = "tnode";
    const models = c.models ? Object.entries(c.models) : [];
    const chRow = document.createElement("div");
    chRow.className = "tnode-row";
    chRow.innerHTML =
      `<span class="twist open">▸</span>` +
      `<span class="ch-status-dot ${c.status || "pending"}" style="margin:0 2px"></span>` +
      `<span class="tnode-name">${escHtml(c.name || "?")}</span>` +
      `<span class="tnode-sub">${escHtml(c.category || "")} · ${models.length} solver${models.length === 1 ? "" : "s"}</span>`;
    chWrap.appendChild(chRow);
    const mKids = document.createElement("div");
    mKids.className = "tkids";
    models.forEach(([spec, m]) => {
      const st = (m && m.status) || "running";
      const row = document.createElement("div");
      row.className = "tnode-row";
      row.style.cursor = "pointer";
      row.innerHTML =
        `<span class="twist"></span><span class="tnode-ic" style="background:${provColor(spec)}">${provAb(spec)}</span>` +
        `<span class="tnode-name">${escHtml(spec)}</span>` +
        `<span class="tnode-sub">${st === "won" ? "🏆 " : ""}${escHtml(st)} · ${m && m.steps || 0} steps · $${(m && m.cost || 0).toFixed(2)}</span>`;
      row.addEventListener("click", () => {
        // Jump to this challenge's detail (Command view) for full controls.
        if (navCmdClick) navCmdClick();
        selectChallenge(c.name);
      });
      mKids.appendChild(row);
    });
    chWrap.appendChild(mKids);
    // collapse toggle
    chRow.addEventListener("click", ev => {
      if (ev.target.closest(".tnode-row") !== chRow) return;
      mKids.classList.toggle("collapsed");
      chRow.querySelector(".twist").classList.toggle("open");
    });
    kidsWrap.appendChild(chWrap);
  });
  tree.appendChild(kidsWrap);
}
function navCmdClick() {
  const navCmd = $("nav-command"), navFleet = $("nav-fleet"), fleetView = $("fleet-view");
  if (!navCmd) return;
  state.view = "command";
  navCmd.classList.add("active"); if (navFleet) navFleet.classList.remove("active");
  if (fleetView) fleetView.style.display = "none";
}

document.addEventListener("DOMContentLoaded", init);
