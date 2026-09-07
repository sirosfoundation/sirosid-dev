# R2PS (Remote PAKE-Protected Signing)

The R2PS service provides a remote WSCD (Wallet Secure Cryptographic Device)
backed by SoftHSM2. It enables the wallet to perform key generation, signing,
and ECDH operations through a PAKE-authenticated protocol.

**Status:** advanced/optional, currently deprioritized in favor of the
generic `siros-wscd-manager` + FIDO2 rawSign path — kept working and
documented here, but not the primary signing path to develop against.

## Starting R2PS

```bash
make up R2PS=yes VC=yes
```

This adds:
- `r2ps-server` — go-r2ps-service (WSCD + WSCA + admin, port 8443)
- `r2ps-softhsm` — SoftHSM2 init (token label `r2ps-wscd`, PIN `1234`)
- `attest-softhsm` — Separate HSM for wallet-backend attestation keys

## Key Provisioning Flow

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

## Admin API (Monitoring)

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

## Wallet-Backend Integration

The wallet-backend is made aware of R2PS via the `WALLET_R2PS_URL` environment
variable (set automatically by `docker-compose.r2ps.yml`):

```yaml
environment:
  - WALLET_R2PS_URL=http://r2ps-server:8443
```

## Android SDK Configuration

For the native Android SDK (`siros-sdk-kotlin`), the R2PS plugin is registered
at runtime via `SirosWallet.wscdManager.registerR2psPlugin(...)`, driven from
the sample app's `WalletViewModel` — see `ANDROID-TESTING.md`'s "WSCD Plugins
Configuration" section for the exact API and how to toggle it (there is no
build-time or shell-env-var switch; `BuildConfig.R2PS_ENABLED` is only the
plugin's *initial* value, hardcoded `false` in `build.gradle.kts`).

When running conformance tests with R2PS, use the port remapping overlay to
avoid conflicts with the conformance suite (both use 8443):

```bash
# R2PS remapped to 9443/9444 to coexist with conformance suite on 8443
docker compose -f docker-compose.test.yml \
  -f docker-compose.vc-services.yml \
  -f docker-compose.mongodb.yml \
  -f docker-compose.go-trust.yml \
  -f docker-compose.go-trust-allow.yml \
  -f docker-compose.r2ps.yml \
  -f docker-compose.r2ps-conformance.yml \
  -f docker-compose.conformance.yml \
  up -d
```

## HSM Details

| Parameter | Value |
|-----------|-------|
| HSM module | `/usr/lib/softhsm/libsofthsm2.so` |
| Token label | `r2ps-wscd` |
| User PIN | `1234` |
| SO PIN | `5678` |
| Key type | EC P-256 |
| Pool size | 4 sessions |

## Verifying Provisioned Keys

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
