#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

if [[ ! -f .env ]]; then
  echo "Missing .env. Run: cp .env.example .env"
  exit 1
fi

if [[ ! -x .venv/bin/uvicorn ]]; then
  echo "Virtualenv missing. Run: sudo bash scripts/install.sh"
  exit 1
fi

set -a
source .env
set +a

exec .venv/bin/uvicorn app.main:app --host "${APP_HOST:-127.0.0.1}" --port "${APP_PORT:-8080}" --reload
