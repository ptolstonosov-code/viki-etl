"""
Generate DIVERSE-FORMAT training data: arbitrary made-up product/inventory
formats (XML / JSON / CSV / key-value, with random-but-plausible field names,
including OmniLedger-style "alien" schemas) -> correct {table,data} records
using ONLY valid schema enum values, with values COPIED from the input.

Goal: teach the model that the *role* of a field (name, barcode, price, group,
unit, tax, flags) is constant regardless of how the field is *named* or the
file is *structured* — and to never invent values (copy them from the input).

Output: JSONL of {"input": "<raw>", "output": [ {table,data}, ... ]}
matching config/schema.sql, same shape as examples/training_data/synthetic_1c.jsonl.
"""
from __future__ import annotations
import json, random, uuid, argparse, html

# ── valid schema enums (from config/schema.sql) ──────────────────────────────
UNITS = {"796": "шт", "166": "кг", "112": "л", "006": "м", "163": "г",
         "168": "т", "111": "м3", "055": "м2"}
TAX_RATES = ["NDS_NO_TAX", "NDS_10", "NDS_22", "NDS_5", "NDS_7"]
PRODUCT_TYPES = ["default", "default", "default", "default", "service", "dish"]
BARCODE_TYPES = ["EAN13", "EAN8", "Code128", "ITF14"]
MARK_GROUPS = ["alcohol", "tobacco", "milk", "beer", "water", "perfumery",
               "shoes", "lp", "tires", "bicycle"]

# ── value pools ──────────────────────────────────────────────────────────────
ADJ = ["Премиум", "Классический", "Органический", "Элитный", "Фермерский",
       "Neon", "Vintage", "Artisanal", "Synthetic", "Industrial", "Smart",
       "Quantum", "Eco", "Handcrafted", "Experimental"]
NOUN = ["Молоко", "Сыр", "Куртка", "Бочка", "Икра", "Дисплей", "Коньяк",
        "Клапан", "Матча", "Удобрение", "Трубка", "Парфюм", "Пиво", "Вино",
        "Jacket", "Barrel", "Caviar", "Display", "Valve", "Powder", "Pipe"]
SUFFIX = ["0.5л", "250г", "50L", "8K", "1985", "No.5", "V9", "Premium", "5.2%", ""]
COUNTRIES = ["Россия", "Japan", "France", "South Korea", "USA", "Italy",
             "Belarus", "Turkey", "Germany", "China"]
GROUPS = ["Молочная продукция", "Алкоголь", "Apparel", "Electronics", "Food",
          "Spirits", "Machinery", "Cosmetics", "Beverages", "Agriculture",
          "Tobacco", "General", "Light Industry"]

# field-name synonym pools (the SAME role, many possible names + made-up ones)
F_NAME = ["name", "title", "label", "Наименование", "HumanTag", "designation",
          "product_name", "ProductTitle", "caption", "Bezeichnung", "nom"]
F_BARCODE = ["barcode", "ean", "code", "ScanPattern", "Штрихкод", "gtin",
             "scan_code", "BarCode", "upc"]
F_ARTICLE = ["article", "sku", "art", "Артикул", "ShadowCode", "vendor_code",
             "item_code", "ref", "КодВПрограмме"]
F_GROUP = ["group", "category", "Группа", "Essence", "Regime", "section",
           "kind", "Категория", "dept"]
F_PRICE = ["price", "Цена", "cost", "amount", "retail_price", "Стоимость"]
F_UNIT = ["unit", "ЕдиницаИзмерения", "measure", "uom", "Unit", "ед"]
F_COUNTRY = ["country", "origin", "TerraOrigin", "Страна", "made_in"]


def _rand_barcode(bt):
    if bt == "EAN8":
        return "".join(random.choice("0123456789") for _ in range(8))
    if bt == "ITF14":
        return "".join(random.choice("0123456789") for _ in range(14))
    if bt == "Code128":
        return random.choice(["ABC", "X", "TRU", "ELEC", "FOOD"]) + "-" + \
               "".join(random.choice("0123456789") for _ in range(random.randint(4, 8)))
    return "".join(random.choice("0123456789") for _ in range(13))  # EAN13


def _rand_article():
    return random.choice(["ART", "SKU", "TRU", "00", "ELEC", "BEV"]) + "-" + \
           "".join(random.choice("0123456789") for _ in range(random.randint(4, 8)))


def _make_product():
    name = " ".join(x for x in [random.choice(ADJ), random.choice(NOUN),
                                 random.choice(SUFFIX)] if x).strip()
    bt = random.choice(BARCODE_TYPES)
    p = {
        "pid": str(uuid.uuid4()),
        "gid": None,           # filled by group
        "name": name,
        "group": random.choice(GROUPS),
        "barcode": _rand_barcode(bt),
        "barcode_type": bt,
        "article": _rand_article(),
        "unit": random.choice(list(UNITS.keys())),
        "tax": random.choice(TAX_RATES),
        "type": random.choice(PRODUCT_TYPES),
        "price_kop": random.randint(1000, 5000000),
        "country": random.choice(COUNTRIES),
        "excise": 0, "alcohol": 0, "marked": 0, "mark_group": None,
    }
    if p["group"] in ("Алкоголь", "Spirits") or random.random() < 0.12:
        p["alcohol"] = 1; p["excise"] = 1; p["marked"] = 1; p["mark_group"] = "alcohol"
    elif p["group"] == "Tobacco" or random.random() < 0.08:
        p["marked"] = 1; p["mark_group"] = "tobacco"
    elif random.random() < 0.1:
        p["marked"] = 1; p["mark_group"] = random.choice(MARK_GROUPS)
    return p


