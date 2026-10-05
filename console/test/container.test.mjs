import test from "node:test";
import assert from "node:assert/strict";
import * as C from "../js/container.js";

const rnd = (n) => globalThis.crypto.getRandomValues(new Uint8Array(n));
const SALT = rnd(32);
const mk = () => ({ credentialId: rnd(32), prfOutput: rnd(32), prfSalt: SALT, transports: ["internal"] });
const hex = (b) => Buffer.from(b).toString("hex");
const raw = (t) => C.unb64u(t.$b64u);

test("create then open returns the same 32-byte main key", async () => {
  const p = mk();
  const { container, mainKey } = await C.createContainer(p);
  assert.equal(mainKey.length, 32);
  assert.deepEqual(await C.openContainer(container, p.credentialId, p.prfOutput), mainKey);
});

test("every container gets its own main key", async () => {
  const [a, b] = [await C.createContainer(mk()), await C.createContainer(mk())];
  assert.notEqual(hex(a.mainKey), hex(b.mainKey));
});

test("a wrong PRF output, an unknown passkey and a tampered container all fail with ONE message", async () => {
  const p = mk();
  const { container } = await C.createContainer(p);
  const messages = new Set();
  for (const attempt of [
    () => C.openContainer(container, p.credentialId, rnd(32)),
    () => C.openContainer(container, p.credentialId, new Uint8Array(32)),
  ]) {
    await assert.rejects(attempt, (e) => { messages.add(e.message); return true; });
  }
  assert.equal(messages.size, 1, "no oracle about WHICH step failed");
  await assert.rejects(C.openContainer(container, rnd(32), p.prfOutput), /not registered/);
});

test("the PRF output must be a real 32+ byte secret", async () => {
  await assert.rejects(C.createContainer({ ...mk(), prfOutput: new Uint8Array(8) }), /at least 32 bytes/);
  await assert.rejects(C.createContainer({ ...mk(), prfOutput: "not bytes" }), /at least 32 bytes/);
});

test("the structure follows the spec's key layer", async () => {
  const p = mk();
  const { container } = await C.createContainer(p);
  assert.equal(container.format, C.FORMAT);
  assert.equal(container.version, 1);
  const pub = container.mainKey.publicKey.importKey;
  assert.deepEqual([pub.format, pub.algorithm.name, pub.algorithm.namedCurve], ["raw", "ECDH", "P-256"]);
  assert.equal(raw(pub.keyData).length, 65);
  assert.equal(raw(pub.keyData)[0], 4, "uncompressed point");
  assert.deepEqual(container.mainKey.unwrapKey, { format: "raw", unwrapAlgo: "AES-KW", unwrappedKeyAlgo: { name: "AES-GCM", length: 256 } });
  const [e] = container.prfKeys;
  assert.equal(raw(e.credentialId).length, 32);
  assert.equal(raw(e.prfSalt).length, 32);
  assert.equal(raw(e.hkdfSalt).length, 32);
  assert.equal(new TextDecoder().decode(raw(e.hkdfInfo)), C.HKDF_INFO);
  assert.equal(raw(e.keypair.publicKey.importKey.keyData).length, 65);
  assert.equal(e.keypair.privateKey.unwrapKey.format, "jwk");
  assert.equal(e.keypair.privateKey.unwrapKey.unwrapAlgo.name, "AES-GCM");
  assert.equal(raw(e.keypair.privateKey.unwrapKey.unwrapAlgo.iv).length, 12);
  assert.equal(raw(e.unwrapKey.wrappedKey).length, 40, "AES-KW of 32 bytes is 40");
  assert.deepEqual(e.transports, ["internal"]);
});

test("binary is tagged base64url with no padding or +/", async () => {
  const text = JSON.stringify((await C.createContainer(mk())).container);
  for (const m of text.matchAll(/"\$b64u":"([^"]*)"/g)) assert.match(m[1], /^[A-Za-z0-9_-]+$/);
  assert.ok(!text.includes("="));
});

test("our own HKDF info keeps a PRF output from being reused as the wallet's", async () => {
  assert.notEqual(C.HKDF_INFO, "eDiplomas PRF");
});

test("serialize and parse round trip, and parse refuses other things", async () => {
  const p = mk();
  const { container, mainKey } = await C.createContainer(p);
  const back = C.parse(C.serialize(container));
  assert.deepEqual(await C.openContainer(back, p.credentialId, p.prfOutput), mainKey);
  assert.throws(() => C.parse(new TextEncoder().encode("{}")), /not a sirosid key container/);
  assert.throws(() => C.parse(new TextEncoder().encode(JSON.stringify({ ...container, version: 2 }))), /not a sirosid/);
  assert.throws(() => C.parse(new TextEncoder().encode("not json")));
});

