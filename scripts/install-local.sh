#!/usr/bin/env bash
# Install the on-prem GPU engine (PyTorch CUDA 13 + Qwen ASR/LLM/TTS) into .venv.
# Tested on Jetson AGX Thor (JetPack 7 / CUDA 13, aarch64).
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
fi
export PIP_CACHE_DIR="${PIP_CACHE_DIR:-$HOME/.cache/pip}"

.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -r requirements-local.txt
# qwen-tts pins transformers==4.57.3 while qwen-asr pins 4.57.6; both work on 4.57.6.
.venv/bin/pip install --no-deps qwen-tts==0.1.1

.venv/bin/python - <<'PY'
import torch
print("torch", torch.__version__, "cuda", torch.version.cuda, "available", torch.cuda.is_available())
if torch.cuda.is_available():
    print("device", torch.cuda.get_device_name(0), "capability", torch.cuda.get_device_capability(0))
PY

bash scripts/download-models.sh
