// DOM helpers. Everything is built with createElement/textContent: never an HTML string, so nothing
// the server returns (names, URLs, error text, the assistant's words) is ever parsed as markup.

export const $ = (id) => document.getElementById(id);

export function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v == null) continue;
    if (k === "on") for (const [ev, fn] of Object.entries(v)) el.addEventListener(ev, fn);
    else if (k === "class") el.className = v;
    else if (k === "value") el.value = v;
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, v);
  }
  for (const kid of kids.flat(Infinity)) if (kid != null && kid !== false) el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  return el;
}

/** Replace an element's children; null/false children are skipped, never rendered as text. */
export const fill = (el, ...kids) => { el.replaceChildren(...kids.flat(Infinity).filter((k) => k != null && k !== false)); return el; };

let toastTimer;
export function toast(msg, bad = false) {
  const t = $("toast");
  t.textContent = msg;
  t.className = "show" + (bad ? " bad" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.className = ""; }, 5000);
}

export const spinner = () => h("span", { class: "spinner", "aria-hidden": "true" });

/** A status pill: {label, tone, spinner} from format.statusPill. */
export const pill = (p, extra = "") => h("span", { class: `pill tone-${p.tone} ${extra}`.trim() }, p.spinner && spinner(), p.label);

const FOCUSABLE = "a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])";
const focusables = (root) => [...root.querySelectorAll(FOCUSABLE)].filter((e) => !e.closest("[hidden]") && e.getClientRects().length > 0);

let openModal = null;
/** A modal dialog: focus is trapped inside, Esc and the backdrop close it, the rest of the page is inert,
 *  and focus returns to whatever opened it. Returns {close, root, body}. */
export function modal({ title, body = [], actions = [], onClose, wide = false }) {
  if (openModal) openModal.close();
  const opener = document.activeElement;
  const titleId = "modal-title-" + Math.random().toString(36).slice(2, 8);
  const closeBtn = h("button", { class: "icon-btn modal-x", "aria-label": "Close" }, "✕");
  const box = h("div", { class: "modal" + (wide ? " wide" : ""), role: "dialog", "aria-modal": "true", "aria-labelledby": titleId },
    h("div", { class: "modal-head" }, h("h2", { id: titleId }, title), closeBtn),
    h("div", { class: "modal-body" }, body),
    actions.length > 0 && h("div", { class: "modal-actions" }, actions));
  const back = h("div", { class: "modal-backdrop" }, box);
  const outside = [document.querySelector(".site-header"), $("main"), $("tabbar"), document.querySelector(".site-footer")].filter(Boolean);
  let closed = false;
  const close = (result) => {
    if (closed) return; closed = true;
    document.removeEventListener("keydown", onKey, true);
    back.remove();
    outside.forEach((e) => { e.inert = false; });
    if (openModal === api) openModal = null;
    if (opener && opener.isConnected && typeof opener.focus === "function") opener.focus();
    onClose?.(result);
  };
  const onKey = (e) => {
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(undefined); return; }
    if (e.key !== "Tab") return;
    const f = focusables(box);
    if (!f.length) return;
    const first = f[0], last = f[f.length - 1];
    if (e.shiftKey && (document.activeElement === first || !box.contains(document.activeElement))) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && (document.activeElement === last || !box.contains(document.activeElement))) { e.preventDefault(); first.focus(); }
  };
  closeBtn.addEventListener("click", () => close(undefined));
  back.addEventListener("mousedown", (e) => { if (e.target === back) close(undefined); });
  document.addEventListener("keydown", onKey, true);
  document.body.append(back);
  outside.forEach((e) => { e.inert = true; });
  const api = { close, root: box, body: box.querySelector(".modal-body") };
  openModal = api;
  const auto = box.querySelector("[data-autofocus]") || focusables(box.querySelector(".modal-body"))[0] || focusables(box).find((e) => e !== closeBtn) || closeBtn;
  setTimeout(() => auto.focus(), 0);
  return api;
}

/** An in-page confirmation (never window.confirm). Resolves true only on the confirm button. */
export function confirmModal({ title, text, confirmLabel = "Confirm", danger = false, details = [] }) {
  return new Promise((resolve) => {
    const yes = h("button", { class: danger ? "danger solid" : "primary" }, confirmLabel);
    const no = h("button", { "data-autofocus": true }, "Cancel");
    const m = modal({ title, body: [h("p", {}, text), ...details], actions: [no, yes], onClose: (r) => resolve(r === true) });
    yes.addEventListener("click", () => m.close(true));
    no.addEventListener("click", () => m.close(false));
  });
}

let openPop = null;
/** A popover menu anchored to a button: Esc and outside clicks close it, arrow keys move between items. */
export function popover(anchor, menu) {
  if (openPop) { const same = openPop.anchor === anchor; openPop.close(); if (same) return null; }
  const items = () => [...menu.querySelectorAll("[role=menuitem]")];
  const close = (refocus = true) => {
    menu.hidden = true; anchor.setAttribute("aria-expanded", "false");
    document.removeEventListener("mousedown", onDown, true); document.removeEventListener("keydown", onKey, true);
    if (openPop?.anchor === anchor) openPop = null;
    if (refocus) anchor.focus();
  };
  const onDown = (e) => { if (!menu.contains(e.target) && !anchor.contains(e.target)) close(false); };
  const onKey = (e) => {
    const list = items(), i = list.indexOf(document.activeElement);
    if (e.key === "Escape") { e.preventDefault(); close(); }
    else if (e.key === "ArrowDown") { e.preventDefault(); list[(i + 1) % list.length]?.focus(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); list[(i - 1 + list.length) % list.length]?.focus(); }
    else if (e.key === "Tab") close(false);
  };
  menu.hidden = false; anchor.setAttribute("aria-expanded", "true");
  document.addEventListener("mousedown", onDown, true); document.addEventListener("keydown", onKey, true);
  items()[0]?.focus();
  openPop = { anchor, close };
  return openPop;
}
export const closePopover = () => openPop?.close(false);

/** A Copy button: copies `text()` (or a string) to the clipboard and says so. */
export function copyButton(text, label = "Copy", what = "") {
  const b = h("button", { class: "small", "aria-label": what ? `Copy ${what}` : label }, label);
  b.addEventListener("click", async () => {
    const value = typeof text === "function" ? text() : text;
    try { await navigator.clipboard.writeText(value); b.textContent = "Copied"; }
    catch { b.textContent = "Copy failed"; }
    setTimeout(() => { b.textContent = label; }, 1500);
  });
  return b;
}

/** A labelled field: label + control (+ an optional hint). */
export const field = (id, label, control, hint) => h("div", { class: "field" }, h("label", { for: id }, label), control, hint && h("p", { class: "hint muted" }, hint));
