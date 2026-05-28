"""
Build training pairs (input XML chunk → output records) from synthetic
or real CommerceML XML files.

Strategy:
  1. Parse each XML via deterministic parser → list of records.
  2. Group records by product (catalog_product + variant + barcode + article…).
  3. Slice the XML into mini-XMLs of CHUNK_SIZE products each.
  4. Each mini-XML + its records → one training example.

The mini-XML keeps the <КоммерческаяИнформация>, <Классификатор> header
and <Каталог> wrapping so the model sees realistic context, but is small
enough to fit in 2048-token training window.

Usage:
    python tools/build_training_set.py \
        --xml-dir examples/synthetic_1c \
        --out examples/training_data/synthetic_1c.jsonl \
        --chunk-size 3
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.parsers.commerceml import parse_import_xml, _find, _findall, _localname  # noqa: E402


def _slice_products(xml_path: Path, chunk_size: int) -> list[tuple[str, list[dict]]]:
    """
    Slice a CommerceML XML by group-of-N products.
    Returns list of (mini_xml_str, records_for_that_chunk).
    """
    tree = ET.parse(xml_path)
    root = tree.getroot()

    # Header: <КоммерческаяИнформация ...> + <Классификатор>
    # We keep the original Classifier (groups + properties) verbatim so the
    # mini-XML retains group references.
    catalog = _find(root, "Каталог", "Catalog")
    if catalog is None:
        return []
    products_node = _find(catalog, "Товары", "Products")
    if products_node is None:
        return []
    all_products = _findall(products_node, "Товар") + _findall(products_node, "Product")
    if not all_products:
        return []

    classifier = _find(root, "Классификатор", "Classifier")
    classifier_xml = ET.tostring(classifier, encoding="unicode") if classifier is not None else ""
    is_en = _localname(root.tag) == "CommercialInformation"
    ROOT_TAG = "CommercialInformation" if is_en else "КоммерческаяИнформация"
    CAT_TAG = "Catalog" if is_en else "Каталог"
    PRODS_TAG = "Products" if is_en else "Товары"
    ID_TAG = "Id" if is_en else "Ид"
    NAME_TAG = "Name" if is_en else "Наименование"

    catalog_id = _find(catalog, ID_TAG)
    catalog_id_xml = ET.tostring(catalog_id, encoding="unicode") if catalog_id is not None else ""
    catalog_name = _find(catalog, NAME_TAG)
    catalog_name_xml = ET.tostring(catalog_name, encoding="unicode") if catalog_name is not None else ""

    # Parse full file once to get all records, then index by product_id
    all_records = parse_import_xml(xml_path)
    records_by_product: dict[str, list[dict]] = {}
    group_records: list[dict] = []
    for rec in all_records:
        if rec["table"] == "catalog_group":
            group_records.append(rec)
            continue
        # Records tied to a specific product/variant share the same UUID
        pid = (rec.get("data") or {}).get("id") or (rec.get("data") or {}).get("product_id") or (rec.get("data") or {}).get("variant_id")
        if pid:
            records_by_product.setdefault(pid, []).append(rec)

    chunks: list[tuple[str, list[dict]]] = []
    for i in range(0, len(all_products), chunk_size):
        slice_ = all_products[i:i + chunk_size]
        # Build mini-XML
        body_parts = ['<?xml version="1.0" encoding="UTF-8"?>']
        body_parts.append(f'<{ROOT_TAG} ВерсияСхемы="2.05">')
        if classifier_xml:
            body_parts.append("  " + classifier_xml)
        body_parts.append(f'  <{CAT_TAG} СодержитТолькоИзменения="false">')
        if catalog_id_xml:
            body_parts.append("    " + catalog_id_xml)
        if catalog_name_xml:
            body_parts.append("    " + catalog_name_xml)
        body_parts.append(f"    <{PRODS_TAG}>")
        slice_ids = []
        for prod_elem in slice_:
            body_parts.append("      " + ET.tostring(prod_elem, encoding="unicode"))
            pid_elem = _find(prod_elem, ID_TAG)
            if pid_elem is not None and pid_elem.text:
                slice_ids.append(pid_elem.text.strip())
        body_parts.append(f"    </{PRODS_TAG}>")
        body_parts.append(f"  </{CAT_TAG}>")
        body_parts.append(f"</{ROOT_TAG}>")
        mini_xml = "\n".join(body_parts)

        # Collect records for this chunk
        chunk_records = list(group_records)  # all groups in every chunk (good for learning)
        for pid in slice_ids:
            chunk_records.extend(records_by_product.get(pid, []))

        if chunk_records:
            chunks.append((mini_xml, chunk_records))

    return chunks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--xml-dir", required=True, help="folder with .xml files")
    ap.add_argument("--out", required=True, help="output JSONL path")
    ap.add_argument("--chunk-size", type=int, default=3, help="products per training example")
    ap.add_argument("--max-examples", type=int, default=10000, help="cap total examples")
    ap.add_argument("--shuffle", action="store_true", default=True)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    xml_dir = Path(args.xml_dir)
    if not xml_dir.is_absolute():
        xml_dir = ROOT / xml_dir
    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = ROOT / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)

    random.seed(args.seed)
    xml_files = sorted(xml_dir.glob("*.xml"))
    print(f"[*] Processing {len(xml_files)} XML files…")

    all_examples: list[dict] = []
    for i, xml_path in enumerate(xml_files):
        try:
            chunks = _slice_products(xml_path, args.chunk_size)
        except Exception as e:
            print(f"  ! {xml_path.name}: {e}")
            continue
        for xml_chunk, records in chunks:
            all_examples.append({"input": xml_chunk, "output": records})
        if (i + 1) % 50 == 0:
            print(f"  {i + 1}/{len(xml_files)} files, {len(all_examples)} examples so far")

    if args.shuffle:
        random.shuffle(all_examples)
    if args.max_examples > 0:
        all_examples = all_examples[:args.max_examples]

    with open(out_path, "w", encoding="utf-8") as f:
        for ex in all_examples:
            f.write(json.dumps(ex, ensure_ascii=False) + "\n")

    # Stats
    sizes = [len(ex["input"]) for ex in all_examples]
    rec_counts = [len(ex["output"]) for ex in all_examples]
    print(f"\n[OK] {len(all_examples)} training examples → {out_path}")
    print(f"     XML chunk size: min={min(sizes)}  avg={sum(sizes)//len(sizes)}  max={max(sizes)}  chars")
    print(f"     Records per ex: min={min(rec_counts)}  avg={sum(rec_counts)//len(rec_counts)}  max={max(rec_counts)}")


if __name__ == "__main__":
    main()
