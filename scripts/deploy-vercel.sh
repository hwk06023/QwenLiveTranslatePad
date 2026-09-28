#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${DASHSCOPE_API_KEY:-}" ]]; then
  echo "ERROR: DASHSCOPE_API_KEY is not exported in this shell."
  exit 1
fi

if [[ -z "${DASHSCOPE_WORKSPACE_ID:-}" ]]; then
  echo "ERROR: DASHSCOPE_WORKSPACE_ID is not exported in this shell."
  exit 1
fi

PROJECT_NAME="${VERCEL_PROJECT_NAME:-qwen-live-translate-pad}"

if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
  echo "[1/6] Installing Node.js/npm..."
  sudo apt-get update
  sudo apt-get install -y nodejs npm
fi

echo "[2/6] Checking Vercel login..."
if ! npx --yes vercel@latest whoami >/dev/null 2>&1; then
  echo "Vercel login is required. Follow the URL/code shown below."
  npx --yes vercel@latest login
fi

echo "[3/6] Creating/linking Vercel project..."
npx --yes vercel@latest project add "$PROJECT_NAME" >/dev/null 2>&1 || true
npx --yes vercel@latest link --yes --project "$PROJECT_NAME"

echo "[4/6] Uploading production environment variables..."
printf '%s' "$DASHSCOPE_API_KEY" |   npx --yes vercel@latest env add DASHSCOPE_API_KEY production --force --sensitive

printf '%s' "$DASHSCOPE_WORKSPACE_ID" |   npx --yes vercel@latest env add DASHSCOPE_WORKSPACE_ID production --force

printf '%s' "${DASHSCOPE_REGION:-ap-southeast-1}" |   npx --yes vercel@latest env add DASHSCOPE_REGION production --force

printf '%s' "${QWEN_MODEL:-qwen3.8-livetranslate-flash-realtime}" |   npx --yes vercel@latest env add QWEN_MODEL production --force

echo "[5/6] Deploying production..."
DEPLOY_URL="$(npx --yes vercel@latest deploy --prod --yes 2>&1 | tee /dev/stderr | grep -Eo 'https://[^ ]+\.vercel\.app' | tail -1 || true)"

echo "[6/6] Done."
if [[ -n "$DEPLOY_URL" ]]; then
  echo
  echo "DEMO URL: $DEPLOY_URL"
  echo "Health:   $DEPLOY_URL/health"
else
  echo "Deployment finished. Run: npx --yes vercel@latest ls --prod"
fi
