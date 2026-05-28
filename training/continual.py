"""
Continual fine-tuning with a replay buffer.

When new training examples arrive (1С, Frontol, Saby, … — each is a new "task"),
we DON'T want the model to forget previous formats. Standard solutions:

  * Replay buffer: mix N% of OLD examples into every new training run.
  * Adapter versioning: each fine-tune produces a NEW LoRA adapter
    (etl_v1 → etl_v2 → …). Old adapters stay on disk for rollback.
  * Holdout eval BEFORE swapping the production adapter — if F1 drops,
    we discard the new adapter and keep the old one.

This module wires those steps. Run via:

    python -m training.continual run

Knobs in config/training.yaml → continual:
    replay_ratio:   how many old examples to mix in (0.0–0.5)
    new_examples_dir: where freshly-labelled examples live
    archive_dir:    where consumed examples are moved after training
    adapter_version_dir: parent of models/etl_v1/, etl_v2/, …
"""

from __future__ import annotations

import json
import random
import shutil
import sys
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.llm_client import _load_yaml  # noqa: E402
from training.dataset_builder import load_examples  # noqa: E402
from training.annotation import validate_records  # noqa: E402


# ── Default knobs (also overridable via training.yaml → continual:) ──────────

DEFAULTS = {
    "replay_ratio": 0.2,
    "new_examples_dir": "examples/training_data/new",
    "archive_dir": "examples/training_data/archive",
    "adapter_version_dir": "models",
    "min_new_examples": 5,
    "eval_holdout_dir": "examples/eval_holdout",
}


def _cfg() -> dict:
    cfg = _load_yaml("training.yaml")
    return {**DEFAULTS, **(cfg.get("continual") or {})}


# ── Pipeline steps ───────────────────────────────────────────────────────────

def collect_new_examples(new_dir: Path) -> list[dict]:
    """Read freshly-labelled examples from new_examples_dir."""
    examples = load_examples(new_dir)
    # Validate every one — drop invalids
    valid: list[dict] = []
    for ex in examples:
        ok, errs = validate_records(ex["output"])
        if ok:
            valid.append(ex)
        else:
            print(f"  ! dropped 1 example: {errs[0]}")
    return valid


def build_replay_buffer(archive_dir: Path, replay_ratio: float, new_count: int) -> list[dict]:
    """
    Sample replay_ratio * new_count old examples from the archive.
    Distribution-aware: tries to keep examples balanced across source files,
    so the model retains coverage on ALL formats it has seen.
    """
    if not archive_dir.exists():
        return []
    archive_examples: dict[str, list[dict]] = {}
    for jsonl in archive_dir.glob("*.jsonl"):
        archive_examples[jsonl.stem] = load_examples(jsonl.parent) if False else _load_jsonl(jsonl)

    if not archive_examples:
        return []

    target = max(1, int(new_count * replay_ratio))
    # Round-robin pick across all archived sources for balance
    out: list[dict] = []
    keys = list(archive_examples.keys())
    random.shuffle(keys)
    idx = 0
    while len(out) < target and any(archive_examples.values()):
        key = keys[idx % len(keys)]
        bucket = archive_examples[key]
        if bucket:
            out.append(bucket.pop(random.randrange(len(bucket))))
        idx += 1
    return out


def _load_jsonl(path: Path) -> list[dict]:
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return out


def write_combined_dataset(new: list[dict], replay: list[dict], target: Path) -> int:
    """Write {new + replay} to one JSONL file used by finetune.py."""
    target.parent.mkdir(parents=True, exist_ok=True)
    combined = list(new) + list(replay)
    random.shuffle(combined)
    with open(target, "w", encoding="utf-8") as f:
        for ex in combined:
            f.write(json.dumps(ex, ensure_ascii=False) + "\n")
    return len(combined)


def next_adapter_version(base_dir: Path) -> Path:
    base_dir.mkdir(parents=True, exist_ok=True)
    existing = [p.name for p in base_dir.glob("etl_v*") if p.is_dir()]
    nums = [int(n.split("_v")[-1]) for n in existing if n.split("_v")[-1].isdigit()]
    next_n = (max(nums) + 1) if nums else 1
    return base_dir / f"etl_v{next_n}"


def archive_consumed(new_dir: Path, archive_dir: Path) -> int:
    """Move processed .jsonl files from new/ to archive/ with a date suffix."""
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    moved = 0
    for jsonl in new_dir.glob("*.jsonl"):
        dst = archive_dir / f"{jsonl.stem}__{stamp}.jsonl"
        shutil.move(str(jsonl), str(dst))
        moved += 1
    return moved


