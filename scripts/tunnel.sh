#!/usr/bin/env bash
# tunnel.sh — Create Cloudflare quick tunnels for the dev environment.
#
# Starts cloudflared quick tunnels for the frontend and backend services,
# extracts the assigned *.trycloudflare.com URLs, writes them to .env.tunnel,
# and optionally restarts the stack with the tunnel URLs injected.
#
# Quick tunnels require no Cloudflare account — they create temporary
# *.trycloudflare.com domains with automatic TLS.
#
# Prerequisites:
#   - cloudflared installed (https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)
#   - Services already running (make up)
#
# Usage:
#   ./scripts/tunnel.sh              # Start tunnels and write .env.tunnel
#   ./scripts/tunnel.sh --restart    # Start tunnels and restart stack with tunnel URLs
#   ./scripts/tunnel.sh --stop       # Stop running tunnels
#   ./scripts/tunnel.sh --status     # Show tunnel status and URLs
#
# Output:
#   .env.tunnel       — Shell-sourceable file with tunnel URLs
#   .tunnel.pids      — PID file for cleanup

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
TUNNEL_ENV_FILE=".env.tunnel"
TUNNEL_PID_FILE=".tunnel.pids"
TUNNEL_LOG_DIR=".tunnel-logs"

FRONTEND_PORT="${FRONTEND_PORT:-3000}"
BACKEND_PORT="${BACKEND_PORT:-8080}"
ENGINE_PORT="${ENGINE_PORT:-8082}"
FACETEC_API_PORT="${FACETEC_API_PORT:-8085}"
VC_APIGW_PORT="${VC_APIGW_PORT:-9003}"

# ---------------------------------------------------------------------------
# Colors
# ---------------------------------------------------------------------------
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${GREEN}[tunnel]${NC} $*" >&2; }
warn()  { echo -e "${YELLOW}[tunnel]${NC} $*" >&2; }
error() { echo -e "${RED}[tunnel]${NC} $*" >&2; }
ok()    { echo -e "${GREEN}[tunnel] ✓${NC} $*" >&2; }

# ---------------------------------------------------------------------------
# Prerequisite check
# ---------------------------------------------------------------------------
check_cloudflared() {
    if ! command -v cloudflared >/dev/null 2>&1; then
        error "cloudflared not found. Install it:"
        error "  Linux:  curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /usr/local/bin/cloudflared && chmod +x /usr/local/bin/cloudflared"
        error "  macOS:  brew install cloudflared"
        error "  Docs:   https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
        exit 1
    fi
}

# ---------------------------------------------------------------------------
# Start a single quick tunnel and extract the URL
# ---------------------------------------------------------------------------
start_tunnel() {
    local name="$1"
    local port="$2"
    local logfile="${TUNNEL_LOG_DIR}/${name}.log"

    info "Starting tunnel for ${name} (localhost:${port})..."

    cloudflared tunnel --url "http://localhost:${port}" \
        --no-autoupdate \
        > "$logfile" 2>&1 &
    local pid=$!
    echo "${name}:${pid}" >> "$TUNNEL_PID_FILE"

    # Wait for the tunnel URL to appear in the log (up to 30s)
    local url=""
    for i in $(seq 1 30); do
        url=$(grep -aoE 'https://[a-z0-9-]+\.trycloudflare\.com' "$logfile" 2>/dev/null | head -1) || true
        if [[ -n "$url" ]]; then
            break
        fi
        sleep 1
    done

    if [[ -z "$url" ]]; then
        error "Failed to get tunnel URL for ${name} after 30s"
        error "Check ${logfile} for details"
        return 1
    fi

    echo "$url"
}

# ---------------------------------------------------------------------------
# Stop all tunnels
# ---------------------------------------------------------------------------
stop_tunnels() {
    if [[ ! -f "$TUNNEL_PID_FILE" ]]; then
        info "No tunnels running"
        return 0
    fi

    info "Stopping tunnels..."
    while IFS=: read -r name pid; do
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true
            ok "Stopped ${name} tunnel (PID ${pid})"
        fi
    done < "$TUNNEL_PID_FILE"

    rm -f "$TUNNEL_PID_FILE" "$TUNNEL_ENV_FILE"
    info "Tunnels stopped. Run 'make up' to restore localhost URLs."
}

