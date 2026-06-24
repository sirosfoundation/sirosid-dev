#!/usr/bin/env bash
# usb-android-test.sh — Build, deploy, and test the SIROS SDK sample app on a USB-connected Android device.
#
# Unlike the Waydroid-based android-test.sh, this script:
#   - Uses `adb` directly over USB (no waydroid shell)
#   - Sets up `adb reverse` port forwarding so the device can reach host services via localhost
#   - Generates a USB-specific vc-config (OIDC issuer = localhost, not gateway IP)
#
# Usage:
#   ./scripts/usb-android-test.sh              # full cycle: setup → build → deploy → register → launch → logs
#   ./scripts/usb-android-test.sh setup        # set up adb reverse port forwarding
#   ./scripts/usb-android-test.sh deploy       # skip build, just deploy + launch
#   ./scripts/usb-android-test.sh logs         # just watch logs
#   ./scripts/usb-android-test.sh register     # just re-register issuer with backend
#   ./scripts/usb-android-test.sh build        # just build APK
#   ./scripts/usb-android-test.sh restart      # restart backend + re-register issuer + relaunch app
#   ./scripts/usb-android-test.sh config       # generate USB-specific vc-config only

set -euo pipefail

# ── Configuration ────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEVENV_DIR="$(dirname "$SCRIPT_DIR")"
WORKSPACE="$(dirname "$DEVENV_DIR")"

SDK_DIR="${WORKSPACE}/siros-sdk-kotlin"
WSCD_DIR="${WORKSPACE}/siros-wscd-manager"
APK="${SDK_DIR}/sample-app/build/outputs/apk/debug/sample-app-debug.apk"
PACKAGE="org.sirosfoundation.sdk.sample"
ACTIVITY="${PACKAGE}/.MainActivity"

export JAVA_HOME="${JAVA_HOME:-/usr/lib/jvm/java-17-openjdk-amd64}"
export ANDROID_HOME="${ANDROID_HOME:-$HOME/Android/Sdk}"

# Prefer adb on PATH, fall back to ANDROID_HOME
if command -v adb &>/dev/null; then
    ADB="adb"
elif [[ -x "$ANDROID_HOME/platform-tools/adb" ]]; then
    ADB="$ANDROID_HOME/platform-tools/adb"
else
    echo "ERROR: adb not found. Install Android SDK platform-tools or set ANDROID_HOME." >&2
    exit 1
fi

# Optional: target a specific device when multiple are connected
ADB_SERIAL="${ADB_SERIAL:-}"
if [[ -n "$ADB_SERIAL" ]]; then
    ADB="$ADB -s $ADB_SERIAL"
fi

ADMIN_URL="${ADMIN_URL:-http://localhost:8081}"
ADMIN_TOKEN="${ADMIN_TOKEN:-e2e-test-admin-token-for-testing-purposes-only}"
TENANT_ID="${TENANT_ID:-default}"
ISSUER_ID="${ISSUER_ID:-http://vc-apigw:8080}"
ISSUER_CLIENT_ID="${ISSUER_CLIENT_ID:-e2e-test-client}"

# Ports to forward via adb reverse (device localhost → host)
# These match the services in docker-compose.test.yml and overlays.
USB_FORWARD_PORTS="${USB_FORWARD_PORTS:-3000 8080 8081 8082 9000 9001 9003 9004 9005 9011 9081 9095}"

# Docker Compose files — USB overlay instead of Waydroid overlay
COMPOSE_FILES=(
    -f docker-compose.test.yml
    -f docker-compose.vc-services.yml
    -f docker-compose.go-trust.yml
    -f docker-compose.go-trust-allow.yml
    -f docker-compose.android-usb.yml
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
        fail "No USB-connected Android device found. Check USB cable and debugging authorization."
        echo "  Tip: run 'adb devices' to check connected devices."
        if [[ -n "$ADB_SERIAL" ]]; then
            echo "  ADB_SERIAL is set to '$ADB_SERIAL' — ensure this device is connected."
        fi
        exit 1
    fi
    local device_model
    device_model=$($ADB shell getprop ro.product.model 2>/dev/null | tr -d '\r')
    ok "ADB connected to: ${device_model:-unknown device}"
}

