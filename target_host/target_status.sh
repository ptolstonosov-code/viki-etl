#!/usr/bin/env bash
# =============================================================================
# Quick health check on the target device.
#
# Shows:
#   - systemd service state
#   - which .gguf is currently active
#   - HTTP API ping
#   - RAM and disk used
#
# Usage:  bash target_status.sh
# =============================================================================
set -uo pipefail

INSTALL_DIR=/opt/llm-etl
MODELS_DIR=$INSTALL_DIR/models
ACTIVE=$MODELS_DIR/active.gguf

echo "===== llm-etl status ====="

# Service
echo
echo "[service]"
systemctl is-active llm-etl.service || true
systemctl is-enabled llm-etl.service || true

# Model
echo
echo "[model]"
if [ -L "$ACTIVE" ]; then
    real=$(readlink -f "$ACTIVE")
    size=$(stat -c%s "$real" 2>/dev/null | awk '{printf "%.1f MB", $1/1024/1024}')
    echo "  active: $(basename "$real") ($size)"
else
    echo "  no active model — drop a .gguf into $MODELS_DIR and rerun deploy."
fi

echo
echo "  all installed:"
ls -1t "$MODELS_DIR"/etl-parser-*.gguf 2>/dev/null | head -5 | sed 's|^|    |'

# HTTP
echo
echo "[http]"
if command -v curl >/dev/null; then
    if curl -fsS --max-time 5 "http://127.0.0.1:8080/health" >/dev/null 2>&1; then
        echo "  /health: OK"
    elif curl -fsS --max-time 5 "http://127.0.0.1:8080/v1/models" >/dev/null 2>&1; then
        echo "  /v1/models: OK"
    else
        echo "  not responding on :8080"
    fi
fi

# RAM / disk
echo
echo "[resources]"
free -h | awk '/^Mem:/ {printf "  RAM: %s used of %s\n", $3, $2}'
df -h "$MODELS_DIR" | awk 'NR==2 {printf "  disk: %s used of %s on %s\n", $3, $2, $6}'
