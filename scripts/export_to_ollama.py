"""
Export the fine-tuned LoRA adapter as a custom Ollama model.

Pipeline:
  1. Merge LoRA adapter into the base HuggingFace model (full weights).
  2. Convert to GGUF via llama.cpp's convert-hf-to-gguf.py.
  3. Quantize (q4_k_m by default) for compact deployment.
  4. Generate a Modelfile and run `ollama create`.

Result: a new Ollama model named `etl-parser` (configurable) that the
inference host can pull/use exactly like any other Ollama model.

Prerequisites on this machine:
  * Trained adapter at models/etl_lora_adapter/
  * llama.cpp cloned to ./llama.cpp/ (or set $env:LLAMA_CPP_DIR)
  * Ollama installed locally (only needed if you call `ollama create` here;
    otherwise just copy the .gguf + Modelfile to the inference host).

Usage:
  python scripts/export_to_ollama.py
  python scripts/export_to_ollama.py --quant q5_k_m --name etl-parser-v2
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


def _load_yaml(name: str) -> dict:
    with open(ROOT / "config" / name, encoding="utf-8") as f:
        return yaml.safe_load(f)


def merge_lora(merged_dir: Path) -> Path:
    """Merge the LoRA adapter into the base model — produces full HF weights."""
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer

    model_cfg = _load_yaml("model.yaml")
    training_cfg = _load_yaml("training.yaml")
    adapter_dir = ROOT / training_cfg["training"]["output_dir"]
    base_id = model_cfg["hf"]["base_model_id"]

    if not adapter_dir.exists():
        raise FileNotFoundError(f"No trained adapter at {adapter_dir}. Run `python main.py train` first.")

    print(f"▶ Loading base model: {base_id}")
    base = AutoModelForCausalLM.from_pretrained(
        base_id, torch_dtype=torch.bfloat16, device_map="cpu", trust_remote_code=True,
    )
    print(f"▶ Applying LoRA adapter: {adapter_dir}")
    merged = PeftModel.from_pretrained(base, str(adapter_dir)).merge_and_unload()

    merged_dir.mkdir(parents=True, exist_ok=True)
    merged.save_pretrained(merged_dir, safe_serialization=True)
    AutoTokenizer.from_pretrained(base_id, trust_remote_code=True).save_pretrained(merged_dir)
    print(f"✓ Merged model written to {merged_dir}")
    return merged_dir


def hf_to_gguf(merged_dir: Path, gguf_path: Path, llama_cpp_dir: Path) -> Path:
    """Convert HF model to GGUF f16 using llama.cpp."""
    converter = llama_cpp_dir / "convert_hf_to_gguf.py"
    if not converter.exists():
        converter = llama_cpp_dir / "convert-hf-to-gguf.py"  # older name
    if not converter.exists():
        raise FileNotFoundError(
            f"convert_hf_to_gguf.py not found in {llama_cpp_dir}. "
            f"Clone https://github.com/ggerganov/llama.cpp first."
        )

    print(f"▶ HF → GGUF (f16): {gguf_path}")
    subprocess.check_call(
        [sys.executable, str(converter), str(merged_dir),
         "--outfile", str(gguf_path), "--outtype", "f16"],
        cwd=str(llama_cpp_dir),
    )
    return gguf_path


def quantize_gguf(gguf_f16: Path, gguf_q: Path, quant: str, llama_cpp_dir: Path) -> Path:
    """Quantise the f16 GGUF to e.g. q4_k_m for compact Ollama deployment."""
    quantize_bin = None
    for candidate in ("llama-quantize.exe", "quantize.exe", "llama-quantize", "quantize"):
        p = llama_cpp_dir / "build" / "bin" / candidate
        if p.exists():
            quantize_bin = p
            break
    if not quantize_bin:
        raise FileNotFoundError(
            f"llama-quantize binary not found under {llama_cpp_dir}/build/bin/. "
            f"Build llama.cpp first: cmake -B build && cmake --build build --config Release."
        )

    print(f"▶ Quantising → {quant}: {gguf_q}")
    subprocess.check_call([str(quantize_bin), str(gguf_f16), str(gguf_q), quant])
    return gguf_q


def write_modelfile(modelfile_path: Path, gguf_path: Path, system_prompt: str) -> Path:
    """Generate Ollama Modelfile that embeds the system prompt + GGUF."""
    # Escape triple-quotes in the system prompt
    safe_prompt = system_prompt.replace('"""', '\\"\\"\\"')
    contents = f"""FROM {gguf_path.resolve()}

PARAMETER temperature 0
PARAMETER top_p 1
PARAMETER num_ctx 8192

SYSTEM \"\"\"
{safe_prompt}
\"\"\"
"""
    modelfile_path.write_text(contents, encoding="utf-8")
    print(f"✓ Modelfile written to {modelfile_path}")
    return modelfile_path


def ollama_create(name: str, modelfile_path: Path) -> None:
    """Register the model with the local Ollama service."""
    if not shutil.which("ollama"):
        print("⚠ ollama CLI not in PATH. Skipping `ollama create` — "
              "transfer the .gguf + Modelfile to the inference host and run it there.")
        return
    print(f"▶ ollama create {name} -f {modelfile_path}")
    subprocess.check_call(["ollama", "create", name, "-f", str(modelfile_path)])
    print(f"✓ Model '{name}' registered with Ollama. Test with: ollama run {name}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="etl-parser", help="Ollama model name")
    parser.add_argument("--quant", default="q4_k_m",
                        help="GGUF quantisation: q4_k_m | q5_k_m | q8_0 | f16")
    parser.add_argument("--llama-cpp", default=os.environ.get("LLAMA_CPP_DIR", str(ROOT / "llama.cpp")),
                        help="Path to llama.cpp checkout")
    parser.add_argument("--skip-ollama-create", action="store_true",
                        help="Stop after producing the .gguf — useful when exporting from a remote host")
    args = parser.parse_args()

    out_dir = ROOT / "models"
    merged_dir = out_dir / "etl_merged"
    gguf_f16 = out_dir / f"{args.name}-f16.gguf"
    gguf_q = out_dir / f"{args.name}-{args.quant}.gguf"
    modelfile = out_dir / f"Modelfile.{args.name}"

    merge_lora(merged_dir)
    hf_to_gguf(merged_dir, gguf_f16, Path(args.llama_cpp))
    if args.quant != "f16":
        quantize_gguf(gguf_f16, gguf_q, args.quant, Path(args.llama_cpp))
    else:
        gguf_q = gguf_f16

    # Build the same system prompt that was used during training
    from core.llm_client import _load_schema_for_prompt
    model_cfg = _load_yaml("model.yaml")
    system_prompt = model_cfg["system_prompt"].replace("{schema}", _load_schema_for_prompt())

    write_modelfile(modelfile, gguf_q, system_prompt)

    if not args.skip_ollama_create:
        ollama_create(args.name, modelfile)

    print("\n=== Export Complete ===")
    print(f"  Merged HF model: {merged_dir}")
    print(f"  GGUF (quant):    {gguf_q}")
    print(f"  Modelfile:       {modelfile}")
    print("\nTo use on the inference host:")
    print(f"  1. scp / robocopy {gguf_q.name} + {modelfile.name} to that host")
    print(f"  2. ollama create {args.name} -f {modelfile.name}")
    print(f"  3. Edit config/model.yaml → ollama.model: \"{args.name}\"")


if __name__ == "__main__":
    main()
