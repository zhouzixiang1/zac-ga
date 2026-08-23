"""Pure-Python oracle for the native boundary scorer.

This module intentionally does not call :mod:`zzx.algorithm_v2`, ``ghost.py`` or
``zcost.py``.  Differential tests would be much less useful if both sides shared
the same implementation bug.
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from math import dist, floor, inf, isfinite, log, log1p, sqrt
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
    RichH0Result,
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
    breakdown = {
        "residency": 0.0,
        "reentry": 0.0,
        "terminal": 0.0,
        "routing": 0.0,
    }
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


def replay_phase_batches(
        phase: MovementPhase,
        exact_threshold: int = 0,
) -> tuple[tuple[int, ...], ...]:
    """Color, then derive an exact endpoint-precedence order for the batches.

    A mover occupies its source before its batch and its target afterwards.  A
    path that crosses another mover's source therefore requires that mover to
    run first; crossing its target requires the current batch to run first.
    Building both constraints before replay avoids the old greedy failure mode
    where an initially safe long batch could move an atom into the path of the
    only remaining batch.  Combined/static ghost conflicts may disappear after
    splitting a colored batch, so a cyclic or blocked multi-leg batch is split
    deterministically before the phase is declared infeasible.
    """
    if not phase.legs:
        return ()
    batches = [list(value) for value in color_phase(
        phase, exact_threshold)]

    def ordered(candidate_batches: Sequence[Sequence[int]]
                ) -> tuple[int, ...] | None:
        batch_by_owner = {}
        if phase.owners:
            for batch_index, members in enumerate(candidate_batches):
                for leg_index in members:
                    owner = phase.owners[leg_index]
                    if owner in batch_by_owner:
                        raise ValueError("phase owner appears in multiple legs")
                    batch_by_owner[owner] = batch_index

        static = {
            ghost.atom: ghost.position for ghost in phase.ghosts
            if ghost.atom not in batch_by_owner
        }
        outgoing = [set() for _ in candidate_batches]
        blocked = [False] * len(candidate_batches)
        for batch_index, members in enumerate(candidate_batches):
            legs = tuple(phase.legs[index] for index in members)
            owners = ({phase.owners[index] for index in members}
                      if phase.owners else set())
            static_ghosts = tuple(
                Ghost(atom, point) for atom, point in sorted(static.items())
                if atom not in owners)
            if ghost_hit_atoms(legs, static_ghosts):
                blocked[batch_index] = True
                continue
            for leg_index, owner in enumerate(phase.owners):
                other_batch = batch_by_owner[owner]
                if other_batch == batch_index:
                    continue
                leg = phase.legs[leg_index]
                if ghost_hit_atoms(legs, (Ghost(owner, leg.source),)):
                    outgoing[other_batch].add(batch_index)
                if ghost_hit_atoms(legs, (Ghost(owner, leg.target),)):
                    outgoing[batch_index].add(other_batch)

        indegree = [0] * len(candidate_batches)
        for neighbors in outgoing:
            for neighbor in neighbors:
                indegree[neighbor] += 1
        result = []
        remaining = set(range(len(candidate_batches)))
        while remaining:
            ready = next((index for index in sorted(remaining)
                          if not blocked[index] and indegree[index] == 0),
                         None)
            if ready is None:
                return None
            result.append(ready)
            remaining.remove(ready)
            for neighbor in outgoing[ready]:
                indegree[neighbor] -= 1
        return tuple(result)

    while True:
        order = ordered(batches)
        if order is not None:
            return tuple(tuple(batches[index]) for index in order)
        split_index = next(
            (index for index, members in enumerate(batches)
             if len(members) > 1), None)
        if split_index is None:
            raise ValueError("phase has no ghost-safe straight-leg batch order")
        members = batches.pop(split_index)
        batches[split_index:split_index] = [[index] for index in members]


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
        try:
            batches = (replay_phase_batches(
                phase, config.exact_coloring_threshold)
                if config.enforce_single_leg_ghost else
                color_phase(phase, config.exact_coloring_threshold))
        except ValueError as exc:
            return FitnessResult(
                candidate.chromosome, False, inf, inf, inf, inf, 0, 0.0,
                0.0, candidate.idle_exposures, 0, (),
                f"phase {phase_index} {exc}")
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


@dataclass(frozen=True, slots=True)
class _RichGeometry:
    positions_t1: tuple[Point, ...]
    back_legs: tuple[Leg, ...]
    back_owners: tuple[int, ...]
    out_legs: tuple[Leg, ...]
    out_owners: tuple[int, ...]
    ghosts_t0: tuple[Ghost, ...]
    ghosts_t1: tuple[Ghost, ...]
    blockers: tuple[int, ...]
    violations: int
    ghost_violations: int


@dataclass(frozen=True, slots=True)
class _RichEvaluated:
    fitness: FitnessResult
    option_indices: tuple[int, ...]
    assignments: tuple[tuple[int, int, Point], ...]
    reseats: tuple[tuple[int, int, Point], ...]
    assignment_key: tuple[int, ...]
    forecast_nll: float
    search_nll: float
    forecast_by_depth: tuple[float, ...]
    forecast_breakdown: dict[str, float]
    return_assignment_rank: int
    return_assignment_evaluated: int
    current_ghost_rejections: int

    @property
    def objective(self) -> tuple:
        def bucket(value: float, quantum: float) -> int:
            if not isfinite(value):
                return (1 << 63) - 1
            return int(floor(value / quantum + 0.5))

        return (
            bucket(self.search_nll, 1e-12),
            self.fitness.move_batches,
            bucket(self.fitness.move_time_us, 1e-6),
            bucket(self.fitness.total_distance_um, 1e-6),
            self.fitness.chromosome,
            self.assignment_key,
        )


def _rich_points(problem: RichH0Problem) -> tuple[Point, ...]:
    if problem.current_points:
        return problem.current_points
    return tuple(problem.architecture.site_coordinates[site]
                 for site in problem.current_site_ids)


def _gate_targets(problem: RichH0Problem, option) -> tuple[Point, Point]:
    if option.target1 is not None:
        return option.target1, option.target2
    coordinates = problem.architecture.site_coordinates
    return (coordinates[option.target1_site_id],
            coordinates[option.target2_site_id])


def _return_point(problem: RichH0Problem, option) -> Point:
    return (option.point if option.point is not None
            else problem.architecture.site_coordinates[option.site_id])


def _rich_normalize(problem: RichH0Problem,
                    chromosome: Sequence[int]) -> tuple[int, ...]:
    gate_count = len(problem.gate_domains)
    if len(chromosome) != gate_count + len(problem.eligible):
        raise ValueError("rich chromosome length mismatch")
    value = [int(chromosome[index]) % len(problem.gate_domains[index])
             for index in range(gate_count)]
    selected = 0
    for index in range(len(problem.eligible)):
        bit = bool(chromosome[gate_count + index])
        if problem.decision_policy == "always_stay":
            bit = False
        elif problem.decision_policy in {"always_return", "adjacent_only"}:
            bit = True
        elif problem.forced_return_mask[index]:
            bit = True
        value.append(int(bit))
        selected += int(bit)
    if selected < problem.min_returns:
        for index in problem.eviction_order_indices:
            offset = gate_count + index
            if not value[offset]:
                value[offset] = 1
                selected += 1
                if selected == problem.min_returns:
                    break
    return tuple(value)


def _new_ghost_conflict(existing: Sequence[Leg], added: Sequence[Leg],
                        ghosts: Sequence[Ghost]) -> bool:
    if not added or not ghosts:
        return False
    existing_columns = {(leg.source.x, leg.target.x) for leg in existing}
    existing_rows = {(leg.source.y, leg.target.y) for leg in existing}
    added_columns = {(leg.source.x, leg.target.x) for leg in added}
    added_rows = {(leg.source.y, leg.target.y) for leg in added}
    all_columns = existing_columns | added_columns
    all_rows = existing_rows | added_rows

    def compatible_cover(first, second) -> bool:
        ok1, time1 = first
        ok2, time2 = second
        return (ok1 and ok2 and
                (time1 is None or time2 is None
                 or abs(time1 - time2) < S_TOL))

    for ghost in ghosts:
        for column in added_columns:
            x = _cover(*column, ghost.position.x)
            if any(compatible_cover(
                    x, _cover(*row, ghost.position.y))
                   for row in all_rows):
                return True
        for row in added_rows:
            y = _cover(*row, ghost.position.y)
            if any(compatible_cover(
                    _cover(*column, ghost.position.x), y)
                   for column in all_columns):
                return True
    return False


def _rich_decode(problem: RichH0Problem, chromosome: Sequence[int]
                 ) -> tuple[int, ...] | None:
    current = _rich_points(problem)

    def option_geometry(option):
        if not problem.indexed_geometry:
            return option.legs, option.seated_ghosts
        target1, target2 = _gate_targets(problem, option)
        legs = []
        ghosts = []
        for atom, target in ((option.q1, target1), (option.q2, target2)):
            leg = Leg.between(current[atom], target)
            if leg.distance_um > EPS:
                legs.append(leg)
            else:
                ghosts.append(Ghost(atom, target))
        return tuple(legs), tuple(ghosts)

    used_sites: set[int] = set()
    accumulated_legs: list[Leg] = []
    accumulated_ghosts = list(problem.static_ghosts)
    selected: list[int] = []
    for gene, domain in zip(chromosome, problem.gate_domains):
        begin = int(gene)
        choice = None
        for offset in range(len(domain)):
            index = (begin + offset) % len(domain)
            option = domain[index]
            if option.site_id in used_sites:
                continue
            option_legs, _option_ghosts = option_geometry(option)
            if _new_ghost_conflict(
                    accumulated_legs, option_legs, accumulated_ghosts):
                continue
            choice = index
            break
        if choice is None:
            for offset in range(len(domain)):
                index = (begin + offset) % len(domain)
                if domain[index].site_id not in used_sites:
                    choice = index
                    break
        if choice is None:
            return None
        option = domain[choice]
        selected.append(choice)
        used_sites.add(option.site_id)
        option_legs, option_ghosts = option_geometry(option)
        accumulated_legs.extend(option_legs)
        accumulated_ghosts.extend(option_ghosts)
    return tuple(selected)


def _limited_return_domains(problem: RichH0Problem,
                            config: RichSearchConfig):
    limited = []
    for domain in problem.return_domains:
        seen = set()
        values = []
        for option in sorted(domain, key=lambda item: (item.cost, item.site_id)):
            if option.site_id in seen:
                continue
            seen.add(option.site_id)
            values.append(option)
            if len(values) == config.return_candidate_limit:
                break
        limited.append(tuple(values))
    return tuple(limited)


def _rich_return_assignments(problem: RichH0Problem,
                             config: RichSearchConfig,
                             returners: Sequence[int]
                             ) -> tuple[tuple[tuple[int, int, Point], ...], ...]:
    domains = _limited_return_domains(problem, config)
    candidates: list[tuple[float, tuple[int, ...], tuple]] = []

    def visit(row: int, used: set[int], cost: float, values: list) -> None:
        if row == len(returners):
            site_ids = tuple(value[1] for value in values)
            candidates.append((cost, site_ids, tuple(values)))
            return
        eligible_index = returners[row]
        for option in domains[eligible_index]:
            if option.site_id in used:
                continue
            used.add(option.site_id)
            values.append((eligible_index, option.site_id,
                           _return_point(problem, option)))
            visit(row + 1, used, cost + option.cost, values)
            values.pop()
            used.remove(option.site_id)

    visit(0, set(), 0.0, [])
    candidates.sort(key=lambda item: (item[0], item[1]))
    return tuple(item[2] for item in candidates[:config.return_assignment_k])


def _rich_geometry(problem: RichH0Problem,
                   option_indices: Sequence[int],
                   assignments: Sequence[tuple[int, int, Point]],
                   reseats: Sequence[tuple[int, int, Point]]) -> _RichGeometry:
    current = _rich_points(problem)
    positions = list(current)
    back_legs: list[Leg] = []
    back_owners: list[int] = []
    for eligible_index, _site_id, target in (*assignments, *reseats):
        atom = problem.eligible[eligible_index]
        source = current[atom]
        leg = Leg.between(source, target)
        if leg.distance_um > EPS:
            back_legs.append(leg)
            back_owners.append(atom)
        positions[atom] = target

    violations = 0
    blockers: set[int] = set()
    occupancy: dict[tuple[float, float], list[int]] = {}
    for atom, point in enumerate(positions):
        occupancy.setdefault((point.x, point.y), []).append(atom)
    for atoms in occupancy.values():
        if len(atoms) > 1:
            violations += len(atoms) - 1
            blockers.update(atoms)

    participants = set(problem.participants)
    out_legs: list[Leg] = []
    out_owners: list[int] = []
    for domain, option_index in zip(problem.gate_domains, option_indices):
        option = domain[option_index]
        target1, target2 = _gate_targets(problem, option)
        for target in (target1, target2):
            for atom, point in enumerate(positions):
                if atom not in participants and point == target:
                    violations += 1
                    blockers.add(atom)
        for atom, target in ((option.q1, target1), (option.q2, target2)):
            leg = Leg.between(positions[atom], target)
            if leg.distance_um > EPS:
                out_legs.append(leg)
                out_owners.append(atom)
    ghosts_t0 = tuple(Ghost(atom, point)
                      for atom, point in enumerate(current))
    ghosts_t1 = tuple(Ghost(atom, point)
                      for atom, point in enumerate(positions))
    back_movers = set(back_owners)
    out_movers = set(out_owners)
    back_static = tuple(ghost for ghost in ghosts_t0
                        if ghost.atom not in back_movers)
    out_static = tuple(ghost for ghost in ghosts_t1
                       if ghost.atom not in out_movers)
    ghost_violations = 0
    for leg in back_legs:
        hits = ghost_hit_atoms((leg,), back_static)
        ghost_violations += len(hits)
        blockers.update(hits)
    for leg in out_legs:
        hits = ghost_hit_atoms((leg,), out_static)
        ghost_violations += len(hits)
        blockers.update(hits)
    violations += ghost_violations
    return _RichGeometry(
        tuple(positions), tuple(back_legs), tuple(back_owners),
        tuple(out_legs), tuple(out_owners), ghosts_t0, ghosts_t1,
        tuple(sorted(blockers)), violations, ghost_violations)


def _score_rich_geometry(problem: RichH0Problem,
                         config: RichSearchConfig,
                         chromosome: tuple[int, ...],
                         geometry: _RichGeometry,
                         return_count: int,
                         *, enforce_ghost: bool) -> FitnessResult:
    candidate = CandidatePlan(
        chromosome=chromosome,
        phases=(
            MovementPhase(geometry.back_legs, geometry.ghosts_t0,
                          geometry.back_owners),
            MovementPhase(geometry.out_legs, geometry.ghosts_t1,
                          geometry.out_owners),
        ),
        idle_exposures=len(problem.eligible) - return_count,
    )
    return evaluate_candidate(
        BoundaryProblem(problem.architecture, (candidate,)), candidate,
        BoundaryConfig(
            exact_coloring_threshold=config.exact_coloring_threshold,
            enforce_single_leg_ghost=enforce_ghost),
    )


def _rich_reseats(problem: RichH0Problem, config: RichSearchConfig,
                  chromosome: tuple[int, ...], option_indices: tuple[int, ...],
                  assignments: tuple[tuple[int, int, Point], ...],
                  return_count: int) -> tuple[tuple[int, int, Point], ...]:
    gate_count = len(problem.gate_domains)
    movable = {atom: index for index, atom in enumerate(problem.eligible)
               if chromosome[gate_count + index] == 0}
    selected: dict[int, tuple[int, int, Point]] = {}
    geometry = _rich_geometry(problem, option_indices, assignments, ())
    for _ in range(len(movable)):
        if not geometry.violations:
            break
        best = None
        for blocker in geometry.blockers:
            if blocker not in movable:
                continue
            eligible_index = movable[blocker]
            source = _rich_points(problem)[blocker]
            sites = sorted(
                (dist((source.x, source.y), (point.x, point.y)), site_id, point)
                for site_id, point in enumerate(
                    problem.architecture.site_coordinates)
                if site_id not in problem.architecture.storage_site_ids)
            for distance, site_id, point in sites:
                if distance <= EPS:
                    continue
                trial = dict(selected)
                trial[eligible_index] = (eligible_index, site_id, point)
                trial_reseats = tuple(trial[index] for index in sorted(trial))
                trial_geometry = _rich_geometry(
                    problem, option_indices, assignments, trial_reseats)
                if trial_geometry.violations >= geometry.violations:
                    continue
                relaxed = _score_rich_geometry(
                    problem, config, chromosome, trial_geometry, return_count,
                    enforce_ghost=False)
                key = (
                    trial_geometry.violations,
                    relaxed.negative_log_fidelity,
                    relaxed.move_batches,
                    relaxed.move_time_us,
                    relaxed.total_distance_um,
                    blocker,
                    site_id,
                )
                if best is None or key < best[0]:
                    best = (key, eligible_index, trial[eligible_index])
        if best is None:
            break
        selected[best[1]] = best[2]
        geometry = _rich_geometry(
            problem, option_indices, assignments,
            tuple(selected[index] for index in sorted(selected)))
    return tuple(selected[index] for index in sorted(selected))


def _infeasible_rich(chromosome: tuple[int, ...], error: str) -> FitnessResult:
    return FitnessResult(
        chromosome, False, inf, inf, inf, inf, 0, 0.0, 0.0, 0, 0, (), error)


def evaluate_rich_exact_candidate(
        problem: RichH0Problem,
        config: RichSearchConfig,
        raw_chromosome: Sequence[int],
) -> _RichEvaluated:
    """Evaluate one rich chromosome independently of the C++ implementation."""
    chromosome = _rich_normalize(problem, raw_chromosome)
    option_indices = _rich_decode(problem, chromosome)
    if option_indices is None:
        fitness = _infeasible_rich(
            chromosome, "gate menu cannot form an injection")
        return _RichEvaluated(
            fitness, (), (), (), (), inf, inf, (), {}, 0, 0, 0)
    gate_count = len(problem.gate_domains)
    returners = tuple(index for index in range(len(problem.eligible))
                      if chromosome[gate_count + index])
    assignment_candidates = _rich_return_assignments(
        problem, config, returners)
    if not assignment_candidates:
        fitness = _infeasible_rich(
            chromosome, "RETURN matching has no bounded injective assignment")
        return _RichEvaluated(
            fitness, option_indices, (), (), (), inf, inf, (), {}, 0, 0, 0)
    evaluated = []
    ghost_rejections = 0
    for rank, assignments in enumerate(assignment_candidates, 1):
        reseats = _rich_reseats(
            problem, config, chromosome, option_indices, assignments,
            len(returners))
        geometry = _rich_geometry(
            problem, option_indices, assignments, reseats)
        if geometry.violations:
            error = ("unresolved current single-leg ghost hit"
                     if geometry.ghost_violations
                     else "unresolved current gate occupancy")
            fitness = _infeasible_rich(chromosome, error)
            rejected = int(bool(geometry.ghost_violations))
        else:
            fitness = _score_rich_geometry(
                problem, config, chromosome, geometry, len(returners),
                enforce_ghost=config.enforce_single_leg_ghost)
            rejected = int(
                not fitness.feasible and fitness.error is not None
                and "ghost-safe" in fitness.error)
        ghost_rejections += rejected
        assignment_key = tuple(value[1] for value in assignments)
        if reseats:
            assignment_key += (-1,) + tuple(
                component
                for eligible_index, site_id, _point in reseats
                for component in (problem.eligible[eligible_index], site_id))
        if fitness.feasible:
            return_pairs = tuple(
                (problem.eligible[index], site_id)
                for index, site_id, _point in assignments)
            forecast = evaluate_decay_forecast(
                problem, config, chromosome, option_indices, return_pairs)
            forecast_nll, by_depth, breakdown = forecast[:3]
            search_nll = fitness.negative_log_fidelity + forecast_nll
        else:
            forecast_nll, search_nll = 0.0, inf
            by_depth, breakdown = (), {}
        evaluated.append(_RichEvaluated(
            fitness, option_indices, assignments, reseats, assignment_key,
            forecast_nll, search_nll, by_depth, breakdown, rank,
            len(assignment_candidates), rejected))
    winner = min(evaluated, key=lambda value: value.objective)
    return _RichEvaluated(
        winner.fitness, winner.option_indices, winner.assignments,
        winner.reseats, winner.assignment_key, winner.forecast_nll,
        winner.search_nll, winner.forecast_by_depth,
        winner.forecast_breakdown, winner.return_assignment_rank,
        winner.return_assignment_evaluated, ghost_rejections)


def solve_rich_exact_reference(
        problem: RichH0Problem,
        config: RichSearchConfig,
        rng_state: tuple,
) -> RichH0Result:
    """Exhaustive small-boundary semantic truth for native differential tests.

    This intentionally supports only the direct-search contract.  It never
    calls the native module and refuses a boundary larger than the configured
    direct-enumeration and unique-evaluation limits.
    """
    gate_domains = [range(len(domain)) for domain in problem.gate_domains]
    decision_domain = (range(2) if problem.decision_policy == "optimize"
                       else range(1))
    domains = gate_domains + [decision_domain for _ in problem.eligible]
    raw_space = 1
    for domain in domains:
        raw_space *= len(domain)
    if (raw_space > config.direct_enumeration_limit
            or raw_space > config.resolved_unique_budget):
        raise ValueError("reference rich solver only accepts direct boundaries")
    raw_values = product(*domains) if domains else [()]
    chromosomes = tuple(dict.fromkeys(
        _rich_normalize(problem, chromosome) for chromosome in raw_values))
    evaluated = tuple(evaluate_rich_exact_candidate(
        problem, config, chromosome) for chromosome in chromosomes)
    feasible = tuple(value for value in evaluated if value.fitness.feasible)
    if not feasible:
        raise RuntimeError(f"boundary {problem.boundary_id!r} has no feasible candidate")
    winner = min(feasible, key=lambda value: value.objective)
    return RichH0Result(
        winner=winner.fitness,
        gate_option_indices=winner.option_indices,
        return_assignments=tuple(
            (problem.eligible[index], site_id)
            for index, site_id, _point in winner.assignments),
        reseat_assignments=tuple(
            (problem.eligible[index], site_id)
            for index, site_id, _point in winner.reseats),
        rng_state=rng_state,
        search_mode="direct" if raw_space <= 1 else "enumerate",
        operator_profile=config.operator_profile,
        evaluations=len(chromosomes),
        unique_evaluations=len(chromosomes),
        deterministic_unique_evaluations=len(chromosomes),
        stochastic_unique_evaluations=0,
        fitness_hits=0,
        decode_hits=0,
        return_match_hits=0,
        generations=0,
        early_stopped=False,
        stochastic_budget=config.resolved_unique_budget,
        early_stop_reason="direct" if raw_space <= 1 else "enumerate",
        operator_stats={},
        forecast_terms_applied=0,
        forecast_terms_skipped_cutoff=0,
        forecast_nll=winner.forecast_nll,
        search_negative_log_fidelity=winner.search_nll,
        forecast_by_depth=winner.forecast_by_depth,
        forecast_breakdown=winner.forecast_breakdown,
        return_assignment_rank=winner.return_assignment_rank,
        return_assignment_evaluated=winner.return_assignment_evaluated,
        current_ghost_rejections=winner.current_ghost_rejections,
        future_ghost_cost=winner.forecast_breakdown.get("routing", 0.0),
        pre_score_reseats=len(winner.reseats),
        timing={},
    )


class ReferenceResidentBackend:
    name = "python-reference-v1"

    def solve_rich_boundary(
            self,
            problem: RichH0Problem,
            config: RichSearchConfig,
            rng_state: tuple,
            *,
            cached_winner: Sequence[int] | None = None,
    ) -> RichH0Result:
        """Exact Python truth for bounded rich boundaries.

        The reference backend intentionally refuses GA-sized spaces.  Its
        purpose after native search-operator tuning is semantic differential
        testing: both backends consume the same DTO and exhaust the same small
        chromosome space, while production-sized boundaries remain native-only.
        A cached approximate winner would change that contract and is therefore
        not accepted on this path.
        """
        if cached_winner is not None:
            raise ValueError(
                "exact rich reference does not accept an approximate LRU winner")
        return solve_rich_exact_reference(problem, config, rng_state)

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
