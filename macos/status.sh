#!/bin/bash
set -euo pipefail

if launchctl list | grep -q nse-scanner; then
    echo "Running:"
    launchctl list | grep nse-scanner
    echo ""
    echo "Recent log activity:"
    tail -5 "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/logs/scanner.log" 2>/dev/null || echo "(no log yet)"
else
    echo "Not running (either not installed, or paused with ./macos/pause.sh)"
fi
