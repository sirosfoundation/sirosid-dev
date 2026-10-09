---
title: "Credential-type registries - vendored vs external"
summary: "Where an instance's credential-type metadata (VCTM / MDDL) comes from, what credential_registries changes, and the vct-mismatch gotcha."
digest: "Without credential_registries the issuer uses vendored VCTMs and advertises its own /type-metadata/<scope> URL while the credential carries the real urn vct - wallets that compare the two (SIROS SDKs, wallet-frontend) refuse it. Set credential_registries to https://registry.siros.org (siros-registry template) to advertise the real vct. mdoc types always stay vendored."
order: 100
tags: [registries, vctm, mddl, vct, metadata, registry.siros.org, mismatch]
---
## Two components need the same document

Each credential type has a metadata document: a **VCTM** (SD-JWT VC type metadata) or an **MDDL** schema
(mdoc). Two parts of an instance need it independently:
- **vc** (issuer and verifier): the issuer builds the credential from it and the verifier derives its
  DCQL queries from it;
- **go-wallet-backend**: its registry serves it to the wallet, which renders the credential and matches
  DCQL queries against it.

When they disagree **nothing errors**: the wallet just ends up holding a credential it cannot present.
That is why one config key sets both.

## Vendored (the default)

With no `credential_registries`, every type's document comes from the copies shipped with the platform
(`fixtures/vc-metadata`, declared in `values-base.yaml`'s `features.credentialTypes`). The issuer then
advertises, for an SD-JWT type, its own `/type-metadata/<scope>` URL, while the credential it issues
carries the VCTM's real `vct` (for example `urn:eudi:pid:arf-1.8:1`).

## External (credential_registries set)

```json
{"schema_version": 1, "credential_registries": ["https://registry.siros.org"]}
```

The vendored documents are dropped and each SD-JWT scope is resolved **by its vct** from the listed
registries, for vc and the wallet backend in one render. The issuer then advertises the real `vct`.
Rules:
- entries must be public https base URLs; at most 50;
- order matters: a later registry overrides an earlier one for the same type;
- the registry's legacy `.well-known/vctm-registry.json` index is used, not its newer TS11
  `/api/v1/schemas.json` (which lists fewer types: 26 against 37 on registry.siros.org when last
  counted);
- **mdoc types stay vendored** even in external mode: mdocs have no `vct` to mismatch, and a local
  variant such as `mdl_zk4` shares its doctype with the full mDL, so resolving it from a registry would
  silently turn it into the full one;
- every SD-JWT type the platform issues must exist in the registry under its vct, or that type's
  metadata cannot be resolved.

## The vct mismatch gotcha

Symptom (wallet error, wording from the SIROS SDK / wallet-frontend): *"Issuer delivered a
'urn:eudi:pid:arf-1.8:1' credential, but this offer was for https://.../type-metadata/pid_1_8"*.

Cause: a vendored instance advertises the type-metadata URL while the credential carries the urn; wallets
that check the advertised type against the issued one refuse it.

Fix: use the `siros-registry` (or `interop`) template - `credential_registries:
["https://registry.siros.org"]` - in a new instance. Existing instances keep their metadata source.

## When to stay vendored

- You are testing a credential type that is not (yet) in a registry.
- You need the exact vendored claim set (the vendored VCTMs are what the synthetic documents were
  written against; every claim in a document must be declared by its type's metadata or it is dropped
  or rejected).

Sources: `CLAUDE.md`, `sirosid_core/vc_render.py`, `sirosid_core/render.py`, `sirosid_core/templates.py`, `sirosid_core/policy.py`, `sirosid_core/spec.py`, `values-base.yaml`, `environments/gdc.yaml`, `README.md`
