"""
Deterministic parser for 1C EnterpriseData (1.2 / 1.10 / 1.20) XML.

Output: list of {"table": ..., "data": {...}} matching config/schema.sql.

EnterpriseData is the official XML exchange format used by 1C 8.3:
УТ 11, ERP 2.x, БП 3.0, КА 2.4, УНФ 1.6, Розница 2.3 — all emit this format.

Version differences this parser handles:
  * 1.2  — minimal: <Группа> at the product level (NOT inside <КлючевыеСвойства>),
            <ЕдиницаИзмерения><Код> without ДанныеКлассификатора, fewer attributes.
  * 1.10 — middle: <Группа> inside <КлючевыеСвойства>, <ЕдиницаИзмерения>
            with <ДанныеКлассификатора>, <ВидАлкогольнойПродукции> with Маркируемый flag.
  * 1.20 — newest: same as 1.10 + <ОбщиеСвойстваОбъектовФормата><ДополнительныеРеквизиты>,
            <СтавкаНДС> may be either string ("БезНДС") OR a structured element with <Ставка>.

Covers Справочник.* (Номенклатура, Контрагенты, Организации, Склады, Штрихкоды,
ПозицияПрайсЛиста) and Документ.ПоступлениеТоваровУслуг (приходная накладная →
doc_accept_invoice + doc_position_accept_invoice, плюс встроенные мастер-сущности).
"""

from __future__ import annotations

import hashlib
import re
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

# ── Helpers ──────────────────────────────────────────────────────────────────

def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _find(elem, *names: str):
    """First direct child whose local name is in names. None if not found."""
    if elem is None:
        return None
    s = set(names)
    for child in elem:
        if _localname(child.tag) in s:
            return child
    return None


def _findall(elem, *names: str):
    if elem is None:
        return []
    s = set(names)
    return [c for c in elem if _localname(c.tag) in s]


def _text(elem) -> str | None:
    if elem is None or elem.text is None:
        return None
    s = elem.text.strip()
    return s or None


def _bool(elem) -> int | None:
    """1C XML boolean — value 'true'/'false' (with xsi:nil for empty)."""
    if elem is None:
        return None
    if elem.attrib.get("{http://www.w3.org/2001/XMLSchema-instance}nil") == "true":
        return None
    t = _text(elem)
    if t is None:
        return None
    return 1 if t.lower() == "true" else 0


def _detect_version(root: ET.Element) -> str:
    """Detect EnterpriseData version from Message/Header/Format or Body xmlns."""
    # Search in any namespace
    for elem in root.iter():
        if _localname(elem.tag) == "Format":
            text = (elem.text or "").strip()
            m = re.search(r"EnterpriseData/([\d.]+)", text)
            if m:
                return m.group(1)
    # Fallback: look at Body xmlns
    body = _find(root, "Body")
    if body is not None and "}" in body.tag:
        ns = body.tag.split("}", 1)[0].lstrip("{")
        m = re.search(r"EnterpriseData/([\d.]+)", ns)
        if m:
            return m.group(1)
    return "1.20"  # default to newest


# ── НДС mapping ──────────────────────────────────────────────────────────────

_NDS_MAP = {
    "0":    "NDS_NO_TAX",
    "5":    "NDS_5",
    "7":    "NDS_7",
    "10":   "NDS_10",
    "20":   "NDS_22",     # 1С пока пишет "20" вместо "22" в старых системах
    "22":   "NDS_22",
    "БезНДС":     "NDS_NO_TAX",
    "Без НДС":    "NDS_NO_TAX",
}


def _parse_nds(elem) -> str:
    """СтавкаНДС can be:
       - text "БезНДС" / "20" / "22"
       - element with <Ставка>22</Ставка>
    """
    if elem is None:
        return "NDS_NO_TAX"
    # Direct text (1.2/1.10 style)
    direct = _text(elem)
    if direct:
        return _NDS_MAP.get(direct, "NDS_NO_TAX")
    # Structured <Ставка>22</Ставка> (1.20 style)
    rate = _find(elem, "Ставка", "Rate")
    if rate is not None:
        t = _text(rate)
        return _NDS_MAP.get(t, "NDS_NO_TAX") if t else "NDS_NO_TAX"
    return "NDS_NO_TAX"


