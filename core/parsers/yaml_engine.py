"""
YAML-driven parser engine.

When the autoparser generates a new parser, it writes a YAML file that
describes:
  - how to detect the format
  - how to map fields from the raw payload to DB records

This module interprets that YAML at runtime — no code generation, no
exec(). Safe to deploy automatically.

Supported source kinds:
  - xml   — uses ElementTree paths
  - json  — uses simple dot-paths
  - csv   — column-name mappings
  - text  — regex-based extraction (last resort)

EXAMPLE YAML:
─────────────
name: "Frontol Export v6"
priority: 80
source_kind: xml
detect:
  starts_with: "<FrontolExport"
  contains: ["<Item", "<Article"]
records:
  - table: catalog_product
    iter: ".//Item"
    fields:
      id:        "@uuid"           # XML attribute
      name:      "Name"            # child text
      tax_rate:  "TaxRate | nds"   # value piped through transformer
      unit:      "Unit | okei"
  - table: variant_article
    iter: ".//Item"
    fields:
      variant_id: "@uuid"
      article:    "Article"
  - table: variant_price
    iter: ".//Item[Price]"         # only items that have <Price>
    fields:
      variant_id: "@uuid"
      price:      "Price | rub_to_kop"
─────────────

Available transformers (pipe operator |):
  nds          → "20"/"22"/"БезНДС" → "NDS_22"/"NDS_NO_TAX"/...
  okei         → unit name "шт"/"кг" → ОКЕИ "796"/"166"
  rub_to_kop   → "150.50" → 15050
  trim         → strip whitespace
  upper / lower
  int          → "5" → 5
"""

from __future__ import annotations

import csv
import io
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import yaml

from .base import Parser


# ── Transformers ─────────────────────────────────────────────────────────────

_NDS_MAP = {
    "0": "NDS_NO_TAX", "5": "NDS_5", "7": "NDS_7", "10": "NDS_10",
    "20": "NDS_22", "22": "NDS_22",
    "БезНДС": "NDS_NO_TAX", "Без НДС": "NDS_NO_TAX",
}
_OKEI_MAP = {
    "шт": "796", "штука": "796",
    "кг": "166", "килограмм": "166",
    "г": "163", "грамм": "163",
    "л": "112", "литр": "112",
    "мл": "111",
    "м": "006",
    "уп": "778",
}


def _t_nds(v):
    if v is None:
        return None
    s = str(v).strip()
    return _NDS_MAP.get(s, "NDS_NO_TAX")


def _t_okei(v):
    if v is None:
        return None
    s = str(v).strip().lower()
    if s.isdigit():
        return s.zfill(3) if len(s) < 3 else s
    return _OKEI_MAP.get(s, "796")


def _t_rub_to_kop(v):
    if v is None:
        return None
    try:
        return int(round(float(str(v).replace(",", ".")) * 100))
    except (ValueError, TypeError):
        return None


def _t_int(v):
    if v is None:
        return None
    try:
        return int(float(str(v).replace(",", ".")))
    except (ValueError, TypeError):
        return None


_TRANSFORMERS = {
    "nds":        _t_nds,
    "okei":       _t_okei,
    "rub_to_kop": _t_rub_to_kop,
    "int":        _t_int,
    "trim":       lambda v: str(v).strip() if v is not None else None,
    "upper":      lambda v: str(v).upper() if v is not None else None,
    "lower":      lambda v: str(v).lower() if v is not None else None,
}


def _apply_transformer(value, name: str):
    fn = _TRANSFORMERS.get(name.strip())
    if fn is None:
        return value
    return fn(value)


def _extract_with_pipes(value: Any, expr: str) -> Any:
    """Apply a chain of '| transformer' suffixes to value."""
    parts = expr.split("|")
    # First part is the field expression (already extracted by caller)
    for tr in parts[1:]:
        value = _apply_transformer(value, tr)
    return value


# ── XML extraction ───────────────────────────────────────────────────────────

def _strip_ns(tree_root: ET.Element) -> ET.Element:
    """Recursively remove XML namespaces — makes paths simple."""
    for elem in tree_root.iter():
        if "}" in elem.tag:
            elem.tag = elem.tag.split("}", 1)[1]
        # Strip attribute namespaces too
        new_attrib = {}
        for k, v in elem.attrib.items():
            new_attrib[k.split("}", 1)[1] if "}" in k else k] = v
        elem.attrib = new_attrib
    return tree_root


def _xml_field(elem: ET.Element, path_expr: str) -> Any:
    """
    Extract a field from XML element.
    Path syntax:
      "Name"           → child element text
      "@id"            → attribute
      ".//Inner/Tag"   → XPath-style descendant
      "Name | trim"    → transformer pipeline
    """
    # Split path from transformers
    pipe_parts = path_expr.split("|", 1)
    raw_path = pipe_parts[0].strip()

    if raw_path.startswith("@"):
        value = elem.attrib.get(raw_path[1:])
    elif raw_path == ".":
        value = elem.text.strip() if elem.text else None
    else:
        # Find with ElementTree XPath
        try:
            target = elem.find(raw_path)
            value = (target.text.strip() if target is not None and target.text else None)
        except SyntaxError:
            value = None

    return _extract_with_pipes(value, path_expr)


