"""
Deterministic parser for 1C CommerceML 2.05 (import.xml, offers.xml).
Used to bootstrap training examples without manual labelling.

Spec: http://1c.ru/rus/partners/standards.htm

Usage:
    from core.parsers.commerceml import parse_import_xml
    records = parse_import_xml(Path("import.xml"))
    # records → list[{"table": ..., "data": {...}}] matching our schema.sql

This is intentionally narrow (covers the 90% case of standard 1C exports).
The LLM is trained on the OUTPUT of this parser, so it learns to generalise
to non-standard fields, custom extensions, and other 1C-like formats
(Frontol, Saby, Контур.Маркет, Такском, Диадок).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterator


# ── ОКЕИ mapping (Russian unit code → SQL `unit` column) ──────────────────────
# Maps both numeric ОКЕИ codes and common Russian names that 1С emits.
OKEI_BY_NAME = {
    "шт": "796", "штука": "796", "штуки": "796",
    "кг": "166", "килограмм": "166",
    "г":  "163", "грамм":   "163",
    "л":  "112", "литр":    "112",
    "мл": "111", "миллилитр": "111",
    "м":  "006", "метр":    "006",
    "м2": "055", "м²": "055",
    "м3": "113", "м³": "113",
    "уп": "778", "упаковка": "778",
}


def _tax_to_enum(rate: str | None) -> str:
    """Map 1C tax-rate string ("20", "10", "0", "без НДС") to schema enum."""
    if not rate:
        return "NDS_NO_TAX"
    rate = rate.strip().lower()
    if "без" in rate or rate in ("0", "0%"):
        return "NDS_NO_TAX"
    digits = "".join(ch for ch in rate if ch.isdigit())
    return {
        "5":  "NDS_5",
        "7":  "NDS_7",
        "10": "NDS_10",
        "20": "NDS_22",   # 1С пока часто пишет "20" вместо "22"
        "22": "NDS_22",
    }.get(digits, "NDS_NO_TAX")


def _unit_to_okei(elem: ET.Element | None) -> str:
    """Resolve <БазоваяЕдиница Код="796"> → '796'."""
    if elem is None:
        return "796"
    code = elem.get("Код") or elem.get("Code")
    if code and code.isdigit():
        return code.zfill(3)
    text = (elem.text or "").strip().lower()
    return OKEI_BY_NAME.get(text, "796")


def _t(elem: ET.Element | None) -> str | None:
    """Strip-and-return text content, or None."""
    if elem is None or elem.text is None:
        return None
    s = elem.text.strip()
    return s or None


def _localname(tag: str) -> str:
    """Strip namespace from an XML tag."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _find(elem: ET.Element, *names: str) -> ET.Element | None:
    """
    Find first direct child whose local name matches ANY of `names`
    (Russian + English variants). Namespace-agnostic.
    Returns None if not found.

    Note: must NOT use `_find(x, 'A') or _find(x, 'B')` — bool(Element) is
    True only if the element has children, so a leaf <Ид>123</Ид> evaluates
    to False and gets discarded by `or`. Always pass the alternatives here.
    """
    if elem is None:
        return None
    name_set = set(names)
    for child in elem:
        if _localname(child.tag) in name_set:
            return child
    return None


def _findall(elem: ET.Element, name: str) -> list[ET.Element]:
    return [c for c in elem if _localname(c.tag) == name]


def _iter_descendants(elem: ET.Element, name: str) -> Iterator[ET.Element]:
    for sub in elem.iter():
        if _localname(sub.tag) == name:
            yield sub


# ── Top-level: import.xml ────────────────────────────────────────────────────

def parse_import_xml(path: str | Path) -> list[dict]:
    """
    Parse a CommerceML import.xml and return records ready for db_writer.
    Emits rows for: catalog_group, catalog_product, catalog_variant,
    variant_article, variant_barcode, variant_price (if present).
    """
    path = Path(path)
    tree = ET.parse(path)
    root = tree.getroot()

    records: list[dict] = []

    # ── Classifier (groups + properties) ──
    classifier = _find(root, "Классификатор", "Classifier")
    if classifier is not None:
        records.extend(_parse_groups(classifier))

    # ── Catalog (products) ──
    catalog = _find(root, "Каталог", "Catalog")
    if catalog is not None:
        products_node = _find(catalog, "Товары", "Products")
        if products_node is not None:
            for prod_elem in _findall(products_node, "Товар") + _findall(products_node, "Product"):
                records.extend(_parse_product(prod_elem))

    return records


def parse_offers_xml(path: str | Path) -> list[dict]:
    """
    Parse offers.xml — populates variant_price (prices) and stock counters.
    """
    path = Path(path)
    tree = ET.parse(path)
    root = tree.getroot()
    records: list[dict] = []

    offers_pkg = _find(root, "ПакетПредложений", "OffersPackage")
    if offers_pkg is None:
        return records
    offers_node = _find(offers_pkg, "Предложения", "Offers")
    if offers_node is None:
        return records

    for offer in _findall(offers_node, "Предложение") + _findall(offers_node, "Offer"):
        variant_id = _t(_find(offer, "Ид", "Id"))
        if not variant_id:
            continue

        # price
        price_node = _find(offer, "Цены", "Prices")
        if price_node is not None:
            first_price = _find(price_node, "Цена", "Price")
            if first_price is not None:
                price_value = _t(_find(first_price, "ЦенаЗаЕдиницу", "PriceForOne"))
                if price_value:
                    try:
                        kopecks = int(round(float(price_value.replace(",", ".")) * 100))
                        records.append({
                            "table": "variant_price",
                            "data": {"variant_id": variant_id, "price": kopecks},
                        })
                    except ValueError:
                        pass

        # quantity → stock_counters (we don't have warehouse_id here, leave null for LLM mapping)
        qty_node = _find(offer, "Количество", "Quantity")
        if qty_node is not None and _t(qty_node):
            try:
                qty = float(_t(qty_node).replace(",", "."))
                records.append({
                    "table": "stock_counters",
                    "data": {
                        "variant_id": variant_id,
                        "warehouse_id": None,    # filled by LLM/post-processing
                        "quantity": qty,
                        "updated_at": None,
                    },
                })
            except ValueError:
                pass

    return records


