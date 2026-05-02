#!/usr/bin/env bash
set -euo pipefail

# Deploy media-fetch-api to Jun's machine
JUN_HOST="jun@192.168.1.181"
REMOTE_DIR="~/openclaw-local/services/media-fetch-api"

echo "==> Syncing files to ${JUN_HOST}:${REMOTE_DIR}"
rsync -avzP --exclude '.venv' --exclude '__pycache__' --exclude '.env' \
    "$(dirname "$0")/" "${JUN_HOST}:${REMOTE_DIR}/"

echo "==> Installing dependencies on remote"
ssh "${JUN_HOST}" "cd ${REMOTE_DIR} && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/playwright install chromium"

echo "==> Stopping old standalone doubao-2api (port 8088) if running"
ssh "${JUN_HOST}" "lsof -ti:8088 | xargs kill 2>/dev/null || true"

echo "==> Done. To start the service:"
echo "    ssh ${JUN_HOST} 'cd ${REMOTE_DIR} && nohup ./run.sh > media-fetch-api.log 2>&1 &'"
echo ""
echo "==> Make sure .env exists on remote with DOUBAO_COOKIE_1 and device fingerprint vars."
echo "    Copy .env.example and fill in values: scp .env.example ${JUN_HOST}:${REMOTE_DIR}/.env"
echo ""
echo "==> Update test-two openclaw.json doubao apiBaseUrl to host.docker.internal:8089"
