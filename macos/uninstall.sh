#!/bin/bash
set -euo pipefail

PLIST_DEST="$HOME/Library/LaunchAgents/com.nse-scanner.plist"

if [ -f "$PLIST_DEST" ]; then
    launchctl unload "$PLIST_DEST" 2>/dev/null || true
    rm "$PLIST_DEST"
    echo "Stopped and removed com.nse-scanner"
else
    echo "Not installed (no plist at $PLIST_DEST)"
fi
