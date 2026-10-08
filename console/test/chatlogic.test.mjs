import test from "node:test";
import assert from "node:assert/strict";
import * as L from "../js/chatlogic.js";

const texts = (c) => c.queue.map((q) => q.text);
const drainAll = (c) => { const sent = []; for (let m; (m = L.takeNext(c)); ) { sent.push(m.text); L.beginTurn(c, m.text); L.endTurn(c); } return sent; };

test("a message sent while idle goes straight out; empty text is never queued", () => {
  const c = L.createChat();
  assert.equal(L.enqueue(c, "   "), null);
  L.enqueue(c, "hello");
  assert.equal(L.takeNext(c).text, "hello");
  assert.equal(c.queue.length, 0);
  assert.equal(L.takeNext(c), null);
});

test("WHILE A TURN RUNS NOTHING IS SENT: messages queue and drain in order, one per turn", () => {
  const c = L.createChat();
  L.enqueue(c, "first"); L.beginTurn(c, L.takeNext(c).text);
  L.enqueue(c, "a"); L.enqueue(c, "b"); L.enqueue(c, "c");
  assert.equal(L.takeNext(c), null, "busy: must not send");
  assert.deepEqual(texts(c), ["a", "b", "c"]);
  assert.equal(L.queueNote(c), "sent when the current reply finishes");
  L.endTurn(c);
  const m = L.takeNext(c);
  assert.equal(m.text, "a");
  L.beginTurn(c, m.text);
  assert.equal(L.takeNext(c), null, "the second waits for the next turn");
  L.endTurn(c);
  assert.deepEqual(drainAll(c), ["b", "c"]);
});

test("remove and move-up reorder the queue", () => {
  const c = L.createChat(); c.busy = true;
  const a = L.enqueue(c, "a"), b = L.enqueue(c, "b"), d = L.enqueue(c, "d");
  assert.equal(L.moveUp(c, a.id), false, "the head cannot move up");
  assert.equal(L.moveUp(c, d.id), true);
  assert.deepEqual(texts(c), ["a", "d", "b"]);
  assert.equal(L.removeQueued(c, a.id), true);
  assert.equal(L.removeQueued(c, 999), false);
  assert.deepEqual(texts(c), ["d", "b"]);
  L.endTurn(c);
  assert.deepEqual(drainAll(c), ["d", "b"]);
  void b;
});

test("A PENDING APPROVAL BLOCKS THE QUEUE until it is answered and that turn ends", () => {
  const c = L.createChat();
  L.beginTurn(c, "destroy it");
  L.reduce(c, { type: "confirm", conversation_id: "cv1", call_id: "k1", name: "destroy_instance", title: "Destroy an instance", args: { id: "abcd1234" } });
  L.endTurn(c);                                             // the stream ends while paused for the approval
  L.enqueue(c, "next");
  assert.equal(c.id, "cv1");
  assert.equal(L.pendingApprovals(c).length, 1);
  assert.equal(L.takeNext(c), null, "an approval is pending: nothing may be sent");
  assert.equal(L.queueNote(c), "waiting for your approval");
  const item = L.pendingApprovals(c)[0];
  assert.deepEqual(L.answer(c, item, true), { conversation_id: "cv1", call_id: "k1", approve: true });
  assert.equal(L.answer(c, item, false), null, "an approval is answered once");
  assert.equal(L.takeNext(c), null, "the confirm turn is running");
  L.endTurn(c);
  assert.equal(L.answer(c, item, true), null, "still answered once after that turn ended");
  assert.equal(L.takeNext(c).text, "next");
});

test("an approval cannot be answered while a turn runs", () => {
  const c = L.createChat();
  L.reduce(c, { type: "confirm", call_id: "k1", title: "Reset" });
  c.busy = true;
  assert.equal(L.answer(c, L.pendingApprovals(c)[0], true), null);
  assert.equal(L.pendingApprovals(c).length, 1);
});

