#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

# Load .env if present
if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

# Install deps if needed
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt
    .venv/bin/playwright install chromium
fi

exec .venv/bin/python main.py
