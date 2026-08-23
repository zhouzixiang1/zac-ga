"""Pure-Python oracle for the native boundary scorer.

This module intentionally does not call :mod:`zzx.algorithm_v2`, ``ghost.py`` or
``zcost.py``.  Differential tests would be much less useful if both sides shared
the same implementation bug.
"""
from __future__ import annotations

from math import dist, inf, log, log1p, sqrt
from time import perf_counter_ns
from typing import Iterable, Sequence

from .boundary_problem import (
    BoundaryConfig,
    BoundaryProblem,
    BoundaryResult,
    CandidatePlan,
    FitnessResult,
    Ghost,
    Leg,
    MovementPhase,
    RichH0Problem,
    RichSearchConfig,
)


EPS = 1e-9
S_TOL = 1e-6
F_EXC = 0.9975
F_TRANSFER = 0.999
T_TRANSFER_US = 15.0
T_RYDBERG_US = 0.36
ACCEL_UM_PER_US2 = 0.00275
T2_US = 1.5e6


def evaluate_decay_forecast(
        problem: RichH0Problem,
        config: RichSearchConfig,
        chromosome: Sequence[int],
        gate_option_indices: Sequence[int],
        return_assignments: Sequence[tuple[int, int]],
) -> tuple[float, tuple[float, ...], dict[str, float], int, int]:
    """Independent Python oracle for the ABI3 bounded-decay term table."""
    if problem.selected_horizon != config.max_horizon:
        raise ValueError("problem/config horizon mismatch")
    gate_count = len(problem.gate_domains)
    eligible_by_atom = {atom: index for index, atom in enumerate(problem.eligible)}
    assigned = {
        eligible_by_atom[int(atom)]: int(site)
        for atom, site in return_assignments
    }
    by_depth = [0.0] * (config.max_horizon + 1)
    breakdown = {"residency": 0.0, "reentry": 0.0, "terminal": 0.0}
    applied = skipped = 0

    def bit(index: int) -> bool:
        return bool(chromosome[gate_count + index])

    for term in problem.forecast_terms:
        decay_factor = config.decay_rho ** (term.depth - 1)
        if decay_factor < config.decay_epsilon:
            skipped += 1
            continue
        weight = config.alpha_lookahead * decay_factor
        if term.kind == "constant":
            matches = True
        elif term.kind == "stay":
            matches = not bit(term.index)
        elif term.kind == "return":
            matches = bit(term.index)
        elif term.kind == "return_site":
            matches = assigned.get(term.index) == term.selector
        elif term.kind == "gate_option":
            matches = gate_option_indices[term.index] == term.selector
        elif term.kind == "stay_pair":
            matches = not bit(term.index) and not bit(term.second_index)
        elif term.kind == "return_pair":
            matches = bit(term.index) and bit(term.second_index)
        else:  # guarded by the DTO, retained for fail-closed direct callers
            raise ValueError(f"unknown forecast kind {term.kind!r}")
        if not matches:
            continue
        contribution = weight * term.nll
        by_depth[term.depth] += contribution
        breakdown[term.category] += contribution
        applied += 1
    return sum(by_depth), tuple(by_depth), breakdown, applied, skipped


def compatible_2d(first: Sequence[float], second: Sequence[float]) -> bool:
    if first[0] == second[0] and first[1] != second[1]:
        return False
    if first[1] == second[1] and first[0] != second[0]:
        return False
    if first[0] < second[0] and first[1] >= second[1]:
        return False
    if first[0] > second[0] and first[1] <= second[1]:
        return False
    if first[2] == second[2] and first[3] != second[3]:
        return False
    if first[3] == second[3] and first[2] != second[2]:
        return False
    if first[2] < second[2] and first[3] >= second[3]:
        return False
    if first[2] > second[2] and first[3] <= second[3]:
        return False
    return True


