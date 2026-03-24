#!/bin/bash
#
# Run E2E tests against a matrix of frontend/backend version combinations
#
# Usage:
#   ./scripts/run-matrix.sh
#   ./scripts/run-matrix.sh --frontend-versions "v1.0.0 v1.1.0" --backend-versions "v1.0.0 v1.1.0"
#
# This script will:
# 1. Iterate through all combinations of frontend and backend versions
# 2. Start the services using docker-compose
# 3. Run the E2E tests
# 4. Save results for each combination
# 5. Generate a summary report

set -e

# Default versions to test
FRONTEND_VERSIONS="${FRONTEND_VERSIONS:-latest}"
BACKEND_VERSIONS="${BACKEND_VERSIONS:-latest}"

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --frontend-versions)
            FRONTEND_VERSIONS="$2"
            shift 2
            ;;
        --backend-versions)
            BACKEND_VERSIONS="$2"
            shift 2
            ;;
        --help)
            echo "Usage: $0 [--frontend-versions \"v1 v2\"] [--backend-versions \"v1 v2\"]"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

# Results directory
RESULTS_DIR="test-results/matrix-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$RESULTS_DIR"

# Summary file
SUMMARY_FILE="$RESULTS_DIR/summary.md"
echo "# E2E Test Matrix Results" > "$SUMMARY_FILE"
echo "" >> "$SUMMARY_FILE"
echo "Run: $(date)" >> "$SUMMARY_FILE"
echo "" >> "$SUMMARY_FILE"
echo "| Frontend | Backend | Status | Duration |" >> "$SUMMARY_FILE"
echo "|----------|---------|--------|----------|" >> "$SUMMARY_FILE"

# Track overall results
TOTAL=0
PASSED=0
FAILED=0

# Run tests for each combination
for frontend in $FRONTEND_VERSIONS; do
    for backend in $BACKEND_VERSIONS; do
        TOTAL=$((TOTAL + 1))
        COMBO_DIR="$RESULTS_DIR/frontend-${frontend}_backend-${backend}"
        mkdir -p "$COMBO_DIR"
        
        echo ""
        echo "=========================================="
        echo "Testing: Frontend $frontend + Backend $backend"
        echo "=========================================="
        
        START_TIME=$(date +%s)
        
        # Set versions and start services
        export FRONTEND_VERSION="$frontend"
        export BACKEND_VERSION="$backend"
        
        # Stop any existing containers
        docker-compose down --remove-orphans 2>/dev/null || true
        
        # Start services
        echo "Starting services..."
        if docker-compose up -d --wait 2>&1 | tee "$COMBO_DIR/docker.log"; then
            echo "Services started successfully"
            
            # Wait a bit for services to be fully ready
            sleep 5
            
            # Run tests
            echo "Running tests..."
            TEST_OUTPUT="$COMBO_DIR/test-output.txt"
            if FRONTEND_URL=http://localhost:3000 BACKEND_URL=http://localhost:8080 \
               npm test -- --reporter=list 2>&1 | tee "$TEST_OUTPUT"; then
                STATUS="✅ PASSED"
                PASSED=$((PASSED + 1))
            else
                STATUS="❌ FAILED"
                FAILED=$((FAILED + 1))
            fi
            
            # Copy test artifacts
            cp -r playwright-report "$COMBO_DIR/" 2>/dev/null || true
            cp -r test-results/* "$COMBO_DIR/" 2>/dev/null || true
        else
            STATUS="❌ FAILED (startup)"
            FAILED=$((FAILED + 1))
        fi
        
        END_TIME=$(date +%s)
        DURATION=$((END_TIME - START_TIME))
        
        # Add to summary
        echo "| $frontend | $backend | $STATUS | ${DURATION}s |" >> "$SUMMARY_FILE"
        
        # Stop services
        docker-compose down --remove-orphans 2>/dev/null || true
    done
done

# Final summary
echo "" >> "$SUMMARY_FILE"
echo "## Summary" >> "$SUMMARY_FILE"
echo "" >> "$SUMMARY_FILE"
echo "- Total combinations tested: $TOTAL" >> "$SUMMARY_FILE"
echo "- Passed: $PASSED" >> "$SUMMARY_FILE"
echo "- Failed: $FAILED" >> "$SUMMARY_FILE"

echo ""
echo "=========================================="
echo "Matrix Test Complete"
echo "=========================================="
echo "Total: $TOTAL | Passed: $PASSED | Failed: $FAILED"
echo "Results saved to: $RESULTS_DIR"
echo ""

# Exit with failure if any tests failed
if [ $FAILED -gt 0 ]; then
    exit 1
fi
