# SIROS verifier-core on Fly (gdc)

gdc runs `siros-verifier-core` as an **additional** verifier. The existing
`https://sirosid-gdc-vc-verifier.fly.dev` service and its nym_auth presets are
unchanged.

## Endpoint and UI

- UI: https://sirosid-gdc-siros-verifier.fly.dev
- Health: https://sirosid-gdc-siros-verifier.fly.dev/health
- OIDC discovery: https://sirosid-gdc-siros-verifier.fly.dev/OIDC/.well-known/openid-configuration

The UI loads SD-JWT credential types and claim metadata from the TS11 catalog at
`https://registry.siros.org/api/v1/schemas.json` (with configured presets as a
fallback). Select a registry-backed preset, choose attributes, or edit the DCQL
directly, then choose an invocation mechanism:

- **QR code**: scan with the SIROS SDK demo wallet. As with other SIROS QR
  presentations, the configured wallet backend retrieves the request and submits
  the response; it is not a device-side direct POST.
- **Direct link**: open the same authorization request using `openid4vp://`,
  `haip://`, `haip-vp://`, or `mdoc-openid4vp://`. The scheme changes invocation
  only, not the request profile.
- **DC API**: use Chrome on the Pixel with the demo wallet installed, registered,
  and unlocked. The browser immediately sends an `openid4vp-v1-signed` request
  and submits the wallet's encrypted `dc_api.jwt` response—there is no secondary
  launch page. Open and unlock the wallet in its current process first.

The page is a thin consumer of the independently publishable
`@sirosfoundation/presentation-flow` ES module. It displays a SIROS spinner while
waiting and resolves to the verified-claims result page. Backend start/status
URLs are origin-bound, and the library supports tenant metadata and pluggable DC
API adapters. Verifier-initiation authentication is out of scope for this test
round.

A successful flow displays the verified claims. For this synthetic test flow the
OIDC subject is the PID's `personal_administrative_number`; do not use that as a
production subject-privacy design. The app reuses gdc's existing issuer,
registry, wallet backend, MongoDB, and go-trust PDP; it does not deploy
duplicates.

## Image and deploy

Build from the sibling `siros-verifier-core` workspace:

```sh
cd ../siros-verifier-core
docker build -t registry.fly.io/sirosid-gdc-siros-verifier:local-$(date +%s) .
flyctl auth docker
docker push registry.fly.io/sirosid-gdc-siros-verifier:local-<epoch>
```

Pin the resulting fully qualified image and digest in `environments/gdc.yaml`.
Then deploy normally:

```sh
cd ../sirosid-dev
make fly-up ENV=gdc
```

There is no shared default image. Setting `images.siros-verifier` is the explicit
opt-in for an environment; without it, the app and its configuration are absent.
The app is supported in the apps layout, not the single-machine layout.

## Trust and registration

The service currently publishes an `x509_hash` OpenID4VP client identity. Deploy
computes the base64url SHA-256 digest of the current gdc signing leaf and adds it
to the existing PDP whitelist. Do not hard-code that hash: reissuing the leaf
changes it. The existing vc-verifier URL remains trusted independently.

Deploy registers both verifier URLs with the wallet backend. gdc's env-admin
0.1.2 image restores both registrations after a storage reset. The state-cookie
key is persistent across redeploys; the request-signing key and chain are the
same ones used by gdc's existing VC services.

Reusing the issuer's credential-signing key for OIDC and OpenID4VP is a
conscious simplification for this isolated test environment, not the intended
production key-separation design. A production deployment should issue a
dedicated verifier key and certificate from the same CA. Rotating the shared
leaf also rotates the verifier's `x509_hash` identity, so the deploy updates the
app and whitelist together.

## Current limits

This is a test deployment of a service that is not production-ready. It supports
the SD-JWT VC gateway flow, not mdoc OpenID4VP or `zkp_req`. Flow and replay state
are in-process: run one machine and do not treat this as the HA implementation.
