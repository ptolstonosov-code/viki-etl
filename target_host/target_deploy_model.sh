#!/usr/bin/env bash
# =============================================================================
# Deploy a new .gguf model to the target device.
#
# Atomic-ish swap: copy → fsync → relink "active.gguf" → restart service.
# Keeps previous versions in models/ so you can rollback.
#
# Usage:
#   sudo bash target_deploy_model.sh /home/pi/etl-parser-q4_k_m.gguf
# =============================================================================
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Run as root: sudo bash $0 <path-to-gguf>"; exit 1
fi
if [ "$#" -ne 1 ]; then
    echo "Usage: sudo bash $0 <path-to-gguf>"; exit 1
fi

SRC="$1"
if [ ! -f "$SRC" ]; then
    echo "  ERROR: file not found: $SRC"; exit 1
fi

INSTALL_DIR=/opt/llm-etl
MODELS_DIR=$INSTALL_DIR/models
SERVICE_USER=llm-etl
ACTIVE=$MODELS_DIR/active.gguf

STAMP=$(date +%Y%m%d-%H%M%S)
DST_NAME="etl-parser-$STAMP.gguf"
DST="$MODELS_DIR/$DST_NAME"

# Free-space sanity check
NEED_KB=$(stat -c%s "$SRC" | awk '{printf "%.0f", $1/1024 * 1.1}')   # +10% slack
FREE_KB=$(df -k --output=avail "$MODELS_DIR" | tail -1 | tr -d ' ')
if [ "$FREE_KB" -lt "$NEED_KB" ]; then
    echo "  ERROR: not enough space (need ~$((NEED_KB/1024)) MB, have $((FREE_KB/1024)) MB)."
    echo "  Free up space and try again. Old models are in $MODELS_DIR/."
    exit 1
fi

# Copy + sync
echo "[*] Copying model → $DST_NAME"
cp "$SRC" "$DST"
sync "$DST"
chown "$SERVICE_USER:$SERVICE_USER" "$DST"
chmod 0644 "$DST"

# Atomic-ish swap
echo "[*] Swapping active model symlink"
ln -sfn "$DST_NAME" "$ACTIVE.new"
mv -Tf "$ACTIVE.new" "$ACTIVE"

# Restart service & smoke check
echo "[*] Restarting llm-etl service"
systemctl restart llm-etl.service
sleep 3
if systemctl is-active --quiet llm-etl.service; then
    echo "[OK] service is active"
    # Quick health probe
    if command -v curl >/dev/null; then
        if curl -fsS --max-time 10 "http://127.0.0.1:8080/health" >/dev/null 2>&1 \
        || curl -fsS --max-time 10 "http://127.0.0.1:8080/v1/models" >/dev/null 2>&1; then
            echo "[OK] HTTP API responding on :8080"
        fi
    fi
else
    echo "  service failed to start. Last journal:"
    journalctl -u llm-etl.service -n 30 --no-pager
    exit 1
fi

# Trim old models (keep latest 3)
echo "[*] Cleaning up old model versions (keeping latest 3)…"
cd "$MODELS_DIR"
# shellcheck disable=SC2012
ls -1t etl-parser-*.gguf 2>/dev/null | tail -n +4 | xargs -r rm -v

echo
echo "============================================================"
echo "  [OK] Deployed: $DST_NAME"
echo "  Test from another machine:"
echo "    curl -X POST http://<this-host>:8080/v1/chat/completions \\"
echo "         -H 'Content-Type: application/json' \\"
echo "         -d '{\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}]}'"
echo "============================================================"
