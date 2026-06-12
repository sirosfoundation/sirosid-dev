#!/usr/bin/env bash
# android-test.sh — Build, deploy, and test the SIROS SDK sample app on Waydroid.
#
# Usage:
#   ./scripts/android-test.sh              # full cycle: build → deploy → register issuer → launch → watch logs
#   ./scripts/android-test.sh deploy       # skip build, just deploy + launch
#   ./scripts/android-test.sh logs         # just watch logs
#   ./scripts/android-test.sh register     # just re-register issuer with backend
#   ./scripts/android-test.sh build        # just build APK
#   ./scripts/android-test.sh restart      # restart backend + re-register issuer + relaunch app
#   ./scripts/android-test.sh config        # generate Android vc-config only

set -euo pipefail

# ── Configuration ────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEVENV_DIR="$(dirname "$SCRIPT_DIR")"
WORKSPACE="$(dirname "$DEVENV_DIR")"

SDK_DIR="${WORKSPACE}/siros-sdk-kotlin"
APK="${SDK_DIR}/sample-app/build/outputs/apk/debug/sample-app-debug.apk"
PACKAGE="org.sirosfoundation.sdk.sample"
ACTIVITY="${PACKAGE}/.MainActivity"

export JAVA_HOME="${JAVA_HOME:-/usr/lib/jvm/java-17-openjdk-amd64}"
export ANDROID_HOME="${ANDROID_HOME:-$HOME/Android/Sdk}"
ADB="${ANDROID_HOME}/platform-tools/adb"

ADMIN_URL="${ADMIN_URL:-http://localhost:8081}"
ADMIN_TOKEN="${ADMIN_TOKEN:-e2e-test-admin-token-for-testing-purposes-only}"
TENANT_ID="${TENANT_ID:-default}"
ISSUER_ID="${ISSUER_ID:-http://vc-apigw:8080}"
ISSUER_CLIENT_ID="${ISSUER_CLIENT_ID:-e2e-test-client}"

WAYDROID_IP="${WAYDROID_IP:-192.168.240.112}"
WAYDROID_GATEWAY="${WAYDROID_GATEWAY:-192.168.240.1}"

# Docker Compose files — the android overlay mounts a patched vc-config and
# sets mini-oidc ISSUER to the host gateway so the Android browser can reach
# both the OIDC provider and the OIDC callback via the issuer proxy.
COMPOSE_FILES=(
    -f docker-compose.test.yml
    -f docker-compose.vc-services.yml
    -f docker-compose.go-trust.yml
    -f docker-compose.go-trust-allow.yml
    -f docker-compose.android.yml
)

ANDROID_CONFIG_SCRIPT="${DEVENV_DIR}/scripts/generate-android-config.py"

# ── Colors ───────────────────────────────────────────────────────────────────

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info()  { echo -e "${BLUE}▸${NC} $*"; }
ok()    { echo -e "${GREEN}✓${NC} $*"; }
warn()  { echo -e "${YELLOW}⚠${NC} $*"; }
fail()  { echo -e "${RED}✗${NC} $*"; }

# ── Functions ────────────────────────────────────────────────────────────────

ensure_adb() {
    if ! $ADB get-state >/dev/null 2>&1; then
        info "Connecting to Waydroid at ${WAYDROID_IP}:5555..."
        $ADB connect "${WAYDROID_IP}:5555" >/dev/null 2>&1 || true
        sleep 1
        if ! $ADB get-state >/dev/null 2>&1; then
            fail "Cannot connect to ADB. Is Waydroid running?"
            exit 1
        fi
    fi
    ok "ADB connected"
}

do_build() {
    info "Building sample-app APK..."
    cd "$SDK_DIR"
    if ./gradlew :sample-app:assembleDebug 2>&1 | tail -3; then
        ok "Build successful"
    else
        fail "Build failed"
        exit 1
    fi
}

do_deploy() {
    ensure_adb
    info "Installing APK..."
    if [ ! -f "$APK" ]; then
        fail "APK not found at $APK — run build first"
        exit 1
    fi
    $ADB install -r "$APK" 2>&1 | grep -v "^$"
    ok "APK installed"
}

