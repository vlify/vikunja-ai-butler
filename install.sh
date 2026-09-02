#!/usr/bin/env bash
# ==============================================================================
# install.sh — Setup and Dependency Verification for Vikunja AI Butler
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

INSTALL_UNITS=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --install-units)
            INSTALL_UNITS=1
            shift
            ;;
        -h|--help)
            echo "Usage: $(basename "$0") [options]"
            echo ""
            echo "Options:"
            echo "  --install-units   Install systemd user services and timers"
            echo "  -h, --help        Show this help message"
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            exit 1
            ;;
    esac
done

echo "========================================================================"
echo " Vikunja AI Butler — Environment Verification"
echo "========================================================================"

# 1. Check Python
if ! command -v python3 >/dev/null 2>&1; then
    echo "[FAIL] python3 is required but not installed." >&2
    exit 1
fi

PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "[INFO] Found Python version: $PY_VER"

# 2. Check Optional Dependencies
echo "[INFO] Checking optional integrations:"
if command -v aw-client >/dev/null 2>&1; then
    echo "  - ActivityWatch client: Found ($(command -v aw-client))"
else
    echo "  - ActivityWatch client: Not found (Only required if activitywatch.enabled=true)"
fi

if command -v himalaya >/dev/null 2>&1; then
    echo "  - Himalaya mail client: Found ($(command -v himalaya))"
else
    echo "  - Himalaya mail client: Not found (Only required if notifier.type=himalaya)"
fi

# 3. Check Configuration Files
echo ""
echo "[INFO] Checking configuration files:"
if [[ -f "$SCRIPT_DIR/.env" ]]; then
    echo "  - Credentials (.env): Found"
else
    echo "  - Credentials (.env): Missing. Please copy .env.example to .env and configure credentials."
fi

if [[ -f "$SCRIPT_DIR/config.yaml" || -f "$SCRIPT_DIR/config.json" ]]; then
    echo "  - Configuration (config.yaml/json): Found"
else
    echo "  - Configuration: Missing. Please copy config.example.yaml to config.yaml and customize."
fi

# 4. Optional Systemd Unit Installation
if [[ $INSTALL_UNITS -eq 1 ]]; then
    echo ""
    echo "[INFO] Installing systemd user units to ~/.config/systemd/user/..."
    TARGET_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
    mkdir -p "$TARGET_DIR"

    for f in "$SCRIPT_DIR"/systemd/*; do
        if [[ -f "$f" ]]; then
            bname=$(basename "$f")
            sed "s|%h/Projects/vikunja-ai-butler|$SCRIPT_DIR|g" "$f" > "$TARGET_DIR/$bname"
            echo "  - Installed $TARGET_DIR/$bname"
        fi
    done

    echo "[INFO] Reloading systemd user daemon..."
    if command -v systemctl >/dev/null 2>&1; then
        systemctl --user daemon-reload || true
        echo "[OK] Systemd units installed. To enable timers, run:"
        echo "       systemctl --user enable --now vikunja-butler-classify.timer"
        echo "       systemctl --user enable --now vikunja-butler-digest.timer"
    fi
else
    echo ""
    echo "[TIP] To install systemd user timers, run:"
    echo "       ./install.sh --install-units"
fi

echo "========================================================================"
echo " Setup check complete."
echo "========================================================================"
