# chart/

The config chart sirosid-dev renders every service's configuration from
(`scripts/render-helm-config.py`, `helm template`; only ConfigMaps are consumed).

**Provenance.** Forked 2026-09-30 from sirosfoundation/siros-id-stack:
`fix/apigw-advertise-mdoc-iacas-uri` (= main at PR #3 "render every service
config through one mergeable template" + `mdoc_iacas_uri`) merged with
`feat/issuer-bbs`. Upstream reverted PR #3 on 2026-09-29 and moved to
`configOverrides`, so the two have diverged on purpose.

**Ownership.** This chart is maintained here and is meant to be *more* generic
than upstream: every knob sirosid-dev needs should be a value, not a
post-render patch. Escape hatch: each service's `extraConfig` (rooted at the
WHOLE config file; lists replace). Keep `make vc-config-parity` green.

**Pruned.** Only ConfigMap documents and the helpers they use remain (no Deployments, Certificates, HTTPRoutes, NetworkPolicies, ...). `06-images.yaml` exposes the merged `images` values so fly-up reads image refs from it.

**Registry layout.** `walletBackend.registryConfigLayout: legacy|integrated` (default legacy) gates the go-wallet-backend#431 layout: `registry:` block inside backend.yaml, no registry.yaml, no `--registry-config`. Fly supports both; compose refuses integrated until its overlay exists. Switch only after bumping `images.walletBackend` to a release containing #431.

**Porting from upstream.** Compare by hand (`git diff` against a fresh clone of
siros-id-stack) and port what is worth having. Not ported yet: WIA mode toggles,
per-type display metadata, presentation-request templates, EU Business Wallet
demo types, `configOverrides`, image bumps to vc 0.7.20.
