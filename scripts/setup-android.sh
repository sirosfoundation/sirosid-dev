#!/usr/bin/env bash
# setup-android.sh — Configure the local dev environment for Android SDK testing.
#
# What this does:
#   1. Generates .well-known/assetlinks.json with the target package's REAL signing
#      certificate — preferring the actually-installed APK (via adb + apksigner) over
#      guessing from the generic ~/.android/debug.keystore, since apps built with a
#      custom keystore (as opposed to the Android Studio default) have a different
#      cert entirely. Using the wrong cert here makes passkey creation fail before
#      the app even shows a biometric prompt.
#   2. Configures ADB to bypass HTTPS Digital Asset Links validation
#      (required because the local dev env runs over plain HTTP)
#
# Fingerprint resolution order:
#   1. --fingerprint, if given explicitly
#   2. The installed APK's actual signing cert, via `adb` + `apksigner` (falls back to
#      `keytool -printcert -jarfile` if apksigner isn't found, though that only detects
#      the older JAR/v1 signing scheme — most modern Gradle builds are v2/v3-only, so
#      apksigner is strongly preferred)
#   3. ~/.android/debug.keystore (only correct if the app is actually signed with the
#      stock Android Studio debug key, which custom-keystore apps are not)
#
# Prerequisites (all optional — the script skips steps it can't do):
#   - Android SDK with adb and apksigner on PATH, or ANDROID_HOME set, or installed at
#     the default ~/Library/Android/sdk (macOS) / ~/Android/Sdk (Linux) location
#   - A running Android device or emulator connected via adb, with the target package
#     installed (for method 2 above)
#   - ~/.android/debug.keystore (created automatically by Android Studio / Gradle, only
#     used as the last-resort fallback)
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
# Common Android SDK install locations, checked when ANDROID_HOME isn't set.
SDK_FALLBACK_PATHS=(
    "${HOME}/Library/Android/sdk"
    "${HOME}/Android/Sdk"
)

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

# Resolve adb: PATH, then ANDROID_HOME, then common SDK install locations.
ADB=""
if command -v adb &>/dev/null; then
    ADB="adb"
elif [[ -n "${ANDROID_HOME:-}" ]] && [[ -x "$ANDROID_HOME/platform-tools/adb" ]]; then
    ADB="$ANDROID_HOME/platform-tools/adb"
else
    for sdk in "${SDK_FALLBACK_PATHS[@]}"; do
        if [[ -x "$sdk/platform-tools/adb" ]]; then
            ADB="$sdk/platform-tools/adb"
            break
        fi
    done
fi

# Resolve apksigner (ships with Android SDK build-tools): PATH, then ANDROID_HOME,
# then common SDK install locations. Picks the highest installed build-tools version.
APKSIGNER=""
find_apksigner() {
    local root="$1"
    [[ -d "$root/build-tools" ]] || return 1
    # Sort versions descending so the newest build-tools wins.
    local candidate
    candidate=$(find "$root/build-tools" -maxdepth 2 -name apksigner -type f 2>/dev/null | sort -rV | head -1)
    [[ -n "$candidate" && -x "$candidate" ]] && echo "$candidate"
}
if command -v apksigner &>/dev/null; then
    APKSIGNER="apksigner"
elif [[ -n "${ANDROID_HOME:-}" ]]; then
    APKSIGNER=$(find_apksigner "$ANDROID_HOME" || true)
fi
if [[ -z "$APKSIGNER" ]]; then
    for sdk in "${SDK_FALLBACK_PATHS[@]}"; do
        APKSIGNER=$(find_apksigner "$sdk" || true)
        [[ -n "$APKSIGNER" ]] && break
    done
fi

# Resolve keytool (ships with JDK, not Android SDK) — used only as a last-resort
# fallback for extracting a cert, since it can't read APKs signed with the modern
# v2/v3 APK Signature Scheme (most current Gradle builds).
KEYTOOL=""
if command -v keytool &>/dev/null; then
    KEYTOOL="keytool"
elif [[ -n "${JAVA_HOME:-}" ]] && [[ -x "$JAVA_HOME/bin/keytool" ]]; then
    KEYTOOL="$JAVA_HOME/bin/keytool"
fi

# ── Step 2: Extract fingerprint ─────────────────────────────────────
#
# Preferred: pull the actually-installed APK for $PACKAGE_NAME off the connected
# device and read its real signing certificate. This is the only method that's
# correct for apps signed with a custom (non-default-debug) keystore.
extract_fingerprint_from_installed_apk() {
    [[ -n "$ADB" ]] || return 1
    [[ "$("$ADB" devices 2>/dev/null | grep -c 'device$' || true)" -gt 0 ]] || return 1

    local apk_path
    apk_path=$("$ADB" shell pm path "$PACKAGE_NAME" 2>/dev/null | head -1 | sed 's/^package://' | tr -d '\r')
    [[ -n "$apk_path" ]] || return 1

    local tmp_apk
    tmp_apk=$(mktemp /tmp/setup-android-XXXXXX.apk)
    trap 'rm -f "$tmp_apk"' RETURN

    if ! "$ADB" pull "$apk_path" "$tmp_apk" &>/dev/null; then
        return 1
    fi

    local fp=""
    if [[ -n "$APKSIGNER" ]]; then
        fp=$("$APKSIGNER" verify --print-certs "$tmp_apk" 2>/dev/null \
            | grep -i 'SHA-256 digest' | head -1 \
            | sed 's/.*: //' | tr -d '[:space:]' \
            | tr 'a-f' 'A-F' | sed 's/../&:/g; s/:$//')
    elif [[ -n "$KEYTOOL" ]]; then
        fp=$("$KEYTOOL" -printcert -jarfile "$tmp_apk" 2>/dev/null \
            | grep 'SHA256:' | head -1 \
            | sed 's/.*SHA256: //' | tr -d '[:space:]')
    fi

    [[ -n "$fp" ]] || return 1
    echo "$fp"
}

if [[ -z "$FINGERPRINT" ]]; then
    INSTALLED_FP=""
    if INSTALLED_FP=$(extract_fingerprint_from_installed_apk); then
        info "Extracting SHA256 fingerprint from the installed APK for $PACKAGE_NAME..."
        FINGERPRINT="$INSTALLED_FP"
        info "  Fingerprint: $FINGERPRINT (from installed APK — correct even for custom keystores)"
    elif [[ -z "$KEYTOOL" ]]; then
        warn "keytool not found (install a JDK or set JAVA_HOME)."
        warn "Skipping fingerprint extraction."
    elif [[ ! -f "$DEBUG_KEYSTORE" ]]; then
        warn "Debug keystore not found at $DEBUG_KEYSTORE"
        warn "Build an Android project first, or provide --fingerprint."
    else
        warn "Could not read the installed APK's cert (no device connected, package not"
        warn "installed, or adb/apksigner unavailable) — falling back to $DEBUG_KEYSTORE."
        warn "This is only correct if $PACKAGE_NAME is actually signed with the stock"
        warn "Android Studio debug key, not a custom keystore."
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
    # NOTE: the trailing \n in printf is required — without it, `fold`'s last
    # line has no newline, so `while read` (which exits nonzero past EOF) skips
    # the final byte, silently truncating the hash by one byte.
    APK_KEY_HASH=$(printf '%s\n' "$FINGERPRINT" \
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
