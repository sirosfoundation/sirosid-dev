# SIROS verifier-core on Fly (gdc)

gdc runs `siros-verifier-core` as an **additional** verifier. The existing
`https://sirosid-gdc-vc-verifier.fly.dev` service and its nym_auth presets are
unchanged.

## Endpoint and UI

- UI: https://sirosid-gdc-siros-verifier.fly.dev
- Health: https://sirosid-gdc-siros-verifier.fly.dev/health
- OIDC discovery: https://sirosid-gdc-siros-verifier.fly.dev/OIDC/.well-known/openid-configuration

The UI loads SD-JWT and mdoc credential types and claim metadata from the TS11 catalog at
`https://registry.siros.org/api/v1/schemas.json`. If the first registry load
fails, the page displays a warning and offers only the configured presets. After
a successful load, a refresh failure may serve the in-memory catalog stale for
up to one hour after its original fetch; beyond that limit, the page falls back
to the configured presets and displays the warning. Select a registry-backed
preset, choose attributes from the multi-select dropdown, or edit the DCQL
directly. The dropdown exposes all claims in the registry metadata even when the
configured preset requests only a subset by default. Then choose an invocation
mechanism:

- **QR code**: scan with the SIROS SDK demo wallet. As with other SIROS QR
  presentations, the configured wallet backend retrieves the request and submits
  the response; it is not a device-side direct POST.
- **Direct link**: open the same authorization request using `openid4vp://`,
  `haip://`, `haip-vp://`, or `mdoc-openid4vp://`. The scheme changes invocation
  only, not the request profile. Pixel interoperability has not yet been
  demonstrated for these direct-link schemes.
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

## Shared flow and replay state

The verifier stores OpenID4VP flows and replay tombstones in the existing
MongoDB, in database `siros_verifier` and collection `presentation_flows`.
Startup checks connectivity and the retention index and fails if either is
unavailable. Atomic response claims and one-time result collection are designed
to allow multiple verifier replicas to share a flow. This is covered by real
MongoDB tests in core CI, but has not been demonstrated with two live gdc
machines. Sessions and verified results are sealed with the shared state key;
MongoDB does not contain the response
private key or verified attributes in plaintext. Retention is enforced both by
request-time checks and a TTL index.

The Fly deploy supplies the MongoDB URI as `SIROS_VERIFIER_MONGODB_URI`. The
current single MongoDB is a shared store but not a highly available database;
database failover requires a replica set or managed HA MongoDB. The upstream
OIDC frontend's authorization-code/refresh-token use store remains process-local,
so full multi-replica OIDC gateway HA is separate upstream work.

## Current limits

This is a test deployment of a service that is not production-ready. Protocol
tests cover SD-JWT VC and mdoc OpenID4VP, including mdoc doctype presets and both
QR/DC API handovers; `zkp_req` is not implemented. Pixel interoperability for
each credential profile must still be demonstrated separately. Keep gdc at one
verifier machine until the OIDC token-use store is shared; otherwise an
authorization code or refresh token could be redeemed once per replica.
