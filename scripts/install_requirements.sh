#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

echo "[1/3] Checking for NVIDIA GPU..."
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then
  echo "NVIDIA GPU detected. Installing CUDA-enabled Torch wheels (cu128)."
  pip install --upgrade --index-url https://download.pytorch.org/whl/cu128 torch torchvision
else
  echo "No NVIDIA GPU detected. Installing CPU-only Torch wheels."
  pip install --upgrade --index-url https://download.pytorch.org/whl/cpu torch torchvision
fi

echo "[2/3] Installing project requirements..."
pip install -r requirements.txt

echo "[3/3] Done."
python - <<'PY'
try:
    import torch
    print(f"Torch: {torch.__version__}")
    print(f"CUDA available: {torch.cuda.is_available()}")
    print(f"CUDA version: {torch.version.cuda}")
except Exception as exc:
    print(f"Torch check failed: {exc}")
PY
