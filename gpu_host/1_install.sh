#!/usr/bin/env bash
# =============================================================================
# GPU host setup — Ubuntu 24.04 LTS, NVIDIA RTX 5090
# =============================================================================
# What this does:
#   1. Verify CUDA driver
#   2. Install system packages (python venv, git, build tools, cmake)
#   3. Create Python venv, install PyTorch (cu124 for Blackwell) + HF stack
#   4. Clone & build llama.cpp (needed for the export-to-GGUF step)
#
# Usage:
#   chmod +x gpu_host/*.sh
#   sudo ./gpu_host/1_install.sh
#
# Run as root or with sudo on first install.
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "============================================================"
echo "  GPU-host setup (Ubuntu)"
echo "  project root: $ROOT"
echo "============================================================"

# ── 1. CUDA check ───────────────────────────────────────────────────────────
echo
echo "[1/4] Checking NVIDIA driver…"
if ! command -v nvidia-smi >/dev/null; then
    echo "  ERROR: nvidia-smi not found. Install the NVIDIA driver first:"
    echo "    sudo apt install -y nvidia-driver-560 nvidia-utils-560"
    echo "  Then reboot and re-run this script."
    exit 1
fi
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader

# ── 2. System packages ──────────────────────────────────────────────────────
echo
echo "[2/4] Installing system packages…"
if [ "$(id -u)" -ne 0 ]; then
    SUDO=sudo
else
    SUDO=
fi
$SUDO apt update
$SUDO apt install -y \
    python3 python3-venv python3-pip python3-dev \
    git build-essential cmake \
    libopenblas-dev ccache pkg-config \
    curl

# ── 3. Python venv + ML stack ──────────────────────────────────────────────
echo
echo "[3/4] Creating Python venv and installing PyTorch + HF stack…"
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --upgrade pip

# PyTorch with CUDA 12.4 (Blackwell support)
pip install --index-url https://download.pytorch.org/whl/cu124 \
    torch torchvision torchaudio

# Project + training deps (skips Ollama client / Gradio — those are workstation-only)
pip install -r requirements-training.txt

# ── 4. Build llama.cpp for GGUF export ──────────────────────────────────────
echo
echo "[4/4] Cloning & building llama.cpp (for HF→GGUF conversion + quantization)…"
if [ ! -d "llama.cpp" ]; then
    git clone https://github.com/ggerganov/llama.cpp.git
fi
cd llama.cpp
# Pull latest stable
git fetch --tags
git checkout master
git pull
# Configure & build (with CUDA — speeds up quantization)
cmake -B build -DGGML_CUDA=ON -DLLAMA_CURL=OFF
cmake --build build --config Release --parallel "$(nproc)"
# Install Python deps required by convert_hf_to_gguf.py
pip install -r requirements.txt
cd "$ROOT"

# ── Smoke check ─────────────────────────────────────────────────────────────
echo
echo "[*] Verifying CUDA in PyTorch…"
python -c "
import torch
print(f'  PyTorch:  {torch.__version__}')
print(f'  CUDA OK:  {torch.cuda.is_available()}')
if torch.cuda.is_available():
    print(f'  Device:   {torch.cuda.get_device_name(0)}')
    print(f'  VRAM:     {torch.cuda.get_device_properties(0).total_memory / 1024**3:.1f} GB')
"

echo
echo "============================================================"
echo "  [OK] GPU host ready."
echo
echo "  Next steps:"
echo "    1. Put labelled examples in examples/training_data/*.jsonl"
echo "    2. ./gpu_host/2_train.sh         # fine-tune Qwen2.5-Coder-1.5B"
echo "    3. ./gpu_host/3_export_gguf.sh   # produce one .gguf file for the target"
echo "============================================================"