do_setup_port_forwarding() {
    ensure_adb
    info "Setting up adb reverse port forwarding..."
    local count=0
    for port in $USB_FORWARD_PORTS; do
        if $ADB reverse tcp:$port tcp:$port 2>/dev/null; then
            count=$((count + 1))
        else
            warn "Failed to forward port $port"
        fi
    done
    ok "Forwarded $count ports (device localhost:PORT → host localhost:PORT)"
}

do_teardown_port_forwarding() {
    info "Removing all adb reverse port forwarding..."
    $ADB reverse --remove-all 2>/dev/null || true
    ok "Port forwarding removed"
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

do_rebuild() {
    # Full clean rebuild: Rust crates → Android AAR → mavenLocal → Gradle clean build
    if [ ! -d "$WSCD_DIR" ]; then
        fail "siros-wscd-manager not found at $WSCD_DIR"
        exit 1
    fi

    info "Rebuilding Rust crates (cargo clean + aar + publish-local)..."
    cd "$WSCD_DIR"
    cargo clean
    if ! make publish-local; then
        fail "siros-wscd-manager build failed"
        exit 1
    fi
    ok "siros-wscd-manager AAR published to mavenLocal"

    info "Cleaning Gradle build cache..."
    cd "$SDK_DIR"
    ./gradlew clean 2>&1 | tail -3

    info "Building sample-app APK (clean)..."
    if ./gradlew :sample-app:assembleDebug 2>&1 | tail -3; then
        ok "Full rebuild successful"
    else
        fail "Gradle build failed"
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
    info "Generating USB Android VC config (gateway=localhost via adb reverse)..."
    if ! command -v python3 >/dev/null 2>&1; then
        fail "python3 is required to generate fixtures/vc-config-android-usb.yaml"
        exit 1
    fi

    cd "$DEVENV_DIR"
    python3 "$ANDROID_CONFIG_SCRIPT" \
        --gateway "localhost" \
        --output "fixtures/vc-config-android-usb.yaml"
    ok "Generated fixtures/vc-config-android-usb.yaml"
}

do_restart_backend() {
    info "Restarting USB Android test stack with compose overlay..."
    cd "$DEVENV_DIR"
    docker compose "${COMPOSE_FILES[@]}" up -d --force-recreate \
        mini-oidc vc-apigw wallet-proxy wallet-backend 2>&1 | tail -6
    ok "USB Android test services restarted"
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

do_status() {
    ensure_adb
    info "Device info:"
    echo "  Model:   $($ADB shell getprop ro.product.model | tr -d '\r')"
    echo "  Android: $($ADB shell getprop ro.build.version.release | tr -d '\r')"
    echo "  SDK:     $($ADB shell getprop ro.build.version.sdk | tr -d '\r')"
    echo ""
    info "Port forwarding:"
    local forwards
    forwards=$($ADB reverse --list 2>/dev/null || echo "(none)")
    if [[ -z "$forwards" || "$forwards" == "(none)" ]]; then
        warn "  No reverse port forwarding active (run: $0 setup)"
    else
        echo "$forwards" | sed 's/^/  /'
    fi
    echo ""
    info "App status:"
    if $ADB shell pm list packages 2>/dev/null | grep -q "$PACKAGE"; then
        ok "  $PACKAGE is installed"
    else
        warn "  $PACKAGE is NOT installed"
    fi
}

# ── Main ─────────────────────────────────────────────────────────────────────

CMD="${1:-full}"

case "$CMD" in
    setup)
        do_setup_port_forwarding
        ;;
    teardown)
        do_teardown_port_forwarding
        ;;
    status)
        do_status
        ;;
    build)
        do_build
        ;;
    rebuild)
        do_rebuild
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
        do_setup_port_forwarding
        do_launch
        sleep 2
        do_logs_snapshot
        ;;
    config)
        do_generate_config
        ;;
    full|"")
        do_generate_config
        do_setup_port_forwarding
        if [[ "${SDK_REBUILD:-}" == "yes" ]]; then
            do_rebuild
        else
            do_build
        fi
        do_deploy
        do_register_issuer
        do_launch
        sleep 3
        do_logs_snapshot
        ;;
    *)
        echo "Usage: $0 {full|setup|teardown|status|config|build|rebuild|deploy|launch|register|restart|logs|snapshot}"
        exit 1
        ;;
esac
