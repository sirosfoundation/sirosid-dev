// The left pane: the assistant. The log, example prompts, the message queue, pending approvals and
// the composer. Everything the assistant or a tool returns is shown as plain text (textContent).
import { api, ApiError, stream } from "./api.js";
import * as L from "./chatlogic.js";
import { briefArgs, describeArgs, envLabel } from "./format.js";
import { state, on, emit, fail, guard, byId, select, refresh, loadInstances, setMobile } from "./store.js";
import { h, fill, spinner, modal } from "./ui.js";

const el = {};                       // the pane's long-lived elements
const rendered = new Map();          // item key -> {v, node}

const chat = () => state.chat;
const enabled = () => !!state.chatStatus?.enabled;
const canSend = () => !!state.me?.unlocked;

export function mountChat(root) {
  el.root = root;
  rendered.clear();
  el.model = h("select", { id: "chat-model", class: "compact", "aria-label": "Model" });
  el.model.addEventListener("change", () => { el.modelChoice = el.model.value; });
  el.newConv = h("button", { class: "small", on: { click: guard(newConversation) } }, "New chat");
  el.log = h("div", { class: "chat-log", role: "log", "aria-live": "polite", "aria-relevant": "additions", "aria-label": "Conversation", tabindex: "0" });
  el.requests = h("div", { class: "requests", hidden: true });
  el.queue = h("div", { class: "queue", hidden: true, "aria-live": "polite" });
  el.input = h("textarea", { id: "chat-input", class: "chat-input", rows: "2", maxlength: "8000", placeholder: "Describe what you need…", "aria-label": "Message to the assistant" });
  el.send = h("button", { class: "primary send", "aria-label": "Send message" }, "Send");
  el.status = h("div", { class: "chat-status muted", "aria-live": "polite" });
  el.usage = h("div", { class: "usage muted" });
  el.notice = h("div", { class: "chat-off", hidden: true });
  el.composer = h("div", { class: "composer" },
    el.requests, el.queue,
    h("div", { class: "composer-box" }, el.input, el.send),
    h("div", { class: "composer-foot" }, el.status, el.usage),
    h("p", { class: "privacy muted" }, "Your messages, configs and environments' status go to OpenRouter and the model's provider. The assistant never sees credentials."));
  fill(root,
    h("div", { class: "pane-head" }, h("h2", { class: "pane-title" }, "Assistant"), h("span", { class: "grow" }), el.model, el.newConv),
    el.notice, el.log, el.composer);
  el.send.addEventListener("click", () => submitInput());
  el.input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); submitInput(); }   // Enter, Ctrl+Enter and Cmd+Enter send; Shift+Enter is a newline
  });
  el.unsubs?.forEach((u) => u());
  el.unsubs = [on("chat", paint), on("examples", paint)];
  paint();
}

/** Put text in the composer and focus it (the "Ask the agent…" buttons). */
export function prefill(text) {
  setMobile("chat");
  el.input.value = text;
  el.input.focus();
  el.input.setSelectionRange(text.length, text.length);
}

export function focusComposer() { setMobile("chat"); el.input?.focus(); }

function submitInput() {
  const text = el.input.value.trim();
  if (!text) return;
  if (submit(text)) el.input.value = "";
}

/** Send (or queue) a message. False if the assistant cannot take it at all. */
export function submit(text) {
  if (!enabled() || !canSend()) return false;
  L.resume(chat());                  // sending again is how a paused queue is resumed
  L.enqueue(chat(), text);
  drain();
  paint();
  return true;
}

function drain() {
  if (!state.me?.unlocked) return;
  const c = chat();
  const m = L.takeNext(c);
  if (!m) return;
  L.beginTurn(c, m.text);
  const body = { message: m.text, conversation_id: c.id || undefined, active_instance: state.selected || undefined };
  if (!c.id) body.model = el.modelChoice || state.chatStatus?.default_model || undefined;
  run("/api/chat", body);
}

async function run(path, body) {
  const c = chat();
  paint();
  let failed = false;
  try {
    await stream(path, body, (ev) => { const fx = L.reduce(c, ev); paint(); effects(fx); });
  } catch (e) {
    failed = true;
    if (e instanceof ApiError && (e.status === 401 || e.status === 423)) { L.endTurn(c, { failed }); paint(); return fail(e); }   // back to sign-in / the lock page
    L.pushError(c, e.message || String(e));
  }
  L.endTurn(c, { failed });
  paint();
  drain();
}

