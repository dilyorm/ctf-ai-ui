"use strict";

// ────────────────────────────────────────────────────────────────────────────
// /team — kanban board wired to /api/team/*
// ────────────────────────────────────────────────────────────────────────────

const STATUS_COLS = ["todo", "in_progress", "blocked", "needs_review", "solved", "skipped"];

const state = {
  ctfId: null,
  platform: "ctfd",
  members: [],
  tasks: [],           // array of task objects
  tasksById: new Map(),
  selectedTaskId: null,
  currentTask: null,
  filter: "",
  dirty: false,
};

// ────────────────────────────────────────────────────────────────────────────
// DOM helpers
// ────────────────────────────────────────────────────────────────────────────

const $ = (id) => document.getElementById(id);
const escHtml = (s) => String(s ?? "").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");

function mdToHtml(md) {
  // Deliberately naive — enough for CTFd-flavored descriptions. We escape first,
  // then convert a handful of markdown constructs. Users wanting rich rendering
  // can upload a .md attachment instead.
  if (!md) return "<p><em>No content.</em></p>";
  // If it already looks like HTML (old-sync CTFd records), pass through
  // without escaping so the user sees rendered tags instead of raw source.
  if (/<(p|div|h[1-6]|pre|code|ul|ol|li|br|strong|em|img|a|span|table)\b/i.test(md)) {
    return md;
  }
  let s = escHtml(md);
  s = s.replace(/```([\s\S]*?)```/g, (_, code) => `<pre><code>${code}</code></pre>`);
  s = s.replace(/`([^`\n]+)`/g, (_, c) => `<code>${c}</code>`);
  s = s.replace(/^###### (.+)$/gm, "<h6>$1</h6>");
  s = s.replace(/^##### (.+)$/gm, "<h5>$1</h5>");
  s = s.replace(/^#### (.+)$/gm, "<h4>$1</h4>");
  s = s.replace(/^### (.+)$/gm, "<h3>$1</h3>");
  s = s.replace(/^## (.+)$/gm, "<h2>$1</h2>");
  s = s.replace(/^# (.+)$/gm, "<h1>$1</h1>");
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/(^|\s)\*([^*]+)\*/g, "$1<em>$2</em>");
  s = s.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  s = s.split(/\n{2,}/).map(p => p.match(/^<(h\d|pre|ul|ol|blockquote)/) ? p : `<p>${p.replace(/\n/g,"<br>")}</p>`).join("\n");
  return s;
}

// ────────────────────────────────────────────────────────────────────────────
// Network
// ────────────────────────────────────────────────────────────────────────────

async function apiGet(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`GET ${path} → ${res.status}`);
  return res.json();
}
async function apiJSON(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : null,
  });
  const data = await res.json().catch(() => ({ ok: false, error: "bad response" }));
  if (!data.ok) throw new Error(data.error || `${method} ${path} failed`);
  return data;
}

// ────────────────────────────────────────────────────────────────────────────
// Initial load
// ────────────────────────────────────────────────────────────────────────────

async function init() {
  // Load team members for the assignee dropdown
  try {
    const data = await apiGet("/api/team/members");
    state.members = data.members || [];
    fillAssigneeOptions();
  } catch (_) { /* non-fatal */ }

  const select = $("team-ctf-select");
  select.addEventListener("change", () => {
    const id = select.value ? parseInt(select.value, 10) : null;
    state.ctfId = id;
    $("btn-sync").disabled = !id;
    $("btn-new-task").disabled = !id;
    if (id) loadTasks();
  });
  if (select.options.length && select.value) {
    state.ctfId = parseInt(select.value, 10);
    $("btn-sync").disabled = false;
    $("btn-new-task").disabled = false;
    loadTasks();
  }

  $("btn-sync").addEventListener("click", syncTasks);
  $("btn-new-task").addEventListener("click", openNewTaskModal);
  initNewTaskModal();
  $("team-filter").addEventListener("input", (e) => {
    state.filter = e.target.value.trim().toLowerCase();
    render();
  });

  initSortables();
  initDrawer();
}

function fillAssigneeOptions() {
  const sel = $("task-assignee");
  // Remove existing "user:" options (keep blank + AI).
  [...sel.querySelectorAll('option[data-dyn="1"]')].forEach(o => o.remove());
  for (const m of state.members) {
    const opt = document.createElement("option");
    opt.dataset.dyn = "1";
    opt.value = `user:${m.id}`;
    opt.textContent = `${m.display_name || m.email}`;
    sel.appendChild(opt);
  }
}

// ────────────────────────────────────────────────────────────────────────────
// Board rendering
// ────────────────────────────────────────────────────────────────────────────

async function loadTasks() {
  if (!state.ctfId) return;
  try {
    const data = await apiGet(`/api/team/tasks?ctf_id=${state.ctfId}`);
    state.platform = data.ctf?.platform || "ctfd";
    state.tasks = data.tasks || [];
    state.tasksById = new Map(state.tasks.map(t => [t.id, t]));
    render();
  } catch (e) {
    window.toast(`Load failed: ${e.message}`, "error");
  }
}

async function syncTasks() {
  if (!state.ctfId) return;
  const btn = $("btn-sync");
  const release = window.setBusy(btn, true, "Syncing…");
  try {
    const res = await apiJSON("POST", "/api/team/tasks/sync", { ctf_id: state.ctfId });
    window.toast(`Synced: ${res.created} new, ${res.updated} updated`, "success");
    await loadTasks();
  } catch (e) {
    window.toast(`Sync failed: ${e.message}`, "error");
  } finally {
    release();
  }
}

function render() {
  const q = state.filter;
  const counts = Object.fromEntries(STATUS_COLS.map(s => [s, 0]));
  for (const col of STATUS_COLS) {
    const body = document.querySelector(`.kanban-col-body[data-status="${col}"]`);
    body.innerHTML = "";
  }
  for (const t of state.tasks) {
    if (q && !(`${t.name} ${t.category}`.toLowerCase().includes(q))) continue;
    const status = STATUS_COLS.includes(t.status) ? t.status : "todo";
    counts[status]++;
    const body = document.querySelector(`.kanban-col-body[data-status="${status}"]`);
    body.appendChild(renderCard(t));
  }
  for (const col of STATUS_COLS) {
    document.querySelector(`.col-count[data-status="${col}"]`).textContent = counts[col];
  }
  const total = state.tasks.length;
  $("team-stats").textContent = total ? `${total} task${total === 1 ? "" : "s"}` : "";
}

function categoryKey(cat) {
  const c = (cat || "").toLowerCase();
  if (/web/.test(c)) return "web";
  if (/pwn|binary|exploit/.test(c)) return "pwn";
  if (/crypto/.test(c)) return "crypto";
  if (/rev|reverse/.test(c)) return "rev";
  if (/foren/.test(c)) return "forensics";
  if (/osint|recon/.test(c)) return "osint";
  if (/misc|general|warmup/.test(c)) return "misc";
  if (/stego/.test(c)) return "stego";
  if (/mobile|android|ios/.test(c)) return "mobile";
  if (/hardware|iot/.test(c)) return "hardware";
  if (/blockchain|smart.?contract/.test(c)) return "blockchain";
  return "other";
}

function descSnippet(md, n = 160) {
  if (!md) return "";
  // Strip markdown syntax for a plain preview
  const plain = String(md)
    .replace(/```[\s\S]*?```/g, "")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/[#>*_~\-]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  return plain.length > n ? plain.slice(0, n - 1) + "…" : plain;
}

function renderCard(t) {
  const div = document.createElement("div");
  div.className = `kanban-card cat-${categoryKey(t.category)}`;
  div.dataset.id = t.id;
  const assignee = t.assignee_type === "ai"
    ? `<span class="chip chip-ai">🤖 AI</span>`
    : (t.assignee_type === "user" && t.assignee_user_id)
      ? `<span class="chip">${escHtml(memberLabel(t.assignee_user_id))}</span>`
      : "";
  const flagDot = t.flag ? `<span class="dot dot-flag" title="Flag recorded"></span>` : "";
  const prio = (t.priority || 0) > 0 ? `<span class="chip chip-prio">★ ${t.priority}</span>` : "";
  const desc = t.description_override_md || t.platform_description_md || "";
  const preview = descSnippet(desc);
  const solvesStr = (t.solves ?? 0) === 1 ? "1 solve" : `${t.solves ?? 0} solves`;
  div.innerHTML = `
    <div class="card-top">
      <span class="card-name">${escHtml(t.name)}</span>
      ${flagDot}
    </div>
    ${preview ? `<div class="card-desc">${escHtml(preview)}</div>` : ""}
    <div class="card-chips">
      ${t.category ? `<span class="chip chip-cat">${escHtml(t.category)}</span>` : ""}
      ${t.points ? `<span class="chip chip-points">${t.points} pts</span>` : ""}
      <span class="chip chip-solves" title="Global solves on the platform">🏆 ${solvesStr}</span>
      ${prio}
      ${assignee}
    </div>
  `;
  div.addEventListener("click", () => openDrawer(t.id));
  return div;
}

function memberLabel(userId) {
  const m = state.members.find(x => x.id === userId);
  return m ? (m.display_name || m.email) : `user:${userId}`;
}

// ────────────────────────────────────────────────────────────────────────────
// Drag-and-drop
// ────────────────────────────────────────────────────────────────────────────

function initSortables() {
  document.querySelectorAll(".kanban-col-body").forEach(body => {
    new Sortable(body, {
      group: "team-kanban",
      animation: 150,
      ghostClass: "kanban-ghost",
      onEnd: async (evt) => {
        const id = parseInt(evt.item.dataset.id, 10);
        const newStatus = evt.to.dataset.status;
        const t = state.tasksById.get(id);
        if (!t || t.status === newStatus) return;
        const prev = t.status;
        t.status = newStatus;
        try {
          await apiJSON("PATCH", `/api/team/tasks/${id}`, { status: newStatus });
          render();  // refresh counts
        } catch (e) {
          t.status = prev;
          window.toast(`Status update failed: ${e.message}`, "error");
          render();
        }
      },
    });
  });
}

// ────────────────────────────────────────────────────────────────────────────
// Drawer
// ────────────────────────────────────────────────────────────────────────────

function initDrawer() {
  $("task-drawer-close").addEventListener("click", closeDrawer);
  $("task-drawer-backdrop").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && state.selectedTaskId != null) closeDrawer();
  });
  document.querySelectorAll(".task-tab").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".task-tab").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      ["description","writeup","attachments","notes"].forEach(name => {
        $(`tab-${name}`).style.display = name === btn.dataset.tab ? "" : "none";
      });
    });
  });
  $("btn-save-task").addEventListener("click", saveTask);
  $("btn-delete-task").addEventListener("click", deleteCurrentTask);

  $("btn-generate-writeup").addEventListener("click", generateWriteup);
  $("btn-writeup-preview-toggle").addEventListener("click", toggleWriteupPreview);
  $("writeup-upload").addEventListener("change", handleWriteupUpload);
  $("attachments-upload").addEventListener("change", handleAttachmentUpload);

  // Watch for changes so Save is the only commit point
  ["task-status","task-assignee","task-priority","task-override","task-flag","task-writeup","task-notes"]
    .forEach(id => $(id).addEventListener("input", () => markDirty()));
}

function markDirty() {
  state.dirty = true;
  $("task-save-status").textContent = "unsaved changes";
  $("task-save-status").classList.add("is-dirty");
}

async function openDrawer(taskId) {
  const data = await apiGet(`/api/team/tasks/${taskId}`).catch(e => {
    window.toast(`Load failed: ${e.message}`, "error"); return null;
  });
  if (!data) return;
  const t = data.task;
  state.selectedTaskId = taskId;
  state.currentTask = t;
  state.dirty = false;

  $("task-name").textContent = t.name;
  const catEl = $("task-category");
  catEl.textContent = t.category || "(no category)";
  catEl.className = `task-pill cat-pill cat-${categoryKey(t.category)}`;
  $("task-points").textContent = `${t.points || 0} pts`;
  const solvesEl = $("task-solves");
  if (solvesEl) solvesEl.textContent = `🏆 ${t.solves ?? 0} solve${(t.solves ?? 0) === 1 ? "" : "s"}`;
  $("task-solver-status").textContent = t.last_solver_status ? `solver: ${t.last_solver_status}` : "";
  $("task-status").value = t.status;
  $("task-priority").value = t.priority || 0;
  $("task-flag").value = t.flag || "";
  $("task-override").value = t.description_override_md || "";
  $("task-writeup").value = t.writeup_md || "";
  $("task-notes").value = t.notes_md || "";

  // Assignee
  if (t.assignee_type === "ai") {
    $("task-assignee").value = "ai";
  } else if (t.assignee_type === "user" && t.assignee_user_id) {
    $("task-assignee").value = `user:${t.assignee_user_id}`;
  } else {
    $("task-assignee").value = "";
  }

  // Description
  $("task-platform-desc").innerHTML = mdToHtml(t.platform_description_md || "");
  const files = t.files || [];
  const fu = $("task-files-upstream");
  fu.innerHTML = files.length
    ? `<h5>Upstream files</h5><ul>${files.map(f => `<li><a target="_blank" rel="noopener" href="${escHtml(f)}">${escHtml(f)}</a></li>`).join("")}</ul>`
    : "";

  // Attachments
  renderAttachments(t.attachments || []);

  $("task-save-status").textContent = "";
  $("task-save-status").classList.remove("is-dirty");

  // Only manually-created tasks can be deleted (synced tasks would just
  // reappear on next sync).
  const isManual = typeof t.external_id === "string" && t.external_id.startsWith("manual-");
  $("btn-delete-task").style.display = isManual ? "" : "none";

  $("task-drawer").classList.add("open");
  $("task-drawer-backdrop").classList.add("open");
}

function closeDrawer() {
  if (state.dirty) {
    if (!confirm("You have unsaved changes. Close anyway?")) return;
  }
  state.selectedTaskId = null;
  state.currentTask = null;
  state.dirty = false;
  $("task-drawer").classList.remove("open");
  $("task-drawer-backdrop").classList.remove("open");
}

function renderAttachments(atts) {
  const list = $("task-attachments");
  list.innerHTML = "";
  if (!atts.length) {
    list.innerHTML = `<li class="empty">No files attached.</li>`;
    return;
  }
  for (const a of atts) {
    const li = document.createElement("li");
    li.innerHTML = `
      <span class="att-icon">${a.kind === "writeup" ? "📝" : (a.kind === "image" ? "🖼" : "📎")}</span>
      <a href="/api/team/tasks/${state.selectedTaskId}/attachments/${a.id}" target="_blank">${escHtml(a.filename)}</a>
      <span class="att-meta">${Math.ceil(a.size_bytes/1024)} KB</span>
      <button class="btn btn-ghost btn-sm att-delete" data-id="${a.id}">Delete</button>
    `;
    list.appendChild(li);
  }
  list.querySelectorAll(".att-delete").forEach(b => {
    b.addEventListener("click", async () => {
      const aid = b.dataset.id;
      if (!confirm("Delete this attachment?")) return;
      try {
        await apiJSON("DELETE", `/api/team/tasks/${state.selectedTaskId}/attachments/${aid}`);
        window.toast("Deleted", "success");
        // refresh task
        const data = await apiGet(`/api/team/tasks/${state.selectedTaskId}`);
        renderAttachments(data.task.attachments || []);
      } catch (e) { window.toast(e.message, "error"); }
    });
  });
}

async function saveTask() {
  if (state.selectedTaskId == null) return;
  const assignVal = $("task-assignee").value;
  let assignee_type = null, assignee_user_id = null;
  if (assignVal === "ai") {
    assignee_type = "ai";
  } else if (assignVal.startsWith("user:")) {
    assignee_type = "user";
    assignee_user_id = parseInt(assignVal.slice(5), 10);
  }
  const body = {
    status: $("task-status").value,
    assignee_type,
    assignee_user_id,
    priority: parseInt($("task-priority").value || "0", 10),
    flag: $("task-flag").value,
    description_override_md: $("task-override").value,
    writeup_md: $("task-writeup").value,
    notes_md: $("task-notes").value,
  };
  const btn = $("btn-save-task");
  const release = window.setBusy(btn, true, "Saving…");
  try {
    const data = await apiJSON("PATCH", `/api/team/tasks/${state.selectedTaskId}`, body);
    state.currentTask = data.task;
    // update local store
    const idx = state.tasks.findIndex(t => t.id === data.task.id);
    if (idx >= 0) state.tasks[idx] = { ...state.tasks[idx], ...data.task };
    state.tasksById.set(data.task.id, state.tasks[idx]);
    render();
    state.dirty = false;
    $("task-save-status").textContent = "saved";
    $("task-save-status").classList.remove("is-dirty");
    window.toast("Task saved", "success");
  } catch (e) {
    window.toast(`Save failed: ${e.message}`, "error");
  } finally {
    release();
  }
}

async function generateWriteup() {
  if (state.selectedTaskId == null) return;
  const btn = $("btn-generate-writeup");
  const release = window.setBusy(btn, true, "Generating…");
  try {
    const res = await apiJSON("POST", `/api/team/tasks/${state.selectedTaskId}/generate-writeup`);
    $("task-writeup").value = res.writeup_md || "";
    markDirty();
    window.toast("Writeup drafted — review & save.", "success");
  } catch (e) {
    window.toast(`Generate failed: ${e.message}`, "error");
  } finally {
    release();
  }
}

function toggleWriteupPreview() {
  const ta = $("task-writeup");
  const preview = $("task-writeup-preview");
  if (preview.style.display === "none") {
    preview.innerHTML = mdToHtml(ta.value);
    preview.style.display = "";
    ta.style.display = "none";
    $("btn-writeup-preview-toggle").textContent = "Edit";
  } else {
    preview.style.display = "none";
    ta.style.display = "";
    $("btn-writeup-preview-toggle").textContent = "Preview";
  }
}

async function handleWriteupUpload(e) {
  const file = e.target.files?.[0];
  if (!file) return;
  const text = await file.text();
  $("task-writeup").value = text;
  markDirty();
  // Also store the original file as an attachment of kind=writeup
  await uploadAttachment(file, "writeup");
  e.target.value = "";
}

async function handleAttachmentUpload(e) {
  const file = e.target.files?.[0];
  if (!file) return;
  await uploadAttachment(file, "file");
  e.target.value = "";
}

async function uploadAttachment(file, kind) {
  if (state.selectedTaskId == null) return;
  const fd = new FormData();
  fd.append("file", file);
  window.toast(`Uploading ${file.name}…`, "info");
  try {
    const res = await fetch(
      `/api/team/tasks/${state.selectedTaskId}/attachments?kind=${encodeURIComponent(kind)}`,
      { method: "POST", body: fd }
    );
    // A too-large upload can be rejected by the proxy (nginx) with an HTML page,
    // not JSON — reading .json() on that throws "unexpected character". Read text
    // first and surface a real message.
    const raw = await res.text();
    let data;
    try { data = JSON.parse(raw); } catch { data = null; }
    if (!res.ok || !data || !data.ok) {
      const msg = (data && data.error)
        || (res.status === 413 ? "file too large for the server to accept" : `upload failed (HTTP ${res.status})`);
      throw new Error(msg);
    }
    window.toast("Uploaded", "success");
    const refreshed = await apiGet(`/api/team/tasks/${state.selectedTaskId}`);
    renderAttachments(refreshed.task.attachments || []);
  } catch (e) {
    window.toast(`Upload failed: ${e.message}`, "error");
  }
}

// ────────────────────────────────────────────────────────────────────────────
// New-task modal
// ────────────────────────────────────────────────────────────────────────────

function initNewTaskModal() {
  const overlay = $("new-task-overlay");
  $("new-task-close").addEventListener("click", closeNewTaskModal);
  $("new-task-cancel").addEventListener("click", closeNewTaskModal);
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) closeNewTaskModal();
  });
  $("new-task-create").addEventListener("click", submitNewTask);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && overlay.style.display !== "none") closeNewTaskModal();
  });
}

function openNewTaskModal() {
  if (!state.ctfId) {
    window.toast("Pick a CTF first", "error");
    return;
  }
  $("new-task-name").value = "";
  $("new-task-category").value = "";
  $("new-task-points").value = "0";
  $("new-task-desc").value = "";
  $("new-task-conn").value = "";
  $("new-task-overlay").style.display = "";
  setTimeout(() => $("new-task-name").focus(), 10);
}

function closeNewTaskModal() {
  $("new-task-overlay").style.display = "none";
}

async function submitNewTask() {
  const name = $("new-task-name").value.trim();
  if (!name) {
    window.toast("Name is required", "error");
    $("new-task-name").focus();
    return;
  }
  const body = {
    ctf_id: state.ctfId,
    name,
    category: $("new-task-category").value.trim(),
    points: parseInt($("new-task-points").value || "0", 10) || 0,
    platform_description_md: $("new-task-desc").value,
    connection_info: $("new-task-conn").value.trim(),
  };
  const btn = $("new-task-create");
  const release = window.setBusy(btn, true, "Creating…");
  try {
    const data = await apiJSON("POST", "/api/team/tasks", body);
    state.tasks.unshift(data.task);
    state.tasksById.set(data.task.id, data.task);
    render();
    closeNewTaskModal();
    window.toast("Task created", "success");
    openDrawer(data.task.id);
  } catch (e) {
    window.toast(`Create failed: ${e.message}`, "error");
  } finally {
    release();
  }
}

async function deleteCurrentTask() {
  if (state.selectedTaskId == null) return;
  const t = state.currentTask;
  if (!t) return;
  if (!confirm(`Delete task "${t.name}"? This cannot be undone.`)) return;
  try {
    await apiJSON("DELETE", `/api/team/tasks/${state.selectedTaskId}`);
    state.tasks = state.tasks.filter(x => x.id !== state.selectedTaskId);
    state.tasksById.delete(state.selectedTaskId);
    state.dirty = false;
    closeDrawer();
    render();
    window.toast("Task deleted", "success");
  } catch (e) {
    window.toast(`Delete failed: ${e.message}`, "error");
  }
}

// Kick things off
document.addEventListener("DOMContentLoaded", init);
