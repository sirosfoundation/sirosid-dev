# sirosid-dev

Local development environment for SIROS ID wallet ecosystem.

## Overview

This repository provides a complete local development stack for the SIROS ID wallet platform, including:

- **wallet-frontend** — Web wallet UI ("SIROS ID (dev)")
- **go-wallet-backend** — Wallet backend API (Go)
- **go-trust** — AuthZEN-compliant Trust PDP (allow / whitelist / deny modes)
- **mock-verifier** — OpenID4VP verifier mock
- **mock-trust-pdp** — Legacy AuthZEN PDP mock
- **vctm-registry** — Verifiable Credential Type Metadata registry (served by
  go-wallet-backend itself in `--mode=all`, not a separate container)
- **vc-issuer / vc-verifier / vc-apigw / vc-registry / mini-oidc** — Production-like VC services (optional)
- **facetec-api** — FaceTec SDK ↔ vc-issuer bridge (optional, `FACETEC=yes`)

## Quick Start

Bootstrap a complete development environment with a single command:

```bash
curl -fsSL https://raw.githubusercontent.com/sirosfoundation/sirosid-dev/main/install.sh | bash
cd sirosid-dev
make up
```

Or if you already have the repos cloned:

```bash
make up            # Start default stack (go-trust allow-all)
make up VC=yes     # … with production-like VC services
make up GOLDEN=yes # … using pre-built golden release images
make status        # Check all service health
make logs          # View Docker logs
make down          # Stop everything
make update        # Force-update all repos to upstream
make help          # Full option reference
```

## Prerequisites

- Docker and Docker Compose
- The following sibling repositories cloned alongside `sirosid-dev`
  (not needed when using `GOLDEN=yes` with pre-built images):
  ```
  siros.org/
  ├── sirosid-dev/          # this repo
  ├── wallet-frontend/      # web wallet UI
  ├── go-wallet-backend/    # wallet backend (Go)
  ├── go-trust/             # trust PDP
  ├── wallet-common/        # shared TypeScript types
  ├── vc/                   # VC services (optional, for VC=yes)
  ├── siros-id-stack/          # public production Helm chart (optional, for PDP=helm)
  └── facetec-api/          # FaceTec SDK bridge (optional, for FACETEC=yes)
  ```

The `install.sh` script clones all of these automatically. Alternatively,
run `make setup` from an existing checkout to verify prerequisites.

## Configuration

A single `make up` command drives all configurations via parameters.
For the full list of options, targets, and usage patterns, run:

```bash
make help
```

This shows all available parameters (PDP, VC, TRANSPORT, CONFORMANCE, R2PS, DOMAIN, GOLDEN, TUNNELS, FACETEC, ANDROID_APPS, WALLET_NAME, APP_PACKAGE),
interaction rules, and complete examples.

## Quickstart Examples

```bash
make up                    # Default stack
make up VC=yes             # Add VC services
make up TRANSPORT=wmp      # Use WMP transport
make up CONFORMANCE=yes    # OpenID conformance suite
make up DOMAIN=<host>.local VC=yes    # Custom domain for mobile
make up GOLDEN=yes         # Pre-built images
```

For more examples and detailed explanations, see `make help`.

### Custom Domain / Mobile Testing

The `DOMAIN=` option replaces all `localhost` references in service URLs
with a custom hostname, enabling access from mobile devices or other
machines on the local network.

```bash
# Using a local hostname (requires DNS/mDNS or /etc/hosts on the device)
make up DOMAIN=myhost.local VC=yes
```

The domain must resolve to the host machine's IP from the testing device
(via `/etc/hosts`, mDNS, or local DNS).

### Cloudflare Tunnels (On-Demand TLS Domains)

For testing with real TLS certificates and publicly reachable URLs (e.g.
for mobile devices not on the same network, or when TLS is required),
use Cloudflare quick tunnels. No Cloudflare account is needed — temporary
`*.trycloudflare.com` domains are assigned automatically.

