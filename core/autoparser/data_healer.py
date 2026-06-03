"""
data_healer — the VALUE-level half of the self-learning loop.

Pattern (mirrors the unknown-format autoparser, but for data that the
deterministic parser produced yet could not LAND):

    parser ok, but a value isn't in the app-level allowlist
        -> record HELD in the data-quarantine (db_writer)   [not lost]
        -> LLM wakes, inspects the held values + column meaning
        -> decides allow (valid value -> extend allowlist) or map (variant -> known)
        -> rules file (config/value_allowlists.json) updated   [LLM-curated]
        -> held records retried -> they land. Quarantine drains.

The LLM never does DDL; it only curates a JSON allowlist, so healing is safe
and reversible.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

# Human/semantic hints per column so the model knows what it's validating.
COLUMN_HINTS = {
    "catalog_product.unit": (
        "ОКЕИ unit-of-measure codes (Russian OKEI classifier, numeric strings). "
        "Examples: 796=штука/piece, 166=килограмм, 163=грамм, 112=литр, "
        "778=упаковка/pack, 715=пара/pair, 839=комплект/set, 657=изделие."
    ),
}

_SYSTEM = ("You curate allowed-value lists for a retail POS database. "
           "You know the Russian OKEI units classifier and retail enums. "
           "Output ONLY a JSON array, no prose, no code fences.")


def _build_prompt(key: str, current_allow: set[str], new_values: list[str]) -> str:
    hint = COLUMN_HINTS.get(key, f"enum column '{key}'")
    return (
        f"Column: {key}\n"
        f"Meaning: {hint}\n"
        f"Already-allowed values: {sorted(current_allow)}\n"
        f"NEW values seen in real data (decide each): {new_values}\n\n"
        "For EACH new value output one object:\n"
        '  valid value for this column      -> {"value":"<v>","action":"allow"}\n'
        '  typo/variant of an allowed value -> {"value":"<v>","action":"map","map_to":"<allowed>"}\n'
        "Output ONLY the JSON array."
    )


def _parse(raw: str) -> list[dict]:
    raw = re.sub(r"```(?:json)?", "", raw).strip().strip("`").strip()
    i, j = raw.find("["), raw.rfind("]")
    if i == -1 or j == -1:
        return []
    try:
        arr = json.loads(raw[i:j + 1])
    except json.JSONDecodeError:
        return []
    return [d for d in arr if isinstance(d, dict) and "value" in d]


def heal(writer, llm, min_to_heal: int = 1, max_tokens: int = 512) -> dict:
    """Inspect the data-quarantine, let the LLM curate the allowlist, retry held rows."""
    summary = writer.held_summary()
    if not summary:
        return {"healed": [], "retry": None, "note": "no held records"}

    rules_path = Path(writer.rules_path)
    try:
        rules = json.loads(rules_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        rules = {}

    actions = []
    for key, info in summary.items():
        if info["count"] < min_to_heal:
            continue
        new_values = sorted(info["values"].keys())
        current_allow = set((rules.get(key) or {}).get("allow") or [])
        prompt = _build_prompt(key, current_allow, new_values)
        try:
            raw = llm.chat(_SYSTEM, prompt, max_tokens=max_tokens, temperature=0.0)
        except Exception as e:  # noqa: BLE001
            actions.append({"key": key, "error": f"llm failed: {e}"})
            continue
        decisions = {str(d["value"]): d for d in _parse(raw)}

        spec = rules.setdefault(key, {"allow": sorted(current_allow), "map": {}})
        spec.setdefault("allow", [])
        spec.setdefault("map", {})
        for v in new_values:
            d = decisions.get(v)
            if not d:
                continue  # LLM didn't rule on it → stays held, try next round
            if d.get("action") == "allow":
                if v not in spec["allow"]:
                    spec["allow"].append(v)
                actions.append({"key": key, "value": v, "action": "allow"})
            elif d.get("action") == "map" and d.get("map_to"):
                spec["map"][v] = str(d["map_to"])
                actions.append({"key": key, "value": v, "action": "map", "map_to": str(d["map_to"])})

    rules_path.write_text(json.dumps(rules, ensure_ascii=False, indent=2), encoding="utf-8")
    retry = writer.retry_held()   # reloads rules, re-attempts held rows
    return {"healed": actions, "retry": retry}
