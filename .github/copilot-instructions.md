# sirosid-dev — Copilot Instructions

## Repository Purpose

`sirosid-dev` is the local development environment for the SIROS ID wallet
platform. It orchestrates Docker Compose stacks for wallet-frontend,
go-wallet-backend, go-trust PDP, mock services, and optional VC services.
The single entry point is the `Makefile`; all configuration flows through
`make up [OPTIONS]`.

## Key Conventions

### Compose file naming
- `docker-compose.test.yml` — primary base stack (always included)
- `docker-compose.<thing>.yml` — optional overlay added via `COMPOSE_FILES`
- `docker-compose.<thing>-allow.yml` / `-deny.yml` — PDP sub-overlays
- Do NOT add `driver: bridge` to any network; the shared `e2e-test-network`
  is always `external: true`. The network must be pre-created with
  `docker network create e2e-test-network` before `docker compose up`.

### Generated env files (never commit these)
- `.env.android` — produced by `make android-setup` / `scripts/setup-android.sh`
  Contains: `APK_KEY_HASH=<base64url>`, `ANDROID_PACKAGE=<name>`
- `.env.tunnel` — produced by `make tunnel` / `scripts/tunnel.sh`
  Contains: `TUNNEL_FRONTEND_URL`, `TUNNEL_BACKEND_URL`, `TUNNEL_ENGINE_URL`
- `.env.golden` — produced by `make fetch-golden-env`
- Generated env files are sourced by `make up` before
  invoking `docker compose`.

### WebAuthn / passkeys
- `WEBAUTHN_RPID` must be a **plain hostname** — no `https://` scheme, no port.
  The tunnel overlay uses `${TUNNEL_RPID}` (scheme stripped by `sed`) not
  `${TUNNEL_FRONTEND_URL}`.
- `WALLET_SERVER_RP_ORIGINS` includes `android:apk-key-hash:<base64url>`.
  The hash must match the debug keystore of the developer running the app.
  It is now dynamic: `${APK_KEY_HASH:-<siros-default>}`, sourced from
  `.env.android`. Developers with a different keystore must run
  `make android-setup` to regenerate `.env.android`.
- `DEVELOPMENT_PASSKEY_REGISTRATION` ADB compat flag must be set on the
  Android device for some physical-device passkey flows. `make up TUNNELS=yes`
  calls `make android-setup` automatically after startup.

### macOS compatibility
- `grep -P` (Perl regex) does not exist on macOS BSD grep — use `grep -E`
- `grep -a` (force text mode) is needed when cloudflared logs contain ANSI codes
- Docker Compose V2 on macOS rejects: same service defined in two files with
  different `container_name`, and any network with both `driver:` and
  `external: true` simultaneously.
- `info()/warn()/ok()` shell functions that print to stdout will corrupt output
  captured in `$(...)` subshells — always redirect them to stderr with `>&2`.

### Error visibility
The `make up` docker compose invocation captures all output to a temp file,
shows a filtered summary (✔/Building/Container/etc.), and on non-zero exit
dumps the full log and fails. Do not revert to the `2>&1 | grep ... || true`
pattern — that silently swallows errors.

## Android SDK Testing Workflow

Full workflow for developers testing `siros-sdk-kotlin` sample app:

```bash
# 1. One-time: generate .env.android and configure ADB
make android-setup [APP_PACKAGE=com.example.app]

# 2. Local network testing
make up [VC=yes]                # backend URL: http://<host>:8090

# 3. Remote / HTTPS testing (required for passkeys on physical devices)
make up TUNNELS=yes             # auto creates/reuses tunnels and re-runs android-setup
```

Key script: `scripts/setup-android.sh`
- Reads `~/.android/debug.keystore` with `keytool`
- Derives APK key hash: `SHA256 fingerprint → hex bytes → base64url (no padding)`
- Writes `.well-known/assetlinks.json` (mounted into wallet-frontend nginx)
- Writes `.env.android` with `APK_KEY_HASH` and `ANDROID_PACKAGE`
- Runs `adb shell am compat enable DEVELOPMENT_PASSKEY_REGISTRATION <package>`

## COMPOSE_FILES Construction

`COMPOSE_FILES` is assembled in `Makefile` lines 107–203 based on parameters:

```
PDP=allow    → + docker-compose.go-trust.yml + docker-compose.go-trust-allow.yml
PDP=whitelist → + go-trust.yml + go-trust-whitelist.yml
PDP=deny     → + go-trust.yml + go-trust-deny.yml
PDP=mock     → nothing extra (mock-trust-pdp is in test.yml)
VC=yes       → + docker-compose.vc-services.yml
CONFORMANCE  → + vc-services + vc-go-trust + conformance (no HTTP transport - it's deprecated)
TRANSPORT=wmp → + wmp-transport.yml
TRANSPORT=http → + http-transport.yml (deprecated)
R2PS=yes     → + r2ps.yml
DOMAIN=<x>   → + domain.yml
GOLDEN=yes   → + golden.yml [+ golden-go-trust.yml]
```

`docker-compose.vc-go-trust.yml` is intentionally NOT included when `VC=yes`
without `CONFORMANCE=yes` — it would duplicate services already in `go-trust.yml`
and break Docker Compose V2 on macOS.

## Service Health

