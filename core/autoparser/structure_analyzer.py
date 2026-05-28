"""
Quick structure analyzer — decides what KIND of parser to build:
xml / json / csv / text.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET


def detect_source_kind(raw: bytes) -> str:
    """
    Return one of: 'xml', 'json', 'csv', 'text'.
    Looks at the first ~2 KB of the payload.
    """
    head = raw[:2048].lstrip()

    if head.startswith(b"<?xml") or head.startswith(b"<"):
        try:
            ET.fromstring(raw[:8192] + b"</fakeRoot>")  # cheap parse-test
            return "xml"
        except ET.ParseError:
            try:
                ET.fromstring(raw)
                return "xml"
            except ET.ParseError:
                pass

    if head.startswith(b"{") or head.startswith(b"["):
        try:
            json.loads(raw.decode("utf-8", errors="replace"))
            return "json"
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass

    try:
        first_line = head.decode("utf-8", errors="ignore").split("\n", 1)[0]
        if (first_line.count(",") >= 3 or first_line.count(";") >= 3) and \
           "<" not in first_line and "{" not in first_line:
            return "csv"
    except Exception:
        pass

    return "text"


def extract_xml_paths(raw: bytes, max_depth: int = 5, max_paths: int = 50) -> list[str]:
    """
    For an XML payload, return a list of XPath-like paths that exist in the file.
    Used by the YAML generator to suggest candidate field locations.
    """
    paths: list[str] = []
    root = ET.fromstring(raw)

    def _strip_ns(t: str) -> str:
        return t.rsplit("}", 1)[-1] if "}" in t else t

    def _walk(elem, cur_path, depth):
        if depth > max_depth or len(paths) >= max_paths:
            return
        tag = _strip_ns(elem.tag)
        new_path = cur_path + "/" + tag if cur_path else tag
        if new_path not in paths:
            paths.append(new_path)
        for child in elem:
            _walk(child, new_path, depth + 1)

    _walk(root, "", 0)
    return paths


def extract_json_paths(raw: bytes, max_paths: int = 50) -> list[str]:
    """For JSON payload, list dot-paths of leaf keys."""
    obj = json.loads(raw.decode("utf-8", errors="replace"))
    paths: set[str] = set()

    def _walk(node, prefix):
        if len(paths) >= max_paths:
            return
        if isinstance(node, dict):
            for k, v in node.items():
                _walk(v, prefix + "." + k if prefix else k)
        elif isinstance(node, list) and node:
            _walk(node[0], prefix + "[]")
        else:
            paths.add(prefix or "$root")

    _walk(obj, "")
    return sorted(paths)


def extract_csv_columns(raw: bytes) -> list[str]:
    """For CSV, return header column names."""
    import csv
    import io
    text = raw.decode("utf-8-sig", errors="replace")
    # detect delimiter
    first_line = text.split("\n", 1)[0]
    delim = ";" if first_line.count(";") > first_line.count(",") else ","
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    try:
        return next(reader)
    except StopIteration:
        return []
