"""Search-based two-qubit target placement compiler variant."""

from __future__ import annotations

from random import Random
from time import monotonic
from typing import Dict, List, Mapping

from .compiler import CircuitInput, CompileResult, Compiler
from .hardware import HardwareSpec, Position
from .ir import Gate2Q
from .search_emit import EmissionCandidate, search_2q_emission, target_signature


class SearchCompiler(Compiler):
    """Compiler variant that searches conflict-swap 2Q target placements."""

    def __init__(
        self,
        hardware: HardwareSpec | None = None,
        *,
        population_size: int = 6,
        iterations: int = 8,
        neighbors_per_solution: int = 2,
        neighbor_sample_size: int = 24,
        elite_parent_fraction: float = 0.3,
        random_initial: bool = True,
        random_seed: int = 0,
        timeout: float | None = 10.0,
        convergence_patience: int | None = 3,
        stop_on_zero_badness: bool = True,
    ):
        super().__init__(hardware)
        self.population_size = population_size
        self.iterations = iterations
        self.neighbors_per_solution = neighbors_per_solution
        self.neighbor_sample_size = neighbor_sample_size
        self.elite_parent_fraction = elite_parent_fraction
        self.random_initial = random_initial
        self.random_seed = random_seed
        self.timeout = timeout
        self.convergence_patience = convergence_patience
        self.stop_on_zero_badness = stop_on_zero_badness
        self._deadline: float | None = None

    def compile(self, circuit: CircuitInput, optimization_level: int = 1) -> CompileResult:
        previous_deadline = self._deadline
        self._deadline = monotonic() + self.timeout if self.timeout is not None else None
        try:
            return super().compile(circuit, optimization_level)
        finally:
            self._deadline = previous_deadline

    def _select_2q_candidate(
        self,
        homes: Dict[int, Position],
        current: Dict[int, Position],
        ordered: List[Gate2Q],
        target_positions: Dict[int, Position],
    ) -> EmissionCandidate:
        initial = self._evaluate_2q_targets(homes, current, ordered, target_positions)

        def evaluate(
            targets: Mapping[int, Position],
            parent_graph_cache=None,
        ) -> EmissionCandidate:
            return self._evaluate_2q_targets(homes, current, ordered, dict(targets), parent_graph_cache)

        deadline = self._deadline
        initial_candidates = ()
        if self.random_initial:
            initial_candidates = self._random_initial_candidates(homes, current, ordered, deadline)

        remaining_timeout = None if deadline is None else max(0.0, deadline - monotonic())
        return search_2q_emission(
            self.hw,
            initial,
            evaluate,
            population_size=self.population_size,
            iterations=self.iterations,
            neighbors_per_solution=self.neighbors_per_solution,
            neighbor_sample_size=self.neighbor_sample_size,
            elite_parent_fraction=self.elite_parent_fraction,
            initial_candidates=initial_candidates,
            timeout=remaining_timeout,
            convergence_patience=self.convergence_patience,
            stop_on_zero_badness=self.stop_on_zero_badness,
        )

    def _random_initial_candidates(
        self,
        homes: Dict[int, Position],
        current: Dict[int, Position],
        ordered: List[Gate2Q],
        deadline: float | None = None,
    ) -> tuple[EmissionCandidate, ...]:
        if len(ordered) > self.hw.entanglement.rows:
            return ()
        rng = Random(self.random_seed)
        candidates: list[EmissionCandidate] = []
        seen: set[tuple[tuple[int, Position], ...]] = set()
        max_attempts = max(100, self.population_size * 20)
        rows = list(range(self.hw.entanglement.rows))
        for _ in range(max_attempts):
            if deadline is not None and monotonic() >= deadline:
                break
            sampled_rows = rng.sample(rows, len(ordered))
            targets = self._build_2q_targets_for_rows(ordered, current, sampled_rows)
            signature = target_signature(targets)
            if signature in seen:
                continue
            seen.add(signature)
            try:
                candidates.append(self._evaluate_2q_targets(homes, current, ordered, targets))
            except ValueError:
                continue
            if len(candidates) >= self.population_size:
                break
        return tuple(candidates)


def compile_circuit(
    circuit: CircuitInput,
    hardware: HardwareSpec | None = None,
    optimization_level: int = 1,
    *,
    population_size: int = 6,
    iterations: int = 16,
    neighbors_per_solution: int = 2,
    neighbor_sample_size: int = 24,
    elite_parent_fraction: float = 0.3,
    random_initial: bool = True,
    random_seed: int = 0,
    timeout: float | None = 10.0,
    convergence_patience: int | None = 3,
    stop_on_zero_badness: bool = True,
) -> CompileResult:
    return SearchCompiler(
        hardware,
        population_size=population_size,
        iterations=iterations,
        neighbors_per_solution=neighbors_per_solution,
        neighbor_sample_size=neighbor_sample_size,
        elite_parent_fraction=elite_parent_fraction,
        random_initial=random_initial,
        random_seed=random_seed,
        timeout=timeout,
        convergence_patience=convergence_patience,
        stop_on_zero_badness=stop_on_zero_badness,
    ).compile(circuit, optimization_level)