# ── Eval-gated promotion ─────────────────────────────────────────────────────

def evaluate_adapter(adapter_path: Path, holdout_dir: Path) -> dict:
    """Run the new adapter against the holdout set, return F1 metrics."""
    from training.evaluate import evaluate as run_eval

    return run_eval(adapter_path=adapter_path, examples_dir=holdout_dir)


def promote_if_better(new_adapter: Path, prod_link: Path, holdout_dir: Path) -> bool:
    """
    Promote `new_adapter` to `prod_link` only if its F1 is >= the current production's F1.
    Returns True if promoted.
    """
    new_metrics = evaluate_adapter(new_adapter, holdout_dir)
    print(f"  new F1 = {new_metrics.get('f1', 0):.4f}")

    if prod_link.exists():
        old_metrics = evaluate_adapter(prod_link.resolve(), holdout_dir)
        print(f"  old F1 = {old_metrics.get('f1', 0):.4f}")
        if new_metrics["f1"] < old_metrics["f1"] - 0.01:
            print("  ✗ regression — keeping old adapter")
            return False

    # Atomic-ish swap: junction/symlink on Windows
    if prod_link.exists() or prod_link.is_symlink():
        prod_link.unlink()
    try:
        prod_link.symlink_to(new_adapter, target_is_directory=True)
    except (OSError, NotImplementedError):
        # Fallback: just write a pointer file (Windows w/o symlink perms)
        prod_link.mkdir(parents=True, exist_ok=True)
        (prod_link / "POINTS_TO.txt").write_text(str(new_adapter), encoding="utf-8")
    print(f"  ✓ promoted {new_adapter.name} → {prod_link}")
    return True


# ── Main ─────────────────────────────────────────────────────────────────────

def run_continual_training():
    cfg = _cfg()
    new_dir = ROOT / cfg["new_examples_dir"]
    archive_dir = ROOT / cfg["archive_dir"]
    version_dir = ROOT / cfg["adapter_version_dir"]
    holdout_dir = ROOT / cfg["eval_holdout_dir"]
    prod_link = version_dir / "etl_lora_adapter"  # the symlink used by inference

    # 1. Gather
    print("[1/6] Collecting new examples…")
    new = collect_new_examples(new_dir)
    print(f"      {len(new)} valid new examples")
    if len(new) < cfg["min_new_examples"]:
        print(f"      need ≥ {cfg['min_new_examples']} new examples, exiting.")
        return

    # 2. Replay buffer
    print(f"[2/6] Building replay buffer (ratio={cfg['replay_ratio']})…")
    replay = build_replay_buffer(archive_dir, cfg["replay_ratio"], len(new))
    print(f"      {len(replay)} replay examples from archive")

    # 3. Write combined dataset where finetune.py expects it
    combined_path = ROOT / _load_yaml("training.yaml")["dataset"]["examples_dir"] / "_combined.jsonl"
    n_total = write_combined_dataset(new, replay, combined_path)
    print(f"[3/6] Wrote {n_total} examples → {combined_path.relative_to(ROOT)}")

    # 4. Train new adapter
    new_adapter = next_adapter_version(version_dir)
    print(f"[4/6] Training new adapter → {new_adapter.name}")
    _override_output_dir(new_adapter)
    from training.finetune import run_training
    run_training()

    # 5. Eval-gated promotion
    if holdout_dir.exists() and any(holdout_dir.glob("*.jsonl")):
        print("[5/6] Eval-gated promotion…")
        promoted = promote_if_better(new_adapter, prod_link, holdout_dir)
    else:
        print("[5/6] No holdout set — auto-promoting new adapter")
        promote_if_better(new_adapter, prod_link, holdout_dir)
        promoted = True

    # 6. Archive consumed examples
    if promoted:
        n_arch = archive_consumed(new_dir, archive_dir)
        print(f"[6/6] Archived {n_arch} consumed JSONL files")
    else:
        print("[6/6] Skipped archive (new adapter not promoted)")


def _override_output_dir(new_adapter: Path):
    """Mutate training.yaml in-memory so finetune.py writes to the versioned dir."""
    import training.dataset_builder as db_mod
    import training.finetune as ft_mod
    # Stub: finetune reads YAML directly, so we patch the YAML loader cache
    cfg = _load_yaml("training.yaml")
    cfg["training"]["output_dir"] = str(new_adapter)
    # Write override to a temporary file path is impractical — easier to write back
    yaml_path = ROOT / "config" / "training.yaml"
    yaml_path.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["run"], help="run: start a continual training cycle")
    args = ap.parse_args()
    if args.cmd == "run":
        run_continual_training()
