"""
ShadowEvaluator — verifies a freshly-generated YAML parser by running it
side-by-side with the LLM on N samples and comparing results.

If they match in ≥ THRESHOLD samples → promote the parser to production.
Otherwise → keep collecting samples or mark as needing manual review.
"""

from __future__ import annotations

import json
import logging
import shutil
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class ShadowResult:
    samples_tested: int
    matches: int      # both parser and LLM agree
    parser_only: int  # parser produced records, LLM did not
    llm_only: int     # LLM produced records, parser did not
    mismatches: int   # both produced but disagree on data
    avg_f1: float


def _records_to_key_set(records: list[dict]) -> set:
    """Hashable representation of records: set of (table, sorted-key-values)."""
    s = set()
    for r in records:
        if not isinstance(r, dict):
            continue
        table = r.get("table", "")
        data = r.get("data") or {}
        # Match on the real source-derived business data: product name, barcode,
        # article, variant display-name. ALL internal ids (id / variant_id /
        # product_id) are synthetic UUIDs the LLM mints inconsistently per-run —
        # on some samples it copies the source id, on others it invents one — so a
        # deterministic parser cannot reproduce them and scoring against them would
        # unfairly fail a correct parser. Identity = business values; fall back to
        # `id` only when a record carries no business value of its own.
        business_keys = ("name", "barcode", "article", "display_name")
        sig = tuple(sorted(
            (k, json.dumps(v, ensure_ascii=False))
            for k, v in data.items()
            if k in business_keys and v is not None
        ))
        if not sig and data.get("id") is not None:
            sig = (("id", json.dumps(data["id"], ensure_ascii=False)),)
        s.add((table, sig))
    return s


def _f1(parser_recs: list[dict], llm_recs: list[dict]) -> float:
    p_set = _records_to_key_set(parser_recs)
    l_set = _records_to_key_set(llm_recs)
    if not p_set and not l_set:
        return 1.0
    if not p_set or not l_set:
        return 0.0
    tp = len(p_set & l_set)
    precision = tp / len(p_set) if p_set else 0
    recall = tp / len(l_set) if l_set else 0
    return 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0


class ShadowEvaluator:
    """
    Evaluate a candidate YAML parser against LLM "gold standard".
    """

    def __init__(self, candidate_parser, llm_callable):
        """
        candidate_parser: a YamlParser instance to test
        llm_callable:     function (raw_bytes) → list[record] (the LLM)
        """
        self.parser = candidate_parser
        self.llm = llm_callable
        self.history: list[float] = []  # per-sample F1 scores

    def evaluate_one(self, raw: bytes, llm_records: list[dict] | None = None) -> float:
        """
        Run parser on raw bytes; compare against llm_records (or call LLM if None).
        Returns F1 score for this sample.
        """
        try:
            parser_records = self.parser.parse(raw)
        except Exception as e:
            logger.warning("candidate parser raised: %s", e)
            parser_records = []
        if llm_records is None:
            try:
                llm_records = self.llm(raw)
            except Exception as e:
                logger.warning("LLM call failed: %s", e)
                return 0.0
        f1 = _f1(parser_records, llm_records)
        self.history.append(f1)
        return f1

    def summary(self, threshold: float = 0.95) -> ShadowResult:
        n = len(self.history)
        if n == 0:
            return ShadowResult(0, 0, 0, 0, 0, 0.0)
        matches = sum(1 for f in self.history if f >= threshold)
        mismatches = sum(1 for f in self.history if 0 < f < threshold)
        # parser_only / llm_only would require keeping more state — skip for now
        avg = sum(self.history) / n
        return ShadowResult(
            samples_tested=n,
            matches=matches,
            parser_only=0,
            llm_only=0,
            mismatches=mismatches,
            avg_f1=avg,
        )

    def should_promote(
        self,
        min_samples: int = 20,
        f1_threshold: float = 0.95,
        match_ratio: float = 0.90,
    ) -> bool:
        """
        Decide if the candidate is good enough to graduate to production.
        Default: at least 20 samples evaluated, average F1 ≥ 0.95,
                 and ≥ 90% of samples individually scored ≥ 0.95.
        """
        s = self.summary(threshold=f1_threshold)
        if s.samples_tested < min_samples:
            return False
        if s.avg_f1 < f1_threshold:
            return False
        if (s.matches / s.samples_tested) < match_ratio:
            return False
        return True


def promote(shadow_path: Path, learned_dir: Path) -> Path:
    """
    Move a shadow YAML to the learned/ directory. Bumps its priority so
    it actually wins routing decisions next time the registry reloads.
    """
    import yaml

    learned_dir.mkdir(parents=True, exist_ok=True)
    target = learned_dir / shadow_path.name

    spec = yaml.safe_load(shadow_path.read_text(encoding="utf-8"))
    spec["priority"] = max(int(spec.get("priority", 30)), 70)
    target.write_text(yaml.safe_dump(spec, allow_unicode=True, sort_keys=False), encoding="utf-8")
    shadow_path.unlink()
    return target


def reject(shadow_path: Path, reason: str = "") -> None:
    """Remove a failed shadow parser. Records reason in sibling .reject file."""
    if shadow_path.exists():
        shadow_path.with_suffix(".reject.txt").write_text(reason, encoding="utf-8")
        shadow_path.unlink()