def _parse_xml(raw: bytes, spec: dict) -> list[dict]:
    root = ET.fromstring(raw)
    _strip_ns(root)
    records: list[dict] = []
    for rec_spec in spec.get("records", []):
        table = rec_spec["table"]
        iter_path = rec_spec.get("iter", ".")
        fields = rec_spec.get("fields", {})

        if iter_path == ".":
            elements = [root]
        else:
            try:
                elements = root.findall(iter_path)
            except SyntaxError:
                continue

        for el in elements:
            data = {}
            for col, expr in fields.items():
                if expr.startswith("constant:"):
                    data[col] = expr[len("constant:"):]
                else:
                    data[col] = _xml_field(el, expr)
            # Drop the record if all required fields are None
            if any(v is not None for v in data.values()):
                records.append({"table": table, "data": data})
    return records


# ── JSON extraction ──────────────────────────────────────────────────────────

def _json_field(obj: Any, path: str) -> Any:
    """Dot-path resolver. 'a.b.0.c' → obj['a']['b'][0]['c']."""
    pipe_parts = path.split("|", 1)
    raw_path = pipe_parts[0].strip()
    cur = obj
    for part in raw_path.split("."):
        if part == "":
            continue
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return _extract_with_pipes(cur, path)


def _parse_json(raw: bytes, spec: dict) -> list[dict]:
    obj = json.loads(raw.decode("utf-8"))
    records: list[dict] = []
    for rec_spec in spec.get("records", []):
        table = rec_spec["table"]
        iter_path = rec_spec.get("iter", "")
        fields = rec_spec.get("fields", {})
        items = _json_field(obj, iter_path) if iter_path else [obj]
        if not isinstance(items, list):
            items = [items] if items else []
        for item in items:
            data = {col: _json_field(item, expr) for col, expr in fields.items()}
            if any(v is not None for v in data.values()):
                records.append({"table": table, "data": data})
    return records


# ── CSV extraction ───────────────────────────────────────────────────────────

def _parse_csv(raw: bytes, spec: dict) -> list[dict]:
    text = raw.decode("utf-8-sig")  # strip BOM if any
    delim = spec.get("delimiter", ",")
    reader = csv.DictReader(io.StringIO(text), delimiter=delim)
    rows = list(reader)
    records: list[dict] = []
    for rec_spec in spec.get("records", []):
        table = rec_spec["table"]
        fields = rec_spec.get("fields", {})
        for row in rows:
            data = {}
            for col, expr in fields.items():
                if expr.startswith("constant:"):
                    data[col] = expr[len("constant:"):]
                    continue
                pipe_parts = expr.split("|", 1)
                src_col = pipe_parts[0].strip()
                val = row.get(src_col)
                data[col] = _extract_with_pipes(val, expr)
            if any(v is not None for v in data.values()):
                records.append({"table": table, "data": data})
    return records


# ── Detection ────────────────────────────────────────────────────────────────

def _matches_detect(raw: bytes, filename: str | None, detect: dict) -> bool:
    """Decide whether the YAML's detect block matches this payload."""
    if not detect:
        return False

    head = raw[:1024]
    text_head = ""
    try:
        text_head = head.decode("utf-8", errors="ignore")
    except Exception:
        pass

    if "starts_with" in detect:
        prefix = detect["starts_with"]
        if isinstance(prefix, str):
            if not text_head.lstrip().startswith(prefix):
                return False
    if "contains" in detect:
        needles = detect["contains"]
        if isinstance(needles, str):
            needles = [needles]
        for n in needles:
            if n not in text_head:
                return False
    if "filename_pattern" in detect and filename:
        import fnmatch
        if not fnmatch.fnmatch(filename.lower(), detect["filename_pattern"].lower()):
            return False
    return True


# ── Parser plugin ────────────────────────────────────────────────────────────

class YamlParser(Parser):
    """A parser whose behaviour is fully described by a YAML file."""

    def __init__(self, spec: dict, source_path: Path | None = None):
        self.spec = spec
        self.name = spec.get("name", "yaml_parser")
        self.priority = int(spec.get("priority", 50))
        self.enabled = bool(spec.get("enabled", True))
        self.source_kind = spec.get("source_kind", "xml")
        self._source_path = source_path

    @classmethod
    def from_file(cls, path: str | Path) -> "YamlParser":
        path = Path(path)
        with open(path, encoding="utf-8") as f:
            spec = yaml.safe_load(f)
        if not isinstance(spec, dict):
            raise ValueError(f"{path}: YAML must be a mapping")
        return cls(spec, source_path=path)

    def can_parse(self, raw: bytes, filename: str | None = None) -> bool:
        return _matches_detect(raw, filename, self.spec.get("detect") or {})

    def parse(self, raw: bytes) -> list[dict]:
        kind = self.source_kind.lower()
        if kind == "xml":
            return _parse_xml(raw, self.spec)
        if kind == "json":
            return _parse_json(raw, self.spec)
        if kind == "csv":
            return _parse_csv(raw, self.spec)
        raise ValueError(f"unsupported source_kind: {kind}")


# ── Quick self-test ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Tiny example: parse a CSV with name + price
    test_yaml = """
    name: "Test CSV"
    source_kind: csv
    detect:
      filename_pattern: "*.csv"
    records:
      - table: catalog_product
        fields:
          name:     "Наименование"
          tax_rate: "НДС | nds"
          unit:     "Ед.изм | okei"
      - table: variant_article
        fields:
          article:  "Артикул"
      - table: variant_price
        fields:
          price:    "Цена_розн | rub_to_kop"
    """
    spec = yaml.safe_load(test_yaml)
    spec["delimiter"] = ";"
    p = YamlParser(spec)
    csv_text = (
        "Наименование;Артикул;Цена_розн;НДС;Ед.изм\n"
        "Хлеб;BR-001;125.50;10;шт\n"
        "Молоко;MK-001;89.90;10;л\n"
    )
    records = p.parse(csv_text.encode("utf-8"))
    print(json.dumps(records, ensure_ascii=False, indent=2))
