"""
Web viewer for the LLM ETL ARM device.

  /             — Status dashboard (main page)
  /db           — list tables
  /db/{table}   — browse a table (first 100 rows)
  /db/{table}/csv — export CSV
  /formats      — known/learned/quarantined formats
  /health       — health probe

All copy in Russian. No .format() on HTML (CSS braces break it) — we build
pages by string concatenation via _page().
"""

from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
import sys
import urllib.request
from urllib.parse import quote
from html import escape
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, StreamingResponse

ROOT = Path(os.environ.get("LLM_ETL_ROOT", "/opt/llm-etl"))
sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "data" / "etl.db"
API_URL = os.environ.get("LLM_ETL_API", "http://127.0.0.1:8080")

app = FastAPI(title="LLM ETL — Просмотр БД", version="1.0")


# ── Helpers ──────────────────────────────────────────────────────────────────

_CSS = """
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
       max-width: 1100px; margin: 2em auto; padding: 0 1em; color: #222; }
h1 { border-bottom: 2px solid #36a; padding-bottom: 0.3em; }
h2 { margin-top: 2em; color: #36a; }
.card { background: #f6f8fa; border-radius: 8px; padding: 1em 1.5em; margin: 1em 0; }
.kv { display: grid; grid-template-columns: max-content 1fr; gap: 0.4em 1.5em; }
.kv dt { font-weight: 600; color: #555; }
table { border-collapse: collapse; width: 100%; margin-top: 0.5em; }
th, td { padding: 0.4em 0.8em; text-align: left; border-bottom: 1px solid #ddd; }
th { background: #eef; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 0.85em; }
.badge-on { background: #d4f8d4; color: #060; }
.badge-off { background: #eee; color: #666; }
nav a { margin-right: 1.2em; color: #36a; text-decoration: none; }
nav a:hover { text-decoration: underline; }
.muted { color: #888; font-size: 0.9em; }
"""

_NAV = (
    '<nav><a href="/">📊 Статус</a>'
    '<a href="/upload">⬆ Загрузить файл</a>'
    '<a href="/db">📦 База данных</a>'
    '<a href="/formats">📁 Форматы</a></nav>'
)


def _page(title: str, body_html: str) -> str:
    """Assemble a full HTML page. No .format() — safe with CSS braces."""
    return (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8"/>'
        '<meta name="viewport" content="width=device-width, initial-scale=1"/>'
        "<title>" + escape(title) + "</title>"
        "<style>" + _CSS + "</style></head><body>"
        + _NAV + body_html +
        "</body></html>"
    )


def _db() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise HTTPException(503, "БД пока не создана")
    return sqlite3.connect(str(DB_PATH))


def _api_get(path: str) -> dict:
    try:
        with urllib.request.urlopen(f"{API_URL}{path}", timeout=4) as r:
            return json.loads(r.read())
    except Exception:
        return {}


# ── Status dashboard ─────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    status = _api_get("/status")
    formats = _api_get("/formats")

    # Records count from DB
    records_total = "—"
    try:
        conn = _db()
        rc = status.get("row_counts") or {}
        if rc:
            records_total = sum(rc.values())
        conn.close()
    except Exception:
        pass

    # Parsers table
    parsers = status.get("parsers", [])
    if parsers:
        rows = "".join(
            "<tr><td>" + escape(p["name"]) + "</td><td>" + str(p["priority"]) + "</td>"
            "<td>" + ("✓ активен" if p["enabled"] else "⏸ выключен") + "</td></tr>"
            for p in parsers
        )
        parsers_table = "<table><tr><th>Парсер</th><th>Приоритет</th><th>Статус</th></tr>" + rows + "</table>"
    else:
        parsers_table = '<p class="muted">API недоступен или парсеры не загружены.</p>'

    # LLM state
    if status.get("llm_running"):
        llm_state = '<span class="badge badge-on">🟢 работает</span> — занимает ~1.5 ГБ, выгрузится через минуту простоя'
    else:
        llm_state = '<span class="badge badge-off">⚫ выключена</span> — запустится автоматически при неизвестном формате'

    # Non-empty tables
    rc = status.get("row_counts") or {}
    nonzero = {k: v for k, v in rc.items() if v > 0}
    if nonzero:
        nz_rows = "".join("<tr><td>" + escape(k) + "</td><td>" + str(v) + "</td></tr>"
                          for k, v in sorted(nonzero.items()))
        nonzero_table = "<table><tr><th>Таблица</th><th>Записей</th></tr>" + nz_rows + "</table>"
    else:
        nonzero_table = '<p class="muted">Пока ничего не загружено. Отправьте файл на :8080/ingest</p>'

    # Warnings (quarantine / shadow)
    warnings = []
    for b in formats.get("buckets", []):
        st = b.get("status")
        fp = escape(str(b.get("fingerprint", "?")))
        if st == "collecting":
            warnings.append("⚠ Новый формат " + fp + ": встречен " + str(b.get("count", 0))
                            + " раз. При 5 примерах создам парсер автоматически.")
        elif st == "shadow":
            warnings.append("🧪 Парсер для " + fp + " на тестировании, точность "
                            + str(b.get("shadow_avg_f1", "—")))
    warnings_block = ""
    if warnings:
        warnings_block = '<div class="card"><h2>🚨 Внимание</h2><p>' + "<br/>".join(warnings) + "</p></div>"

    body = (
        "<h1>LLM ETL — состояние системы</h1>"
        '<div class="card"><h2>📊 Сводка</h2>'
        '<dl class="kv">'
        "<dt>Записей в БД:</dt><dd>" + str(records_total) + "</dd>"
        "<dt>Загружено форматов-парсеров:</dt><dd>" + str(len(parsers)) + "</dd>"
        "<dt>База данных:</dt><dd>" + escape(str(DB_PATH)) + "</dd>"
        "</dl></div>"
        '<div class="card"><h2>🧩 Парсеры</h2>' + parsers_table + "</div>"
        '<div class="card"><h2>📦 Данные в таблицах</h2>' + nonzero_table + "</div>"
        '<div class="card"><h2>🌡️ Нейросеть</h2><p>' + llm_state + "</p>"
        '<p class="muted">Запускается только когда парсер не знает формат, '
        "выключается через минуту простоя — экономит память.</p></div>"
        + warnings_block
    )
    return _page("LLM ETL — статус", body)


