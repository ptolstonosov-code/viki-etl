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

import json
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

    def __init__(self, db_path: str | Path, rules_path: str | Path | None = None,
                 quarantine_dir: str | Path | None = None):
        self.db_path = Path(db_path)
        self._lock = threading.Lock()
        self._table_columns: dict[str, set[str]] = {}
        # App-level validation for open-domain enum columns (LLM-curated).
        # Use the un-resolved path so symlinked data/ -> /dev/shm keeps config on disk.
        self.rules_path = Path(rules_path) if rules_path else (
            self.db_path.parent.parent / "config" / "value_allowlists.json")
        self.quarantine_dir = Path(quarantine_dir) if quarantine_dir else (
            self.db_path.parent / "data_quarantine")
        self._allow: dict[str, set[str]] = {}
        self._map: dict[str, dict[str, str]] = {}
        self._accept_any: set[str] = set()
        self.reload_rules()

    def reload_rules(self) -> None:
        """(Re)load the LLM-curated allow/map rules from value_allowlists.json."""
        self._allow, self._map, self._accept_any = {}, {}, set()
        try:
            data = json.loads(self.rules_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return
        for key, spec in data.items():
            if key.startswith("_") or not isinstance(spec, dict):
                continue
            if spec.get("allow_any"):
                self._accept_any.add(key)   # open-domain column: accept any value
            self._allow[key] = {str(v) for v in (spec.get("allow") or [])}
            self._map[key] = {str(k): str(v) for k, v in (spec.get("map") or {}).items()}

    def _quarantine(self, table: str, column: str, value: str, data: dict) -> None:
        """Hold a record whose value isn't allowed yet — for LLM healing, not lost."""
        self.quarantine_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"table": table, "column": column, "value": value, "data": data},
                          ensure_ascii=False)
        with open(self.quarantine_dir / "rejects.jsonl", "a", encoding="utf-8") as f:
            f.write(line + "\n")

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
        stats: dict[str, Any] = {"inserted": 0, "skipped": 0, "held": 0, "errors": []}
        if not records:
            return stats

        with self._lock:
            conn = self._conn()
            try:
                pending = 0
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

                    # App-level enum validation (LLM-curated allowlists). An unknown
                    # value is HELD in the data-quarantine for LLM healing — not
                    # inserted, not lost. A mapped value is substituted in place.
                    held = False
                    for col in list(data.keys()):
                        key = f"{table}.{col}"
                        if data[col] is None or key in self._accept_any:
                            continue  # open-domain column → no gatekeeping
                        if key not in self._allow:
                            continue
                        sval = str(data[col])
                        mapped = self._map.get(key, {}).get(sval)
                        if mapped is not None:
                            data[col] = mapped
                        elif sval not in self._allow[key]:
                            self._quarantine(table, col, sval, data)
                            stats["held"] += 1
                            held = True
                            break
                    if held:
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
                        pending += 1
                    except sqlite3.IntegrityError as e:
                        stats["skipped"] += 1
                        pending += 1
                        if "UNIQUE" not in str(e) and "PRIMARY KEY" not in str(e):
                            stats["errors"].append(f"{table}: {e}")
                    except sqlite3.Error as e:
                        stats["errors"].append(f"{table}: {e}")

                    # Commit in chunks so a huge ingest never builds ONE giant
                    # transaction. A 6 MB / 1200-item file once hard-locked the
                    # box on the single final fsync — batching lets a low-RAM
                    # device and a weak eMMC drain I/O between chunks.
                    if pending >= 500:
                        conn.commit()
                        pending = 0
                conn.commit()
            finally:
                conn.close()
        return stats

    def held_summary(self) -> dict:
        """Quarantined (held) records grouped by '<table>.<column>' → {values:{v:count}, count}."""
        path = self.quarantine_dir / "rejects.jsonl"
        out: dict[str, dict] = {}
        if not path.exists():
            return out
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = f"{r.get('table')}.{r.get('column')}"
            slot = out.setdefault(key, {"values": {}, "count": 0})
            v = str(r.get("value"))
            slot["values"][v] = slot["values"].get(v, 0) + 1
            slot["count"] += 1
        return out

    def retry_held(self) -> dict:
        """Re-attempt held records after rules changed. Now-allowed rows insert;
        still-unknown rows get re-quarantined by write()."""
        self.reload_rules()
        path = self.quarantine_dir / "rejects.jsonl"
        if not path.exists():
            return {"retried": 0, "inserted": 0, "still_held": 0}
        records = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            records.append({"table": r.get("table"), "data": r.get("data") or {}})
        path.unlink()  # cleared; write() re-quarantines any still-unknown
        stats = self.write(records)
        return {"retried": len(records), "inserted": stats["inserted"], "still_held": stats["held"]}

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
