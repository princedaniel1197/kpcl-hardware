#!/bin/bash
set -euo pipefail
LABEL="com.orianode.crpms.sldc-recorder"
launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
echo "removed $LABEL (logs in ~/Library/Logs/orianode-crpms are kept)"