test("adding a passkey keeps the main key and opens with either, with only the main key in hand", async () => {
  const a = mk(), b = mk();
  const { container, mainKey } = await C.createContainer(a);
  const two = await C.addPasskey(container, mainKey, b);
  assert.equal(two.prfKeys.length, 2);
  assert.deepEqual(await C.openContainer(two, a.credentialId, a.prfOutput), mainKey);
  assert.deepEqual(await C.openContainer(two, b.credentialId, b.prfOutput), mainKey);
  assert.notEqual(raw(two.mainKey.publicKey.importKey.keyData).toString(), raw(container.mainKey.publicKey.importKey.keyData).toString(),
    "a fresh ephemeral key is used each time");
  assert.deepEqual(C.credentialIds(two).map(hex), [hex(a.credentialId), hex(b.credentialId)]);
  assert.equal(container.prfKeys.length, 1, "the input is not mutated");
});

test("the same passkey cannot be added twice", async () => {
  const a = mk();
  const { container, mainKey } = await C.createContainer(a);
  await assert.rejects(C.addPasskey(container, mainKey, a), /already in the key container/);
});

test("adding with the wrong main key yields a container nobody can reconcile with the data", async () => {
  const a = mk(), b = mk();
  const { container, mainKey } = await C.createContainer(a);
  const two = await C.addPasskey(container, rnd(32), b);                  // wrong key supplied
  const viaB = await C.openContainer(two, b.credentialId, b.prfOutput);
  assert.notDeepEqual(viaB, mainKey, "which is why the caller must verify the key (the server's key check does)");
});

test("removing a passkey: the removed one cannot open the NEW container, the rest can", async () => {
  const a = mk(), b = mk();
  const { container, mainKey } = await C.createContainer(a);
  const two = await C.addPasskey(container, mainKey, b);
  const one = await C.removePasskey(two, mainKey, a.credentialId);
  await assert.rejects(C.openContainer(one, a.credentialId, a.prfOutput), /not registered/);
  assert.deepEqual(await C.openContainer(one, b.credentialId, b.prfOutput), mainKey);
  await assert.rejects(C.removePasskey(one, mainKey, b.credentialId), /at least one passkey/);
  await assert.rejects(C.removePasskey(one, mainKey, rnd(32)), /not in the key container/);
});

test("tampering with any wrapped value or the ephemeral key makes opening fail", async () => {
  const p = mk();
  const { container } = await C.createContainer(p);
  const flip = (t) => { const b = raw(t); b[b.length - 1] ^= 1; t.$b64u = C.b64u(b); };
  for (const path of [
    (c) => c.prfKeys[0].unwrapKey.wrappedKey,
    (c) => c.prfKeys[0].keypair.privateKey.unwrapKey.wrappedKey,
    (c) => c.prfKeys[0].hkdfSalt,
    (c) => c.prfKeys[0].keypair.privateKey.unwrapKey.unwrapAlgo.iv,
  ]) {
    const c = structuredClone(container);
    flip(path(c));
    await assert.rejects(C.openContainer(c, p.credentialId, p.prfOutput), /could not open/);
  }
  const c = structuredClone(container);
  const other = (await C.createContainer(mk())).container;
  c.mainKey = other.mainKey;                                              // someone else's ephemeral key
  await assert.rejects(C.openContainer(c, p.credentialId, p.prfOutput), /could not open/);
});

test("an entry from another container does not open this one", async () => {
  const a = mk(), b = mk();
  const A = (await C.createContainer(a)).container;
  const B = (await C.createContainer(b)).container;
  const frank = structuredClone(A);
  frank.prfKeys = B.prfKeys;
  await assert.rejects(C.openContainer(frank, b.credentialId, b.prfOutput), /could not open/);
});

test("base64url helpers", () => {
  for (const n of [0, 1, 2, 3, 31, 32, 33, 65]) {
    const b = rnd(n);
    assert.deepEqual(C.unb64u(C.b64u(b)), b);
  }
  assert.throws(() => C.unb64u("a+b"), /not base64url/);
  assert.throws(() => C.unb64u("a=="), /not base64url/);
});

test("the main key must be 32 bytes", async () => {
  const a = mk();
  const { container } = await C.createContainer(a);
  await assert.rejects(C.addPasskey(container, new Uint8Array(16), mk()), /32 bytes/);
});
