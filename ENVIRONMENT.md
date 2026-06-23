# sirosid-dev: Complete Development Environment

A comprehensive, reproducible local development environment for SIROS wallet and identity components with full OpenID4VCI/OpenID4VP conformance testing support, Android SDK testing, and R2PS remote signing.

## Quick Start

### Minimum Requirements
- Docker & Docker Compose
- Linux/macOS (tested on Linux)  
- 8+ GB RAM, 20+ GB disk space
- `/etc/hosts` entry: `127.0.0.1 localhost.emobix.co.uk`

### Start Environment

```bash
cd sirosid-dev
make up CONFORMANCE=yes VC=yes
```

This brings up:
- **Frontend**: http://localhost:3000
- **Backend**: http://localhost:8080 (admin UI on 8081)
- **Conformance Suite**: https://localhost.emobix.co.uk:8443/
- **VC Services**: vc-issuer, vc-verifier, vc-apigw, vc-registry, vc-mockas
- **Trust/PDPs**: go-trust variants, allow-all configuration by default
- **Databases**: MongoDB (VC), PostgreSQL (wallet)

### Verify Health

```bash
make status
```

Expected output: 18+ containers running and healthy

### Run Conformance Tests

Tests are located in two places:

**CI Integration Tests** (in `siros-conformance/`):
```bash
cd ../siros-conformance
make test-issuer    # OID4VCI issuer conformance
make test-verifier  # OID4VP verifier conformance  
make test-wallet    # Both wallet tests (VCI + VP)
```

**Local Dev Tests** (run directly via make targets or scripts):
```bash
# Android SDK conformance testing:
node run-android-conformance.mjs --plan vci   # OID4VCI against Waydroid app
node run-android-conformance.mjs --plan vp    # OID4VP against Waydroid app
node run-android-conformance.mjs --plan all   # Both plans

# Or use the make target:
make android-test                             # Full Android test flow

# Conformance suite UI is always available at:
# https://localhost.emobix.co.uk:8443/
```

## Environment Architecture

### Docker Compose Layers

The environment is built from overlay files, combined via `make up [OPTIONS]`:

| File | Services | Required For |
|------|----------|--------------|
| `docker-compose.test.yml` | wallet-frontend, wallet-backend, wallet-proxy | Core wallet stack |
| `docker-compose.vc-services.yml` | vc-issuer, vc-verifier, vc-apigw, vc-registry, vc-mockas, mongodb | Credential issuance & verification |
| `docker-compose.go-trust.yml` | go-trust-allow, go-trust-deny, go-trust-whitelist (PDPs) | Trust decision framework |
| `docker-compose.conformance.yml` | conformance-suite-server, conformance-suite-nginx, conformance-runner | OpenID conformance testing |
| `docker-compose.r2ps.yml` | r2ps-server, r2ps-softhsm | Remote PAKE-protected signing |
| `docker-compose.android.yml` | Waydroid integration helpers | Android SDK testing |

### Configuration Options

```bash
# Enable conformance testing
make up CONFORMANCE=yes

# Enable VC services (issuance & verification)
make up VC=yes

# Enable R2PS remote signing (with SoftHSM2)
make up R2PS=yes

# Transport layer experiments (WebSocket is default)
make up TRANSPORT=wmp     # WMP transport (JSON-RPC+SSE)
make up TRANSPORT=http    # HTTP transport (deprecated)

# Use golden releases (pre-built images from specific milestone)
make up GOLDEN=yes

# Custom domain (for mobile testing on local network)
# Note: Domain must resolve from test device to host machine IP
make up DOMAIN=myhost.local

# Custom Android SDK package name (default: org.sirosfoundation.sdk.sample)
make android-setup APP_PACKAGE=com.example.customapp

# Cloudflare quick tunnels (on-demand TLS for mobile testing)
make up TUNNELS=yes            # Auto-create/reuse tunnels and start with tunnel URLs
make tunnel-status             # Show active tunnel URLs
make tunnel-stop               # Stop and cleanup tunnels
# Tunnels create temporary https://*.trycloudflare.com domains
# TUNNELS=yes and DOMAIN=... are mutually exclusive

# Combine multiple options
make up CONFORMANCE=yes VC=yes DOMAIN=test.local
```

