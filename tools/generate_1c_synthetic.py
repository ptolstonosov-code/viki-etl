"""
Synthetic 1C CommerceML 2.05 generator.

Produces realistic import.xml files mimicking outputs of various 1C
configurations (УТ, УНФ, Розница, ERP). Used to bootstrap a large training
set for the parser model.

Usage:
    python tools/generate_1c_synthetic.py --count 300 --out examples/synthetic_1c/
    python tools/generate_1c_synthetic.py --count 50  --variety extreme

Each XML is paired with the deterministic parser output (core.parsers.commerceml)
to make a (input → records) training example. That conversion is done by
`tools/build_training_set.py`, not this script.
"""

from __future__ import annotations

import argparse
import json
import random
import string
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).parent.parent

# ── Catalog of realistic Russian retail products ─────────────────────────────

PRODUCT_TEMPLATES = {
    "Хлебобулочные изделия": [
        ("Хлеб Бородинский {weight}", [("weight", ["400г", "300г", "500г"])], "kg", 10),
        ("Батон Нарезной {weight}", [("weight", ["400г", "500г"])], "kg", 10),
        ("Хлеб Ржаной {weight}", [("weight", ["700г", "500г"])], "kg", 10),
        ("Булочка с маком", [], "pcs", 10),
        ("Багет французский", [], "pcs", 10),
        ("Лаваш армянский тонкий", [], "pcs", 10),
        ("Сухари Ванильные {weight}", [("weight", ["200г", "300г"])], "kg", 10),
    ],
    "Молочная продукция": [
        ("Молоко {fat} {volume}", [("fat", ["1.5%", "2.5%", "3.2%", "4%"]), ("volume", ["1л", "0.5л", "0.9л"])], "l", 5),
        ("Кефир {fat} {volume}", [("fat", ["1%", "2.5%", "3.2%"]), ("volume", ["1л", "0.5л"])], "l", 5),
        ("Творог {fat} {weight}", [("fat", ["0%", "5%", "9%", "18%"]), ("weight", ["180г", "200г", "250г"])], "kg", 10),
        ("Йогурт питьевой {flavor} {volume}", [("flavor", ["клубника", "вишня", "малина"]), ("volume", ["330г", "200г"])], "kg", 10),
        ("Сыр Российский {weight}", [("weight", ["200г", "250г"])], "kg", 10),
        ("Сметана {fat} {weight}", [("fat", ["15%", "20%", "25%"]), ("weight", ["200г", "400г"])], "kg", 10),
        ("Сливочное масло {weight}", [("weight", ["180г", "200г", "250г"])], "kg", 10),
    ],
    "Бакалея": [
        ("Сахар-песок {weight}", [("weight", ["1кг", "5кг"])], "kg", 20),
        ("Мука пшеничная {weight}", [("weight", ["2кг", "5кг", "10кг"])], "kg", 10),
        ("Соль поваренная {weight}", [("weight", ["1кг", "500г"])], "kg", 10),
        ("Гречка ядрица {weight}", [("weight", ["800г", "900г"])], "kg", 10),
        ("Рис круглозерный {weight}", [("weight", ["800г", "1кг"])], "kg", 10),
        ("Макароны {type} {weight}", [("type", ["спагетти", "перья", "рожки"]), ("weight", ["400г", "500г"])], "kg", 10),
        ("Масло подсолнечное {volume}", [("volume", ["1л", "0.9л", "5л"])], "l", 10),
    ],
    "Кондитерские изделия": [
        ("Шоколад {brand} {weight}", [("brand", ["Алёнка", "Бабаевский", "Россия", "Мишка косолапый"]), ("weight", ["100г", "90г", "200г"])], "kg", 20),
        ("Конфеты {brand} {weight}", [("brand", ["Мишка косолапый", "Красная шапочка", "Птичье молоко"]), ("weight", ["200г", "300г", "500г"])], "kg", 20),
        ("Печенье {brand} {weight}", [("brand", ["Юбилейное", "Топлёное молоко", "Курабье"]), ("weight", ["313г", "200г"])], "kg", 20),
        ("Зефир {flavor} {weight}", [("flavor", ["ванильный", "шоколадный"]), ("weight", ["250г", "300г"])], "kg", 20),
    ],
    "Безалкогольные напитки": [
        ("Вода питьевая {brand} {volume}", [("brand", ["Аква Минерале", "БонАква", "Архыз"]), ("volume", ["1.5л", "0.5л", "5л"])], "l", 0),
        ("Сок {flavor} {volume}", [("flavor", ["яблочный", "апельсиновый", "мультифрукт"]), ("volume", ["1л", "0.95л", "2л"])], "l", 10),
        ("Кола {brand} {volume}", [("brand", ["Coca-Cola", "Pepsi"]), ("volume", ["0.5л", "1.5л", "2л"])], "l", 10),
        ("Чай {brand} пакетированный", [("brand", ["Greenfield", "Lipton", "Akbar"])], "pcs", 20),
        ("Кофе {brand} растворимый {weight}", [("brand", ["Jacobs", "Nescafe", "Tchibo"]), ("weight", ["95г", "190г"])], "kg", 20),
    ],
    "Алкогольная продукция": [
        ("Водка {brand} {volume}", [("brand", ["Столичная", "Беленькая", "Талка", "Парламент"]), ("volume", ["0.5л", "0.7л", "1л"])], "l", 20, True, 40.0),
        ("Вино {color} {brand} {volume}", [("color", ["красное", "белое"]), ("brand", ["Каберне", "Шардоне", "Мерло"]), ("volume", ["0.75л", "1л"])], "l", 20, True, 12.0),
        ("Пиво {brand} {volume}", [("brand", ["Балтика №7", "Жигулёвское", "Очаково"]), ("volume", ["0.5л", "0.45л", "1.35л"])], "l", 20, True, 5.0),
        ("Коньяк {brand} {volume}", [("brand", ["Старый Кёнигсберг", "Лезгинка", "Арарат"]), ("volume", ["0.5л", "0.25л"])], "l", 20, True, 40.0),
    ],
    "Табачные изделия": [
        ("Сигареты {brand}", [("brand", ["Marlboro", "Winston", "Парламент", "Bond"])], "pcs", 0, False, None, "tobacco"),
    ],
    "Бытовая химия": [
        ("Стиральный порошок {brand} {weight}", [("brand", ["Tide", "Ariel", "Persil"]), ("weight", ["3кг", "6кг", "9кг"])], "kg", 20),
        ("Жидкость для мытья посуды {brand} {volume}", [("brand", ["Fairy", "Sorti", "AOS"]), ("volume", ["500мл", "900мл", "1.4л"])], "l", 20),
        ("Шампунь {brand} {volume}", [("brand", ["Head & Shoulders", "Pantene", "Чистая линия"]), ("volume", ["400мл", "250мл"])], "l", 20),
    ],
}

