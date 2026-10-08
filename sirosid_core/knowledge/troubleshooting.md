---
title: "Troubleshooting - symptom, likely cause, how to check"
summary: "The common failures of an environment and its flows, each with the likely cause and how to check or fix it with the tools that exist."
digest: "Diagnose from get_instance (status, error) and the exact error text; never guess at logs you cannot see. Edge 503 = stopped; empty 502 = no such instance. \"no documents\" = wrong user or stale datastore. \"Issuer not trusted\" = identity mismatch or PDP still booting."
order: 130
tags: [troubleshooting, errors, creating, failed, trust, passkey, 502, 503, no-documents]
---
Start every diagnosis with `get_instance` (status, `error`, `expires_at`, `urls`) and the exact error
text the user sees. The assistant and MCP tools cannot read component logs; when the answer is only in a
log, say which component's log and what to look for.

## Instance status problems

**Stuck in `creating`.**
- Normal: creating takes several minutes; poll, do not assume.
- Longer: `trusted_issuers` names an issuer without a reachable JWKS endpoint - the PDP spends its whole
  discovery timeout (minutes) before it starts listening (see `trust`).
- Much longer than other instances, with no `error`: the deploy (or reset) job runs inside the
  control-plane process and is not resumed after a service restart, so the row can stay `creating` or
  `resetting` for good (read from the code: there is no recovery path). `destroy_instance` works in any
  status: destroy it and create again.
- Single-machine limit: a machine with too many distinct images/volumes (about 12 block devices) never
  boots and sits in Fly's `created` state; custom images can push an instance over it.

**`failed`.** Read `error` (up to 500 characters). Typical causes: an image that does not exist or does
not boot (`images`), a component whose health check never turns green, a Fly error. Options: `start_instance`
(retries bringing it up), `reset_instance` (wipe and restart), or destroy and recreate with a fixed config.
A failed destroy also shows as `failed` with `could not destroy: ...`; retry the destroy.

**`InvalidState` "cannot stop/start/reset an instance that is X".** The operation is not allowed in that
status (see the table in `lifecycle`), for example starting a running instance or resetting while
`creating`.

**"locked: your key session ended".** The user's key session expired (sessions are capped; an agent's
token lives at most 8 hours). Creating, resetting and reading configs or credentials need it. The user
must unlock again in the console (or reconnect the application). Listing, stop, start and destroy work
without it.

**Quota messages.** "you already have N instance(s); your limit is M", "the service is at capacity",
"your account has no allowance to keep instances", "your keep allowance has expired", "at most 50 saved
configs": see `limits-and-policy`; stop is not enough - only destroying frees a slot.

## URL answers

| what you see | meaning |
|---|---|
| a 503 page from the edge (after about 10 s) | the instance is stopped: `start_instance` |
| Fly's empty 502 after about 7 s | no such instance app: destroyed or expired |
| errors right after `running`, then fine | a component still warming up (unverified beyond the documented PDP boot) |

## Issuance

**"Issuer not trusted".** The offer's `credential_issuer` is not exactly in the PDP's issuer list: use the
apigw public URL, https, exact host (see `trust`). Right after create/start with `trusted_issuers` set,
the PDP may still be booting; wait and retry.

**"no documents".** The logged-in mini-oidc user has no document for that scope (see the table in
`issuing`), the presented PID does not belong to a user with one, or the datastore predates a fixture
change (documents are imported only into an empty datastore): `reset_instance` or a new instance.

**"Credential issuance failed".** A sanitized message from the wallet backend; the reason is in vc-apigw's
log. Known causes: proof/attestation format disagreements between a custom wallet-backend or vc image and
the other side; vc images too old for a configured scope. Back out custom images first.

**Wallet refuses the credential: "Issuer delivered a 'urn:...' credential, but this offer was for
.../type-metadata/<scope>".** The vct mismatch: use `credential_registries` /
the `siros-registry` template (see `registries`).

## Presentation

**"No credentials match DCQL query".** The request's format, vct/doctype or claim paths do not match what
the wallet holds (for example `vc+sd-jwt`, or an ARF 1.5 query against an ARF 1.8 PID). Issue the type the
request asks for, or use a matching template (see `presenting`).

**External verifier "not trusted".** Add the subject quoted in the denial to `trusted_verifiers` in its
normalized form (`https://<host>` for x509_san_dns and origins, `x509_hash:` verbatim); add the
reader-CA root if its chain ends in a self-signed root. An `x509_san_dns:` entry never matches.

## Wallet login and passkeys

**Sign-up fails, login with an existing passkey works ("Error validating origin", HTTP 400).** The app's
signing key is not in `android_apps` (see `mobile-testing`).

**Passkey prompt fails or offers no passkey.** Wrong host: wallet passkeys are bound to that instance's
wallet-frontend host. A new instance has a new host; a reset deleted the account behind the passkey.
Sign up again.

**Web wallet loses its session or calls fail with CORS errors.** The backend allows credentialed
requests only from the instance's own wallet-frontend origin; use the wallet at its own URL. The console
itself uses a host-only, SameSite=Strict cookie: sign in at the console URL directly, not through another
site.

**Native app cannot reach the backend.** Its backend URL must be the instance's wallet-proxy URL, and must
be updated after creating a new instance.

## Before blaming the platform

- Was a custom image involved? Re-test with the `standard` template.
- Is the instance the same one the user configured the app/partner with (same id)?
- Has it expired (`expires_at`) or been reset since the passkey was made?

Sources: `sirosid_service/service.py`, `sirosid_service/mcp.py`, `sirosid_service/oauth.py`, `sirosid_service/api.py`, `sirosid_core/lifecycle.py`, `sirosid_core/singlemachine.py`, `sirosid_core/render.py`, `sirosid_core/assets.py`, `environments/gdc.yaml`, `fixtures/vc-presentation-requests/eudi_pid.yaml`, `README.md`, `CLAUDE.md`
