#!/usr/bin/env bash
# Copy the bridge from the WSL repo (source of truth) to the Windows folder
# the TeamsBusyLight scheduled task runs from, then restart the task.
set -euo pipefail

DEST=/mnt/c/Tools/teams-busylight
cp "$(dirname "$0")/teams_light_bridge.py" "$DEST/"
echo "Copied teams_light_bridge.py -> $DEST"

powershell.exe -NoProfile -Command \
    "Stop-ScheduledTask -TaskName TeamsBusyLight -ErrorAction SilentlyContinue; \
     Start-ScheduledTask -TaskName TeamsBusyLight" </dev/null
echo "Restarted TeamsBusyLight scheduled task."
