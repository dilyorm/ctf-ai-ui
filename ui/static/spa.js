/* Flagrunner SPA — mission-control shell wired to the live backend.
   Screens: command, fleet, agent, team, connect, board. */
"use strict";
(function () {
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const el = (t, c, h) => { const e = document.createElement(t); if (c) e.className = c; if (h != null) e.innerHTML = h; return e; };
  const esc = s => String(s == null ? "" : s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;

  // ---------- providers ----------
  const PROV_VAR = { "claude-sdk": "--claude", claude: "--claude", codex: "--codex", "codex-sub": "--codex",
    copilot: "--copilot", google: "--gemini", gemini: "--gemini", antigravity: "--antigravity",
    grok: "--grok", kimi: "--kimi", bedrock: "--claude", azure: "--copilot", zen: "--purple", openai: "--codex" };
  const provOf = spec => (spec || "").split("/")[0];
  const provColor = spec => `var(${PROV_VAR[provOf(spec)] || "--accent2"})`;
  const provAb = spec => { const p = provOf(spec); const m = { "claude-sdk": "CL", claude: "CL", codex: "GX", "codex-sub": "GX",
    copilot: "CP", google: "GM", gemini: "GM", antigravity: "AG", grok: "GK", kimi: "KM", bedrock: "BR", azure: "AZ", zen: "ZN", openai: "AI" };
    return m[p] || p.slice(0, 2).toUpperCase(); };
  const catClass = c => { const k = (c || "").toLowerCase();
    if (/pwn|binary|exploit/.test(k)) return "pwn"; if (/crypto/.test(k)) return "crypto"; if (/web/.test(k)) return "web";
    if (/rev|reversing/.test(k)) return "rev"; if (/forensic|stego/.test(k)) return "forensics"; return "misc"; };
  const modelShort = spec => (spec || "").split("/").slice(1, 3).join("·") || spec;

  const initials = n => { const p = String(n || "?").split(/[\s@._-]+/).filter(Boolean); return ((p[0]?.[0] || "?") + (p[1]?.[0] || "")).toUpperCase(); };
  const actorColor = n => { let h = 0; for (const c of String(n || "")) h = (h * 31 + c.charCodeAt(0)) & 0xffff; const hues = [278, 210, 165, 300, 24, 190]; return `oklch(0.6 0.15 ${hues[h % hues.length]})`; };
  const relTime = ts => { const s = Math.max(0, Math.floor(Date.now() / 1000 - (ts || 0))); if (s < 60) return s + "s"; if (s < 3600) return (s / 60 | 0) + "m"; if (s < 86400) return (s / 3600 | 0) + "h"; return (s / 86400 | 0) + "d"; };
  const pad = n => String(n).padStart(2, "0");

  // ---------- state ----------
  const state = {
    screen: "command", challenges: {}, logs: {}, cost: { total: 0, tokens: 0, byModel: {} },
    ctfd: { connected: false }, interventions: [], events: [], accounts: [], members: [], ctfs: [], tasks: [],
    selChal: null, selAgent: null, run: { running: false }, autopilot: true, ws: false,
  };

  // ---------- toast ----------
  function toast(msg, kind = "success") {
    const t = el("div", "toast " + kind, esc(msg)); $("#toasts").appendChild(t);
    setTimeout(() => { t.style.opacity = "0"; setTimeout(() => t.remove(), 250); }, 3200);
  }
  async function api(path, opts) {
    const r = await fetch(path, opts); let d = {}; try { d = await r.json(); } catch { /* */ }
    return { ok: r.ok, status: r.status, data: d };
  }

  // ---------- websocket ----------
  let ws = null, wsTimer = null;
  function connectWS() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/ws`);
    ws.onopen = () => { state.ws = true; setWs(true); if (wsTimer) { clearTimeout(wsTimer); wsTimer = null; } };
    ws.onmessage = e => { try { onEvent(JSON.parse(e.data)); } catch (err) { console.error(err); } };
    ws.onclose = ws.onerror = () => { state.ws = false; setWs(false); wsTimer = setTimeout(connectWS, 3000); };
  }
  function setWs(on) { const d = $("#wsDot"); if (d) d.className = "dot" + (on ? " live" : " red"); const t = $("#onlineTxt"); if (t) t.textContent = on ? "live" : "offline"; }

  function onEvent(evt) {
    const d = evt.data || {};
    switch (evt.type) {
      case "snapshot":
        state.challenges = d.challenges || {};
        state.cost = { total: d.total_cost || 0, tokens: d.total_tokens || 0, byModel: d.cost_summary || {} };
        state.ctfd = d.ctfd_status || state.ctfd;
        state.interventions = d.interventions || [];
        state.logs = {}; Object.entries(d.logs || {}).forEach(([k, v]) => state.logs[k] = v.slice());
        // seed activity feed from recent interventions + already-solved challenges
        state.events = [];
        state.interventions.slice(0, 12).forEach(i => { const n = new Date((i.ts || 0) * 1000);
          state.events.push({ cls: "op", who: i.actor, t: `${pad(n.getUTCHours())}:${pad(n.getUTCMinutes())}`, msg: i.model && i.model !== "coordinator" ? `→ ${i.challenge}/${modelShort(i.model)}: ${i.text || i.action}` : (i.text || i.action) }); });
        Object.values(state.challenges).filter(c => c.status === "solved").forEach(c => state.events.push({ cls: "win", who: c.winner_model || "solver", t: "", msg: `flag ${c.name} → ${c.flag || "captured"}` }));
        break;
      case "challenge_new": case "challenge_update": case "challenge_started":
        upsert(d); if (evt.type === "challenge_started") pushEvent("", "coordinator", `spawned swarm · ${d.name}`); break;
      case "challenge_solved":
        upsert({ name: d.name, status: "solved", flag: d.flag, winner_model: d.winner_model });
        pushEvent("win", d.winner_model || "solver", `flag ${d.name} → ${d.flag || "captured"}`); break;
      case "challenge_failed": upsert({ name: d.name, status: "failed" }); break;
      case "solver_update": {
        const ch = state.challenges[d.challenge] || (state.challenges[d.challenge] = { name: d.challenge, models: {} });
        ch.models = ch.models || {}; ch.models[d.model] = { status: d.status, steps: d.steps, cost: d.cost, findings: d.findings };
        break; }
      case "log_line": {
        (state.logs[d.challenge] = state.logs[d.challenge] || []).push({ ts: evt.timestamp, model: d.model, text: d.text, level: d.level });
        if (state.logs[d.challenge].length > 600) state.logs[d.challenge].shift(); break; }
      case "cost_update": state.cost = { total: d.total_cost ?? state.cost.total, tokens: d.total_tokens ?? state.cost.tokens, byModel: d.by_model || state.cost.byModel }; break;
      case "ctfd_status": state.ctfd = Object.assign({}, state.ctfd, d); break;
      case "agent_intervention": state.interventions.unshift(d); if (state.interventions.length > 200) state.interventions.pop();
        pushEvent("op", d.actor, d.model && d.model !== "coordinator" ? `→ ${d.challenge}/${modelShort(d.model)}: ${d.text || d.action}` : (d.text || d.action)); break;
    }
    scheduleRender();
  }
  function upsert(d) { const n = d.name; if (!n) return; const ex = state.challenges[n] || { name: n, models: {} }; state.challenges[n] = Object.assign(ex, d); }
  function pushEvent(cls, who, msg) { const now = new Date(); state.events.unshift({ cls, who, msg, t: `${pad(now.getUTCHours())}:${pad(now.getUTCMinutes())}:${pad(now.getUTCSeconds())}` }); if (state.events.length > 60) state.events.pop(); }

  let renderPending = false;
  function scheduleRender() { if (renderPending) return; renderPending = true; requestAnimationFrame(() => { renderPending = false; renderScreen(state.screen); updateTopbar(); }); }

  // ---------- topbar / nav ----------
  const NAMES = { command: ["Command", () => crumbCtf()], fleet: ["Agent Fleet", () => "coordinator → swarms → solvers"],
    agent: ["Agent Detail", () => state.selAgent ? `${state.selAgent.challenge} · ${modelShort(state.selAgent.model)}` : "no agent selected"],
    team: ["Team & Subscriptions", () => `${state.members.length} members · ${state.accounts.length} accounts`],
    connect: ["Connect Platform", () => "agent-driven adapter builder"], board: ["Kanban", () => "synced with the swarms"] };
  function crumbCtf() { const sel = $("#runCtf"); const c = state.ctfs.find(x => String(x.id) === (sel && sel.value)); return c ? `${c.name} · ${c.platform}` : (state.ctfd.url || "no CTF selected"); }
  function go(name) {
    state.screen = name;
    $$(".screen").forEach(s => s.classList.toggle("active", s.dataset.screen === name));
    $$(".rail-btn").forEach(b => b.classList.toggle("active", b.dataset.nav === name));
    $("#screenName").textContent = NAMES[name][0];
    renderScreen(name); updateTopbar(); $(".main").scrollTop = 0;
  }
  function updateTopbar() {
    $("#crumb").textContent = NAMES[state.screen][1]();
    $("#costTxt").textContent = "$" + (state.cost.total || 0).toFixed(2);
    const online = state.members.filter(m => m.is_active !== false).length || state.members.length;
    $("#onlineTxt").textContent = state.ws ? `${online || "–"} online` : "offline";
  }

  function renderScreen(name) {
    if (name === "command") renderCommand();
    else if (name === "fleet") renderFleet();
    else if (name === "agent") renderAgent();
    else if (name === "team") renderTeam();
    else if (name === "board") renderBoard();
    // connect is static form; rendered on demand
  }

  // ---------- COMMAND ----------
  function challengeArr() { return Object.values(state.challenges); }
  function renderCommand() {
    const arr = challengeArr();
    const solved = arr.filter(c => c.status === "solved").length;
    const running = arr.filter(c => c.status === "running").length;
    const parked = arr.filter(c => c.status === "parked").length;
    const healthy = state.accounts.filter(a => a.status === "healthy" || a.status === "in_use").length;
    const kpi = (n, k, sub, color) => `<div class="panel kpi"><div class="n mono">${n}</div><div class="k label">${k}</div><div class="sub" style="color:${color || 'var(--text2)'}">${sub}</div></div>`;
    $("#kpis").innerHTML =
      kpi(`${solved}<small>/${arr.length}</small>`, "Flags captured", arr.length ? `${Math.round(solved / arr.length * 100)}% solved` : "no challenges", "var(--green)") +
      kpi(running, "Swarms live", `${countSolvers()} solvers${parked ? " · " + parked + " parked" : ""}`) +
      kpi("$" + state.cost.total.toFixed(2), "Spend", "subscriptions pooled", "var(--cyan)") +
      kpi(`${healthy}<small>/${state.accounts.length}</small>`, "Subs healthy", `${state.accounts.filter(a => a.status === "cooling").length} cooling`, "var(--purple)");

    $("#chalCount").textContent = running + " running";
    const list = $("#chalList");
    if (!arr.length) { list.innerHTML = '<div class="empty">No challenges yet. Select a CTF and start a run.</div>'; }
    else {
      const order = { running: 0, parked: 1, pending: 2, solved: 3, failed: 4 };
      arr.sort((a, b) => (order[a.status] ?? 5) - (order[b.status] ?? 5) || (b.value || 0) - (a.value || 0));
      list.innerHTML = "";
      arr.forEach(c => {
        const models = c.models ? Object.keys(c.models) : [];
        const racers = models.slice(0, 5).map(s => `<span class="rc" style="background:${provColor(s)}" title="${esc(s)}">${provAb(s)}</span>`).join("");
        const note = c.status === "solved" ? "✓ flag" : c.status === "parked" ? "parked" : c.status === "pending" ? "queued" : models.length ? models.length + " racing" : (c.status || "");
        const row = el("div", "chal");
        row.innerHTML = `<span class="st ${c.status || "pending"}"></span><div><div class="nm">${esc(c.name)}</div>
          <div class="meta"><span class="cat ${catClass(c.category)}">${esc(c.category || "misc")}</span><span>${esc(note)}</span></div></div>
          <div style="display:flex;align-items:center;gap:12px"><div class="racers">${racers}</div><div class="pts">${c.value || 0}</div></div>`;
        row.onclick = () => { state.selChal = c.name; go("fleet"); };
        list.appendChild(row);
      });
    }

    // feed
    const feed = $("#feed");
    const items = state.events.slice(0, 40);
    if (!items.length) feed.innerHTML = '<div class="empty">Quiet. Activity from agents and teammates shows here.</div>';
    else feed.innerHTML = items.map(f => `<div class="ln ${f.cls}"><span class="t">${f.t}</span><span class="who" style="color:${whoColor(f.who)}">${esc(f.who)}</span><span class="msg">${esc(f.msg)}</span></div>`).join("");

    // pool
    const pool = $("#poolMini"); $("#poolCount").textContent = state.accounts.length ? state.accounts.length + " accounts" : "shared";
    if (!state.accounts.length) pool.innerHTML = '<div class="empty">No accounts pooled. Connect one on Team &amp; Subs.</div>';
    else pool.innerHTML = state.accounts.slice(0, 8).map(a => {
      const pct = Math.round((a.active_leases || 0) / Math.max(1, a.max_concurrent || 1) * 100);
      const color = a.status === "cooling" ? "var(--orange)" : a.status === "disabled" ? "var(--text3)" : pct >= 100 ? "var(--red)" : "var(--green)";
      return `<div class="acctrow"><span class="node-ic" style="background:${provColor(a.provider)}">${provAb(a.provider)}</span>
        <div style="flex:1;min-width:0"><div class="mono" style="font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(a.label)}</div>
        <div class="bar" style="margin-top:5px"><i style="width:${a.status === "disabled" ? 0 : pct}%;background:${color}"></i></div></div>
        <div class="mono" style="font-size:10.5px;color:${color};min-width:66px;text-align:right">${esc((a.status || "").replace("_", " "))}</div></div>`;
    }).join("");
  }
  function countSolvers() { let n = 0; challengeArr().forEach(c => { if (c.status === "running" && c.models) n += Object.keys(c.models).length; }); return n; }
  function whoColor(w) { const m = state.members.find(x => (x.display_name || x.email) === w); if (m) return actorColor(w);
    if (/^claude/.test(w)) return "var(--claude)"; if (/^gpt|codex/.test(w)) return "var(--codex)"; if (/^copilot/.test(w)) return "var(--copilot)";
    if (/^grok/.test(w)) return "var(--text)"; if (/^kimi/.test(w)) return "var(--kimi)"; if (w === "coordinator") return "var(--accent)"; return "var(--text2)"; }

  // ---------- FLEET ----------
  function renderFleet() {
    const arr = challengeArr().filter(c => c.status === "running" || (c.models && Object.keys(c.models).length));
    $("#fleetCount").textContent = countSolvers() + " solvers";
    const tree = $("#tree");
    if (!arr.length) { tree.innerHTML = '<div class="empty">No live swarms. Start a run on Command.</div>'; }
    else {
      tree.innerHTML = "";
      const root = el("div", "node-row");
      root.innerHTML = `<span class="twist open">▸</span><span class="node-ic" style="background:var(--accent);color:#fff">CO</span><span class="node-nm">coordinator</span><span class="node-sub">${arr.length} swarm${arr.length === 1 ? "" : "s"}</span>`;
      tree.appendChild(root);
      const kids = el("div", "kids");
      arr.forEach(c => {
        const models = c.models ? Object.entries(c.models) : [];
        const chRow = el("div", "node-row");
        chRow.innerHTML = `<span class="twist open">▸</span><span class="st ${c.status || "pending"}" style="width:9px;height:9px;border-radius:50%"></span>
          <span class="node-nm">${esc(c.name)}</span><span class="node-sub">${esc(c.category || "")} · ${models.length}</span>`;
        const mKids = el("div", "kids");
        models.forEach(([spec, m]) => {
          const r = el("div", "node-row" + (state.selAgent && state.selAgent.challenge === c.name && state.selAgent.model === spec ? " sel" : ""));
          r.innerHTML = `<span class="twist"></span><span class="node-ic" style="background:${provColor(spec)}">${provAb(spec)}</span>
            <span class="node-nm">${esc(modelShort(spec))}</span><span class="node-sub">${m.status === "won" ? "🏆 " : ""}${esc(m.status || "run")} · ${m.steps || 0}st</span>`;
          r.onclick = e => { e.stopPropagation(); openAgent(c.name, spec); };
          mKids.appendChild(r);
        });
        chRow.onclick = () => { state.selChal = c.name; renderFleet(); };
        kids.appendChild(chRow); kids.appendChild(mKids);
      });
      tree.appendChild(kids);
    }
    // right: selected swarm agent cards
    if (!state.selChal || !state.challenges[state.selChal]) { state.selChal = arr[0] && arr[0].name || null; }
    const c = state.selChal && state.challenges[state.selChal];
    $("#swarmTitle").textContent = c ? c.name : "Select a challenge";
    $("#swarmMeta").textContent = c ? `${c.value || 0} pts` : "";
    const cards = $("#agCards");
    if (!c || !c.models || !Object.keys(c.models).length) { cards.innerHTML = '<div class="empty">Pick a swarm from the tree.</div>'; return; }
    cards.innerHTML = "";
    Object.entries(c.models).forEach(([spec, m]) => {
      const card = el("div", "panel agcard " + (m.status === "won" ? "won" : m.status === "running" ? "running" : ""));
      const now = latestLog(c.name, spec);
      card.innerHTML = `<div class="prov"><span class="node-ic" style="background:${provColor(spec)}">${provAb(spec)}</span><h4>${esc(modelShort(spec))}</h4>
        <span class="tag ${m.status === "won" ? "green" : "amber"}" style="margin-left:auto">${m.status === "won" ? "🏆 won" : esc(m.status || "run")}</span></div>
        <div class="row"><span>steps</span><b>${m.steps || 0}</b></div><div class="row"><span>cost</span><b>$${(m.cost || 0).toFixed(2)}</b></div>
        <div class="now">▸ ${esc(now || (m.findings || "").slice(0, 80) || "working…")}</div>
        <div class="ctrls"><button class="icobtn" title="Open transcript">↗</button><button class="icobtn" title="Message">✎</button>
        <button class="icobtn" title="Restart">↻</button><button class="icobtn" title="Stop">⏹</button></div>`;
      const [open, msg, restart, stop] = card.querySelectorAll(".icobtn");
      open.onclick = () => openAgent(c.name, spec);
      msg.onclick = () => { openAgent(c.name, spec); $("#agInput").focus(); };
      restart.onclick = () => agentAction("restart", c.name, spec);
      stop.onclick = () => agentAction("stop", c.name, spec);
      cards.appendChild(card);
    });
  }
  function latestLog(ch, spec) { const L = state.logs[ch] || []; for (let i = L.length - 1; i >= 0; i--) if (!spec || L[i].model === spec || modelShort(spec).includes(L[i].model) || (L[i].model || "").includes(modelShort(spec).split("·")[0])) return L[i].text; return ""; }

  // ---------- AGENT ----------
  function openAgent(challenge, model) { state.selAgent = { challenge, model }; go("agent"); }
  function renderAgent() {
    const sel = state.selAgent;
    const enable = !!sel;
    $("#agInput").disabled = !enable; $("#agSend").disabled = !enable;
    $$("#agControls .btn").forEach(b => b.disabled = !enable);
    if (!sel) { $("#agTitle").textContent = "No agent selected"; $("#agSub").textContent = "open one from the Fleet";
      $("#transcript").innerHTML = '<div class="empty">Pick a solver in the Fleet to watch its live transcript here.</div>';
      $("#whoList").innerHTML = '<div class="empty">No interventions yet.</div>'; $("#siblings").innerHTML = '<div class="empty">—</div>'; return; }
    const ch = state.challenges[sel.challenge] || {}; const m = (ch.models || {})[sel.model] || {};
    $("#agIc").style.background = provColor(sel.model); $("#agIc").textContent = provAb(sel.model);
    $("#agTitle").textContent = modelShort(sel.model);
    $("#agSub").innerHTML = `${esc(sel.challenge)} · step ${m.steps || 0} · $${(m.cost || 0).toFixed(2)}`;
    $("#agStatus").textContent = m.status || "";
    // transcript from logs filtered to this model (best-effort match)
    const L = (state.logs[sel.challenge] || []).filter(x => !x.model || x.model === sel.model || modelShort(sel.model).split("·")[0].includes(x.model) || (x.model || "").includes(sel.model.split("/")[1] || ""));
    const rows = (L.length ? L : (state.logs[sel.challenge] || [])).slice(-120);
    const t = $("#transcript");
    if (!rows.length) t.innerHTML = '<div class="empty">No output yet from this solver.</div>';
    else {
      t.innerHTML = rows.map(x => {
        const kind = { error: "error", warning: "info", success: "out", info: "tool", debug: "think" }[x.level] || "tool";
        return `<div class="turn"><div class="head"><span class="kind ${kind}">${esc(x.level || "log")}</span><span class="ts">${x.model ? esc(x.model) : ""}</span></div><div class="say">${esc(x.text)}</div></div>`;
      }).join("");
      t.scrollTop = t.scrollHeight;
    }
    // who steered
    const who = state.interventions.filter(i => i.challenge === sel.challenge && (i.model === sel.model || !i.model));
    $("#whoList").innerHTML = who.length ? who.slice(0, 20).map(i => `<div class="who-msg"><span class="who-av" style="background:${actorColor(i.actor)}">${esc(initials(i.actor))}</span>
      <div class="body"><span class="n">${esc(i.actor)}</span><span class="tm">${relTime(i.ts)}</span><p>${esc(i.text || i.action)}</p></div></div>`).join("")
      : '<div class="empty">No interventions on this agent yet.</div>';
    // siblings
    const sibs = Object.entries(ch.models || {}).filter(([s]) => s !== sel.model && (ch.models[s].findings));
    $("#siblings").innerHTML = sibs.length ? sibs.map(([s, mm]) => `<div><span style="color:${provColor(s)}">${esc(modelShort(s))}</span> → ${esc((mm.findings || "").slice(0, 120))}</div>`).join("")
      : '<div class="empty">No sibling findings yet.</div>';
  }
  async function agentAction(act, challenge, model, extra) {
    const body = Object.assign({ challenge, model_spec: model }, extra || {});
    const r = await api(`/api/run/agent/${act}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (r.data.ok) toast(`${act} sent to ${modelShort(model)}`); else toast(r.data.error || `no live agent for ${act}`, "error");
  }
  $("#agSend").onclick = () => { const v = $("#agInput").value.trim(); if (!v || !state.selAgent) return; agentAction("message", state.selAgent.challenge, state.selAgent.model, { text: v }); $("#agInput").value = ""; };
  $("#agInput").addEventListener("keydown", e => { if (e.key === "Enter") $("#agSend").click(); });
  $("#agControls").addEventListener("click", e => { const b = e.target.closest(".btn"); if (!b || !state.selAgent) return; agentAction(b.dataset.act, state.selAgent.challenge, state.selAgent.model); });

  // ---------- TEAM ----------
  function renderTeam() {
    const grid = $("#teamGrid");
    if (!state.members.length && !state.accounts.length) { grid.innerHTML = '<div class="empty">No team members or accounts yet.</div>'; return; }
    const byOwner = {}; state.accounts.forEach(a => { const k = a.owner || "shared"; (byOwner[k] = byOwner[k] || []).push(a); });
    const cards = [];
    const seen = new Set();
    state.members.forEach(m => {
      const email = m.email || m.login || ""; seen.add(email);
      const subs = byOwner[email] || [];
      cards.push(memberCard(m.display_name || email, m.role || "member", subs, m.is_active !== false));
    });
    // shared / other-owner accounts not tied to a listed member
    Object.entries(byOwner).forEach(([owner, subs]) => { if (owner !== "shared" && !seen.has(owner)) cards.push(memberCard(owner, "member", subs, true)); });
    if (byOwner.shared) cards.push(memberCard("Shared pool", "unowned", byOwner.shared, true));
    grid.innerHTML = cards.join("") || '<div class="empty">No members yet.</div>';
  }
  function memberCard(name, role, subs, online) {
    const subHtml = subs.length ? subs.map(a => {
      const color = a.status === "cooling" ? "var(--orange)" : a.status === "disabled" ? "var(--text3)" : "var(--green)";
      return `<div class="sub"><span class="node-ic" style="background:${provColor(a.provider)}">${provAb(a.provider)}</span>
        <span class="snm">${esc(a.label)}</span><span class="status" style="color:${color}">${esc((a.status || "").replace("_", " "))} ${a.active_leases || 0}/${a.max_concurrent || 1}</span></div>`;
    }).join("") : '<div class="note">No subscriptions connected.</div>';
    return `<div class="panel member"><div class="top"><span class="av" style="background:${actorColor(name)}">${esc(initials(name))}</span>
      <div><h4>${esc(name)}</h4><div class="role">${esc(role)}</div></div><span class="dot ${online ? "live" : ""}" style="margin-left:auto"></span></div>
      <div class="subs">${subHtml}</div></div>`;
  }

  // ---------- CONNECT (probe wizard) ----------
  let probeDraft = null;
  function bubble(kind, who, html) { const c = $("#connectChat"); if (c.querySelector(".empty")) c.innerHTML = "";
    const b = el("div", "bub " + kind, (kind === "ai" && who ? `<span class="who">${esc(who)}</span>` : "") + html); c.appendChild(b); c.scrollTop = c.scrollHeight; }
  function jparse(s, f) { try { return JSON.parse(s); } catch { return f; } }
  function buildAdapter(draft, answers) {
    const a = JSON.parse(JSON.stringify(draft || {}));
    a.list = a.list || { method: "GET", path: "", items_path: "data" };
    a.submit = a.submit || { method: "POST", path: "", body_template: { flag: "{flag}" }, success: {} };
    a.auth = a.auth || { mode: "bearer", header: "Authorization", prefix: "Bearer " };
    if (answers["list.path"]) a.list.path = answers["list.path"];
    if (answers["auth.mode"]) { a.auth.mode = answers["auth.mode"]; if (a.auth.mode === "bearer") { a.auth.header = "Authorization"; a.auth.prefix = "Bearer "; } }
    if (answers["submit.path"]) a.submit.path = answers["submit.path"];
    if (answers["submit.body"]) a.submit.body_template = jparse(answers["submit.body"], a.submit.body_template);
    if (answers["submit.success"]) { const raw = answers["submit.success"].trim(); const m = raw.match(/^([\w.]+)\s*==\s*(.+)$/);
      if (m) a.submit.success = { status_path: m[1], correct_values: [m[2].trim().replace(/^["']|["']$/g, "")] }; else { const j = jparse(raw, null); if (j) a.submit.success = j; } }
    return a;
  }
  async function runProbe() {
    const url = $("#cxUrl").value.trim(); if (!url) { toast("Enter the site URL", "error"); return; }
    $("#connectChat").innerHTML = ""; $("#cxQuestions").innerHTML = ""; $("#cxState").textContent = "probing";
    bubble("sys", null, "connector agent probing " + esc(url) + " …");
    const btn = $("#cxProbe"); btn.disabled = true; btn.textContent = "Probing…";
    const r = await api("/api/platform/probe", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, token: $("#cxToken").value.trim(), context: $("#cxContext").value.trim(), platform_hint: $("#cxType").value }) });
    btn.disabled = false; btn.textContent = "Probe & connect"; $("#cxState").textContent = "idle";
    if (!r.data.ok) { bubble("ai", "Connector agent", esc(r.data.error || "Probe failed.")); return; }
    probeDraft = r.data.adapter;
    (r.data.log || []).forEach(l => bubble("sys", null, esc(l)));
    if (r.data.kind === "ctfd" || r.data.kind === "rctf") {
      bubble("ai", "Connector agent", `Standard <b>${r.data.kind}</b> platform (${Math.round((r.data.confidence || 0) * 100)}% sure). Ready to save.`);
      saveBar(r.data.kind); return;
    }
    bubble("ai", "Connector agent", r.data.kind === "generic" ? "Not stock CTFd/rCTF, but I found the challenge list. I need a few details to submit flags:" : "Couldn't auto-detect the API. Tell me where things live:");
    renderQuestions(r.data.questions || []);
  }
  function renderQuestions(qs) {
    const box = $("#cxQuestions"); box.innerHTML = "";
    qs.forEach(q => { const w = el("div", "field"); const lab = el("label", "label", esc(q.prompt)); w.appendChild(lab);
      let inp; if (q.kind === "choice") { inp = el("select"); (q.options || []).forEach(o => { const op = el("option"); op.value = o; op.textContent = o; inp.appendChild(op); }); if (q.suggestion) inp.value = q.suggestion; }
      else { inp = el("input"); inp.placeholder = q.suggestion || ""; } inp.dataset.qid = q.id; w.appendChild(inp); box.appendChild(w); });
    saveBar("generic");
  }
  function collectAnswers() { const a = {}; $$("#cxQuestions [data-qid]").forEach(e => { if (e.value && e.value.trim()) a[e.dataset.qid] = e.value.trim(); }); return a; }
  function saveBar(kind) {
    const box = $("#cxQuestions"); const bar = el("div"); bar.style.cssText = "display:flex;gap:8px;justify-content:flex-end;margin-top:14px";
    const vb = el("button", "btn sm", "Validate"), sb = el("button", "btn primary sm", "Save platform");
    vb.onclick = async () => { const adapter = kind === "generic" ? buildAdapter(probeDraft, collectAnswers()) : probeDraft; vb.disabled = true; vb.textContent = "…";
      const r = await api("/api/platform/validate", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url: $("#cxUrl").value.trim(), token: $("#cxToken").value.trim(), adapter }) });
      bubble("ai", "Connector agent", (r.data.ok ? "✓ " : "✕ ") + esc(r.data.message || r.data.error || "")); vb.disabled = false; vb.textContent = "Validate"; };
    sb.onclick = async () => { const name = $("#cxName").value.trim(); if (!name) { toast("Give the CTF a name", "error"); return; }
      const adapter = kind === "generic" ? buildAdapter(probeDraft, collectAnswers()) : probeDraft; sb.disabled = true; sb.textContent = "Saving…";
      const r = await api("/api/ctfs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name, platform: kind === "generic" ? "generic" : kind, ctfd_url: $("#cxUrl").value.trim(), ctfd_token: $("#cxToken").value.trim(), adapter: kind === "generic" ? adapter : undefined }) });
      if (r.data.ok) { toast(`Saved "${name}"`); loadCtfs(); go("command"); } else { toast(r.data.error || "Save failed", "error"); sb.disabled = false; sb.textContent = "Save platform"; } };
    bar.appendChild(vb); bar.appendChild(sb); box.appendChild(bar);
  }
  $("#cxProbe").onclick = runProbe;

  // ---------- BOARD ----------
  const STATUS_COLS = [["todo", "To Do"], ["in_progress", "In Progress"], ["blocked", "Blocked"], ["needs_review", "Needs Review"], ["solved", "Solved"], ["skipped", "Skipped"]];
  async function loadBoard() {
    const sel = $("#boardCtf"); const id = sel.value; const board = $("#board");
    if (!id) { board.innerHTML = '<div class="empty">Select a CTF to load its board.</div>'; return; }
    const r = await api(`/api/team/tasks?ctf_id=${id}`);
    state.tasks = r.data.tasks || [];
    board.innerHTML = STATUS_COLS.map(([key, title]) => {
      const items = state.tasks.filter(t => (t.status || "todo") === key);
      const cards = items.map(t => `<div class="card"><div class="cn">${esc(t.name)}</div><div class="cc">
        <span class="cat ${catClass(t.category)}">${esc(t.category || "misc")}</span>
        <span class="mono" style="color:var(--text3);font-size:11px">${t.points || 0}pt</span>
        ${t.assignee_type === "ai" ? '<span class="tag purple" style="margin-left:auto">🤖 AI</span>' : (t.flag ? '<span class="tag green" style="margin-left:auto">✓</span>' : "")}</div></div>`).join("") || '<div class="note">—</div>';
      return `<div class="col"><div class="col-h">${title}<span class="ct">${items.length}</span></div>${cards}</div>`;
    }).join("");
  }
  $("#boardCtf").onchange = loadBoard;

  // ---------- RUN CONTROL ----------
  async function runStart() {
    const ctf = $("#runCtf").value; if (!ctf) { toast("Select a CTF first", "error"); return; }
    const btn = $("#btnStart"); btn.disabled = true; btn.textContent = "Starting…";
    const r = await api("/api/run/start", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ctf_id: parseInt(ctf), coordinator: "claude", max_concurrent_challenges: parseInt($("#runConc").value) || 10, no_submit: $("#runDry").checked, autopilot: state.autopilot }) });
    btn.disabled = false; btn.textContent = "▶ Start run";
    if (r.data.ok) toast("Run started."); else toast(r.data.error || "Failed to start", "error");
    refreshRun();
  }
  async function runStop() {
    const r = await api("/api/run/stop", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ force: true }) });
    if (r.data.ok) toast("Run stopped."); refreshRun();
  }
  async function refreshRun() {
    const r = await api("/api/run/status"); const st = r.data.status || r.data || {};
    state.run.running = !!st.running;
    $("#btnStart").disabled = state.run.running; $("#btnStop").disabled = !state.run.running;
    const tag = $("#runStatusTag"); tag.textContent = state.run.running ? "running" : (st.last_error ? "error" : "idle");
    tag.className = "tag " + (state.run.running ? "green" : "");
  }
  $("#btnStart").onclick = runStart; $("#btnStop").onclick = runStop;

  // ---------- loaders ----------
  async function loadCtfs() { const r = await api("/api/ctfs"); state.ctfs = r.data.ctfs || [];
    const opts = '<option value="">select a CTF…</option>' + state.ctfs.map(c => `<option value="${c.id}">${esc(c.name)}</option>`).join("");
    $("#runCtf").innerHTML = opts;
    const b = $("#boardCtf"); b.innerHTML = '<option value="">select a CTF…</option>' + state.ctfs.map(c => `<option value="${c.id}">${esc(c.name)}</option>`).join("");
    const params = new URLSearchParams(location.search); const pre = params.get("ctf_id"); if (pre) { $("#runCtf").value = pre; b.value = pre; }
    updateTopbar(); }
  async function loadTeam() { const [m, a] = await Promise.all([api("/api/team/members"), api("/api/accounts")]);
    state.members = m.data.members || m.data.users || (Array.isArray(m.data) ? m.data : []); state.accounts = a.data.accounts || [];
    if (state.screen === "team") renderTeam(); if (state.screen === "command") renderCommand(); updateTopbar(); }
  async function loadStatus() { const r = await api("/api/status"); if (r.data.challenges) { state.challenges = r.data.challenges; state.cost = { total: r.data.cost?.total_usd || 0, tokens: r.data.cost?.total_tokens || 0, byModel: r.data.cost?.by_model || {} }; state.ctfd = r.data.ctfd || state.ctfd; scheduleRender(); } }

  // ---------- autopilot / misc ----------
  function setupAutopilot() { const sw = $("#apSwitch"); try { state.autopilot = localStorage.getItem("fr_ap") !== "0"; } catch { }
    const apply = () => { sw.classList.toggle("on", state.autopilot); sw.setAttribute("aria-checked", String(state.autopilot)); }; apply();
    const t = () => { state.autopilot = !state.autopilot; try { localStorage.setItem("fr_ap", state.autopilot ? "1" : "0"); } catch { } apply();
      toast(state.autopilot ? "Autopilot on — agents run autonomously." : "Autopilot off — start challenges manually."); };
    sw.onclick = t; sw.addEventListener("keydown", e => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); t(); } }); }

  // ---------- init ----------
  function init() {
    const email = document.body.dataset.email || "user";
    $("#userAvatar").textContent = initials(email); $("#userAvatar").style.background = actorColor(email);
    if (document.body.dataset.admin) $("#menuAdmin").hidden = false;
    $("#userAvatar").onclick = () => $("#userMenu").classList.toggle("open");
    document.addEventListener("click", e => { if (!e.target.closest("#userMenu") && !e.target.closest("#userAvatar")) $("#userMenu").classList.remove("open"); });
    $$(".rail-btn").forEach(b => b.onclick = () => go(b.dataset.nav));
    setupAutopilot();
    if (!reduce) setInterval(() => { const n = new Date(); $("#clock").textContent = `${pad(n.getUTCHours())}:${pad(n.getUTCMinutes())} UTC`; }, 15000);
    { const n = new Date(); $("#clock").textContent = `${pad(n.getUTCHours())}:${pad(n.getUTCMinutes())} UTC`; }
    connectWS(); loadCtfs(); loadTeam(); loadStatus(); refreshRun();
    setInterval(() => { loadTeam(); refreshRun(); }, 8000);
    setInterval(() => { if (!state.ws) loadStatus(); }, 10000);
    go("command");
  }
  document.addEventListener("DOMContentLoaded", init);
})();
