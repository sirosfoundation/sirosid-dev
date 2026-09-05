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

## Android SDK Testing

Full setup/testing workflow, WSCD plugin configuration, and troubleshooting
live in `ANDROID-TESTING.md` — read that before touching Android-related
code or config. Quick orientation:

```bash
make android-setup [APP_PACKAGE=com.example.app]   # one-time: .env.android + ADB config
make up [VC=yes]                                    # local network testing
make up TUNNELS=yes                                 # HTTPS testing (required for passkeys)
```

## COMPOSE_FILES Construction

`COMPOSE_FILES` is assembled incrementally in the `Makefile` (search for
`COMPOSE_FILES +=`, not a fixed line range - it moves) based on parameters:

```
PDP=allow     → + docker-compose.go-trust.yml + docker-compose.go-trust-allow.yml
PDP=whitelist → + go-trust.yml + go-trust-whitelist.yml
PDP=deny      → + go-trust.yml + go-trust-deny.yml
PDP=mock      → nothing extra (mock-trust-pdp is in test.yml)
PDP=helm      → + docker-compose.helm-config.yml (requires ../siros-id-stack)
VC=yes        → + docker-compose.vc-services.yml
FACETEC=yes   → + vc-services (implied) + docker-compose.facetec.yml
CONFORMANCE   → + vc-services + vc-go-trust + conformance (no HTTP transport - it's deprecated)
TRANSPORT=wmp → + wmp-transport.yml
TRANSPORT=http → + http-transport.yml (deprecated)
R2PS=yes      → + r2ps.yml
DOMAIN=<x>    → + domain.yml
TUNNELS=yes   → + tunnel.yml [+ tunnel-vc.yml if VC services are present]
GOLDEN=yes    → + golden.yml [+ golden-go-trust.yml]
VC or PDP=helm → + docker-compose.mongodb.yml (appended last; the persistent Mongo volume)
```

`scripts/stack.py` holds the same matrix as data (`make plan` prints the
result) and `tests/test_stack_parity.py` asserts it agrees with the Makefile -
change both or the test fails.

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
1. Read `README.md`'s "Cloudflare Tunnels" section and `ANDROID-TESTING.md`
   in full for anything Android-related
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

USB (`make usb-android-*`, `adb reverse` to `127.0.0.1`) is recommended over
WiFi ADB (`adb reverse` is unreliable on Android 11+; use Cloudflare tunnels
instead) — see ANDROID-TESTING.md's "Architecture: Waydroid vs Physical
Device" for the full comparison. **Never hardcode host IPs** (`10.0.0.x`) in
BuildConfig regardless of which path you're on.

## Conformance Testing

See ANDROID-TESTING.md for the full workflow (USB vs WiFi setup,
prerequisites, troubleshooting). Two gotchas not written down elsewhere:
- `make tunnel` produced garbled `.env.tunnel` when `info()/warn()` output
  went to stdout and got captured — all shell helper functions must use
  `>&2` (already fixed; keep it that way in any new script).
- APK key hash mismatch → developer has a different debug keystore from the
  one used to generate the hardcoded default. Run `make android-setup`.

## WSCA Lifecycle Test Automation

Full architecture, available actions, running tests, and key repos are in
ANDROID-TESTING.md's "WSCA Lifecycle Test Automation" section — read that
first. Agent-specific guidance for extending this test automation:

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
