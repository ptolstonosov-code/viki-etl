"""
SampleCollector — saves unknown-format files in a quarantine directory,
grouped by a structural fingerprint.

Fingerprint logic:
  - XML: root tag (with namespace stripped) + first 3 child tag names
  - JSON: top-level keys (sorted, first 5)
  - CSV: header row hash
  - other: file extension + first 64 bytes hash

Two files share a fingerprint only if their *structure* matches. Same
fingerprint → likely same format → same future parser.

Directory layout:
    {data_dir}/samples/
        <fingerprint>/
            meta.json          ← stats, source_kind, first_seen, count
            sample_0001.dat    ← raw bytes
            sample_0002.dat
            ...
            llm_records_0001.json  ← LLM-extracted records (after labeling)
            ...
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path


# ── Fingerprint computation ──────────────────────────────────────────────────

def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def fingerprint(raw: bytes, filename: str | None = None) -> str:
    """
    Compute a stable short fingerprint of the file's structure.
    Two files with the same fingerprint should be parseable by the same parser.
    """
    head = raw[:1024].lstrip()

    # ── XML ──
    if head.startswith(b"<?xml") or head.startswith(b"<"):
        try:
            root = ET.fromstring(raw)
            root_tag = _localname(root.tag)
            child_tags = [_localname(c.tag) for c in list(root)[:3]]
            structural = f"xml:{root_tag}:{':'.join(child_tags)}"
            return _short_hash(structural)
        except ET.ParseError:
            pass

    # ── JSON ──
    if head.startswith(b"{") or head.startswith(b"["):
        try:
            obj = json.loads(raw.decode("utf-8", errors="replace"))
            if isinstance(obj, dict):
                keys = sorted(obj.keys())[:5]
                structural = "json:obj:" + ":".join(keys)
            elif isinstance(obj, list) and obj:
                first = obj[0]
                if isinstance(first, dict):
                    keys = sorted(first.keys())[:5]
                    structural = "json:arr-of-obj:" + ":".join(keys)
                else:
                    structural = "json:arr:" + type(first).__name__
            else:
                structural = "json:empty"
            return _short_hash(structural)
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass

    # ── CSV (heuristic: first line has delimiters, no XML/JSON markers) ──
    try:
        first_line = head.decode("utf-8", errors="ignore").split("\n", 1)[0]
        # Heuristic: 3+ commas or semicolons in first line, mostly printable
        if (first_line.count(",") >= 3 or first_line.count(";") >= 3) and \
           "<" not in first_line and "{" not in first_line:
            structural = "csv:" + first_line.strip()[:200]
            return _short_hash(structural)
    except Exception:
        pass

    # ── Fallback: extension + content hash ──
    ext = (Path(filename).suffix if filename else "") or "unknown"
    return _short_hash(f"other:{ext}:{hashlib.sha256(head).hexdigest()[:16]}")


def _short_hash(s: str) -> str:
    """Compact 12-char hex hash."""
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:12]


# ── SampleCollector ──────────────────────────────────────────────────────────

class SampleCollector:
    """
    Manages a quarantine directory of unknown-format samples.

    Usage:
        sc = SampleCollector(Path("/opt/llm-etl/data/samples"))
        fp = sc.add(raw_bytes, filename="weird.xml")
        if sc.is_ready_for_labeling(fp, min_samples=5):
            samples = sc.load_samples(fp)
            # ... feed to LLM, generate YAML, etc.
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def add(self, raw: bytes, filename: str | None = None) -> str:
        """
        Save a sample under its fingerprint directory.
        Returns the fingerprint id.
        """
        fp = fingerprint(raw, filename)
        bucket = self.root / fp
        bucket.mkdir(parents=True, exist_ok=True)

        # Load or init meta
        meta_path = bucket / "meta.json"
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        else:
            meta = {
                "fingerprint": fp,
                "first_seen": time.time(),
                "count": 0,
                "last_seen": 0,
                "filenames": [],
                "status": "collecting",  # collecting → labeled → shadow → promoted | abandoned
            }
        meta["count"] = meta.get("count", 0) + 1
        meta["last_seen"] = time.time()
        if filename and filename not in meta.get("filenames", []):
            meta["filenames"] = (meta.get("filenames") or [])[:20] + [filename]

        # Save sample (cap at MAX_SAMPLES per fingerprint to avoid disk filling)
        MAX_SAMPLES = 20
        existing = sorted(bucket.glob("sample_*.dat"))
        if len(existing) < MAX_SAMPLES:
            idx = len(existing) + 1
            sample_path = bucket / f"sample_{idx:04d}.dat"
            sample_path.write_bytes(raw)

        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return fp

    def is_ready_for_labeling(self, fingerprint_id: str, min_samples: int = 5) -> bool:
        """Has enough samples accumulated to start LLM labeling?"""
        meta = self.get_meta(fingerprint_id)
        if not meta:
            return False
        if meta.get("status") not in (None, "collecting"):
            return False
        n_samples = len(list((self.root / fingerprint_id).glob("sample_*.dat")))
        return n_samples >= min_samples

    def list_buckets(self, status: str | None = None) -> list[dict]:
        """List all fingerprint buckets, optionally filtered by status."""
        out = []
        for d in sorted(self.root.iterdir()):
            if not d.is_dir():
                continue
            meta = self.get_meta(d.name)
            if meta is None:
                continue
            if status is not None and meta.get("status") != status:
                continue
            out.append(meta)
        return out

    def get_meta(self, fingerprint_id: str) -> dict | None:
        meta_path = self.root / fingerprint_id / "meta.json"
        if not meta_path.exists():
            return None
        return json.loads(meta_path.read_text(encoding="utf-8"))

    def update_meta(self, fingerprint_id: str, **kwargs) -> None:
        meta = self.get_meta(fingerprint_id) or {}
        meta.update(kwargs)
        (self.root / fingerprint_id / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def load_samples(self, fingerprint_id: str) -> list[bytes]:
        bucket = self.root / fingerprint_id
        if not bucket.exists():
            return []
        return [p.read_bytes() for p in sorted(bucket.glob("sample_*.dat"))]

    def save_llm_records(self, fingerprint_id: str, sample_idx: int, records: list[dict]) -> None:
        bucket = self.root / fingerprint_id
        path = bucket / f"llm_records_{sample_idx:04d}.json"
        path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")

    def load_llm_records(self, fingerprint_id: str) -> list[list[dict]]:
        bucket = self.root / fingerprint_id
        if not bucket.exists():
            return []
        return [json.loads(p.read_text(encoding="utf-8"))
                for p in sorted(bucket.glob("llm_records_*.json"))]

    def abandon(self, fingerprint_id: str, reason: str = "") -> None:
        """Mark this fingerprint as not parseable — won't try again."""
        self.update_meta(fingerprint_id, status="abandoned", abandon_reason=reason)
