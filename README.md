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
| `R2PS=` | `1`, `yes`, `on`, `up` | off | Enable R2PS service with SoftHSM2 (WSCD/WSCA) |
| `DOMAIN=` | hostname/FQDN | `localhost` | Custom domain for mobile device access |
| `GOLDEN=` | `yes`, `<release-name>` | off | Use pre-built images from a golden release |

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

# Custom domain for mobile device access on the local network
make up DOMAIN=myhost.local VC=yes
```

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
# 1. Start the stack normally
make up VC=yes

# 2. Create tunnels (assigns random *.trycloudflare.com URLs)
make tunnel

# 3. Restart the stack with tunnel URLs injected
make restart-with-tunnels

# 4. Open the frontend tunnel URL on any device
#    (shown in the output of 'make tunnel')

# Check tunnel status
make tunnel-status

# Stop tunnels and revert to localhost
make tunnel-stop
make up VC=yes    # restart with localhost
```

**Prerequisites:** `cloudflared` must be installed:
```bash
# Linux
curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
  -o /usr/local/bin/cloudflared && chmod +x /usr/local/bin/cloudflared

# macOS
brew install cloudflared
```

**How it works:** `make tunnel` starts three `cloudflared` quick tunnel
processes (frontend:3000, backend:8080, engine:8082). Each gets a unique
`https://<random>.trycloudflare.com` URL with a valid TLS certificate.
`make restart-with-tunnels` then reconfigures the frontend and backend
containers to use these URLs instead of `localhost`.

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

The R2PS service provides a remote WSCD (Wallet Secure Cryptographic Device)
backed by SoftHSM2. It enables the wallet to perform key generation, signing,
and ECDH operations through a PAKE-authenticated protocol.

### Starting R2PS

```bash
make up R2PS=yes VC=yes
```

This adds:
- `r2ps-server` — go-r2ps-service (WSCD + WSCA + admin, port 8443)
- `r2ps-softhsm` — SoftHSM2 init (token label `r2ps-wscd`, PIN `1234`)
- `attest-softhsm` — Separate HSM for wallet-backend attestation keys

### Key Provisioning Flow

R2PS keys are **not** provisioned via the admin API. Instead, the wallet SDK
performs key provisioning through the R2PS protocol itself:

1. **Registration** — The wallet registers with the R2PS server using OPAQUE
   (password-authenticated key exchange). This creates a credential on the
   server tied to the wallet's `client_id` and `context`.

2. **Authentication** — On subsequent connections, the wallet authenticates
   via OPAQUE to establish a session (with session ID and symmetric key).

3. **Key Generation** — The authenticated wallet sends a `P256Generate`
   request through the R2PS protocol. The server generates an EC P-256 key
   pair in the HSM and returns confirmation. The public key is stored in the
   key store for later retrieval.

4. **Signing** — The wallet sends sign requests (with the key ID) through
   the authenticated session. The server signs using the HSM-held private key.

### Admin API (Monitoring)

The admin API (port 8444 on host, 8081 inside container) provides read-only
inspection and status list management:

```bash
# List all HSM-generated public keys
curl -s http://localhost:8444/admin/store/keys | jq .

# List keys for a specific wallet client
curl -s http://localhost:8444/admin/store/keys?client_id=<wallet-id> | jq .

# Get a specific key by KID
curl -s http://localhost:8444/admin/store/keys/<kid> | jq .

# List status entries (ka = key attestation, wia = wallet instance attestation)
curl -s http://localhost:8444/admin/store/statuses/ka | jq .
curl -s http://localhost:8444/admin/store/statuses/wia | jq .

# Allocate a new status list index (for testing)
curl -s -X POST http://localhost:8444/admin/store/allocate/ka | jq .

# Verify R2PS health
curl -s http://localhost:8443/healthz
```

### Wallet-Backend Integration

The wallet-backend is made aware of R2PS via the `WALLET_R2PS_URL` environment
variable (set automatically by `docker-compose.r2ps.yml`):

```yaml
environment:
  - WALLET_R2PS_URL=http://r2ps-server:8443
```

### Android SDK Configuration

For the native Android SDK (`siros-sdk-kotlin`), R2PS is configured in the
sample app's `WalletViewModel`:

- `BuildConfig.R2PS_ENABLED` — Toggle R2PS (requires native lib built with
  `r2ps` Cargo feature in `siros-wscd-manager`)
- `DEFAULT_R2PS_URL` — R2PS server URL (default: `http://192.168.240.1:9443`
  for Waydroid, where `192.168.240.1` is the host gateway)

When running conformance tests with R2PS, use the port remapping overlay to
avoid conflicts with the conformance suite (both use 8443):

```bash
# R2PS remapped to 9443/9444 to coexist with conformance suite on 8443
docker compose -f docker-compose.test.yml \
  -f docker-compose.vc-services.yml \
  -f docker-compose.go-trust.yml \
  -f docker-compose.go-trust-allow.yml \
  -f docker-compose.r2ps.yml \
  -f docker-compose.r2ps-conformance.yml \
  -f docker-compose.conformance.yml \
  up -d
```

### HSM Details

| Parameter | Value |
|-----------|-------|
| HSM module | `/usr/lib/softhsm/libsofthsm2.so` |
| Token label | `r2ps-wscd` |
| User PIN | `1234` |
| SO PIN | `5678` |
| Key type | EC P-256 |
| Pool size | 4 sessions |

### Verifying Provisioned Keys

After the wallet has registered and generated a key:

```bash
# Run the setup script to check status
make r2ps-setup

# Or manually query the admin API
curl -s http://localhost:8444/admin/store/keys | python3 -c "
import sys, json
keys = json.load(sys.stdin)
for k in keys:
    print(f\"  KID: {k['kid']}  Curve: {k['curve']}  Client: {k.get('client_id','?')}\")
"
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
├── docker-compose.r2ps.yml              # R2PS service (go-r2ps-service + SoftHSM2)
├── docker-compose.r2ps-conformance.yml  # R2PS port remapping for conformance coexistence
├── docker-compose.android.yml           # Android SDK overlay
├── docker-compose.domain.yml            # Custom domain overlay (DOMAIN= support)
├── docker-compose.tunnel.yml           # Cloudflare tunnel URL overlay
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
    ├── stop-soft-fido2.sh
    └── tunnel.sh                        # Cloudflare quick tunnel management
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