# ── ОКЕИ unit code ───────────────────────────────────────────────────────────

def _parse_unit(elem) -> str:
    """<ЕдиницаИзмерения> → ОКЕИ code, e.g. '796'."""
    if elem is None:
        return "796"
    # 1.2 style: <Код>796</Код> directly
    code = _find(elem, "Код", "Code")
    if code is not None and _text(code):
        return _text(code).strip().zfill(3)[:3].lstrip("0") or _text(code).strip()
    # 1.10/1.20 style: <ДанныеКлассификатора><Код>796</Код></ДанныеКлассификатора>
    klass = _find(elem, "ДанныеКлассификатора", "ClassifierData")
    if klass is not None:
        code = _find(klass, "Код", "Code")
        if code is not None and _text(code):
            return _text(code).strip()
    return "796"


# ── Group tree extraction ────────────────────────────────────────────────────

def _walk_groups(root_group: ET.Element, parent_id: str | None, sink: list[dict]) -> str | None:
    """
    Walk a recursive <Группа>...<Группа>...</Группа></Группа> chain
    (innermost is the leaf — that's the product's actual group).
    Adds catalog_group records to sink. Returns the leaf group id.
    """
    if root_group is None:
        return None
    ref = _text(_find(root_group, "Ссылка", "Ref"))
    name = _text(_find(root_group, "Наименование", "Name")) or "Без названия"
    if not ref:
        ref = str(uuid.uuid5(uuid.NAMESPACE_URL, name))
    sink.append({"table": "catalog_group", "data": {"id": ref, "parent_id": parent_id, "name": name}})

    # In EnterpriseData each level holds the NEXT-UP parent as a nested
    # <Группа>. So as we recurse, the *new* element becomes parent of current.
    # Result: innermost element is the most general (root). We want leaf →
    # caller's perspective. So we treat the outer as the leaf and the nested
    # as the parent. That matches the file layout.
    inner = _find(root_group, "Группа", "Group")
    if inner is not None:
        parent_real = _walk_groups(inner, parent_id, sink)
        # Update our own parent_id once parent's been emitted
        for rec in sink:
            if rec["data"].get("id") == ref:
                rec["data"]["parent_id"] = parent_real
                break
    return ref


# ── Main: parse a single Справочник.Номенклатура element ─────────────────────