# ── Sub-parsers ──────────────────────────────────────────────────────────────

def _parse_groups(classifier: ET.Element, parent_id: str | None = None) -> list[dict]:
    """Recursively walk <Группы>/<Группа> tree."""
    groups_node = _find(classifier, "Группы", "Groups")
    if groups_node is None:
        return []
    return _walk_groups(groups_node, parent_id=None)


def _walk_groups(container: ET.Element, parent_id: str | None) -> list[dict]:
    out: list[dict] = []
    for grp in _findall(container, "Группа") + _findall(container, "Group"):
        gid = _t(_find(grp, "Ид", "Id"))
        name = _t(_find(grp, "Наименование", "Name"))
        if not gid:
            continue
        out.append({
            "table": "catalog_group",
            "data": {
                "id": gid,
                "parent_id": parent_id,
                "name": name or "Без названия",
            },
        })
        sub_groups = _find(grp, "Группы", "Groups")
        if sub_groups is not None:
            out.extend(_walk_groups(sub_groups, parent_id=gid))
    return out


def _parse_product(prod: ET.Element) -> list[dict]:
    out: list[dict] = []
    pid = _t(_find(prod, "Ид", "Id"))
    if not pid:
        return out

    name = _t(_find(prod, "Наименование", "Name")) or "Без названия"
    article = _t(_find(prod, "Артикул", "Article"))
    barcode = _t(_find(prod, "ШтрихКод", "Barcode"))
    unit_elem = _find(prod, "БазоваяЕдиница", "BaseUnit")
    description = _t(_find(prod, "Описание", "Description"))

    # Tax rate
    tax_rate = "NDS_NO_TAX"
    rates_node = _find(prod, "СтавкиНалогов", "TaxRates")
    if rates_node is not None:
        first_rate = _find(rates_node, "СтавкаНалога", "TaxRate")
        if first_rate is not None:
            tax_rate = _tax_to_enum(_t(_find(first_rate, "Ставка", "Rate")))

    # Group reference
    groups_node = _find(prod, "Группы", "Groups")
    group_id = None
    if groups_node is not None:
        group_id = _t(_find(groups_node, "Ид", "Id"))

    # catalog_product
    out.append({
        "table": "catalog_product",
        "data": {
            "id": pid,
            "name": name,
            "type": "default",
            "tax_rate": tax_rate,
            "group_id": group_id,
            "unit": _unit_to_okei(unit_elem),
            "description": description,
        },
    })

    # 1 product → 1 variant (the default SKU). Real variants come from <Характеристики>.
    variant_id = pid  # for simple 1C exports, variant_id == product_id
    out.append({
        "table": "catalog_variant",
        "data": {
            "id": variant_id,
            "product_id": pid,
            "display_name": name,
        },
    })
    if article:
        out.append({
            "table": "variant_article",
            "data": {"variant_id": variant_id, "article": article},
        })
    if barcode:
        out.append({
            "table": "variant_barcode",
            "data": {
                "variant_id": variant_id,
                "barcode": barcode,
                "type": "EAN13" if len(barcode) == 13 else "Code128",
            },
        })

    return out


# ── parse_bytes wrapper (used by plugin) ─────────────────────────────────────

def parse_bytes(raw: bytes) -> list[dict]:
    """Parse a CommerceML XML payload. Tries import.xml structure first."""
    root = ET.fromstring(raw)
    records: list[dict] = []
    # Classifier
    classifier = _find(root, "Классификатор", "Classifier")
    if classifier is not None:
        records.extend(_parse_groups(classifier))
    # Catalog (products)
    catalog = _find(root, "Каталог", "Catalog")
    if catalog is not None:
        products_node = _find(catalog, "Товары", "Products")
        if products_node is not None:
            for prod_elem in _findall(products_node, "Товар") + _findall(products_node, "Product"):
                records.extend(_parse_product(prod_elem))
    return records


# ── Plugin class (registered by registry's discover_builtin) ─────────────────

try:
    from .base import Parser

    class CommerceMLParser(Parser):
        """1С CommerceML 2.05 — обмен 1С ↔ сайт (import.xml / offers.xml)."""
        name = "commerceml"
        priority = 90
        enabled = True

        def can_parse(self, raw: bytes, filename: str | None = None) -> bool:
            head = raw[:400]
            if b"<\xd0\x9a\xd0\xbe\xd0\xbc\xd0\xbc\xd0\xb5\xd1\x80" in head:  # <Коммер
                return True
            if b"<CommercialInformation" in head:
                return True
            if filename and filename.lower() in ("import.xml", "offers.xml"):
                return True
            return False

        def parse(self, raw: bytes) -> list[dict]:
            return parse_bytes(raw)
except ImportError:
    pass


# ── CLI test ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import json

    ap = argparse.ArgumentParser(description="Parse a 1C CommerceML XML file")
    ap.add_argument("path", help="Path to import.xml or offers.xml")
    ap.add_argument("--type", choices=["import", "offers"], default="import")
    args = ap.parse_args()

    if args.type == "import":
        recs = parse_import_xml(args.path)
    else:
        recs = parse_offers_xml(args.path)

    print(json.dumps(recs, ensure_ascii=False, indent=2))
    print(f"\n[OK] {len(recs)} records extracted", file=__import__("sys").stderr)
