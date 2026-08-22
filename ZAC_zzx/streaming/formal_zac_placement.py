"""Formal bounded placement stream shared by Large M1, M3 and M4."""

from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Iterator, Mapping, Sequence

from zzx.algorithm_v2 import CacheStats, ForecastOracle, validate_schema2_setting
from zzx.resident import NextUse, ResidentRegistry
from zzx.zplacer import ResidentPlacer

from .qasm_sqlite import LayerStore, ZacStage
from .resident_transition import (
    ProviderScheduleView,
    ResidentTransitionKernel,
)
from .zac_m1_transition import ZACM1TransitionKernel, ZACM1TransitionState
from .zac_stage_provider import LayerStoreZacStageProvider


Location = tuple[int, int, int]
FrozenMapping = tuple[Location, ...]
PLACEMENT_STATE_FORMAT = "formal-zac-placement-state-v1"


def _freeze_mapping(mapping: Sequence[Sequence[int]]) -> FrozenMapping:
    result = tuple(tuple(int(value) for value in site) for site in mapping)
    if any(len(site) != 3 for site in result):
        raise ValueError("ZAC mapping locations must be triples")
    return result


@dataclass(frozen=True)
class FormalZacStagePlacement:
    """One router-ready physical stage and its exact logical attachments."""

    layer: int
    stage: ZacStage
    b_l: FrozenMapping
    g_l: FrozenMapping
    b_l_plus_1: FrozenMapping
    parent_one_qubit_gates: tuple[tuple[str, int], ...]
    selected_reuse_qubits: tuple[int, ...] = ()
    decision_log: Mapping[str, Any] | None = None

    @property
    def gates(self) -> tuple[tuple[int, int], ...]:
        return tuple(gate.gate_pair for gate in self.stage.gates)

    @property
    def gate_ids(self) -> tuple[int, ...]:
        return tuple(gate.two_qubit_index for gate in self.stage.gates)