OKEI_BY_NAME = {"pcs": "796", "kg": "166", "g": "163", "l": "112", "ml": "111", "m": "006"}

# Maps category → ЧЗ группа (если применимо)
MARK_GROUPS = {
    "Алкогольная продукция": "alcohol",
    "Табачные изделия": "tobacco",
    "Молочная продукция": "milk",
}


def random_inn(legal_entity: bool = True) -> str:
    """Generate a syntactically valid INN."""
    length = 10 if legal_entity else 12
    return "".join(random.choices(string.digits, k=length))


def random_ean13() -> str:
    """Generate EAN-13 with valid checksum."""
    digits = [random.randint(0, 9) for _ in range(12)]
    # Checksum: sum(odd*1) + sum(even*3) = N → check = (10 - N%10) % 10
    total = sum(d * (3 if i % 2 else 1) for i, d in enumerate(digits))
    check = (10 - total % 10) % 10
    return "".join(map(str, digits + [check]))


def random_alccode() -> str:
    """Generate 19-digit alcohol code."""
    return "0" + "".join(random.choices(string.digits, k=18))


def random_chestnyznak_mark(group: str = "tobacco") -> str:
    """Generate a Честный Знак mark code."""
    if group == "tobacco":
        # GS1 + cryptotail
        return "0104607034" + "".join(random.choices(string.digits, k=8)) + "91" + "".join(random.choices(string.ascii_letters + string.digits, k=8))
    if group == "milk":
        return "010" + "".join(random.choices(string.digits, k=13)) + "215" + "".join(random.choices(string.ascii_letters + string.digits, k=6))
    return "01" + "".join(random.choices(string.digits, k=13)) + "21" + "".join(random.choices(string.ascii_letters + string.digits, k=6))


