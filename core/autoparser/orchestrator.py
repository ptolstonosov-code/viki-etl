"""
The orchestrator wires all the autoparser stages together.

Two entry points:

  ingest_file(raw, filename)
    Called by the public-facing API whenever a file arrives.
    - Tries known parsers (built-in + learned). If one wins → returns its records.
    - Otherwise: adds to quarantine, calls LLM, also returns records.
    - When enough samples accumulate → triggers learning.

  tick()
    Background maintenance. Should be called once a minute.
    - Checks each fingerprint bucket: should we start labeling?
    - Runs shadow evaluation on candidates.
    - Promotes parsers that pass.

The orchestrator is created once per process (singleton-ish) and lives as
long as the ARM api service does.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path

from core.parsers import (
    list_parsers,
    register,
    route_and_parse,
)

from .llm_service import LLMService
from .sample_collector import SampleCollector, fingerprint
from .shadow_evaluator import ShadowEvaluator, promote, reject
from .structure_analyzer import detect_source_kind
from .yaml_generator import generate_yaml_parser, write_yaml

logger = logging.getLogger(__name__)

# ── LLM-side parser using llama-server ───────────────────────────────────────

def _llm_parse_factory(llm: LLMService, system_prompt: str):
    """Builds a callable (raw_bytes) → records using the LLM service."""
    def _parse(raw: bytes) -> list[dict]:
        user_msg = "Parse the following data:\n\n<data>\n" + raw.decode("utf-8", errors="replace") + "\n</data>"
        content = llm.chat(system_prompt, user_msg, max_tokens=2048)
        # Strip code fences
        content = re.sub(r"```(?:json)?\s*", "", content).strip().rstrip("`").strip()
        i, j = content.find("["), content.rfind("]")
        if i == -1 or j == -1:
            return []
        try:
            arr = json.loads(content[i:j + 1])
            return [r for r in arr if isinstance(r, dict) and "table" in r and "data" in r]
        except json.JSONDecodeError:
            return []
    return _parse


# ── Orchestrator ─────────────────────────────────────────────────────────────

class AutoparserOrchestrator:
    """
    Singleton coordinating sample collection, LLM labeling, parser
    generation, shadow evaluation, and promotion.
    """

    def __init__(
        self,
        data_dir: Path,
        parsers_dir: Path,
        llm: LLMService,
        system_prompt: str,
        min_samples_to_label: int = 5,
        shadow_min_samples: int = 20,
    ):
        self.data_dir = Path(data_dir)
        self.parsers_dir = Path(parsers_dir)
        self.llm = llm
        self.system_prompt = system_prompt
        self.collector = SampleCollector(self.data_dir / "samples")
        self.shadow_dir = self.parsers_dir / "shadow"
        self.learned_dir = self.parsers_dir / "learned"
        self.shadow_dir.mkdir(parents=True, exist_ok=True)
        self.learned_dir.mkdir(parents=True, exist_ok=True)
        self.min_samples_to_label = min_samples_to_label
        self.shadow_min_samples = shadow_min_samples
        self._lock = threading.Lock()
        self._llm_parse = _llm_parse_factory(llm, system_prompt)

    # ── Main public API ──────────────────────────────────────────────────────

    def ingest_file(self, raw: bytes, filename: str | None = None) -> dict:
        """
        Parse a file. Always returns:
            {
              "records": [...],
              "parser": "<name>",
              "fingerprint": "...",
              "quarantined": True/False,
              "note": optional,
            }
        """
        # Try known parsers first
        try:
            records, parser_name, elapsed = route_and_parse(
                raw, filename=filename, use_llm=False,
            )
            return {
                "records": records,
                "parser": parser_name,
                "fingerprint": None,
                "quarantined": False,
                "elapsed_ms": elapsed * 1000,
            }
        except ValueError:
            pass  # nobody matched → fallback path

        # Unknown format — quarantine + LLM
        fp = self.collector.add(raw, filename)
        logger.info("unknown format %s (sample saved), invoking LLM", fp)

        try:
            records = self._llm_parse(raw)
        except Exception as e:
            logger.error("LLM call failed: %s", e)
            return {
                "records": [],
                "parser": None,
                "fingerprint": fp,
                "quarantined": True,
                "note": f"LLM failed: {e}",
            }

        # Save LLM's interpretation alongside the sample
        bucket_samples = sorted((self.collector.root / fp).glob("sample_*.dat"))
        if bucket_samples:
            self.collector.save_llm_records(fp, len(bucket_samples), records)

        return {
            "records": records,
            "parser": "LLM",
            "fingerprint": fp,
            "quarantined": True,
            "elapsed_ms": None,
        }

    # ── Background maintenance ───────────────────────────────────────────────

    def tick(self) -> dict:
        """
        Run one round of maintenance. Returns summary of actions taken.
        """
        with self._lock:
            actions = {"labeling_started": [], "promoted": [], "rejected": []}

            for meta in self.collector.list_buckets():
                fp = meta["fingerprint"]
                status = meta.get("status", "collecting")

                if status == "collecting" and self.collector.is_ready_for_labeling(fp, self.min_samples_to_label):
                    self._generate_and_shadow(fp)
                    actions["labeling_started"].append(fp)
                elif status == "shadow":
                    if self._evaluate_shadow(fp):
                        actions["promoted"].append(fp)
                    elif self._should_reject(fp):
                        actions["rejected"].append(fp)

            return actions

    # ── Internal stages ──────────────────────────────────────────────────────

    def _generate_and_shadow(self, fp: str) -> None:
        """Label all samples with LLM, generate YAML, save to shadow/."""
        samples = self.collector.load_samples(fp)
        logger.info("labeling %d samples for fingerprint %s", len(samples), fp)

        # Label any samples that don't have LLM records yet
        existing = self.collector.load_llm_records(fp)
        for idx, raw in enumerate(samples):
            if idx >= len(existing):
                try:
                    records = self._llm_parse(raw)
                    self.collector.save_llm_records(fp, idx + 1, records)
                    existing.append(records)
                except Exception as e:
                    logger.warning("LLM label failed for sample %d: %s", idx, e)
                    existing.append([])

        # Detect format signature from common content
        head = samples[0][:200].decode("utf-8", errors="ignore").lstrip()
        first_marker = head.split("\n", 1)[0][:80].strip()
        signature = first_marker if first_marker else None

        # Generate YAML
        parser_name = f"auto_{fp}"
        spec = generate_yaml_parser(
            samples=samples,
            llm_records=existing,
            parser_name=parser_name,
            detect_signature=signature,
        )
        if not spec or not spec.get("records"):
            logger.warning("autogen produced empty spec for %s — leaving collecting", fp)
            return

        # Save as shadow parser
        yaml_path = self.shadow_dir / f"{parser_name}.yaml"
        write_yaml(spec, yaml_path)
        self.collector.update_meta(fp, status="shadow",
                                   parser_path=str(yaml_path))
        logger.info("created shadow parser %s", yaml_path.name)

    def _evaluate_shadow(self, fp: str) -> bool:
        """
        Evaluate the candidate parser against more samples.
        Returns True if promoted.
        """
        meta = self.collector.get_meta(fp) or {}
        parser_path = Path(meta.get("parser_path", ""))
        if not parser_path.exists():
            return False
        from core.parsers.yaml_engine import YamlParser
        try:
            candidate = YamlParser.from_file(parser_path)
        except Exception as e:
            logger.error("can't load candidate %s: %s", parser_path, e)
            return False

        # Test against ALL collected samples (LLM-records are gold standard)
        samples = self.collector.load_samples(fp)
        llm_records = self.collector.load_llm_records(fp)

        evaluator = ShadowEvaluator(candidate, self._llm_parse)
        for raw, gold in zip(samples, llm_records):
            evaluator.evaluate_one(raw, gold)

        if evaluator.should_promote(
            min_samples=min(self.shadow_min_samples, len(samples)),
            f1_threshold=0.85,    # be slightly lenient — LLM itself is noisy
            match_ratio=0.80,
        ):
            promoted_path = promote(parser_path, self.learned_dir)
            # Register the new parser in the live registry
            new_parser = YamlParser.from_file(promoted_path)
            register(new_parser)
            self.collector.update_meta(fp, status="promoted",
                                       promoted_path=str(promoted_path))
            logger.info("PROMOTED parser %s", promoted_path.name)
            return True

        # Not yet — log progress
        summary = evaluator.summary()
        self.collector.update_meta(
            fp,
            shadow_avg_f1=summary.avg_f1,
            shadow_match_count=summary.matches,
            shadow_total=summary.samples_tested,
        )
        return False

    def _should_reject(self, fp: str) -> bool:
        """Reject parsers that stay at low F1 for too many evaluations."""
        meta = self.collector.get_meta(fp) or {}
        n = meta.get("shadow_total", 0)
        avg = meta.get("shadow_avg_f1", 0)
        # If after 50 evaluations F1 is still < 0.5 — give up
        if n >= 50 and avg < 0.5:
            parser_path = Path(meta.get("parser_path", ""))
            reject(parser_path, f"avg_f1={avg:.2f} after {n} evals")
            self.collector.update_meta(fp, status="abandoned")
            return True
        return False
