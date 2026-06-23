#!/usr/bin/env bash
# setup-android.sh — Configure the local dev environment for Android SDK testing.
#
# What this does:
#   1. Generates .well-known/assetlinks.json from your debug keystore
#   2. Configures ADB to bypass HTTPS Digital Asset Links validation
#      (required because the local dev env runs over plain HTTP)
#
# Prerequisites (all optional — the script skips steps it can't do):
#   - Android SDK with adb and keytool on PATH (or ANDROID_HOME set)
#   - A running Android device or emulator connected via adb
#   - ~/.android/debug.keystore (created automatically by Android Studio / Gradle)
#
# Usage:
#   ./scripts/setup-android.sh                    # auto-detect everything
#   ./scripts/setup-android.sh --package com.x.y  # custom package name
#   ./scripts/setup-android.sh --fingerprint XX:YY:ZZ  # provide fingerprint directly

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
WELL_KNOWN_DIR="$PROJECT_DIR/.well-known"

# Defaults
PACKAGE_NAME="org.sirosfoundation.sdk.sample"
FINGERPRINT=""
DEBUG_KEYSTORE="${HOME}/.android/debug.keystore"
SKIP_ADB=false

# Colors
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${GREEN}$*${NC}"; }
warn()  { echo -e "${YELLOW}$*${NC}"; }
error() { echo -e "${RED}$*${NC}" >&2; }

usage() {
    echo "Usage: $0 [OPTIONS]"
    echo ""
    echo "Configure the local dev environment for Android SDK testing."
    echo ""
    echo "Options:"
    echo "  --package NAME        Android package name (default: $PACKAGE_NAME)"
    echo "  --fingerprint HEX     SHA256 cert fingerprint (auto-detected from debug.keystore)"
    echo "  --keystore PATH       Path to debug keystore (default: ~/.android/debug.keystore)"
    echo "  --skip-adb            Only generate assetlinks.json, skip ADB configuration"
    echo "  -h, --help            Show this help"
    exit 0
}

# Parse arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        --package)     PACKAGE_NAME="$2"; shift 2 ;;
        --fingerprint) FINGERPRINT="$2"; shift 2 ;;
        --keystore)    DEBUG_KEYSTORE="$2"; shift 2 ;;
        --skip-adb)    SKIP_ADB=true; shift ;;
        -h|--help)     usage ;;
        *)             error "Unknown option: $1"; usage ;;
    esac
done

# ── Step 1: Find tools ──────────────────────────────────────────────

# Resolve adb
ADB=""
if command -v adb &>/dev/null; then
    ADB="adb"
elif [[ -n "${ANDROID_HOME:-}" ]] && [[ -x "$ANDROID_HOME/platform-tools/adb" ]]; then
    ADB="$ANDROID_HOME/platform-tools/adb"
fi

# Resolve keytool (ships with JDK, not Android SDK)
KEYTOOL=""
if command -v keytool &>/dev/null; then
    KEYTOOL="keytool"
elif [[ -n "${JAVA_HOME:-}" ]] && [[ -x "$JAVA_HOME/bin/keytool" ]]; then
    KEYTOOL="$JAVA_HOME/bin/keytool"
fi

# ── Step 2: Extract fingerprint ─────────────────────────────────────

if [[ -z "$FINGERPRINT" ]]; then
    if [[ -z "$KEYTOOL" ]]; then
        warn "keytool not found (install a JDK or set JAVA_HOME)."
        warn "Skipping fingerprint extraction."
    elif [[ ! -f "$DEBUG_KEYSTORE" ]]; then
        warn "Debug keystore not found at $DEBUG_KEYSTORE"
        warn "Build an Android project first, or provide --fingerprint."
    else
        info "Extracting SHA256 fingerprint from $DEBUG_KEYSTORE..."
        FINGERPRINT=$("$KEYTOOL" -list -v \
            -keystore "$DEBUG_KEYSTORE" \
            -alias androiddebugkey \
            -storepass android 2>/dev/null \
            | grep 'SHA256:' \
            | sed 's/.*SHA256: //' \
            | tr -d '[:space:]')

        if [[ -n "$FINGERPRINT" ]]; then
            info "  Fingerprint: $FINGERPRINT"
        else
            warn "Could not extract fingerprint from keystore."
        fi
    fi
fi

# ── Step 3: Generate assetlinks.json ─────────────────────────────────