# ---------------------------------------------------------------------------
# Show tunnel status
# ---------------------------------------------------------------------------
show_status() {
    if [[ ! -f "$TUNNEL_ENV_FILE" ]]; then
        info "No tunnels configured"
        return 0
    fi

    echo -e "${CYAN}Active Cloudflare Tunnels:${NC}"
    # shellcheck disable=SC1090
    source "$TUNNEL_ENV_FILE"
    echo "  Frontend:    ${TUNNEL_FRONTEND_URL:-not set}"
    echo "  Backend:     ${TUNNEL_BACKEND_URL:-not set}"
    echo "  Engine:      ${TUNNEL_ENGINE_URL:-not set}"
    echo "  facetec-api: ${TUNNEL_FACETEC_API_URL:-not set}"
    echo "  vc-apigw:    ${TUNNEL_VC_APIGW_URL:-not set}"
    echo ""

    if [[ -f "$TUNNEL_PID_FILE" ]]; then
        echo -e "${CYAN}Processes:${NC}"
        while IFS=: read -r name pid; do
            if kill -0 "$pid" 2>/dev/null; then
                echo -e "  ${name}: ${GREEN}running${NC} (PID ${pid})"
            else
                echo -e "  ${name}: ${RED}stopped${NC}"
            fi
        done < "$TUNNEL_PID_FILE"
    fi

    echo ""
    echo "To use these URLs:"
    echo "  make up TUNNELS=yes"
}

# ---------------------------------------------------------------------------
# Ensure tunnels exist and are running
# ---------------------------------------------------------------------------
ensure_tunnels() {
    check_cloudflared

    local running=true
    if [[ ! -f "$TUNNEL_ENV_FILE" || ! -f "$TUNNEL_PID_FILE" ]]; then
        running=false
    else
        while IFS=: read -r _name pid; do
            if ! kill -0 "$pid" 2>/dev/null; then
                running=false
                break
            fi
        done < "$TUNNEL_PID_FILE"
    fi

    if [[ "$running" == "true" ]]; then
        info "Reusing existing Cloudflare tunnels from ${TUNNEL_ENV_FILE}"
        return 0
    fi

    warn "No active tunnels found. Creating fresh tunnels..."
    stop_tunnels >/dev/null 2>&1 || true
    start_tunnels
}

# ---------------------------------------------------------------------------
# Start all tunnels
# ---------------------------------------------------------------------------
start_tunnels() {
    check_cloudflared

    # Stop any existing tunnels
    if [[ -f "$TUNNEL_PID_FILE" ]]; then
        stop_tunnels
    fi

    mkdir -p "$TUNNEL_LOG_DIR"
    > "$TUNNEL_PID_FILE"

    info "Creating Cloudflare quick tunnels (no account required)..."
    echo ""

    local frontend_url backend_url engine_url facetec_api_url vc_apigw_url

    frontend_url=$(start_tunnel "frontend" "$FRONTEND_PORT")
    backend_url=$(start_tunnel "backend" "$BACKEND_PORT")
    engine_url=$(start_tunnel "engine" "$ENGINE_PORT")
    facetec_api_url=$(start_tunnel "facetec-api" "$FACETEC_API_PORT")
    vc_apigw_url=$(start_tunnel "vc-apigw" "$VC_APIGW_PORT")

    # Write .env.tunnel
        local tunnel_rpid
        tunnel_rpid=$(printf '%s' "$frontend_url" | sed 's|https://||')
    cat > "$TUNNEL_ENV_FILE" <<EOF
# Cloudflare tunnel URLs — generated by scripts/tunnel.sh
    # Reused automatically by: make up TUNNELS=yes
TUNNEL_FRONTEND_URL=${frontend_url}
TUNNEL_BACKEND_URL=${backend_url}
TUNNEL_ENGINE_URL=${engine_url}
TUNNEL_FACETEC_API_URL=${facetec_api_url}
TUNNEL_VC_APIGW_URL=${vc_apigw_url}
    TUNNEL_RPID=${tunnel_rpid}
EOF

    echo ""
    ok "Tunnels created successfully!"
    echo ""
    echo -e "  ${CYAN}Frontend:${NC}    ${frontend_url}"
    echo -e "  ${CYAN}Backend:${NC}     ${backend_url}"
    echo -e "  ${CYAN}Engine:${NC}      ${engine_url}"
    echo -e "  ${CYAN}facetec-api:${NC} ${facetec_api_url}"
    echo -e "  ${CYAN}vc-apigw:${NC}    ${vc_apigw_url}"
    echo ""
    info "URLs written to ${TUNNEL_ENV_FILE}"
    info "Run 'make up TUNNELS=yes' to reconfigure services with tunnel URLs"
    info "For the wallet Android app, build with:"
    info "  WWWALLET_FACETEC_API_BASE_URL=\"${facetec_api_url}/process-request\""
    info "vc-apigw's config (credential_issuer, token_endpoint, CORS) is regenerated"
    info "automatically by 'make up TUNNELS=yes VC=yes' — see scripts/generate-tunnel-config.py"
    info "Run 'make tunnel-stop' to tear down tunnels"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
case "${1:-start}" in
    start|--start)    start_tunnels ;;
    ensure|--ensure)  ensure_tunnels ;;
    stop|--stop)      stop_tunnels ;;
    status|--status)  show_status ;;
    restart|--restart)
        start_tunnels
        info "Restarting stack with tunnel URLs..."
        # This is handled by the Makefile target
        ;;
    *)
        echo "Usage: $0 [start|ensure|stop|status|restart]"
        exit 1
        ;;
esac