# ── DB browser ───────────────────────────────────────────────────────────────

@app.get("/db", response_class=HTMLResponse)
async def db_index():
    try:
        conn = _db()
    except HTTPException:
        return HTMLResponse(_page("БД", "<h1>База данных</h1><p>БД ещё не создана.</p>"))
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
    tables = [r[0] for r in cur.fetchall()]
    rows = []
    for t in tables:
        try:
            n = conn.execute('SELECT COUNT(*) FROM "' + t + '"').fetchone()[0]
        except sqlite3.Error:
            n = "?"
        rows.append('<tr><td><a href="/db/' + escape(t) + '">' + escape(t) + "</a></td><td>" + str(n) + "</td></tr>")
    conn.close()
    body = ("<h1>База данных</h1><table><tr><th>Таблица</th><th>Записей</th></tr>"
            + "".join(rows) + "</table>")
    return _page("База данных", body)


@app.get("/db/{table}", response_class=HTMLResponse)
async def db_table(table: str, limit: int = 100):
    conn = _db()
    try:
        cur = conn.execute('SELECT * FROM "' + table + '" LIMIT ?', (limit,))
        cols = [c[0] for c in cur.description]
        rows = cur.fetchall()
    except sqlite3.Error as e:
        conn.close()
        raise HTTPException(404, str(e))
    conn.close()
    header = "".join("<th>" + escape(c) + "</th>" for c in cols)
    body_rows = "".join(
        "<tr>" + "".join("<td>" + escape(str(v)[:200]) + "</td>" for v in r) + "</tr>"
        for r in rows
    )
    body = ("<h1>Таблица: " + escape(table) + "</h1>"
            '<p><a href="/db">← все таблицы</a> &nbsp; '
            '<a href="/db/' + escape(table) + '/csv">⬇ скачать CSV</a></p>'
            "<table><tr>" + header + "</tr>" + body_rows + "</table>"
            '<p class="muted">Показаны первые ' + str(limit) + " строк.</p>")
    return _page("Таблица " + table, body)


@app.get("/db/{table}/csv")
async def db_table_csv(table: str):
    conn = _db()
    try:
        cur = conn.execute('SELECT * FROM "' + table + '"')
        cols = [c[0] for c in cur.description]
    except sqlite3.Error as e:
        conn.close()
        raise HTTPException(404, str(e))

    def _gen():
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(cols)
        yield buf.getvalue()
        for row in cur:
            buf.seek(0); buf.truncate()
            w.writerow(row)
            yield buf.getvalue()
        conn.close()

    return StreamingResponse(_gen(), media_type="text/csv",
                             headers={"Content-Disposition": 'attachment; filename="' + table + '.csv"'})


# ── Formats ──────────────────────────────────────────────────────────────────

