# Candidate VC App Bugs — Conformance Testing

Found during OID4VCI issuer conformance testing against the OpenID Foundation
conformance suite (`registry.gitlab.com/openid/conformance-suite:latest`).

These are **VC app issues**, not test infrastructure problems.

---

## 1. Content Negotiation — nonce endpoint returns Go struct literal

**Endpoint:** `POST /nonce`
**Symptom:** When `Accept: text/plain, application/json, …` (text/plain listed first),
gin's content negotiation prefers `text/plain` and serializes the Go struct with
`fmt.Sprint`, producing:

```
&{lGP6Rjbi34B4mU_I_EzSsoV8xrg_Q9Pi0S5MqbOgi89}
```

instead of:

```json
{"c_nonce":"lGP6Rjbi34B4mU_I_EzSsoV8xrg_Q9Pi0S5MqbOgi89"}
```

**Impact:** Conformance suite receives non-JSON and crashes with
`MalformedJsonException: … line 1 column 3 path $`.
This blocks **all 20** non-metadata tests (both pre-auth and auth code variants).
Only the 2 metadata tests pass; 6 are skipped (signed-metadata, key-attestation,
encryption).

**Likely affected endpoints:** Any endpoint using the same `RegEndpoint` gin
content-negotiation wrapper (token, credential, etc.).

**Spec reference:** OID4VCI §12 — nonce endpoint MUST return `application/json`.

---

## 2. Nonce endpoint missing `Cache-Control: no-store`

**Endpoint:** `POST /nonce`
**Symptom:** Response does not include `Cache-Control: no-store`.
**Spec reference:** OID4VCI §12.2.2 — nonce endpoint response MUST include
`Cache-Control: no-store`.
**Conformance check:** `VCICheckNonceCacheControlHeader` — FAILS without header.

**Note:** Currently worked around via nginx `proxy_hide_header` + `add_header`
in vc-proxy.conf. This is a legitimate reverse proxy config but masks the
underlying app bug.

---

## 3. `tx_code` serialized with empty fields (no `omitempty`)

**Endpoint:** Credential offer (pre-authorized code grant)
**Symptom:** Go serializes `GrantPreAuthorizedCode.TXCode` as:

```json
{"tx_code": {"input_mode": "", "length": 0, "description": ""}}
```

even when no tx_code is configured. The spec says `tx_code` SHOULD be omitted
when not used.

**Impact:** Conformance suite interprets presence of `tx_code` as requiring
user PIN entry, adding an extra WAITING cycle. Not a hard failure but
complicates the test flow.

---

## 4. Token endpoint includes `Location: /` header

**Endpoint:** `POST /token`
**Symptom:** Successful (200) token response includes `Location: /` header.
**Impact:** Unclear — may confuse HTTP clients that follow redirects on any
response with a Location header. Observed in conformance suite logs.

---

## 5. Credential endpoint rejects its own credential_identifier

**Endpoint:** `POST /credential`
**Symptom:** Token response includes `authorization_details` with a
`credential_identifiers` array (e.g. `["9bdbe49a-..."]`). When the conformance
suite sends a credential request with that same identifier, the server
responds 404:

```json
{"error":{"title":"internal_server_error","details":"credential_identifier \"9bdbe49a-...\" not found in Token Response authorization_details"}}
```

**Impact:** Blocks all happy-flow and error-handling tests that proceed past
the token exchange. This is now the primary blocker after bug #1 is fixed.

**Status:** PR #438 fixes bug #1. With that fix applied, this bug surfaces.

---

*Last updated: 2026-06-06*