### Environment Variables

Key variables set by docker-compose:

```env
WALLET_BACKEND_URL=http://wallet-backend:8080
WALLET_ADMIN_URL=http://wallet-admin:8081
WALLET_FRONTEND_URL=http://wallet-frontend:3000
VC_ISSUER_URL=http://vc-issuer:8080
VC_VERIFIER_URL=http://vc-verifier:8080
VC_APIGW_URL=http://vc-apigw:8080
VC_REGISTRY_URL=http://vc-registry:8080
CONFORMANCE_URL=https://localhost.emobix.co.uk:8443/
GO_TRUST_URL=http://go-trust-allow:8080
R2PS_URL=http://r2ps-server:8443 (or 9443 with conformance)
```

## Advanced Configuration

### Custom Domain (DOMAIN=)

For mobile device testing on local network without hostnames:

```bash
# Set domain (must be resolvable from test device to host IP)
make up DOMAIN=myhost.local VC=yes

# This updates:
# - WALLET_BACKEND_URL → https://myhost.local:8080
# - WALLET_FRONTEND_URL → https://myhost.local:3000
# - WEBAUTHN_RPID → myhost.local (for WebAuthn attestation)
# - WALLET_SERVER_RP_ID → myhost.local

# On test device, add to /etc/hosts:
# <HOST_IP> myhost.local
```

**Use Case**: Testing with physical mobile device on same network

### Custom Android App Package Name (APP_PACKAGE=)

Deploy sample app with custom package name:

```bash
# Generate Android setup with a custom package
make android-setup APP_PACKAGE=com.example.mywalletapp

# This updates:
# - assetlinks.json with new package
# - .env.android with custom package name

# Verify in generated assetlinks.json:
cat .well-known/assetlinks.json
# Should show: "package_name": "com.example.mywalletapp"
```

**Use Case**: Testing multiple wallet app variants, custom branding

### Cloudflare Quick Tunnels (TUNNELS=yes)

Create temporary HTTPS domains for testing without local network access:

```bash
# Prerequisites
command -v cloudflared >/dev/null || \
  (brew install cloudflare/cloudflare/cloudflared || apt-get install cloudflared)

# Start stack with tunnels
make up TUNNELS=yes
# Output shows tunnel URLs such as:
# Frontend: https://xxx-yyy-zzz.trycloudflare.com
# Backend:  https://aaa-bbb-ccc.trycloudflare.com  
# Engine:   https://ddd-eee-fff.trycloudflare.com

# Check tunnel status
make tunnel-status

# Test on any device with internet
curl https://xxx-yyy-zzz.trycloudflare.com

# Cleanup when done
make tunnel-stop
```

**Use Case**: Testing from external networks, demoing to remote devices

**Configuration**: 
- Tunnels written to `.env.tunnel`
- docker-compose.tunnel.yml overlay injects them
- No Cloudflare account required (quick tunnel mode)
- URLs valid for ~24 hours
- Tunnel processes continue running after `make down`; use `make tunnel-stop` to remove them

### WebAuthn RP ID & FIDO2 Configuration

For FIDO2 attestation validation:

```bash
# Default: localhost
WEBAUTHN_RPID=localhost

# With custom domain:
make up DOMAIN=myhost.local
# Automatically sets: WEBAUTHN_RPID=myhost.local

# go-trust validates FIDO2 authenticators' RP ID claims
# Mismatch causes attestation verification failure
```

**Key Points**:
- RP ID must match domain frontend is served from
- Used for WebAuthn credential generation
- YubiKey firmware validates RP ID matches user intent
- Android pre-filled in sample app via `BuildConfig.WEBAUTHN_RPID`

## Testing Scenarios

### OID4VCI: Pre-Authorized Code Flow

1. Start environment: `make up VC=yes CONFORMANCE=yes`
2. Access conformance suite: https://localhost.emobix.co.uk:8443/
3. Create test plan: OID4VCI Issuer, variant "pre-authorized_code"
4. Watch issuer respond to conformance suite requests
5. Check backend logs: `docker logs wallet-backend-e2e-test`

