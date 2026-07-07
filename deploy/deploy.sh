#!/usr/bin/env bash
# Deploy the PetLibro rhythm-feeding web UI to scott-server (LAN, systemd+uvicorn).
# Run from the repo root:  bash deploy/deploy.sh
set -euo pipefail

HOST="${PETLIBRO_DEPLOY_HOST:-root@192.168.68.53}"
DEST=/opt/petlibro-webui

echo ">> syncing repo to $HOST:$DEST"
rsync -az --delete \
  --exclude '.git' --exclude '.venv' --exclude '__pycache__' --exclude '.pytest_cache' \
  --exclude '.superpowers' --exclude '.playwright-mcp' --exclude '*.png' \
  ./ "$HOST:$DEST/"

echo ">> copying credentials (.env)"
scp -q .env "$HOST:$DEST/.env"

echo ">> building venv + installing + (re)starting service"
ssh "$HOST" bash -s <<'REMOTE'
set -euo pipefail
cd /opt/petlibro-webui
if ! python3 -m venv .venv 2>/dev/null; then
  echo "venv module missing; installing python3-venv"
  apt-get update -qq && apt-get install -y -qq python3-venv
  python3 -m venv .venv
fi
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q -e '.[web]'
chmod 600 .env
install -m 644 deploy/petlibro-webui.service /etc/systemd/system/petlibro-webui.service
systemctl daemon-reload
systemctl enable --now petlibro-webui
sleep 2
systemctl is-active petlibro-webui
echo "--- /api/pets ---"
curl -fsS http://127.0.0.1:8080/api/pets | head -c 500
echo
REMOTE

echo ">> done. UI: http://192.168.68.53:8080/"
