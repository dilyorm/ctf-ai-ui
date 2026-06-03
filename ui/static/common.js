/* ─────────────────────────────────────────────────────────────────
   CTF Agent — Shared UI utilities (loaded on every page)
   ───────────────────────────────────────────────────────────────── */

"use strict";

(function () {
  function escHtml(str) {
    if (str === null || str === undefined) return "";
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function ensureToastContainer() {
    let c = document.getElementById("toast-container");
    if (!c) {
      c = document.createElement("div");
      c.id = "toast-container";
      c.className = "toast-container";
      document.body.appendChild(c);
    }
    return c;
  }

  function toast(msg, kind, opts) {
    kind = kind || "info";
    opts = opts || {};
    const duration = opts.duration === undefined ? 3500 : opts.duration;
    const allowHtml = !!opts.allowHtml;
    const c = ensureToastContainer();
    const el = document.createElement("div");
    el.className = "toast " + kind;
    const icons = { success: "✓", error: "⚠", warn: "!", info: "ℹ" };
    const icon = '<span class="toast-icon">' + (icons[kind] || icons.info) + "</span>";
    const body = '<div class="toast-msg">' + (allowHtml ? msg : escHtml(msg)) + "</div>";
    el.innerHTML = icon + body + '<button class="toast-close" aria-label="Dismiss">✕</button>';
    c.appendChild(el);
    requestAnimationFrame(() => el.classList.add("show"));
    const remove = () => {
      el.classList.remove("show");
      setTimeout(() => el.remove(), 220);
    };
    el.querySelector(".toast-close").addEventListener("click", remove);
    if (duration > 0) setTimeout(remove, duration);
    return remove;
  }

  function confirmDialog(opts) {
    opts = opts || {};
    const title = opts.title || "Are you sure?";
    const body = opts.body || "";
    const confirmText = opts.confirmText || "Confirm";
    const cancelText = opts.cancelText || "Cancel";
    const danger = opts.danger !== false;
    const icon = opts.icon || "?";

    return new Promise(resolve => {
      const overlay = document.createElement("div");
      overlay.className = "modal-overlay";
      overlay.innerHTML =
        '<div class="confirm-modal" role="dialog" aria-modal="true">' +
          '<div class="confirm-header">' +
            '<div class="confirm-icon ' + (danger ? "" : "info") + '">' + icon + '</div>' +
            '<div class="confirm-title">' + escHtml(title) + '</div>' +
          '</div>' +
          '<div class="confirm-body">' + body + '</div>' +
          '<div class="confirm-footer">' +
            '<button class="btn btn-secondary" data-act="cancel">' + escHtml(cancelText) + '</button>' +
            '<button class="btn ' + (danger ? "btn-danger" : "btn-primary") + '" data-act="ok">' + escHtml(confirmText) + '</button>' +
          '</div>' +
        '</div>';
      document.body.appendChild(overlay);
      const cleanup = (val) => {
        document.removeEventListener("keydown", onKey);
        overlay.remove();
        resolve(val);
      };
      const onKey = e => {
        if (e.key === "Escape") cleanup(false);
        if (e.key === "Enter")  cleanup(true);
      };
      document.addEventListener("keydown", onKey);
      overlay.addEventListener("click", e => { if (e.target === overlay) cleanup(false); });
      overlay.querySelector('[data-act="cancel"]').addEventListener("click", () => cleanup(false));
      overlay.querySelector('[data-act="ok"]').addEventListener("click", () => cleanup(true));
      overlay.querySelector('[data-act="ok"]').focus();
    });
  }

  function setBusy(btn, busy, busyLabel) {
    if (!btn) return () => {};
    if (busy) {
      btn.dataset.busy = "1";
      if (!btn.dataset.origHtml) btn.dataset.origHtml = btn.innerHTML;
      btn.innerHTML = '<span class="spinner"></span> ' + escHtml(busyLabel || "Working…");
      btn.disabled = true;
    } else {
      delete btn.dataset.busy;
      if (btn.dataset.origHtml) {
        btn.innerHTML = btn.dataset.origHtml;
        delete btn.dataset.origHtml;
      }
      btn.disabled = false;
    }
    return () => setBusy(btn, false);
  }

  function setupNavToggle() {
    const t = document.getElementById("nav-toggle");
    const links = document.querySelector(".topnav-links");
    if (!t || !links) return;
    t.addEventListener("click", e => {
      e.stopPropagation();
      const open = links.classList.toggle("open");
      t.setAttribute("aria-expanded", open ? "true" : "false");
    });
    document.addEventListener("click", e => {
      if (!links.contains(e.target) && !t.contains(e.target)) {
        links.classList.remove("open");
        t.setAttribute("aria-expanded", "false");
      }
    });
  }

  // Esc closes any open drawer/menu
  document.addEventListener("keydown", e => {
    if (e.key === "Escape") {
      document.querySelectorAll(".sidebar.open, .right-panel.open, .topnav-links.open")
        .forEach(p => p.classList.remove("open"));
      const t = document.getElementById("nav-toggle");
      if (t) t.setAttribute("aria-expanded", "false");
      const ov = document.getElementById("drawer-overlay");
      if (ov) ov.classList.remove("open");
    }
  });

  document.addEventListener("DOMContentLoaded", setupNavToggle);

  window.escHtml = escHtml;
  window.toast = toast;
  window.confirmDialog = confirmDialog;
  window.setBusy = setBusy;
})();