The status check in `make up` uses HTTP polling (curl) not docker health status:
- `curl -sf http://localhost:3000` → wallet-frontend
- `curl -sf http://localhost:8080/health` → wallet-backend
- `curl -sf http://localhost:9000/health` → vc-issuer
- `curl -sf http://localhost:9001/health` → vc-verifier

All services use container name suffix `-e2e-test` or `-e2e` to avoid conflicts
with other docker-compose projects on the same host.

## Common Pitfalls

- `make up VC=yes` fails silently on macOS → check for duplicate service
  definitions across compose files, or network defined as both `driver: bridge`
  AND `external: true` in the merged config.
- Containers start but services not running → usually a missing volume file
  (e.g. `.well-known/assetlinks.json` must exist before `make up`; it is
  created empty automatically if missing).

## CRITICAL: Read Before Troubleshooting

**Before manually hacking URLs, ports, or network config:**
1. Read `README.md` sections: "Cloudflare Tunnels", "Android SDK Testing"
2. Run `make help` to see all available targets
3. Use the documented infrastructure (tunnels, `make android-setup`) instead of
   patching BuildConfig or hardcoding IPs

## Port Architecture

The wallet-backend exposes services on SEPARATE ports:
- **8080**: HTTP API (auth, backend, admin endpoints)
- **8081**: Admin API (issuer/verifier registration)
- **8082**: WebSocket engine (WMP v2 protocol at `/api/v2/wallet`)
- **8443**: Conformance suite (TLS, self-signed cert)

The SDK's `WalletConfig.engineUrl` must point to port 8082 if the backend
is on 8080. In production (single-port deployments), both are on the same port.
In the dev environment, they are always separate.

## USB vs WiFi ADB

- **USB (`make usb-android-*`)**: `adb reverse` works. The device accesses
  host services via `127.0.0.1:<port>`. This is the recommended approach
  for local conformance testing.
- **WiFi ADB**: `adb reverse` does NOT work reliably on Android 11+.
  Use **Cloudflare tunnels** (`make up TUNNELS=yes`) instead. The tunnels
  provide real HTTPS URLs accessible from any network.
- **Never hardcode host IPs** (`10.0.0.x`) in BuildConfig — use either
  `127.0.0.1` (USB) or tunnel URLs (WiFi).

## Conformance Testing

```bash
# USB device (recommended):
make usb-android-setup
make usb-android-full SDK_REBUILD=yes  # builds + deploys + starts stack
make usb-android-conformance PLAN=all

# WiFi device (requires tunnels):
make android-setup
make up TUNNELS=yes VC=yes CONFORMANCE=yes
# Then point the app at the tunnel backend URL
```

The conformance suite needs:
1. Issuer registered in wallet-backend admin API (done by `make usb-android-full`)
2. ADB reverse port forwarding for 13 ports (USB) or tunnel URLs (WiFi)
3. App data cleared before first run (`adb shell pm clear <package>`)
4. `DEVELOPMENT_PASSKEY_REGISTRATION` ADB flag set on device
- `make tunnel` produces garbled `.env.tunnel` → `info()/warn()` output was
  going to stdout and getting captured. All shell helper functions must use `>&2`.
- APK key hash wrong → developer has a different debug keystore from the one
  used to generate the hardcoded default. Run `make android-setup`.

## WSCA Lifecycle Test Automation

### Overview

The `sirosid-tests` repo contains automated WSCA/WSCD lifecycle conformance
tests driven via ADB intents. They exercise the real Android sample app — no
mocks, no backend bypass.

### How It Works

1. Test code sends an ADB intent with action
   `org.sirosfoundation.sdk.sample.WSCA_TEST` and extra `wsca_action`
2. Sample app's `MainActivity` receives the intent (debug builds only)
3. `dispatchWscaTestAction()` calls the real ViewModel lifecycle methods
4. ViewModel emits results as JSON on logcat tag `WSCA_TEST_RESULT`
5. Test code polls logcat for the structured result

### Running WSCA Tests

```bash
cd sirosid-tests

# Softkey plugin only (no external dependencies)
make test-wsca-softkey

# R2PS plugin (requires R2PS service running)
make test-wsca-r2ps R2PS_URL=http://192.168.240.1:9443

# Physical device: set ANDROID_DEVICE_SERIAL
ANDROID_DEVICE_SERIAL=ABC123 make test-wsca-softkey
```

### Key Repos

| Repo | Role |
|------|------|
| `siros-sdk-kotlin` | Sample app with WSCA_TEST intent handler + WscaDeveloperScreen |
| `siros-wscd-manager` | UniFFI Rust crate providing lifecycle API |
| `sirosid-tests` | Playwright + ADB test specs (`helpers/wsca-automation.ts`) |
| `sirosid-dev` | Docker environment orchestration |

### Agent Guidelines for WSCA Tests

- The test automation only works with **debug builds** of the sample app.
  The intent filter is in `src/debug/AndroidManifest.xml` — not in release.
- `ensureAuthenticatedForTesting()` handles passkey login/registration
  automatically when deep links arrive. No manual auth step needed.
- Results are **always** JSON on logcat tag `WSCA_TEST_RESULT`. Parse with:
  `adb logcat -d -s WSCA_TEST_RESULT:*`
- To add new test actions: add a case in `MainActivity.dispatchWscaTestAction()`
  and a matching ViewModel method.
- For physical devices, use `make usb-android-setup` which sets up
  `adb reverse` port forwarding automatically.
