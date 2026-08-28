"""Registered, fail-closed controls for the Schema-2 ablation track.

The main M3/M4 configuration files remain immutable and continue to differ only
in their method identity and look-ahead horizon.  Ablation controls live in a
separate wrapper that is accepted only by ``run_kind=ablation`` attempts.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from zzx.algorithm_v2 import (maximum_lookahead_horizon,
                              validate_schema2_setting)

from .contracts import CanonicalCircuitManifest, stable_sha256


ABLATION_PROTOCOL_VERSION = 1
PAPER_ABLATION_PROTOCOL_VERSION = 2
QMAP_STRATA = (
    ("le_300", 0, 300),
    ("301_1500", 301, 1500),
    ("gt_1500", 1501, None),
)


@dataclass(frozen=True)
class AblationVariant:
    name: str
    base_method: str
    lookahead_horizon: int
    decision_policy: str
    fitness_phase_mode: str
    routing_batcher: str

    def controls(self) -> dict[str, Any]:
        return {
            "lookahead_horizon": self.lookahead_horizon,
            "decision_policy": self.decision_policy,
            "fitness_phase_mode": self.fitness_phase_mode,
            "routing_batcher": self.routing_batcher,
        }


@dataclass(frozen=True)
class PaperAblationVariant:
    """One registered paper-only ABI9 search or sensitivity control."""

    name: str
    base_method: str
    lookahead_horizon: int
    search_policy: str
    protocol_version: int = PAPER_ABLATION_PROTOCOL_VERSION

    def controls(self) -> dict[str, Any]:
        return {
            "lookahead_horizon": self.lookahead_horizon,
            "decision_policy": "optimize",
            "fitness_phase_mode": "phase",
            "routing_batcher": "coloring",
            "search_policy": self.search_policy,
        }


# ``decay_phase_coloring`` is the complete formal M4 reference.  Its registered
# maximum is eight and the shared geometric cutoff determines the effective
# depth; no formal ablation revives the legacy adaptive H=0/1/2 selector.
ABLATION_VARIANTS: Mapping[str, AblationVariant] = {
    item.name: item for item in (
        AblationVariant("h0", "M3", 0, "optimize", "phase", "coloring"),
        AblationVariant(
            "decay_phase_coloring", "M4", 8, "optimize", "phase", "coloring"),
        AblationVariant(
            "always_stay", "M4", 8, "always_stay", "phase", "coloring"),
        AblationVariant(
            "always_return", "M4", 8, "always_return", "phase", "coloring"),
        AblationVariant(
            "adjacent_only", "M4", 8, "adjacent_only", "phase", "coloring"),
        AblationVariant(
            "lumped_greedy", "M4", 8, "optimize", "lumped_greedy", "greedy"),
    )
}


PAPER_SENSITIVITY_HORIZONS: Mapping[str, int] = {
    "default": 8,
    "budget_192": 8,
    "budget_1152": 8,
    "return_4_2": 8,
    "return_10_8": 8,
    "horizon_2": 2,
    "horizon_4": 4,
    "decay_0p2_0p5": 8,
    "decay_0p35_0p6": 8,
}


PAPER_ABLATION_VARIANTS: Mapping[str, PaperAblationVariant] = {
    item.name: item for item in (
        PaperAblationVariant("paper_h0_ga", "M3", 0, "ga"),
        PaperAblationVariant("paper_h8_ga", "M4", 8, "ga"),
        PaperAblationVariant(
            "paper_h8_greedy_only", "M4", 8, "greedy_only"),
        *(PaperAblationVariant(
            f"paper_sensitivity_{profile}", "M4", horizon, "ga")
          for profile, horizon in PAPER_SENSITIVITY_HORIZONS.items()),
    )
}


def registered_variant(name: str) -> AblationVariant:
    try:
        return ABLATION_VARIANTS[name]
    except KeyError as error:
        raise ValueError(f"unknown ablation variant: {name!r}") from error


def registered_paper_variant(name: str) -> PaperAblationVariant:
    try:
        return PAPER_ABLATION_VARIANTS[name]
    except KeyError as error:
        raise ValueError(f"unknown paper ablation variant: {name!r}") from error


def _effective_setting(payload: Mapping[str, Any]) -> dict[str, Any]:
    if "zac_setting" not in payload:
        return dict(payload)
    settings = payload["zac_setting"]
    if (not isinstance(settings, list) or len(settings) != 1 or
            not isinstance(settings[0], Mapping)):
        raise ValueError("ablation base config must contain one zac_setting")
    return dict(settings[0])


def build_ablation_config(variant_name: str,
                          base_payload: Mapping[str, Any]) -> dict[str, Any]:
    """Wrap one frozen M3/M4 config without mutating its effective setting."""
    variant = registered_variant(variant_name)
    setting = _effective_setting(base_payload)
    validate_schema2_setting(setting)
    expected_method_id = "ours_nl" if variant.base_method == "M3" else "ours_lk"
    if setting.get("method_id") != expected_method_id:
        raise ValueError(
            f"{variant_name} requires {variant.base_method}/{expected_method_id}")
    if maximum_lookahead_horizon(
            setting.get("lookahead_horizon")) != variant.lookahead_horizon:
        raise ValueError(
            f"{variant_name} base maximum horizon differs from registered controls")
    payload = {
        "experiment_schema": 2,
        "run_kind": "ablation",
        "ablation_protocol": ABLATION_PROTOCOL_VERSION,
        "ablation_variant": variant.name,
        "base_method": variant.base_method,
        "base_config": copy.deepcopy(dict(base_payload)),
        "controls": variant.controls(),
    }
    validate_ablation_config(payload, expected_variant=variant_name,
                             expected_method=variant.base_method)
    return payload


def _validate_paper_ablation_config(
        payload: Mapping[str, Any], *, expected_variant: str | None,
        expected_method: str | None,
        ) -> tuple[dict[str, Any], PaperAblationVariant]:
    required = {
        "experiment_schema", "run_kind", "ablation_protocol",
        "ablation_variant", "base_method", "base_config", "controls",
        "search_policy",
    }
    if set(payload) != required:
        raise ValueError(
            "paper ablation config fields differ from the registered wrapper: "
            f"missing={sorted(required - set(payload))}, "
            f"extra={sorted(set(payload) - required)}")
    if (payload.get("experiment_schema") != 2 or
            payload.get("run_kind") != "ablation" or
            payload.get("ablation_protocol") !=
            PAPER_ABLATION_PROTOCOL_VERSION):
        raise ValueError(
            "paper ablation wrapper requires Schema 2, run_kind=ablation, "
            "and protocol 2")
    name = payload.get("ablation_variant")
    if not isinstance(name, str):
        raise ValueError("paper ablation_variant must be a string")
    variant = registered_paper_variant(name)
    if expected_variant is not None and name != expected_variant:
        raise ValueError(
            f"ablation argument/config mismatch: {expected_variant!r} != "
            f"{name!r}")
    if payload.get("base_method") != variant.base_method:
        raise ValueError("paper ablation base_method differs from the registry")
    if expected_method is not None and variant.base_method != expected_method:
        raise ValueError(
            f"ablation method/config mismatch: {expected_method!r} != "
            f"{variant.base_method!r}")
    controls = payload.get("controls")
    if controls != variant.controls():
        raise ValueError(
            "paper ablation controls differ from registry: "
            f"{controls!r} != {variant.controls()!r}")
    if payload.get("search_policy") != variant.search_policy:
        raise ValueError(
            "paper ablation top-level search_policy differs from controls")
    base = payload.get("base_config")
    if not isinstance(base, Mapping):
        raise ValueError("paper ablation base_config must be an object")
    setting = _effective_setting(base)
    expected_id = "ours_nl" if variant.base_method == "M3" else "ours_lk"
    lookahead = setting.get("lookahead_horizon")
    if (setting.get("method_id") != expected_id or
            not isinstance(lookahead, Mapping) or
            maximum_lookahead_horizon(lookahead) !=
            variant.lookahead_horizon):
        raise ValueError("paper ablation base config identity/horizon mismatch")
    if setting.get("native_abi_version") != 9:
        raise ValueError("paper ablation base config requires native ABI9")

    # The accepted main-table contract remains ABI8 with the fixed H0/H8
    # method identities.  Paper protocol 2 deliberately changes only the
    # native ABI and, for two sensitivity profiles, M4's maximum visible
    # depth.  Normalize those registered dimensions solely for reuse of the
    # otherwise strict Schema-2 validator, then retain the actual ABI9 setting.
    validation_copy = copy.deepcopy(setting)
    validation_copy["native_abi_version"] = 8
    if expected_id == "ours_lk":
        validation_copy["lookahead_horizon"]["max_horizon"] = 8
    validate_schema2_setting(validation_copy)
    return copy.deepcopy(dict(base)), variant


def validate_ablation_config(payload: Mapping[str, Any], *,
                             expected_variant: str | None = None,
                             expected_method: str | None = None
                             ) -> tuple[
                                 dict[str, Any],
                                 AblationVariant | PaperAblationVariant,
                             ]:
    if payload.get("ablation_protocol") == PAPER_ABLATION_PROTOCOL_VERSION:
        return _validate_paper_ablation_config(
            payload, expected_variant=expected_variant,
            expected_method=expected_method)
    required = {
        "experiment_schema", "run_kind", "ablation_protocol",
        "ablation_variant", "base_method", "base_config", "controls",
    }
    if set(payload) != required:
        raise ValueError(
            "ablation config fields differ from the registered wrapper: "
            f"missing={sorted(required - set(payload))}, "
            f"extra={sorted(set(payload) - required)}")
    if payload.get("experiment_schema") != 2 or payload.get("run_kind") != "ablation":
        raise ValueError("ablation wrapper requires Schema 2 and run_kind=ablation")
    if payload.get("ablation_protocol") != ABLATION_PROTOCOL_VERSION:
        raise ValueError("unknown ablation protocol version")
    name = payload.get("ablation_variant")
    if not isinstance(name, str):
        raise ValueError("ablation_variant must be a string")
    variant = registered_variant(name)
    if expected_variant is not None and name != expected_variant:
        raise ValueError(
            f"ablation argument/config mismatch: {expected_variant!r} != {name!r}")
    if payload.get("base_method") != variant.base_method:
        raise ValueError("ablation base_method differs from the registry")
    if expected_method is not None and payload.get("base_method") != expected_method:
        raise ValueError(
            f"ablation method/config mismatch: {expected_method!r} != "
            f"{payload.get('base_method')!r}")
    controls = payload.get("controls")
    if controls != variant.controls():
        raise ValueError(
            f"ablation controls differ from registry: {controls!r} != "
            f"{variant.controls()!r}")
    base = payload.get("base_config")
    if not isinstance(base, Mapping):
        raise ValueError("ablation base_config must be an object")
    setting = _effective_setting(base)
    validate_schema2_setting(setting)
    expected_id = "ours_nl" if variant.base_method == "M3" else "ours_lk"
    if (setting.get("method_id") != expected_id or
            maximum_lookahead_horizon(setting.get("lookahead_horizon")) !=
            variant.lookahead_horizon):
        raise ValueError("ablation base config identity/horizon mismatch")
    return copy.deepcopy(dict(base)), variant


def select_ablation_cohort(dataset_name: str,
                           manifests: Sequence[CanonicalCircuitManifest]
                           ) -> tuple[list[CanonicalCircuitManifest], dict[str, Any]]:
    """Freeze the requested HPCA18/QMAP30 ablation cohorts deterministically."""
    rows = list(manifests)
    if dataset_name == "zac18":
        if len(rows) != 18:
            raise ValueError(f"zac18 ablation requires exactly 18 circuits, found {len(rows)}")
        selected = sorted(rows, key=lambda row: (row.canonical_sha256,
                                                 Path(row.canonical_path).name))
        return selected, {
            "selection": "all",
            "population": 18,
            "selected": 18,
            "circuits": [Path(row.canonical_path).stem for row in selected],
        }
    if dataset_name != "qmap154":
        raise ValueError(
            "formal ablation datasets must be named zac18 or qmap154")
    if len(rows) != 154:
        raise ValueError(
            f"qmap154 ablation requires exactly 154 circuits, found {len(rows)}")
    selected: list[CanonicalCircuitManifest] = []
    strata: dict[str, Any] = {}
    for label, lower, upper in QMAP_STRATA:
        members = [row for row in rows if row.gates_2q >= lower and
                   (upper is None or row.gates_2q <= upper)]
        members.sort(key=lambda row: (row.canonical_sha256,
                                      Path(row.canonical_path).name))
        if len(members) < 10:
            raise ValueError(
                f"qmap154 stratum {label} has only {len(members)} circuits; needs 10")
        chosen = members[:10]
        selected.extend(chosen)
        strata[label] = {
            "bounds_2q": [lower, upper],
            "population": len(members),
            "selected": 10,
            "circuits": [Path(row.canonical_path).stem for row in chosen],
            "canonical_sha256": [row.canonical_sha256 for row in chosen],
        }
    if len({Path(row.canonical_path).stem for row in selected}) != 30:
        raise ValueError("qmap154 ablation strata did not yield 30 unique circuits")
    return selected, {
        "selection": "three_2q_strata_sha256_min10",
        "population": 154,
        "selected": 30,
        "strata": strata,
    }


def ablation_experiment_id(base_experiment_id: str, dataset_name: str,
                           selected: Sequence[CanonicalCircuitManifest]) -> str:
    return stable_sha256({
        "experiment_schema": 2,
        "run_kind": "ablation",
        "ablation_protocol": ABLATION_PROTOCOL_VERSION,
        "base_experiment_id": base_experiment_id,
        "dataset": dataset_name,
        "variants": {
            name: {
                "base_method": variant.base_method,
                "controls": variant.controls(),
            }
            for name, variant in sorted(ABLATION_VARIANTS.items())
        },
        "cohort": [
            [Path(row.canonical_path).stem, row.canonical_sha256,
             row.gates_2q]
            for row in selected
        ],
        "seeds": [0, 1, 2, 3, 4],
    })


__all__ = [
    "ABLATION_PROTOCOL_VERSION", "ABLATION_VARIANTS", "AblationVariant",
    "PAPER_ABLATION_PROTOCOL_VERSION", "PAPER_ABLATION_VARIANTS",
    "PAPER_SENSITIVITY_HORIZONS", "PaperAblationVariant",
    "QMAP_STRATA", "ablation_experiment_id", "build_ablation_config",
    "registered_paper_variant", "registered_variant",
    "select_ablation_cohort",
    "validate_ablation_config",
]