@app.get("/formats", response_class=HTMLResponse)
async def formats_page():
    formats = _api_get("/formats")
    label = {"collecting": "🟡 собирает примеры", "shadow": "🧪 тестируется",
             "promoted": "✅ работает", "abandoned": "❌ отброшен"}
    rows = []
    for b in formats.get("buckets", []):
        st = label.get(b.get("status", ""), str(b.get("status", "?")))
        fnames = ", ".join(b.get("filenames", [])[:3])
        rows.append("<tr><td>" + escape(str(b.get("fingerprint", "?"))) + "</td><td>" + st + "</td><td>"
                    + str(b.get("count", 0)) + "</td><td>" + escape(fnames) + "</td><td>"
                    + str(b.get("shadow_avg_f1", "—")) + "</td></tr>")
    table = ("<table><tr><th>Отпечаток</th><th>Статус</th><th>Файлов</th>"
             "<th>Примеры имён</th><th>Точность</th></tr>" + "".join(rows) + "</table>") if rows else \
            '<p class="muted">Пока не встречалось неизвестных форматов.</p>'
    body = ("<h1>Форматы данных</h1>" + table
            + '<p class="muted">«Собирает примеры» = парсер ещё не создан, файлы идут через нейросеть. '
            "Через 5 примеров система создаст парсер автоматически.</p>")
    return _page("Форматы", body)


# ── Upload (browser form → API /ingest) ─────────────────────────────────────

_UPLOAD_FORM = """
<h1>Загрузить файл на разбор</h1>
<div class="card">
<p>Выберите файл выгрузки 1С (XML EnterpriseData / CommerceML), прайс CSV или
другой поддерживаемый формат. Файл будет разобран и записан в базу.</p>
<form action="/upload" method="post" enctype="multipart/form-data">
  <p><input type="file" name="file" required
            style="padding:0.5em; border:1px solid #ccc; border-radius:6px;"/></p>
  <p><button type="submit"
       style="background:#36a; color:#fff; border:none; padding:0.6em 1.5em;
              border-radius:6px; font-size:1em; cursor:pointer;">
     Разобрать и записать в БД</button></p>
</form>
<p class="muted">Можно загружать файлы по одному. Известные форматы 1С
обрабатываются мгновенно. Незнакомый формат запустит нейросеть (медленнее).</p>
</div>
"""


@app.get("/upload", response_class=HTMLResponse)
async def upload_form():
    return _page("Загрузка файла", _UPLOAD_FORM)


@app.post("/upload", response_class=HTMLResponse)
async def upload_file(file: UploadFile = File(...)):
    raw = await file.read()
    if not raw:
        return _page("Загрузка", _UPLOAD_FORM + '<div class="card"><p>❗ Пустой файл.</p></div>')

    # Forward raw bytes to the API /ingest/raw endpoint
    req = urllib.request.Request(
        f"{API_URL}/ingest/raw",
        data=raw,
        headers={"Content-Type": "application/octet-stream",
                 "X-Filename": quote(file.filename or "upload.dat")},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            result = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        body = _UPLOAD_FORM + '<div class="card"><h2>❌ Ошибка</h2><p>' + escape(str(e)) + "</p></div>"
        return _page("Загрузка — ошибка", body)

    parser = result.get("parser", "?")
    recs = result.get("records", [])
    db = result.get("db", {})
    quarantined = result.get("quarantined", False)
    elapsed = result.get("elapsed_ms")

    # Summary by table
    by_table = {}
    for r in recs:
        t = r.get("table", "?")
        by_table[t] = by_table.get(t, 0) + 1
    table_rows = "".join("<tr><td>" + escape(k) + "</td><td>" + str(v) + "</td></tr>"
                         for k, v in sorted(by_table.items()))

    parser_badge = ('<span class="badge badge-on">' + escape(parser) + "</span>"
                    if parser != "LLM" else
                    '<span class="badge" style="background:#ffe4b5;color:#844;">нейросеть (новый формат)</span>')

    body = (
        "<h1>Результат разбора</h1>"
        '<div class="card">'
        "<p><b>Файл:</b> " + escape(file.filename or "—") + "</p>"
        "<p><b>Парсер:</b> " + parser_badge
        + (" &nbsp; <span class='muted'>" + str(round(elapsed, 1)) + " мс</span>" if elapsed else "")
        + "</p>"
        "<p><b>Записано в БД:</b> вставлено " + str(db.get("inserted", 0))
        + ", пропущено " + str(db.get("skipped", 0))
        + ", ошибок " + str(len(db.get("errors", []))) + "</p>"
        + (("<p class='muted'>⚠ Формат пока неизвестен — сохранён для обучения парсера.</p>") if quarantined else "")
        + "</div>"
        '<div class="card"><h2>Извлечено записей: ' + str(len(recs)) + "</h2>"
        + ("<table><tr><th>Таблица</th><th>Записей</th></tr>" + table_rows + "</table>" if table_rows
           else "<p class='muted'>Записей не извлечено.</p>")
        + "</div>"
        + (("<div class='card'><h2>Ошибки записи</h2><pre>"
            + escape("\n".join(map(str, db.get("errors", [])[:10]))) + "</pre></div>")
           if db.get("errors") else "")
        + '<p><a href="/upload">⬆ Загрузить ещё</a> &nbsp; '
          '<a href="/db">📦 Посмотреть БД</a></p>'
    )
    return _page("Результат разбора", body)


@app.get("/health")
async def health():
    return {"status": "ok"}
