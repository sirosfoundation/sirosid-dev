#!/usr/bin/env bash
# generate-vc-tunnel-config.sh — Patch vc-config.yaml with tunnel URLs
#
# Reads the base fixtures/vc-config.yaml and replaces the verifier and apigw
# public URLs with Cloudflare tunnel URLs from .env.tunnel.
#
# Output: fixtures/.vc-config.tunnel.yaml (gitignored)
#
# Usage:
#   ./scripts/generate-vc-tunnel-config.sh
#
# Requires: .env.tunnel with TUNNEL_VC_VERIFIER_URL and TUNNEL_VC_APIGW_URL

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

BASE_CONFIG="${PROJECT_DIR}/fixtures/vc-config.yaml"
OUTPUT_CONFIG="${PROJECT_DIR}/fixtures/.vc-config.tunnel.yaml"
TUNNEL_ENV="${PROJECT_DIR}/.env.tunnel"

if [[ ! -f "$TUNNEL_ENV" ]]; then
    echo "Error: .env.tunnel not found. Run 'make tunnel' first." >&2
    exit 1
fi

# shellcheck disable=SC1090
source "$TUNNEL_ENV"

if [[ -z "${TUNNEL_VC_VERIFIER_URL:-}" ]]; then
    echo "Error: TUNNEL_VC_VERIFIER_URL not set in .env.tunnel." >&2
    echo "  Tunnels may have been created without VC services running." >&2
    echo "  Run: TUNNEL_VC=yes make tunnel" >&2
    exit 1
fi

if [[ ! -f "$BASE_CONFIG" ]]; then
    echo "Error: Base config not found: ${BASE_CONFIG}" >&2
    exit 1
fi

# Patch the config:
# 1. Verifier public_url: http://localhost:9001 → tunnel URL
# 2. Verifier OIDC issuer: http://localhost:9001 → tunnel URL
# 3. Verifier token_endpoint: http://localhost:9001/token → tunnel URL/token
# 4. Verifier redirect_uri for wallet frontend: use tunnel frontend URL
# 5. APIGW public_url and related fields: use tunnel apigw URL (if set)
sed \
    -e "s|public_url: \"http://localhost:9001\"|public_url: \"${TUNNEL_VC_VERIFIER_URL}\"|g" \
    -e "s|issuer: \"http://localhost:9001\"|issuer: \"${TUNNEL_VC_VERIFIER_URL}\"|g" \
    -e "s|token_endpoint: \"http://localhost:9001/token\"|token_endpoint: \"${TUNNEL_VC_VERIFIER_URL}/token\"|g" \
    "$BASE_CONFIG" > "$OUTPUT_CONFIG"

# If APIGW tunnel URL is available, also patch the apigw public_url
# (currently uses https://vc-proxy:8443 which is Docker-internal)
if [[ -n "${TUNNEL_VC_APIGW_URL:-}" ]]; then
    tmp=$(mktemp)
    sed \
        -e "s|public_url: \"https://vc-proxy:8443\"|public_url: \"${TUNNEL_VC_APIGW_URL}\"|g" \
        -e "s|token_endpoint: \"https://vc-proxy:8443/token\"|token_endpoint: \"${TUNNEL_VC_APIGW_URL}/token\"|g" \
        -e "s|issuer_url: \"https://vc-proxy:8443\"|issuer_url: \"${TUNNEL_VC_APIGW_URL}\"|g" \
        "$OUTPUT_CONFIG" > "$tmp" && mv "$tmp" "$OUTPUT_CONFIG"
fi

# Patch redirect URIs to use the frontend tunnel URL if available
if [[ -n "${TUNNEL_FRONTEND_URL:-}" ]]; then
    tmp=$(mktemp)
    sed \
        -e "s|redirect_uri: \"http://localhost:3000/cb\"|redirect_uri: \"${TUNNEL_FRONTEND_URL}/cb\"|g" \
        -e "s|redirect_uri: \"http://localhost:3000/callback\"|redirect_uri: \"${TUNNEL_FRONTEND_URL}/callback\"|g" \
        "$OUTPUT_CONFIG" > "$tmp" && mv "$tmp" "$OUTPUT_CONFIG"
fi

echo "Generated tunnel VC config: ${OUTPUT_CONFIG}"
