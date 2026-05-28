"""
Build (input → output) training pairs from EnterpriseData XML files.

Strategy:
  1. Parse each XML via deterministic EnterpriseData parser → records.
  2. Slice the body into mini-XMLs of CHUNK_SIZE Справочник.Номенклатура each.
  3. For each chunk: build a valid XML wrapper (Message + Header + Body)
     so the model sees realistic structure.
  4. Match records to chunk by product UUID.
  5. Write JSONL of {"input": mini_xml, "output": [records...]}.

Usage:
    python tools/build_ed_training_set.py \
        --xml-dir examples/real_1c \
        --out examples/training_data/ed_real.jsonl \
        --chunk-size 3
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.parsers.enterprise_data import parse_xml as parse_ed, _detect_version, _find, _localname  # noqa: E402


def _serialize_element(elem: ET.Element) -> str:
    """Stringify a single element with its namespaces preserved."""
    return ET.tostring(elem, encoding="unicode")


def _slice_xml(xml_path: Path, chunk_size: int) -> list[tuple[str, list[dict]]]:
    """
    Return list of (mini_xml_str, records) tuples for this file.
    Each mini_xml contains chunk_size Справочник.Номенклатура elements.
    """
    tree = ET.parse(xml_path)
    root = tree.getroot()
    version = _detect_version(root)

    # Get the full Message header — keep it verbatim for realism
    header_elem = None
    for child in root:
        if _localname(child.tag) == "Header":
            header_elem = child
            break
    body = _find(root, "Body")
    if body is None:
        return []

    items = [c for c in body if _localname(c.tag) in ("Справочник.Номенклатура", "Catalog.Nomenclature")]
    if not items:
        return []

    # Parse all records once, then index by product id for fast lookup
    all_records = parse_ed(xml_path)
    records_by_id: dict[str, list[dict]] = {}
    group_records: list[dict] = []
    for rec in all_records:
        if rec["table"] == "catalog_group":
            group_records.append(rec)
            continue
        # Each non-group record carries an id or product_id or variant_id
        pid = (rec.get("data") or {}).get("id") \
            or (rec.get("data") or {}).get("product_id") \
            or (rec.get("data") or {}).get("variant_id")
        if pid:
            records_by_id.setdefault(pid, []).append(rec)

    # Namespaces — we need to declare them on the root of mini-XML so children parse OK
    # Original root: <Message xmlns:msg="..." xmlns:xs="..." xmlns:xsi="...">
    root_attribs = " ".join(f'{k}="{v}"' for k, v in root.attrib.items())
    root_ns_attribs = " ".join(f'xmlns:{p}="{u}"' for p, u in (
        ("msg", "http://www.1c.ru/SSL/Exchange/Message"),
        ("xs",  "http://www.w3.org/2001/XMLSchema"),
        ("xsi", "http://www.w3.org/2001/XMLSchema-instance"),
    ))
    body_xmlns = f"http://v8.1c.ru/edi/edi_stnd/EnterpriseData/{version}"

    chunks: list[tuple[str, list[dict]]] = []
    for i in range(0, len(items), chunk_size):
        slice_items = items[i:i + chunk_size]
        body_inner = "\n        ".join(_serialize_element(it) for it in slice_items)
        # Strip Body namespace from inner elements (ET adds ns prefix when sibling has different ns)
        body_inner_clean = body_inner.replace(f' xmlns="{body_xmlns}"', "")

        # Header verbatim if available
        header_str = ""
        if header_elem is not None:
            header_str = _serialize_element(header_elem)

        mini = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<Message {root_ns_attribs}>\n'
            f'    {header_str}\n'
            f'    <Body xmlns="{body_xmlns}">\n'
            f'        {body_inner_clean}\n'
            f'    </Body>\n'
            f'</Message>'
        )

        # Get UUIDs of products in this slice
        slice_uuids: list[str] = []
        for it in slice_items:
            key = _find(it, "КлючевыеСвойства", "KeyProperties")
            ref = None
            if key is not None:
                ref_elem = _find(key, "Ссылка", "Ref")
                if ref_elem is not None and ref_elem.text:
                    ref = ref_elem.text.strip()
            if ref:
                slice_uuids.append(ref)

        chunk_records: list[dict] = list(group_records)  # all groups (for context)
        for uid in slice_uuids:
            chunk_records.extend(records_by_id.get(uid, []))

        if chunk_records:
            chunks.append((mini, chunk_records))

    return chunks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--xml-dir", required=True, help="folder with EnterpriseData *.xml files")
    ap.add_argument("--out", required=True, help="output JSONL path")
    ap.add_argument("--chunk-size", type=int, default=3, help="products per training example")
    ap.add_argument("--max-examples", type=int, default=0, help="cap total examples (0 = no cap)")
    ap.add_argument("--holdout-out", default=None, help="separate JSONL for holdout (--holdout-frac of data)")
    ap.add_argument("--holdout-frac", type=float, default=0.0, help="fraction to put in holdout")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    random.seed(args.seed)
    xml_dir = Path(args.xml_dir)
    if not xml_dir.is_absolute():
        xml_dir = ROOT / xml_dir
    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = ROOT / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)

    xml_files = sorted(xml_dir.glob("*.xml"))
    print(f"[*] Processing {len(xml_files)} XML files…")

    all_examples: list[dict] = []
    for i, xml_path in enumerate(xml_files):
        try:
            chunks = _slice_xml(xml_path, args.chunk_size)
        except Exception as e:
            print(f"  ! {xml_path.name}: {e}")
            continue
        for mini_xml, records in chunks:
            all_examples.append({"input": mini_xml, "output": records, "_source": xml_path.name})
        print(f"  {xml_path.name}: +{len(chunks)} chunks  (total {len(all_examples)})")

    random.shuffle(all_examples)
    if args.max_examples > 0:
        all_examples = all_examples[:args.max_examples]

    # Holdout split
    train_examples = all_examples
    holdout_examples: list[dict] = []
    if args.holdout_frac > 0 and args.holdout_out:
        n_holdout = int(len(all_examples) * args.holdout_frac)
        holdout_examples = all_examples[:n_holdout]
        train_examples = all_examples[n_holdout:]

    def write(path: Path, items: list[dict]):
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for ex in items:
                clean = {k: v for k, v in ex.items() if not k.startswith("_")}
                f.write(json.dumps(clean, ensure_ascii=False) + "\n")

    write(out_path, train_examples)
    sizes = [len(ex["input"]) for ex in train_examples]
    recs = [len(ex["output"]) for ex in train_examples]
    print(f"\n[OK] {len(train_examples)} training examples → {out_path}")
    if sizes:
        print(f"     XML chars: min={min(sizes)}  avg={sum(sizes)//len(sizes)}  max={max(sizes)}")
        print(f"     Records per ex: min={min(recs)}  avg={sum(recs)//len(recs)}  max={max(recs)}")

    if holdout_examples and args.holdout_out:
        holdout_path = Path(args.holdout_out)
        if not holdout_path.is_absolute():
            holdout_path = ROOT / holdout_path
        write(holdout_path, holdout_examples)
        print(f"[OK] {len(holdout_examples)} holdout examples → {holdout_path}")


if __name__ == "__main__":
    main()
