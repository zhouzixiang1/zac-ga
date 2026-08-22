"""Exact bounded-memory transition kernel for formal M3/M4 placement.

The batch :class:`~zzx.zplacer.ResidentPlacer` materialises a ``2*n+1`` mapping
stream.  Large compilation needs the same decisions while retaining only the
initial home mapping, the current gate mapping and the next boundary.  This
module drives the *same* ``_ga_step_v2`` and terminal-boundary functions and
compacts their already-committed mapping stream after every transition.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from copy import deepcopy
from dataclasses import dataclass

from zzx.algorithm_v2 import ForecastLayerProvider
from zzx.zplacer import ResidentPlacer


Site = tuple[int, int, int]
FrozenMapping = tuple[Site, ...]


def _freeze_mapping(mapping: Sequence[Sequence[int]]) -> FrozenMapping:
    return tuple((int(site[0]), int(site[1]), int(site[2])) for site in mapping)


class ProviderScheduleView(Sequence[tuple[tuple[int, int], ...]]):
    """Sequence facade over a bounded layer provider.

    The resident placement planner still uses ordinary sequence indexing for
    physical stage zero.  All cross-boundary future reads remain gated by
    ``ForecastOracle``; this facade merely avoids materialising the schedule.
    """

    def __init__(self, provider: ForecastLayerProvider):
        self.provider = provider

    def __len__(self) -> int:
        return self.provider.layer_count

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self.provider.read_layer(i)
                    for i in range(*index.indices(len(self)))]
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError(index)
        return tuple(tuple(gate) for gate in self.provider.read_layer(index))

    def __iter__(self) -> Iterator[tuple[tuple[int, int], ...]]:
        for layer in range(len(self)):
            yield self[layer]


@dataclass(frozen=True)
class ResidentBoundaryTransition:
    """Router-ready mappings committed at one resident boundary."""

    source_layer: int
    source_gate_mapping: FrozenMapping
    boundary_mapping: FrozenMapping
    target_gate_mapping: FrozenMapping | None
    decision_log: dict


class ResidentTransitionKernel:
    """Drive formal ``ResidentPlacer`` transitions with bounded mapping state.

    ``advance`` handles boundaries with a following two-qubit stage and invokes
    the exact Schema-2 GA.  ``finish`` handles the final boundary.  Reassembling
    ``initial_mapping``, ``initial_gate_mapping`` and the returned mappings must
    reproduce the batch placer's complete ``2*n+1`` mapping stream byte for byte.
    """

    def __init__(self, placer: ResidentPlacer, architecture,
                 initial_mapping: Sequence[Sequence[int]],
                 provider: ForecastLayerProvider):
        if placer.experiment_schema != 2:
            raise ValueError("resident streaming kernel requires experiment_schema=2")
        if placer.engine != "ga":
            raise ValueError("resident streaming kernel requires engine='ga'")
        self.placer = placer
        self.provider = provider
        self.schedule = ProviderScheduleView(provider)
        self.layer_count = len(self.schedule)
        self.initial_mapping = _freeze_mapping(initial_mapping)
        self.current_layer: int | None = None
        self.initial_gate_mapping: FrozenMapping | None = None
        self.finished = False

        n = placer._initialize_run_state(
            architecture, [list(initial_mapping)], self.schedule,
            forecast_source=provider)
        if n != self.layer_count:
            raise AssertionError("resident schedule/provider layer-count mismatch")
        if n == 0:
            self.finished = True
            return

        placement = placer._plan_round(0)
        placer._repair_ghosts(placement, {})
        placer._commit_round(0, placement)
        self.current_layer = 0
        self.initial_gate_mapping = _freeze_mapping(placer.mapping[-1])
        self._assert_bounded_mapping_state()

    @property
    def retained_mapping_count(self) -> int:
        return len(self.placer.mapping)

    def _assert_bounded_mapping_state(self) -> None:
        if self.finished and self.layer_count == 0:
            expected = 1
        else:
            expected = 2
        if len(self.placer.mapping) != expected:
            raise AssertionError(
                f"resident kernel retained {len(self.placer.mapping)} mappings; "
                f"expected {expected}")

    def _compact(self, current_mapping: FrozenMapping) -> None:
        self.placer.mapping = [
            [tuple(site) for site in self.initial_mapping],
            [tuple(site) for site in current_mapping],
        ]
        self._assert_bounded_mapping_state()

    def advance(self) -> ResidentBoundaryTransition:
        """Commit the current boundary and the next two-qubit gate mapping."""
        if self.finished or self.current_layer is None:
            raise RuntimeError("resident transition kernel is already finished")
        if self.current_layer + 1 >= self.layer_count:
            raise RuntimeError("final resident boundary must be committed with finish()")

        source_layer = self.current_layer
        source = _freeze_mapping(self.placer.mapping[-1])
        log_count = len(self.placer.decision_log)
        self.placer._ga_step_v2(source_layer)
        if len(self.placer.decision_log) != log_count + 1:
            raise AssertionError("resident transition did not append exactly one decision")
        boundary = _freeze_mapping(self.placer.mapping[-2])
        target = _freeze_mapping(self.placer.mapping[-1])
        decision = deepcopy(self.placer.decision_log[-1])
        self.current_layer += 1
        self._compact(target)
        return ResidentBoundaryTransition(
            source_layer=source_layer,
            source_gate_mapping=source,
            boundary_mapping=boundary,
            target_gate_mapping=target,
            decision_log=decision,
        )

    def finish(self) -> ResidentBoundaryTransition:
        """Commit the exact final boundary without forcing an unregistered return."""
        if self.finished or self.current_layer is None:
            raise RuntimeError("resident transition kernel is already finished")
        if self.current_layer != self.layer_count - 1:
            raise RuntimeError("advance all non-terminal boundaries before finish()")

        source_layer = self.current_layer
        source = _freeze_mapping(self.placer.mapping[-1])
        log_count = len(self.placer.decision_log)
        self.placer._finish_terminal_boundary(source_layer)
        if len(self.placer.decision_log) != log_count + 1:
            raise AssertionError("terminal transition did not append exactly one decision")
        boundary = _freeze_mapping(self.placer.mapping[-1])
        decision = deepcopy(self.placer.decision_log[-1])
        self.finished = True
        self._compact(boundary)
        return ResidentBoundaryTransition(
            source_layer=source_layer,
            source_gate_mapping=source,
            boundary_mapping=boundary,
            target_gate_mapping=None,
            decision_log=decision,
        )


__all__ = [
    "ProviderScheduleView",
    "ResidentBoundaryTransition",
    "ResidentTransitionKernel",
]
