"""
Lightweight SQLite writer — no SQLAlchemy dependency.

For the ARM device we want a minimal footprint: pure stdlib `sqlite3` module,
no ORM, no migrations engine. This module:

  1. Initialises the schema from `config/schema.sql` if the DB is empty.
  2. Writes records (list of {"table": ..., "data": {...}}) into matching tables.
  3. Handles conflicts gracefully (INSERT OR REPLACE for master data,
     INSERT OR IGNORE for documents) based on schema.yaml conflict_strategy.

Stats are returned by write(): inserted, skipped, errors.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ── Schema init ──────────────────────────────────────────────────────────────

def init_db(db_path: str | Path, schema_sql_path: str | Path) -> dict:
    """
    Create the database file and run schema.sql if no user tables exist yet.
    Returns: {"created": bool, "tables": int}
    """
    db_path = Path(db_path)
    schema_sql_path = Path(schema_sql_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
        n_existing = cur.fetchone()[0]
        if n_existing > 0:
            logger.info("DB already has %d tables — skipping init", n_existing)
            return {"created": False, "tables": n_existing}

        sql_text = schema_sql_path.read_text(encoding="utf-8")
        # executescript handles multiple statements separated by ;
        # SQLite is permissive — ignore errors per-statement so we don't fail
        # on one malformed CHECK constraint
        statements = _split_statements(sql_text)
        for stmt in statements:
            if not stmt.strip():
                continue
            try:
                conn.execute(stmt)
            except sqlite3.Error as e:
                logger.warning("skipped statement: %s …  (%s)", stmt[:80], e)
        conn.commit()
        cur = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
        n = cur.fetchone()[0]
        logger.info("DB initialised with %d tables", n)
        return {"created": True, "tables": n}
    finally:
        conn.close()


def _split_statements(sql_text: str) -> list[str]:
    """Crude statement splitter — works for our schema (no ; inside strings)."""
    out, buf = [], []
    for line in sql_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("--") or not stripped:
            if buf:  # keep comments inside multi-line statements
                buf.append(line)
            continue
        buf.append(line)
        if stripped.endswith(";"):
            out.append("\n".join(buf))
            buf = []
    if buf:
        out.append("\n".join(buf))
    return out


# ── Writer ───────────────────────────────────────────────────────────────────

class SQLiteWriter:
    """
    Writes parser records into a SQLite database.

    Conflict strategy (per table):
      master data (catalog_*, variant_*, legal_entity, counterparty, etc.)
        → INSERT OR REPLACE  (overwrite on PK conflict)
      documents (doc_*)
        → INSERT OR IGNORE   (skip duplicates)
      registries (stock_*)
        → INSERT (raw append)
    """

    _MASTER_TABLES = {
        "catalog_group", "catalog_group_attribute", "catalog_product",
        "catalog_variant", "variant_price", "variant_barcode", "variant_image",
        "variant_article", "variant_marked_meta", "variant_alcohol_meta",
        "variant_egais_mark", "variant_thuemark_mark",
        "catalog_attribute", "catalog_attribute_value", "variant_attribute",
        "composition_bom",
        "legal_entity", "shop", "warehouse",
        "counterparty", "counterparty_bank_account", "edo_opertor",
    }
    _DOC_TABLES_PREFIX = ("doc_",)

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._lock = threading.Lock()
        self._table_columns: dict[str, set[str]] = {}

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=15)
        conn.execute("PRAGMA foreign_keys = OFF")  # tolerate parser ordering
        return conn

    def _columns_of(self, conn: sqlite3.Connection, table: str) -> set[str]:
        if table not in self._table_columns:
            try:
                rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
                self._table_columns[table] = {r[1] for r in rows}
            except sqlite3.Error:
                return set()
        return self._table_columns[table]

    def _conflict_clause(self, table: str) -> str:
        if table in self._MASTER_TABLES:
            return "OR REPLACE"
        if any(table.startswith(p) for p in self._DOC_TABLES_PREFIX):
            return "OR IGNORE"
        return ""

    def write(self, records: list[dict]) -> dict:
        """
        Write a batch of records. Returns
            {"inserted": int, "skipped": int, "errors": list[str]}.
        """
        stats: dict[str, Any] = {"inserted": 0, "skipped": 0, "errors": []}
        if not records:
            return stats

        with self._lock:
            conn = self._conn()
            try:
                for rec in records:
                    table = rec.get("table") or ""
                    data = rec.get("data") or {}
                    if not table or not isinstance(data, dict):
                        stats["errors"].append("malformed record (no table/data)")
                        continue
                    cols = self._columns_of(conn, table)
                    if not cols:
                        stats["errors"].append(f"unknown table: {table}")
                        continue

                    # Filter only known columns, coerce booleans → int
                    clean = {}
                    for k, v in data.items():
                        if k not in cols:
                            continue
                        if isinstance(v, bool):
                            v = int(v)
                        clean[k] = v
                    if not clean:
                        stats["skipped"] += 1
                        continue

                    col_list = ", ".join(f'"{c}"' for c in clean.keys())
                    placeholders = ", ".join("?" * len(clean))
                    conflict = self._conflict_clause(table)
                    sql = f'INSERT {conflict} INTO "{table}" ({col_list}) VALUES ({placeholders})'.replace("  ", " ")

                    try:
                        conn.execute(sql, list(clean.values()))
                        stats["inserted"] += 1
                    except sqlite3.IntegrityError as e:
                        stats["skipped"] += 1
                        if "UNIQUE" not in str(e) and "PRIMARY KEY" not in str(e):
                            stats["errors"].append(f"{table}: {e}")
                    except sqlite3.Error as e:
                        stats["errors"].append(f"{table}: {e}")
                conn.commit()
            finally:
                conn.close()
        return stats

    def row_counts(self) -> dict[str, int]:
        """Return number of rows per table."""
        out: dict[str, int] = {}
        with self._lock:
            conn = self._conn()
            try:
                tables = [r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()]
                for t in tables:
                    try:
                        n = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                        out[t] = n
                    except sqlite3.Error:
                        out[t] = 0
            finally:
                conn.close()
        return dict(sorted(out.items()))