```bash
# 1. Start the stack with tunnel support
make up TUNNELS=yes VC=yes

# 2. Open the frontend tunnel URL on any device
#    (shown in the output of 'make up')

# Check tunnel status
make tunnel-status

# Stop tunnels and remove the public URLs
make tunnel-stop
make up VC=yes    # restart with localhost
```

**Android passkeys + tunnels:** `make up TUNNELS=yes` automatically creates or
reuses `.env.tunnel`, injects the tunnel URLs into the stack, and re-runs
`make android-setup` after startup so Android testing remains aligned with the
current environment. See [Android SDK Testing](#android-sdk-testing) for the
full workflow.

**Prerequisites:** `cloudflared` must be installed:
```bash
# Linux
curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
  -o /usr/local/bin/cloudflared && chmod +x /usr/local/bin/cloudflared

# macOS
brew install cloudflared
```

**How it works:** `make up TUNNELS=yes` ensures three `cloudflared` quick tunnel
processes exist (frontend:3000, backend:8080, engine:8082). Each gets a unique
`https://<random>.trycloudflare.com` URL with a valid TLS certificate. The
tunnel overlay reconfigures the frontend and backend containers to use these
URLs, injects the correct WebAuthn RP ID (hostname only — no `https://`
scheme), and updates `WALLET_SERVER_RP_ORIGINS` with the current APK key hash
from `.env.android`.

**Lifecycle note:** the tunnels are host processes, not Docker containers.
`make down` stops the stack but leaves the tunnels running so the URLs can be
reused. Use `make tunnel-stop` when you want to tear them down.

## Android SDK Testing

The Android SDK sample app (`siros-sdk-kotlin`) or native wrapper apps (TypeScript/JavaScript wrappers)
can be tested against the local dev environment using a physical Android device or emulator.
`APP_PACKAGE` specifies the app's package name for both SDK-based and wrapper implementations.

### One-Time Setup

```bash
# Connect your Android device via USB (or start an emulator), then:
make android-setup

# For SDK sample app (siros-sdk-kotlin):
make android-setup APP_PACKAGE=org.siros.sdk.sample

# For native wrapper apps:
make android-setup APP_PACKAGE=com.example.mywalletapp
```

This does four things automatically:

1. Extracts the SHA256 fingerprint from `~/.android/debug.keystore`
2. Computes the base64url APK key hash and writes it to `.env.android`
3. Generates `.well-known/assetlinks.json` served by the frontend nginx
4. Enables `DEVELOPMENT_PASSKEY_REGISTRATION` on the connected device via ADB
   (required because `trycloudflare.com` subdomains are not in Google's DAL cache)

After `make android-setup`, both `make up` and `make up TUNNELS=yes`
automatically source `.env.android` so `WALLET_SERVER_RP_ORIGINS` always
includes the correct APK key hash for your keystore.

**No device connected?** Steps 1–3 still run; only the ADB step is skipped.
You can re-run `make android-setup` later when a device is available.

### Local Network Testing

If the device is on the same Wi-Fi as the dev machine, use `DOMAIN=`:

```bash
make android-setup
make up DOMAIN=<hostname>.local VC=yes
```

The device must be able to resolve `<hostname>.local` (mDNS or `/etc/hosts`).
Point the SDK sample app at `http://<hostname>.local:8090` (via the wallet-proxy).

### Cloudflare Tunnel Testing (Recommended for Passkeys)

Passkeys on physical devices require HTTPS. Cloudflare quick tunnels provide
real TLS without any account:

```bash
# 1. Generate assetlinks.json and configure ADB
make android-setup

# 2. Start the stack with public TLS URLs
make up TUNNELS=yes [VC=yes]

# 3. Point the sample app at the backend tunnel URL shown in the output
```

`make up TUNNELS=yes` re-runs `make android-setup` automatically. This keeps
the generated Android config current and re-applies any device-side passkey
setup that may be needed for physical-device testing.

### Passkey Troubleshooting

| Symptom | Most likely cause | Fix |
|---------|-------------------|-----|
| "RP ID cannot be validated" | `DEVELOPMENT_PASSKEY_REGISTRATION` not set | Connect device and run `make android-setup` |
| "Origin not allowed" | APK key hash mismatch | Delete `.env.android` and re-run `make android-setup` with the correct keystore |
| Passkey creation works locally but fails with tunnels | Wrong RP ID (scheme included) | Fixed in current code — ensure you are on latest `main` |
| Sample app can't reach backend | Tunnel URL not updated in app | Use the backend tunnel URL from the `make up TUNNELS=yes` or `make tunnel-status` output |

**Manual ADB fallback** (if `make android-setup` can't set the flag automatically):
```bash
adb shell am compat enable DEVELOPMENT_PASSKEY_REGISTRATION org.siros.sdk.sample
```

### Key Files

| File | Description |
|------|-------------|
| `.env.android` | Generated by `make android-setup`; contains `APK_KEY_HASH` and `ANDROID_PACKAGE`; sourced by `make up` |
| `.well-known/assetlinks.json` | Served by wallet-frontend nginx; required for Android passkey RP ID validation |
| `scripts/setup-android.sh` | Script behind `make android-setup` |
| `docker-compose.tunnel.yml` | Injects `TUNNEL_FRONTEND_URL`, `TUNNEL_RPID` (hostname only), `APK_KEY_HASH` |

### Trust PDP Modes

| Mode | Description |
|------|-------------|
| `PDP=allow` (default) | go-trust allow-all — every entity is trusted |
| `PDP=whitelist` | go-trust whitelist — only entities in `fixtures/vc-go-trust-whitelist.yaml` are trusted |
| `PDP=deny` | go-trust deny-all — rejects everything (negative testing) |
| `PDP=mock` | Legacy mock-trust-pdp (no go-trust) |
| `PDP=helm` | go-trust whitelist + wallet-backend, both configured from config files rendered off the [siros-id-stack](https://github.com/sirosfoundation/siros-id-stack) chart (see `scripts/render-helm-config.py`) instead of hand-maintained env vars/flags. Requires a sibling `../siros-id-stack` checkout. This is the transitional step towards aligning sirosid-dev's config with the production Helm chart — over time the other PDP modes' hand-maintained env vars are meant to be replaced by this path, not kept alongside it indefinitely. |

## VC Services

When started with `VC=yes`, the environment includes production-like VC
services built from the `../vc` source repository. On startup, the issuer
and verifier are automatically registered with the wallet backend via the
admin API — no manual registration needed.

Available credentials: PID (ARF 1.5 + 1.8), EHIC, Diploma.

### Service Architecture

```
Browser → wallet-frontend (3000)
            ↓
          wallet-backend (8080/8081/8082)
            ↓ (credential discovery)
          vc-apigw (9003) ← OAuth2 AS + OpenID4VCI metadata
            ↓
          vc-issuer (9000) ← credential signing
          vc-verifier (9001) ← OpenID4VP + OIDC provider
          vc-registry (9004) ← status lists, type metadata
          mongodb ← persistence
```

The **apigw** is the public-facing entry point for OpenID4VCI — it serves
`.well-known/openid-credential-issuer` metadata and handles token/credential
endpoints. The issuer and registry are internal backend services.

## Service Ports

### Default Stack

| Service | Port | Description |
|---------|------|-------------|
| wallet-frontend | 3000 | Web wallet UI |
| wallet-backend | 8080 | Backend API |
| wallet-backend admin | 8081 | Admin API |
| wallet-engine | 8082 | Credential engine |
| mock-verifier | 9011 | OpenID4VP verifier mock |
| mock-trust-pdp | 9081 | Trust PDP mock |

### VC Services (when `VC=yes`)

| Service | HTTP | gRPC | Description |
|---------|------|------|-------------|
| vc-issuer | 9000 | 9190 | OpenID4VCI credential issuer |
| vc-verifier | 9001 | 9091 | OpenID4VP verifier + OIDC provider |
| vc-apigw | 9003 | — | OAuth2 AS + credential metadata |
| vc-registry | 9004 | 9094 | Status lists and type metadata |

### go-trust Instances

| Instance | Port | Description |
|----------|------|-------------|
| go-trust-allow | 9095 | Allow-all (default PDP) |
| go-trust-whitelist | 9096 | Whitelist mode |
| go-trust-deny | 9097 | Deny-all |

### R2PS Services (when `R2PS=yes`)

| Service | Port | Description |
|---------|------|-------------|
| r2ps-server | 8443 | R2PS protocol (WSCD/WSCA) |
| r2ps-server admin | 8444 | Admin API (key listing, status lists) |
| r2ps-server (conformance) | 9443 | R2PS when running with conformance suite |
| r2ps-server admin (conformance) | 9444 | Admin when running with conformance suite |

## Source Paths

Services are built from sibling directories by default. Override with
environment variables or on the command line:

| Variable | Default | Description |
|----------|---------|-------------|
| `FRONTEND_PATH` | `../wallet-frontend` | Wallet frontend source |
| `BACKEND_PATH` | `../go-wallet-backend` | Wallet backend source |
| `VC_PATH` | `../vc` | VC services source |
| `GO_TRUST_PATH` | `../go-trust` | go-trust source |
| `FACETEC_PATH` | `../facetec-api` | facetec-api source (`FACETEC=yes` only) |
| `SIROS_ID_STACK_PATH` | `../siros-id-stack` | siros-id-stack source (`PDP=helm` / `make fly-up` only) |
| `WALLET_NAME` | `SIROS ID (dev)` | Wallet display name |

```bash
# Example: use a different frontend checkout
make up FRONTEND_PATH=~/other/wallet-frontend
```

## Golden Releases

The `GOLDEN=` option lets you run pre-built container images from a tested
release instead of building from local source. This is useful for quick
demos, reproducing reported issues, or running the stack without cloning
all the source repos.

```bash
# Use the default golden release
make up GOLDEN=yes

# Use a specific named release
make up GOLDEN=beta_r2 VC=yes
```

Golden release definitions are maintained in the
[siros-conformance](https://github.com/sirosfoundation/siros-conformance)
repo (`golden-releases.yaml`). The Makefile fetches this file on demand
and generates a `.env.golden` file with the resolved image tags.

**Note:** When using `GOLDEN=yes VC=yes`, the wallet and go-trust services
use golden images but VC services are still built from local source. This is
because the VC configuration format (`fixtures/vc-config.yaml`) evolves
between releases, making golden VC images incompatible with the current
config. The golden VC overlay (`docker-compose.golden-vc.yml`) is available
for future use once configs are version-aligned.

Images are pulled from `ghcr.io/sirosfoundation/*` — you may need
`docker login ghcr.io` if the images are not public.

## Updating Repos

Force-update all sibling repositories to their default upstream branches:

```bash
make update
```

This fetches and hard-resets each repo (`main` for most, `release/sirosid`
for `wallet-frontend` and `wallet-common`).

## Conformance Testing

The OpenID Foundation Conformance Suite validates the wallet's OID4VCI and
OID4VP implementations.

```bash
# 1. Start conformance environment (auto-configures /etc/hosts)
make up CONFORMANCE=yes

# 2. Install test dependencies
cd ../sirosid-tests && make install

# 3. Run conformance tests
cd ../sirosid-tests && make test-conformance

# Conformance UI: https://localhost.emobix.co.uk:8443/
```

## R2PS (Remote PAKE-Protected Signing)

An advanced, currently deprioritized WSCD option: a remote HSM-backed signing
service (SoftHSM2 + PAKE-authenticated protocol), as an alternative to the
default on-device keystore.

```bash
make up R2PS=yes VC=yes
make r2ps-setup          # verify health + list provisioned keys
```

See [R2PS.md](R2PS.md) for the key-provisioning protocol, admin API
cookbook, HSM parameters, and Android SDK plugin configuration.

## Fly.io Deployment

Spin up a full, independently addressable wallet stack (frontend, wallet-proxy,
backend, PDP, issuer, verifier, apigw, registry, mongo, mini-oidc) on Fly.io
under the shared `sirosfoundation` org - each named environment gets its own set of
`sirosid-<env>-*` apps and `*.fly.dev` URLs, fully isolated from every other
environment. Config is rendered from the `siros-id-stack` chart (same
mechanism as `PDP=helm`, see below) and images are pulled straight from that
chart's `values.yaml` - no local Docker build.

```bash
make fly-up ENV=alice              # deploy a new environment
make fly-status ENV=alice          # check all 10 apps
make fly-down ENV=alice            # tear it down
```

Requires `flyctl` installed and authenticated, and a sibling `../siros-id-stack`
checkout (`make setup` clones it).

### Multiple developers, multiple environments

Environments are fully isolated by name - two developers can run
`make fly-up ENV=alice` and `make fly-up ENV=bob` at the same time with zero
collision (verified with two concurrent full deploys).

### Overriding image versions

Two different override mechanisms exist, for two different purposes -
they're not redundant, each solves a problem the other can't:

- **`IMAGES=` (ad-hoc, per-environment, opt-in)** - override any one of the
  10 components' image for *your* environment only, e.g. to test your own
  branch build:
  ```bash
  make fly-up ENV=alice IMAGES="wallet-backend=ghcr.io/sirosfoundation/go-wallet-backend:pr-123"
  # comma-separate multiple: IMAGES="wallet-backend=...,pdp=..."
  ```
  This never touches any checked-in file - it only affects the one
  environment you passed it to.

  **Testing a local build, with no registry to push to by hand**: if the
  value is a bare, unqualified tag (no `/`) that's already present in your
  local Docker daemon - exactly what `make up REBUILD=yes` / plain
  `docker-compose build` already produce (`wallet-backend-e2e-test:local`,
  `vc-issuer-e2e-test:local`, etc.) - `fly-up.py` pushes it into that
  environment's own `registry.fly.io/sirosid-<env>-<component>` namespace for
  you and deploys from there:
  ```bash
  make fly-up ENV=alice IMAGES="wallet-backend=wallet-backend-e2e-test:local"
  ```
  No manual `docker tag`/`docker push`/`flyctl auth docker` needed - it
  reuses your existing `flyctl auth login` session. A fully-qualified ref
  (anything with a `/`, e.g. `ghcr.io/sirosfoundation/...`) is always passed
  through untouched, even if that exact tag also happens to be cached
  locally, so this never reinterprets an intentionally-remote value.

  Images pushed this way live in that Fly app's own registry namespace, so
  they're torn down for free with the environment itself
  (`make fly-down ENV=alice`) - no separate cleanup/expiry job needed.

- **`values-fly.yaml`'s `images:` block (shared default, checked in)** -
  pins several components (currently `images.pdp`, `images.walletBackend`,
  and the four `vc.*` image keys) ahead of `siros-id-stack`'s own, slower-moving
  pins, each because this repo needs a fix or feature the chart hasn't
  caught up to yet - e.g. the `images.pdp` override exists because the
  chart's own pin predates a fix
  ([sirosfoundation/go-trust#112](https://github.com/sirosfoundation/go-trust/pull/112))
  the PDP's whitelist needs to function *at all* once deployed on Fly
  (without it, JWKS fetch fails for every whitelisted issuer/verifier and
  the whitelist never becomes healthy). This is deliberately **not**
  handled by asking every developer to remember `IMAGES=pdp=...` on every
  `make fly-up` - a broken default should be fixed at the default, not
  worked around per-invocation.

  Every entry in that file's `images:` block carries its own comment naming
  the exact upstream condition under which it should be deleted (e.g. "once
  siros-id-stack bumps images.pdp past vX.Y.Z") - read `values-fly.yaml`
  directly for the current pinned versions and per-override rationale
  rather than trusting a version number here; this prose already drifted
  out of sync with the file once and will again.

### Android app identities (debug builds + Play Store keys)

wallet-backend's server-side WebAuthn accept list (`server.rp_origins`) must
carry an `android:apk-key-hash:...` entry for every Android app/signing-key
pair you want to test passkeys with - registering a package in
`assetlinks.json` alone passes Android's OS-level Digital Asset Links check
but still fails the actual WebAuthn ceremony if `rp_origins` doesn't also
have it (see `scripts/android_apps.py`'s docstring).

Copy `.android-apps.example` to `.android-apps` (gitignored, per-developer/
per-checkout) and list every package=fingerprint pair you want trusted -
several debug builds and/or Play Store upload keys at once:

```
com.example.debug=AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99
com.example=11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00
```

This one file is read by **both** targets, regardless of which one you use:

```bash
make up                              # default local docker-compose
make up PDP=helm                    # helm-rendered local config
make fly-up ENV=alice                # Fly.io
```

`ANDROID_APPS=pkg=fingerprint,...` (comma-separated, repeatable-by-comma) adds
one-off entries on the command line without touching `.android-apps`, and
`.env.android` (from `make android-setup`) is still honored on top of both -
none of these replace each other, they all merge. See
`scripts/android_apps.py` for the exact precedence and the hex/base64url
conversion every consumer needs.

## Directory Structure

```
sirosid-dev/
├── install.sh                     # Bootstrap script (curl | bash)
├── Makefile                       # Single entry point for all operations
├── docker-compose.test.yml        # Primary dev environment (always included)
├── docker-compose.*.yml           # Optional overlays - one per make up flag
│                                  # (go-trust modes, VC, R2PS, conformance,
│                                  # Android/USB, tunnel, domain, golden
│                                  # images, transports, FaceTec, helm-config)
│                                  # -- run `make help` for which flag adds
│                                  # which overlay; there are ~30 of these
├── nginx-e2e.conf                 # Frontend nginx config (dashboard + health proxies)
├── values-dev.yaml / values-fly.yaml   # Helm values overlays for render-helm-config.py
├── fixtures/                      # VC/PDP config templates, PKI, presentation requests
├── mocks/                         # mock-verifier (OpenID4VP) + trust-pdp (legacy AuthZEN) mocks
└── scripts/                       # All Makefile-invoked automation (Fly deploy, Android/USB
                                   # setup, helm config rendering, tunnels, PKI) - see each
                                   # script's own header/--help; setup-android.sh is the
                                   # actively-used Android setup path, generate-assetlinks.sh
                                   # is its (unused) legacy predecessor
```

## Troubleshooting

```bash
# Check service health
make status          # Core services
make status-vc       # VC services (when VC=yes)

# View logs for a specific service
docker logs wallet-backend-e2e-test
docker logs vc-apigw-e2e

# Rebuild from clean state
make clean           # Remove all containers and volumes
make up VC=yes       # Rebuild and start

# Regenerate PKI (signing keys and certificates)
make pki
```

## Integration with sirosid-tests

```bash
# Start dev environment
cd sirosid-dev && make up

# Run tests (in another terminal)
cd sirosid-tests && make test

# With conformance suite
cd sirosid-dev && make up CONFORMANCE=yes
cd sirosid-tests && make test-conformance
```

## See Also

- [ANDROID-TESTING.md](ANDROID-TESTING.md) — Android SDK / Waydroid / USB device testing deep dive
- [R2PS.md](R2PS.md) — Remote PAKE-Protected Signing deep dive
- [Local Development Environment](https://developers.siros.org/howto/local-dev-environment) — full setup guide on the developer docs site
- [sirosid-tests](https://github.com/sirosfoundation/sirosid-tests) — E2E test suites
- [go-wallet-backend](https://github.com/sirosfoundation/go-wallet-backend) — Wallet backend
- [wallet-frontend](https://github.com/wwWallet/wallet-frontend) — Web wallet UI
- [go-trust](https://github.com/sirosfoundation/go-trust) — Trust PDP
- [SUNET/vc](https://github.com/SUNET/vc) — VC services (issuer, verifier, registry)
- [siros-id-stack](https://github.com/sirosfoundation/siros-id-stack) — Production Helm chart (`PDP=helm` / `make fly-up`)