def _parse_nomenclature(item: ET.Element, version: str) -> list[dict]:
    out: list[dict] = []
    key = _find(item, "КлючевыеСвойства", "KeyProperties")

    pid = _text(_find(key, "Ссылка", "Ref")) if key is not None else None
    if not pid:
        return out

    name = (_text(_find(key, "Наименование", "Name")) if key is not None else None) \
        or _text(_find(item, "Наименование", "Name")) \
        or "Без названия"
    full_name = (_text(_find(key, "НаименованиеПолное", "FullName")) if key is not None else None)
    article = (_text(_find(key, "Артикул", "Article")) if key is not None else None) \
        or _text(_find(item, "Артикул", "Article"))
    program_code = (_text(_find(key, "КодВПрограмме", "ProgramCode")) if key is not None else None)

    # Group: either inside KeyProperties (1.10, 1.20) or at item level (1.2)
    group_elem = (_find(key, "Группа", "Group") if key is not None else None) \
        or _find(item, "Группа", "Group")
    group_id = None
    if group_elem is not None:
        group_id = _walk_groups(group_elem, None, out)
    if not group_id:
        # Item has no group — attach to a default "Без категории" group.
        # schema.sql requires catalog_product.group_id NOT NULL.
        group_id = "00000000-0000-0000-0000-000000000000"
        out.append({
            "table": "catalog_group",
            "data": {"id": group_id, "parent_id": None, "name": "Без категории"},
        })

    # Type
    nom_type = _text(_find(item, "ТипНоменклатуры", "NomenclatureType")) or "Товар"
    if nom_type == "Услуга":
        product_type = "service"
    elif nom_type == "Работа":
        product_type = "service"
    else:
        product_type = "default"

    # Unit + tax
    unit = _parse_unit(_find(item, "ЕдиницаИзмерения", "UnitOfMeasure"))
    tax_rate = _parse_nds(_find(item, "СтавкаНДС", "VATRate"))

    # Flags (1.10, 1.20)
    excise = _bool(_find(item, "ПодакцизныйТовар", "Excise"))
    weighable = _bool(_find(item, "Весовой", "Weighable"))
    marked_flag = _bool(_find(item, "Маркируемый", "Markable"))
    alc_data = _find(item, "ДанныеАлкогольнойПродукции", "AlcoholProductData")
    is_alc = _bool(_find(alc_data, "АлкогольнаяПродукция", "AlcoholProduct")) if alc_data is not None else 0

    # Description: full_name typically duplicates name; if it adds info, store
    description = full_name if (full_name and full_name != name) else None

    # catalog_product
    out.append({
        "table": "catalog_product",
        "data": {
            "id": pid,
            "name": name,
            "type": product_type,
            "tax_rate": tax_rate,
            "group_id": group_id,
            "unit": unit,
            "description": description,
            "excise": excise or 0,
            "marked": marked_flag,
            "alcohol": is_alc,
        },
    })

    # catalog_variant — 1 default SKU per product
    variant_id = pid  # we don't have separate variant UUID in this format
    out.append({
        "table": "catalog_variant",
        "data": {"id": variant_id, "product_id": pid, "display_name": name},
    })

    # variant_article — prefer КодВПрограмме if no Артикул
    art = article or program_code
    if art:
        out.append({"table": "variant_article", "data": {"variant_id": variant_id, "article": art}})

    # variant_alcohol_meta if alcoholic
    if is_alc and alc_data is not None:
        vap = _find(alc_data, "ВидАлкогольнойПродукции", "AlcoholProductKind")
        alc_type_code = _text(_find(vap, "Код", "Code")) if vap is not None else None
        alc_volume_raw = _text(_find(alc_data, "Крепость", "Strength"))
        try:
            alc_volume = float(alc_volume_raw.replace(",", ".")) if alc_volume_raw else None
        except (ValueError, AttributeError):
            alc_volume = None
        out.append({
            "table": "variant_alcohol_meta",
            "data": {
                "variant_id": variant_id,
                "alc_type_code": alc_type_code,
                "alc_volume": alc_volume,
                "alc_unitType": "PACKED",  # default
            },
        })

    # variant_marked_meta if explicitly marked
    if marked_flag:
        # Try to derive mark_group from "ВидПродукцииИС"
        prod_kind = _text(_find(item, "ВидПродукцииИС", "ISProductKind")) or ""
        mark_group = None
        if "Алкоголь" in prod_kind:
            mark_group = "alcohol"
        elif "Табак" in prod_kind:
            mark_group = "tobacco"
        elif "Молоч" in prod_kind:
            mark_group = "milk"
        if mark_group:
            out.append({
                "table": "variant_marked_meta",
                "data": {"variant_id": variant_id, "group": mark_group},
            })

    return out


# ── Справочник.Контрагенты ───────────────────────────────────────────────────

def _parse_counterparty(item: ET.Element) -> list[dict]:
    out: list[dict] = []
    key = _find(item, "КлючевыеСвойства", "KeyProperties")
    cid = _text(_find(key, "Ссылка", "Ref")) if key is not None else None
    if not cid:
        return out

    name = (_text(_find(key, "Наименование", "Name")) if key is not None else None) \
        or _text(_find(item, "Наименование", "Name")) or "Без названия"
    full_name = (_text(_find(key, "НаименованиеПолное", "FullName")) if key is not None else None) or name
    inn = (_text(_find(key, "ИНН", "INN")) if key is not None else None) \
        or _text(_find(item, "ИНН", "INN"))
    kpp = (_text(_find(key, "КПП", "KPP")) if key is not None else None) \
        or _text(_find(item, "КПП", "KPP"))
    legal_address = _text(_find(item, "ЮридическийАдрес", "LegalAddress"))
    yur_fiz = _text(_find(item, "ЮридическоеФизическоеЛицо", "LegalOrIndividual"))

    cp_type = "legal_entity"
    if yur_fiz:
        if "Физическое" in yur_fiz:
            cp_type = "individual"
        elif "Индивидуальный" in yur_fiz or "ИП" in yur_fiz:
            cp_type = "individual_entrepreneur"

    out.append({
        "table": "counterparty",
        "data": {
            "id": cid,
            "created_at": _now_iso(),
            "name": name,
            "full_name": full_name,
            "type": cp_type,
            "inn": inn or "",
            "kpp": kpp,
            "legal_address": legal_address,
            "is_supplier": 1,
            "is_customer": 0,
            "fsrar_id": "",  # EnterpriseData rarely carries fsrar — fill empty
        },
    })
    return out


