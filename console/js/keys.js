// Passkeys and the key container: enrolment, sign-in/unlock, adding and removing a passkey.
// The PRF output stays in this page: it derives the container's wrapping key and is never sent.
import { api } from "./api.js";
import * as C from "./container.js";
import { creationOptions, requestOptions, credentialToJSON, prfEnabled, prfFirst, randomChallenge } from "./webauthn.js";
import { state } from "./store.js";

const saltOf = (options) => C.unb64u(options.extensions.prf.eval.first);

/** Ask the authenticator for the PRF output of ONE credential (a second touch after create). */
async function prfFor(credentialId, options) {
  const a = await navigator.credentials.get({ publicKey: {
    challenge: randomChallenge(), rpId: options.rp.id, userVerification: "required", timeout: 60000,
    allowCredentials: [{ type: "public-key", id: credentialId }],
    extensions: { prf: { eval: { first: saltOf(options) } } } } });
  const out = prfFirst(a);
  if (!out) throw new Error("this passkey did not return a PRF output, so it cannot protect your data");
  return out;
}

async function createPasskey(begin) {
  const cred = await navigator.credentials.create({ publicKey: creationOptions(begin.options) });
  if (!prfEnabled(cred)) {
    throw new Error("this authenticator does not support the WebAuthn PRF extension, which protects your data. " +
                    "Use a different passkey (a recent security key, or a platform passkey that supports PRF).");
  }
  return cred;
}

const storeContainer = (container) => api("PUT", "/api/privatedata", { container: C.b64u(C.serialize(container)) });
const unlockServer = (mainKey) => api("POST", "/api/unlock", { main_key: C.b64u(mainKey) });

async function firstContainer(rawId, prf, options) {
  const { container, mainKey } = await C.createContainer({ credentialId: rawId, prfOutput: prf, prfSalt: saltOf(options) });
  await storeContainer(container);
  state.container = container;
  state.mainKey = mainKey;
  await unlockServer(mainKey);
}

export async function enroll(invite, name, email) {
  const begin = await api("POST", "/api/enroll/begin", { invite, name, email });
  const cred = await createPasskey(begin);            // refuses non-PRF BEFORE the invite can be spent
  await api("POST", "/api/enroll/finish", { ceremony_id: begin.ceremony_id, credential: credentialToJSON(cred) });
  const prf = await prfFor(cred.rawId, begin.options);
  await firstContainer(new Uint8Array(cred.rawId), prf, begin.options);
}

export async function login() {
  const begin = await api("POST", "/api/login/begin");
  const a = await navigator.credentials.get({ publicKey: requestOptions(begin.options) });
  const prf = prfFirst(a);
  await api("POST", "/api/login/finish", { ceremony_id: begin.ceremony_id, credential: credentialToJSON(a) });
  if (!prf) throw new Error("this passkey did not return a PRF output; it cannot unlock your data");
  const raw = new Uint8Array(a.rawId);
  const { container } = await api("GET", "/api/privatedata");
  if (!container) return firstContainer(raw, prf, begin.options);        // enrolment was interrupted before the container was stored
  state.container = C.parse(C.unb64u(container));
  state.mainKey = await C.openContainer(state.container, raw, prf);
  await unlockServer(state.mainKey);
}

export async function addPasskey(label) {
  if (!state.mainKey) throw new Error("sign in again first: adding a passkey needs your unlocked key");
  const begin = await api("POST", "/api/passkeys/begin");
  const cred = await createPasskey(begin);
  const prf = await prfFor(cred.rawId, begin.options);
  // The container is stored FIRST: a failure after this leaves an unused entry (harmless), never a
  // registered passkey that cannot open the container.
  const next = await C.addPasskey(state.container, state.mainKey,
    { credentialId: new Uint8Array(cred.rawId), prfOutput: prf, prfSalt: saltOf(begin.options), transports: cred.response.getTransports?.() });
  await storeContainer(next);
  await api("POST", "/api/passkeys/finish", { ceremony_id: begin.ceremony_id, credential: credentialToJSON(cred), label });
  state.container = next;
}

export async function removePasskey(id) {
  await api("DELETE", `/api/passkeys/${encodeURIComponent(id)}`);
  if (state.mainKey) {
    const next = await C.removePasskey(state.container, state.mainKey, C.unb64u(id));
    await storeContainer(next);
    state.container = next;
  }
}
