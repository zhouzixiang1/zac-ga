"""Unified physical evaluation API for the four-method experiment."""

from .adapters import normalize_na, normalize_zair
from .model import (
    CanonicalTraceEvent,
    EventType,
    FidelityModel,
    FidelityResult,
    TraceValidationError,
    UnsupportedOperationError,
    fixed_duration_matches,
)
from .scorer import score_trace
from .validator import validate_trace_physics

__all__ = [
    "CanonicalTraceEvent",
    "EventType",
    "FidelityModel",
    "FidelityResult",
    "fixed_duration_matches",
    "TraceValidationError",
    "UnsupportedOperationError",
    "normalize_na",
    "normalize_zair",
    "score_trace", "validate_trace_physics",
]
