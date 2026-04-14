/* ─────────────────────────────────────────────────────────────────
   CTF Agent Dashboard  —  Frontend JS
   ───────────────────────────────────────────────────────────────── */

"use strict";

// ── State ──────────────────────────────────────────────────────────
const state = {
  challenges: {},     // name → challenge object
  selectedChallenge: null,
  costByModel: {},
  totalCost: 0,
  totalTokens: 0,
  wsConnected: false,
  logAutoScroll: true,
  filter: "all",
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
const btnConfig        = $("btn-config");
const modalOverlay     = $("modal-overlay");
const modalClose       = $("modal-close");
const btnCancelConfig  = $("btn-cancel-config");
const btnSaveConfig    = $("btn-save-config");
const btnCopyFlag      = $("btn-copy-flag");

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
    case "snapshot":
      applySnapshot(evt.data);
      break;
    case "challenge_new":
    case "challenge_update":
    case "challenge_started":
      upsertChallenge(evt.data);
      break;
    case "challenge_solved":
      onChallengeSolved(evt.data);
      break;
    case "challenge_failed":
      onChallengeFailed(evt.data);
      break;
    case "solver_update":
      onSolverUpdate(evt.data);
      break;
    case "log_line":
      onLogLine(evt.data);
      break;
    case "cost_update":
      onCostUpdate(evt.data);
      break;
    case "ctfd_status":
      onCTFdStatus(evt.data);
      break;
  }
}

// ── Snapshot (full state on connect) ──────────────────────────────
function applySnapshot(data) {
  state.challenges = data.challenges || {};
  state.totalCost = data.total_cost || 0;
  state.totalTokens = data.total_tokens || 0;
  state.costByModel = data.cost_summary || {};

  if (data.ctfd_status) onCTFdStatus(data.ctfd_status);
  onCostUpdate({ total_cost: state.totalCost, total_tokens: state.totalTokens, by_model: state.costByModel });

  // Restore logs
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
  // Keep max 500
  if (state.challenges[name]._logs.length > 500) state.challenges[name]._logs.shift();
  if (state.selectedChallenge === name) appendLogLine(line);
}

function onCostUpdate(data) {
  if (data.total_cost !== undefined) state.totalCost = data.total_cost;
  if (data.total_tokens !== undefined) state.totalTokens = data.total_tokens;
  if (data.by_model) state.costByModel = data.by_model;
  costTotal.textContent = "$" + state.totalCost.toFixed(4);
  valCost.textContent = "$" + state.totalCost.toFixed(2);
  renderModelCosts();
}

function onCTFdStatus(data) {
  const connected = data.connected;
  ctfdBadge.className = "ctfd-badge " + (connected ? "connected" : "disconnected");
  ctfdLabel.textContent = "CTFd " + (connected ? "Connected" : "Disconnected");
}

// ── Render challenge list ──────────────────────────────────────────
function renderChallengeList() {
  const items = Object.values(state.challenges);
  const filtered = state.filter === "all" ? items : items.filter(c => c.status === state.filter);
  filtered.sort((a, b) => {
    // Running first, then by status
    const order = { running: 0, solved: 1, pending: 2, failed: 3 };
    return (order[a.status] ?? 99) - (order[b.status] ?? 99) || (a.name || "").localeCompare(b.name || "");
  });

  if (filtered.length === 0) {
    challengeList.innerHTML = '<div class="empty-state">No challenges match this filter.</div>';
    return;
  }

  challengeList.innerHTML = filtered.map(ch => `
    <div class="challenge-item${state.selectedChallenge === ch.name ? " active" : ""}" data-name="${escHtml(ch.name)}">
      <div class="ch-status-dot ${ch.status || "pending"}"></div>
      <div class="ch-info">
        <div class="ch-name">${escHtml(ch.name)}</div>
        <div class="ch-meta">${escHtml(ch.category || "")}${ch.flag ? " • " + escHtml(ch.flag) : ""}</div>
      </div>
      <div class="ch-pts">${ch.value ? ch.value + "pt" : ""}</div>
    </div>
  `).join("");

  challengeList.querySelectorAll(".challenge-item").forEach(el => {
    el.addEventListener("click", () => selectChallenge(el.dataset.name));
  });
}

