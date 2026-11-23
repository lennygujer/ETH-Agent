#!/usr/bin/env bash
# Variante A: Server holt den ETH-Ordner direkt aus OneDrive (rclone-Remote "eth", siehe docs/sync.md).
# Gelöschte/überschriebene Dateien landen 30 Tage in ~/eth-agent/material-trash.
set -euo pipefail
cd "$(dirname "$0")/.."
REMOTE="${ETH_AGENT_REMOTE:-eth:ETH}"
DEST="${ETH_AGENT_MATERIAL:-$HOME/eth-agent/material}"
TRASH="$HOME/eth-agent/material-trash/$(date +%Y-%m-%d)"

rclone sync "$REMOTE" "$DEST" \
  --exclude-from deploy/rclone-excludes.txt \
  --max-size 200M \
  --backup-dir "$TRASH" \
  --fast-list --transfers 4 \
  --log-level NOTICE --stats-one-line --stats 60s

# alten Papierkorb aufräumen
find "$HOME/eth-agent/material-trash" -mindepth 1 -maxdepth 1 -type d -mtime +30 -exec rm -rf {} + 2>/dev/null || true
