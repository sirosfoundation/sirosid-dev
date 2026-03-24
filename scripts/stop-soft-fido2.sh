#!/bin/bash
#
# Stop soft-fido2 virtual authenticator
#
# Usage:
#   ./scripts/stop-soft-fido2.sh
#
# Environment:
#   SOFT_FIDO2_PID - PID file path (default: /tmp/soft-fido2.pid)

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
NC='\033[0m'

SOFT_FIDO2_PID="${SOFT_FIDO2_PID:-/tmp/soft-fido2.pid}"
SOFT_FIDO2_LOG="${SOFT_FIDO2_LOG:-/tmp/soft-fido2.log}"

if [ ! -f "$SOFT_FIDO2_PID" ]; then
    echo -e "${YELLOW}soft-fido2 not running (no PID file)${NC}"
    exit 0
fi

PID=$(cat "$SOFT_FIDO2_PID")

if kill -0 "$PID" 2>/dev/null; then
    echo -e "${YELLOW}Stopping soft-fido2 (PID: $PID)...${NC}"
    kill "$PID" 2>/dev/null || true
    
    # Wait for graceful shutdown
    for i in $(seq 1 5); do
        if ! kill -0 "$PID" 2>/dev/null; then
            break
        fi
        sleep 1
    done
    
    # Force kill if still running
    if kill -0 "$PID" 2>/dev/null; then
        echo -e "${YELLOW}Force killing...${NC}"
        kill -9 "$PID" 2>/dev/null || true
    fi
    
    echo -e "${GREEN}✓ soft-fido2 stopped${NC}"
else
    echo -e "${YELLOW}soft-fido2 process not running (stale PID file)${NC}"
fi

rm -f "$SOFT_FIDO2_PID"
