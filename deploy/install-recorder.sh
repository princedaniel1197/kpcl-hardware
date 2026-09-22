#!/bin/bash
# Install the SLDC recorder as a launchd agent. Idempotent: re-running it
# reloads the agent with the current paths.
set -euo pipefail

WORKDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.orianode.crpms.sldc-recorder"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGDIR="$HOME/Library/Logs/orianode-crpms"
PYTHON="$WORKDIR/.venv/bin/python"
DSN="${CRPMS_DSN:-postgresql://crpms:crpms@localhost:5432/crpms}"

[ -x "$PYTHON" ] || { echo "no virtualenv at $PYTHON — run 'make install' first"; exit 1; }

mkdir -p "$HOME/Library/LaunchAgents" "$LOGDIR"

sed -e "s|__PYTHON__|$PYTHON|g" \
    -e "s|__WORKDIR__|$WORKDIR|g" \
    -e "s|__LOGDIR__|$LOGDIR|g" \
    -e "s|__DSN__|$DSN|g" \
    "$WORKDIR/deploy/$LABEL.plist.template" > "$PLIST"

# bootout is expected to fail when the agent was not loaded; that is fine.
launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$PLIST"
launchctl enable "gui/$UID/$LABEL"

echo "installed : $PLIST"
echo "logs      : $LOGDIR/sldc-recorder.log"
echo "status    : launchctl print gui/$UID/$LABEL | head"
