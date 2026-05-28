#!/usr/bin/env bash
# =============================================================================
# Build a single tarball that fully deploys the LLM ETL system on a bare
# Debian/Ubuntu ARM machine. Run on the GPU host (which has the trained .gguf).
#
# Output: dist/llm-etl-arm-<date>.tar.gz   (around 1 GB)
#
# Contents:
#   ├── llm-etl/
#   │   ├── README_ARM.txt            ← step-by-step install for end users
#   │   ├── arm_install.sh            ← one-shot installer
#   │   ├── core/                     ← parser + autoparser Python modules
#   │   ├── config/                   ← schema.sql, model.yaml
#   │   ├── target_host/
#   │   │   ├── api_service.py        ← FastAPI app
#   │   │   ├── viewer/               ← Web viewer (FastAPI)
#   │   │   └── systemd/              ← .service files
#   │   ├── models/
#   │   │   └── etl-parser-q4_k_m.gguf  ← THE model (~940 MB)
#   │   ├── system_prompt.txt         ← preloaded full schema prompt
#   │   ├── llama.cpp-source.tar.gz   ← cloned llama.cpp for on-device build
#   │   └── requirements-arm.txt      ← Python deps for ARM
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DIST="$ROOT/dist"
PKG_NAME="llm-etl-arm-$(date +%Y%m%d)"
STAGE="$DIST/$PKG_NAME"

echo "=== building ARM deploy package ==="
echo "  root:   $ROOT"
echo "  output: $DIST/$PKG_NAME.tar.gz"

rm -rf "$STAGE"
mkdir -p "$STAGE"

# 1. Copy the model + Modelfile
echo "[1/7] copying GGUF model"
mkdir -p "$STAGE/models"
if [ -f "$ROOT/models/etl-parser-q4_k_m.gguf" ]; then
    cp "$ROOT/models/etl-parser-q4_k_m.gguf" "$STAGE/models/etl-parser-q4_k_m.gguf"
elif [ -f "$ROOT/dist/etl-parser-q4_k_m.gguf" ]; then
    cp "$ROOT/dist/etl-parser-q4_k_m.gguf" "$STAGE/models/etl-parser-q4_k_m.gguf"
else
    echo "  ! model file not found! aborting."
    exit 1
fi

# 2. Project source
echo "[2/7] copying source code"
for dir in core config target_host; do
    rsync -a --exclude='__pycache__' --exclude='*.pyc' "$ROOT/$dir" "$STAGE/"
done

# 3. Bake the system prompt (schema.sql + parsing_hints) into a file
echo "[3/7] baking system prompt"
"$ROOT/.venv/bin/python" -c "
import sys, yaml
sys.path.insert(0, '$ROOT')
from core.llm_client import _load_schema_for_prompt
cfg = yaml.safe_load(open('$ROOT/config/model.yaml', encoding='utf-8'))
prompt = cfg['system_prompt'].replace('{schema}', _load_schema_for_prompt())
open('$STAGE/system_prompt.txt', 'w', encoding='utf-8').write(prompt)
print(f'  prompt length: {len(prompt)} chars')
"

# 4. llama.cpp source for on-device build (much smaller than prebuilt binaries)
echo "[4/7] bundling llama.cpp source (will be compiled on ARM)"
if [ -d "$ROOT/llama.cpp" ]; then
    git -C "$ROOT/llama.cpp" archive --format=tar.gz HEAD -o "$STAGE/llama.cpp-source.tar.gz"
else
    # Clone fresh if not present
    git clone --depth=1 https://github.com/ggerganov/llama.cpp.git /tmp/llama.cpp-clone
    git -C /tmp/llama.cpp-clone archive --format=tar.gz HEAD -o "$STAGE/llama.cpp-source.tar.gz"
fi

# 5. Python requirements + offline aarch64 wheels for ARM (Debian 11 / py3.9)
echo "[5/7] requirements-arm.txt + downloading aarch64/cp39 wheels"
cp "$ROOT/target_host/requirements-arm.txt" "$STAGE/requirements-arm.txt"

# Download aarch64 wheels for Python 3.9 (Debian 11 Bullseye) so the ARM box
# can pip-install fully offline (pypi.org is DPI-blocked in RU).
mkdir -p "$STAGE/wheels"
# Explicit transitive deps incl. conditional ones (exceptiongroup/sniffio for py<3.11)
# — cross-version pip download misses markers, so list them by hand.
"$ROOT/.venv/bin/pip" download \
    --platform manylinux2014_aarch64 \
    --platform manylinux_2_17_aarch64 \
    --platform manylinux_2_28_aarch64 \
    --python-version 3.9 \
    --implementation cp \
    --abi cp39 \
    --only-binary=:all: \
    --dest "$STAGE/wheels" \
    fastapi uvicorn pydantic pydantic-core python-multipart pyyaml \
    anyio sniffio exceptiongroup starlette click h11 idna \
    typing_extensions typing-inspection annotated-types annotated-doc \
    2>&1 | tail -5 || echo "  ! some wheels failed — check pip output"
echo "  wheels downloaded: $(ls "$STAGE/wheels" | wc -l) files"

# 6. The end-user installer
echo "[6/7] adding arm_install.sh + README"
cp "$ROOT/target_host/arm_install.sh" "$STAGE/arm_install.sh"
chmod +x "$STAGE/arm_install.sh"
cp "$ROOT/target_host/README_ARM.txt" "$STAGE/README_ARM.txt"

# 7. Create the tarball
echo "[7/7] packaging tarball"
cd "$DIST"
tar -czf "$PKG_NAME.tar.gz" "$PKG_NAME"
SIZE_MB=$(du -m "$PKG_NAME.tar.gz" | cut -f1)

echo ""
echo "================================================================"
echo " [OK] Package ready: $DIST/$PKG_NAME.tar.gz ($SIZE_MB MB)"
echo "================================================================"
echo ""
echo "Ship this single .tar.gz to the ARM device, then on the ARM run:"
echo "  tar -xzf $PKG_NAME.tar.gz"
echo "  cd $PKG_NAME"
echo "  sudo bash arm_install.sh"