# ── Справочник.Организации ───────────────────────────────────────────────────

def _parse_organization(item: ET.Element) -> list[dict]:
    out: list[dict] = []
    key = _find(item, "КлючевыеСвойства", "KeyProperties")
    oid = _text(_find(key, "Ссылка", "Ref")) if key is not None else None
    if not oid:
        return out
    name = (_text(_find(key, "Наименование", "Name")) if key is not None else None) or "Без названия"
    inn = _text(_find(item, "ИНН", "INN")) or _text(_find(key, "ИНН", "INN"))
    kpp = _text(_find(item, "КПП", "KPP")) or _text(_find(key, "КПП", "KPP"))
    out.append({
        "table": "legal_entity",
        "data": {
            "id": oid,
            "name": name,
            "inn": inn or "",
            "kpp": kpp or "",
            "address": _text(_find(item, "ЮридическийАдрес", "LegalAddress")) or "",
            "fsrar_id": _text(_find(item, "ИдентификаторФСРАР", "FSRARID")) or "",
        },
    })
    return out


# ── Справочник.Склады ────────────────────────────────────────────────────────

def _parse_warehouse(item: ET.Element) -> list[dict]:
    out: list[dict] = []
    key = _find(item, "КлючевыеСвойства", "KeyProperties")
    wid = _text(_find(key, "Ссылка", "Ref")) if key is not None else None
    if not wid:
        return out
    name = (_text(_find(key, "Наименование", "Name")) if key is not None else None) or "Склад"
    out.append({
        "table": "warehouse",
        "data": {"id": wid, "name": name, "shop_id": ""},  # shop_id filled by post-processing
    })
    return out


# ── Справочник.ШтрихкодыНоменклатуры ─────────────────────────────────────────

def _parse_barcode_ref(item: ET.Element) -> list[dict]:
    out: list[dict] = []
    # Format usually: variant ref + barcode value(s)
    variant_ref = _text(_find(item, "Номенклатура", "Nomenclature")) \
        or _text(_find(item, "ВладелецЗначений", "ValueOwner"))
    if not variant_ref:
        # Try a nested KlючевыеСвойства lookup
        key = _find(item, "КлючевыеСвойства", "KeyProperties")
        if key is not None:
            variant_ref = _text(_find(key, "Номенклатура", "Nomenclature"))
    if not variant_ref:
        return out
    bc = _text(_find(item, "Штрихкод", "Barcode")) or _text(_find(item, "Значение", "Value"))
    if bc:
        out.append({
            "table": "variant_barcode",
            "data": {
                "variant_id": variant_ref,
                "barcode": bc,
                "type": "EAN13" if len(bc) == 13 else "Code128",
            },
        })
    return out


# ── Справочник.ПозицияПрайсЛиста (price entries) ─────────────────────────────

def _parse_price_item(item: ET.Element) -> list[dict]:
    out: list[dict] = []
    variant_ref = _text(_find(item, "Номенклатура", "Nomenclature"))
    if not variant_ref:
        return out
    price_text = _text(_find(item, "Цена", "Price")) \
        or _text(_find(item, "ЦенаЗаЕдиницу", "PriceForOne"))
    if not price_text:
        return out
    try:
        rub = float(price_text.replace(",", "."))
        out.append({
            "table": "variant_price",
            "data": {"variant_id": variant_ref, "price": int(round(rub * 100))},
        })
    except ValueError:
        pass
    return out