async function effects(fx) {
  try {
    if (fx.usage && state.chatStatus) { state.chatStatus.usage = fx.usage; paint(); }
    if (fx.refresh) await refresh(fx.refresh);
    if (fx.focus) {
      if (!byId(fx.focus)) await loadInstances();
      if (byId(fx.focus)) select(fx.focus);
    }
  } catch (e) { fail(e); }
}

async function newConversation() {
  const c = chat();
  if (c.busy) return;
  if (c.id) await api("DELETE", `/api/chat/${encodeURIComponent(c.id)}`).catch(() => {});
  state.chat = L.createChat();
  rendered.clear();
  el.log.replaceChildren();
  paint();
  el.input.focus();
}

function answer(item, approve) {
  const body = L.answer(chat(), item, approve);
  if (body) run("/api/chat/confirm", body);
}

// ---- rendering ------------------------------------------------------------------------------------

const nameOf = (id) => { const i = byId(id); return i ? envLabel(i) : null; };

function itemNode(it) {
  if (it.kind === "user") return h("div", { class: "msg user" }, h("span", { class: "sr-only" }, "You: "), it.text);
  if (it.kind === "assistant") return h("div", { class: "msg assistant" }, h("span", { class: "sr-only" }, "Assistant: "), it.text);
  if (it.kind === "error") return h("div", { class: "msg error", role: "alert" }, it.text);
  if (it.kind === "tool") {
    const mark = it.ok === false ? "✗" : it.ok ? "✓" : "…";
    return h("details", { class: "msg tool" + (it.ok === false ? " failed" : "") },
      h("summary", {}, h("span", { class: "tool-mark", "aria-hidden": "true" }, mark), " ", it.name,
        h("span", { class: "sr-only" }, it.ok === false ? " failed" : it.ok ? " done" : " running")),
      briefArgs(it.args) && h("div", { class: "tool-args" }, briefArgs(it.args)),
      it.result && h("div", { class: "tool-result" }, it.result));
  }
  // an approval card
  const lines = describeArgs(it.args, nameOf);
  return h("div", { class: "approval" + (it.answered ? " answered" : ""), "data-call": it.call_id },
    h("p", { class: "approval-title" }, `Approve: ${it.title}?`),
    lines.length > 0 && h("dl", { class: "args" }, lines.map(([k, v]) => [h("dt", {}, k), h("dd", {}, v)])),
    it.answered
      ? h("p", { class: "muted approval-outcome" }, it.answered === "approved" ? "Approved" : "Declined", it.result && it.answered === "approved" ? ` — ${it.result}` : "")
      : h("div", { class: "row end" },
          h("button", { disabled: chat().busy, on: { click: () => answer(it, false) } }, "Decline"),
          h("button", { class: "danger solid", disabled: chat().busy, on: { click: () => answer(it, true) } }, "Approve")));
}

function welcome() {
  const groups = L.groupExamples(state.examples || L.FALLBACK_EXAMPLES);
  return h("div", { class: "welcome" },
    h("h3", {}, "What would you like to test?"),
    h("p", { class: "muted" }, "Describe an environment in your own words, or start from one of these. The assistant can create, change and inspect your environments; destroying or resetting one always asks you first."),
    examplesList(groups, (p) => submit(p)));
}

export function examplesList(groups, pick) {
  return h("div", { class: "examples" }, groups.map((g) => h("div", { class: "example-group" },
    h("h4", {}, g.category),
    h("div", { class: "chips" }, g.items.map((x) => h("button", { class: "chip", title: x.prompt, on: { click: () => pick(x.prompt) } },
      h("span", { class: "chip-title" }, x.title), x.title !== x.prompt && h("span", { class: "chip-prompt" }, x.prompt)))))));
}

/** "Try an example": the examples in a modal (clicking one sends it). */
export function showExamples() {
  const groups = L.groupExamples(state.examples || L.FALLBACK_EXAMPLES);
  const m = modal({ title: "Try an example", wide: true, body: [h("p", { class: "muted" }, "Pick one: it is sent to the assistant as your message."),
    examplesList(groups, (p) => { m.close(); setMobile("chat"); submit(p); })] });
}

