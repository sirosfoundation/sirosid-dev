#!/usr/bin/env bash
# Generate /.well-known/assetlinks.json from the developer's Android debug keystore.
# Usage: ./generate-assetlinks.sh [output-dir] [package-name]
set -euo pipefail

OUTPUT_DIR="${1:-$(dirname "$0")/../fixtures/well-known}"
PACKAGE="${2:-org.siros.sdk.sample}"
KEYSTORE="${ANDROID_DEBUG_KEYSTORE:-$HOME/.android/debug.keystore}"

if [[ ! -f "$KEYSTORE" ]]; then
  echo "Error: debug keystore not found at $KEYSTORE" >&2
  echo "Set ANDROID_DEBUG_KEYSTORE to override." >&2
  exit 1
fi

FINGERPRINT=$(keytool -list -v -keystore "$KEYSTORE" -alias androiddebugkey \
  -storepass android 2>/dev/null | grep 'SHA256:' | head -1 | sed 's/.*SHA256: //' | tr -d '[:space:]')

if [[ -z "$FINGERPRINT" ]]; then
  echo "Error: could not extract SHA-256 fingerprint from $KEYSTORE" >&2
  exit 1
fi

mkdir -p "$OUTPUT_DIR"
cat > "$OUTPUT_DIR/assetlinks.json" <<EOF
[{
  "relation": ["delegate_permission/common.handle_all_urls", "delegate_permission/common.get_login_creds"],
  "target": {
    "namespace": "android_app",
    "package_name": "$PACKAGE",
    "sha256_cert_fingerprints": ["$FINGERPRINT"]
  }
}]
EOF

echo "Generated $OUTPUT_DIR/assetlinks.json (fingerprint: $FINGERPRINT)"
