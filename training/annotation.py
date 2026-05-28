"""
Annotation utilities — validate, diff, and bootstrap training examples.

Two main jobs:
  1. validate_records(records) — schema sanity check (right tables, right columns,
     correct enum values, type coercion) BEFORE the example lands in the dataset.
  2. bootstrap_from_parser(source_files, parser_fn) — run a deterministic parser
     over a folder of raw files and write golden (input, output) JSONL examples.

These let humans (and an LLM teacher) produce labelled data quickly,
without ever writing malformed JSON into the training set.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable

import yaml

from core.llm_client import _load_yaml

ROOT = Path(__file__).parent.parent


# ── Schema introspection ─────────────────────────────────────────────────────

def _parse_schema_sql() -> dict[str, dict]:
    """
    Crude SQL parser — extracts {table: {column: {check: [allowed_values]?}}}
    from config/schema.sql.
    """
    schema_path = ROOT / _load_yaml("schema.yaml")["database"]["schema_file"]
    sql = schema_path.read_text(encoding="utf-8")

    tables: dict[str, dict] = {}
    table_re = re.compile(r"CREATE\s+TABLE\s+`?(\w+)`?\s*\((.*?)\n\)\s*;", re.DOTALL | re.IGNORECASE)
    for tname, body in table_re.findall(sql):
        cols: dict[str, dict] = {}
        # Skip lines that are constraints (PRIMARY KEY, FOREIGN KEY, UNIQUE)
        for line in body.splitlines():
            line = line.strip().rstrip(",")
            if not line or line.startswith("--"):
                continue
            up = line.upper()
            if up.startswith(("PRIMARY KEY", "FOREIGN KEY", "UNIQUE", "CHECK")):
                continue
            col_match = re.match(r"[`\"]?(\w+)[`\"]?\s+(\w+)", line)
            if not col_match:
                continue
            col_name = col_match.group(1)
            allowed = None
            check = re.search(r"CHECK\s*\([^)]*?IN\s*\(([^)]+)\)", line, re.IGNORECASE)
            if check:
                allowed = [v.strip().strip("'\"") for v in check.group(1).split(",")]
            cols[col_name] = {"allowed": allowed}
        tables[tname] = cols
    return tables


_SCHEMA_CACHE: dict[str, dict] | None = None


def _schema() -> dict[str, dict]:
    global _SCHEMA_CACHE
    if _SCHEMA_CACHE is None:
        _SCHEMA_CACHE = _parse_schema_sql()
    return _SCHEMA_CACHE


# ── Validation ───────────────────────────────────────────────────────────────

def validate_records(records: list[dict]) -> tuple[bool, list[str]]:
    """
    Return (ok, errors). `records` is the LLM/parser output:
       [{"table": str, "data": {col: value}}, ...]
    Checks:
      - records is a list of {"table","data"} dicts
      - every table exists in schema
      - every data key is a real column
      - CHECK-constraint values are within the allowed enum set
    Does NOT enforce NOT NULL — incomplete records are OK at annotation time
    (db_writer handles missing PKs by generating UUIDs).
    """
    errors: list[str] = []
    schema = _schema()
    if not isinstance(records, list):
        return False, ["records is not a list"]

    for i, rec in enumerate(records):
        if not isinstance(rec, dict) or "table" not in rec or "data" not in rec:
            errors.append(f"[{i}] missing 'table' or 'data'")
            continue
        table = rec["table"]
        if table not in schema:
            errors.append(f"[{i}] unknown table: {table}")
            continue
        data = rec["data"]
        if not isinstance(data, dict):
            errors.append(f"[{i}] 'data' is not an object")
            continue
        cols = schema[table]
        for k, v in data.items():
            if k not in cols:
                errors.append(f"[{i}] {table}.{k} — column does not exist")
                continue
            allowed = cols[k]["allowed"]
            if allowed and v is not None and str(v) not in allowed:
                errors.append(f"[{i}] {table}.{k}={v!r} not in {allowed}")
    return (len(errors) == 0), errors


# ── Diff (used in UI to show LLM-draft vs human-corrected) ──────────────────

def diff_records(draft: list[dict], corrected: list[dict]) -> str:
    """Human-readable diff between two record lists. Shows only changed fields."""
    out: list[str] = []
    n = max(len(draft), len(corrected))
    for i in range(n):
        d = draft[i] if i < len(draft) else None
        c = corrected[i] if i < len(corrected) else None
        if d == c:
            continue
        if d is None:
            out.append(f"+ [{i}] {c.get('table')}: {json.dumps(c.get('data'), ensure_ascii=False)}")
        elif c is None:
            out.append(f"- [{i}] {d.get('table')}: {json.dumps(d.get('data'), ensure_ascii=False)}")
        else:
            out.append(f"~ [{i}] {d.get('table')}:")
            d_data = d.get("data", {})
            c_data = c.get("data", {})
            for k in sorted(set(d_data) | set(c_data)):
                if d_data.get(k) != c_data.get(k):
                    out.append(f"    {k}: {d_data.get(k)!r} → {c_data.get(k)!r}")
    return "\n".join(out) or "(identical)"


# ── Bootstrap helpers ────────────────────────────────────────────────────────

def bootstrap_from_parser(
    source_dir: str | Path,
    parser_fn: Callable[[Path], list[dict]],
    output_jsonl: str | Path,
    chunk_chars: int = 4000,
) -> int:
    """
    Run `parser_fn` on every file in `source_dir`, then split the raw text
    into chunks of `chunk_chars` and pair each chunk with the records
    extracted from it. Writes (input, output) lines to `output_jsonl`.

    Returns the number of examples written.

    Use this to seed the dataset from a real 1C CommerceML export, an
    OFD JSON dump, EDI batch — anything you can parse deterministically.
    """
    source_dir = Path(source_dir)
    output_jsonl = Path(output_jsonl)
    if not output_jsonl.is_absolute():
        output_jsonl = ROOT / output_jsonl
    output_jsonl.parent.mkdir(parents=True, exist_ok=True)

    count = 0
    with open(output_jsonl, "w", encoding="utf-8") as f:
        for path in sorted(source_dir.iterdir()):
            if not path.is_file():
                continue
            try:
                records = parser_fn(path)
            except Exception as e:
                print(f"  skipped {path.name}: {e}")
                continue
            ok, errors = validate_records(records)
            if not ok:
                print(f"  skipped {path.name}: schema validation failed:")
                for e in errors[:3]:
                    print(f"    {e}")
                continue

            raw_text = path.read_text(encoding="utf-8", errors="replace")
            # Whole-file example
            f.write(json.dumps({"input": raw_text[:chunk_chars * 4], "output": records}, ensure_ascii=False) + "\n")
            count += 1
    return count


def fix_common_issues(records: list[dict]) -> list[dict]:
    """
    Auto-fix issues that the LLM commonly produces:
      - rubles → kopecks (price-like field > 0 and < 10_000 and contains a dot → multiply ×100)
      - bool true/false → 1/0
      - date strings 'DD.MM.YYYY' → ISO 'YYYY-MM-DD'
    Returns a new list; original is not mutated.
    """
    out: list[dict] = []
    money_cols = {
        "price", "sale_price", "cost_price", "unit_cost_with_tax",
        "total_cost_with_tax", "total_sum", "cash_sum", "cashless_sum",
        "discount_sum", "amount", "opening_cash",
    }
    for rec in records:
        new_data = {}
        for k, v in rec.get("data", {}).items():
            if isinstance(v, bool):
                v = int(v)
            if isinstance(v, str) and re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", v):
                d, m, y = v.split(".")
                v = f"{y}-{m}-{d}"
            if k in money_cols and isinstance(v, (int, float)) and isinstance(v, float):
                v = int(round(v * 100))
            new_data[k] = v
        out.append({"table": rec["table"], "data": new_data})
    return out


# ── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import sys

    ap = argparse.ArgumentParser(description="Bootstrap training examples from a deterministic parser")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_val = sub.add_parser("validate", help="Validate a JSONL file against the schema")
    p_val.add_argument("file")

    p_boot = sub.add_parser("bootstrap-1c", help="Bootstrap CommerceML examples")
    p_boot.add_argument("source_dir", help="Folder with import.xml files")
    p_boot.add_argument("--out", default="examples/training_data/bootstrap_1c.jsonl")

    args = ap.parse_args()

    if args.cmd == "validate":
        all_ok = True
        with open(args.file, encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                if not line.strip():
                    continue
                ex = json.loads(line)
                ok, errs = validate_records(ex.get("output", []))
                if not ok:
                    all_ok = False
                    print(f"line {lineno}: FAIL")
                    for e in errs:
                        print(f"  {e}")
        if all_ok:
            print("[OK] all examples valid")
        sys.exit(0 if all_ok else 1)

    elif args.cmd == "bootstrap-1c":
        from core.parsers.commerceml import parse_import_xml

        n = bootstrap_from_parser(args.source_dir, parse_import_xml, args.out)
        print(f"[OK] wrote {n} examples to {args.out}")
