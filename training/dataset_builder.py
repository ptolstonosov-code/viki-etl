"""
Build a HuggingFace Dataset from training examples in JSONL format.

Each line of a .jsonl file must be:
{
  "input":  "<raw protocol text or hex dump>",
  "output": [{"table": "...", "data": {...}}, ...]
}

The builder converts these to chat-formatted examples for SFTTrainer.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

import yaml

from core.llm_client import _load_schema_for_prompt, _load_yaml

if TYPE_CHECKING:
    from datasets import Dataset

ROOT = Path(__file__).parent.parent


def load_examples(examples_dir: str | Path) -> list[dict]:
    """Read all .jsonl files under examples_dir."""
    path = Path(examples_dir)
    if not path.is_absolute():
        path = ROOT / path
    examples: list[dict] = []
    if not path.exists():
        return examples
    for jsonl_file in sorted(path.glob("*.jsonl")):
        with open(jsonl_file, encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if "input" not in obj or "output" not in obj:
                        raise ValueError("missing 'input' or 'output'")
                    examples.append(obj)
                except (json.JSONDecodeError, ValueError) as e:
                    print(f"  ⚠ {jsonl_file.name}:{lineno} skipped — {e}")
    return examples


def _build_messages(example: dict, system_prompt: str) -> list[dict]:
    return [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": f"Parse the following data:\n\n<data>\n{example['input']}\n</data>",
        },
        {
            "role": "assistant",
            "content": json.dumps(example["output"], ensure_ascii=False),
        },
    ]


def build_dataset(examples_dir: str | Path | None = None) -> "Dataset":
    """
    Read examples from disk and produce a HuggingFace Dataset
    with one "messages" field per row (chat format).
    """
    from datasets import Dataset  # heavy import — only when training

    model_cfg = _load_yaml("model.yaml")
    training_cfg = _load_yaml("training.yaml")
    schema_block = _load_schema_for_prompt()
    system_prompt = model_cfg["system_prompt"].replace("{schema}", schema_block)

    examples_dir = examples_dir or training_cfg["dataset"]["examples_dir"]
    raw_examples = load_examples(examples_dir)
    if not raw_examples:
        raise ValueError(
            f"No training examples found in {examples_dir}. "
            f"Add .jsonl files with 'input'/'output' pairs first."
        )

    rows = [{"messages": _build_messages(ex, system_prompt)} for ex in raw_examples]

    ds = Dataset.from_list(rows)
    val_split = training_cfg["dataset"].get("validation_split", 0.1)
    if val_split > 0 and len(ds) >= 10:
        return ds.train_test_split(test_size=val_split, seed=42)
    return ds


def example_count(examples_dir: str | Path | None = None) -> int:
    examples_dir = examples_dir or _load_yaml("training.yaml")["dataset"]["examples_dir"]
    return len(load_examples(examples_dir))


def add_example(raw_input: str, parsed_output: list[dict], filename: str = "user_added.jsonl") -> Path:
    """Append a new (input, output) pair to a JSONL file. Used by UI 'Add Example' tab."""
    examples_dir = ROOT / _load_yaml("training.yaml")["dataset"]["examples_dir"]
    examples_dir.mkdir(parents=True, exist_ok=True)
    target = examples_dir / filename
    record = {"input": raw_input, "output": parsed_output}
    with open(target, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return target
