// The user's key container: one main AES-256-GCM key, openable by any of the user's
// passkeys. WebCrypto only, no dependencies, no build step.
//
// This is the KEY LAYER of the SIROS private data specification (privatedata-spec, sections
// 4-5), as implemented by wallet-frontend's keystore.ts - the same structure and the same
// primitives - without the wallet-state JWE payload (a console has no wallet state) and with
// its own HKDF info string, so a PRF output can never be mistaken for the wallet's.
//
//   PRF output --HKDF-SHA256--> AES-GCM wrapping key
//              --unwraps-->     this passkey's ECDH P-256 private key (stored wrapped, as JWK)
//              --ECDH with-->   mainKey.publicKey (an EPHEMERAL public key stored in the container)
//              --> AES-KW key --unwraps--> the main key
//
// Because every passkey holds its own keypair, adding or removing a passkey needs only the
// main key (which the browser has while unlocked) and the other entries' PUBLIC keys: nothing
// has to be unlocked by the other passkeys.
//
// Limit worth knowing: removing a passkey from the container does not revoke access for
// someone who kept an old copy of the container AND can still produce that passkey's PRF
// output. Real revocation means a new main key and re-sealing everything under it.

export const FORMAT = "sirosid-keycontainer";
export const VERSION = 1;
export const HKDF_INFO = "sirosid-console PRF v1";

const subtle = globalThis.crypto.subtle;
const enc = new TextEncoder();
const ECDH = { name: "ECDH", namedCurve: "P-256" };
const AES_GCM = { name: "AES-GCM", length: 256 };
const AES_KW = { name: "AES-KW", length: 256 };

// ---- tagged binary: { "$b64u": "..." } (privatedata-spec 3.3) -----------------------------

export function b64u(bytes) {
  let s = "";
  for (const b of new Uint8Array(bytes)) s += String.fromCharCode(b);
  return btoa(s).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}

export function unb64u(str) {
  if (typeof str !== "string" || !/^[A-Za-z0-9_-]*$/.test(str)) throw new Error("not base64url");
  const s = str.replaceAll("-", "+").replaceAll("_", "/") + "=".repeat((4 - (str.length % 4)) % 4);
  return Uint8Array.from(atob(s), (c) => c.charCodeAt(0));
}

const tag = (bytes) => ({ $b64u: b64u(bytes) });
const untag = (t) => {
  if (!t || typeof t !== "object" || typeof t.$b64u !== "string") throw new Error("expected tagged binary");
  return unb64u(t.$b64u);
};

export function serialize(container) {
  return enc.encode(JSON.stringify(container));
}

export function parse(bytes) {
  const c = JSON.parse(new TextDecoder().decode(bytes));
  if (c?.format !== FORMAT || c.version !== VERSION || !Array.isArray(c.prfKeys) || !c.mainKey) {
    throw new Error("not a sirosid key container");
  }
  return c;
}

// ---- building blocks -----------------------------------------------------------------------

async function ecdhKeypair() {
  const { publicKey, privateKey } = await subtle.generateKey(ECDH, true, ["deriveKey"]);
  return { publicKey, privateKey, raw: new Uint8Array(await subtle.exportKey("raw", publicKey)) };
}

const publicKeyInfo = (raw) => ({ importKey: { format: "raw", keyData: tag(raw), algorithm: ECDH } });

async function prfWrappingKey(prfOutput, hkdfSalt, hkdfInfo) {
  if (!(prfOutput instanceof Uint8Array) || prfOutput.length < 32) throw new Error("the PRF output must be at least 32 bytes");
  const ikm = await subtle.importKey("raw", prfOutput, "HKDF", false, ["deriveKey"]);
  return subtle.deriveKey({ name: "HKDF", hash: "SHA-256", salt: hkdfSalt, info: hkdfInfo }, ikm, AES_GCM, false, ["wrapKey", "unwrapKey"]);
}

async function importPublic(info) {
  const k = info.importKey;
  if (k.format !== "raw" || k.algorithm.name !== "ECDH" || k.algorithm.namedCurve !== "P-256") throw new Error("unsupported public key");
  const raw = untag(k.keyData);
  if (raw.length !== 65 || raw[0] !== 4) throw new Error("expected an uncompressed P-256 point");
  return subtle.importKey("raw", raw, ECDH, true, []);
}

async function encapsulate(mainKey, ephemeralPrivate, recipientPublic) {
  const kw = await subtle.deriveKey({ name: "ECDH", public: recipientPublic }, ephemeralPrivate, AES_KW, false, ["wrapKey"]);
  return new Uint8Array(await subtle.wrapKey("raw", mainKey, kw, "AES-KW"));
}

