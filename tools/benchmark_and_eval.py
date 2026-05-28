"""
End-to-end benchmark + accuracy eval of a GGUF model via llama-server.

Phase 1: spin up llama-server in CPU-only mode with --threads 4 (simulate RK3566)
Phase 2: send N holdout examples, measure tokens/sec and F1 vs ground truth

Usage:
    python tools/benchmark_and_eval.py \
        --gguf models/etl-parser-q4_k_m.gguf \
        --holdout examples/eval_holdout/ed_real_holdout.jsonl \
        --llama-server llama.cpp/build/bin/llama-server \
        --n 30 \
        --threads 4
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.request
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).parent.parent


def _start_server(llama_server: Path, gguf: Path, port: int, threads: int) -> subprocess.Popen:
    cmd = [
        str(llama_server),
        "--model", str(gguf),
        "--port", str(port),
        "--host", "127.0.0.1",
        # System prompt embeds the full schema.sql (~7-8K tokens). Plus input + output → need 12K.
        # On ARM with 4 GB RAM this will eat ~600 MB extra for KV cache. We may need to shrink the schema prompt later.
        "--ctx-size", "16384",
        "--threads", str(threads),
        "--batch-size", "256",
        "--no-mmap",
        "--n-gpu-layers", "0",  # CPU only — simulate RK3566
    ]
    print(f"[*] starting llama-server (threads={threads}, no-gpu)...")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    # Wait until it's ready
    start = time.time()
    while time.time() - start < 60:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=2) as r:
                if r.status == 200:
                    print(f"[OK] server ready after {time.time() - start:.1f}s")
                    return proc
        except Exception:
            pass
        time.sleep(2)
    proc.kill()
    raise RuntimeError("llama-server didn't start in 60s")


def _load_system_prompt() -> str:
    """Build system prompt same way as core.llm_client does."""
    import yaml
    sys.path.insert(0, str(ROOT))
    from core.llm_client import _load_schema_for_prompt
    cfg = yaml.safe_load((ROOT / "config" / "model.yaml").read_text(encoding="utf-8"))
    return cfg["system_prompt"].replace("{schema}", _load_schema_for_prompt())


def _post(url: str, payload: dict, timeout: int = 600) -> tuple[dict, float]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code}: {body[:500]}") from None
    elapsed = time.time() - t0
    return resp, elapsed


def _extract_json_array(text: str) -> list:
    text = re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("`").strip()
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1:
        return []
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return []


def _records_to_pairs(records: list) -> set:
    """Set of (table, json-of-data) for set-based comparison."""
    out = set()
    for r in records:
        if not isinstance(r, dict):
            continue
        table = r.get("table")
        data = r.get("data") or {}
        # Sort keys for consistent comparison
        key = (table, json.dumps({k: v for k, v in data.items() if v is not None},
                                 sort_keys=True, ensure_ascii=False))
        out.add(key)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gguf", required=True)
    ap.add_argument("--holdout", required=True)
    ap.add_argument("--llama-server", required=True, help="path to llama-server binary")
    ap.add_argument("--n", type=int, default=20, help="number of holdout examples to evaluate")
    ap.add_argument("--threads", type=int, default=4, help="CPU threads (simulate RK3566)")
    ap.add_argument("--port", type=int, default=18080)
    ap.add_argument("--max-tokens", type=int, default=2048)
    args = ap.parse_args()

    gguf = Path(args.gguf)
    if not gguf.is_absolute():
        gguf = ROOT / gguf
    holdout = Path(args.holdout)
    if not holdout.is_absolute():
        holdout = ROOT / holdout
    server = Path(args.llama_server)
    if not server.is_absolute():
        server = ROOT / server

    # Load holdout examples
    examples = []
    with open(holdout, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                examples.append(json.loads(line))
    examples = examples[:args.n]
    print(f"[*] {len(examples)} eval examples loaded")
    print(f"[*] GGUF: {gguf.name} ({gguf.stat().st_size / 1024 / 1024:.0f} MB)")
    print(f"[*] CPU threads: {args.threads}")

    system_prompt = _load_system_prompt()
    proc = _start_server(server, gguf, args.port, args.threads)

    try:
        # Warmup — first request always slower
        print("\n[*] warmup...")
        _post(f"http://127.0.0.1:{args.port}/v1/chat/completions", {
            "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": "ping"}],
            "max_tokens": 5, "temperature": 0,
        })

        per_example_times = []
        per_example_tokens = []
        totals = {"tp": 0, "fp": 0, "fn": 0}
        per_table = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0})

        for i, ex in enumerate(examples):
            user_msg = f"Parse the following data:\n\n<data>\n{ex['input']}\n</data>"
            payload = {
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_msg},
                ],
                "max_tokens": args.max_tokens,
                "temperature": 0,
                "cache_prompt": True,
            }
            try:
                resp, elapsed = _post(f"http://127.0.0.1:{args.port}/v1/chat/completions", payload)
            except Exception as e:
                print(f"  [{i + 1}/{len(examples)}] error: {e}")
                continue

            content = resp["choices"][0]["message"]["content"]
            n_tokens = resp.get("usage", {}).get("completion_tokens", len(content) // 4)
            tps = n_tokens / elapsed if elapsed > 0 else 0
            per_example_times.append(elapsed)
            per_example_tokens.append(n_tokens)

            # Accuracy
            pred = _extract_json_array(content)
            gold = ex["output"]
            pred_set = _records_to_pairs(pred)
            gold_set = _records_to_pairs(gold)
            tp = len(pred_set & gold_set)
            fp = len(pred_set - gold_set)
            fn = len(gold_set - pred_set)
            totals["tp"] += tp
            totals["fp"] += fp
            totals["fn"] += fn

            # Per-table breakdown (TP based on table only)
            gold_tables = {r["table"] for r in gold if isinstance(r, dict)}
            pred_tables = {r["table"] for r in pred if isinstance(r, dict)}
            for t in (gold_tables | pred_tables):
                pg = [r for r in pred if isinstance(r, dict) and r.get("table") == t]
                gg = [r for r in gold if r.get("table") == t]
                pg_set = _records_to_pairs(pg)
                gg_set = _records_to_pairs(gg)
                per_table[t]["tp"] += len(pg_set & gg_set)
                per_table[t]["fp"] += len(pg_set - gg_set)
                per_table[t]["fn"] += len(gg_set - pg_set)

            print(f"  [{i + 1}/{len(examples)}] {elapsed:5.1f}s  {n_tokens:4d} tok  "
                  f"{tps:5.1f} tok/s  TP={tp:2d}/FP={fp:2d}/FN={fn:2d}")

        # ── Summary ────────────────────────────────────────────────────────
        print("\n" + "=" * 60)
        print("BENCHMARK")
        print("=" * 60)
        if per_example_times:
            avg_t = sum(per_example_times) / len(per_example_times)
            avg_tok = sum(per_example_tokens) / len(per_example_tokens)
            tot_tok = sum(per_example_tokens)
            tot_t = sum(per_example_times)
            print(f"  avg per request: {avg_t:.1f}s  ({avg_tok:.0f} tokens)")
            print(f"  throughput:      {tot_tok / tot_t:.1f} tokens/sec")
            print(f"  total time:      {tot_t:.0f}s on {len(per_example_times)} examples")

        print("\n" + "=" * 60)
        print("ACCURACY (set-match on entire record dict)")
        print("=" * 60)
        p = totals["tp"] / (totals["tp"] + totals["fp"]) if (totals["tp"] + totals["fp"]) else 0
        r = totals["tp"] / (totals["tp"] + totals["fn"]) if (totals["tp"] + totals["fn"]) else 0
        f1 = 2 * p * r / (p + r) if (p + r) else 0
        print(f"  precision: {p:.3f}")
        print(f"  recall:    {r:.3f}")
        print(f"  F1:        {f1:.3f}")
        print(f"  (TP={totals['tp']}  FP={totals['fp']}  FN={totals['fn']})")

        print("\n" + "=" * 60)
        print("PER-TABLE F1")
        print("=" * 60)
        for t, d in sorted(per_table.items()):
            tp_, fp_, fn_ = d["tp"], d["fp"], d["fn"]
            p_ = tp_ / (tp_ + fp_) if (tp_ + fp_) else 0
            r_ = tp_ / (tp_ + fn_) if (tp_ + fn_) else 0
            f1_ = 2 * p_ * r_ / (p_ + r_) if (p_ + r_) else 0
            print(f"  {t:30s} P={p_:.2f}  R={r_:.2f}  F1={f1_:.2f}  (TP={tp_}/FP={fp_}/FN={fn_})")

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    main()