def random_tax_rate(category: str) -> str:
    """Tax rate per category (RU realistic)."""
    if category in ("Хлебобулочные изделия", "Молочная продукция", "Бакалея", "Кондитерские изделия"):
        return random.choice(["10", "10", "20"])  # mostly 10% with some 20%
    if category == "Алкогольная продукция" or category == "Табачные изделия":
        return "20"
    return random.choice(["20", "10"])


def render_product(category: str, template: tuple, group_id: str) -> dict:
    """Render one product from template into a dict."""
    name_tmpl, vars_, unit, _tax_pct = template[:4]
    is_excise = template[4] if len(template) > 4 else False
    alc_volume = template[5] if len(template) > 5 else None
    mark_group_override = template[6] if len(template) > 6 else None

    fillings = {}
    for var_name, choices in vars_:
        fillings[var_name] = random.choice(choices)
    name = name_tmpl.format(**fillings)

    product = {
        "id": str(uuid.uuid4()),
        "name": name,
        "article": "ART-" + "".join(random.choices(string.digits, k=6)),
        "barcode": random_ean13(),
        "group_id": group_id,
        "category": category,
        "unit_okei": OKEI_BY_NAME[unit],
        "unit_name": {"pcs": "шт", "kg": "кг", "g": "г", "l": "л", "ml": "мл", "m": "м"}[unit],
        "tax_rate": random_tax_rate(category),
        "description": random.choice([None, None, f"Качественный {category.lower()}", "Производство РФ", None]),
    }

    if is_excise:
        product["alc_code"] = random_alccode()
        product["alc_volume"] = alc_volume
        product["alc_capacity"] = fillings.get("volume", "0.5л")
        product["mark_group"] = "alcohol"
    elif mark_group_override:
        product["mark_group"] = mark_group_override
        product["mark_code"] = random_chestnyznak_mark(mark_group_override)
    elif category in MARK_GROUPS and random.random() < 0.3:
        product["mark_group"] = MARK_GROUPS[category]
        product["mark_code"] = random_chestnyznak_mark(MARK_GROUPS[category])

    return product


# ── XML rendering ────────────────────────────────────────────────────────────