# ── Документы: helpers ───────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _num(text: str | None):
    """Parse a 1C numeric ('15', '1,5', '12.0') → int if whole, else float."""
    if not text:
        return 0
    s = str(text).replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        f = float(s)
        return int(f) if f == int(f) else f
    except ValueError:
        return 0


def _money_to_kop(text: str | None) -> int:
    """Money in rubles ('100', '126.50', '1 890,00') → integer kopecks."""
    if not text:
        return 0
    s = str(text).replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        return int(round(float(s) * 100))
    except ValueError:
        return 0


def _embedded_nomenclature(nom_elem, line_unit: str, line_tax: str,
                           out: list[dict], seen_products: set) -> str | None:
    """
    Номенклатура embedded inside a document line has its fields as DIRECT
    children (Ссылка/Наименование/КодВПрограмме/Группа) — no <КлючевыеСвойства>.
    Emit catalog_group + catalog_product + catalog_variant (+ article) once per id.
    Returns the variant_id (== product ref) for the line to reference.
    """
    if nom_elem is None:
        return None
    ref = _text(_find(nom_elem, "Ссылка", "Ref"))
    if not ref:
        return None
    if ref in seen_products:
        return ref
    seen_products.add(ref)

    name = _text(_find(nom_elem, "Наименование", "Name")) \
        or _text(_find(nom_elem, "НаименованиеПолное", "FullName")) \
        or "Без названия"
    full = _text(_find(nom_elem, "НаименованиеПолное", "FullName"))
    code = _text(_find(nom_elem, "КодВПрограмме", "ProgramCode")) \
        or _text(_find(nom_elem, "Артикул", "Article"))

    group_elem = _find(nom_elem, "Группа", "Group")
    group_id = _walk_groups(group_elem, None, out) if group_elem is not None else None
    if not group_id:
        group_id = "00000000-0000-0000-0000-000000000000"
        out.append({"table": "catalog_group",
                    "data": {"id": group_id, "parent_id": None, "name": "Без категории"}})

    out.append({"table": "catalog_product", "data": {
        "id": ref, "name": name, "type": "default", "tax_rate": line_tax,
        "group_id": group_id, "unit": line_unit,
        "description": full if (full and full != name) else None,
    }})
    out.append({"table": "catalog_variant",
                "data": {"id": ref, "product_id": ref, "display_name": name}})
    if code:
        out.append({"table": "variant_article", "data": {"variant_id": ref, "article": code}})
    return ref


# ── Документ.ПоступлениеТоваровУслуг (приходная накладная) ────────────────────

