"""
Evaluate a trained adapter against a holdout set.

Metrics:
  * per-table precision/recall/F1 of correctly-emitted records
  * per-column field-level accuracy (LLM produced the same value as ground truth)
  * overall F1 (macro across tables)

A "record match" = same table name AND same set of (key, value) pairs.
Order doesn't matter; missing optional fields don't count against you
(only keys present in BOTH ground truth and prediction are compared).

Used by training/continual.py to gate adapter promotion.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from training.dataset_builder import load_examples  # noqa: E402


def _record_key(rec: dict) -> tuple:
    """Hashable representation: (table, frozenset of (key, str(value)) pairs)."""
    table = rec["table"]
    items = frozenset((k, json.dumps(v, sort_keys=True, ensure_ascii=False))
                      for k, v in (rec.get("data") or {}).items() if v is not None)
    return (table, items)


def _per_table_pr(gold: list[dict], pred: list[dict]) -> dict[str, dict]:
    """Aggregate TP/FP/FN per table."""
    by_table: dict[str, dict] = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0})
    gold_keys: dict[str, list[tuple]] = defaultdict(list)
    for r in gold:
        gold_keys[r["table"]].append(_record_key(r))
    pred_keys: dict[str, list[tuple]] = defaultdict(list)
    for r in pred:
        pred_keys[r["table"]].append(_record_key(r))

    all_tables = set(gold_keys) | set(pred_keys)
    for t in all_tables:
        g = list(gold_keys[t])
        p = list(pred_keys[t])
        # Greedy match
        for pk in list(p):
            if pk in g:
                by_table[t]["tp"] += 1
                g.remove(pk)
                p.remove(pk)
        by_table[t]["fp"] += len(p)
        by_table[t]["fn"] += len(g)
    return by_table


def evaluate(adapter_path: Path | None = None, examples_dir: Path | None = None) -> dict:
    """
    Run the current LLMClient over every example in examples_dir,
    compare to ground truth, return aggregate metrics.

    If adapter_path is provided, model.yaml's hf.adapter_path is overridden.
    """
    from core.llm_client import LLMClient
    import yaml

    if adapter_path is not None:
        model_cfg_path = ROOT / "config" / "model.yaml"
        cfg = yaml.safe_load(model_cfg_path.read_text(encoding="utf-8"))
        cfg["inference_backend"] = "hf"
        cfg["hf"]["adapter_path"] = str(adapter_path)
        client = LLMClient(config=cfg)
    else:
        client = LLMClient()

    examples_dir = examples_dir or (ROOT / "examples" / "eval_holdout")
    examples = load_examples(examples_dir)
    if not examples:
        return {"examples": 0, "f1": 0.0, "precision": 0.0, "recall": 0.0, "per_table": {}}

    totals = {"tp": 0, "fp": 0, "fn": 0}
    per_table_totals: dict[str, dict] = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0})

    for ex in examples:
        try:
            pred = client.parse_to_records(ex["input"])
        except Exception as e:
            print(f"  inference failed: {e}")
            pred = []
        per_table = _per_table_pr(ex["output"], pred)
        for t, d in per_table.items():
            for k in ("tp", "fp", "fn"):
                per_table_totals[t][k] += d[k]
                totals[k] += d[k]

    p = totals["tp"] / (totals["tp"] + totals["fp"]) if (totals["tp"] + totals["fp"]) else 0.0
    r = totals["tp"] / (totals["tp"] + totals["fn"]) if (totals["tp"] + totals["fn"]) else 0.0
    f1 = 2 * p * r / (p + r) if (p + r) else 0.0

    per_table_metrics: dict[str, dict] = {}
    for t, d in per_table_totals.items():
        tp_, fp_, fn_ = d["tp"], d["fp"], d["fn"]
        tp_p = tp_ / (tp_ + fp_) if (tp_ + fp_) else 0.0
        tp_r = tp_ / (tp_ + fn_) if (tp_ + fn_) else 0.0
        tp_f1 = 2 * tp_p * tp_r / (tp_p + tp_r) if (tp_p + tp_r) else 0.0
        per_table_metrics[t] = {
            "precision": round(tp_p, 4),
            "recall": round(tp_r, 4),
            "f1": round(tp_f1, 4),
            "support": tp_ + fn_,
        }

    return {
        "examples": len(examples),
        "precision": round(p, 4),
        "recall": round(r, 4),
        "f1": round(f1, 4),
        "per_table": per_table_metrics,
    }


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--adapter", help="Path to adapter dir (defaults to model.yaml hf.adapter_path)")
    ap.add_argument("--holdout", default="examples/eval_holdout", help="Folder of JSONL eval examples")
    args = ap.parse_args()

    metrics = evaluate(
        adapter_path=Path(args.adapter) if args.adapter else None,
        examples_dir=Path(args.holdout) if args.holdout else None,
    )
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
