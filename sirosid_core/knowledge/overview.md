---
title: "What a SIROS ID Dev environment is"
summary: "What an environment (instance) contains, its public hostnames, how long it lives, and what in it is synthetic."
digest: "An environment (instance) is a disposable, complete SIROS ID stack - wallet, wallet backend, issuer, verifier, registry, trust service (PDP), test OIDC login - with public URLs <component>-<id>.<domain>. It expires after the platform TTL (3 days by default) unless kept. All users and data in it are synthetic."
order: 10
tags: [overview, instances, components, hostnames, urls, ttl]
---
## What you get

An **environment** (the tools call it an *instance*) is a complete, isolated copy of the SIROS ID wallet
stack, deployed on Fly.io from a **saved config** (see `config-reference`). Every instance has a random
8-character id (letters a-z and digits 2-7) and its own data, keys and certificates. Nothing is shared
with other instances.

The components, in deploy order:

| component | role | public? |
|---|---|---|
| `mongodb` | database for the wallet backend and the vc services | no |
| `mini-oidc` | minimal OpenID Provider standing in for a real government login; test users are picked from a list | yes |
| `vc-registry` | vc registry service: status lists and type metadata | yes |
| `vc-issuer` | issuer core: signs credentials; called only by vc-apigw over gRPC | no |
| `vc-verifier` | the verifier (OpenID4VP, presentation requests, DC API) | yes |
| `vc-apigw` | the issuer's public face: OpenID4VCI metadata, token and credential endpoints | yes |
| `pdp` | go-trust, the trust service (AuthZEN Policy Decision Point) | no |
| `wallet-backend` | go-wallet-backend: accounts, passkeys, credential storage, issuance/presentation engine | no (reached through wallet-proxy) |
| `wallet-proxy` | nginx in front of wallet-backend: the backend's public URL, serves `/.well-known/assetlinks.json` | yes |
| `env-admin` | the in-instance "Clear all data" actor; deployed only by the `make fly-up` CLI, never by the hosted service | no |
| `wallet-frontend` | the web wallet (served under `/id/default/`) and the environment dashboard (at `/`) | yes |

Four more components exist only with the OpenID conformance suite (`conformance: true`):
`conformance-mongodb`, `conformance-server`, `conformance` (its TLS front) and `conformance-runner`. The
hosted service refuses conformance (see `limits-and-policy`).

## Public hostnames

On the hosted service every instance is ONE Fly app (`sid-<id>`) with ONE multi-container machine
(the "single-machine layout"), reached through a shared edge. Public components answer at:

`https://<component>-<id>.<instance domain>`, for example `https://wallet-frontend-<id>.sirosid.dev`.

The six public names are `mini-oidc`, `vc-registry`, `vc-verifier`, `vc-apigw`, `wallet-proxy` and
`wallet-frontend`. `get_instance` / `list_instances` return them under `urls` once the instance is
`running`. Instances are siblings of the console (`console.sirosid.dev`), never under it, so instance
pages can never use the console's passkeys.

These URLs are load-bearing identities, not just addresses:
- the wallet's passkey RP ID is the **wallet-frontend** host;
- the issuer's identity (`credential_issuer`, the `iss` of issued credentials) is the **vc-apigw** URL;
- the verifier's OpenID4VP `client_id` is derived from the **vc-verifier** URL (`x509_san_dns:<host>`);
- the wallet backend's public URL (what a native app's "backend URL" setting needs) is **wallet-proxy**.

Because the id is part of every hostname, a new instance always has new URLs. Stop, start, reset and
keep never change them.

The `make fly-up` CLI (developers with Fly access) uses the older "apps" layout instead: one Fly app per
component, `https://sirosid-<env>-<component>.fly.dev`. Everything else in this knowledge base applies to
both unless it says otherwise.

## Lifetime

- A new instance expires `ttl_days` after creation (3 days by default; `get_account` reports the value).
  The reaper then destroys it with all its data.
- **Keep** an instance (`set_keep` or `keep: true` on create) to remove the expiry, if your account has a
  keep allowance. Releasing it puts it back on the normal clock from that moment.
- Each account has a limit on concurrent instances (2 by default) and the platform has a global cap
  (10 by default). See `limits-and-policy`.
- A **stopped** instance keeps its data and URLs and still counts against your limits and expiry.

## What is synthetic

Everything inside an instance is test material:
- the people: mini-oidc's users (alice-001, bob-002, carol-003, erik-010, maria-011, jan-012,
  sophie-013) are fictional; login is a choice from a list, with no password;
- the documents credentials are issued from (`fixtures/vc-bootstrapping`), with fictional names, companies
  and bank accounts;
- the PKI: every instance generates its own signing keys and certificates; nothing chains to a real
  trust anchor, so a production wallet that checks real trust lists will not trust these issuers;
- passkeys created in the web wallet belong to the instance's wallet-backend and are lost on reset or
  destroy.

Never put real personal data into an instance.

Sources: `sirosid_core/components.py`, `sirosid_core/naming.py`, `sirosid_core/singlemachine.py`, `sirosid_core/render.py`, `sirosid_core/deploy.py`, `sirosid_service/config.py`, `sirosid_service/service.py`, `deploy/control-plane/fly.toml`, `CLAUDE.md`, `README.md`, `../mini-oidc/README.md`
