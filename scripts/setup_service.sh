#!/usr/bin/env bash
# Richtet den ETH-Agent als Dienst ein (startet automatisch, auch ohne Anmeldung).
#   ./scripts/setup_service.sh                 -> Server nur lokal (127.0.0.1)
#   ETH_AGENT_HOST=172.17.0.1 ./scripts/setup_service.sh   -> für Open WebUI in Docker
set -euo pipefail
cd "$(dirname "$0")/.."
APP_DIR="$(pwd)"
HOST="${ETH_AGENT_HOST:-127.0.0.1}"
PORT="${ETH_AGENT_PORT:-8000}"

if [ ! -f .env ] || ! grep -q '^ETH_AGENT_API_KEY=' .env; then
  KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  echo "ETH_AGENT_API_KEY=$KEY" >> .env
  echo "== Neuer API-Schlüssel in .env erzeugt."
fi
# Host/Port immer aktualisieren
sed -i '/^ETH_AGENT_HOST=/d;/^ETH_AGENT_PORT=/d' .env
printf 'ETH_AGENT_HOST=%s\nETH_AGENT_PORT=%s\n' "$HOST" "$PORT" >> .env
chmod 600 .env

if [ "$APP_DIR" != "$HOME/eth-agent/app" ]; then
  echo "! Die Dienst-Datei erwartet das Repo in ~/eth-agent/app (aktuell: $APP_DIR)."
  exit 1
fi

mkdir -p ~/.config/systemd/user
cp deploy/eth-agent.service ~/.config/systemd/user/eth-agent.service
systemctl --user daemon-reload
systemctl --user enable --now eth-agent.service
# Dienst auch ohne aktive Anmeldung laufen lassen
sudo loginctl enable-linger "$USER" || echo "! linger nicht gesetzt – Dienst läuft nur, solange du angemeldet bist."

sleep 2
systemctl --user --no-pager status eth-agent.service | head -5 || true
echo
echo "Fertig. Server: http://$HOST:$PORT/v1"
echo "API-Schlüssel für Open WebUI:"
grep '^ETH_AGENT_API_KEY=' .env | cut -d= -f2-
echo
echo "Logs: journalctl --user -u eth-agent -f"
