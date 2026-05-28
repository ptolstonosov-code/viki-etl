"""
Quick diagnostic: send ONE holdout example to llama-server and dump
the full raw response so we can see what the model is actually producing.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent.parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--holdout", required=True)
    ap.add_argument("--port", type=int, default=18080)
    ap.add_argument("--n", type=int, default=1)
    ap.add_argument("--max-tokens", type=int, default=1024)
    args = ap.parse_args()

    sys.path.insert(0, str(ROOT))
    import yaml
    from core.llm_client import _load_schema_for_prompt
    cfg = yaml.safe_load((ROOT / "config" / "model.yaml").read_text(encoding="utf-8"))
    system_prompt = cfg["system_prompt"].replace("{schema}", _load_schema_for_prompt())

    examples = []
    with open(args.holdout, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                examples.append(json.loads(line))

    for i, ex in enumerate(examples[:args.n]):
        print(f"\n{'='*70}\nEXAMPLE {i + 1}\n{'='*70}\n")
        print(f"--- INPUT (first 500 chars) ---\n{ex['input'][:500]}\n...")
        print(f"\n--- GOLD OUTPUT ({len(ex['output'])} records) ---")
        for r in ex['output']:
            print(f"  {r['table']:30s} {json.dumps(r['data'], ensure_ascii=False)[:120]}")

        user_msg = f"Parse the following data:\n\n<data>\n{ex['input']}\n</data>"
        body = json.dumps({
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg},
            ],
            "max_tokens": args.max_tokens,
            "temperature": 0,
            "cache_prompt": True,
        }).encode("utf-8")

        t0 = time.time()
        req = urllib.request.Request(
            f"http://127.0.0.1:{args.port}/v1/chat/completions",
            data=body, headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=600) as r:
            resp = json.loads(r.read().decode("utf-8"))
        elapsed = time.time() - t0
        content = resp["choices"][0]["message"]["content"]
        n_tok = resp.get("usage", {}).get("completion_tokens", 0)

        print(f"\n--- MODEL OUTPUT ({n_tok} tokens, {elapsed:.1f}s, {n_tok / elapsed:.1f} tok/s) ---")
        print(content[:3000])
        if len(content) > 3000:
            print(f"... [truncated, total {len(content)} chars]")


if __name__ == "__main__":
    main()
