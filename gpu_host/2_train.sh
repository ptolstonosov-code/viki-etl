#!/usr/bin/env bash
# =============================================================================
# Train (or continue training) the LLM ETL parser on the GPU host.
#
# Uses examples from examples/training_data/*.jsonl
# Output: models/etl_lora_adapter/  (and merged weights in models/etl_merged/)
#
# Usage:
#   ./gpu_host/2_train.sh
#
# Hyper-parameters live in config/training.yaml. Adjust there, not here.
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# venv check
if [ ! -f ".venv/bin/python" ]; then
    echo "  Run ./gpu_host/1_install.sh first."
    exit 1
fi
# shellcheck disable=SC1091
source .venv/bin/activate

# Count examples
N_EX=$(find examples/training_data -name "*.jsonl" -exec wc -l {} + 2>/dev/null | tail -1 | awk '{print $1}')
N_EX=${N_EX:-0}
echo "============================================================"
echo "  Training Qwen2.5-Coder-1.5B-Instruct on $N_EX example lines"
echo "============================================================"

if [ "$N_EX" -lt 5 ]; then
    echo "  ERROR: need at least 5 labelled examples (have $N_EX)."
    echo "  Label more in the UI on the workstation, then re-package."
    exit 1
fi

# Train
python main.py train

echo
echo "============================================================"
echo "  [OK] Training finished."
echo "  Output: models/etl_lora_adapter/"
echo "  Next:   ./gpu_host/3_export_gguf.sh"
echo "============================================================"
