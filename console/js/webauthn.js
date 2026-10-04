// WebAuthn glue: JSON <-> ArrayBuffer conversion and the PRF extension, and nothing else, so it
// can be tested without a browser.
//
// The PRF OUTPUT is a secret that must never leave the browser. credentialToJSON therefore sends
// the server an EMPTY clientExtensionResults, and prfFirst is the only way to read the output.
import { b64u, unb64u } from "./container.js";

const buf = (s) => unb64u(s);

function prfExtension(ext) {
  const first = ext?.prf?.eval?.first;
  return first ? { prf: { eval: { first: buf(first) } } } : {};
}

export function creationOptions(json) {
  const o = { ...json };
  o.challenge = buf(json.challenge);
  o.user = { ...json.user, id: buf(json.user.id) };
  o.excludeCredentials = (json.excludeCredentials || []).map((c) => ({ ...c, id: buf(c.id) }));
  o.extensions = prfExtension(json.extensions);
  return o;
}

export function requestOptions(json, allowCredentialId = null) {
  const o = { ...json };
  o.challenge = buf(json.challenge);
  o.allowCredentials = allowCredentialId
    ? [{ type: "public-key", id: allowCredentialId }]
    : (json.allowCredentials || []).map((c) => ({ ...c, id: buf(c.id) }));
  o.extensions = prfExtension(json.extensions);
  return o;
}

export function credentialToJSON(cred) {
  const r = cred.response;
  const response = { clientDataJSON: b64u(r.clientDataJSON) };
  if (r.attestationObject) {
    response.attestationObject = b64u(r.attestationObject);
    if (r.getTransports) response.transports = r.getTransports();
  } else {
    response.authenticatorData = b64u(r.authenticatorData);
    response.signature = b64u(r.signature);
    if (r.userHandle) response.userHandle = b64u(r.userHandle);
  }
  return { id: cred.id, rawId: b64u(cred.rawId), type: cred.type, response, clientExtensionResults: {} };
}

/** true only when the authenticator says it supports PRF (reported at registration). */
export const prfEnabled = (cred) => cred.getClientExtensionResults?.()?.prf?.enabled === true;

/** The PRF output (32 bytes) or null. */
export function prfFirst(cred) {
  const first = cred.getClientExtensionResults?.()?.prf?.results?.first;
  return first ? new Uint8Array(first) : null;
}

export const randomChallenge = () => globalThis.crypto.getRandomValues(new Uint8Array(32));
