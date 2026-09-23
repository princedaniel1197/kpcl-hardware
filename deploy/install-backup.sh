#!/bin/bash
set -euo pipefail
WORKDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.orianode.crpms.backup"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGDIR="$HOME/Library/Logs/orianode-crpms"
# Resolved now, to an absolute path, because launchd runs jobs with a minimal
# PATH on which `docker` is usually not found.
source "$WORKDIR/ops/docker.sh"
mkdir -p "$HOME/Library/LaunchAgents" "$LOGDIR"
sed -e "s|__WORKDIR__|$WORKDIR|g" -e "s|__LOGDIR__|$LOGDIR|g" \
    -e "s|__DOCKER__|$DOCKER|g" \
    "$WORKDIR/deploy/$LABEL.plist.template" > "$PLIST"
launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$PLIST"
echo "hourly backup installed: $PLIST"
echo "RPO is therefore one hour; see docs/backup-and-restore.md"
