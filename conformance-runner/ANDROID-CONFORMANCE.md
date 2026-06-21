# Android Wallet Conformance Testing

Run OpenID conformance tests against the native Android sample-app in Waydroid.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Host (Linux)                                               │
│  ┌─────────────────┐  ┌──────────────────────────────────┐  │
│  │ Conformance     │  │ Docker containers                │  │
│  │ Runner (Node)   │  │  - conformance-suite-nginx:8443  │  │
│  │                 │  │  - conformance-suite-server       │  │
│  │ run-android-    │  │  - conformance-suite-mongodb      │  │
│  │ conformance.mjs │  │  - wallet-backend:8080/8081      │  │
│  └────────┬────────┘  └──────────────────────────────────┘  │
│           │                                                  │
│  ┌────────▼────────────────────────────────────────────────┐ │
│  │ Waydroid (LXC container, IP: 192.168.240.112)           │ │
│  │  - Android sample-app (org.sirosfoundation.sdk.sample)  │ │
│  │  - Reaches host via gateway: 192.168.240.1              │ │
│  │  - /etc/hosts: 192.168.240.1 localhost.emobix.co.uk     │ │
│  └─────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

## Prerequisites

### 1. Host DNS

```bash
# /etc/hosts (already present in most dev setups)
127.0.0.1 localhost.emobix.co.uk
```

### 2. Waydroid DNS

The Android guest must resolve `localhost.emobix.co.uk` to the host gateway:

```bash
sudo waydroid shell -- sh -c 'echo "192.168.240.1 localhost.emobix.co.uk" >> /etc/hosts'
```

### 3. TLS Certificate with SANs

The conformance suite nginx serves self-signed TLS on port 8443. The default cert
has `CN=localhost` with no SANs, which Android rejects. Generate a cert with proper SANs:

```bash
openssl req -x509 -nodes -days 365 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 \
  -keyout /tmp/conformance-selfsigned.key \
  -out /tmp/conformance-selfsigned.crt \
  -subj "/CN=localhost.emobix.co.uk" \
  -addext "subjectAltName=DNS:localhost.emobix.co.uk,DNS:localhost,IP:127.0.0.1,IP:192.168.240.1"
```

Install into the nginx container:

```bash
docker cp /tmp/conformance-selfsigned.crt conformance-suite-nginx:/etc/ssl/certs/nginx-selfsigned.crt
docker cp /tmp/conformance-selfsigned.key conformance-suite-nginx:/etc/ssl/private/nginx-selfsigned.key
docker exec conformance-suite-nginx nginx -s reload
```

### 4. Install CA in Android Trust Store

```bash
HASH=$(openssl x509 -subject_hash_old -noout -in /tmp/conformance-selfsigned.crt)
sudo cp /tmp/conformance-selfsigned.crt "/var/lib/waydroid/overlay/system/etc/security/cacerts/${HASH}.0"
sudo chmod 644 "/var/lib/waydroid/overlay/system/etc/security/cacerts/${HASH}.0"
# Restart Waydroid for the overlay to take effect
waydroid session stop && waydroid session start
```

### 5. Docker Compose Overlays

```bash
docker compose -f docker-compose.test.yml \
  -f docker-compose.vc-services.yml \
  -f docker-compose.go-trust.yml \
  -f docker-compose.go-trust-allow.yml \
  -f docker-compose.r2ps.yml \
  -f docker-compose.android.yml \
  -f docker-compose.conformance.yml \
  up -d
```

The conformance overlay adds `extra_hosts` for `localhost.emobix.co.uk` to the
wallet-backend container and sets `WALLET_HTTP_CLIENT_INSECURE_SKIP_VERIFY=true`.

### 6. Issuer Registration

The conformance suite's issuer must be registered with the wallet-backend's admin API:

```bash
ADMIN_TOKEN=e2e-test-admin-token-for-testing-purposes-only
curl -s http://localhost:8081/admin/tenants/default/issuers \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "credential_issuer_identifier": "https://localhost.emobix.co.uk:8443/test/a/siros-wallet-vci-test/",
    "client_id": "siros-wallet-test",
    "client_jwk": "<JWK with d parameter>"
  }'
```

The `run-android-conformance.mjs` script handles this automatically when
`ADMIN_TOKEN` is set.

## Running Tests

```bash
cd sirosid-dev
NODE_TLS_REJECT_UNAUTHORIZED=0 \
  ADMIN_TOKEN=e2e-test-admin-token-for-testing-purposes-only \
  node run-android-conformance.mjs --plan vci
```

Options: `--plan vci`, `--plan vp`, `--plan all`

## How It Works

1. Creates a conformance test plan via the conformance suite API
2. For each module:
   - Starts the test module (enters WAITING state)
   - Extracts the credential offer URL from the conformance API response
   - Sends it as an `openid-credential-offer://` deep link to the Android app
   - The app's `ensureAuthenticatedForTesting()` auto-registers a user if needed
   - Monitors logcat for `SirosWallet$connectEngine: Flow ... complete`
   - Waits for the conformance suite to evaluate the result
3. Reports pass/fail per module

## Key Configuration

### vci-wallet-config.json

- `client.jwks.keys[0]` — public key only (no `d` parameter)
- `client.private_key` — full key with `d` for `private_key_jwt` auth
- The conformance suite checks `EnsureClientJwksDoesNotContainPrivateOrSymmetricKeys`

### Wallet State Management

The sample app's `WalletViewModel` uses an indirection (`_walletState` MutableStateFlow)
to survive wallet instance rebuilds during `ensureAuthenticatedForTesting()`.
Without this, Compose observes the old destroyed wallet's state and shows stale errors.

## Troubleshooting

| Problem | Cause | Fix |
|---------|-------|-----|
| `x509: certificate signed by unknown authority` | CA not in Android trust store | Install cert in Waydroid overlay |
| `dial tcp 127.0.0.1:8443: connect: connection refused` | Backend can't reach conformance | Add `extra_hosts` in conformance overlay |
| `invalid_client` | Issuer not registered | Run with `ADMIN_TOKEN` or register manually |
| `EnsureClientJwksDoesNotContainPrivateOrSymmetricKeys` | `d` param in JWKS | Move private key to `client.private_key` |
| Stale error on screen | UI observing destroyed wallet | Fixed via `_walletState` indirection |
