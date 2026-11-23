#!/usr/bin/env bash
# Installation auf dem Ubuntu-Server.
#   ./install.sh            -> mit Embeddings (sentence-transformers, CPU-Version von torch)
#   EMBED=0 ./install.sh    -> nur Stichwortsuche (schneller, weniger RAM/Platz)
set -euo pipefail
cd "$(dirname "$0")"

EMBED="${EMBED:-1}"
DATA_ROOT="${DATA_ROOT:-$HOME/eth-agent}"

echo "== Python prüfen"
PY=python3
$PY -c 'import sys; assert sys.version_info >= (3, 10), sys.version' \
  || { echo "Python >= 3.10 nötig"; exit 1; }
if ! $PY -c 'import venv, ensurepip' 2>/dev/null; then
  echo "python3-venv fehlt -> sudo apt install -y python3-venv"
  sudo apt-get update && sudo apt-get install -y python3-venv
fi

echo "== Virtuelle Umgebung .venv"
[ -d .venv ] || $PY -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q --upgrade pip

if [ "$EMBED" = "1" ]; then
  echo "== torch (nur CPU, spart mehrere GB gegenüber der CUDA-Version)"
  pip install -q torch --index-url https://download.pytorch.org/whl/cpu
  pip install -q -e '.[embed,dev]'
else
  pip install -q -e '.[dev]'
fi

echo "== Ordner und Konfiguration"
mkdir -p "$DATA_ROOT/material" "$DATA_ROOT/data"
if [ ! -f config.yaml ]; then
  cp config.example.yaml config.yaml
  echo "   config.yaml angelegt – Pfade/Kurse bei Bedarf anpassen."
fi

echo "== Tests"
pytest -q

cat <<EOF

Fertig.
  Material nach $DATA_ROOT/material/<Kursordner>/ kopieren, dann:
    source .venv/bin/activate
    eth-agent index
    eth-agent search "hydrogen atom expectation value"
EOF
