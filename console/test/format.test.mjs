import test from "node:test";
import assert from "node:assert/strict";
import * as F from "../js/format.js";
import { ApiError, isMissingEndpoint } from "../js/api.js";

test("durations keep the two most significant units", () => {
  assert.equal(F.duration(2 * 86400 + 3 * 3600 + 59), "2d 3h");
  assert.equal(F.duration(2 * 86400), "2d");
  assert.equal(F.duration(3 * 3600 + 5 * 60), "3h 5m");
  assert.equal(F.duration(3600), "1h");
  assert.equal(F.duration(12 * 60 + 30), "12m");
  assert.equal(F.duration(20), "less than a minute");
  assert.equal(F.duration(-5), "less than a minute");
});

test("expiry: kept, expires in, expired, unknown", () => {
  const now = 1_000_000;
  assert.equal(F.expiry({ kept: true, expires_at: now + 10 }, now), "kept");
  assert.equal(F.expiry({ kept: false, expires_at: now + 86400 + 7200 }, now), "expires in 1d 2h");
  assert.equal(F.expiry({ kept: false, expires_at: now - 1 }, now), "expired");
  assert.equal(F.expiry({ kept: false, expires_at: null }, now), "");
  assert.equal(F.expiry(null, now), "");
  assert.equal(F.ago(now - 30, now), "just now");
  assert.equal(F.ago(now - 300, now), "5m ago");
});

test("status pills", () => {
  assert.deepEqual(F.statusPill("running"), { label: "Running", tone: "ok", spinner: false });
  for (const s of ["creating", "reconfiguring", "resetting"]) {
    assert.equal(F.statusPill(s).tone, "busy", s); assert.equal(F.statusPill(s).spinner, true, s);
    assert.ok(F.isTransient(s));
  }
  assert.equal(F.statusPill("stopped").tone, "idle");
  assert.equal(F.statusPill("failed").tone, "bad");
  assert.equal(F.statusPill(undefined).label, "Unknown");
  assert.ok(!F.isTransient("running") && !F.isTransient("failed") && !F.isTransient("stopped"));
});

test("urls are labelled by role, in a fixed order, unknown keys last", () => {
  const cards = F.urlCards({ "zz-thing": "https://z", "mini-oidc": "https://m", "wallet-frontend": "https://w",
    "vc-apigw": "https://i", "vc-verifier": "https://v", "vc-registry": "https://r", empty: "", bad: 5 });
  assert.deepEqual(cards.map((c) => c.role), ["Wallet", "Issuer", "Verifier", "Registry", "Test login (mini-oidc)", "zz-thing"]);
  assert.equal(cards[0].url, "https://w");
  assert.deepEqual(F.urlCards(null), []);
  assert.ok(F.isHttpsUrl("https://wallet-abc.sirosid.dev"));
  for (const u of ["http://x", "javascript:alert(1)", "https://", " https://x", 5]) assert.ok(!F.isHttpsUrl(u), String(u));
});

test("tool arguments in plain words", () => {
  const lines = F.describeArgs({ id: "abcd1234", keep: true, config_name: "base", extra: { a: 1 }, none: null, long: "x".repeat(300) },
    (id) => (id === "abcd1234" ? "demo" : null));
  assert.deepEqual(lines.slice(0, 5), [["Environment", "demo (abcd1234)"], ["Keep", "yes"], ["Config name", "base"], ["Extra", '{"a":1}'], ["None", "none"]]);
  assert.equal(lines[5][1].length, 238);
  assert.deepEqual(F.describeArgs({ id: "zz" }), [["Environment", "zz"]]);
  assert.equal(F.briefArgs({}), "");
});

test("quota line", () => {
  assert.equal(F.quotaLine([{}, {}], { max_concurrent: 5 }), "2 of 5 environments");
  assert.equal(F.quotaLine([], { max_concurrent: 1 }), "0 of 1 environment");
  assert.equal(F.quotaLine([{}], null), "1 environment");
  assert.equal(F.envLabel({ id: "a1", name: "" }), "a1");
});

test("a missing endpoint is the framework's code-less 404/405, never the API's not_found", () => {
  assert.ok(isMissingEndpoint(new ApiError(404, {})));
  assert.ok(isMissingEndpoint(new ApiError(405, {})));
  assert.ok(!isMissingEndpoint(new ApiError(404, { error: "not_found", message: "no such instance" })));
  assert.ok(!isMissingEndpoint(new ApiError(500, {})));
  assert.ok(!isMissingEndpoint(new Error("x")));
});

test("parseRich: paragraphs, bullets, bold and code, and nothing else", () => {
  const b = F.parseRich("I created **demo** for you.\n\nNext:\n- open `the wallet`\n- sign up");
  assert.equal(b.length, 3);
  assert.deepEqual(b[0], { type: "p", inlines: [{ t: "text", v: "I created " }, { t: "b", v: "demo" }, { t: "text", v: " for you." }] });
  assert.equal(b[2].type, "ul");
  assert.deepEqual(b[2].items[0], [{ t: "text", v: "open " }, { t: "code", v: "the wallet" }]);
  assert.deepEqual(F.parseRich(""), []);
  assert.deepEqual(F.parseRich(null), []);
});

test("parseRich never produces markup: html, links and images stay literal text", () => {
  const hostile = '<img src=x onerror=alert(1)> [click](javascript:alert(1)) ![i](http://e/x.png) <script>1</script>';
  const b = F.parseRich(hostile);
  assert.equal(b.length, 1);
  assert.deepEqual(b[0].inlines, [{ t: "text", v: hostile }], "one literal text run: no tags are interpreted");
  assert.deepEqual(F.parseRich("**unclosed and `unclosed")[0].inlines, [{ t: "text", v: "**unclosed and `unclosed" }]);
});
