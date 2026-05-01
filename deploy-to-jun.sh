#!/usr/bin/env bash
set -euo pipefail

# Deploy media-fetch-api to Jun's machine
JUN_HOST="jun@192.168.1.181"
REMOTE_DIR="~/openclaw-local/services/media-fetch-api"

echo "==> Syncing files to ${JUN_HOST}:${REMOTE_DIR}"
rsync -avzP --exclude '.venv' --exclude '__pycache__' \
    "$(dirname "$0")/" "${JUN_HOST}:${REMOTE_DIR}/"

echo "==> Installing dependencies on remote"
ssh "${JUN_HOST}" "cd ${REMOTE_DIR} && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/playwright install chromium"

echo "==> Done. To start the service:"
echo "    ssh ${JUN_HOST} 'cd ${REMOTE_DIR} && nohup .venv/bin/python main.py > media-fetch-api.log 2>&1 &'"
echo ""
echo "==> Don't forget to set up XHS cookies:"
echo "    Log in to xiaohongshu.com in a browser, export cookies to"
echo "    ~/.media-fetch-api/xhs-cookies.json on Jun's machine"