if [[ -n "$FINGERPRINT" ]]; then
    # Compute the base64url APK key hash used in WALLET_SERVER_RP_ORIGINS.
    # Format: remove colon separators, hex-decode, base64url-encode (no padding).
    APK_KEY_HASH=$(printf '%s' "$FINGERPRINT" \
        | tr -d ':' \
        | fold -w2 \
        | while IFS= read -r byte; do printf "\\x$byte"; done \
        | base64 \
        | tr '+/' '-_' \
        | tr -d '=')

    mkdir -p "$WELL_KNOWN_DIR"
    cat > "$WELL_KNOWN_DIR/assetlinks.json" <<EOF
[
  {
    "relation": [
      "delegate_permission/common.handle_all_urls",
      "delegate_permission/common.get_login_creds"
    ],
    "target": {
      "namespace": "android_app",
      "package_name": "$PACKAGE_NAME",
      "sha256_cert_fingerprints": [
        "$FINGERPRINT"
      ]
    }
  }
]
EOF
    info "Generated $WELL_KNOWN_DIR/assetlinks.json"
    info "  Package: $PACKAGE_NAME"

    # Write .env.android so make up can inject the
    # correct APK key hash into WALLET_SERVER_RP_ORIGINS at runtime.
    cat > "$PROJECT_DIR/.env.android" <<EOF
APK_KEY_HASH=$APK_KEY_HASH
ANDROID_PACKAGE=$PACKAGE_NAME
EOF
    info "Written .env.android (APK key hash: $APK_KEY_HASH)"
    info "  Run 'make up' to apply."
else
    warn "No fingerprint available — skipping assetlinks.json generation."
    warn "Provide one with: $0 --fingerprint 'XX:YY:...:ZZ'"
fi

# ── Step 4: ADB bypass for HTTP Digital Asset Links ─────────────────
#
# Android's Credential Manager / Play Services validates Digital Asset Links
# over HTTPS. Since the dev environment runs over HTTP, we need to bypass
# this check. The `am compat enable` command tells Android to skip the
# signature verification for the specified package.
#
# Note: These compat flags are not available on Waydroid (Android container).
# On Waydroid, passkey development requires additional configuration or
# alternative approaches.

if [[ "$SKIP_ADB" == "true" ]]; then
    info "Skipping ADB configuration (--skip-adb)."
elif [[ -z "$ADB" ]]; then
    warn "adb not found. Skipping ADB configuration."
    warn "When a device is connected, you may need to run:"
    warn "  adb shell am compat enable DEVELOPMENT_PASSKEY_REGISTRATION $PACKAGE_NAME"
else
    # Check if a device is connected
    DEVICES=$("$ADB" devices 2>/dev/null | grep -c 'device$' || true)
    if [[ "$DEVICES" -eq 0 ]]; then
        warn "No Android device/emulator connected."
        warn "Skip this step for now or connect a device."
    else
        # Detect Waydroid or other container environments by attempting the compat flag
        # and checking if it's not available
        info "Configuring ADB for passkey development..."
        
        # Try to enable DEVELOPMENT_PASSKEY_REGISTRATION
        ADB_OUTPUT=$("$ADB" shell am compat enable DEVELOPMENT_PASSKEY_REGISTRATION "$PACKAGE_NAME" 2>&1 || true)
        
        if echo "$ADB_OUTPUT" | grep -qi "Unknown or invalid change"; then
            # Compat flags not supported (typical on Waydroid)
            info "Note: ADB compat flags not supported on this device (typical on Waydroid)"
            info "If passkey registration fails:"
            info "  - This is expected on Waydroid — you may need to test with a real device"
            info "  - Or use alternative credential verification methods"
        elif echo "$ADB_OUTPUT" | grep -qi "error\|failed"; then
            # Some other error occurred
            warn "Unable to configure ADB compat flags."
            warn "You may need to configure manually when this issue is resolved."
        else
            info "  ✓ Enabled DEVELOPMENT_PASSKEY_REGISTRATION for $PACKAGE_NAME"
        fi


        info ""
        info "Done. If passkey registration still fails, ensure:"
        info "  1. The dev stack is running (make up) and assetlinks.json is served"
        info "  2. The app can reach the wallet backend (check network config)"
        info "  3. On Waydroid: manually verify app link delegation if needed"
    fi
fi

echo ""
info "Android dev setup complete."