def render_import_xml(products: list[dict], groups: dict, lang: str = "ru") -> str:
    """Build a CommerceML 2.05 import.xml."""
    now = datetime.now() - timedelta(days=random.randint(0, 30))
    ts = now.isoformat(timespec="seconds")

    # Tag names (Russian or English variants — CommerceML supports both)
    T = {
        "ru": {
            "root": "КоммерческаяИнформация",
            "classifier": "Классификатор",
            "id": "Ид",
            "name": "Наименование",
            "groups": "Группы",
            "group": "Группа",
            "catalog": "Каталог",
            "products": "Товары",
            "product": "Товар",
            "article": "Артикул",
            "base_unit": "БазоваяЕдиница",
            "barcode": "ШтрихКод",
            "tax_rates": "СтавкиНалогов",
            "tax_rate": "СтавкаНалога",
            "tax_value": "Ставка",
            "description": "Описание",
        },
        "en": {
            "root": "CommercialInformation",
            "classifier": "Classifier",
            "id": "Id",
            "name": "Name",
            "groups": "Groups",
            "group": "Group",
            "catalog": "Catalog",
            "products": "Products",
            "product": "Product",
            "article": "Article",
            "base_unit": "BaseUnit",
            "barcode": "Barcode",
            "tax_rates": "TaxRates",
            "tax_rate": "TaxRate",
            "tax_value": "Rate",
            "description": "Description",
        },
    }[lang]

    classifier_id = str(uuid.uuid4())
    catalog_id = str(uuid.uuid4())

    lines: list[str] = []
    lines.append('<?xml version="1.0" encoding="UTF-8"?>')
    lines.append(f'<{T["root"]} ВерсияСхемы="2.05" ДатаФормирования="{ts}">' if lang == "ru" else f'<{T["root"]} SchemaVersion="2.05" CreationDate="{ts}">')

    # Classifier
    lines.append(f"  <{T['classifier']}>")
    lines.append(f"    <{T['id']}>{classifier_id}</{T['id']}>")
    lines.append(f"    <{T['name']}>Основной классификатор</{T['name']}>")
    lines.append(f"    <{T['groups']}>")
    for gid, gname in groups.items():
        lines.append(f"      <{T['group']}>")
        lines.append(f"        <{T['id']}>{gid}</{T['id']}>")
        lines.append(f"        <{T['name']}>{escape(gname)}</{T['name']}>")
        lines.append(f"      </{T['group']}>")
    lines.append(f"    </{T['groups']}>")
    lines.append(f"  </{T['classifier']}>")

    # Catalog
    lines.append(f'  <{T["catalog"]} СодержитТолькоИзменения="false">')
    lines.append(f"    <{T['id']}>{catalog_id}</{T['id']}>")
    lines.append(f"    <{T['name']}>Каталог товаров</{T['name']}>")
    lines.append(f"    <{T['products']}>")

    for p in products:
        lines.append(f"      <{T['product']}>")
        lines.append(f"        <{T['id']}>{p['id']}</{T['id']}>")
        lines.append(f"        <{T['article']}>{p['article']}</{T['article']}>")
        lines.append(f"        <{T['name']}>{escape(p['name'])}</{T['name']}>")
        lines.append(f'        <{T["base_unit"]} Код="{p["unit_okei"]}" НаименованиеПолное="{p["unit_name"].title()}">{p["unit_name"]}</{T["base_unit"]}>')
        lines.append(f"        <{T['groups']}><{T['id']}>{p['group_id']}</{T['id']}></{T['groups']}>")
        lines.append(f"        <{T['barcode']}>{p['barcode']}</{T['barcode']}>")
        lines.append(f"        <{T['tax_rates']}><{T['tax_rate']}><{T['name']}>НДС</{T['name']}><{T['tax_value']}>{p['tax_rate']}</{T['tax_value']}></{T['tax_rate']}></{T['tax_rates']}>")
        if p.get("description"):
            lines.append(f"        <{T['description']}>{escape(p['description'])}</{T['description']}>")
        lines.append(f"      </{T['product']}>")

    lines.append(f"    </{T['products']}>")
    lines.append(f"  </{T['catalog']}>")
    lines.append(f"</{T['root']}>")

    return "\n".join(lines)


# ── Main generator ───────────────────────────────────────────────────────────

def make_one(min_products: int = 5, max_products: int = 60, lang: str = "ru") -> tuple[str, list[dict]]:
    """Generate one synthetic 1C catalog + return raw_products meta."""
    n = random.randint(min_products, max_products)

    # Pick categories to include in this catalog
    categories = random.sample(list(PRODUCT_TEMPLATES.keys()),
                                k=random.randint(1, min(5, len(PRODUCT_TEMPLATES))))

    # Build groups (one group_id per category)
    groups = {str(uuid.uuid4()): cat for cat in categories}
    cat_to_gid = {v: k for k, v in groups.items()}

    products = []
    for _ in range(n):
        cat = random.choice(categories)
        tmpl = random.choice(PRODUCT_TEMPLATES[cat])
        products.append(render_product(cat, tmpl, cat_to_gid[cat]))

    xml = render_import_xml(products, groups, lang=lang)
    return xml, products


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=300, help="how many XMLs to generate")
    ap.add_argument("--out", default="examples/synthetic_1c", help="output dir")
    ap.add_argument("--min-products", type=int, default=5)
    ap.add_argument("--max-products", type=int, default=80)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    random.seed(args.seed)
    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out
    out.mkdir(parents=True, exist_ok=True)

    print(f"[*] Generating {args.count} XMLs into {out}…")
    total_products = 0
    for i in range(args.count):
        # 1 in 10 in English tags, rest in Russian
        lang = "en" if random.random() < 0.1 else "ru"
        xml, products = make_one(args.min_products, args.max_products, lang=lang)
        path = out / f"import_{i:04d}.xml"
        path.write_text(xml, encoding="utf-8")
        total_products += len(products)
        if (i + 1) % 50 == 0:
            print(f"  {i + 1}/{args.count} files, {total_products} products so far")

    print(f"[OK] {args.count} XMLs, {total_products} products total")
    print(f"     Average: {total_products / args.count:.1f} products/file")


if __name__ == "__main__":
    main()