def _cover(begin: float, end: float, coordinate: float):
    if abs(end - begin) < EPS:
        return (True, None) if abs(coordinate - begin) < EPS else (False, 0.0)
    value = (coordinate - begin) / (end - begin)
    if -EPS <= value <= 1.0 + EPS:
        return True, min(max(value, 0.0), 1.0)
    return False, 0.0


def ghost_hit_atoms(legs: Sequence[Leg], ghosts: Sequence[Ghost]) -> tuple[int, ...]:
    if not legs or not ghosts:
        return ()
    columns = sorted({(leg.source.x, leg.target.x) for leg in legs})
    rows = sorted({(leg.source.y, leg.target.y) for leg in legs})
    x_values = [value for pair in columns for value in pair]
    y_values = [value for pair in rows for value in pair]
    bounds = min(x_values), max(x_values), min(y_values), max(y_values)
    hits = []
    for ghost in ghosts:
        gx, gy = ghost.position.x, ghost.position.y
        if not (bounds[0] - EPS <= gx <= bounds[1] + EPS
                and bounds[2] - EPS <= gy <= bounds[3] + EPS):
            continue
        x_cover = [_cover(a, b, gx) for a, b in columns]
        y_cover = [_cover(a, b, gy) for a, b in rows]
        if any(
            ok_x and ok_y and (
                sx is None or sy is None or abs(sx - sy) < S_TOL)
            for ok_x, sx in x_cover for ok_y, sy in y_cover
        ):
            hits.append(ghost.atom)
    return tuple(hits)


def _phase_adjacency(phase: MovementPhase) -> list[set[int]]:
    legs = phase.legs
    adjacency = [set() for _ in legs]
    vectors = [
        (leg.source.x, leg.target.x, leg.source.y, leg.target.y)
        for leg in legs]
    for i in range(len(legs)):
        for j in range(i + 1, len(legs)):
            conflict = not compatible_2d(vectors[i], vectors[j])
            if not conflict and phase.ghosts:
                skipped = ({phase.owners[i], phase.owners[j]}
                           if phase.owners else set())
                ghosts = tuple(g for g in phase.ghosts if g.atom not in skipped)
                conflict = bool(ghost_hit_atoms((legs[i], legs[j]), ghosts))
            if conflict:
                adjacency[i].add(j)
                adjacency[j].add(i)
    return adjacency


def _exact_colors(adjacency: Sequence[set[int]], upper_bound: int
                  ) -> list[int] | None:
    colors = [-1] * len(adjacency)
    neighbor_counts: list[dict[int, int]] = [dict() for _ in adjacency]
    best = upper_bound
    best_colors = None
    budget = 200_000

    def search(used: int) -> None:
        nonlocal best, best_colors, budget
        if used >= best or budget <= 0:
            return
        budget -= 1
        remaining = [v for v, color in enumerate(colors) if color < 0]
        if not remaining:
            best = used
            best_colors = colors[:]
            return
        vertex = max(
            remaining,
            key=lambda v: (len(neighbor_counts[v]), len(adjacency[v]), -v),
        )
        for color in range(min(used, best - 1) + 1):
            if color in neighbor_counts[vertex]:
                continue
            colors[vertex] = color
            for neighbor in adjacency[vertex]:
                neighbor_counts[neighbor][color] = (
                    neighbor_counts[neighbor].get(color, 0) + 1)
            search(max(used, color + 1))
            for neighbor in adjacency[vertex]:
                neighbor_counts[neighbor][color] -= 1
                if not neighbor_counts[neighbor][color]:
                    del neighbor_counts[neighbor][color]
            colors[vertex] = -1

    search(0)
    return best_colors


