# sirosid-dev

Local development environment for SIROS ID wallet ecosystem.

## Overview

This repository provides a complete local development stack for the SIROS ID wallet platform, including:

- **wallet-frontend** — Web wallet UI ("SIROS ID (dev)")
- **go-wallet-backend** — Wallet backend API (Go)
- **go-trust** — AuthZEN-compliant Trust PDP (allow / whitelist / deny modes)
- **mock-verifier** — OpenID4VP verifier mock
- **mock-trust-pdp** — Legacy AuthZEN PDP mock
- **vctm-registry** — Verifiable Credential Type Metadata registry
- **vc-issuer / vc-verifier / vc-apigw / vc-registry** — Production-like VC services (optional)

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
  └── vc/                   # VC services (optional, for VC=yes)
  ```

The `install.sh` script clones all of these automatically. Alternatively,
run `make setup` from an existing checkout to verify prerequisites.

## Stack Options

A single `make up` command drives all configurations via parameters:

| Option | Values | Default | Description |
|--------|--------|---------|-------------|
| `PDP=` | `allow`, `whitelist`, `deny`, `mock` | `allow` | Trust PDP mode |
| `VC=` | `1`, `yes`, `on`, `up` | off | Enable VC services (issuer, verifier, apigw, registry) |
| `TRANSPORT=` | `wmp`, `http` | websocket | Transport protocol |
| `CONFORMANCE=` | `1`, `yes`, `on`, `up` | off | Enable OpenID Conformance Suite |
| `GOLDEN=` | `yes`, `<release-name>` | off | Use pre-built images from a golden release |
| `DOMAIN=` | any hostname | `localhost` | Domain name used in all service URLs |

### Examples

```bash
# Default: frontend + backend + go-trust allow-all
make up

# Add production-like VC issuer/verifier
make up VC=yes

# VC services with whitelist trust (only configured issuers trusted)
make up PDP=whitelist VC=1

# Negative testing: deny all trust evaluations
make up PDP=deny VC=1

# Legacy mock PDP (no go-trust)
make up PDP=mock

# WMP transport
make up TRANSPORT=wmp

# Full conformance test stack (implies VC + allow + http transport)
make up CONFORMANCE=yes

# Use pre-built images from the default golden release (no local source needed)
make up GOLDEN=yes

# Golden release with VC services
make up GOLDEN=yes VC=yes

# Use a specific golden release
make up GOLDEN=beta_r2 VC=1

# Mobile device development: use host's LAN name so real devices can reach services
make up DOMAIN=myhost.local
```

### Trust PDP Modes

| Mode | Description |
|------|-------------|
| `PDP=allow` | go-trust allow-all — trusts everything (default, for development) |
| `PDP=whitelist` | go-trust whitelist — only entities in `fixtures/vc-go-trust-whitelist.yaml` are trusted |
| `PDP=deny` | go-trust deny-all — rejects everything (negative testing) |
| `PDP=mock` | Legacy mock-trust-pdp (no go-trust) |

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

## Source Paths

Services are built from sibling directories by default. Override with
environment variables or on the command line:

| Variable | Default | Description |
|----------|---------|-------------|
| `FRONTEND_PATH` | `../wallet-frontend` | Wallet frontend source |
| `BACKEND_PATH` | `../go-wallet-backend` | Wallet backend source |
| `VC_PATH` | `../vc` | VC services source |
| `GO_TRUST_PATH` | `../go-trust` | go-trust source |
| `WALLET_NAME` | `SIROS ID (dev)` | Wallet display name |

```bash
# Example: use a different frontend checkout
make up FRONTEND_PATH=~/other/wallet-frontend
```

## Mobile Development with Real Devices

By default, all services advertise themselves as `localhost`. This works
fine in a browser on the same machine, but JavaScript running on a real
mobile device cannot reach `localhost` — it would refer to the device
itself, not your development machine.

Use the `DOMAIN=` variable to replace `localhost` with a hostname that is
reachable from devices on the same local network:

```bash
# Use your machine's mDNS name (macOS/Linux with Avahi)
make up DOMAIN=myhost.local

# Or use your LAN IP address
make up DOMAIN=192.168.1.42
```

With this set, the wallet frontend will issue XHR requests to
`http://myhost.local:8080` instead of `http://localhost:8080`, making it
work correctly when opened on a phone or tablet connected to the same
network.

**Note:** WebAuthn (passkey) flows require the `DOMAIN` value to match the
origin served in the browser. Ensure you open the frontend using
`http://myhost.local:3000` (not `http://localhost:3000`) when testing on a
real device.

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

## Directory Structure

```
sirosid-dev/
├── install.sh                           # Bootstrap script (curl | bash)
├── Makefile                             # Single entry point for all operations
├── docker-compose.test.yml              # Primary dev environment
├── docker-compose.go-trust.yml          # go-trust base overlay
├── docker-compose.go-trust-allow.yml    # go-trust allow-all
├── docker-compose.go-trust-whitelist.yml
├── docker-compose.go-trust-deny.yml
├── docker-compose.vc-services.yml       # VC services (issuer, verifier, apigw, registry)
├── docker-compose.vc-go-trust.yml       # VC ↔ go-trust wiring
├── docker-compose.conformance.yml       # OpenID Conformance Suite
├── docker-compose.golden.yml            # Golden overlay: wallet images
├── docker-compose.golden-go-trust.yml   # Golden overlay: go-trust image
├── docker-compose.golden-vc.yml         # Golden overlay: VC service images
├── docker-compose.http-transport.yml    # HTTP proxy transport
├── docker-compose.wmp-transport.yml     # WMP transport
├── nginx-e2e.conf                       # Frontend nginx config (dashboard + health proxies)
├── fixtures/
│   ├── create-pki.sh                    # PKI generation script
│   ├── vc-config.yaml                   # Shared VC services config
│   ├── vc-go-trust-whitelist.yaml       # Trust whitelist
│   ├── vc-pki/                          # Signing keys and certificates
│   ├── vc-metadata/                     # VCTM JSON files
│   └── vc-presentation-requests/        # Presentation request templates
├── mocks/
│   ├── verifier/                        # OpenID4VP mock verifier
│   └── trust-pdp/                       # AuthZEN PDP mock
└── scripts/
    ├── start-soft-fido2.sh
    └── stop-soft-fido2.sh
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

- [Local Development Environment](https://developers.siros.org/howto/local-dev-environment) — full setup guide on the developer docs site
- [sirosid-tests](https://github.com/sirosfoundation/sirosid-tests) — E2E test suites
- [go-wallet-backend](https://github.com/sirosfoundation/go-wallet-backend) — Wallet backend
- [wallet-frontend](https://github.com/wwWallet/wallet-frontend) — Web wallet UI
- [go-trust](https://github.com/sirosfoundation/go-trust) — Trust PDP
- [SUNET/vc](https://github.com/SUNET/vc) — VC services (issuer, verifier, registry)
