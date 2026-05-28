#!/usr/bin/env bash
# =============================================================================
# Merge LoRA → convert to GGUF f16 → quantize Q4_K_M.
# Output: dist/etl-parser-q4_k_m.gguf — ONE file you ship to the target device.
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# shellcheck disable=SC1091
source .venv/bin/activate

LLAMA_CPP="${LLAMA_CPP_DIR:-$ROOT/llama.cpp}"
if [ ! -d "$LLAMA_CPP" ]; then
    echo "  ERROR: llama.cpp not found at $LLAMA_CPP."
    echo "  Re-run ./gpu_host/1_install.sh"
    exit 1
fi

mkdir -p dist
MERGED_DIR="$ROOT/models/etl_merged"
GGUF_F16="$ROOT/dist/etl-parser-f16.gguf"
GGUF_Q4="$ROOT/dist/etl-parser-q4_k_m.gguf"
MODELFILE="$ROOT/dist/etl-parser.modelfile"

echo "[1/4] Merging LoRA adapter into base model…"
python scripts/export_to_ollama.py \
    --skip-ollama-create \
    --name "etl-parser" \
    --quant "q4_k_m" \
    --llama-cpp "$LLAMA_CPP"

# scripts/export_to_ollama.py writes outputs under models/. Move to dist/.
if [ -f "$ROOT/models/etl-parser-q4_k_m.gguf" ]; then
    mv "$ROOT/models/etl-parser-q4_k_m.gguf" "$GGUF_Q4"
fi
if [ -f "$ROOT/models/Modelfile.etl-parser" ]; then
    mv "$ROOT/models/Modelfile.etl-parser" "$MODELFILE"
fi

# Also save the system prompt + schema separately, so the target can serve
# it as a "system message" via llama-server without parsing a Modelfile.
python -c "
import sys, yaml, json
from pathlib import Path
sys.path.insert(0, '.')
from core.llm_client import _load_schema_for_prompt
cfg = yaml.safe_load(Path('config/model.yaml').read_text(encoding='utf-8'))
sys_prompt = cfg['system_prompt'].replace('{schema}', _load_schema_for_prompt())
Path('dist/system_prompt.txt').write_text(sys_prompt, encoding='utf-8')
"

SIZE_MB=$(stat -c%s "$GGUF_Q4" 2>/dev/null | awk '{printf \"%.0f\", $1/1024/1024}')
echo
echo "============================================================"
echo "  [OK] Export complete."
echo "  Ship these two files to the target device:"
echo "    dist/etl-parser-q4_k_m.gguf  (${SIZE_MB} MB)"
echo "    dist/system_prompt.txt"
echo
echo "  Then on the target device run:"
echo "    ./target_host/target_deploy_model.sh /path/to/etl-parser-q4_k_m.gguf"
echo "============================================================"
