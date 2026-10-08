// The chat's pure state: the log, the message queue and the approval gate, and the reduction of the
// server's SSE events into that state. No DOM and no fetch (console/test/chatlogic.test.mjs).
//
// The server runs ONE turn per user and refuses a new message while an approval is pending, so the
// console never sends while a turn runs, an approval waits, or the queue is paused after an error:
// a message typed then waits, visibly, in the queue, and the queue drains in order, one per turn.

export function createChat() {
  return { id: null, items: [], busy: false, queue: [], paused: false, statusText: "", seq: 0 };
}

const nextId = (c) => ++c.seq;
const push = (c, item) => { const it = { key: nextId(c), v: 1, ...item }; c.items.push(it); return it; };
const touch = (it) => { it.v = (it.v || 0) + 1; };

export const pendingApprovals = (c) => c.items.filter((x) => x.kind === "confirm" && !x.answered);

/** True while nothing may be sent: a turn runs, an approval waits, or the queue is paused. */
export const blocked = (c) => c.busy || c.paused || pendingApprovals(c).length > 0;

/** Queue a message (every message goes through the queue; drain sends it at once if nothing blocks). */
export function enqueue(c, text) {
  const t = String(text || "").trim();
  if (!t) return null;
  const q = { id: nextId(c), text: t };
  c.queue.push(q);
  return q;
}

export function removeQueued(c, id) {
  const i = c.queue.findIndex((q) => q.id === id);
  if (i >= 0) c.queue.splice(i, 1);
  return i >= 0;
}

export function moveUp(c, id) {
  const i = c.queue.findIndex((q) => q.id === id);
  if (i <= 0) return false;
  [c.queue[i - 1], c.queue[i]] = [c.queue[i], c.queue[i - 1]];
  return true;
}

/** The next message to send, removed from the queue, or null when the queue must wait. */
export function takeNext(c) {
  if (blocked(c) || c.queue.length === 0) return null;
  return c.queue.shift();
}

/** Why the queue is not moving (shown next to "Queued (n)"). */
export function queueNote(c) {
  if (!c.queue.length) return "";
  if (pendingApprovals(c).length) return "waiting for your approval";
  if (c.paused) return "paused after an error";
  if (c.busy) return "sent when the current reply finishes";
  return "";
}

/** Start a turn for a user message: the bubble goes in the log and the chat is busy. */
export function beginTurn(c, text) {
  push(c, { kind: "user", text });
  c.busy = true;
  c.statusText = "Thinking…";
}

/** Answer an approval: returns the request body for /api/chat/confirm, or null if it cannot be answered now. */
export function answer(c, item, approve) {
  if (c.busy || !item || item.kind !== "confirm" || item.answered) return null;
  item.answered = approve ? "approved" : "declined";
  touch(item);
  c.busy = true;
  c.statusText = approve ? "Working…" : "";
  return { conversation_id: c.id, call_id: item.call_id, approve: !!approve };
}

/** The stream ended. A failed request (not an error event inside a turn) pauses a non-empty queue,
 *  so one refusal (a spent budget, a lost session) does not fail every queued message in turn. */
export function endTurn(c, { failed = false } = {}) {
  c.busy = false;
  c.statusText = "";
  if (failed && c.queue.length) c.paused = true;
}

export const resume = (c) => { c.paused = false; };

/** Apply one SSE event. Returns effects for the page: {focus: id}, {refresh: [...]}, {usage}. */
export function reduce(c, ev) {
  const fx = {};
  if (!ev || typeof ev !== "object") return fx;
  if (typeof ev.conversation_id === "string" && ev.conversation_id) c.id = ev.conversation_id;
  switch (ev.type) {
    case "status":
      c.statusText = String(ev.text || ev.message || "");
      break;
    case "tool":
      push(c, { kind: "tool", call: ev.call_id, name: String(ev.name || "tool"), args: ev.args || {}, ok: undefined });
      c.statusText = `Running ${ev.name || "a tool"}…`;
      break;
    case "tool_result": {
      const t = [...c.items].reverse().find((x) => x.kind === "tool" && x.call === ev.call_id);
      if (t) { t.ok = !!ev.ok; t.result = String(ev.summary ?? ""); touch(t); }
      else {
        const k = c.items.find((x) => x.kind === "confirm" && x.call_id === ev.call_id);
        if (k) { k.result = String(ev.summary ?? ""); touch(k); }
      }
      c.statusText = "Thinking…";
      break;
    }
    case "confirm":
      push(c, { kind: "confirm", call_id: ev.call_id, name: String(ev.name || ""), title: String(ev.title || ev.name || "this action"), args: ev.args || {}, answered: null });
      c.statusText = "";
      break;
    case "message":
      if (ev.text) push(c, { kind: "assistant", text: String(ev.text) });
      break;
    case "error":
      push(c, { kind: "error", text: String(ev.message || "something went wrong") });
      break;
    case "done":
      if (ev.usage) fx.usage = ev.usage;
      c.statusText = "";
      break;
    case "focus":
      if (ev.instance_id) fx.focus = String(ev.instance_id);
      break;
    case "refresh":
      fx.refresh = Array.isArray(ev.what) ? ev.what.map(String) : [];
      break;
    default:
      break;
  }
  return fx;
}

/** A failure of the request itself, as an error bubble. */
export const pushError = (c, text) => push(c, { kind: "error", text: String(text) });

export const isEmpty = (c) => c.items.length === 0;

// Shown when GET /api/examples is missing or fails.
export const FALLBACK_EXAMPLES = [
  { id: "standard", category: "Get started", title: "A standard test stack", prompt: "Create a standard test environment with a wallet, an issuer and a verifier." },
  { id: "registry", category: "Get started", title: "Credentials from the SIROS registry", prompt: "Create an environment that uses the credential types from the SIROS registry." },
  { id: "status", category: "Manage", title: "What is running?", prompt: "Which of my environments are running, and when do they expire?" },
  { id: "dcapi", category: "Explore", title: "Try the Digital Credentials API", prompt: "Set up an environment where I can test the W3C Digital Credentials API." },
];

/** Examples grouped by category, in first-seen order; malformed entries are dropped. */
export function groupExamples(examples) {
  const groups = new Map();
  for (const e of examples || []) {
    if (!e || typeof e.prompt !== "string" || !e.prompt.trim()) continue;
    const cat = String(e.category || "Examples");
    if (!groups.has(cat)) groups.set(cat, []);
    groups.get(cat).push({ id: String(e.id || e.prompt), title: String(e.title || e.prompt), prompt: e.prompt });
  }
  return [...groups].map(([category, items]) => ({ category, items }));
}