do_launch() {
    ensure_adb
    info "Launching app..."
    $ADB shell am force-stop "$PACKAGE" 2>/dev/null || true
    $ADB shell am start -n "$ACTIVITY" 2>&1 | grep -v "^$"
    ok "App launched"
}

do_register_issuer() {
    info "Waiting for wallet-backend admin API..."
    for i in $(seq 1 15); do
        if curl -sf "${ADMIN_URL}/admin/tenants/${TENANT_ID}" \
            -H "Authorization: Bearer ${ADMIN_TOKEN}" >/dev/null 2>&1; then
            break
        fi
        sleep 2
    done

    info "Registering issuer: ${ISSUER_ID} (client_id=${ISSUER_CLIENT_ID})"
    local result
    result=$(curl -sf -X POST "${ADMIN_URL}/admin/tenants/${TENANT_ID}/issuers" \
        -H "Authorization: Bearer ${ADMIN_TOKEN}" \
        -H "Content-Type: application/json" \
        -d "{\"credential_issuer_identifier\":\"${ISSUER_ID}\",\"client_id\":\"${ISSUER_CLIENT_ID}\",\"visible\":true}" 2>&1) || true

    if echo "$result" | grep -q "credential_issuer_identifier"; then
        ok "Issuer registered: $(echo "$result" | grep -o '"id":[0-9]*')"
    elif echo "$result" | grep -qi "already\|duplicate\|exists"; then
        ok "Issuer already registered"
    else
        warn "Issuer registration response: $result"
    fi
}

do_generate_config() {
    info "Generating Android VC config (gateway=${WAYDROID_GATEWAY})..."
    if ! command -v python3 >/dev/null 2>&1; then
        fail "python3 is required to generate fixtures/vc-config-android.yaml"
        exit 1
    fi

    cd "$DEVENV_DIR"
    python3 "$ANDROID_CONFIG_SCRIPT" --gateway "$WAYDROID_GATEWAY"
    ok "Generated fixtures/vc-config-android.yaml"
}

do_restart_backend() {
    info "Restarting Android test stack with compose overlay..."
    cd "$DEVENV_DIR"
    docker compose "${COMPOSE_FILES[@]}" up -d --force-recreate \
        mini-oidc vc-apigw wallet-proxy wallet-backend 2>&1 | tail -6
    ok "Android test services restarted"
    sleep 2
    do_register_issuer
}

do_logs() {
    ensure_adb
    info "Watching logs (Ctrl-C to stop)..."
    echo ""
    $ADB logcat -v time | grep --line-buffered -iE "siros|SIROS_VM|trust|auth.*required|credential|flow.*progress|flow.*error|flow.*complete"
}

do_logs_snapshot() {
    ensure_adb
    info "Recent log snapshot:"
    echo ""
    $ADB logcat -t 100 | grep -iE "siros|SIROS_VM|trust|auth.*required|credential|flow.*progress|flow.*error|flow.*complete" | tail -30
}

# ── Main ─────────────────────────────────────────────────────────────────────

CMD="${1:-full}"

case "$CMD" in
    build)
        do_build
        ;;
    deploy)
        do_deploy
        do_launch
        sleep 2
        do_logs_snapshot
        ;;
    launch|run)
        do_launch
        sleep 2
        do_logs_snapshot
        ;;
    register)
        do_register_issuer
        ;;
    logs)
        do_logs
        ;;
    snapshot)
        do_logs_snapshot
        ;;
    restart)
        do_generate_config
        do_restart_backend
        do_launch
        sleep 2
        do_logs_snapshot
        ;;
    config)
        do_generate_config
        ;;
    full|"")
        do_generate_config
        do_build
        do_deploy
        do_register_issuer
        do_launch
        sleep 3
        do_logs_snapshot
        ;;
    *)
        echo "Usage: $0 {full|config|build|deploy|launch|register|restart|logs|snapshot}"
        exit 1
        ;;
esac
