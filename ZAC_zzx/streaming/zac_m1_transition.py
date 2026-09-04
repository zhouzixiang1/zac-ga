"""Exact bounded-window transition adapter for the formal M1 ZAC placer.

The batch :class:`zac.placer.vmplacer.VertexMatchingPlacer` materializes the
complete ``B_0, G_0, B_1, G_1, ...`` mapping stream.  Its actual placement
semantics are nevertheless local: at boundary ``L`` it constructs the
non-reuse and (when available) adjacent-reuse candidates from ``L``/``L+1``;
the reuse candidate for ``G_{L+1}`` additionally consults ``L+2``.  This module
adapts that exact implementation to a bounded state without copying its
matching, candidate generation, or ``filter_mapping`` objective.

Only two mappings are carried between calls: the boundary mapping ``B_L`` and
the current gate mapping ``G_L``.  A transition returns the routing triplet
``(B_L, G_L, B_{L+1})`` and, for a non-terminal layer, ``G_{L+1}``.  The stage
provider remains external and is read only at indices ``L`` through ``L+2``.

This adapter deliberately does not cover scheduling, capacity splitting, the
initial SA placement, or routing.  Those are separate formal-equivalence
boundaries.  It assumes that the provider already exposes the exact physical
ZAC stages produced by ``Scheduler_mixin.scheduling``.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Protocol, Sequence, runtime_checkable

from zac.placer.vmplacer import VertexMatchingPlacer
from zac.zac import ZAC


Gate = tuple[int, int]
Stage = tuple[Gate, ...]
Location = tuple[int, int, int]
FrozenMapping = tuple[Location, ...]


@runtime_checkable
class ZACPhysicalStageProvider(Protocol):
    """Indexed source of already capacity-split physical ZAC 2Q stages.

    Implementations may expose either ``stage(index)`` or ``__getitem__``.
    They must also expose their finite length through ``__len__`` or a
    ``stage_count`` attribute/property.  A SQLite implementation can therefore
    issue one indexed query per requested stage without retaining the circuit.
    """

    def __len__(self) -> int: ...

    def __getitem__(self, index: int) -> Sequence[Sequence[int]]: ...


def _freeze_mapping(mapping: Sequence[Sequence[int]]) -> FrozenMapping:
    frozen = tuple(tuple(int(value) for value in location) for location in mapping)
    if any(len(location) != 3 for location in frozen):
        raise ValueError("every ZAC mapping location must be an (slm,row,column) triple")
    if len(set(frozen)) != len(frozen):
        raise ValueError("ZAC mapping must be injective")
    return frozen


def _thaw_mapping(mapping: FrozenMapping) -> list[Location]:
    return [tuple(location) for location in mapping]


def _materialize_stage(stage: Sequence[Sequence[int]], index: int) -> list[list[int]]:
    result: list[list[int]] = []
    seen: set[int] = set()
    for raw_gate in stage:
        if len(raw_gate) != 2:
            raise ValueError(f"physical stage {index} contains a non-2Q gate")
        q0, q1 = int(raw_gate[0]), int(raw_gate[1])
        if q0 < 0 or q1 < 0 or q0 == q1:
            raise ValueError(f"physical stage {index} contains invalid gate {(q0, q1)}")
        if q0 in seen or q1 in seen:
            raise ValueError(f"physical stage {index} is not pairwise disjoint")
        seen.update((q0, q1))
        # Do not sort endpoints or gates.  VertexMatchingPlacer uses authored
        # order for deterministic sparse-matrix columns and orientation ties.
        result.append([q0, q1])
    if not result:
        raise ValueError(f"physical stage {index} is empty")
    return result


def _provider_stage_count(provider: Any) -> int:
    raw = getattr(provider, "stage_count", None)
    if raw is not None:
        raw = raw() if callable(raw) else raw
        count = int(raw)
    else:
        try:
            count = len(provider)
        except TypeError as exc:  # pragma: no cover - defensive API error
            raise TypeError(
                "stage provider must define __len__ or stage_count") from exc
    if count < 0:
        raise ValueError("stage provider length cannot be negative")
    return count


def _provider_stage(provider: Any, index: int) -> list[list[int]]:
    stage_method = getattr(provider, "stage", None)
    raw = stage_method(index) if callable(stage_method) else provider[index]
    return _materialize_stage(raw, index)


def adjacent_reuse_qubits(
    previous_stage: Sequence[Sequence[int]],
    next_stage: Sequence[Sequence[int]],
    n_qubits: int,
) -> frozenset[int]:
    """Return ZAC's exact adjacent-layer reuse set using its own implementation.

    ``ZAC.collect_reuse_qubit`` allocates ``layers x qubits`` even though each
    decision is pair-local.  Invoking that implementation on the two relevant
    stages preserves its SciPy maximum-matching semantics while bounding the
    temporary table to ``2 x qubits``.
    """

    if n_qubits <= 0:
        raise ValueError("n_qubits must be positive")
    previous = _materialize_stage(previous_stage, 0)
    following = _materialize_stage(next_stage, 1)
    for gate in previous + following:
        if max(gate) >= n_qubits:
            raise ValueError("physical stage references a qubit outside the mapping")
    collector = SimpleNamespace(
        n_q=n_qubits,
        gate_scheduling=[previous, following],
    )
    # Deliberately call the production method unbound.  The method only consumes
    # the two attributes above and writes reuse_qubit/extra_reuse_qubit.
    ZAC.collect_reuse_qubit(collector)
    return frozenset(int(q) for q in collector.reuse_qubit[0])


class _ReuseWindow:
    """Sparse list-like view required by VertexMatchingPlacer.

    ``filter_mapping`` mutates ``list_reuse_qubit[L]`` to an empty list when the
    non-reuse world wins, so this adapter supports both indexed reads and that
    exact write without allocating an entry for every circuit layer.
    """

    def __init__(self, values: dict[int, frozenset[int]]):
        self._values = {int(index): set(value) for index, value in values.items()}

    def __getitem__(self, index: int) -> set[int]:
        try:
            return self._values[int(index)]
        except KeyError as exc:
            raise IndexError(
                f"reuse layer {index} lies outside the bounded transition window") from exc

    def __setitem__(self, index: int, value: Sequence[int] | set[int]) -> None:
        if int(index) not in self._values:
            raise IndexError(
                f"reuse layer {index} lies outside the bounded transition window")
        self._values[int(index)] = {int(q) for q in value}


@dataclass(frozen=True)
class ZACM1TransitionState:
    """Persistent placement state at the entrance to physical stage ``layer``."""

    layer: int
    b_l: FrozenMapping
    g_l: FrozenMapping


@dataclass(frozen=True)
class ZACM1TransitionResult:
    """Exact batch-equivalent output for one M1 physical stage."""

    layer: int
    b_l: FrozenMapping
    g_l: FrozenMapping
    b_l_plus_1: FrozenMapping
    g_l_plus_1: FrozenMapping | None
    reuse_candidates: tuple[int, ...]
    selected_reuse_qubits: tuple[int, ...]
    next_state: ZACM1TransitionState | None

    @property
    def route_triplet(self) -> tuple[FrozenMapping, FrozenMapping, FrozenMapping]:
        """Mappings consumed by ``route_qubit_mis(layer)``."""

        return self.b_l, self.g_l, self.b_l_plus_1

    @property
    def selected_reuse(self) -> bool:
        return bool(self.selected_reuse_qubits)


class ZACM1TransitionKernel:
    """Bounded-window adapter around the production M1 vertex matcher."""

    def __init__(self, architecture: Any, initial_mapping: Sequence[Sequence[int]],
                 stages: ZACPhysicalStageProvider | Any):
        self.architecture = architecture
        self.initial_mapping = _freeze_mapping(initial_mapping)
        if not self.initial_mapping:
            raise ValueError("M1 transition kernel requires at least one qubit")
        self.n_qubits = len(self.initial_mapping)
        self.stages = stages
        self.stage_count = _provider_stage_count(stages)
        if self.stage_count <= 0:
            raise ValueError("M1 transition kernel requires at least one physical stage")

    def _new_placer(self) -> VertexMatchingPlacer:
        placer = VertexMatchingPlacer(_thaw_mapping(self.initial_mapping))
        placer.architecture = self.architecture
        return placer

    def initial_state(self) -> ZACM1TransitionState:
        """Construct ``G_0`` exactly as ``VertexMatchingPlacer.run`` does."""

        first_stage = _provider_stage(self.stages, 0)
        placer = self._new_placer()
        placer.list_reuse_qubit = _ReuseWindow({0: frozenset()})
        homes = _thaw_mapping(self.initial_mapping)
        # Batch run passes list_gate[0:2], but test_reuse=False means place_gate
        # reads only element zero.  Supplying exactly that element avoids a
        # needless stage-1 read without changing production semantics.
        placer.place_gate([homes], [first_stage], 0, False)
        return ZACM1TransitionState(
            layer=0,
            b_l=self.initial_mapping,
            g_l=_freeze_mapping(placer.mapping[-1]),
        )

    # A concise alias for callers that model compiler bootstrap explicitly.
    bootstrap = initial_state

    def transition(self, state: ZACM1TransitionState) -> ZACM1TransitionResult:
        """Advance exactly one physical stage using at most ``L..L+2``.

        The method intentionally drives the production placer's public helper
        methods in the same mutation order as ``VertexMatchingPlacer.run``.
        Keeping the five transient mappings in that order is important because
        ``filter_mapping`` addresses them by negative index.
        """

        layer = int(state.layer)
        if layer < 0 or layer >= self.stage_count:
            raise IndexError(f"physical stage {layer} is outside the provider")
        b_l = _freeze_mapping(state.b_l)
        g_l = _freeze_mapping(state.g_l)
        if len(b_l) != self.n_qubits or len(g_l) != self.n_qubits:
            raise ValueError("transition state width differs from initial mapping")

        current_stage = _provider_stage(self.stages, layer)
        for gate in current_stage:
            if max(gate) >= self.n_qubits:
                raise ValueError("physical stage references a qubit outside the mapping")

        terminal = layer + 1 >= self.stage_count
        next_stage = None
        following_stage = None
        reuse_current = frozenset()
        reuse_next = frozenset()
        if not terminal:
            next_stage = _provider_stage(self.stages, layer + 1)
            reuse_current = adjacent_reuse_qubits(
                current_stage, next_stage, self.n_qubits)
            if layer + 2 < self.stage_count:
                following_stage = _provider_stage(self.stages, layer + 2)
                reuse_next = adjacent_reuse_qubits(
                    next_stage, following_stage, self.n_qubits)

        reuse = _ReuseWindow({
            layer: reuse_current,
            layer + 1: reuse_next,
        })
        placer = self._new_placer()
        placer.list_reuse_qubit = reuse
        # mapping[0] must remain the original homes: place_qubit uses it as the
        # storage anchor.  The trailing entries reproduce the production
        # negative-index layout for this single transition.
        placer.mapping = [
            _thaw_mapping(self.initial_mapping),
            _thaw_mapping(g_l),
        ]

        stage_window = [current_stage]
        if next_stage is not None:
            stage_window.append(next_stage)
        placer.place_qubit(stage_window, layer, False)

        if terminal:
            b_next = _freeze_mapping(placer.mapping[-1])
            return ZACM1TransitionResult(
                layer=layer,
                b_l=b_l,
                g_l=g_l,
                b_l_plus_1=b_next,
                g_l_plus_1=None,
                reuse_candidates=(),
                selected_reuse_qubits=(),
                next_state=None,
            )

        assert next_stage is not None
        next_window = [next_stage]
        if following_stage is not None:
            next_window.append(following_stage)

        # Non-reuse world: G_L, B_nr, G_nr.
        placer.place_gate(placer.mapping[-2:], next_window, layer + 1, False)

        if reuse_current:
            # Reuse world: append B_r and G_r after the non-reuse pair, then let
            # production filter_mapping choose and pop the losing pair.
            placer.place_qubit(stage_window, layer, True)
            placer.place_gate(
                [placer.mapping[-4], placer.mapping[-1]],
                next_window,
                layer + 1,
                True,
            )
            placer.filter_mapping(layer)

        b_next = _freeze_mapping(placer.mapping[-2])
        g_next = _freeze_mapping(placer.mapping[-1])
        selected = tuple(sorted(reuse[layer]))
        next_state = ZACM1TransitionState(
            layer=layer + 1,
            b_l=b_next,
            g_l=g_next,
        )
        return ZACM1TransitionResult(
            layer=layer,
            b_l=b_l,
            g_l=g_l,
            b_l_plus_1=b_next,
            g_l_plus_1=g_next,
            reuse_candidates=tuple(sorted(reuse_current)),
            selected_reuse_qubits=selected,
            next_state=next_state,
        )

    # A short state-machine spelling used by streaming compiler loops.
    step = transition


__all__ = [
    "FrozenMapping",
    "ZACM1TransitionKernel",
    "ZACM1TransitionResult",
    "ZACM1TransitionState",
    "ZACPhysicalStageProvider",
    "adjacent_reuse_qubits",
]
