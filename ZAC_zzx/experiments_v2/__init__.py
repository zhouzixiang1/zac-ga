"""Schema-2 experiment infrastructure for the four-method ZAC study.

The package deliberately does not read any legacy ``*_fidelity.json`` files.
Every public loader validates ``experiment_schema == 2`` before returning.
"""

from evaluation import (FidelityResult, normalize_na, normalize_zair,
                        score_trace)

from .contracts import (
    CanonicalCircuitManifest,
    RunManifest,
    RunStatus,
    load_run_manifest,
)
from .canonicalize import canonicalize_circuit, canonicalize_suite
from .ablation_statistics import aggregate_ablation
from .runner import AttemptSpec, run_attempt
from .statistics import aggregate_experiment

__all__ = [
    "AttemptSpec",
    "CanonicalCircuitManifest",
    "RunManifest",
    "RunStatus",
    "aggregate_ablation",
    "aggregate_experiment",
    "canonicalize_circuit",
    "canonicalize_suite",
    "load_run_manifest",
    "normalize_na",
    "normalize_zair",
    "run_attempt",
    "score_trace",
    "FidelityResult",
]
