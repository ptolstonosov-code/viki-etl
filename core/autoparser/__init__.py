"""
Autoparser pipeline — turns unknown files into a learned YAML parser on the device.

Pipeline stages (all run on the ARM device, fully autonomous):

    [unknown file] → SAMPLE COLLECTOR → data/samples/<fingerprint>/*.dat
                                              │
                                              ↓ (≥ N samples collected)
                          STRUCTURE ANALYZER  ← classify as XML/JSON/CSV
                                              ↓
                          LLM LABELER (start/stop llama-server)
                                              ↓
                          YAML GENERATOR      → parsers/shadow/<name>.yaml
                                              ↓
                          SHADOW EVALUATOR    ← parse new files in parallel,
                                                compare with LLM
                                              ↓ (≥ 95% match over 20 files)
                          PROMOTE             → parsers/learned/<name>.yaml
                                                (registered, parser becomes active)

LLM is loaded only when needed and unloaded when the pipeline finishes.
"""

from .sample_collector import SampleCollector, fingerprint
from .structure_analyzer import detect_source_kind
from .llm_service import LLMService
from .yaml_generator import generate_yaml_parser
from .shadow_evaluator import ShadowEvaluator

__all__ = [
    "SampleCollector",
    "fingerprint",
    "detect_source_kind",
    "LLMService",
    "generate_yaml_parser",
    "ShadowEvaluator",
]
