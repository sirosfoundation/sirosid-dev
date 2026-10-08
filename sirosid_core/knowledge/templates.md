---
title: "Config templates - which one to start from"
summary: "The starting-point configs offered by list_config_templates, what each one turns on, and when to use which."
digest: "Start from list_config_templates, never a blank page - standard (default stack), siros-registry (real vct values from registry.siros.org), wallet-attestation, dc-api, interop (registry + attestation + DC API), custom-wallet-backend (needs custom_images). A template is an ordinary config - copy, edit, validate, save."
order: 50
tags: [templates, config, get-started, interop]
---
`list_config_templates` returns the templates the caller's capabilities allow, each with `id`, `title`,
`description`, `config`, `hints` and `requires`. A template is nothing special: it is a saved config plus
words. To use one: take its `config`, change what you need, `validate_config`, `save_config` under a
name, then `create_instance` with that `config_name` (or pass the config inline to `create_instance`).
Every template is checked by the test suite against the policy, so the config as shipped is always
valid for a user holding its `requires`.

## The templates

### `standard` - Standard test stack

`{"schema_version": 1}`. The whole stack at the current released versions, with the credential type
definitions shipped with the platform. Use it when you just need a working wallet, issuer and verifier.
Hints: add `android_apps` to use passkeys from an Android app; add `trusted_issuers` /
`trusted_verifiers` to accept a partner's issuer or verifier.

### `siros-registry` - Credential types from the SIROS registry

Adds `credential_registries: ["https://registry.siros.org"]`. Every SD-JWT type is resolved from the
registry, so the issuer advertises the type's real `vct` (for example `urn:eudi:pid:arf-1.8:1`). Use it
when testing a wallet that checks the advertised type against the issued credential (the SIROS SDKs and
wallet-frontend do), or to test against production-like metadata. mdoc types stay on the platform's own
definitions.

### `wallet-attestation` - Wallet attestation

Adds `wallet_attestation: true`: the wallet backend authenticates to the issuer with a wallet instance
attestation (WIA) instead of a pre-registered client. Use it to test a wallet app that proves what it is
before it is issued credentials. Newer and less validated than the standard stack: expect to read the
wallet-backend and issuer logs when something does not line up. See `attestation`.

### `dc-api` - W3C Digital Credentials API on

Adds `dc_api_enable: "true"`: the verifier explicitly offers the browser Digital Credentials API. Use it
to test presentation from a browser or a platform wallet. Set `"false"` to test the fallback without it.

### `interop` - Interop: registry, attestation and Digital Credentials API

All three optional features together (`credential_registries`, `wallet_attestation`, `dc_api_enable`).
The base for interoperability events and for checking a partner's wallet or verifier against the most
demanding configuration. Add the partner's issuer or verifier to `trusted_issuers` /
`trusted_verifiers` before testing against them.

### `custom-wallet-backend` - Run my own wallet backend build

The standard stack with `images: {"wallet-backend": <the version the platform currently deploys>}`.
**Requires the `custom_images` capability**; users without it do not see this template, and saving an
`images` key without it is refused. Change the tag (or the whole reference) to your own build in a public
registry; other components can be replaced the same way. The platform does not check that the image
boots. See `custom-images`.

## Choosing

| goal | template |
|---|---|
| first contact, demo, "does it work" | `standard` |
| your wallet refuses a credential with a vct mismatch | `siros-registry` |
| WIA / OAuth-Client-Attestation testing | `wallet-attestation` |
| browser presentation (DC API, digital-credentials.dev) | `dc-api` |
| plugfest or partner interop | `interop`, plus `trusted_issuers` / `trusted_verifiers` |
| your own wallet-backend build | `custom-wallet-backend` |

Templates do not set `trusted_*`, `android_apps` or `values`; add those yourself.

Sources: `sirosid_core/templates.py`, `sirosid_service/service.py`, `sirosid_service/mcp.py`, `environments/gdc.yaml`, `CLAUDE.md`
