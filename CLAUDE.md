# sirosid-dev — instructions for Claude Code

This repo is a **harness, not a service**: it orchestrates sibling repos
(go-wallet-backend, go-trust, vc, wallet-frontend, wallet-common, siros-id-stack)
via a large self-documenting `Makefile`, docker-compose files, and Python
scripts under `scripts/`. It has no application code of its own. Run
`make help` for the authoritative, current list of targets/flags — this file
only covers what `make help` and `README.md` don't: the non-obvious traps.

## Sibling repo layout

`make setup` clones (or, for `siros-id-stack`, fast-forwards) these into `../`:

| repo | branch | notes |
|---|---|---|
| `wallet-frontend` | `release/sirosid` | |
| `wallet-common` | `release/sirosid` | shared TS types |
| `go-wallet-backend` | `main` | |
| `go-trust` | `main` | trust PDP |
| `vc` | `main` | SUNET/vc issuer/verifier/apigw/registry — only needed for `VC=yes` |
| `facetec-api` | `main` | only needed for `FACETEC=yes` |
| `siros-id-stack` | `main` | **read-only, public config-rendering source** — the public [SIROS ID Stack Helm chart](https://github.com/sirosfoundation/siros-id-stack), not branched for feature work. `make setup` auto-`git pull --ff-only`s it if it's on `main`; if checked out to something else (e.g. a PR branch someone's deliberately testing), it's left alone with a warning. |

`make update` force-hard-resets every repo above (except siros-id-stack) to its
default branch — destructive, only run it when you actually want to discard
local changes in the sibling checkouts.

## Two deployment targets — when to use which

- **Local docker-compose (`make up ...`)** — fast iteration, builds from local
  source in the sibling repos (or `REBUILD=yes` to force a no-cache rebuild).
  Use this for day-to-day development and for anything `sirosid-tests` runs
  against `localhost`.
- **Named Fly.io environments (`make fly-up ENV=<name>` / `fly-down` /
  `fly-status`)** — a full, independently-addressable, shareable stack at
  `sirosid-<env>-*.fly.dev` URLs under the `sirosfoundation` Fly org, region
  `arn`. Images are pulled straight from `siros-id-stack`'s
  `values.yaml` (layered with `values-fly.yaml`) — **no local Docker build** by
  default. Use this for: handing a URL to someone else, native
  Android/iOS app testing (real assetlinks/AASA over real TLS), OIDC-backed
  issuance (mini-oidc needs to be reachable from a real browser redirect, not
  just a container network), or running several isolated environments at once
  (`ENV=alice`, `ENV=bob`, ... — fully isolated, verified with concurrent
  deploys, each env gets its own Fly `--network` segment so apps in one
  environment can't resolve another's `.internal` addresses).

Both paths share one underlying mechanism: `scripts/render-helm-config.py`
renders wallet-backend/PDP config from the same `siros-id-stack` chart, just
with different hostname targets (`--target fly` uses `.internal`/`.fly.dev`,
the local `PDP=helm` path uses compose service names). This is why `PDP=helm`
locally and any `fly-up` deploy are the two contexts where a stale/wrong
`../siros-id-stack` checkout will silently produce wrong config — always
check `git -C ../siros-id-stack branch --show-current` if PDP behavior looks
off in either mode.

### `make up` — key flags (see `make help` for the full, current list)

- `PDP=allow|whitelist|deny|mock|helm` — trust policy provider. `helm` renders
  wallet-backend + PDP config from the chart (see above); the other modes are
  hand-maintained env vars/CLI flags, kept independently and known to drift
  from the chart (that's the whole reason `PDP=helm` exists — see
  "Helm alignment" below).
- `VC=yes` — adds production-like issuer/verifier/apigw/registry + mongodb,
  built from `../vc`.
- `TRANSPORT=websocket|wmp|http` — wallet transport; `http` is deprecated.
- `CONFORMANCE=yes` — layers in VC services + VC↔go-trust wiring
  (`docker-compose.vc-go-trust.yml`) and the OpenID conformance suite overlay,
  on top of whatever `PDP=` you set (default stays `allow` since that's the
  Makefile default, not something CONFORMANCE forces).
- `R2PS=yes` — adds go-r2ps-service + SoftHSM2 (remote WSCD/WSCA signing).
- `DOMAIN=<host>` / `TUNNELS=yes` — mutually exclusive; both exist for mobile
  device testing (custom LAN hostname vs. real-TLS Cloudflare quick tunnels).
- `GOLDEN=yes|<release>` — pre-built images from `siros-conformance`'s
  `golden-releases.yaml` instead of local builds (see below). VC services
  still build from source even with `GOLDEN=yes VC=yes` — `fixtures/vc-config.yaml`
  evolves between releases, so golden VC images aren't config-compatible yet
  (`docker-compose.golden-vc.yml` exists but is deliberately unused, per an
  explicit comment in the Makefile — don't "fix" this without checking
  config/image version alignment first).
- `ANDROID_APPS=pkg=fingerprint,...` — extra Android package/signing-key
  pairs to trust, on top of the gitignored `.android-apps` file (copy from
  `.android-apps.example`). Honored identically by `make up` (any PDP mode)
  **and** `make fly-up` — same underlying `scripts/android_apps.py`, one
  source of truth, no drift between local and Fly.
- `FACETEC=yes` — implies `VC=yes`; requires `FACETEC_SERVER_URL` exported in
  your shell (a live credential, never committed).

### `make fly-up ENV=<name>` — key flags

- `IMAGES=component=ref,...` — ad-hoc, per-environment image override (e.g.
  your own branch build). A bare unqualified tag (no `/`) that's already in
  your local Docker daemon (what `make up REBUILD=yes` produces) gets
  auto-pushed into that Fly app's own `registry.fly.io` namespace for you —
  no manual `docker tag`/`push`/`flyctl auth docker` needed. A fully-qualified
  ref (has a `/`) is always passed through untouched even if it's also
  cached locally.
- `ANDROID_APPS=` — same as local (see above).
- `CONFORMANCE=yes` — deploys 3 extra apps (`conformance-mongodb`,
  `conformance-server`, `conformance` nginx front) after the core 10.
- `TRUSTED_ISSUERS=url,...` — ad-hoc external issuers for interop testing
  (e.g. a third-party mdoc issuer). **Only ever added to `pid_issuers`, never
  to `verifiers`** — there is no `TRUSTED_VERIFIERS` equivalent today. See the
  PDP gotcha below before assuming this covers "trust as a verifier" too.

## Why `values-fly.yaml` overrides exist (don't remove without checking)

`siros-id-stack/values.yaml`'s own image pins lag behind what this
repo needs; `values-fly.yaml`'s `images:` block patches specific components:

- **`images.pdp`** — pinned to a specific `go-trust` tag ahead of the chart's
  own (pre-release, commit-sha) default, because the chart's default predates
  the `AllowHTTP` fix ([go-trust#112](https://github.com/sirosfoundation/go-trust/pull/112))
  the PDP's config-file whitelist needs to become healthy *at all* on Fly —
  without it, JWKS fetch fails for every whitelisted entity. Explicitly
  marked in the file as a stopgap to delete once the chart's own pin catches
  up — don't add more permanent special-casing here if you hit a similar
  lag elsewhere; fix it the same documented, delete-when-fixed way.
- **`images.walletBackend`** — pinned ahead of the chart's default, for
  Key Attestation / wallet-provider work this repo exercises ahead of the
  chart's own release cadence.
- **`images.issuerRegistry/issuerApigw/issuerCore/verifier`** (the `vc.*`
  services) — pinned ahead of the chart's default for the same reason
  (mdoc/BLE proximity, DC API, MDDLSchema fixes). Check the current pinned
  tag in `values-fly.yaml` before assuming a `fly-up` will pick up recent
  `vc` work — it deploys whatever tag is pinned, not `../vc`'s local HEAD,
  unless you pass `IMAGES=vc-apigw=...,...` explicitly.

None of these overrides are meant to be permanent — each one's comment names
the exact condition under which it should be deleted. If you're bumping one
of these, check whether the chart itself has since caught up first.

## Known-good end-to-end smoke test (validating an environment/image bump)

The following round trip is a useful reference recipe for confirming a fresh
named Fly environment — or a bump to the standard image pins above — actually
works end-to-end, beyond individual components' own health checks:

1. Point a real client at the environment's `wallet-proxy`/`wallet-frontend`
   public URL and sign up via passkey — e.g. siros-sdk-kotlin's `sample-app`
   (package `org.siros.sdk.sample`, a separate SDK test app, not
   `wallet-frontend` itself) has a runtime-configurable `backend_url` in its
   Settings, no rebuild needed to point it at a new environment.
2. Request and receive a real mDL credential via OpenID4VCI using
   OAuth-Client-Attestation — confirm in wallet-backend's own logs
   (`flyctl logs -a sirosid-<env>-wallet-backend`) for lines like `"using
   OAuth-Client-Attestation authentication (client-signed PoP)"` and
   `"Server-side issuer trust evaluation" ... "trusted":true`.
3. Present that mDL via Google's public `digital-credentials.dev` DC API
   conformance test site — a stricter, independent isomdoc-based verifier
   that catches COSE/mdoc conformance issues this stack's own verifier
   doesn't.
4. Present the same mDL via real BLE proximity (`siros-verifier-cli`'s
   `siros-verify read --mode peripheral`), confirming `deviceSignature
   VALID`.

All four succeeding together confirms issuance, DC API presentation, and BLE
proximity presentation all interoperate correctly against the currently
pinned images — worth re-running whenever bumping `images.walletBackend` or
the `images.issuer*`/`verifier` pins above, not just checking that each
component's own health check goes green.

## Golden release mechanism

`GOLDEN=<name>` fetches `golden-releases.yaml` from
`sirosfoundation/siros-conformance` (`fetch-golden-env` target), parses it
with the `GOLDEN_AWK` script embedded in the Makefile, and writes
`.env.golden` with resolved `ghcr.io/sirosfoundation/*` image refs consumed by
`docker-compose.golden*.yml`. `GOLDEN=yes` resolves to whatever
`golden-releases.yaml` names as its `default:`; anything else is treated as a
named release.

## Gotchas — symptom → likely cause → how to check

**A `values-fly.yaml` image pin looks unpublished/inaccessible (404, no
access) even though the release actually shipped:** GHCR image tags never
carry the `v` prefix that the corresponding git tag does — git tag
`v0.7.0-sirosid.0` publishes as image tag `0.7.0-sirosid.0`. Checking the
`v`-prefixed form first (the natural thing to try) will look like the image
doesn't exist. `docker manifest inspect
ghcr.io/sirosfoundation/<image>:<tag>` is the reliable way to confirm what's
actually published before pinning it; `gh api .../packages/.../versions`
needs a `read:packages` token scope a default `gh auth login` session may not
have.

**wallet-backend crash-loops with `Failed to load backend configuration`
after bumping `images.walletBackend` past go-wallet-backend v0.10.0, if
the chart's wallet-backend template sets `wallet_provider.wia.enabled:
true`:** v0.10.0 added a hard startup-time validation
(`pkg/config/config.go`'s `WIAConfig.WalletVersion` check) requiring
`wallet_provider.wia.wallet_version` whenever `wallet_provider.wia.mode`
defaults to `"etsi"` (EC TS03 v1.5.2 §2.3.1 made it a mandatory WIA claim,
with no sensible built-in default per that field's own comment) — `wia.
enabled: true` with no `wallet_version` alongside it fails config validation
and the backend never comes up. As things stand, `siros-id-stack`'s `main`
branch doesn't render a `wallet_provider.wia` block in
`templates/04-wallet-backend.yaml` at all, so this isn't a live bug today —
but if a future chart update adds one, always pair `wia.enabled: true` with a
`wallet_version` (this repo uses go-wallet-backend's own version as the
value) in the same change.

**`scripts/render-helm-config.py --target fly --env <name> ...` run by
itself (not via `make fly-up`) without `--mongo-password <value>` silently
renders a Mongo connection URI with no credentials at all:** `mongo_password`
defaults to `None`, and `patch_wallet_backend_fly()` does `mongo_auth =
f"root:{mongo_password}@" if mongo_password else ""` — empty string, not an
error. This happens because `fly-up.py`'s `main()` generates a fresh random
`mongo_password` per invocation and sets it as *both* the mongodb app's own
Fly secret (`force=True` — rotated every full `fly-up` run) *and* the value
baked into the rendered config; the two are only guaranteed consistent within
the same `fly-up.py` invocation. Hand-rendering one component's config to
tweak a single field, then `flyctl deploy`-ing just that app, silently breaks
that component's Mongo auth, because the freshly-rendered password won't
match whatever's still set as the mongodb app's actual Fly secret from the
last full `fly-up` run. For any single-field config tweak, just re-run `make
fly-up ENV=<name>` again instead of hand-rolling a partial `flyctl deploy` —
confirmed idempotent/safe, it redeploys every component with fresh,
mutually-consistent config+secrets rather than clobbering anything that's
already there.

**PDP boot appears stuck / "Issuer not trusted" right after `fly-up` with
`TRUSTED_ISSUERS=` set (or any PDP redeploy with it already set):**
`go-trust`'s `WhitelistRegistry.StartRefreshLoop` does a *synchronous*,
unbounded initial JWKS refresh for every whitelisted entity before the HTTP
listener even starts. An mdoc-only issuer with no JWKS endpoint burns the
full discovery-timeout budget — several minutes. Check
`flyctl logs -a sirosid-<env>-pdp` for `"Configuring whitelist registry from
config file"` sitting unfinished before assuming a new trust-config
regression — this is deliberate, tolerated behavior (`extra_trusted_issuers`
is deliberately still added to `pid_issuers` despite the slow-boot cost,
because a resolution-only trust check doesn't need the JWKS to have actually
resolved). Separately: if testing wallet-initiated *presentation* against an
external verifier, `TRUSTED_ISSUERS`/`extra_trusted_issuers` is **only** added
to `pid_issuers`, never `verifiers` — use `TRUSTED_VERIFIERS=` for that
instead (`make fly-up ENV=<name> TRUSTED_VERIFIERS=identity,...`); check
`fixtures/rendered/fly-<env>/pdp.yaml`'s `whitelist.lists.verifiers` if a
presentation-trust failure looks like it should've been covered but wasn't.
`TRUSTED_VERIFIERS` entries must be the exact string go-trust's
`WhitelistRegistry` compares against *after* its own `Subject.ID`
normalization, confirmed via a live PDP rejection: an `x509_hash:...`
`client_id` is left un-normalized (safe to paste verbatim from the wallet's
"not trusted" error log), but `x509_san_dns:<host>`/`x509_san_uri:<uri>`
values get normalized to `https://<host>`/`<uri>` before any whitelist match
runs — an entry written in the original `x509_san_dns:`/`x509_san_uri:` form
(the shape go-trust's own docs example at `docs/docs/sirosid/trust/go-trust.md`
uses) silently never matches.

**vc-apigw/vc-issuer crash-looping on Fly with an `mdl`/mdoc-schema config
error (`unexpected end of JSON input` / `vctm_file_path ... required_without`):**
the deployed image is older than the `MDDLSchema` support
`fixtures/vc-config.yaml`'s `mdl` scope needs (`mddl_file_path`). Check
`values-fly.yaml`'s current `images.issuer*`/`verifier` pins — they should be
a published tag that includes `sirosfoundation/vc#23`
(`feature/support-mdoc-schema-driven`, merged upstream into `SUNET/vc`'s
`main`). If a `fly-up` was run with an explicit `IMAGES=` override for these
components, a *subsequent* plain `fly-up` (no `IMAGES=`) silently reverts them
back to whatever's pinned in `values-fly.yaml` — check
`flyctl status -a sirosid-<env>-vc-apigw`/`-vc-issuer` after any redeploy of
an environment you didn't build from scratch yourself.

**Fly networking, in general:**
- 6PN (Fly's internal network) is **IPv6-only** — a component binding only
  IPv4 (`mongod`'s default) is unreachable from sibling apps over
  `.internal` unless it's explicitly told to bind `--ipv6` too.
- **Autostart only fires on the public edge**, never for internal 6PN calls
  between sibling apps — an internal-only component left on
  `auto_stop_machines='stop'` goes idle and *stays* stopped forever once a
  caller only ever reaches it over 6PN. Every component here uses
  `auto_stop_machines='off'`, `min_machines_running=1` — the environment's
  own `fly-up`/`fly-down` lifecycle is the actual on-demand mechanism, not
  per-machine autostop.
- A machine that crash-looped and got fixed by a config-only redeploy does
  **not** restart itself — `scripts/fly_common.py`'s `ensure_running()`
  explicitly checks and `fly machine start`s anything not `started` after
  every deploy. This runs inside `fly-up.py`'s own per-component deploy loop
  (right after each `flyctl deploy`), so it covers every component during a
  normal `make fly-up` — the gap is specifically a single component deployed
  by hand (bypassing `fly-up.py`), where nothing calls `ensure_running()` for
  you. The Mongo-auth footgun above is a common way to end up needing this:
  a manual partial redeploy crash-loops the component, `flyctl status` shows
  it `stopped`, and the fix is either `flyctl machine start -a
  sirosid-<env>-<component>` or (safer, and the recommended path per the
  Mongo-auth gotcha) just re-running the full `make fly-up ENV=<name>`.
- `flyctl secrets list --json` returns lowercase `"name"`, not `"Name"` — an
  idempotency check keyed on the wrong case silently always re-sets secrets.
- The PDP whitelist must list the identity that actually appears as the
  credential's `iss` claim (vc-apigw's/vc-verifier's *public* URL) — not an
  internal `.internal` hostname that's merely reachable. Whitelisting the
  wrong one produces a 404 on JWKS fetch that looks like a trust bug but is
  really a URL-choice bug.

## Where to look next

- `make help` — authoritative, current flag/target reference (this file
  intentionally doesn't duplicate it).
- `README.md` — setup, service ports, Android passkey troubleshooting table,
  directory structure.
- `ANDROID-TESTING.md` / `R2PS.md` — deep dives on Android SDK testing and
  the R2PS key-provisioning flow, respectively. (`ENVIRONMENT.md` and
  `CONFORMANCE-CANDIDATE-BUGS.md` were removed as stale/resolved - don't
  recreate them from an old cached read.)
- `scripts/fly_common.py`'s module/function docstrings — extremely thorough
  inline rationale for every Fly-specific design decision (component ordering,
  health-check strategy, nginx configs); read these before changing anything
  Fly-related rather than re-deriving it from scratch.
- `scripts/render-helm-config.py`'s `build_fly_values_overlay()` — the
  whitelist/mdociaca construction referenced above.

## Working conventions

- This repo pushes directly to `main` with **no PR workflow** — there is no
  review gate catching a bad direct push. Be conservative about committing
  changes here unprompted, and never run `make fly-up`/`fly-down`/anything
  Fly-touching without confirming no one else has a deployment for that
  `ENV=` name in progress (`flyctl apps list`/`fly-status ENV=<name>` first).
- `values-fly.yaml` and `.golden-releases.yaml` are checked-in, shared
  defaults — changes to them affect every developer's next `fly-up`/`GOLDEN=`
  run, not just yours. `.android-apps`, `.env*` files are gitignored,
  per-developer state.
