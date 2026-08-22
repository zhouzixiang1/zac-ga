"""Formal bounded placement stream shared by Large M1, M3 and M4."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator, Mapping, Sequence

from zzx.algorithm_v2 import validate_schema2_setting
from zzx.zplacer import ResidentPlacer

from .qasm_sqlite import LayerStore, ZacStage
from .resident_transition import ResidentTransitionKernel
from .zac_m1_transition import ZACM1TransitionKernel
from .zac_stage_provider import LayerStoreZacStageProvider


Location = tuple[int, int, int]
FrozenMapping = tuple[Location, ...]


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
        self.provider = LayerStoreZacStageProvider(
            store, max_gates_per_stage, max_cached_stages=4)
        self.stage_count = self.provider.stage_count
        self._iterated = False
        self.initial_gate_mapping: FrozenMapping | None = None
        self._m1_kernel = None
        self._m1_state = None
        self._resident_kernel = None

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
            if setting is None:
                raise ValueError("formal M3/M4 placement requires frozen Schema-2 setting")
            resolved = dict(setting)
            validate_schema2_setting(resolved)
            expected_id = "ours_nl" if method == "M3" else "ours_lk"
            if resolved["method_id"] != expected_id:
                raise ValueError(
                    f"{method} requires method_id={expected_id!r}")
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

    def __iter__(self) -> Iterator[FormalZacStagePlacement]:
        if self._iterated:
            raise RuntimeError("formal ZAC placement stream is single-pass")
        self._iterated = True
        if self.stage_count == 0:
            self.provider.assert_exhausted()
            return

        if self.method == "M1":
            yield from self._iter_m1()
        else:
            yield from self._iter_resident()
        self.provider.assert_exhausted()

    def _one_qubit_for_stage(self, stage: ZacStage) -> tuple[tuple[str, int], ...]:
        return tuple(
            (event.operation, event.qubits[0])
            for event in self.store.iter_zac_one_qubit_for_stage(stage))

    def _iter_m1(self) -> Iterator[FormalZacStagePlacement]:
        state = self._m1_state
        assert state is not None and self._m1_kernel is not None
        for layer in range(self.stage_count):
            stage = self.provider.stage_record(layer)
            result = self._m1_kernel.step(state)
            yield FormalZacStagePlacement(
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
            else:
                state = result.next_state

    def _iter_resident(self) -> Iterator[FormalZacStagePlacement]:
        kernel = self._resident_kernel
        assert kernel is not None
        boundary = self.initial_mapping
        for layer in range(self.stage_count):
            stage = self.provider.stage_record(layer)
            transition = (
                kernel.finish() if layer == self.stage_count - 1
                else kernel.advance())
            if transition.source_layer != layer:
                raise AssertionError("resident transition layer drift")
            yield FormalZacStagePlacement(
                layer=layer,
                stage=stage,
                b_l=boundary,
                g_l=transition.source_gate_mapping,
                b_l_plus_1=transition.boundary_mapping,
                parent_one_qubit_gates=self._one_qubit_for_stage(stage),
                decision_log=transition.decision_log,
            )
            boundary = transition.boundary_mapping


__all__ = ["FormalZacPlacementStream", "FormalZacStagePlacement"]
