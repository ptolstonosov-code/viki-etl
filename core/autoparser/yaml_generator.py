"""
Generate a YAML parser spec from (samples + LLM-extracted records).

Strategy (XML case — most common for 1С):
  For each table in LLM's records:
    For each column with a known value:
      Find an XML path inside the sample whose text equals (or contains) that value.
      If found in ALL samples → that path is a stable mapping for this column.
  Combine all stable mappings into a YAML 'fields' block.

For JSON / CSV — analogous but using JSON dot-paths or CSV column names.

The result is conservative: better to miss a column than to map it incorrectly.
The autoparser will then run this YAML in shadow mode and refine over time.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml

from .structure_analyzer import detect_source_kind


# ── XML path finder ──────────────────────────────────────────────────────────

def _strip_ns_inplace(root: ET.Element) -> ET.Element:
    for elem in root.iter():
        if "}" in elem.tag:
            elem.tag = elem.tag.split("}", 1)[1]
        new_attrib = {}
        for k, v in elem.attrib.items():
            new_attrib[k.split("}", 1)[1] if "}" in k else k] = v
        elem.attrib = new_attrib
    return root


def _all_xml_paths_to_value(root: ET.Element, value: str) -> list[str]:
    """
    Return list of XPath-like paths in `root` whose text equals `value`.
    Also tries attributes.
    """
    out: list[str] = []
    if value is None or value == "":
        return out
    v_str = str(value).strip()

    def _walk(elem, path_segs):
        # Text
        if elem.text and elem.text.strip() == v_str:
            out.append("/".join(path_segs))
        # Attributes
        for attr_name, attr_val in elem.attrib.items():
            if str(attr_val).strip() == v_str:
                out.append("/".join(path_segs) + f"/@{attr_name}")
        for child in elem:
            _walk(child, path_segs + [child.tag])

    _walk(root, [root.tag])
    return out


def _find_repeating_record_path(roots: list[ET.Element], target_records_per_file: list[int]) -> str:
    """
    Find a path inside the XML that repeats `target_records_per_file[i]` times in roots[i].
    Heuristic: count occurrences of each unique path prefix; pick the one whose
    repetition matches the record count in MOST samples.
    """
    path_counts: dict[str, list[int]] = defaultdict(lambda: [])
    for root in roots:
        seen_counts: dict[str, int] = defaultdict(int)
        for elem in root.iter():
            # Build a path of all ancestors (without indices)
            # Use simpler: just the tag name (most repeating items share a tag like 'Item' or 'Товар')
            tag = elem.tag
            seen_counts[tag] += 1
        # Append count of each tag (0 if not seen) — for alignment
        for tag, n in seen_counts.items():
            path_counts[tag].append(n)

    best_tag = None
    best_score = -1
    for tag, counts in path_counts.items():
        # Need at least len(roots) counts (pad with 0)
        padded = counts + [0] * (len(roots) - len(counts))
        # Score: how many times count == expected
        score = sum(1 for cnt, expected in zip(padded, target_records_per_file) if cnt == expected and expected > 0)
        if score > best_score:
            best_score = score
            best_tag = tag
    return f".//{best_tag}" if best_tag else ".//*"


# ── Per-source-kind generators ───────────────────────────────────────────────

def _generate_xml_yaml(samples: list[bytes], llm_records: list[list[dict]],
                       detect_block: dict, parser_name: str) -> dict:
    """
    Build a YAML spec by finding stable XML paths in all samples.

    For each (table, column, gold_value), check whether the same path
    extracts that value in MULTIPLE samples. If a path works in ≥ 60%
    of samples for a column, we accept it.
    """
    # Parse all samples
    parsed_roots: list[ET.Element] = []
    for raw in samples:
        try:
            root = ET.fromstring(raw)
            _strip_ns_inplace(root)
            parsed_roots.append(root)
        except ET.ParseError:
            parsed_roots.append(None)

    # Group LLM records by table → list of (sample_idx, record)
    by_table: dict[str, list[tuple[int, dict]]] = defaultdict(list)
    for sample_idx, rec_list in enumerate(llm_records):
        for rec in rec_list:
            if isinstance(rec, dict) and "table" in rec:
                by_table[rec["table"]].append((sample_idx, rec))

    # For each table, find common XPath paths for each column
    records_yaml: list[dict] = []
    for table, sample_records in by_table.items():
        # Counts per file (how many records of this table per sample)
        counts_per_file = [0] * len(samples)
        for s_idx, _ in sample_records:
            counts_per_file[s_idx] += 1

        iter_path = _find_repeating_record_path(
            [r for r in parsed_roots if r is not None],
            [c for c, r in zip(counts_per_file, parsed_roots) if r is not None],
        )

        # For each column, find a relative path inside the iter_path element
        all_columns = set()
        for _, rec in sample_records:
            all_columns.update((rec.get("data") or {}).keys())

        column_paths: dict[str, str] = {}
        for col in all_columns:
            # Try to find consistent XPath
            candidate_counts: dict[str, int] = defaultdict(int)
            for s_idx, rec in sample_records:
                root = parsed_roots[s_idx]
                if root is None:
                    continue
                value = (rec.get("data") or {}).get(col)
                if not value:
                    continue
                # Find ALL paths to this value in the sample
                paths = _all_xml_paths_to_value(root, str(value))
                for p in paths:
                    # Use the path's last 1-3 segments as the column expression
                    segs = p.split("/")
                    if len(segs) >= 2:
                        # Take last segment as relative path
                        candidate_counts[segs[-1]] += 1

            if candidate_counts:
                best = max(candidate_counts, key=candidate_counts.get)
                if candidate_counts[best] >= max(1, len(sample_records) // 2):
                    column_paths[col] = best

        if column_paths:
            records_yaml.append({
                "table": table,
                "iter": iter_path,
                "fields": column_paths,
            })

    spec = {
        "name": parser_name,
        "priority": 30,  # shadow parsers start low
        "source_kind": "xml",
        "detect": detect_block,
        "records": records_yaml,
    }
    return spec


def _generate_csv_yaml(samples: list[bytes], llm_records: list[list[dict]],
                       detect_block: dict, parser_name: str) -> dict:
    """
    For CSV: match column names directly. We assume LLM has already mapped them.
    """
    if not samples:
        return {}
    text = samples[0].decode("utf-8-sig", errors="replace")
    first_line = text.split("\n", 1)[0]
    delim = ";" if first_line.count(";") > first_line.count(",") else ","
    headers = [h.strip() for h in first_line.split(delim)]

    # Group records by table
    by_table: dict[str, list[dict]] = defaultdict(list)
    for rec_list in llm_records:
        for rec in rec_list:
            if isinstance(rec, dict):
                by_table[rec["table"]].append(rec.get("data") or {})

    records_yaml: list[dict] = []
    for table, datas in by_table.items():
        if not datas:
            continue
        col_to_header: dict[str, str] = {}
        for col_name in datas[0].keys():
            best_match = None
            for hdr in headers:
                # Loose match: if any data row's col value == header value (unlikely)
                # OR if column name resembles header name (case-insensitive)
                if col_name.lower() in hdr.lower() or hdr.lower() in col_name.lower():
                    best_match = hdr
                    break
            if best_match:
                col_to_header[col_name] = best_match
        if col_to_header:
            records_yaml.append({"table": table, "fields": col_to_header})

    return {
        "name": parser_name,
        "priority": 30,
        "source_kind": "csv",
        "delimiter": delim,
        "detect": detect_block,
        "records": records_yaml,
    }


# ── Public entry point ───────────────────────────────────────────────────────

def generate_yaml_parser(
    *,
    samples: list[bytes],
    llm_records: list[list[dict]],
    parser_name: str,
    detect_signature: str | None = None,
) -> dict:
    """
    Generate a YAML parser spec given samples and their LLM-extracted records.

    Args:
        samples: raw bytes of N sample files
        llm_records: list of length N — each item is the LLM's records for that sample
        parser_name: e.g. "auto_format_d7f3"
        detect_signature: optional unique substring to detect this format

    Returns dict ready to be yaml.dump'd to a .yaml file.
    """
    if not samples or not llm_records:
        return {}

    # Build detect block
    detect_block = {}
    if detect_signature:
        detect_block["contains"] = detect_signature
    else:
        # Use first 80 chars of first sample as detection prefix (rough heuristic)
        head = samples[0][:200].decode("utf-8", errors="ignore").lstrip()
        first_marker = head.split("\n", 1)[0][:80]
        if first_marker:
            detect_block["starts_with"] = first_marker

    kind = detect_source_kind(samples[0])
    if kind == "xml":
        return _generate_xml_yaml(samples, llm_records, detect_block, parser_name)
    if kind == "csv":
        return _generate_csv_yaml(samples, llm_records, detect_block, parser_name)
    # JSON/text fallback — not implemented yet, mark as needing manual help
    return {
        "name": parser_name,
        "priority": -10,
        "source_kind": kind,
        "detect": detect_block,
        "records": [],
        "_note": f"autogenerator does not yet support {kind} — needs manual completion",
    }


def write_yaml(spec: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(spec, f, allow_unicode=True, sort_keys=False)