def color_phase(phase: MovementPhase,
                exact_threshold: int = 0) -> tuple[tuple[int, ...], ...]:
    if not phase.legs:
        return ()
    adjacency = _phase_adjacency(phase)
    colors = [-1] * len(phase.legs)
    neighbor_colors = [set() for _ in phase.legs]
    for _ in phase.legs:
        vertex = max(
            (v for v in range(len(phase.legs)) if colors[v] < 0),
            key=lambda v: (len(neighbor_colors[v]), len(adjacency[v]),
                           phase.legs[v].distance_um, -v),
        )
        color = 0
        while color in neighbor_colors[vertex]:
            color += 1
        colors[vertex] = color
        for neighbor in adjacency[vertex]:
            neighbor_colors[neighbor].add(color)
    used = max(colors) + 1
    if exact_threshold and len(phase.legs) <= exact_threshold and used > 1:
        exact = _exact_colors(adjacency, used)
        if exact is not None:
            colors = exact
    classes: dict[int, list[int]] = {}
    for vertex, color in enumerate(colors):
        classes.setdefault(color, []).append(vertex)
    batches = [
        tuple(sorted(members,
                     key=lambda v: (-phase.legs[v].distance_um, v)))
        for members in classes.values()]
    return tuple(sorted(
        batches,
        key=lambda members: -max(phase.legs[v].distance_um for v in members),
    ))


def _expanded_batch_time(legs: Sequence[Leg], members: Sequence[int]) -> float:
    selected = [legs[index] for index in members]
    if not selected:
        return 0.0
    if len(selected) == 1:
        return 2 * T_TRANSFER_US + sqrt(
            dist((selected[0].source.x, selected[0].source.y),
                 (selected[0].target.x, selected[0].target.y))
            / ACCEL_UM_PER_US2)
    rows: dict[float, list[Leg]] = {}
    for leg in selected:
        rows.setdefault(leg.source.y, []).append(leg)
    ordered_rows = sorted(rows.items())
    row_count = len(ordered_rows)
    duration = (row_count + 1) * T_TRANSFER_US
    if row_count > 1:
        duration += (row_count - 1) * sqrt(sqrt(2.0) / ACCEL_UM_PER_US2)
    row_moves = []
    last_row_for_x: dict[float, int] = {}
    target_x_for_source: dict[float, float] = {}
    for row_index, (source_y, row_legs) in enumerate(ordered_rows):
        row_moves.append((
            source_y + (1.0 if row_index < row_count - 1 else 0.0),
            row_legs[0].target.y,
        ))
        for leg in row_legs:
            last_row_for_x[leg.source.x] = row_index
            target_x_for_source.setdefault(leg.source.x, leg.target.x)
    column_moves = [
        (source_x + (1.0 if last_row_for_x[source_x] < row_count - 1 else 0.0),
         target_x_for_source[source_x])
        for source_x in sorted(last_row_for_x)]
    longest = max(
        dist((column_begin, row_begin), (column_end, row_end))
        for row_begin, row_end in row_moves
        for column_begin, column_end in column_moves)
    return duration + sqrt(longest / ACCEL_UM_PER_US2)


def _single_leg_violation(phase: MovementPhase) -> tuple[int, ...]:
    hits = set()
    for index, leg in enumerate(phase.legs):
        owner = phase.owners[index] if phase.owners else None
        ghosts = tuple(g for g in phase.ghosts if g.atom != owner)
        hits.update(ghost_hit_atoms((leg,), ghosts))
    return tuple(sorted(hits))


