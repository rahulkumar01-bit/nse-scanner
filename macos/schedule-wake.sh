#!/bin/bash
# Optional. Schedules macOS to auto-wake at 09:05 IST on weekdays, so the
# scanner is ready before the 09:15 market open even if the Mac slept
# overnight. Requires sudo. Assumes your Mac's clock is already set to
# IST — check with `date` first if unsure.
#
# This wakes the Mac from regular idle/system sleep. It does NOT wake it
# from a closed lid with no external display — clamshell sleep is a
# hardware policy pmset can't override either.
set -euo pipefail

echo "This will run: sudo pmset repeat wakeorpoweron MTWTF 09:05:00"
read -p "Continue? [y/N] " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    exit 0
fi

sudo pmset repeat wakeorpoweron MTWTF 09:05:00
echo "Scheduled. Check with: pmset -g sched"
echo "Remove later with:      sudo pmset repeat cancel"
