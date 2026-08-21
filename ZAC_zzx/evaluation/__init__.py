"""Unified physical evaluation API for the four-method experiment."""

from .adapters import normalize_na, normalize_zair
from .model import (
    CanonicalTraceEvent,
    EventType,
    FidelityModel,
    FidelityResult,
    TraceValidationError,
    UnsupportedOperationError,
)
from .scorer import score_trace
from .validator import validate_trace_physics

__all__ = [
    "CanonicalTraceEvent",
    "EventType",
    "FidelityModel",
    "FidelityResult",
    "TraceValidationError",
    "UnsupportedOperationError",
    "normalize_na",
    "normalize_zair",
    "score_trace", "validate_trace_physics",
]
