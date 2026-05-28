"""
LLM client abstraction — supports Ollama (inference) and HuggingFace (fine-tuned models).
Selected via config/model.yaml → inference_backend.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent


def _load_yaml(name: str) -> dict:
    with open(ROOT / "config" / name, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _load_schema_for_prompt() -> str:
    """
    Return the CREATE TABLE statements + parsing hints, ready to inject
    into the system prompt. Source of truth: config/schema.sql + parsing_hints in schema.yaml.
    """
    schema_cfg = _load_yaml("schema.yaml")
    sql_path = ROOT / schema_cfg["database"]["schema_file"]
    sql_text = sql_path.read_text(encoding="utf-8")
    hints = schema_cfg.get("parsing_hints", "")
    allowed = schema_cfg.get("allowed_tables") or []
    allowed_block = ""
    if allowed:
        allowed_block = f"\nIMPORTANT: only emit records for these tables: {', '.join(allowed)}.\n"
    return f"{sql_text}\n\n{hints}{allowed_block}"


class LLMClient:
    """Unified interface for Ollama and HuggingFace inference."""

    def __init__(self, config: dict | None = None):
        self.config = config or _load_yaml("model.yaml")
        self.backend = self.config.get("inference_backend", "ollama")
        self._schema_block = _load_schema_for_prompt()
        self._system_prompt = self.config["system_prompt"].replace(
            "{schema}", self._schema_block
        )
        self._hf_pipeline = None  # lazy

    # ── Public API ────────────────────────────────────────────────────────────

    def generate(self, user_message: str) -> str:
        if self.backend == "none":
            raise RuntimeError(
                "inference_backend is 'none' — this machine has no LLM running. "
                "Either: (1) point llama_server.url at a remote llama.cpp server, "
                "or (2) annotate examples manually in the UI."
            )
        if self.backend == "llama_server":
            return self._llama_server_generate(user_message)
        if self.backend == "ollama":
            return self._ollama_generate(user_message)
        if self.backend == "hf":
            return self._hf_generate(user_message)
        raise ValueError(f"Unknown backend: {self.backend}")

    def parse_to_records(self, raw_data: str) -> list[dict]:
        """
        Ask the LLM to parse raw_data and return a list of
        {"table": str, "data": dict} records. Retries once on JSON parse failure.
        """
        prompt = f"Parse the following data:\n\n<data>\n{raw_data}\n</data>"
        response = self.generate(prompt)
        try:
            return self._extract_json(response)
        except (json.JSONDecodeError, ValueError) as exc:
            fix_prompt = (
                f"Your previous response was not valid JSON.\n"
                f"Error: {exc}\n"
                f"Raw response:\n{response}\n\n"
                f"Return ONLY the corrected JSON array, nothing else."
            )
            response = self.generate(fix_prompt)
            return self._extract_json(response)

    def test_connection(self) -> bool:
        if self.backend == "none":
            return False
        try:
            resp = self.generate("Reply with the single word: OK")
            return len(resp.strip()) > 0
        except Exception:
            return False

    # ── llama.cpp HTTP server backend ─────────────────────────────────────────

    def _llama_server_generate(self, user_message: str) -> str:
        """
        Call a llama.cpp HTTP server (POST /v1/chat/completions, OpenAI-compatible).
        Works the same whether the server runs on the target ARM device or on the GPU host.
        """
        import urllib.request
        import urllib.error

        cfg = self.config["llama_server"]
        url = cfg["url"].rstrip("/") + "/v1/chat/completions"
        opts = cfg.get("options", {})

        payload = json.dumps({
            "messages": [
                {"role": "system", "content": self._system_prompt},
                {"role": "user", "content": user_message},
            ],
            "temperature": opts.get("temperature", 0.0),
            "top_p": opts.get("top_p", 1.0),
            "max_tokens": opts.get("n_predict", 4096),
            "cache_prompt": opts.get("cache_prompt", True),
            "stream": False,
        }).encode("utf-8")

        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as e:
            raise ConnectionError(
                f"llama.cpp server at {cfg['url']} unreachable: {e}. "
                f"Is llama-server running on the target/GPU host?"
            ) from None
        return body["choices"][0]["message"]["content"]

    # ── Ollama backend ────────────────────────────────────────────────────────

    def _ollama_generate(self, user_message: str) -> str:
        import ollama  # type: ignore

        cfg = self.config["ollama"]
        response = ollama.chat(
            model=cfg["model"],
            messages=[
                {"role": "system", "content": self._system_prompt},
                {"role": "user", "content": user_message},
            ],
            options=cfg.get("options", {}),
        )
        return response["message"]["content"]

    # ── HuggingFace backend (post fine-tuning) ────────────────────────────────

    def _hf_generate(self, user_message: str) -> str:
        pipe = self._get_hf_pipeline()
        messages = [
            {"role": "system", "content": self._system_prompt},
            {"role": "user", "content": user_message},
        ]
        result = pipe(messages, max_new_tokens=2048, do_sample=False)
        return result[0]["generated_text"][-1]["content"]

    def _get_hf_pipeline(self):
        if self._hf_pipeline is not None:
            return self._hf_pipeline

        import torch
        from transformers import (  # type: ignore
            AutoModelForCausalLM,
            AutoTokenizer,
            BitsAndBytesConfig,
            pipeline,
        )
        from peft import PeftModel  # type: ignore

        hf_cfg = self.config["hf"]
        base_id = hf_cfg["base_model_id"]
        adapter_path = hf_cfg.get("adapter_path")

        bnb_config = None
        if hf_cfg.get("load_in_4bit"):
            bnb_config = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_use_double_quant=True,
                bnb_4bit_compute_dtype=torch.bfloat16,
            )

        model = AutoModelForCausalLM.from_pretrained(
            base_id,
            quantization_config=bnb_config,
            device_map="auto",
            trust_remote_code=True,
        )
        tokenizer = AutoTokenizer.from_pretrained(base_id, trust_remote_code=True)

        if adapter_path and Path(adapter_path).exists():
            model = PeftModel.from_pretrained(model, adapter_path)

        self._hf_pipeline = pipeline("text-generation", model=model, tokenizer=tokenizer)
        return self._hf_pipeline

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _extract_json(text: str) -> list[dict]:
        """Extract a JSON array from text, even if wrapped in markdown fences."""
        text = re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("`").strip()
        start = text.find("[")
        end = text.rfind("]")
        if start == -1 or end == -1:
            raise ValueError("No JSON array found in response")
        data = json.loads(text[start : end + 1])
        if not isinstance(data, list):
            raise ValueError("Response is not a JSON array")
        for rec in data:
            if not isinstance(rec, dict) or "table" not in rec or "data" not in rec:
                raise ValueError(f"Record missing 'table' or 'data' keys: {rec}")
        return data
