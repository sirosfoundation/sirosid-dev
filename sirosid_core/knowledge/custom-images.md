---
title: "Custom images - running your own builds"
summary: "How to replace a component's image with your own build, who may, how to name the image, and what can go wrong."
digest: "images maps component name -> fully qualified image (registry/repo:tag or @sha256:...), needs the custom_images capability, and registry.fly.io, localhost and private registries are refused. The platform does not check that it boots. The four vc services share one config shape - bump vc-registry, vc-issuer, vc-verifier and vc-apigw together."
order: 120
tags: [images, custom-images, capability, build, wallet-backend, vc, versions]
---
## Who may

Choosing an image is choosing code that runs in the platform's Fly org, so `images` needs the
**`custom_images`** capability, granted per account by an admin. Without it a non-empty `images` is
refused at save time and again at create time (capabilities are re-checked then). `get_account` shows
your capabilities. The `custom-wallet-backend` template is only listed for accounts that hold it.

## To run your own build

1. Push the image to a **public** registry (for example `ghcr.io/<you>/<image>:<tag>`).
2. Start from the `custom-wallet-backend` template (it begins at the version the platform deploys) or
   add an `images` object to any config:

```json
{"schema_version": 1, "images": {"wallet-backend": "ghcr.io/sirosfoundation/go-wallet-backend:0.22.3", "pdp": "ghcr.io/sirosfoundation/go-trust:0.24.1"}}
```

3. `validate_config`, `save_config`, `create_instance`, then wait for `running` or `failed`.

## Naming rules (enforced)

- Keys are component names: `mongodb`, `mini-oidc`, `vc-registry`, `vc-issuer`, `vc-verifier`,
  `vc-apigw`, `pdp`, `wallet-backend`, `wallet-proxy`, `env-admin`, `wallet-frontend` (and the
  conformance components). Not chart keys such as `walletBackend`.
- A reference must name its registry host (with a dot or a port): a bare `image:tag` is refused, because
  it would be looked up in a local docker daemon.
- It needs a tag or a `@sha256:<64 hex>` digest; at most 300 characters; repository path in lower case.
- Refused registries: `registry.fly.io` (other apps' images), `localhost`, hosts ending in an internal
  suffix, private IP addresses, and anything outside the operator's allow-list if one is set.
- GHCR tags never carry the `v` of the git tag: git tag `v0.7.0-sirosid.0` is image tag
  `0.7.0-sirosid.0`.

## What can go wrong

- **The platform does not check that the image exists or boots.** A wrong tag or a crashing image ends in
  `failed` (with the error) or a component that never turns healthy.
- **Config shape drift.** Every component's config is rendered by the platform for the versions it
  pins. A much newer or older image can reject that config and crash-loop: for example the four vc
  services (`vc-registry`, `vc-issuer`, `vc-verifier`, `vc-apigw`) share one config model, so bump them
  **together**; a vc image older than mdoc-schema support crash-loops on the `mdl` scope; a
  go-wallet-backend at or after v0.10.0 refuses WIA config without a wallet version (the platform handles
  this for its own pins).
- **Feature builds.** Some features need special builds (for example a verifier built without its ZK
  libraries dies at start with a shared-library error; a stock issuer has no BBS support). A default image
  deploys cleanly and then fails only when the feature is used.
- **Single-machine layout.** On the hosted service `wallet-proxy` and `env-admin` are not separate
  containers (wallet-proxy is served by the front nginx; env-admin is not deployed), so overriding them
  has no useful effect (unverified whether it is refused).
- **Versions vs channels.** To pick another released set of versions without naming images, use a
  `channel` if the operator offers one (`get_config_schema`, `validate_config` lists valid ones).

Sources: `sirosid_core/policy.py`, `sirosid_core/templates.py`, `sirosid_core/deploy.py`, `sirosid_core/singlemachine.py`, `sirosid_core/components.py`, `sirosid_service/service.py`, `environments/gdc.yaml`, `environments/bbs.yaml`, `values-fly.yaml`, `CLAUDE.md`
