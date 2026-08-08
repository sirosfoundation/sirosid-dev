#!/usr/bin/env node
/**
 * Generate an IETF Token Status List fixture for exercising DIIP v5 revocation locally.
 *
 * DIIP v5 mandates the IETF Token Status List as the revocation mechanism. The dev stack has no
 * issuer that publishes one, so this produces a signed Status List Token you can serve as a
 * static file and point a credential's `status.status_list.uri` at.
 *
 * Usage:
 *   node scripts/make-status-list.mjs --uri https://issuer.local/statuslist/1 \
 *                                     --issuer https://issuer.local \
 *                                     --revoke 3,7 --suspend 5 --size 256 --out fixtures/status-list
 *
 * Writes:
 *   <out>/statuslist.jwt   the Status List Token (serve as application/statuslist+jwt)
 *   <out>/public-jwk.json  the public key, for the verifier/issuer metadata
 *   <out>/private-jwk.json the private key, so the same list can be regenerated
 *
 * @see https://datatracker.ietf.org/doc/draft-ietf-oauth-status-list/15/
 */

import { mkdir, readFile, writeFile } from "node:fs/promises";
import { deflateSync } from "node:zlib";
import { webcrypto } from "node:crypto";

const STATUS = { VALID: 0x00, INVALID: 0x01, SUSPENDED: 0x02 };

const ES256 = { name: "ECDSA", namedCurve: "P-256" };
const ES256_SIGN = { name: "ECDSA", hash: "SHA-256" };

function parseArgs(argv) {
	const args = {};
	for (let i = 0; i < argv.length; i += 2) {
		if (!argv[i].startsWith("--")) {
			throw new Error(`Unexpected argument: ${argv[i]}`);
		}
		args[argv[i].slice(2)] = argv[i + 1];
	}
	return args;
}

const parseIndexList = (value) =>
	(value ?? "").split(",").map((s) => s.trim()).filter(Boolean).map((s) => {
		const n = Number(s);
		if (!Number.isInteger(n) || n < 0) {
			throw new Error(`Not a valid status list index: ${s}`);
		}
		return n;
	});

const toBase64Url = (bytes) => Buffer.from(bytes).toString("base64url");
const encodeJson = (value) => Buffer.from(JSON.stringify(value)).toString("base64url");

/** Sign a compact JWS with ES256, so this script needs no dependencies outside Node. */
async function signCompactJws(header, payload, privateKey) {
	const signingInput = `${encodeJson(header)}.${encodeJson(payload)}`;
	const signature = await webcrypto.subtle.sign(ES256_SIGN, privateKey, Buffer.from(signingInput));
	return `${signingInput}.${toBase64Url(new Uint8Array(signature))}`;
}

const args = parseArgs(process.argv.slice(2));
const uri = args.uri ?? "http://localhost:8080/statuslist/1";
const issuer = args.issuer ?? new URL(uri).origin;
const out = args.out ?? "fixtures/status-list";
const size = Number(args.size ?? 256);
const ttl = Number(args.ttl ?? 300);
// 2 bits per entry so SUSPENDED is representable alongside VALID and INVALID.
const bits = 2;

const revoked = parseIndexList(args.revoke);
const suspended = parseIndexList(args.suspend);

const entriesPerByte = 8 / bits;
const list = new Uint8Array(Math.ceil(size / entriesPerByte));
const setStatus = (idx, status) => {
	if (idx >= size) {
		throw new Error(`Index ${idx} is outside a list of ${size} entries; raise --size`);
	}
	list[Math.floor(idx / entriesPerByte)] |= (status & 0b11) << ((idx % entriesPerByte) * bits);
};
revoked.forEach((idx) => setStatus(idx, STATUS.INVALID));
suspended.forEach((idx) => setStatus(idx, STATUS.SUSPENDED));

// Reuse the key when one is already present, so regenerating a list does not invalidate
// credentials that were issued against the previous public key.
const privateJwkPath = `${out}/private-jwk.json`;
const { privateKey, privateJwk } = await (async () => {
	try {
		const jwk = JSON.parse(await readFile(privateJwkPath, "utf8"));
		return {
			privateKey: await webcrypto.subtle.importKey("jwk", jwk, ES256, true, ["sign"]),
			privateJwk: jwk,
		};
	}
	catch {
		const pair = await webcrypto.subtle.generateKey(ES256, true, ["sign", "verify"]);
		return {
			privateKey: pair.privateKey,
			privateJwk: await webcrypto.subtle.exportKey("jwk", pair.privateKey),
		};
	}
})();

const { d, key_ops, ext, ...publicJwk } = privateJwk;

const token = await signCompactJws(
	{ alg: "ES256", typ: "statuslist+jwt" },
	{
		iss: issuer,
		sub: uri,
		iat: Math.floor(Date.now() / 1000),
		ttl,
		status_list: { bits, lst: toBase64Url(deflateSync(Buffer.from(list))) },
	},
	privateKey,
);

await mkdir(out, { recursive: true });
await writeFile(`${out}/statuslist.jwt`, token);
await writeFile(`${out}/public-jwk.json`, JSON.stringify(publicJwk, null, 2));
await writeFile(privateJwkPath, JSON.stringify(privateJwk, null, 2));

console.log(`Status List Token written to ${out}/statuslist.jwt`);
console.log(`  issuer:    ${issuer}`);
console.log(`  uri (sub): ${uri}`);
console.log(`  entries:   ${size} at ${bits} bits`);
console.log(`  revoked:   ${revoked.length ? revoked.join(", ") : "none"}`);
console.log(`  suspended: ${suspended.length ? suspended.join(", ") : "none"}`);
console.log("");
console.log("Reference it from a credential with:");
console.log(JSON.stringify({ status: { status_list: { idx: revoked[0] ?? 0, uri } } }, null, 2));
console.log("");
console.log(`Serve it with: Content-Type: application/statuslist+jwt`);
