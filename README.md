# sirosid-dev

Local development environment for SIROS ID wallet ecosystem.

## Overview

This repository provides a complete local development stack for the SIROS ID wallet platform, including:

- **wallet-frontend** - Web wallet UI
- **go-wallet-backend** - Wallet backend API (Go implementation)
- **mock-issuer** - OpenID4VCI credential issuer mock
- **mock-verifier** - OpenID4VP verifier mock
- **mock-trust-pdp** - AuthZEN Trust PDP mock
- **vctm-registry** - Verifiable Credential Type Metadata registry
- **go-trust** - Production trust PDP service (optional)
- **vc-issuer/verifier/apigw/registry** - Production-like VC services (optional)

## Quick Start

```bash
# Start the default development environment
make up

# Check status of all services
make status

# View logs
make logs

# Stop all services
make down
```

## Common Use Cases

### Testing a Credential Issuance Flow

The wallet supports OpenID4VCI credential issuance. To test an issuance flow:

#### 1. Using Mock Issuer (Development)

```bash
# Start the default environment with mock issuer
make up

# The mock issuer runs at http://localhost:9000
# Use the admin API to register it with the wallet backend
curl -X POST http://localhost:8081/admin/issuers \
  -H "Authorization: Bearer e2e-test-admin-token-for-testing-purposes-only" \
  -H "Content-Type: application/json" \
  -d '{"name":"Mock Issuer","url":"http://localhost:9000"}'
```

The mock issuer is pre-configured with several credential types (PID, diploma, ehic). 
You can trigger issuance from the wallet UI or via API.

#### 2. Using Production-like VC Services

```bash
# Start with full VC stack (issuer, verifier, registry, apigw, mockas)
make up-vc

# Services available:
# - vc-issuer:     http://localhost:9000 (gRPC: localhost:9090)
# - vc-verifier:   http://localhost:9001 (gRPC: localhost:9091)
# - vc-mockas:     http://localhost:9002 (authentication)
# - vc-apigw:      http://localhost:9003 (OAuth2 AS)
# - vc-registry:   http://localhost:9004 (status lists, gRPC: localhost:9094)
```

#### 3. With Trust Evaluation (go-trust)

For testing issuer trust evaluation during credential issuance:

```bash
# Allow-all mode (development - accepts any issuer)
make up-vc-go-trust-allow

# Whitelist mode (staging - only trusted issuers)
make up-vc-go-trust-whitelist

# Deny-all mode (negative testing - rejects all issuers)
make up-vc-go-trust-deny
```

### Testing a Verification Flow

The wallet supports OpenID4VP for credential presentation to verifiers.

#### 1. Using Mock Verifier (Development)

```bash
# Start with default environment
make up

# Mock verifier available at http://localhost:9001
# Register the verifier
curl -X POST http://localhost:8081/admin/verifiers \
  -H "Authorization: Bearer e2e-test-admin-token-for-testing-purposes-only" \
  -H "Content-Type: application/json" \
  -d '{"name":"Mock Verifier","url":"http://localhost:9001"}'
```

#### 2. Using Production-like Verifier

```bash
# Start VC services which includes the production verifier
make up-vc

# Verifier runs at http://localhost:9001
# It supports:
# - OpenID4VP authorization requests
# - OIDC login with credential presentation
# - Dynamic presentation request configuration
```

#### 3. End-to-End Verification with Trust

```bash
# Start VC services with whitelist-based trust
make up-vc-go-trust-whitelist

# The verifier (localhost:9001) will only accept credentials from 
# issuers listed in fixtures/vc-go-trust-whitelist.yaml
```

### Testing Trust Policy Scenarios

The go-trust service provides AuthZEN-compliant trust evaluation:

| Mode | Command | Behavior |
|------|---------|----------|
| Allow All | `make up-vc-go-trust-allow` | All issuers/verifiers trusted (dev) |
| Whitelist | `make up-vc-go-trust-whitelist` | Only configured entities trusted |
| Deny All | `make up-vc-go-trust-deny` | No entities trusted (negative testing) |

Configure the whitelist in `fixtures/vc-go-trust-whitelist.yaml`:

```yaml
whitelist:
  issuers:
    - http://localhost:9000
    - http://localhost:9003
  verifiers:
    - http://localhost:9001
  trusted_subjects:
    - pattern: "*.example.com"
```

## Controlling Software Versions

### Component Sources

By default, services are built from sibling directories. Override by setting paths:

