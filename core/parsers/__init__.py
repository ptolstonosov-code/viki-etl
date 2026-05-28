"""
Parser registry + router.

Discovers all Parser subclasses in this package and in optional
plugin directories (parsers/learned/, parsers/shadow/), sorts them
by priority, and provides the main entry point `route_and_parse()`.

Use it like:
    from core.parsers import route_and_parse
    records, parser_name = route_and_parse(raw_bytes, filename="x.xml")
    # → parser_name = "enterprise_data" / "commerceml" / "auto_format_X" / "LLM"
"""

from __future__ import annotations

import importlib
import logging
import time
from pathlib import Path
from typing import Callable

from .base import Parser, validate_records

logger = logging.getLogger(__name__)

# ── Registry ─────────────────────────────────────────────────────────────────

_PARSERS: list[Parser] = []
_LLM_FALLBACK: Callable[[bytes], list[dict]] | None = None


def register(parser: Parser) -> None:
    """Add a parser to the registry. Called by built-in parsers' __init__."""
    if not isinstance(parser, Parser):
        raise TypeError(f"{parser!r} is not a Parser instance")
    # Avoid duplicates by name
    _PARSERS[:] = [p for p in _PARSERS if p.name != parser.name]
    _PARSERS.append(parser)
    logger.debug("registered parser: %s (priority=%d)", parser.name, parser.priority)


def set_llm_fallback(fn: Callable[[bytes], list[dict]]) -> None:
    """Wire up LLM fallback. Function: bytes -> list[record]. Optional."""
    global _LLM_FALLBACK
    _LLM_FALLBACK = fn


def list_parsers() -> list[Parser]:
    """Return parsers ordered by priority (desc), enabled first."""
    return sorted(
        [p for p in _PARSERS if p.enabled],
        key=lambda p: -p.priority,
    )


# ── Plugin discovery ─────────────────────────────────────────────────────────

def discover_builtin() -> None:
    """Import built-in parser modules and register their Parser subclasses."""
    import inspect

    for mod_name in ("enterprise_data", "commerceml"):
        try:
            mod = importlib.import_module(f"core.parsers.{mod_name}")
        except ImportError as e:
            logger.warning("could not import built-in parser %s: %s", mod_name, e)
            continue

        # Look for any Parser subclass in this module
        for attr_name in dir(mod):
            attr = getattr(mod, attr_name)
            if (inspect.isclass(attr)
                    and issubclass(attr, Parser)
                    and attr is not Parser
                    and attr.__module__ == mod.__name__):
                try:
                    register(attr())
                except Exception as e:
                    logger.warning("failed to instantiate %s: %s", attr.__name__, e)


def discover_learned(parsers_dir: Path) -> None:
    """
    Load all .yaml parsers from parsers_dir/learned/.
    Each .yaml is interpreted by yaml_engine.YamlParser.
    """
    learned_dir = parsers_dir / "learned"
    if not learned_dir.exists():
        return
    try:
        from .yaml_engine import YamlParser
    except ImportError:
        logger.warning("yaml_engine not available — skipping YAML parsers")
        return
    for yaml_file in sorted(learned_dir.glob("*.yaml")):
        try:
            p = YamlParser.from_file(yaml_file)
            register(p)
        except Exception as e:
            logger.error("failed to load %s: %s", yaml_file.name, e)


def discover_shadow(parsers_dir: Path, eval_callback=None) -> list[Parser]:
    """
    Load shadow parsers from parsers_dir/shadow/.
    They are NOT registered for normal routing. Returned separately
    so the autoparser pipeline can run them in shadow mode.
    """
    shadow_dir = parsers_dir / "shadow"
    if not shadow_dir.exists():
        return []
    try:
        from .yaml_engine import YamlParser
    except ImportError:
        return []
    out = []
    for yaml_file in sorted(shadow_dir.glob("*.yaml")):
        try:
            p = YamlParser.from_file(yaml_file)
            p.priority = -10  # shadow parsers never win the route
            out.append(p)
        except Exception as e:
            logger.error("failed shadow load %s: %s", yaml_file.name, e)
    return out


# ── Main routing entry point ─────────────────────────────────────────────────

def route_and_parse(
    raw: bytes,
    filename: str | None = None,
    use_llm: bool = True,
) -> tuple[list[dict], str, float]:
    """
    Try each registered parser by priority. First one that
    can_parse() AND returns non-empty validated records wins.

    Returns (records, parser_name, elapsed_seconds).

    If no built-in/learned parser handles the file:
      - use_llm=True  → falls back to LLM (slow, ~20s on ARM)
      - use_llm=False → raises ValueError

    The caller should log the result and, if parser_name == "LLM",
    feed the (raw, records) pair into the autoparser pipeline so
    the system can learn this format.
    """
    if isinstance(raw, str):
        raw = raw.encode("utf-8")

    t0 = time.time()
    last_error = None

    for parser in list_parsers():
        try:
            if not parser.can_parse(raw, filename):
                continue
        except Exception as e:
            logger.debug("can_parse(%s) raised: %s", parser.name, e)
            continue

        try:
            records = parser.parse(raw)
        except Exception as e:
            last_error = e
            logger.debug("parse(%s) raised: %s", parser.name, e)
            continue

        ok, errors = validate_records(records)
        if not ok:
            last_error = ValueError(f"{parser.name}: {errors[0] if errors else 'invalid output'}")
            logger.debug("validate failed for %s: %d errors", parser.name, len(errors))
            continue

        if not records:
            continue  # empty result — try next parser

        elapsed = time.time() - t0
        logger.info("%s parsed %d records in %.3fs", parser.name, len(records), elapsed)
        return records, parser.name, elapsed

    # LLM fallback
    if use_llm and _LLM_FALLBACK is not None:
        logger.info("no parser matched — falling back to LLM")
        try:
            records = _LLM_FALLBACK(raw)
            elapsed = time.time() - t0
            return records, "LLM", elapsed
        except Exception as e:
            raise ValueError(f"LLM fallback failed: {e}") from e

    if last_error:
        raise ValueError(f"no parser matched and LLM disabled: {last_error}")
    raise ValueError("no parser matched and LLM disabled")


# Auto-discover on import
discover_builtin()