def _parse_goods_receipt(doc: ET.Element, version: str) -> list[dict]:
    out: list[dict] = []
    key = _find(doc, "КлючевыеСвойства", "KeyProperties")

    doc_id = (_text(_find(key, "Ссылка", "Ref")) if key is not None else None) \
        or _text(_find(doc, "Ссылка", "Ref"))
    if not doc_id:
        return out
    date = (_text(_find(key, "Дата", "Date")) if key is not None else None) or _now_iso()
    number = (_text(_find(key, "Номер", "Number")) if key is not None else None) or "б/н"

    # Организация → legal_entity
    org = _find(key, "Организация", "Organization") if key is not None else None
    org_id = _text(_find(org, "Ссылка", "Ref")) if org is not None else None
    if org_id:
        out.append({"table": "legal_entity", "data": {
            "id": org_id,
            "name": _text(_find(org, "Наименование", "Name")) or "Организация",
            "inn": _text(_find(org, "ИНН", "INN")) or "",
            "kpp": _text(_find(org, "КПП", "KPP")) or "",
            "address": "", "fsrar_id": "",
        }})

    # Контрагент → counterparty (поставщик)
    cp = _find(key, "Контрагент", "Counterparty") if key is not None else None
    cp_id = _text(_find(cp, "Ссылка", "Ref")) if cp is not None else None
    if cp_id:
        yf = _text(_find(cp, "ЮридическоеФизическоеЛицо", "LegalOrIndividual")) or ""
        cp_type = "individual" if "Физическое" in yf else "legal_entity"
        cp_name = _text(_find(cp, "Наименование", "Name")) or "Поставщик"
        out.append({"table": "counterparty", "data": {
            "id": cp_id, "created_at": date, "name": cp_name, "full_name": cp_name,
            "type": cp_type, "inn": _text(_find(cp, "ИНН", "INN")) or "",
            "kpp": _text(_find(cp, "КПП", "KPP")),
            "is_supplier": 1, "is_customer": 0, "fsrar_id": "",
        }})

    # Склад → warehouse (+ synthetic shop to satisfy NOT NULL store_id)
    wh = _find(doc, "Склад", "Warehouse")
    wh_id = _text(_find(wh, "Ссылка", "Ref")) if wh is not None else None
    wh_name = (_text(_find(wh, "Наименование", "Name")) if wh is not None else None) or "Склад"
    shop_id = None
    if org_id:
        shop_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "shop:" + org_id))
        out.append({"table": "shop", "data": {
            "id": shop_id, "name": wh_name, "address": "", "legal_entity_id": org_id,
        }})
    if wh_id:
        out.append({"table": "warehouse",
                    "data": {"id": wh_id, "name": wh_name, "shop_id": shop_id or ""}})

    # Строки товаров → doc_position_accept_invoice (+ embedded nomenclature)
    goods = _find(doc, "Товары", "Goods", "Products")
    lines = _findall(goods, "Строка", "Line", "Row") if goods is not None else []
    seen_products: set = set()
    total_kop, pos_count = 0, 0
    for i, line in enumerate(lines, start=1):
        nom_data = _find(line, "ДанныеНоменклатуры", "NomenclatureData")
        nom = _find(nom_data, "Номенклатура", "Nomenclature") if nom_data is not None \
            else _find(line, "Номенклатура", "Nomenclature")
        unit = _parse_unit(_find(line, "ЕдиницаИзмерения", "UnitOfMeasure"))
        tax = _parse_nds(_find(line, "СтавкаНДС", "VATRate"))
        variant_id = _embedded_nomenclature(nom, unit, tax, out, seen_products)
        if not variant_id:
            continue
        qty = _num(_text(_find(line, "Количество", "Quantity")))
        price_kop = _money_to_kop(_text(_find(line, "Цена", "Price")))
        sum_kop = _money_to_kop(_text(_find(line, "Сумма", "Sum", "Amount")))
        if not sum_kop and price_kop and qty:
            sum_kop = int(round(price_kop * qty))
        total_kop += sum_kop
        pos_count += 1
        # deterministic id → re-ingesting the same file is idempotent (OR IGNORE)
        pos_id = int(hashlib.sha1(f"{doc_id}|{i}".encode()).hexdigest()[:15], 16)
        out.append({"table": "doc_position_accept_invoice", "data": {
            "id": pos_id, "document_id": doc_id, "variant_id": variant_id,
            "name": _text(_find(nom, "Наименование", "Name")) if nom is not None else None,
            "quantity": qty, "unit_cost_with_tax": price_kop,
            "total_cost_with_tax": sum_kop, "tax": tax, "measure": unit,
            "article": _text(_find(nom, "КодВПрограмме", "ProgramCode")) if nom is not None else None,
        }})

    # Header total — prefer the document-level <Сумма>
    doc_sum = _money_to_kop(_text(_find(doc, "Сумма", "Sum", "Amount")))
    if doc_sum:
        total_kop = doc_sum

    # Incoming document reference → comment
    incoming = _find(key, "ДанныеВходящегоДокумента", "IncomingDocumentData") if key is not None else None
    comment = None
    if incoming is not None:
        in_num = _text(_find(incoming, "НомерВходящегоДокумента", "IncomingNumber"))
        in_date = _text(_find(incoming, "ДатаВходящегоДокумента", "IncomingDate"))
        if in_num or in_date:
            comment = f"Вх. №{in_num or '?'} от {in_date or '?'}"

    out.append({"table": "doc_accept_invoice", "data": {
        "id": doc_id, "num": number, "created_at": date, "accepted_at": date,
        "warehouse_id": wh_id or "", "store_id": shop_id or "",
        "legal_entity_id": org_id or "", "contractor_id": cp_id or "",
        "source": "EDO", "status": "ACCEPTED",
        "positions_count": pos_count, "total_cost_with_tax": total_kop,
        "comment": comment, "move_stock": 0,
    }})
    return out


