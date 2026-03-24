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

## Stack Configurations

| Configuration | Command | Description |
|--------------|---------|-------------|
| Default | `make up` | Frontend + Go backend + mocks |
| Go Trust | `make up-go-trust` | Adds go-trust PDP services |
| Go Trust Whitelist | `make up-go-trust-whitelist` | Uses go-trust whitelist mode as PDP |
| VC Services | `make up-vc` | Production-like VC issuer/verifier |
| TS Backend | `make up-ts-backend` | TypeScript wallet-backend-server |

## Service Ports

| Service | Port | Description |
|---------|------|-------------|
| wallet-frontend | 3000 | Web wallet UI |
| wallet-backend | 8080 | Backend API |
| wallet-backend admin | 8081 | Admin API |
| wallet-engine | 8082 | Credential engine |
| mock-issuer | 9000 | OpenID4VCI issuer |
| mock-verifier | 9001 | OpenID4VP verifier |
| mock-trust-pdp | 9091 | Trust PDP mock |
| go-trust-deny | 9092 | go-trust (deny all) |
| go-trust-whitelist | 9093 | go-trust (whitelist) |
| go-trust-allow | 9094 | go-trust (allow all) |
| vctm-registry | 8097 | VCTM registry |

## Directory Structure

```
sirosid-dev/
├── docker-compose.test.yml            # Primary dev environment
├── docker-compose.go-trust.yml        # go-trust overlay
├── docker-compose.go-trust-whitelist.yml
├── docker-compose.vc-services.yml     # Production VC services
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
│   ├── vc-pki/                        # Signing keys/certs
│   ├── vc-metadata/                   # VCTM JSON files
│   └── vc-presentation-requests/
└── scripts/
    ├── start-soft-fido2.sh
    └── stop-soft-fido2.sh
```

## Environment Variables

Override default configuration via environment variables:

```bash
# Repository locations (for building from source)
export FRONTEND_REPO=../wallet-frontend
export BACKEND_REPO=../go-wallet-backend

# Service versions
export FRONTEND_VERSION=latest
export BACKEND_VERSION=latest
```

## Fixtures

### PKI Generation

```bash
# Generate fresh signing keys and certificates
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
```

## See Also

- [sirosid-tests](https://github.com/sirosfoundation/sirosid-tests) - E2E test suites
- [go-wallet-backend](https://github.com/sirosfoundation/go-wallet-backend) - Wallet backend
- [wallet-frontend](https://github.com/wwWallet/wallet-frontend) - Web wallet UI
- [go-trust](https://github.com/sirosfoundation/go-trust) - Trust PDP
