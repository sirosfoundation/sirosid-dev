import test from "node:test";
import assert from "node:assert/strict";
import * as W from "../js/webauthn.js";
import { b64u } from "../js/container.js";

const bytes = (...v) => Uint8Array.from(v);

test("creation options: binary fields become buffers and the PRF salt is passed through", () => {
  const o = W.creationOptions({
    challenge: b64u(bytes(1, 2)), rp: { id: "x" }, user: { id: b64u(bytes(3)), name: "n" },
    excludeCredentials: [{ type: "public-key", id: b64u(bytes(9)) }],
    extensions: { prf: { eval: { first: b64u(bytes(7, 7)) } } } });
  assert.deepEqual(o.challenge, bytes(1, 2));
  assert.deepEqual(o.user.id, bytes(3));
  assert.deepEqual(o.excludeCredentials[0].id, bytes(9));
  assert.deepEqual(o.extensions.prf.eval.first, bytes(7, 7));
});

test("request options: allowCredentials can pin one credential, extensions default to none", () => {
  const o = W.requestOptions({ challenge: b64u(bytes(1)) }, bytes(5));
  assert.deepEqual(o.allowCredentials, [{ type: "public-key", id: bytes(5) }]);
  assert.deepEqual(o.extensions, {});
});

const fakeCred = (extras = {}, response = {}) => ({
  id: "abc", type: "public-key", rawId: bytes(1, 2, 3).buffer,
  response: { clientDataJSON: bytes(1).buffer, ...response },
  getClientExtensionResults: () => extras });

test("THE PRF OUTPUT NEVER GOES INTO WHAT THE SERVER GETS", () => {
  const secret = bytes(...Array(32).fill(0xAB));
  const cred = fakeCred({ prf: { enabled: true, results: { first: secret.buffer } } },
    { authenticatorData: bytes(2).buffer, signature: bytes(3).buffer });
  const json = W.credentialToJSON(cred);
  assert.deepEqual(json.clientExtensionResults, {});
  const wire = JSON.stringify(json);
  assert.ok(!wire.includes(b64u(secret)), "the PRF output must not appear in the request body");
  assert.ok(!wire.toLowerCase().includes("prf"));
  assert.deepEqual(W.prfFirst(cred), secret, "but the page can still read it");
});

test("registration and assertion shapes", () => {
  const reg = W.credentialToJSON(fakeCred({}, { attestationObject: bytes(4).buffer, getTransports: () => ["usb"] }));
  assert.deepEqual(Object.keys(reg.response).sort(), ["attestationObject", "clientDataJSON", "transports"]);
  const as = W.credentialToJSON(fakeCred({}, { authenticatorData: bytes(1).buffer, signature: bytes(2).buffer, userHandle: bytes(3).buffer }));
  assert.deepEqual(Object.keys(as.response).sort(), ["authenticatorData", "clientDataJSON", "signature", "userHandle"]);
  assert.equal(as.rawId, b64u(bytes(1, 2, 3)));
});

test("prfEnabled is true only for an explicit true", () => {
  assert.equal(W.prfEnabled(fakeCred({ prf: { enabled: true } })), true);
  assert.equal(W.prfEnabled(fakeCred({ prf: { enabled: false } })), false);
  assert.equal(W.prfEnabled(fakeCred({ prf: {} })), false);
  assert.equal(W.prfEnabled(fakeCred({})), false);
  assert.equal(W.prfFirst(fakeCred({})), null);
});