**Expected**: All modules pass ✅

### OID4VP: Verifier Testing

1. Start: `make up VC=yes CONFORMANCE=yes`
2. Create VP Verifier test plan
3. Wallet receives presentation request
4. Wallet submits presentation (via conformance suite automation)

**Expected**: All modules pass ✅

### R2PS Remote Signing

**Prerequisites**:
- R2PS service running: `make up R2PS=yes`
- SoftHSM2 token initialized with label "r2ps-wscd"
- Wallet SDK configured with R2PS enabled

**Flow**:
1. Wallet SDK discovers R2PS at http://r2ps-server:8443 (via env var)
2. Wallet registers with R2PS (OPAQUE authentication)
3. Wallet requests key generation via P256Generate
4. Wallet signs credentials using remote key via authenticated session
5. Backend verifies signatures

**Known Issue**:
- SoftHSM slot initialization may fail in container
- Workaround: R2PS uses token label discovery, which is more robust

### Android SDK Testing

**Setup**:
```bash
make android-setup    # Generate assetlinks.json + .env.android
make android-up       # Start Waydroid integration
```

**Status**:
- siros-wscd-manager 0.1.0 AAR with all plugins (softkey, r2ps, fido2) published to mavenLocal
- siros-sdk-kotlin sample-app ready for testing with R2PS and rawSign support
- Requires Waydroid with sample-app APK installed

**Test Flow**:
1. App initializes siros-wscd-manager FFI bindings
2. Attempts credential issuance with OID4VCI issuer
3. Wallet signs with available WSCD plugin (softkey default, r2ps if enabled)
4. Verifier confirms signature validity

## Troubleshooting

### Conformance Suite Not Accessible

```bash
# Verify container is running
docker ps | grep conformance-suite-nginx

# Check logs
docker logs conformance-suite-nginx

# Verify host entry
cat /etc/hosts | grep localhost.emobix.co.uk
```

### VC Services Not Ready

```bash
# Check health
docker ps --filter "name=vc-" --format "{{.Names}}\t{{.Status}}"

# View logs
docker logs vc-issuer
docker logs vc-apigw
docker logs vc-verifier
```

### R2PS Token Initialization Failed

```bash
# Check R2PS logs
docker logs r2ps-softhsm
docker logs r2ps-server

# R2PS uses token label discovery as fallback (R2PS_HSM_TOKEN_LABEL=r2ps-wscd)
# Service should still start successfully even if initialization fails
```

### Backend Connection Issues

```bash
# Verify backend is healthy
curl -s http://localhost:8080/api/health | jq

# Check logs
docker logs wallet-backend-e2e-test | tail -50

# Verify database connectivity
docker exec wallet-backend-e2e-test nc -zv postgres 5432
```

## Makefile Targets

### Core Operations

- `make up [OPTIONS]` - Start environment with selected overlays
- `make down` - Stop all containers
- `make status` - Show running services health
- `make clean` - Destroy containers, volumes, cache (full reset)
- `make logs SERVICE` - Tail service logs (e.g., `make logs wallet-backend`)

### Android-Specific

- `make android-setup` - Generate assetlinks.json and APK key hash
- `make android-up` - Start Android conformance helpers
- `make android-launch [OFFER_URL]` - Send credential offer to Waydroid

### Testing

- `node run-android-conformance.mjs --plan vci` - OID4VCI Android wallet conformance
- `node run-android-conformance.mjs --plan vp` - OID4VP Android wallet conformance
- `node run-android-conformance.mjs --plan all` - Both plans
- `make android-test` - Full Android build + deploy + test flow
- Conformance suite UI: https://localhost.emobix.co.uk:8443/

### Development

- `make shell SERVICE` - Open shell in service container
- `make pki` - Regenerate self-signed certificates
- `make tunnel` - Start Cloudflare quick tunnels for mobile testing
- `make tunnel-status` - Show active tunnel URLs  
- `make tunnel-stop` - Stop tunnels and cleanup

## Architecture Decisions

### Why Multiple Overlays?