test("a failed request pauses a non-empty queue; resume lets it drain", () => {
  const c = L.createChat();
  L.beginTurn(c, "x"); L.enqueue(c, "y");
  L.endTurn(c, { failed: true });
  assert.equal(L.takeNext(c), null);
  assert.equal(L.queueNote(c), "paused after an error");
  L.resume(c);
  assert.equal(L.takeNext(c).text, "y");
  const d = L.createChat(); L.beginTurn(d, "x"); L.endTurn(d, { failed: true });
  assert.equal(d.paused, false, "an empty queue is not paused");
});

test("SSE reduction: tools, results, messages, errors, usage, focus and refresh", () => {
  const c = L.createChat();
  L.beginTurn(c, "make one");
  assert.deepEqual(L.reduce(c, { type: "tool", call_id: "t1", name: "create_instance", args: { name: "demo" } }), {});
  L.reduce(c, { type: "tool_result", call_id: "t1", ok: true, summary: "created" });
  const tool = c.items.find((x) => x.kind === "tool");
  assert.equal(tool.ok, true); assert.equal(tool.result, "created"); assert.ok(tool.v > 1, "a changed item gets a new version");
  assert.deepEqual(L.reduce(c, { type: "focus", instance_id: "abcd1234" }), { focus: "abcd1234" });
  assert.deepEqual(L.reduce(c, { type: "refresh", what: ["instances"] }), { refresh: ["instances"] });
  assert.deepEqual(L.reduce(c, { type: "refresh" }), { refresh: [] });
  L.reduce(c, { type: "message", text: "Done." });
  L.reduce(c, { type: "error", message: "oops" });
  assert.deepEqual(L.reduce(c, { type: "done", conversation_id: "cv9", usage: { used: 5, limit: 10 } }), { usage: { used: 5, limit: 10 } });
  assert.equal(c.id, "cv9");
  assert.deepEqual(c.items.map((x) => x.kind), ["user", "tool", "assistant", "error"]);
  assert.deepEqual(L.reduce(c, null), {});
  assert.deepEqual(L.reduce(c, { type: "unknown" }), {});
  const keys = c.items.map((x) => x.key);
  assert.equal(new Set(keys).size, keys.length, "every item has its own key");
});

test("a declined approval records the result on the approval card", () => {
  const c = L.createChat();
  L.reduce(c, { type: "confirm", call_id: "k1", title: "Reset" });
  L.answer(c, c.items[0], false);
  L.reduce(c, { type: "tool_result", call_id: "k1", ok: false, summary: "declined" });
  assert.equal(c.items[0].result, "declined");
  assert.equal(c.items[0].answered, "declined");
});

test("examples are grouped by category in order, malformed ones dropped", () => {
  const g = L.groupExamples([{ id: "1", title: "A", prompt: "pa", category: "X" }, { id: "2", prompt: "pb", category: "Y" },
    { id: "3", title: "C", prompt: "pc", category: "X" }, { id: "4", title: "bad" }, null, { prompt: "  " }]);
  assert.deepEqual(g.map((x) => [x.category, x.items.map((i) => i.title)]), [["X", ["A", "C"]], ["Y", ["pb"]]]);
  assert.equal(L.groupExamples(L.FALLBACK_EXAMPLES).flatMap((x) => x.items).length, 4);
  assert.deepEqual(L.groupExamples([{ prompt: "p", category: "get-started" }, { prompt: "q" }]).map((g) => g.category), ["Get started", "Examples"]);
});

test("a prompt with a <slot> is prefilled with the slot selected, not sent", () => {
  assert.deepEqual(L.firstSlot("Trust the issuer at <issuer URL> for me"), [20, 32]);
  assert.equal(L.firstSlot("Create a standard environment"), null);
  assert.equal(L.firstSlot("a <> b"), null);
  assert.equal(L.firstSlot(null), null);
});
