#!/usr/bin/env bash
# =============================================================================
# Target device setup — Debian 11 ARM (RK3566, 4 GB RAM, 32 GB disk)
# =============================================================================
# What this does:
#   1. Install build deps (apt: cmake, build tools, libgomp, curl)
#   2. Build llama.cpp natively for arm64 with NEON/dotprod (no CUDA)
#   3. Create directory layout /opt/llm-etl and a systemd unit for llama-server
#
# Usage (one-shot, on the target device):
#   curl -fsSL https://...your-host.../target_install.sh -o target_install.sh
#   sudo bash target_install.sh
#
# After install, drop your .gguf model file in /opt/llm-etl/models/ and run:
#   sudo systemctl restart llm-etl
# =============================================================================
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Run as root: sudo bash $0"; exit 1
fi

INSTALL_DIR=/opt/llm-etl
MODELS_DIR=$INSTALL_DIR/models
LLAMA_DIR=$INSTALL_DIR/llama.cpp
SERVICE_USER=llm-etl
LLAMA_PORT=8080
CTX=4096
THREADS=4

echo "============================================================"
echo "  Target host setup (Debian 11 ARM)"
echo "============================================================"

# ── 1. System packages ──────────────────────────────────────────────────────
echo
echo "[1/4] Installing system packages…"
apt-get update
apt-get install -y --no-install-recommends \
    build-essential cmake git curl ca-certificates \
    libgomp1 libstdc++6 pkg-config

# Create unprivileged user
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi

# Layout
mkdir -p "$INSTALL_DIR" "$MODELS_DIR"

# ── 2. Build llama.cpp for ARM64 ────────────────────────────────────────────
echo
echo "[2/4] Building llama.cpp natively (ARM64 + NEON)…"
if [ ! -d "$LLAMA_DIR" ]; then
    git clone --depth=1 https://github.com/ggerganov/llama.cpp.git "$LLAMA_DIR"
else
    git -C "$LLAMA_DIR" pull --ff-only || true
fi
cd "$LLAMA_DIR"
# RK3566 is Cortex-A55 → ARMv8.2-A with dotprod
cmake -B build \
    -DCMAKE_BUILD_TYPE=Release \
    -DGGML_NATIVE=ON \
    -DGGML_LTO=ON \
    -DLLAMA_CURL=OFF
cmake --build build --config Release --parallel "$(nproc)" --target llama-server llama-cli llama-quantize

# Copy binaries to a stable path
install -m 0755 build/bin/llama-server "$INSTALL_DIR/llama-server"
install -m 0755 build/bin/llama-cli    "$INSTALL_DIR/llama-cli"

# ── 3. Create systemd unit ─────────────────────────────────────────────────
echo
echo "[3/4] Creating systemd unit…"
cat > /etc/systemd/system/llm-etl.service <<EOF
[Unit]
Description=LLM ETL parser (llama.cpp server)
After=network.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
# The active model is exposed as a symlink → can be swapped without editing the unit.
ExecStart=$INSTALL_DIR/llama-server \\
    --model $MODELS_DIR/active.gguf \\
    --port $LLAMA_PORT \\
    --host 0.0.0.0 \\
    --ctx-size $CTX \\
    --threads $THREADS \\
    --batch-size 256 \\
    --no-mmap \\
    --log-disable
Restart=on-failure
RestartSec=5s
# Memory guard — kill the service if it tries to use >3 GB on a 4 GB box.
MemoryMax=3000M
MemorySwapMax=0

[Install]
WantedBy=multi-user.target
EOF

chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
systemctl daemon-reload
systemctl enable llm-etl.service

# ── 4. Summary ──────────────────────────────────────────────────────────────
echo
echo "[4/4] Layout:"
echo "  binary:   $INSTALL_DIR/llama-server"
echo "  models:   $MODELS_DIR/"
echo "  active:   $MODELS_DIR/active.gguf  (symlink, set by deploy script)"
echo "  service:  systemctl status llm-etl"
echo "  port:     $LLAMA_PORT"
echo
echo "============================================================"
echo "  [OK] Install complete."
echo
echo "  Next: copy etl-parser-q4_k_m.gguf to this machine and run:"
echo "    sudo $INSTALL_DIR/target_deploy_model.sh /path/to/etl-parser-q4_k_m.gguf"
echo "  (or use scp from the GPU host)"
echo "============================================================"