def _build_output(products, groups_map):
    out = []
    for gname, gid in groups_map.items():
        out.append({"table": "catalog_group",
                    "data": {"id": gid, "parent_id": None, "name": gname}})
    for p in products:
        out.append({"table": "catalog_product", "data": {
            "id": p["pid"], "name": p["name"], "type": p["type"],
            "tax_rate": p["tax"], "group_id": p["gid"], "unit": p["unit"],
            "excise": p["excise"], "marked": p["marked"], "alcohol": p["alcohol"],
        }})
        out.append({"table": "catalog_variant",
                    "data": {"id": p["pid"], "product_id": p["pid"], "display_name": p["name"]}})
        out.append({"table": "variant_barcode", "data": {
            "variant_id": p["pid"], "barcode": p["barcode"], "type": p["barcode_type"]}})
        out.append({"table": "variant_article",
                    "data": {"variant_id": p["pid"], "article": p["article"]}})
        if p["marked"] and p["mark_group"]:
            out.append({"table": "variant_marked_meta",
                        "data": {"variant_id": p["pid"], "group": p["mark_group"]}})
    return out


# ── format renderers (field names randomized per example) ────────────────────
def _fields():
    return {k: random.choice(pool) for k, pool in [
        ("name", F_NAME), ("barcode", F_BARCODE), ("article", F_ARTICLE),
        ("group", F_GROUP), ("price", F_PRICE), ("unit", F_UNIT),
        ("country", F_COUNTRY)]}


def _render_json(products):
    f = _fields()
    root_key = random.choice(["items", "products", "catalog", "Artifacts", "goods", "data"])
    arr = []
    for p in products:
        o = {f["name"]: p["name"], f["group"]: p["group"],
             f["barcode"]: p["barcode"], f["article"]: p["article"],
             f["unit"]: UNITS[p["unit"]], f["price"]: round(p["price_kop"] / 100, 2),
             f["country"]: p["country"]}
        if p["alcohol"]:
            o["alcohol"] = True
        if p["marked"]:
            o["traceable"] = True
        arr.append(o)
    return json.dumps({root_key: arr}, ensure_ascii=False, indent=2)


def _render_csv(products):
    f = _fields()
    cols = [f["name"], f["group"], f["barcode"], f["article"], f["unit"], f["price"], f["country"]]
    sep = random.choice([",", ";", "\t"])
    lines = [sep.join(cols)]
    for p in products:
        lines.append(sep.join([p["name"], p["group"], p["barcode"], p["article"],
                               UNITS[p["unit"]], str(round(p["price_kop"] / 100, 2)), p["country"]]))
    return "\n".join(lines)


def _render_xml(products):
    f = _fields()
    item_tag = random.choice(["item", "Product", "Artifact", "Position", "Tovar", "row"])
    root_tag = random.choice(["Catalog", "OmniLedger", "Export", "Goods", "Inventory"])
    parts = ['<?xml version="1.0" encoding="UTF-8"?>', f"<{root_tag}>"]
    for p in products:
        e = html.escape
        attrs = ""
        flags = ""
        if random.random() < 0.5:  # OmniLedger-style flag elements
            flags = (f'<Bureaucracy><Excise applicable="{str(bool(p["excise"])).lower()}"/>'
                     f'<Traceability required="{str(bool(p["marked"])).lower()}"/>'
                     f'<Alcohol is="{str(bool(p["alcohol"])).lower()}"/></Bureaucracy>')
        else:
            if p["excise"]:
                flags += "<excise>true</excise>"
            if p["marked"]:
                flags += "<marked>true</marked>"
            if p["alcohol"]:
                flags += "<alcohol>true</alcohol>"
        parts.append(
            f'  <{item_tag}{attrs}>'
            f'<{f["name"]}>{e(p["name"])}</{f["name"]}>'
            f'<{f["group"]}>{e(p["group"])}</{f["group"]}>'
            f'<{f["barcode"]}>{p["barcode"]}</{f["barcode"]}>'
            f'<{f["article"]}>{e(p["article"])}</{f["article"]}>'
            f'<{f["unit"]}>{UNITS[p["unit"]]}</{f["unit"]}>'
            f'<{f["price"]}>{round(p["price_kop"]/100,2)}</{f["price"]}>'
            f'<{f["country"]}>{e(p["country"])}</{f["country"]}>'
            f'{flags}</{item_tag}>')
    parts.append(f"</{root_tag}>")
    return "\n".join(parts)


RENDERERS = [_render_xml, _render_xml, _render_json, _render_json, _render_csv]


def make_example():
    n = random.randint(1, 6)
    products = [_make_product() for _ in range(n)]
    # assign group ids
    groups_map = {}
    for p in products:
        if p["group"] not in groups_map:
            groups_map[p["group"]] = str(uuid.uuid4())
        p["gid"] = groups_map[p["group"]]
    raw = random.choice(RENDERERS)(products)
    out = _build_output(products, groups_map)
    return {"input": raw, "output": out}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=3000)
    ap.add_argument("-o", default="diverse_formats.jsonl")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--preview", action="store_true")
    args = ap.parse_args()
    random.seed(args.seed)

    if args.preview:
        for _ in range(2):
            ex = make_example()
            print("===== INPUT ====="); print(ex["input"][:900])
            print("===== OUTPUT (first 3) ====="); print(json.dumps(ex["output"][:3], ensure_ascii=False, indent=1))
            print()
        raise SystemExit

    with open(args.o, "w", encoding="utf-8") as fh:
        for _ in range(args.n):
            fh.write(json.dumps(make_example(), ensure_ascii=False) + "\n")
    print(f"wrote {args.n} examples to {args.o}")
