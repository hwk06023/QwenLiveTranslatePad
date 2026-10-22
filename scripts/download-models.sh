#!/usr/bin/env bash
# Download the on-prem engine weights into models/ (~16 GB).
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

MODELS=(
  "${LOCAL_ASR_REPO:-Qwen/Qwen3-ASR-1.7B}"
  "${LOCAL_LLM_REPO:-Qwen/Qwen3-4B-Instruct-2507}"
  "${LOCAL_TTS_REPO:-Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice}"
)

# A global HF_HOME may point at an unmounted/unwritable disk; keep the hub
# cache under the user's home and store weights in models/<name>.
export HF_HOME="${MODEL_HF_HOME:-$HOME/.cache/huggingface}"

for repo in "${MODELS[@]}"; do
  echo "==> $repo"
  .venv/bin/python - "$repo" <<'PY'
import sys
from huggingface_hub import snapshot_download
repo = sys.argv[1]
print(snapshot_download(repo, local_dir=f"models/{repo.split('/')[-1]}"))
PY
done
du -sh models/*