async function newEntry({ credentialId, prfOutput, prfSalt, transports }) {
  const hkdfSalt = globalThis.crypto.getRandomValues(new Uint8Array(32));
  const hkdfInfo = enc.encode(HKDF_INFO);
  const kp = await ecdhKeypair();
  const iv = globalThis.crypto.getRandomValues(new Uint8Array(12));
  const wrappingKey = await prfWrappingKey(prfOutput, hkdfSalt, hkdfInfo);
  const wrappedPrivate = new Uint8Array(await subtle.wrapKey("jwk", kp.privateKey, wrappingKey, { name: "AES-GCM", iv }));
  return {
    credentialId: tag(credentialId),
    ...(transports ? { transports } : {}),
    prfSalt: tag(prfSalt),
    hkdfSalt: tag(hkdfSalt),
    hkdfInfo: tag(hkdfInfo),
    keypair: {
      publicKey: publicKeyInfo(kp.raw),
      privateKey: { unwrapKey: { format: "jwk", wrappedKey: tag(wrappedPrivate), unwrapAlgo: { name: "AES-GCM", iv: tag(iv) }, unwrappedKeyAlgo: ECDH } },
    },
    unwrapKey: { wrappedKey: null, unwrappingKey: { deriveKey: { algorithm: { name: "ECDH" }, derivedKeyAlgorithm: AES_KW } } },
  };
}

/** Re-encapsulate the main key for every entry under a fresh ephemeral key. Needs only the
 *  entries' PUBLIC keys, which is why adding/removing a passkey never needs another passkey. */
async function seal(mainKey, entries) {
  const eph = await ecdhKeypair();
  for (const e of entries) {
    e.unwrapKey.wrappedKey = tag(await encapsulate(mainKey, eph.privateKey, await importPublic(e.keypair.publicKey)));
  }
  return {
    format: FORMAT,
    version: VERSION,
    mainKey: { publicKey: publicKeyInfo(eph.raw), unwrapKey: { format: "raw", unwrapAlgo: "AES-KW", unwrappedKeyAlgo: AES_GCM } },
    prfKeys: entries,
  };
}

// ---- public API ------------------------------------------------------------------------------

/** A new container with a fresh random main key, openable by one passkey.
 *  Returns { container, mainKey } where mainKey is the raw 32 bytes (what /api/unlock takes). */
export async function createContainer({ credentialId, prfOutput, prfSalt, transports }) {
  const mainKey = await subtle.generateKey(AES_GCM, true, ["encrypt", "decrypt", "wrapKey", "unwrapKey"]);
  const entry = await newEntry({ credentialId, prfOutput, prfSalt, transports });
  return { container: await seal(mainKey, [entry]), mainKey: new Uint8Array(await subtle.exportKey("raw", mainKey)) };
}

function findEntry(container, credentialId) {
  const want = b64u(credentialId);
  const e = container.prfKeys.find((k) => k.credentialId?.$b64u === want);
  if (!e) throw new Error("this passkey is not registered in the key container");
  return e;
}

/** Open the container with one passkey's PRF output; returns the raw 32-byte main key. */
export async function openContainer(container, credentialId, prfOutput) {
  const e = findEntry(container, credentialId);
  const u = e.keypair.privateKey.unwrapKey;
  if (u.format !== "jwk" || u.unwrapAlgo.name !== "AES-GCM") throw new Error("unsupported container");
  try {
    const wrapping = await prfWrappingKey(prfOutput, untag(e.hkdfSalt), untag(e.hkdfInfo));
    const priv = await subtle.unwrapKey("jwk", untag(u.wrappedKey), wrapping, { name: "AES-GCM", iv: untag(u.unwrapAlgo.iv) }, ECDH, false, ["deriveKey"]);
    const kw = await subtle.deriveKey({ name: "ECDH", public: await importPublic(container.mainKey.publicKey) }, priv, AES_KW, false, ["unwrapKey"]);
    const mainKey = await subtle.unwrapKey("raw", untag(e.unwrapKey.wrappedKey), kw, "AES-KW", AES_GCM, true, ["encrypt", "decrypt"]);
    return new Uint8Array(await subtle.exportKey("raw", mainKey));
  } catch (err) {
    throw new Error("could not open the key container with this passkey");     // one message, no oracle
  }
}

async function mainKeyFromBytes(bytes) {
  if (!(bytes instanceof Uint8Array) || bytes.length !== 32) throw new Error("the main key must be 32 bytes");
  return subtle.importKey("raw", bytes, AES_GCM, true, ["encrypt", "decrypt", "wrapKey", "unwrapKey"]);
}

/** A new container that ALSO opens with this passkey. Needs the main key (the browser has it
 *  while unlocked) and nothing from the other passkeys. */
export async function addPasskey(container, mainKeyBytes, { credentialId, prfOutput, prfSalt, transports }) {
  if (container.prfKeys.some((k) => k.credentialId?.$b64u === b64u(credentialId))) throw new Error("this passkey is already in the key container");
  const entries = structuredClone(container.prfKeys);
  entries.push(await newEntry({ credentialId, prfOutput, prfSalt, transports }));
  return seal(await mainKeyFromBytes(mainKeyBytes), entries);
}

/** A new container without this passkey (never the last one). See the note at the top about
 *  what this does not do. */
export async function removePasskey(container, mainKeyBytes, credentialId) {
  const entries = structuredClone(container.prfKeys).filter((k) => k.credentialId?.$b64u !== b64u(credentialId));
  if (entries.length === container.prfKeys.length) throw new Error("this passkey is not in the key container");
  if (entries.length === 0) throw new Error("a container needs at least one passkey");
  return seal(await mainKeyFromBytes(mainKeyBytes), entries);
}

export const credentialIds = (container) => container.prfKeys.map((k) => unb64u(k.credentialId.$b64u));
