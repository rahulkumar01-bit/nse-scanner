#!/bin/bash
set -euo pipefail

PLIST_DEST="$HOME/Library/LaunchAgents/com.nse-scanner.plist"

if [ ! -f "$PLIST_DEST" ]; then
    echo "Not installed — run ./macos/install.sh first."
    exit 1
fi

launchctl load -w "$PLIST_DEST"
echo "Resumed. Check it's running with: launchctl list | grep nse-scanner"
