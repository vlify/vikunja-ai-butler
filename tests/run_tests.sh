#!/usr/bin/env bash
# ==============================================================================
# run_tests.sh — One-click test runner for Vikunja AI Butler
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

GREEN='\033[0;32m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${BLUE}========================================================================${NC}"
echo -e "${BLUE} Running Vikunja AI Butler Automated Test Suite                          ${NC}"
echo -e "${BLUE}========================================================================${NC}"

FAILED=0

if command -v pytest >/dev/null 2>&1; then
    echo "[INFO] Using pytest test runner:"
    pytest -v tests/ || FAILED=1
else
    echo "[INFO] Using Python standard unittest test runner:"
    python3 -m unittest discover -s tests -v || FAILED=1
fi

echo -e "${BLUE}========================================================================${NC}"
if [[ $FAILED -eq 0 ]]; then
    echo -e "${GREEN}[SUCCESS] All tests passed cleanly! (100% Green)${NC}"
    exit 0
else
    echo -e "${RED}[FAILURE] Some tests failed. Please review the output above.${NC}"
    exit 1
fi
