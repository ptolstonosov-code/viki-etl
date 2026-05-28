"""
Data parser — takes raw bytes or text, detects format, and calls LLM to extract records.
Supports: text/CSV/pipe-delimited, JSON, XML, binary (hex preview), mixed.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Generator

from .llm_client import LLMClient


# Maximum characters sent to the LLM per chunk (fits within 8k context)
CHUNK_MAX_CHARS = 6000


class DataParser:
    def __init__(self, llm_client: LLMClient | None = None):
        self.llm = llm_client or LLMClient()

    # ── Main entry point ──────────────────────────────────────────────────────

    def parse_file(self, path: str | Path) -> list[dict]:
        """Parse a file and return all extracted records."""
        path = Path(path)
        raw = path.read_bytes()
        return self.parse_bytes(raw, filename=path.name)

    def parse_text(self, text: str) -> list[dict]:
        """Parse a string (already decoded)."""
        records: list[dict] = []
        for chunk in self._split_into_chunks(text):
            records.extend(self.llm.parse_to_records(chunk))
        return records

    def parse_bytes(self, data: bytes, filename: str = "") -> list[dict]:
        """
        Detect encoding / format and parse.
        Binary files are sent as hex preview + ascii side-channel.
        """
        fmt = self._detect_format(data, filename)
        text = self._decode(data, fmt)
        return self.parse_text(text)

    # ── Format detection ──────────────────────────────────────────────────────

    def _detect_format(self, data: bytes, filename: str) -> str:
        ext = Path(filename).suffix.lower()
        if ext in (".json",):
            return "json"
        if ext in (".xml",):
            return "xml"
        if ext in (".csv",):
            return "csv"
        if ext in (".bin", ".dat", ".edi", ".x12", ".edifact"):
            return "binary"
        # Heuristic: try UTF-8 decode; if it fails → binary
        try:
            text = data.decode("utf-8")
            stripped = text.strip()
            if stripped.startswith("{") or stripped.startswith("["):
                return "json"
            if stripped.startswith("<"):
                return "xml"
            return "text"
        except UnicodeDecodeError:
            return "binary"

    def _decode(self, data: bytes, fmt: str) -> str:
        if fmt == "binary":
            return self._binary_to_readable(data)
        for enc in ("utf-8", "utf-8-sig", "cp1251", "latin-1"):
            try:
                return data.decode(enc)
            except UnicodeDecodeError:
                continue
        return data.decode("latin-1", errors="replace")

    @staticmethod
    def _binary_to_readable(data: bytes, max_bytes: int = 3000) -> str:
        """
        Render binary as hex+ASCII dump (similar to xxd).
        LLMs can often pattern-match fixed-width binary records from this.
        """
        lines = []
        chunk = data[:max_bytes]
        for i in range(0, len(chunk), 16):
            row = chunk[i : i + 16]
            hex_part = " ".join(f"{b:02x}" for b in row)
            ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
            lines.append(f"{i:06x}  {hex_part:<48}  |{ascii_part}|")
        note = ""
        if len(data) > max_bytes:
            note = f"\n... ({len(data) - max_bytes} more bytes truncated)"
        return "[Binary/hex dump]\n" + "\n".join(lines) + note

    # ── Chunking ──────────────────────────────────────────────────────────────

    def _split_into_chunks(self, text: str) -> Generator[str, None, None]:
        """
        Split large inputs into overlapping chunks so the LLM
        doesn't miss records that span chunk boundaries.
        """
        if len(text) <= CHUNK_MAX_CHARS:
            yield text
            return

        # Try to split on natural boundaries (newlines)
        lines = text.splitlines(keepends=True)
        chunk: list[str] = []
        size = 0
        for line in lines:
            if size + len(line) > CHUNK_MAX_CHARS and chunk:
                yield "".join(chunk)
                # Overlap: keep last 5 lines for context
                chunk = chunk[-5:]
                size = sum(len(l) for l in chunk)
            chunk.append(line)
            size += len(line)
        if chunk:
            yield "".join(chunk)