function updateHeaderStats() {
  const all = Object.values(state.challenges);
  valChallenges.textContent = all.length;
  valSolved.textContent = all.filter(c => c.status === "solved").length;
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
  }
}

function renderChallengeDetail(ch) {
  detailName.textContent = ch.name;
  detailStatus.textContent = ch.status || "pending";
  detailStatus.className = "status-badge " + (ch.status || "pending");
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

function updateModelsGrid(ch) {
  if (state.selectedChallenge !== ch.name) return;
  const models = ch.models || {};
  const specs = ch.model_specs || ch.models_list || Object.keys(models);

  if (specs.length === 0 && Object.keys(models).length === 0) {
    modelsGrid.innerHTML = '<div class="empty-state">No models running yet.</div>';
    return;
  }

  const allSpecs = [...new Set([...specs, ...Object.keys(models)])];

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
  logContainer.innerHTML = "";
  (ch._logs || []).forEach(line => appendLogLine(line, false));
  if (state.logAutoScroll) logContainer.scrollTop = logContainer.scrollHeight;
}

function appendLogLine(line, doScroll = true) {
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
btnSendMsg.addEventListener("click", async () => {
  const msg = msgInput.value.trim();
  if (!msg) return;
  msgStatus.textContent = "Sending…";
  msgStatus.className = "msg-status";
  try {
    const res = await fetch("/api/message", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: msg }),
    });
    const data = await res.json();
    if (data.ok) {
      msgStatus.textContent = "Sent!";
      msgStatus.className = "msg-status ok";
      msgInput.value = "";
    } else {
      msgStatus.textContent = data.error || "Failed";
      msgStatus.className = "msg-status err";
    }
  } catch {
    msgStatus.textContent = "Network error";
    msgStatus.className = "msg-status err";
  }
  setTimeout(() => { msgStatus.textContent = ""; }, 4000);
});

// ── Config modal ───────────────────────────────────────────────────
btnConfig.addEventListener("click", () => { modalOverlay.style.display = "flex"; });
modalClose.addEventListener("click", closeModal);
btnCancelConfig.addEventListener("click", closeModal);
modalOverlay.addEventListener("click", e => { if (e.target === modalOverlay) closeModal(); });

function closeModal() { modalOverlay.style.display = "none"; }

btnSaveConfig.addEventListener("click", async () => {
  const body = {};
  const map = [
    ["cfg-ctfd-url", "ctfd_url"],
    ["cfg-ctfd-token", "ctfd_token"],
    ["cfg-anthropic", "anthropic_api_key"],
    ["cfg-openai", "openai_api_key"],
    ["cfg-gemini", "gemini_api_key"],
  ];
  map.forEach(([id, key]) => {
    const val = $(id).value.trim();
    if (val) body[key] = val;
  });
  try {
    const res = await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (data.ok) {
      closeModal();
      alert(`Saved: ${data.updated.join(", ") || "nothing changed"}`);
    }
  } catch {
    alert("Failed to save config");
  }
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

// ── Log auto-scroll toggle ─────────────────────────────────────────
logAutoScrollChk.addEventListener("change", () => {
  state.logAutoScroll = logAutoScrollChk.checked;
});

// ── Copy flag ──────────────────────────────────────────────────────
btnCopyFlag.addEventListener("click", () => {
  navigator.clipboard.writeText(flagText.textContent).then(() => {
    btnCopyFlag.textContent = "Copied!";
    setTimeout(() => { btnCopyFlag.textContent = "Copy"; }, 2000);
  });
});

// ── Utility ───────────────────────────────────────────────────────
function escHtml(str) {
  if (!str) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// ── Init ───────────────────────────────────────────────────────────
function init() {
  connectWS();
  updateWSStatus("connecting");
  // Poll for status every 10s as a fallback
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
