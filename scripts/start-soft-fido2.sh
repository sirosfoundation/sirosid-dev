#!/bin/bash
#
# Start soft-fido2 virtual authenticator for E2E testing
#
# This script starts the soft-fido2 virtual authenticator as a background
# daemon. It creates a UHID device that appears as a USB FIDO2 authenticator
# to the browser.
#
# Prerequisites:
#   - Linux with UHID support (modprobe uhid)
#   - User in 'fido' group with UHID permissions
#   - Rust/Cargo installed (for building soft-fido2)
#
# Usage:
#   SOFT_FIDO2_PATH=/path/to/soft-fido2 ./scripts/start-soft-fido2.sh
#
# Environment:
#   SOFT_FIDO2_PATH    - Path to the soft-fido2 repository (required)
#   SOFT_FIDO2_LOG     - Log file path (default: /tmp/soft-fido2.log)
#   SOFT_FIDO2_PID     - PID file path (default: /tmp/soft-fido2.pid)
#   SOFT_FIDO2_REBUILD - Set to 1 to force rebuild even if binary exists

set -e

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Configuration
SOFT_FIDO2_PATH="${SOFT_FIDO2_PATH:-}"
SOFT_FIDO2_LOG="${SOFT_FIDO2_LOG:-/tmp/soft-fido2.log}"
SOFT_FIDO2_PID="${SOFT_FIDO2_PID:-/tmp/soft-fido2.pid}"
SOFT_FIDO2_TIMEOUT="${SOFT_FIDO2_TIMEOUT:-10}"

# Check if path is provided
if [ -z "$SOFT_FIDO2_PATH" ]; then
    echo -e "${RED}ERROR: SOFT_FIDO2_PATH environment variable not set${NC}"
    echo "Usage: SOFT_FIDO2_PATH=/path/to/soft-fido2 $0"
    exit 1
fi

# Resolve to absolute path
SOFT_FIDO2_PATH="$(cd "$SOFT_FIDO2_PATH" 2>/dev/null && pwd)"
if [ ! -d "$SOFT_FIDO2_PATH" ]; then
    echo -e "${RED}ERROR: SOFT_FIDO2_PATH does not exist: $SOFT_FIDO2_PATH${NC}"
    exit 1
fi

# Path to the binary
BINARY="$SOFT_FIDO2_PATH/target/release/examples/virtual_authenticator"

# Force rebuild if SOFT_FIDO2_REBUILD is set
if [ "${SOFT_FIDO2_REBUILD:-0}" = "1" ] && [ -x "$BINARY" ]; then
    echo -e "${YELLOW}Rebuilding soft-fido2 (SOFT_FIDO2_REBUILD=1)...${NC}"
    (cd "$SOFT_FIDO2_PATH" && cargo build --release -p soft-fido2 --example virtual_authenticator)
fi

# Check if binary exists, try to build if not
if [ ! -x "$BINARY" ]; then
    echo -e "${YELLOW}Virtual authenticator binary not found, building...${NC}"
    (cd "$SOFT_FIDO2_PATH" && cargo build --release -p soft-fido2 --example virtual_authenticator)
    if [ ! -x "$BINARY" ]; then
        echo -e "${RED}ERROR: Failed to build virtual_authenticator${NC}"
        exit 1
    fi
fi

# Check UHID module
if ! lsmod | grep -q uhid; then
    echo -e "${YELLOW}Loading UHID kernel module...${NC}"
    if ! sudo modprobe uhid 2>/dev/null; then
        echo -e "${RED}ERROR: Failed to load UHID module. Run: sudo modprobe uhid${NC}"
        exit 1
    fi
fi

# Check if already running
if [ -f "$SOFT_FIDO2_PID" ]; then
    OLD_PID=$(cat "$SOFT_FIDO2_PID")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo -e "${YELLOW}soft-fido2 already running (PID: $OLD_PID)${NC}"
        exit 0
    else
        echo -e "${YELLOW}Removing stale PID file${NC}"
        rm -f "$SOFT_FIDO2_PID"
    fi
fi

# Clear old log
> "$SOFT_FIDO2_LOG"

echo -e "${GREEN}Starting soft-fido2 virtual authenticator...${NC}"
echo "  Binary: $BINARY"
echo "  Log: $SOFT_FIDO2_LOG"
echo "  PID file: $SOFT_FIDO2_PID"

# Start the authenticator in background
nohup "$BINARY" > "$SOFT_FIDO2_LOG" 2>&1 &
AUTHENTICATOR_PID=$!
echo "$AUTHENTICATOR_PID" > "$SOFT_FIDO2_PID"

# Wait for it to be ready (look for "Authenticator Ready" in log)
echo -n "  Waiting for authenticator to be ready"
for i in $(seq 1 $SOFT_FIDO2_TIMEOUT); do
    if grep -q "Authenticator Ready" "$SOFT_FIDO2_LOG" 2>/dev/null; then
        echo ""
        echo -e "${GREEN}✓ soft-fido2 virtual authenticator started (PID: $AUTHENTICATOR_PID)${NC}"
        exit 0
    fi
    # Check if process died
    if ! kill -0 "$AUTHENTICATOR_PID" 2>/dev/null; then
        echo ""
        echo -e "${RED}ERROR: Authenticator process died${NC}"
        echo "Log output:"
        cat "$SOFT_FIDO2_LOG"
        rm -f "$SOFT_FIDO2_PID"
        exit 1
    fi
    echo -n "."
    sleep 1
done

echo ""
echo -e "${RED}ERROR: Timeout waiting for authenticator to start${NC}"
echo "Log output:"
cat "$SOFT_FIDO2_LOG"
kill "$AUTHENTICATOR_PID" 2>/dev/null || true
rm -f "$SOFT_FIDO2_PID"
exit 1