class FormalZacPlacementStream:
    """Generate exact M1 or resident mapping triplets from a forward stage ring."""

    def __init__(
        self,
        *,
        method: str,
        architecture,
        initial_mapping: Sequence[Sequence[int]],
        store: LayerStore,
        max_gates_per_stage: int,
        setting: Mapping[str, Any] | None = None,
    ):
        method = str(method).upper()
        if method not in {"M1", "M3", "M4"}:
            raise ValueError("formal ZAC placement method must be M1, M3 or M4")
        self.method = method
        self.architecture = architecture
        self.initial_mapping = _freeze_mapping(initial_mapping)
        self.store = store
        self.max_gates_per_stage = int(max_gates_per_stage)
        resolved: dict[str, Any] | None = None
        if method != "M1":
            if setting is None:
                raise ValueError("formal M3/M4 placement requires frozen Schema-2 setting")
            resolved = dict(setting)
            validate_schema2_setting(resolved)
            expected_id = "ours_nl" if method == "M3" else "ours_lk"
            if resolved["method_id"] != expected_id:
                raise ValueError(
                    f"{method} requires method_id={expected_id!r}")
        cache_limit = (3 if method == "M1" else
                       int(resolved["lookahead_horizon"]) + 2)
        self.provider = LayerStoreZacStageProvider(
            store, self.max_gates_per_stage,
            max_cached_stages=cache_limit)
        self.stage_count = self.provider.stage_count
        self._iterated = False
        self._next_layer = 0
        self.initial_gate_mapping: FrozenMapping | None = None
        self._m1_kernel = None
        self._m1_state = None
        self._resident_kernel = None
        self._resident_boundary = self.initial_mapping

        if method == "M1":
            if setting:
                horizon = int(setting.get("lookahead_horizon", 0))
                if horizon != 0:
                    raise ValueError("formal M1 does not accept resident lookahead")
            if self.stage_count:
                self._m1_kernel = ZACM1TransitionKernel(
                    architecture, self.initial_mapping, self.provider)
                self._m1_state = self._m1_kernel.bootstrap()
                self.initial_gate_mapping = self._m1_state.g_l
        else:
            assert resolved is not None
            placer = ResidentPlacer(
                list(self.initial_mapping), **resolved)
            self._resident_kernel = ResidentTransitionKernel(
                placer, architecture, self.initial_mapping, self.provider)
            self.initial_gate_mapping = self._resident_kernel.initial_gate_mapping

    @property
    def leading_one_qubit_gates(self) -> tuple[tuple[str, int], ...]:
        return tuple(
            (event.operation, event.qubits[0])
            for event in self.store.iter_zac_leading_one_qubit())

    @property
    def next_layer(self) -> int:
        """Absolute physical stage at the next safe compiler boundary."""
        return self._next_layer

    def __iter__(self) -> Iterator[FormalZacStagePlacement]:
        if self._iterated:
            raise RuntimeError("formal ZAC placement stream is single-pass")
        self._iterated = True
        if self.stage_count == 0:
            self.provider.assert_exhausted()
            return
        while self._next_layer < self.stage_count:
            yield self.next_placement()
        self.provider.assert_exhausted()

    def _one_qubit_for_stage(self, stage: ZacStage) -> tuple[tuple[str, int], ...]:
        return tuple(
            (event.operation, event.qubits[0])
            for event in self.store.iter_zac_one_qubit_for_stage(stage))

    def next_placement(self) -> FormalZacStagePlacement:
        """Commit and return one placement, advancing state before exposure."""
        if self._next_layer >= self.stage_count:
            raise StopIteration
        if self.method == "M1":
            return self._next_m1()
        return self._next_resident()

    def _next_m1(self) -> FormalZacStagePlacement:
        layer = self._next_layer
        state = self._m1_state
        assert state is not None and self._m1_kernel is not None
        if state.layer != layer:
            raise AssertionError("M1 transition state drift")
        stage = self.provider.stage_record(layer)
        result = self._m1_kernel.step(state)
        placement = FormalZacStagePlacement(
                layer=layer,
                stage=stage,
                b_l=result.b_l,
                g_l=result.g_l,
                b_l_plus_1=result.b_l_plus_1,
                parent_one_qubit_gates=self._one_qubit_for_stage(stage),
                selected_reuse_qubits=result.selected_reuse_qubits,
            )
        if result.next_state is None:
            if layer != self.stage_count - 1:
                raise AssertionError("M1 transition ended before final stage")
        self._m1_state = result.next_state
        self._resident_boundary = result.b_l_plus_1
        self._next_layer += 1
        return placement

    def _next_resident(self) -> FormalZacStagePlacement:
        layer = self._next_layer
        kernel = self._resident_kernel
        assert kernel is not None
        stage = self.provider.stage_record(layer)
        transition = (
            kernel.finish() if layer == self.stage_count - 1
            else kernel.advance())
        if transition.source_layer != layer:
            raise AssertionError("resident transition layer drift")
        placement = FormalZacStagePlacement(
                layer=layer,
                stage=stage,
                b_l=self._resident_boundary,
                g_l=transition.source_gate_mapping,
                b_l_plus_1=transition.boundary_mapping,
                parent_one_qubit_gates=self._one_qubit_for_stage(stage),
                decision_log=transition.decision_log,
            )
        self._resident_boundary = transition.boundary_mapping
        self._next_layer += 1
        # Decision rows are emitted with the placement stream.  Retaining the
        # full history inside the placer would defeat Large bounded-memory mode.
        kernel.placer.decision_log = [deepcopy(transition.decision_log)]
        return placement

    def state_dict(
        self, *, borrow_caches: bool = False,
    ) -> dict[str, Any]:
        """Return the complete bounded placement state at a safe boundary.

        ``borrow_caches`` is reserved for synchronous checkpoint
        serialization.  The placer cannot advance while the checkpoint is
        written, so borrowing the five resident LRUs avoids a second large
        object graph without weakening snapshot consistency.  The public
        default retains the historical independent-copy behavior.
        """
        provider_state = self.provider.state_dict(
            resume_stage=self._next_layer)
        state: dict[str, Any] = {
            "format": PLACEMENT_STATE_FORMAT,
            "method": self.method,
            "next_layer": self._next_layer,
            "stage_count": self.stage_count,
            "max_gates_per_stage": self.max_gates_per_stage,
            "initial_mapping": self.initial_mapping,
            "initial_gate_mapping": self.initial_gate_mapping,
            "resident_boundary": self._resident_boundary,
            "provider": provider_state,
        }
        if self.method == "M1":
            state["m1_state"] = (None if self._m1_state is None else {
                "layer": self._m1_state.layer,
                "b_l": self._m1_state.b_l,
                "g_l": self._m1_state.g_l,
            })
            state["resident_state"] = None
        else:
            state["m1_state"] = None
            state["resident_state"] = self._resident_state_dict(
                borrow_caches=borrow_caches)
        return state

    def _resident_state_dict(
        self, *, borrow_caches: bool = False,
    ) -> dict[str, Any]:
        kernel = self._resident_kernel
        assert kernel is not None
        placer = kernel.placer
        registry = placer.registry
        if registry is None:
            raise AssertionError("resident placer has no registry")
        cache_names = (
            "transition_cache", "phase_cost_cache",
            "return_candidate_cache", "rollout_pair_cache",
            "rollout_site_cache",
        )
        caches = {
            name: (getattr(placer, name) if borrow_caches
                   else deepcopy(getattr(placer, name)))
            for name in cache_names
        }
        return {
            "format": "resident-transition-state-v1",
            "current_layer": kernel.current_layer,
            "finished": kernel.finished,
            "mapping": deepcopy(placer.mapping),
            "registry": {
                "homes": deepcopy(registry.homes),
                "zone_seat": deepcopy(registry.zone_seat),
                "storage_site": deepcopy(registry.storage_site),
                "theta": float(registry.theta),
                "zone_sites": int(registry.zone_sites),
            },
            "rng_state": placer.rng.getstate(),
            "safety_rng_state": placer.safety_rng.getstate(),
            "residency_commitments": deepcopy(placer.residency_commitments),
            "cache_stats": placer.cache_stats.as_dict(),
            "caches": caches,
            "cache_limits": {
                "transition_cache": placer.transition_cache_limit,
                "phase_cost_cache": placer.phase_cost_cache_limit,
                "return_candidate_cache": placer.return_cache_limit,
                "rollout_pair_cache": placer.rollout_geometry_cache_limit,
                "rollout_site_cache": placer.rollout_geometry_cache_limit,
            },
            "decision_log": deepcopy(placer.decision_log[-1:]),
            "search_time": float(placer.search_time),
            "ghost_fixes": int(getattr(placer, "ghost_fixes", 0)),
        }

    @classmethod
    def from_state(
        cls,
        *,
        architecture,
        initial_mapping: Sequence[Sequence[int]],
        store: LayerStore,
        max_gates_per_stage: int,
        state: Mapping[str, Any],
        setting: Mapping[str, Any] | None = None,
        take_cache_ownership: bool = False,
    ) -> "FormalZacPlacementStream":
        """Restore at ``state['next_layer']`` without replaying prior stages."""
        if state.get("format") != PLACEMENT_STATE_FORMAT:
            raise ValueError("unsupported formal ZAC placement state")
        method = str(state.get("method", "")).upper()
        initial = _freeze_mapping(initial_mapping)
        if initial != _freeze_mapping(state["initial_mapping"]):
            raise ValueError("checkpoint initial mapping mismatch")
        if int(max_gates_per_stage) != int(state["max_gates_per_stage"]):
            raise ValueError("checkpoint ZAC stage capacity mismatch")
        next_layer = int(state["next_layer"])

        resolved: dict[str, Any] | None = None
        if method == "M1":
            cache_limit = 3
        else:
            if setting is None:
                raise ValueError("restoring M3/M4 requires its frozen setting")
            resolved = dict(setting)
            validate_schema2_setting(resolved)
            expected_id = "ours_nl" if method == "M3" else "ours_lk"
            if resolved["method_id"] != expected_id:
                raise ValueError(f"{method} requires method_id={expected_id!r}")
            cache_limit = int(resolved["lookahead_horizon"]) + 2

        obj = cls.__new__(cls)
        obj.method = method
        obj.architecture = architecture
        obj.initial_mapping = initial
        obj.store = store
        obj.max_gates_per_stage = int(max_gates_per_stage)
        obj.provider = LayerStoreZacStageProvider(
            store, obj.max_gates_per_stage,
            max_cached_stages=cache_limit,
            start_stage=next_layer,
        )
        obj.stage_count = obj.provider.stage_count
        if obj.stage_count != int(state["stage_count"]):
            raise ValueError("checkpoint/store physical stage count mismatch")
        if not 0 <= next_layer <= obj.stage_count:
            raise ValueError("checkpoint next physical stage is invalid")
        saved_provider = state["provider"]
        if (saved_provider.get("format") != "zac-stage-provider-state-v1" or
                int(saved_provider["resume_stage"]) != next_layer or
                int(saved_provider["stage_count"]) != obj.stage_count or
                int(saved_provider["max_cached_stages"]) != cache_limit):
            raise ValueError("checkpoint provider summary mismatch")
        obj._iterated = False
        obj._next_layer = next_layer
        raw_initial_gate = state.get("initial_gate_mapping")
        obj.initial_gate_mapping = (
            None if raw_initial_gate is None else _freeze_mapping(raw_initial_gate))
        obj._resident_boundary = _freeze_mapping(state["resident_boundary"])
        obj._m1_kernel = None
        obj._m1_state = None
        obj._resident_kernel = None

        if method == "M1":
            obj._m1_kernel = ZACM1TransitionKernel(
                architecture, initial, obj.provider)
            raw_m1 = state.get("m1_state")
            if raw_m1 is not None:
                obj._m1_state = ZACM1TransitionState(
                    layer=int(raw_m1["layer"]),
                    b_l=_freeze_mapping(raw_m1["b_l"]),
                    g_l=_freeze_mapping(raw_m1["g_l"]),
                )
            if ((next_layer < obj.stage_count) != (obj._m1_state is not None)):
                raise ValueError("checkpoint M1 state/boundary mismatch")
            if obj._m1_state is not None and obj._m1_state.layer != next_layer:
                raise ValueError("checkpoint M1 layer mismatch")
        else:
            assert resolved is not None
            placer = ResidentPlacer(list(initial), **resolved)
            obj._resident_kernel = obj._restore_resident_kernel(
                placer, state["resident_state"],
                take_cache_ownership=take_cache_ownership,
            )
        return obj

    def _restore_resident_kernel(
        self,
        placer: ResidentPlacer,
        state: Mapping[str, Any],
        *,
        take_cache_ownership: bool = False,
    ) -> ResidentTransitionKernel:
        if state.get("format") != "resident-transition-state-v1":
            raise ValueError("unsupported resident transition state")
        kernel = ResidentTransitionKernel.__new__(ResidentTransitionKernel)
        kernel.placer = placer
        kernel.provider = self.provider
        kernel.schedule = ProviderScheduleView(self.provider)
        kernel.layer_count = self.stage_count
        kernel.initial_mapping = self.initial_mapping
        kernel.current_layer = (
            None if state.get("current_layer") is None
            else int(state["current_layer"]))
        kernel.initial_gate_mapping = self.initial_gate_mapping
        kernel.finished = bool(state["finished"])

        placer.architecture = self.architecture
        placer.gate_scheduling = kernel.schedule
        # Schema-2 resident placement never consults legacy adjacent reuse.
        placer.list_reuse_qubit = ()
        placer.mapping = deepcopy(state["mapping"])
        registry_state = state["registry"]
        registry = ResidentRegistry(
            self.architecture, list(self.initial_mapping),
            float(registry_state["theta"]))
        registry.homes = deepcopy(registry_state["homes"])
        registry.zone_seat = {
            int(q): tuple(site)
            for q, site in registry_state["zone_seat"].items()
        }
        registry.storage_site = {
            int(q): tuple(site)
            for q, site in registry_state["storage_site"].items()
        }
        if registry.zone_sites != int(registry_state["zone_sites"]):
            raise ValueError("checkpoint resident architecture capacity mismatch")
        placer.registry = registry
        placer.nu = NextUse([])
        placer.forecast = ForecastOracle(
            self.provider, placer.lookahead_horizon)
        placer.rng.setstate(state["rng_state"])
        placer.safety_rng.setstate(state["safety_rng_state"])
        placer.residency_commitments = {
            int(q): (int(value[0]), tuple(value[1]))
            for q, value in state["residency_commitments"].items()
        }
        cache_stats = CacheStats()
        for name, value in state["cache_stats"].items():
            if not hasattr(cache_stats, name):
                raise ValueError(f"unknown checkpoint cache statistic {name!r}")
            setattr(cache_stats, name, int(value))
        placer.cache_stats = cache_stats
        expected_limits = {
            "transition_cache": placer.transition_cache_limit,
            "phase_cost_cache": placer.phase_cost_cache_limit,
            "return_candidate_cache": placer.return_cache_limit,
            "rollout_pair_cache": placer.rollout_geometry_cache_limit,
            "rollout_site_cache": placer.rollout_geometry_cache_limit,
        }
        if dict(state["cache_limits"]) != expected_limits:
            raise ValueError("checkpoint resident cache limits/config mismatch")
        for name, limit in expected_limits.items():
            saved_cache = state["caches"][name]
            if not isinstance(saved_cache, OrderedDict):
                raise ValueError(f"checkpoint {name} is not an OrderedDict")
            cache = (saved_cache if take_cache_ownership
                     else OrderedDict(deepcopy(saved_cache)))
            if len(cache) > limit:
                raise ValueError(f"checkpoint {name} exceeds its bounded limit")
            setattr(placer, name, cache)
        placer.decision_log = deepcopy(state["decision_log"])
        if len(placer.decision_log) > 1:
            raise ValueError("checkpoint resident decision log is unbounded")
        placer.search_time = float(state["search_time"])
        placer.ghost_fixes = int(state["ghost_fixes"])

        expected_current = (None if self.stage_count == 0 else
                            (self.stage_count - 1 if kernel.finished
                             else self._next_layer))
        if kernel.current_layer != expected_current:
            raise ValueError("checkpoint resident layer/boundary mismatch")
        kernel._assert_bounded_mapping_state()
        return kernel


__all__ = [
    "FormalZacPlacementStream",
    "FormalZacStagePlacement",
    "PLACEMENT_STATE_FORMAT",
]
