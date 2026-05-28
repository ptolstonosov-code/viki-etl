"""
Fine-tune the base LLM on protocol→DB-records examples using QLoRA.

Usage:
    python -m training.finetune
or programmatically:
    from training.finetune import run_training
    run_training()

Hyperparameters live in config/training.yaml.
After training:
  * LoRA adapter weights saved to models/etl_lora_adapter/
  * Optionally merged into a full model in models/etl_merged/
Set inference_backend: "hf" in config/model.yaml to switch parsing to the
fine-tuned model.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from training.dataset_builder import build_dataset  # noqa: E402
from core.llm_client import _load_yaml  # noqa: E402


def _compute_dtype(name: str):
    import torch
    return {"bfloat16": torch.bfloat16, "float16": torch.float16}.get(name, torch.bfloat16)


def _check_cuda():
    try:
        import torch
        if not torch.cuda.is_available():
            print("⚠ CUDA not available. Fine-tuning will be extremely slow on CPU.")
            print("  Recommend running on a machine with NVIDIA GPU + CUDA drivers.")
            return False
        device = torch.cuda.get_device_name(0)
        mem = torch.cuda.get_device_properties(0).total_memory / 1024**3
        print(f"✓ CUDA device: {device} ({mem:.1f} GB VRAM)")
        return True
    except ImportError:
        print("✗ PyTorch not installed. Run: pip install -r requirements.txt")
        sys.exit(1)


def run_training():
    """Main entry — load configs, build dataset, run LoRA fine-tune."""
    _check_cuda()

    import torch
    from transformers import (  # type: ignore
        AutoModelForCausalLM,
        AutoTokenizer,
    )
    from peft import LoraConfig  # type: ignore
    from trl import SFTConfig, SFTTrainer  # type: ignore

    model_cfg = _load_yaml("model.yaml")
    training_cfg = _load_yaml("training.yaml")

    base_id = model_cfg["hf"]["base_model_id"]
    print(f"\n▶ Loading base model: {base_id}")

    # ── Optional QLoRA quantization (off by default for 1.5B) ────────────
    q_cfg = training_cfg["quantization"]
    bnb_config = None
    if q_cfg.get("enabled", False):
        from transformers import BitsAndBytesConfig  # type: ignore
        from peft import prepare_model_for_kbit_training  # type: ignore
        bnb_config = BitsAndBytesConfig(
            load_in_4bit=q_cfg["load_in_4bit"],
            bnb_4bit_quant_type=q_cfg["bnb_4bit_quant_type"],
            bnb_4bit_use_double_quant=q_cfg["bnb_4bit_use_double_quant"],
            bnb_4bit_compute_dtype=_compute_dtype(q_cfg["bnb_4bit_compute_dtype"]),
        )

    model = AutoModelForCausalLM.from_pretrained(
        base_id,
        quantization_config=bnb_config,
        torch_dtype=torch.bfloat16 if bnb_config is None else None,
        device_map="auto",
        trust_remote_code=True,
    )
    if bnb_config is not None:
        model = prepare_model_for_kbit_training(model)
    model.config.use_cache = False  # Required for gradient checkpointing

    tokenizer = AutoTokenizer.from_pretrained(base_id, trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "right"

    # ── LoRA adapter ─────────────────────────────────────────────────────
    lora_cfg_raw = training_cfg["lora"]
    lora_config = LoraConfig(
        r=lora_cfg_raw["r"],
        lora_alpha=lora_cfg_raw["lora_alpha"],
        lora_dropout=lora_cfg_raw["lora_dropout"],
        bias=lora_cfg_raw["bias"],
        target_modules=lora_cfg_raw["target_modules"],
        task_type="CAUSAL_LM",
    )

    # ── Dataset ──────────────────────────────────────────────────────────
    print("▶ Building dataset from examples...")
    ds = build_dataset()
    if hasattr(ds, "keys"):
        train_ds, eval_ds = ds["train"], ds["test"]
        print(f"  train={len(train_ds)}  eval={len(eval_ds)}")
    else:
        train_ds, eval_ds = ds, None
        print(f"  train={len(train_ds)} (no eval split — too few examples)")

    # ── SFT training ─────────────────────────────────────────────────────
    # Build kwargs and accept whichever name SFTConfig supports
    # (max_seq_length in trl 0.x → max_length in trl 1.x).
    t = training_cfg["training"]
    import inspect
    sft_sig = set(inspect.signature(SFTConfig.__init__).parameters)
    seq_kwarg = {"max_length": t["max_seq_length"]} if "max_length" in sft_sig else {"max_seq_length": t["max_seq_length"]}
    sft_config = SFTConfig(
        output_dir=t["output_dir"],
        num_train_epochs=t["num_train_epochs"],
        per_device_train_batch_size=t["per_device_train_batch_size"],
        per_device_eval_batch_size=t.get("per_device_eval_batch_size", 1),
        eval_accumulation_steps=t.get("eval_accumulation_steps", 4),
        gradient_accumulation_steps=t["gradient_accumulation_steps"],
        learning_rate=t["learning_rate"],
        lr_scheduler_type=t["lr_scheduler_type"],
        warmup_ratio=t["warmup_ratio"],
        weight_decay=t["weight_decay"],
        max_grad_norm=t["max_grad_norm"],
        logging_steps=t["logging_steps"],
        save_steps=t["save_steps"],
        eval_steps=t["eval_steps"] if eval_ds is not None else None,
        eval_strategy="steps" if eval_ds is not None else "no",
        save_total_limit=t["save_total_limit"],
        load_best_model_at_end=t["load_best_model_at_end"] and eval_ds is not None,
        metric_for_best_model=t.get("metric_for_best_model", "eval_loss"),
        bf16=t.get("bf16", True),
        fp16=t.get("fp16", False),
        optim=t["optim"],
        packing=training_cfg["dataset"].get("packing", False),
        report_to="none",
        gradient_checkpointing=True,
        gradient_checkpointing_kwargs={"use_reentrant": False},
        **seq_kwarg,
    )

    # SFTTrainer signature varies across trl versions:
    #   - trl 0.10–0.12:  `tokenizer=...`
    #   - trl 0.13–0.18:  `processing_class=...`
    #   - trl 1.x:        `processing_class=...` (sometimes auto-detected from model)
    trainer_sig = set(inspect.signature(SFTTrainer.__init__).parameters)
    trainer_kwargs = dict(
        model=model,
        args=sft_config,
        train_dataset=train_ds,
        eval_dataset=eval_ds,
        peft_config=lora_config,
    )
    if "processing_class" in trainer_sig:
        trainer_kwargs["processing_class"] = tokenizer
    elif "tokenizer" in trainer_sig:
        trainer_kwargs["tokenizer"] = tokenizer
    trainer = SFTTrainer(**trainer_kwargs)

    print("\n▶ Starting training...\n")
    trainer.train()

    # ── Save adapter ─────────────────────────────────────────────────────
    out_dir = Path(t["output_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    trainer.save_model(str(out_dir))
    tokenizer.save_pretrained(str(out_dir))
    print(f"\n✓ LoRA adapter saved to {out_dir}")

    # ── Optional: merge LoRA into base for inference ─────────────────────
    post = training_cfg.get("post_training", {})
    if post.get("merge_adapter"):
        print("▶ Merging adapter into base model...")
        from peft import PeftModel  # type: ignore

        merged_dir = Path(post["merged_output_dir"])
        merged_dir.mkdir(parents=True, exist_ok=True)
        base = AutoModelForCausalLM.from_pretrained(
            base_id, torch_dtype=torch.bfloat16, device_map="auto", trust_remote_code=True
        )
        merged = PeftModel.from_pretrained(base, str(out_dir)).merge_and_unload()
        merged.save_pretrained(str(merged_dir))
        tokenizer.save_pretrained(str(merged_dir))
        print(f"✓ Merged model saved to {merged_dir}")

    print("\n=== Training Complete ===")
    print(f"Next step: edit config/model.yaml → inference_backend: 'hf' to use the new model.")


if __name__ == "__main__":
    run_training()