function paintLog() {
  const c = chat();
  if (L.isEmpty(c)) {
    rendered.clear();
    fill(el.log, welcome());
    return;
  }
  const w = el.log.querySelector(".welcome");
  if (w) w.remove();
  const nearBottom = el.log.scrollHeight - el.log.scrollTop - el.log.clientHeight < 80;
  c.items.forEach((it, i) => {
    const have = rendered.get(it.key);
    if (have && have.v === it.v) return;
    const node = itemNode(it);
    if (have) have.node.replaceWith(node);
    else if (el.log.children[i]) el.log.insertBefore(node, el.log.children[i]);
    else el.log.append(node);
    rendered.set(it.key, { v: it.v, node });
  });
  // approval buttons follow the busy flag
  for (const b of el.log.querySelectorAll(".approval:not(.answered) button")) b.disabled = c.busy;
  if (nearBottom || c.busy) el.log.scrollTop = el.log.scrollHeight;
}

function paintRequests() {
  const pend = L.pendingApprovals(chat());
  el.requests.hidden = pend.length === 0;
  if (!pend.length) return el.requests.replaceChildren();
  fill(el.requests, h("span", { class: "requests-title" }, `Requests (${pend.length})`),
    pend.map((it) => h("button", { class: "link", on: { click: () => {
      const card = [...el.log.querySelectorAll(".approval")].find((n) => n.dataset.call === it.call_id);
      card?.scrollIntoView({ block: "center", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
      card?.querySelector("button.danger")?.focus({ preventScroll: true });
    } } }, `Approve: ${it.title}?`)));
}

function paintQueue() {
  const c = chat();
  el.queue.hidden = c.queue.length === 0;
  if (!c.queue.length) return el.queue.replaceChildren();
  const note = L.queueNote(c);
  fill(el.queue,
    h("div", { class: "queue-head" }, h("span", { class: "queue-title" }, `Queued (${c.queue.length})`), note && h("span", { class: "muted" }, ` · ${note}`),
      c.paused && h("button", { class: "small", on: { click: () => { L.resume(c); drain(); paint(); } } }, "Resume")),
    h("ol", { class: "queue-list" }, c.queue.map((q, i) => h("li", {},
      h("span", { class: "queue-text" }, q.text),
      h("button", { class: "icon-btn", disabled: i === 0, "aria-label": `Move up: ${q.text.slice(0, 40)}`, title: "Move up", on: { click: () => { L.moveUp(c, q.id); paint(); } } }, "↑"),
      h("button", { class: "icon-btn", "aria-label": `Remove from queue: ${q.text.slice(0, 40)}`, title: "Remove", on: { click: () => { L.removeQueued(c, q.id); paint(); } } }, "✕")))));
}

export function paint() {
  if (!el.root) return;
  const st = state.chatStatus;
  const on_ = enabled();
  el.notice.hidden = on_ || !st;
  if (st && !on_) fill(el.notice, h("div", { class: "card notice" }, h("h3", {}, "The assistant is not enabled on this server"),
    h("p", { class: "muted" }, "Everything still works by hand: pick a template in the library to create an environment, and manage it in the middle pane.")));
  el.log.hidden = !on_; el.composer.hidden = !on_;
  el.model.hidden = el.newConv.hidden = !on_;
  if (!on_) return;
  const c = chat();
  const models = st.models || [];
  if (el.model.options.length !== models.length) fill(el.model, models.map((m) => h("option", { value: m }, m)));
  if (el.modelChoice) el.model.value = el.modelChoice;
  el.model.hidden = models.length < 2;
  el.model.disabled = !!c.id;
  el.newConv.disabled = c.busy || L.isEmpty(c);
  paintLog(); paintRequests(); paintQueue();
  el.send.disabled = !canSend();
  el.input.placeholder = L.blocked(c) ? "Type your next message: it waits in the queue" : "Describe what you need…";
  fill(el.status, c.busy && [spinner(), " ", c.statusText || "Working…"]);
  const u = st.usage || {};
  el.usage.textContent = u.limit ? `${Number(u.used || 0).toLocaleString()} of ${Number(u.limit).toLocaleString()} tokens today` : "";
  emit("chat-painted");
}
