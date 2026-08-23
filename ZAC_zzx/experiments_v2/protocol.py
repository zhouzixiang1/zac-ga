"""Method-level policy that separates paper baselines from our correctness gate."""

from __future__ import annotations


PAPER_NATIVE_GHOST_POLICY = "paper_native_record_only"
STRICT_GHOST_POLICY = "strict_zero"
PAPER_NATIVE_PHYSICALIZATION = "paper_native_unmodified"
OURS_PHYSICALIZATION = "in_method_ghost_safe"
BASELINE_METHODS = frozenset(("M1", "M2"))
OURS_METHODS = frozenset(("M3", "M4"))
TRACE_PROTOCOLS = {
    "M1": "zac_paper_original_raw_v1",
    "M2": "iccad_qmap_3_2_paper_original_raw_v1",
    "M3": "ours_nl_ghost_safe_v2",
    "M4": "ours_lk_ghost_safe_v2",
}


def ghost_policy_for_method(method: str) -> str:
    """Return the only registered ghost policy for a formal method."""
    if method in BASELINE_METHODS:
        return PAPER_NATIVE_GHOST_POLICY
    if method in OURS_METHODS:
        return STRICT_GHOST_POLICY
    raise ValueError(f"unknown formal method: {method}")


def enforces_ghost_safety(method: str) -> bool:
    return ghost_policy_for_method(method) == STRICT_GHOST_POLICY


def physicalization_policy_for_method(method: str) -> str:
    if method in BASELINE_METHODS:
        return PAPER_NATIVE_PHYSICALIZATION
    if method in OURS_METHODS:
        return OURS_PHYSICALIZATION
    raise ValueError(f"unknown formal method: {method}")


def trace_protocol_for_method(method: str) -> str:
    try:
        return TRACE_PROTOCOLS[method]
    except KeyError as error:
        raise ValueError(f"unknown formal method: {method}") from error


__all__ = [
    "BASELINE_METHODS",
    "OURS_METHODS",
    "PAPER_NATIVE_GHOST_POLICY",
    "PAPER_NATIVE_PHYSICALIZATION",
    "STRICT_GHOST_POLICY",
    "TRACE_PROTOCOLS",
    "OURS_PHYSICALIZATION",
    "enforces_ghost_safety",
    "ghost_policy_for_method",
    "physicalization_policy_for_method",
    "trace_protocol_for_method",
]
