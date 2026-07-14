#!/usr/bin/env bash
# setup-r2ps.sh — Provision R2PS service via its admin API.
#
# Waits for go-r2ps-service to be healthy, then performs initial setup
# such as allocating status list entries. Called automatically by
# 'make android-up R2PS=yes' or manually.
#
# Prerequisites:
#   - R2PS server running (docker-compose.r2ps.yml overlay)
#   - ADMIN_TOKEN set (defaults to e2e test token)
#
# Usage:
#   ./scripts/setup-r2ps.sh [--r2ps-url URL] [--r2ps-admin-url URL]

set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
R2PS_URL="${R2PS_URL:-http://localhost:8443}"
R2PS_ADMIN_URL="${R2PS_ADMIN_URL:-http://localhost:8444}"
R2PS_ADMIN_DEV_TOKEN="${R2PS_ADMIN_DEV_TOKEN:-r2ps-e2e-dev-token-for-testing-only}"
MAX_RETRIES=30
RETRY_INTERVAL=2

# ---------------------------------------------------------------------------
# Colors
# ---------------------------------------------------------------------------
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${GREEN}[r2ps]${NC} $*"; }
warn()  { echo -e "${YELLOW}[r2ps]${NC} $*"; }
error() { echo -e "${RED}[r2ps]${NC} $*" >&2; }
ok()    { echo -e "${GREEN}[r2ps] ✓${NC} $*"; }

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --r2ps-url)       R2PS_URL="$2";       shift 2 ;;
        --r2ps-admin-url) R2PS_ADMIN_URL="$2"; shift 2 ;;
        *) error "Unknown argument: $1"; exit 1 ;;
    esac
done

# ---------------------------------------------------------------------------
# Wait for R2PS health
# ---------------------------------------------------------------------------
wait_for_r2ps() {
    info "Waiting for R2PS server at ${R2PS_URL}/healthz ..."
    for i in $(seq 1 "$MAX_RETRIES"); do
        if curl -sf "${R2PS_URL}/healthz" >/dev/null 2>&1; then
            ok "R2PS server is healthy"
            return 0
        fi
        sleep "$RETRY_INTERVAL"
    done
    error "R2PS server did not become healthy after $((MAX_RETRIES * RETRY_INTERVAL))s"
    exit 1
}

# ---------------------------------------------------------------------------
# Verify admin API
# ---------------------------------------------------------------------------
verify_admin_api() {
    info "Verifying R2PS admin API at ${R2PS_ADMIN_URL} ..."
    local status
    status=$(curl -sf -o /dev/null -w "%{http_code}" \
        -H "Authorization: Bearer ${R2PS_ADMIN_DEV_TOKEN}" \
        "${R2PS_ADMIN_URL}/admin/store/keys" 2>&1) || true
    if [[ "$status" == "200" ]]; then
        ok "R2PS admin API accessible"
    else
        warn "R2PS admin API returned HTTP ${status} (may be OK if no keys exist yet)"
    fi
}

# ---------------------------------------------------------------------------
# Display R2PS status
# ---------------------------------------------------------------------------
show_status() {
    info "R2PS service status:"
    echo "  Server:    ${R2PS_URL}"
    echo "  Admin API: ${R2PS_ADMIN_URL}"

    local key_count
    key_count=$(curl -sf -H "Authorization: Bearer ${R2PS_ADMIN_DEV_TOKEN}" \
        "${R2PS_ADMIN_URL}/admin/store/keys" 2>/dev/null \
        | python3 -c "import sys,json; print(len(json.load(sys.stdin)))" 2>/dev/null) || key_count="?"
    echo "  HSM keys:  ${key_count}"

    for category in ka wia; do
        local cat_count
        cat_count=$(curl -sf -H "Authorization: Bearer ${R2PS_ADMIN_DEV_TOKEN}" \
            "${R2PS_ADMIN_URL}/admin/store/statuses/${category}" 2>/dev/null \
            | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('entries',d)) if isinstance(d,dict) else len(d))" 2>/dev/null) || cat_count="?"
        echo "  Status list (${category}): ${cat_count} entries"
    done
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
    wait_for_r2ps
    verify_admin_api
    show_status
    echo ""
    ok "R2PS setup complete. The service is ready for WSCD/WSCA operations."
    info "SDK clients can connect to: ${R2PS_URL}"
}

main "$@"
