#!/bin/bash
# Installs the NSE scanner as a macOS LaunchAgent so it runs in the
# background, restarts if it crashes, and starts automatically on login.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLIST_LABEL="com.nse-scanner"
PLIST_DEST="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"

if [ ! -d "$PROJECT_DIR/venv" ]; then
    echo "No venv found at $PROJECT_DIR/venv"
    echo "Run this first:"
    echo "  cd $PROJECT_DIR && python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt"
    exit 1
fi

if [ ! -f "$PROJECT_DIR/.env" ]; then
    echo "No .env found at $PROJECT_DIR/.env — copy .env.example to .env and fill it in first."
    exit 1
fi

PYTHON_BIN="$PROJECT_DIR/venv/bin/python3"

mkdir -p "$PROJECT_DIR/logs"

sed \
    -e "s|__PYTHON_BIN__|$PYTHON_BIN|g" \
    -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
    "$PROJECT_DIR/macos/com.nse-scanner.plist.template" > "$PLIST_DEST"

# Unload any previous version first (harmless if it wasn't loaded)
launchctl unload "$PLIST_DEST" 2>/dev/null || true
launchctl load "$PLIST_DEST"

echo "Installed and started: $PLIST_LABEL"
echo "  Plist:  $PLIST_DEST"
echo "  Logs:   $PROJECT_DIR/logs/scanner.log        (application log)"
echo "          $PROJECT_DIR/logs/launchd.out.log    (stdout)"
echo "          $PROJECT_DIR/logs/launchd.err.log    (stderr / crash output)"
echo ""
echo "Check it's running:   launchctl list | grep nse-scanner"
echo "Stop it:               ./macos/uninstall.sh"
echo "Tail the logs:         tail -f logs/scanner.log"