```bash
# Use different repository locations
export FRONTEND_PATH=~/projects/my-wallet-frontend
export BACKEND_PATH=~/projects/my-wallet-backend
export TS_BACKEND_PATH=~/projects/wallet-backend-server

# Start with custom paths
make up
```

### Using Specific Branches

To test a specific branch of a component:

```bash
# 1. Clone or checkout the desired branch in the sibling directory
cd ../wallet-frontend
git checkout feature/new-oidc-flow

# 2. Start the environment (rebuilds from source)
cd ../sirosid-dev
make up
```

### Using Docker Images Instead of Source

To use pre-built images instead of building from source, modify the docker-compose file:

```yaml
# In docker-compose.test.yml, change:
wallet-frontend:
  build:
    context: ${FRONTEND_PATH:-../wallet-frontend}
  # To:
  image: ghcr.io/wwwallet/wallet-frontend:v1.2.3
```

### Version Matrix for Testing

| Component | Default Source | Override Variable | Pre-built Images |
|-----------|---------------|-------------------|------------------|
| wallet-frontend | `../wallet-frontend` | `FRONTEND_PATH` | `ghcr.io/wwwallet/wallet-frontend:TAG` |
| go-wallet-backend | `../go-wallet-backend` | `BACKEND_PATH` | `ghcr.io/sirosfoundation/go-wallet-backend:TAG` |
| wallet-backend-server | `../wallet-backend-server` | `TS_BACKEND_PATH` | N/A (build from source) |
| go-trust | `../go-trust` | `GO_TRUST_PATH` | `ghcr.io/sirosfoundation/go-trust:TAG` |
| VC services | `../../vc` | `VC_PATH` | N/A (build from source) |

### CI Environment

In CI, repositories are typically checked out alongside each other:

```bash
# CI checkout structure
workspace/
├── sirosid-dev/
├── wallet-frontend/
├── go-wallet-backend/
└── sirosid-tests/

# CI typically sets:
export FRONTEND_PATH=./wallet-frontend
export BACKEND_PATH=./go-wallet-backend
```

## Stack Configurations

| Configuration | Command | Description |
|--------------|---------|-------------|
| Default | `make up` | Frontend + Go backend + mocks |
| Go Trust | `make up-go-trust` | Adds go-trust PDP services |
| Go Trust Whitelist | `make up-go-trust-whitelist` | Uses go-trust whitelist mode as PDP |
| VC Services | `make up-vc` | Production-like VC issuer/verifier |
| VC + Trust Allow | `make up-vc-go-trust-allow` | VC services + allow-all trust |
| VC + Trust Whitelist | `make up-vc-go-trust-whitelist` | VC services + whitelist trust |
| VC + Trust Deny | `make up-vc-go-trust-deny` | VC services + deny-all trust |
| TS Backend | `make up-ts-backend` | TypeScript wallet-backend-server |

## Service Ports

### Default Stack

| Service | Port | Description |
|---------|------|-------------|
| wallet-frontend | 3000 | Web wallet UI |
| wallet-backend | 8080 | Backend API |
| wallet-backend admin | 8081 | Admin API |
| wallet-engine | 8082 | Credential engine |
| mock-issuer | 9000 | OpenID4VCI issuer |
| mock-verifier | 9001 | OpenID4VP verifier |
| mock-trust-pdp | 9091 | Trust PDP mock |
| vctm-registry | 8097 | VCTM registry |

### VC Services Stack (when using `up-vc*`)

| Service | HTTP Port | gRPC Port | Description |
|---------|-----------|-----------|-------------|
| vc-issuer | 9000 | 9090 | OpenID4VCI issuer |
| vc-verifier | 9001 | 9091 | OpenID4VP verifier + OIDC |
| vc-mockas | 9002 | - | Mock authentication server |
| vc-apigw | 9003 | - | OAuth2 authorization server |
| vc-registry | 9004 | 9094 | Status lists and revocation |
| go-trust-allow | 9095 | - | Trust PDP (allow all) |
| go-trust-whitelist | 9096 | - | Trust PDP (whitelist) |
| go-trust-deny | 9097 | - | Trust PDP (deny all) |

## Directory Structure