Each overlay represents an optional capability:
- **Conformance**: Can test without running conformance suite (smaller footprint)
- **VC**: Can test wallet without issuer/verifier (debug wallet flows only)
- **R2PS**: Optional for advanced remote signing tests
- **Android**: Separate Waydroid integration configuration

### Why go-trust in All Configs?

Trust PDP is foundational to the architecture. It makes authorization decisions for:
- Issuer registration
- Credential offer acceptance  
- Verifier authorization
- Wallet capability restrictions

### Certificate Management

- Self-signed certificates generated at startup
- Stored in `/etc/ssl/certs/` within containers
- Mounted from `docker-compose` volumes
- Regenerate with: `make pki`

### Database Persistence

- `docker-compose.test.yml` uses named volumes: `wallet-db`, `wallet-frontend-cache`
- VC MongoDB uses `vc-mongodb-data`
- Volumes survive `make down` but not `make clean`
- Clear specific volume: `docker volume rm wallet-db`

## Integration Points

### siros-conformance

Separate CI integration for containerized conformance testing. Runs in parallel to sirosid-dev for:
- Release candidate validation
- Golden releases baseline
- Automated PR conformance checks

Location: `../siros-conformance/`

### go-wallet-backend (staging/r4)

Latest production-ready backend implementation. Tested via sirosid-dev conformance.

Key changes in staging/r4:
- Centralized audit logging via go-siros-set SET framework
- Production VC service integration
- WebAuthn attestation validation via go-trust

### siros-wscd-manager (0.1.0)

Native Android/iOS WSCD manager with pluggable signing backends:
- **plugin-softkey**: Default JWE-encrypted container
- **plugin-r2ps**: Remote PAKE-protected signing  
- **plugin-fido2**: YubiKey previewSign/rawSign

Published to mavenLocal at: `~/.m2/repository/org/sirosfoundation/siros-wscd-manager/0.1.0/`

Consumed by siros-sdk-kotlin sample-app.

## Next Steps After Setup

1. **Verify Wallet Works**:
   ```bash
   open http://localhost:3000
   # Register test user → Create wallet → Test OID4VCI flow
   ```

2. **Run Conformance Suite**:
   ```bash
   cd ../siros-conformance
   make test-wallet  # Full wallet + issuer + verifier test
   ```

3. **Test Android SDK**:
   ```bash
   make android-setup
   make android-up
   # Deploy sample-app APK via adb / Waydroid
   # Send credential offers → Verify SDK receives + processes them
   ```

4. **Inspect R2PS** (advanced):
   ```bash
   curl -s http://r2ps-server:8443/admin/store/keys --cert ... 
   # View provisioned remote keys
   ```

5. **Review Backend Logs**:
   ```bash
   docker logs wallet-backend-e2e-test --follow
   # Watch audit events emitted via SET framework
   ```

## Environment Files

Key configuration files generated/used:

- `.env` - Primary environment variables
- `.env.android` - Android-specific config (APK key hash)
- `.env.golden` - Golden release image overrides
- `.well-known/assetlinks.json` - Android App Link verification
- `docker-compose.*.yml` - Overlay configurations
- `.golden-releases.yaml` - Pinned image versions for milestones

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────┐
│                    sirosid-dev Environment              │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  Frontend (3000)  →  Proxy (443)  →  Backend (8080)   │
│                         ↓                               │
│  Conformance Suite (8443)  ←→  VC Services (8080s)    │
│                         ↓                               │
│        Trust PDPs  ←→  Databases (PG, Mongo)          │
│                                                         │
│  Optional:                                              │
│  - R2PS (8443/9443) + SoftHSM for remote signing       │
│  - Waydroid + Android SDK for mobile testing           │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

## Support & Debugging

- **Logs**: `docker logs <service>` or `make logs SERVICE`
- **Health**: `make status` shows real-time container status
- **Database**: `docker exec -it wallet-db psql -U wallet`
- **Conformance UI**: https://localhost.emobix.co.uk:8443/
- **Conformance Suite**: https://localhost.emobix.co.uk:8443/ (direct access)

---

**Last Updated**: 2026-06-23  
**Tested On**: Linux (Docker CE, Compose v2.20+)
