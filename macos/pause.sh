#!/bin/bash
# Pauses the scanner. Unlike a plain `launchctl unload`, the -w flag
# persists this across reboots — it stays off until you run resume.sh,
# it won't silently come back at your next login.
set -euo pipefail

PLIST_DEST="$HOME/Library/LaunchAgents/com.nse-scanner.plist"

if [ ! -f "$PLIST_DEST" ]; then
    echo "Not installed — nothing to pause. Run ./macos/install.sh first."
    exit 1
fi

launchctl unload -w "$PLIST_DEST"
echo "Paused. It will stay off (including across restarts) until you run:"
echo "  ./macos/resume.sh"