```
sirosid-dev/
├── docker-compose.test.yml            # Primary dev environment
├── docker-compose.go-trust.yml        # go-trust overlay
├── docker-compose.go-trust-whitelist.yml
├── docker-compose.vc-services.yml     # Production VC services
├── docker-compose.vc-go-trust.yml     # VC + go-trust overlay
├── docker-compose.ts-backend.yml      # TypeScript backend
├── dockerfiles/
│   └── frontend.Dockerfile
├── mocks/
│   ├── issuer/                        # OpenID4VCI mock issuer
│   ├── verifier/                      # OpenID4VP mock verifier
│   └── trust-pdp/                     # AuthZEN PDP mock
├── fixtures/
│   ├── create-pki.sh                  # PKI generation script
│   ├── vc-config.yaml                 # VC services config
│   ├── vc-go-trust-whitelist.yaml     # Trust whitelist config
│   ├── vc-pki/                        # Signing keys/certs
│   ├── vc-metadata/                   # VCTM JSON files
│   └── vc-presentation-requests/
└── scripts/
    ├── start-soft-fido2.sh
    └── stop-soft-fido2.sh
```

## Configuration Files

### VC Services Configuration

The `fixtures/vc-config.yaml` configures all VC services:

- **apigw** - OAuth2 authorization server settings
- **issuer** - Credential issuer with JWT/signing configuration
- **verifier** - OpenID4VP and OIDC configuration
- **registry** - Status list and revocation settings
- **mock_as** - Mock authentication (auto-approve mode for testing)

### Trust Whitelist

The `fixtures/vc-go-trust-whitelist.yaml` defines trusted entities:

```yaml
whitelist:
  issuers:
    - http://localhost:9000      # Mock issuer
    - http://localhost:9003      # VC apigw
    - http://vc-issuer:8080      # Docker internal
  verifiers:
    - http://localhost:9001      # Mock verifier
    - http://vc-verifier:8080    # Docker internal
  trusted_subjects:
    - pattern: "*.siros.org"     # Wildcard patterns
```

## Environment Variables

Override default configuration via environment variables:

```bash
# Repository locations (for building from source)
export FRONTEND_PATH=../wallet-frontend
export BACKEND_PATH=../go-wallet-backend
export TS_BACKEND_PATH=../wallet-backend-server

# Service URLs (exported for use by sirosid-tests)
export FRONTEND_URL=http://localhost:3000
export BACKEND_URL=http://localhost:8080
export ADMIN_URL=http://localhost:8081

# VC service URLs
export VC_ISSUER_URL=http://localhost:9000
export VC_VERIFIER_URL=http://localhost:9001
export VC_APIGW_URL=http://localhost:9003
export VC_REGISTRY_URL=http://localhost:9004

# Admin authentication
export ADMIN_TOKEN=e2e-test-admin-token-for-testing-purposes-only
```

## Troubleshooting

### Services Not Starting

```bash
# Check Docker logs
docker compose -f docker-compose.test.yml logs

# Check specific service
docker compose -f docker-compose.test.yml logs wallet-backend
```

### Port Conflicts

If ports are already in use:

```bash
# Find what's using a port
lsof -i :9000

# Or change ports via environment variable
# (requires editing docker-compose files)
```

### Rebuilding After Code Changes

```bash
# Force rebuild all services
make down
docker compose -f docker-compose.test.yml build --no-cache
make up
```

### PKI Issues

```bash
# Regenerate signing keys and certificates
make pki
```

## Fixtures

### PKI Generation

```bash
# Generate fresh signing keys and certificates
make pki
# Or directly:
cd fixtures && ./create-pki.sh
```

### VCTM Metadata

The `fixtures/vc-metadata/` directory contains credential type definitions:

- `pid-1.5.json` - EU PID 1.5
- `pid-1.8.json` - EU PID 1.8
- `ehic.json` - European Health Insurance Card
- `diploma.json` - Educational diploma
- `eduid.json` - eduID credential
- `elm.json` - European Learning Model
- `microcredential.json` - Microcredential
- `pda1.json` - Portable Document A1

## Integration with sirosid-tests

This environment is designed to be tested by [sirosid-tests](../sirosid-tests):

```bash
# Start dev environment
cd sirosid-dev && make up

# Run tests against it (in another terminal)
cd sirosid-tests && make test

# Run specific test suites
cd sirosid-tests && make test-public    # Public API tests only
cd sirosid-tests && make test-admin     # Admin API tests
cd sirosid-tests && make test-webauthn  # WebAuthn tests
```

## See Also

- [sirosid-tests](https://github.com/sirosfoundation/sirosid-tests) - E2E test suites
- [go-wallet-backend](https://github.com/sirosfoundation/go-wallet-backend) - Wallet backend
- [wallet-frontend](https://github.com/wwWallet/wallet-frontend) - Web wallet UI
- [go-trust](https://github.com/sirosfoundation/go-trust) - Trust PDP
- [SUNET/vc](https://github.com/SUNET/vc) - VC services (issuer, verifier, registry)
