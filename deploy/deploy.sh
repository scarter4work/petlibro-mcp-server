#!/usr/bin/env bash
# Deploy the PetLibro rhythm-feeding web UI to scott-server (LAN, systemd+uvicorn).
# Run from the repo root:  bash deploy/deploy.sh
set -euo pipefail

HOST="${PETLIBRO_DEPLOY_HOST:-root@192.168.68.53}"
DEST=/opt/petlibro-webui

echo ">> syncing repo to $HOST:$DEST"
# The journal and the event store are deploy-surviving state, not source:
# --delete would wipe the applied-schedule history that dates every evidence
# window, and the accumulated event history that outlives the cloud's own
# 60-day retention. Losing petlibro.db is unrecoverable — the cloud cannot
# give back records older than its window.
rsync -az --delete \
  --exclude '.git' --exclude '.venv' --exclude '__pycache__' --exclude '.pytest_cache' \
  --exclude '.superpowers' --exclude '.playwright-mcp' --exclude '*.png' \
  --exclude '.petlibro-journal.jsonl' \
  --exclude 'petlibro.db' --exclude 'petlibro.db-wal' --exclude 'petlibro.db-shm' \
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
# dedicated unprivileged service user (not root)
id petlibro &>/dev/null || useradd --system --no-create-home --shell /usr/sbin/nologin petlibro
chown -R petlibro:petlibro /opt/petlibro-webui
chmod 600 .env
install -m 644 deploy/petlibro-webui.service /etc/systemd/system/petlibro-webui.service
systemctl daemon-reload
systemctl enable petlibro-webui
systemctl restart petlibro-webui   # restart (not just enable --now) so unit changes apply
sleep 2
systemctl is-active petlibro-webui
echo "--- /api/pets ---"
curl -fsS http://127.0.0.1:8080/api/pets | head -c 500
echo
REMOTE

echo ">> done. UI: http://192.168.68.53:8080/"
