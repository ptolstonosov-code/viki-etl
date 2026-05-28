"""
Write LLM-parsed records to the target database.
Uses SQLAlchemy Core for portability (SQLite by default).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import MetaData, Table, create_engine, func, inspect, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

ROOT = Path(__file__).parent.parent


def _load_schema_yaml() -> dict:
    with open(ROOT / "config" / "schema.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


class DBWriter:
    def __init__(self, dry_run: bool = False):
        self.cfg = _load_schema_yaml()
        self.dry_run = dry_run
        self.engine: Engine = create_engine(
            self.cfg["database"]["url"], future=True, echo=False
        )
        self._metadata = MetaData()
        # Initialise schema if requested and DB is empty
        if self.cfg["database"].get("init_on_startup", False):
            self._ensure_schema()
        self._metadata.reflect(bind=self.engine)

    # ── Public API ────────────────────────────────────────────────────────────

    def write(self, records: list[dict]) -> dict:
        """
        Insert records: [{"table": "...", "data": {...}}, ...]
        Returns stats: {"inserted": int, "skipped": int, "errors": [...]}.
        """
        stats: dict[str, Any] = {"inserted": 0, "skipped": 0, "errors": []}
        if not records:
            return stats

        with self.engine.begin() as conn:
            for rec in records:
                table_name = rec.get("table")
                data = rec.get("data", {})
                if table_name not in self._metadata.tables:
                    stats["errors"].append(f"Unknown table: {table_name}")
                    continue
                table = self._metadata.tables[table_name]
                clean = self._coerce_columns(table, data)
                strategy = self._strategy_for(table_name)
                try:
                    if self.dry_run:
                        stats["skipped"] += 1
                        continue
                    self._insert_with_strategy(conn, table, clean, strategy)
                    stats["inserted"] += 1
                except IntegrityError as e:
                    stats["skipped"] += 1
                    stats["errors"].append(f"{table_name}: {e.orig}")
                except SQLAlchemyError as e:
                    stats["errors"].append(f"{table_name}: {e}")
        return stats

    def list_tables(self) -> list[str]:
        return sorted(self._metadata.tables.keys())

    def row_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        with self.engine.connect() as conn:
            for name, table in self._metadata.tables.items():
                out[name] = conn.execute(select(func.count()).select_from(table)).scalar() or 0
        return out

    # ── Internals ─────────────────────────────────────────────────────────────

    def _ensure_schema(self) -> None:
        """Run schema.sql once if DB has no user tables yet."""
        with self.engine.begin() as conn:
            inspector = inspect(conn)
            existing = [t for t in inspector.get_table_names() if not t.startswith("sqlite_")]
            if existing:
                return
            sql_path = ROOT / self.cfg["database"]["schema_file"]
            sql_text = sql_path.read_text(encoding="utf-8")
            # SQLite needs statements one at a time
            for stmt in self._split_sql_statements(sql_text):
                if stmt.strip():
                    try:
                        conn.exec_driver_sql(stmt)
                    except SQLAlchemyError as e:
                        # Skip statements unsupported on this dialect (CHECK on quoted col, etc.)
                        print(f"[schema init] skipped: {e}")

    @staticmethod
    def _split_sql_statements(sql: str) -> list[str]:
        # Naive splitter — handles our schema (no semicolons inside strings/comments needed)
        out: list[str] = []
        buf: list[str] = []
        for line in sql.splitlines():
            stripped = line.strip()
            if stripped.startswith("--") or not stripped:
                continue
            buf.append(line)
            if stripped.endswith(";"):
                out.append("\n".join(buf))
                buf = []
        if buf:
            out.append("\n".join(buf))
        return out

    def _strategy_for(self, table_name: str) -> str:
        per_table = self.cfg.get("conflict_strategy", {}) or {}
        return per_table.get(table_name, self.cfg.get("default_conflict_strategy", "insert"))

    @staticmethod
    def _coerce_columns(table: Table, data: dict) -> dict:
        """Drop unknown keys, convert booleans to ints for SQLite."""
        cols = {c.name for c in table.columns}
        out: dict[str, Any] = {}
        for k, v in data.items():
            if k not in cols:
                continue
            if isinstance(v, bool):
                v = int(v)
            out[k] = v
        return out

    def _insert_with_strategy(self, conn, table: Table, data: dict, strategy: str) -> None:
        if not data:
            return
        if strategy == "insert" or self.engine.dialect.name != "sqlite":
            conn.execute(table.insert().values(**data))
            return
        stmt = sqlite_insert(table).values(**data)
        if strategy == "ignore":
            stmt = stmt.on_conflict_do_nothing()
        elif strategy == "update":
            pk_cols = [c.name for c in table.primary_key.columns]
            update_cols = {c.name: stmt.excluded[c.name] for c in table.columns if c.name not in pk_cols}
            if update_cols and pk_cols:
                stmt = stmt.on_conflict_do_update(index_elements=pk_cols, set_=update_cols)
            else:
                stmt = stmt.on_conflict_do_nothing()
        conn.execute(stmt)