def evaluate_candidate(problem: BoundaryProblem, candidate: CandidatePlan,
                       config: BoundaryConfig | None = None) -> FitnessResult:
    config = config or BoundaryConfig()
    phase_batches = []
    move_time = 0.0
    total_distance = 0.0
    movers = 0
    coherence_nll = -candidate.idle_exposures * log1p(-T_RYDBERG_US / T2_US)
    for phase_index, phase in enumerate(candidate.phases):
        violations = (_single_leg_violation(phase)
                      if config.enforce_single_leg_ghost else ())
        if violations:
            return FitnessResult(
                candidate.chromosome, False, inf, inf, inf, inf, 0, 0.0,
                0.0, candidate.idle_exposures, 0, (),
                f"phase {phase_index} single-leg ghost hit: {list(violations)}")
        batches = color_phase(phase, config.exact_coloring_threshold)
        phase_batches.append(batches)
        phase_time = sum(_expanded_batch_time(phase.legs, batch)
                         for batch in batches)
        phase_movers = len(phase.legs)
        if phase_movers > problem.architecture.n_atoms:
            return FitnessResult(
                candidate.chromosome, False, inf, inf, inf, inf, 0, 0.0,
                0.0, candidate.idle_exposures, 0, (),
                f"phase {phase_index} has more movers than atoms")
        stationary_idle = phase_time
        mover_idle = max(0.0, phase_time - 2 * T_TRANSFER_US)
        if stationary_idle >= T2_US or mover_idle >= T2_US:
            return FitnessResult(
                candidate.chromosome, False, inf, inf, inf, inf,
                sum(len(value) for value in phase_batches),
                move_time + phase_time,
                total_distance + sum(leg.distance_um for leg in phase.legs),
                candidate.idle_exposures, 2 * (movers + phase_movers),
                tuple(phase_batches), "linear coherence model out of domain")
        coherence_nll -= (problem.architecture.n_atoms - phase_movers) * log1p(
            -stationary_idle / T2_US)
        coherence_nll -= phase_movers * log1p(-mover_idle / T2_US)
        move_time += phase_time
        total_distance += sum(leg.distance_um for leg in phase.legs)
        movers += phase_movers
    transfers = 2 * movers
    transfer_nll = -transfers * log(F_TRANSFER)
    idle_nll = -candidate.idle_exposures * log(F_EXC)
    negative_log = transfer_nll + idle_nll + coherence_nll
    return FitnessResult(
        chromosome=candidate.chromosome,
        feasible=True,
        negative_log_fidelity=negative_log,
        transfer_nll=transfer_nll,
        idle_excitation_nll=idle_nll,
        coherence_nll=coherence_nll,
        move_batches=sum(len(value) for value in phase_batches),
        move_time_us=move_time,
        total_distance_um=total_distance,
        idle_exposures=candidate.idle_exposures,
        transfers=transfers,
        phase_batches=tuple(phase_batches),
    )


class ReferenceResidentBackend:
    name = "python-reference-v1"

    def evaluate_many(self, problem: BoundaryProblem,
                      chromosomes: Iterable[Sequence[int]] | None = None,
                      config: BoundaryConfig | None = None
                      ) -> tuple[FitnessResult, ...]:
        config = config or BoundaryConfig()
        return tuple(evaluate_candidate(problem, candidate, config)
                     for candidate in problem.select(chromosomes))

    def solve_boundary(self, problem: BoundaryProblem,
                       config: BoundaryConfig | None = None) -> BoundaryResult:
        config = config or BoundaryConfig()
        started = perf_counter_ns()
        candidates = problem.candidates
        if config.max_unique_evaluations:
            candidates = candidates[:config.max_unique_evaluations]
        fitness_started = perf_counter_ns()
        evaluated = self.evaluate_many(
            problem, (candidate.chromosome for candidate in candidates), config)
        fitness_stopped = perf_counter_ns()
        feasible = [result for result in evaluated if result.feasible]
        if not feasible:
            details = "; ".join(result.error or "infeasible" for result in evaluated)
            raise RuntimeError(f"boundary {problem.boundary_id!r} has no feasible candidate: {details}")
        selection_started = perf_counter_ns()
        winner = min(feasible, key=lambda result: result.objective)
        selection_stopped = perf_counter_ns()
        return BoundaryResult(
            winner=winner,
            evaluated=evaluated,
            evaluations=len(evaluated),
            unique_evaluations=len(evaluated),
            search_kernel_ns=perf_counter_ns() - started,
            backend=self.name,
            fitness_ns=fitness_stopped - fitness_started,
            selection_ns=selection_stopped - selection_started,
            effective_horizon=problem.effective_horizon,
        )
