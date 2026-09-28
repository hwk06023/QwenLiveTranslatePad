#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

if [[ -z "${DASHSCOPE_API_KEY:-}" ]]; then
  echo "ERROR: DASHSCOPE_API_KEY is missing. Put it in $APP_DIR/.env"
  exit 1
fi

if [[ -z "${DASHSCOPE_WORKSPACE_ID:-}" ]]; then
  echo "ERROR: DASHSCOPE_WORKSPACE_ID is missing. Put it in $APP_DIR/.env"
  exit 1
fi

PROJECT_NAME="${VERCEL_PROJECT_NAME:-qwen-live-translate-pad}"

if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
  echo "[1/7] Installing Node.js/npm..."
  sudo apt-get update
  sudo apt-get install -y nodejs npm
else
  echo "[1/7] Node.js/npm already installed."
fi

echo "[2/7] Checking Vercel login..."
if ! npx --yes vercel@latest whoami >/dev/null 2>&1; then
  echo "Vercel login is required. Follow the URL/code shown below."
  npx --yes vercel@latest login
fi
echo "Vercel user: $(npx --yes vercel@latest whoami)"

echo "[3/7] Creating/linking Vercel project..."
npx --yes vercel@latest project add "$PROJECT_NAME" >/dev/null 2>&1 || true
npx --yes vercel@latest link --yes --project "$PROJECT_NAME"

echo "[4/7] Uploading production environment variables..."
printf '%s' "$DASHSCOPE_API_KEY" |   npx --yes vercel@latest env add DASHSCOPE_API_KEY production --force --sensitive

printf '%s' "$DASHSCOPE_WORKSPACE_ID" |   npx --yes vercel@latest env add DASHSCOPE_WORKSPACE_ID production --force

printf '%s' "${DASHSCOPE_REGION:-ap-southeast-1}" |   npx --yes vercel@latest env add DASHSCOPE_REGION production --force

printf '%s' "${QWEN_MODEL:-qwen3.8-livetranslate-flash-realtime}" |   npx --yes vercel@latest env add QWEN_MODEL production --force

echo "[5/7] Local Python syntax check..."
python3 -m py_compile app/*.py

echo "[6/7] Deploying production..."
set +e
DEPLOY_OUTPUT="$(npx --yes vercel@latest deploy --prod --yes 2>&1)"
DEPLOY_EXIT=$?
set -e
printf '%s\n' "$DEPLOY_OUTPUT"

if [[ $DEPLOY_EXIT -ne 0 ]]; then
  echo
  echo "=================================================="
  echo "VERCEL DEPLOY FAILED (exit=$DEPLOY_EXIT)"
  echo "=================================================="
  echo "Run this for more detail:"
  echo "  npx --yes vercel@latest deploy --prod --yes --debug"
  exit "$DEPLOY_EXIT"
fi

DEPLOY_URL="$(printf '%s\n' "$DEPLOY_OUTPUT" | grep -Eo 'https://[^ ]+\.vercel\.app' | tail -1 || true)"

echo "[7/7] Done."
if [[ -n "$DEPLOY_URL" ]]; then
  echo
  echo "DEMO URL: $DEPLOY_URL"
  echo "Health:   $DEPLOY_URL/health"
else
  echo "Deployment succeeded. Run:"
  echo "  npx --yes vercel@latest ls --prod"
fi