# ── Top-level entity dispatcher ──────────────────────────────────────────────

_ENTITY_HANDLERS: dict[str, callable] = {
    "Справочник.Номенклатура":             lambda el, v: _parse_nomenclature(el, v),
    "Catalog.Nomenclature":                lambda el, v: _parse_nomenclature(el, v),
    "Справочник.Контрагенты":              lambda el, v: _parse_counterparty(el),
    "Catalog.Counterparties":              lambda el, v: _parse_counterparty(el),
    "Справочник.Организации":              lambda el, v: _parse_organization(el),
    "Catalog.Organizations":               lambda el, v: _parse_organization(el),
    "Справочник.Склады":                   lambda el, v: _parse_warehouse(el),
    "Catalog.Warehouses":                  lambda el, v: _parse_warehouse(el),
    "Справочник.ШтрихкодыНоменклатуры":    lambda el, v: _parse_barcode_ref(el),
    "Справочник.ПозицияПрайсЛиста":        lambda el, v: _parse_price_item(el),
    # Документы
    "Документ.ПоступлениеТоваровУслуг":    lambda el, v: _parse_goods_receipt(el, v),
    "Документ.ПриобретениеТоваровУслуг":   lambda el, v: _parse_goods_receipt(el, v),
    "Document.GoodsReceipt":               lambda el, v: _parse_goods_receipt(el, v),
}


# ── Public entry point ───────────────────────────────────────────────────────

def parse_xml(path: str | Path) -> list[dict]:
    """Parse an EnterpriseData XML file path and return DB-ready records."""
    path = Path(path)
    return parse_bytes(path.read_bytes())


def parse_bytes(raw: bytes) -> list[dict]:
    """Parse EnterpriseData XML payload (versions 1.2 / 1.10 / 1.20)."""
    root = ET.fromstring(raw)
    version = _detect_version(root)

    body = _find(root, "Body")
    if body is None:
        return []

    records: list[dict] = []
    for child in body:
        local = _localname(child.tag)
        handler = _ENTITY_HANDLERS.get(local)
        if handler is not None:
            try:
                records.extend(handler(child, version))
            except Exception:
                # Skip malformed entries — better partial than fail whole file
                continue

    # Dedup catalog_group, counterparty, legal_entity, warehouse (shared across items)
    deduped: list[dict] = []
    seen: dict[str, set[str]] = {}
    for rec in records:
        table = rec["table"]
        if table in ("catalog_group", "counterparty", "legal_entity", "warehouse", "shop"):
            ident = rec["data"].get("id")
            if not ident:
                deduped.append(rec)
                continue
            seen_in_table = seen.setdefault(table, set())
            if ident in seen_in_table:
                continue
            seen_in_table.add(ident)
        deduped.append(rec)
    return deduped


# ── Plugin class (registered by registry's discover_builtin) ─────────────────

try:
    from .base import Parser

    class EnterpriseDataParser(Parser):
        """1C EnterpriseData XML — versions 1.2 / 1.10 / 1.20."""
        name = "enterprise_data"
        priority = 95
        enabled = True

        def can_parse(self, raw: bytes, filename: str | None = None) -> bool:
            head = raw[:600]
            if b"EnterpriseData" in head:
                return True
            if b"<Message" in head and b"v8.1c.ru/edi" in head:
                return True
            return False

        def parse(self, raw: bytes) -> list[dict]:
            return parse_bytes(raw)
except ImportError:
    pass  # base.py not present — standalone CLI mode


# ── CLI test ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import json
    import sys

    ap = argparse.ArgumentParser()
    ap.add_argument("path", help="EnterpriseData XML file")
    ap.add_argument("--stats", action="store_true", help="print summary instead of full JSON")
    args = ap.parse_args()

    recs = parse_xml(args.path)
    if args.stats:
        from collections import Counter
        counts = Counter(r["table"] for r in recs)
        print(f"{args.path}: {len(recs)} records")
        for table, n in counts.most_common():
            print(f"  {table:35s} {n}")
    else:
        print(json.dumps(recs, ensure_ascii=False, indent=2))
        print(f"\n[OK] {len(recs)} records", file=sys.stderr)
