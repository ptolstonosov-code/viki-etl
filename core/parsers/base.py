"""
Base Parser interface for the LLM ETL plugin system.

Three types of parsers:
  1. Built-in Python parsers (enterprise_data, commerceml, ofd_receipt).
     Subclass Parser and implement can_parse + parse.
  2. Generated YAML parsers (in parsers/learned/ on the ARM device).
     Loaded and interpreted by yaml_engine.YamlParser at runtime.
  3. Shadow parsers (in parsers/shadow/) — same as learned but in
     trial mode: results compared against LLM, NOT written to DB
     until promoted.

The router (__init__.py) tries parsers in priority order. Highest priority
wins. LLM fallback (priority < 0) is last.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class Parser(ABC):
    """Base class for all parsers."""

    name: str = "unnamed"
    """Human-readable identifier (used in logs, UI). Must be unique."""

    priority: int = 50
    """0..100. Higher = tried first. Conventions:
       - 100: built-in formats (EnterpriseData, CommerceML — known very well)
       -  70-90: user-learned parsers (proven in shadow mode)
       -  30-60: experimental learned parsers
       -  10-20: shadow parsers (only for shadow eval, never main answer)
       -  -1:  LLM fallback (always last)
    """

    enabled: bool = True
    """Operator may disable via UI without removing the file."""

    @abstractmethod
    def can_parse(self, raw: bytes, filename: str | None = None) -> bool:
        """
        Fast sniff: does this look like my format?  Should run in <1ms.
        DO NOT try to actually parse here — just look at first few bytes,
        XML root tag, JSON top-level keys, file extension.
        """

    @abstractmethod
    def parse(self, raw: bytes) -> list[dict]:
        """
        Parse the raw payload and return DB-ready records.
        Each record: {"table": "<table_name>", "data": {col: value, ...}}
        Must NOT modify state. Must be idempotent.
        Raises ValueError on bad input — caller falls back to next parser.
        """

    # ── Metadata ──────────────────────────────────────────────────────────

    def stats(self) -> dict:
        """Return runtime stats; default empty. Built-ins may override."""
        return {}


# ── Helper: check / coerce parsed records against schema.sql ──────────────────

def validate_records(records: list[dict]) -> tuple[bool, list[str]]:
    """
    Sanity-check parser output against schema.sql.
    Returns (ok, list_of_errors). Empty errors = OK.
    Used by the router to decide whether to accept the result or try next parser.
    """
    # Lazy import to avoid hard dependency
    try:
        from training.annotation import validate_records as _v
        return _v(records)
    except ImportError:
        # Minimal stub if training module not present (target ARM)
        if not isinstance(records, list):
            return False, ["records is not a list"]
        errors = []
        for i, r in enumerate(records):
            if not isinstance(r, dict) or "table" not in r or "data" not in r:
                errors.append(f"[{i}] missing table/data")
        return (len(errors) == 0), errors
