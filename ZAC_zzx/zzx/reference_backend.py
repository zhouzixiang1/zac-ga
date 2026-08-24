"""Pure-Python oracle for the native boundary scorer.

This module intentionally does not call :mod:`zzx.algorithm_v2`, ``ghost.py`` or
``zcost.py``.  Differential tests would be much less useful if both sides shared
the same implementation bug.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
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
    Point,
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
T_ONE_Q_US = 52.0
ACCEL_UM_PER_US2 = 0.00275
T2_US = 1.5e6


def evaluate_decay_forecast(
        problem: RichH0Problem,
        config: RichSearchConfig,
        chromosome: Sequence[int],
        gate_option_indices: Sequence[int],
        return_assignments: Sequence[tuple[int, int]],
) -> tuple[float, tuple[float, ...], dict[str, float], int, int]:
    """Independent Python oracle for the legacy bounded-decay term table."""
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
    if len(legs) == 1:
        # The general combined-ghost test constructs the Cartesian product of
        # all distinct row and column trajectories.  For one leg that product
        # has exactly one element, so evaluate it directly.  Future rollout
        # endpoint replay performs this check millions of times; avoiding the
        # temporary sets/lists is an exact fast path, not a geometric proxy.
        leg = legs[0]
        min_x, max_x = sorted((leg.source.x, leg.target.x))
        min_y, max_y = sorted((leg.source.y, leg.target.y))
        hits = []
        for ghost in ghosts:
            gx, gy = ghost.position.x, ghost.position.y
            if not (min_x - EPS <= gx <= max_x + EPS
                    and min_y - EPS <= gy <= max_y + EPS):
                continue
            ok_x, sx = _cover(leg.source.x, leg.target.x, gx)
            ok_y, sy = _cover(leg.source.y, leg.target.y, gy)
            if (ok_x and ok_y and
                    (sx is None or sy is None or abs(sx - sy) < S_TOL)):
                hits.append(ghost.atom)
        return tuple(hits)
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


def _expanded_batch_time(
        legs: Sequence[Leg], members: Sequence[int], *,
        transfer_us: float = T_TRANSFER_US,
        acceleration: float = ACCEL_UM_PER_US2,
) -> float:
    selected = [legs[index] for index in members]
    if not selected:
        return 0.0
    if len(selected) == 1:
        return 2 * transfer_us + sqrt(
            dist((selected[0].source.x, selected[0].source.y),
                 (selected[0].target.x, selected[0].target.y))
            / acceleration)
    rows: dict[float, list[Leg]] = {}
    for leg in selected:
        rows.setdefault(leg.source.y, []).append(leg)
    ordered_rows = sorted(rows.items())
    row_count = len(ordered_rows)
    duration = (row_count + 1) * transfer_us
    if row_count > 1:
        duration += (row_count - 1) * sqrt(sqrt(2.0) / acceleration)
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
    return duration + sqrt(longest / acceleration)


@dataclass(frozen=True, slots=True)
class _ExecutableBatch:
    original_members: tuple[int, ...]
    legs: tuple[Leg, ...]
    owners: tuple[int, ...]


def _first_ghost_tracks(
        legs: Sequence[Leg], owners: Sequence[int],
        positions: dict[int, Point],
) -> tuple[tuple[float, float], tuple[float, float]] | None:
    if not legs:
        return None
    moving = set(owners)
    columns = sorted({(leg.source.x, leg.target.x) for leg in legs})
    rows = sorted({(leg.source.y, leg.target.y) for leg in legs})
    x_values = [value for pair in columns for value in pair]
    y_values = [value for pair in rows for value in pair]
    bounds = min(x_values), max(x_values), min(y_values), max(y_values)
    for atom, point in sorted(positions.items()):
        if atom in moving or not (
                bounds[0] - EPS <= point.x <= bounds[1] + EPS
                and bounds[2] - EPS <= point.y <= bounds[3] + EPS):
            continue
        for column in columns:
            ok_x, sx = _cover(*column, point.x)
            if not ok_x:
                continue
            for row in rows:
                ok_y, sy = _cover(*row, point.y)
                if (ok_y and (sx is None or sy is None
                              or abs(sx - sy) < S_TOL)):
                    return column, row
    return None


def _expanded_batch_conflict_members(
        phase: MovementPhase, members: Sequence[int],
        replay_positions: dict[int, Point],
) -> set[int]:
    member_by_owner = {phase.owners[index]: index for index in members}
    if len(member_by_owner) != len(members):
        raise ValueError("expanded replay repeats a phase owner")
    source = {phase.owners[index]: phase.legs[index].source
              for index in members}
    target = {phase.owners[index]: phase.legs[index].target
              for index in members}
    rows: dict[float, list[int]] = {}
    for owner in sorted(member_by_owner):
        rows.setdefault(source[owner].y, []).append(owner)
    physical = dict(source)
    held: set[int] = set()
    activated_columns: set[float] = set()

    def audit_move(next_positions: dict[int, Point]) -> set[int]:
        detail_owners = tuple(
            owner for owner in sorted(held)
            if physical[owner] != next_positions[owner])
        detail_legs = tuple(Leg.between(
            physical[owner], next_positions[owner])
            for owner in detail_owners)
        vectors = tuple((
            leg.source.x, leg.target.x, leg.source.y, leg.target.y)
            for leg in detail_legs)
        for left in range(len(vectors)):
            for right in range(left + 1, len(vectors)):
                if not compatible_2d(vectors[left], vectors[right]):
                    return {
                        member_by_owner[detail_owners[left]],
                        member_by_owner[detail_owners[right]],
                    }
        positions = dict(replay_positions)
        positions.update(physical)
        hit = _first_ghost_tracks(detail_legs, detail_owners, positions)
        if hit is None:
            return set()
        column, row = hit
        bad = {
            member_by_owner[owner]
            for owner, leg in zip(detail_owners, detail_legs)
            if (leg.source.x, leg.target.x) == column
            or (leg.source.y, leg.target.y) == row
        }
        return bad or ({member_by_owner[detail_owners[0]]}
                       if detail_owners else set())

    ordered_rows = sorted(rows.items())
    for row_index, (source_y, row_owners) in enumerate(ordered_rows):
        shifted = dict(physical)
        for owner in row_owners:
            source_x = source[owner].x
            if source_x not in activated_columns:
                continue
            for held_owner in held:
                if source[held_owner].x == source_x:
                    shifted[held_owner] = Point(
                        source_x, shifted[held_owner].y)
        bad = audit_move(shifted)
        if bad:
            return bad
        physical = shifted
        held.update(row_owners)
        activated_columns.update(source[owner].x for owner in row_owners)
        if row_index + 1 < len(ordered_rows):
            parked_columns = {source[owner].x for owner in row_owners}
            parked = dict(physical)
            for owner in held:
                point = parked[owner]
                parked[owner] = Point(
                    point.x + (1.0 if source[owner].x in parked_columns else 0.0),
                    source_y + 1.0 if owner in row_owners else point.y,
                )
            bad = audit_move(parked)
            if bad:
                return bad
            physical = parked
    final = dict(physical)
    final.update({owner: target[owner] for owner in held})
    return audit_move(final)


def _production_replay_phase_batches(
        phase: MovementPhase, exact_threshold: int,
) -> tuple[_ExecutableBatch, ...]:
    if len(phase.owners) != len(phase.legs):
        raise ValueError("production replay requires one owner per movement leg")
    canonical_to_original = tuple(sorted(
        range(len(phase.legs)),
        key=lambda index: phase.legs[index].distance_um,
        reverse=True,
    ))
    routed = MovementPhase(
        tuple(phase.legs[index] for index in canonical_to_original),
        phase.ghosts,
        tuple(phase.owners[index] for index in canonical_to_original),
        phase.batching,
    )
    positions = {ghost.atom: ghost.position for ghost in routed.ghosts}
    if len(positions) != len(routed.ghosts):
        raise ValueError("production replay repeats a ghost atom")
    positions.update({owner: routed.legs[index].source
                      for index, owner in enumerate(routed.owners)})

    def executable(members: Sequence[int]) -> _ExecutableBatch:
        return _ExecutableBatch(
            tuple(canonical_to_original[index] for index in members),
            tuple(routed.legs[index] for index in members),
            tuple(routed.owners[index] for index in members),
        )

    def audit_pass(input_batches, clean, deferred):
        queue = [list(batch) for batch in input_batches]
        while queue:
            pending = queue.pop(0)
            while True:
                legs = tuple(routed.legs[index] for index in pending)
                owners = tuple(routed.owners[index] for index in pending)
                hit = _first_ghost_tracks(legs, owners, positions)
                if hit is None:
                    break
                if len(pending) == 1:
                    deferred.append(pending[0])
                    pending = []
                    break
                column, row = hit
                bad = {
                    index for index in pending
                    if (routed.legs[index].source.x,
                        routed.legs[index].target.x) == column
                    or (routed.legs[index].source.y,
                        routed.legs[index].target.y) == row
                } or {pending[0]}
                deferred.extend(sorted(bad))
                pending = [index for index in pending if index not in bad]
                if not pending:
                    break
            if not pending:
                continue
            expanded_bad = _expanded_batch_conflict_members(
                routed, pending, positions)
            if expanded_bad:
                if len(pending) == 1:
                    deferred.append(pending[0])
                    continue
                keep = [index for index in pending
                        if index not in expanded_bad]
                bad = [index for index in pending
                       if index in expanded_bad]
                if keep and bad:
                    queue.insert(0, keep)
                    deferred.extend(bad)
                    continue
                row_groups: dict[float, list[int]] = {}
                for index in pending:
                    row_groups.setdefault(
                        routed.legs[index].source.y, []).append(index)
                groups = [row_groups[key] for key in sorted(row_groups)]
                if len(groups) == 1:
                    groups = [[index] for index in pending]
                queue[0:0] = groups
                continue
            clean.append(executable(pending))
            for index in pending:
                positions[routed.owners[index]] = routed.legs[index].target

    clean: list[_ExecutableBatch] = []
    deferred: list[int] = []
    audit_pass(color_phase(routed, exact_threshold), clean, deferred)
    for round_index in range(3):
        if not deferred:
            break
        subset = sorted(set(deferred))
        if round_index == 2:
            batches = [(index,) for index in subset]
        else:
            local_phase = MovementPhase(
                tuple(routed.legs[index] for index in subset), (), ())
            batches = tuple(tuple(subset[index] for index in batch)
                            for batch in color_phase(
                                local_phase, exact_threshold))
        deferred = []
        audit_pass(batches, clean, deferred)
    if deferred:
        raise ValueError("production route has unresolved ghost batches")
    return tuple(clean)


def _single_leg_violation(phase: MovementPhase) -> tuple[int, ...]:
    hits = set()
    for index, leg in enumerate(phase.legs):
        owner = phase.owners[index] if phase.owners else None
        ghosts = tuple(g for g in phase.ghosts if g.atom != owner)
        hits.update(ghost_hit_atoms((leg,), ghosts))
    return tuple(sorted(hits))


def _linear_coherence_delta_nll(
        prior_idle_time_us: Sequence[float],
        candidate_idle_time_us: Sequence[float]) -> float:
    """Return the exact incremental NLL of the final linear T2 model.

    The final scorer evaluates one factor ``1 - t_q/T2`` per atom.  Movement
    phases therefore accumulate into one ``dt_q`` before the log-ratio is
    taken; summing an independent ``-log(1-dt_phase/T2)`` per phase is merely a
    first-order approximation once an atom already has idle time.
    """
    if len(prior_idle_time_us) != len(candidate_idle_time_us):
        raise ValueError("coherence vectors differ in length")
    total = 0.0
    for prior, delta in zip(prior_idle_time_us, candidate_idle_time_us):
        after = prior + delta
        if prior >= T2_US or after >= T2_US:
            return inf
        if delta < -1e-12:
            raise ValueError("candidate idle-time delta must be non-negative")
        total += log1p(-prior / T2_US) - log1p(-after / T2_US)
    return total


def evaluate_candidate(problem: BoundaryProblem, candidate: CandidatePlan,
                       config: BoundaryConfig | None = None) -> FitnessResult:
    """Evaluate the candidate-dependent physical increment.

    ABI7 uses the exact per-atom coherence log-ratio against the accumulated
    boundary-entry idle times.  Target-CZ and already-scheduled 1Q durations are
    omitted because they are identical for every chromosome at this boundary;
    the scheduler commits those common terms once.  Location-dependent idle
    excitation remains in ``idle_exposures``.

    Owner-less phases are retained only for non-formal, empty-prior source
    compatibility.  A formal ABI7 DTO supplies all prior times and one owner
    per movement leg and fails closed otherwise.
    """
    config = config or BoundaryConfig()
    phase_batches = []
    move_time = 0.0
    total_distance = 0.0
    movers = 0
    exact_owners = all(not phase.legs or bool(phase.owners)
                       for phase in candidate.phases)
    if problem.prior_idle_time_us and not exact_owners:
        raise ValueError(
            "formal coherence accounting requires one owner per movement leg")
    prior_idle = (problem.prior_idle_time_us or
                  (0.0,) * problem.architecture.n_atoms)
    candidate_idle = [0.0] * problem.architecture.n_atoms
    # Legacy owner-less fixtures retain their aggregate pre-ABI7 approximation.
    coherence_nll = (
        0.0 if exact_owners else
        -candidate.idle_exposures * log1p(-T_RYDBERG_US / T2_US))
    for phase_index, phase in enumerate(candidate.phases):
        try:
            if (exact_owners and config.enforce_single_leg_ghost
                    and config.production_parking_replay):
                executable_batches = _production_replay_phase_batches(
                    phase, config.exact_coloring_threshold)
                batches = tuple(batch.original_members
                                for batch in executable_batches)
            else:
                batches = (replay_phase_batches(
                    phase, config.exact_coloring_threshold)
                    if config.enforce_single_leg_ghost else
                    color_phase(phase, config.exact_coloring_threshold))
                executable_batches = tuple(_ExecutableBatch(
                    tuple(batch), tuple(phase.legs[index] for index in batch),
                    tuple(phase.owners[index] for index in batch)
                    if phase.owners else ()) for batch in batches)
        except ValueError as exc:
            return FitnessResult(
                candidate.chromosome, False, inf, inf, inf, inf, 0, 0.0,
                0.0, candidate.idle_exposures, 0, (),
                f"phase {phase_index} {exc}")
        phase_batches.append(batches)
        phase_time = sum(_expanded_batch_time(
            batch.legs, range(len(batch.legs)))
            for batch in executable_batches)
        phase_movers = (sum(len(batch.owners) for batch in executable_batches)
                        if exact_owners else len(phase.legs))
        if phase_movers > problem.architecture.n_atoms:
            return FitnessResult(
                candidate.chromosome, False, inf, inf, inf, inf, 0, 0.0,
                0.0, candidate.idle_exposures, 0, (),
                f"phase {phase_index} has more movers than atoms")
        stationary_idle = phase_time
        mover_idle = max(0.0, phase_time - 2 * T_TRANSFER_US)
        if exact_owners:
            if len(set(phase.owners)) != len(phase.owners):
                raise ValueError("a movement phase cannot repeat an owner")
            if any(owner < 0 or owner >= problem.architecture.n_atoms
                   for owner in phase.owners):
                raise ValueError("movement owner is outside the architecture")
            for batch in executable_batches:
                batch_time = _expanded_batch_time(
                    batch.legs, range(len(batch.legs)))
                for atom in range(problem.architecture.n_atoms):
                    candidate_idle[atom] += batch_time
                for owner in batch.owners:
                    candidate_idle[owner] -= 2 * T_TRANSFER_US
            updated_coherence = _linear_coherence_delta_nll(
                prior_idle, candidate_idle)
        else:
            updated_coherence = (
                inf if stationary_idle >= T2_US or mover_idle >= T2_US else
                coherence_nll
                - (problem.architecture.n_atoms - phase_movers) * log1p(
                    -stationary_idle / T2_US)
                - phase_movers * log1p(-mover_idle / T2_US))
        if not isfinite(updated_coherence):
            return FitnessResult(
                candidate.chromosome, False, inf, inf, inf, inf,
                sum(len(value) for value in phase_batches),
                move_time + phase_time,
                total_distance + sum(
                    leg.distance_um for batch in executable_batches
                    for leg in batch.legs),
                candidate.idle_exposures, 2 * (movers + phase_movers),
                tuple(phase_batches), "linear coherence model out of domain")
        coherence_nll = updated_coherence
        move_time += phase_time
        total_distance += sum(
            leg.distance_um for batch in executable_batches
            for leg in batch.legs)
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
        candidate_idle_time_us=(tuple(candidate_idle) if exact_owners else ()),
    )


@dataclass(frozen=True, slots=True)
class _RichGeometry:
    positions_t1: tuple[Point, ...]
    site_ids_t1: tuple[int, ...]
    final_site_ids: tuple[int, ...]
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
    site_ids = list(problem.current_site_ids)
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
        if site_ids:
            site_ids[atom] = int(_site_id)

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
    final_site_ids = list(site_ids)
    if final_site_ids:
        for domain, option_index in zip(problem.gate_domains, option_indices):
            option = domain[option_index]
            if (option.target1_site_id is None
                    or option.target2_site_id is None):
                raise ValueError("exact indexed geometry lacks target site ids")
            final_site_ids[option.q1] = option.target1_site_id
            final_site_ids[option.q2] = option.target2_site_id
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
        tuple(positions), tuple(site_ids), tuple(final_site_ids),
        tuple(back_legs), tuple(back_owners),
        tuple(out_legs), tuple(out_owners), ghosts_t0, ghosts_t1,
        tuple(sorted(blockers)), violations, ghost_violations)


def _score_exact_current_scheduler(
        problem: RichH0Problem,
        config: RichSearchConfig,
        score: FitnessResult,
        candidate: CandidatePlan,
        geometry: _RichGeometry,
) -> FitnessResult:
    """Independent compact replay of the ABI7 current scheduler suffix.

    This intentionally mirrors resource/dependency semantics instead of calling
    the production router.  Tests compare both implementations to make the C++
    hot path and final emitted trace fail independently.
    """
    if len(problem.scheduler_aod_end_us) != 1 or \
            len(problem.scheduler_rydberg_end_us) != 1:
        raise ValueError("ABI7 reference currently requires one AOD/Rydberg zone")
    n_atoms = problem.architecture.n_atoms
    active = list(problem.scheduler_active_union_us)
    qubit_dependency = list(problem.scheduler_qubit_dependency_end_us)
    current_site_ids = list(problem.current_site_ids)
    aod_end = problem.scheduler_aod_end_us[0]
    one_qubit_end = problem.scheduler_one_qubit_end_us
    rydberg_end = problem.scheduler_rydberg_end_us[0]
    trace_end = problem.scheduler_trace_end_us
    site_dependency = dict(zip(
        problem.scheduler_site_dependency_site_ids,
        problem.scheduler_site_dependency_activation_finish_us))
    site_id_by_point = {
        point: site_id for site_id, point in enumerate(
            problem.architecture.site_coordinates)}
    transfer_us = problem.scheduler_transfer_duration_us
    acceleration = problem.scheduler_accel_um_per_us2
    rydberg_us = problem.scheduler_rydberg_duration_us
    one_qubit_us = problem.scheduler_one_qubit_duration_us
    one_qubit_common_us = problem.scheduler_one_qubit_common_us
    coherence_t2_us = problem.coherence_t2_us
    model_move_time_us = 0.0

    def schedule_phase(phase_index: int, target_sites: Sequence[int],
                       source_back: bool) -> None:
        nonlocal aod_end, trace_end, model_move_time_us
        phase = candidate.phases[phase_index]
        if len(target_sites) != n_atoms or len(phase.owners) != len(phase.legs):
            raise ValueError("ABI7 exact phase geometry is incomplete")
        executable_batches = _production_replay_phase_batches(
            phase, config.exact_coloring_threshold)
        if tuple(batch.original_members for batch in executable_batches) != \
                score.phase_batches[phase_index]:
            raise ValueError("ABI7 executable batch audit payload drift")
        for batch in executable_batches:
            rows = {leg.source.y for leg in batch.legs}
            duration = _expanded_batch_time(
                batch.legs, range(len(batch.legs)),
                transfer_us=transfer_us, acceleration=acceleration)
            model_move_time_us += duration
            activation_finish_offset = (
                len(rows) * transfer_us
                + max(0, len(rows) - 1)
                * sqrt(sqrt(2.0) / acceleration))
            deactivation_offset = duration - transfer_us
            begin = aod_end
            batch_target_sites = []
            for owner, leg in zip(batch.owners, batch.legs):
                begin = max(
                    begin,
                    (problem.scheduler_back_dependency_end_us[owner]
                     if source_back else qubit_dependency[owner]))
                try:
                    target_site = site_id_by_point[leg.target]
                except KeyError as exc:
                    raise ValueError(
                        "ABI7 executable batch target is not an SLM site") from exc
                batch_target_sites.append(target_site)
                prior_site = site_dependency.get(target_site)
                if prior_site is not None:
                    begin = max(begin, prior_site - deactivation_offset)
            end = begin + duration
            activation_finish = begin + activation_finish_offset
            for owner, target_site in zip(
                    batch.owners, batch_target_sites):
                active[owner] += 2 * transfer_us
                qubit_dependency[owner] = end
                site_dependency[current_site_ids[owner]] = activation_finish
                current_site_ids[owner] = target_site
            aod_end = end
            trace_end = max(trace_end, end)

    schedule_phase(0, geometry.site_ids_t1, True)
    schedule_phase(1, geometry.final_site_ids, False)

    participants = set(problem.participants)
    zone_sites = {
        site for pair in problem.architecture.entangling_site_pairs
        for site in pair
    }
    if problem.gate_domains:
        before_gate_dependency = list(qubit_dependency)
        target_one_qubit = set(problem.target_one_qubit_atoms)
        cz_begin = rydberg_end
        for atom in range(n_atoms):
            in_zone = current_site_ids[atom] in zone_sites
            if atom in participants or (
                    in_zone and atom not in target_one_qubit):
                cz_begin = max(cz_begin, before_gate_dependency[atom])
        cz_end = cz_begin + rydberg_us
        for atom in participants:
            active[atom] += rydberg_us
        rydberg_end = cz_end
        trace_end = max(trace_end, cz_end)

        if problem.target_one_qubit_atoms:
            one_qubit_begin = one_qubit_end
            for atom in problem.target_one_qubit_atoms:
                one_qubit_begin = max(
                    one_qubit_begin,
                    cz_end if atom in participants
                    else before_gate_dependency[atom])
            cursor = one_qubit_begin
            for atom in problem.target_one_qubit_atoms:
                active[atom] += one_qubit_us
                cursor += one_qubit_us
            one_qubit_end = cursor + one_qubit_common_us
            trace_end = max(trace_end, one_qubit_end)

        for atom in range(n_atoms):
            if atom in target_one_qubit:
                qubit_dependency[atom] = one_qubit_end
            elif atom in participants or current_site_ids[atom] in zone_sites:
                qubit_dependency[atom] = max(
                    before_gate_dependency[atom], cz_end)

    idle_after = tuple(max(0.0, trace_end - value) for value in active)
    coherence_nll = 0.0
    for before, after in zip(problem.prior_idle_time_us, idle_after):
        if before >= coherence_t2_us or after >= coherence_t2_us:
            return FitnessResult(
                chromosome=score.chromosome, feasible=False,
                negative_log_fidelity=inf, transfer_nll=inf,
                idle_excitation_nll=inf, coherence_nll=inf,
                move_batches=score.move_batches,
                move_time_us=model_move_time_us,
                total_distance_um=score.total_distance_um,
                idle_exposures=score.idle_exposures,
                transfers=score.transfers,
                phase_batches=score.phase_batches,
                error="linear coherence model out of domain")
        coherence_nll += (
            log1p(-before / coherence_t2_us)
            - log1p(-after / coherence_t2_us))
    idle_exposures = sum(
        atom not in participants and current_site_ids[atom] in zone_sites
        for atom in range(n_atoms))
    transfer_nll = -score.transfers * log(F_TRANSFER)
    idle_nll = -idle_exposures * log(F_EXC)
    return FitnessResult(
        chromosome=score.chromosome,
        feasible=True,
        negative_log_fidelity=transfer_nll + idle_nll + coherence_nll,
        transfer_nll=transfer_nll,
        idle_excitation_nll=idle_nll,
        coherence_nll=coherence_nll,
        move_batches=score.move_batches,
        move_time_us=model_move_time_us,
        total_distance_um=score.total_distance_um,
        idle_exposures=idle_exposures,
        transfers=score.transfers,
        phase_batches=score.phase_batches,
        candidate_idle_time_us=tuple(
            after - before for before, after in zip(
                problem.prior_idle_time_us, idle_after)),
    )


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
    if problem.exact_current_scheduler:
        score = evaluate_candidate(
            BoundaryProblem(problem.architecture, (candidate,)),
            candidate,
            BoundaryConfig(
                exact_coloring_threshold=config.exact_coloring_threshold,
                enforce_single_leg_ghost=enforce_ghost,
                production_parking_replay=True),
        )
        if not score.feasible:
            return score
        try:
            return _score_exact_current_scheduler(
                problem, config, score, candidate, geometry)
        except ValueError as exc:
            # The production router would require an explicit two-leg waypoint.
            # Until that payload is native-owned, treat this chromosome as
            # infeasible exactly like the C++ compact replay; never abort the
            # whole reference search because one repair candidate needs it.
            return _infeasible_rich(chromosome, str(exc))
    participant_set = set(problem.participants)
    pulse_idle = tuple(
        (0.0 if atom in participant_set else T_RYDBERG_US)
        if problem.gate_domains else 0.0
        for atom in range(problem.architecture.n_atoms))
    prior_idle = (problem.prior_idle_time_us or
                  (0.0,) * problem.architecture.n_atoms)
    scoring_prior = tuple(
        prior + pulse for prior, pulse in zip(prior_idle, pulse_idle))
    score = evaluate_candidate(
        BoundaryProblem(
            problem.architecture,
            (candidate,),
            prior_idle_time_us=scoring_prior,
        ),
        candidate,
        BoundaryConfig(
            exact_coloring_threshold=config.exact_coloring_threshold,
            enforce_single_leg_ghost=enforce_ghost),
    )
    pulse_nll = _linear_coherence_delta_nll(prior_idle, pulse_idle)
    if not score.feasible or not isfinite(pulse_nll):
        if score.feasible:
            return FitnessResult(
                score.chromosome, False, inf, inf, inf, inf,
                score.move_batches, score.move_time_us,
                score.total_distance_um, score.idle_exposures,
                score.transfers, score.phase_batches,
                "linear coherence model out of domain")
        return score
    total_idle = tuple(
        pulse + movement
        for pulse, movement in zip(
            pulse_idle, score.candidate_idle_time_us))
    return FitnessResult(
        chromosome=score.chromosome,
        feasible=True,
        negative_log_fidelity=score.negative_log_fidelity + pulse_nll,
        transfer_nll=score.transfer_nll,
        idle_excitation_nll=score.idle_excitation_nll,
        coherence_nll=score.coherence_nll + pulse_nll,
        move_batches=score.move_batches,
        move_time_us=score.move_time_us,
        total_distance_um=score.total_distance_um,
        idle_exposures=score.idle_exposures,
        transfers=score.transfers,
        phase_batches=score.phase_batches,
        candidate_idle_time_us=total_idle,
    )


def _evaluate_native_future_rollout(
        problem: RichH0Problem,
        config: RichSearchConfig,
        option_indices: Sequence[int],
        assignments: Sequence[tuple[int, int, Point]],
        reseats: Sequence[tuple[int, int, Point]],
        current_idle_delta_us: Sequence[float],
) -> tuple[float, tuple[float, ...], dict[str, float], int, int]:
    """Independent physical oracle for ABI7 raw future-layer rollout."""
    if problem.selected_horizon != config.max_horizon:
        raise ValueError("problem/config horizon mismatch")
    positions = list(_rich_points(problem))
    if len(current_idle_delta_us) != problem.architecture.n_atoms:
        raise ValueError("current ABI7 fitness lacks per-atom idle delta")
    accumulated_idle = list(
        problem.prior_idle_time_us or
        (0.0,) * problem.architecture.n_atoms)
    for atom, delta in enumerate(current_idle_delta_us):
        accumulated_idle[atom] += delta
    for eligible_index, _site_id, point in (*assignments, *reseats):
        positions[problem.eligible[eligible_index]] = point
    for domain, option_index in zip(problem.gate_domains, option_indices):
        option = domain[option_index]
        target1, target2 = _gate_targets(problem, option)
        positions[option.q1] = target1
        positions[option.q2] = target2

    architecture = problem.architecture
    coordinates = architecture.site_coordinates
    storage_ids = architecture.storage_site_ids
    site_pairs = architecture.entangling_site_pairs
    zone_points = {
        coordinates[site_id] for pair in site_pairs for site_id in pair}
    storage_order_cache: dict[Point, tuple[int, ...]] = {}

    def storage_by_distance(source: Point) -> tuple[int, ...]:
        cached = storage_order_cache.get(source)
        if cached is None:
            cached = tuple(sorted(
                storage_ids,
                key=lambda site_id: (
                    dist((source.x, source.y),
                         (coordinates[site_id].x, coordinates[site_id].y)),
                    site_id),
            ))
            storage_order_cache[source] = cached
        return cached

    def occupied(point: Point, except_atom: int = -1) -> bool:
        return any(atom != except_atom and value == point
                   for atom, value in enumerate(positions))

    def score_phase(legs: Sequence[Leg], owners: Sequence[int],
                    snapshot: Sequence[Point],
                    idle_exposures: int = 0) -> FitnessResult:
        phases = ()
        if legs:
            phases = (MovementPhase(
                tuple(legs),
                tuple(Ghost(atom, point)
                      for atom, point in enumerate(snapshot)),
                tuple(owners)),)
        candidate = CandidatePlan((), phases, idle_exposures)
        return evaluate_candidate(
            BoundaryProblem(
                architecture,
                (candidate,),
                prior_idle_time_us=tuple(accumulated_idle),
            ),
            candidate,
            BoundaryConfig(
                exact_coloring_threshold=config.exact_coloring_threshold,
                enforce_single_leg_ghost=config.enforce_single_leg_ghost),
        )

    def advance_idle(score: FitnessResult) -> None:
        if not score.feasible:
            return
        if len(score.candidate_idle_time_us) != architecture.n_atoms:
            raise ValueError("forecast fitness lacks per-atom idle delta")
        for atom, delta in enumerate(score.candidate_idle_time_us):
            accumulated_idle[atom] += delta

    def finite_nll(score: FitnessResult) -> float:
        return score.negative_log_fidelity if score.feasible else inf

    def move_to_storage(atom: int) -> tuple[int, FitnessResult] | None:
        source = positions[atom]
        best = None
        tested = 0
        for site_id in storage_by_distance(source):
            target = coordinates[site_id]
            if occupied(target, atom):
                continue
            leg = Leg.between(source, target)
            if leg.distance_um <= EPS:
                continue
            score = score_phase((leg,), (atom,), positions)
            if not score.feasible:
                continue
            tested += 1
            key = (
                score.negative_log_fidelity,
                score.move_batches,
                score.move_time_us,
                score.total_distance_um,
                site_id,
            )
            if best is None or key < best[0]:
                best = (key, site_id, score)
            if (best is not None and
                    tested >= config.forecast_gate_candidate_budget):
                break
        if best is None:
            return None
        positions[atom] = coordinates[best[1]]
        return best[1], best[2]

    def relocation_batch(before: Sequence[Point], atoms: set[int]
                         ) -> FitnessResult:
        legs = []
        owners = []
        for atom in sorted(atoms):
            leg = Leg.between(before[atom], positions[atom])
            if leg.distance_um <= EPS:
                continue
            legs.append(leg)
            owners.append(atom)
        return score_phase(legs, owners, before)

    by_depth = [0.0] * (config.max_horizon + 1)
    breakdown = {
        "residency": 0.0,
        "reentry": 0.0,
        "terminal": 0.0,
        "routing": 0.0,
    }
    applied = skipped = 0

    def add(depth: int, category: str, raw_nll: float) -> None:
        decay = config.decay_rho ** (depth - 1)
        if decay < config.decay_epsilon:
            return
        contribution = config.alpha_lookahead * decay * raw_nll
        by_depth[depth] += contribution
        breakdown[category] += contribution

    for layer_index, (depth, gates) in enumerate(problem.future_layers):
        decay = config.decay_rho ** (depth - 1)
        if decay < config.decay_epsilon:
            skipped += 1
            continue
        participants = {atom for gate in gates for atom in gate}
        occupancy: dict[Point, list[int]] = {}
        for atom, point in enumerate(positions):
            occupancy.setdefault(point, []).append(atom)

        def blocker_distance(target: Point) -> float:
            total = 0.0
            for atom in occupancy.get(target, ()):
                if atom in participants:
                    continue
                ordered = storage_by_distance(positions[atom])
                if not ordered:
                    return inf
                nearest = coordinates[ordered[0]]
                total += dist(
                    (positions[atom].x, positions[atom].y),
                    (nearest.x, nearest.y))
            return total

        placements = []
        used_pairs: set[int] = set()
        for q1, q2 in gates:
            selected = None
            for pair_index, pair in enumerate(site_pairs):
                if pair_index in used_pairs:
                    continue
                left, right = coordinates[pair[0]], coordinates[pair[1]]
                for reversed_order in (False, True):
                    first, second = ((right, left) if reversed_order
                                     else (left, right))
                    cost = (
                        dist((positions[q1].x, positions[q1].y),
                             (first.x, first.y))
                        + dist((positions[q2].x, positions[q2].y),
                               (second.x, second.y))
                        + blocker_distance(first)
                    )
                    if first != second:
                        cost += blocker_distance(second)
                    key = (cost, pair_index, reversed_order)
                    if selected is None or key < selected[0]:
                        selected = (key, pair_index, first, second)
            if selected is None:
                return inf, tuple(by_depth), breakdown, applied, skipped
            used_pairs.add(selected[1])
            placements.append((q1, q2, selected[2], selected[3]))

        blockers: set[int] = set()
        for _q1, _q2, target1, target2 in placements:
            for target in (target1, target2):
                blockers.update(
                    atom for atom in occupancy.get(target, ())
                    if atom not in participants)
        ghosts = tuple(Ghost(atom, point)
                       for atom, point in enumerate(positions))
        for q1, q2, target1, target2 in placements:
            for atom, target in ((q1, target1), (q2, target2)):
                leg = Leg.between(positions[atom], target)
                if leg.distance_um <= EPS:
                    continue
                blockers.update(
                    hit for hit in ghost_hit_atoms((leg,), ghosts)
                    if hit not in participants)

        before_blockers = tuple(positions)
        routing_nll = 0.0
        for blocker in sorted(blockers):
            moved = move_to_storage(blocker)
            if moved is None:
                routing_nll = inf
                break
            routing_nll += finite_nll(moved[1])
        if isfinite(routing_nll) and blockers:
            batched = relocation_batch(before_blockers, blockers)
            if batched.feasible:
                routing_nll = finite_nll(batched)
                advance_idle(batched)
            else:
                routing_nll = inf

        out_legs = []
        out_owners = []
        for q1, q2, target1, target2 in placements:
            for atom, target in ((q1, target1), (q2, target2)):
                leg = Leg.between(positions[atom], target)
                if leg.distance_um > EPS:
                    out_legs.append(leg)
                    out_owners.append(atom)
        reentry_score = score_phase(out_legs, out_owners, positions)
        reentry_nll = finite_nll(reentry_score)
        advance_idle(reentry_score)
        for q1, q2, target1, target2 in placements:
            positions[q1] = target1
            positions[q2] = target2

        idle_exposures = sum(
            atom not in participants and point in zone_points
            for atom, point in enumerate(positions))
        pulse_idle = tuple(
            0.0 if atom in participants else T_RYDBERG_US
            for atom in range(architecture.n_atoms))
        pulse_nll = _linear_coherence_delta_nll(
            accumulated_idle, pulse_idle)
        residency_nll = -idle_exposures * log(F_EXC) + pulse_nll
        if isfinite(pulse_nll):
            for atom, delta in enumerate(pulse_idle):
                accumulated_idle[atom] += delta

        later_use = {
            atom
            for _later_depth, later_gates in problem.future_layers[
                layer_index + 1:]
            for gate in later_gates for atom in gate
        }
        before_terminal = tuple(positions)
        terminal_atoms = {
            atom for atom, point in enumerate(positions)
            if point in zone_points and atom not in later_use
        }
        terminal_nll = 0.0
        for atom in sorted(terminal_atoms):
            moved = move_to_storage(atom)
            if moved is None:
                terminal_nll = inf
                break
            terminal_nll += finite_nll(moved[1])
        if isfinite(terminal_nll) and terminal_atoms:
            batched = relocation_batch(before_terminal, terminal_atoms)
            if batched.feasible:
                terminal_nll = finite_nll(batched)
                advance_idle(batched)
            else:
                terminal_nll = inf

        add(depth, "residency", residency_nll)
        add(depth, "reentry", reentry_nll)
        add(depth, "terminal", terminal_nll)
        add(depth, "routing", routing_nll)
        applied += 1
    return sum(by_depth), tuple(by_depth), breakdown, applied, skipped


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


def _evaluate_rich_assignment_cohort(
        problem: RichH0Problem,
        config: RichSearchConfig,
        raw_chromosome: Sequence[int],
) -> tuple[_RichEvaluated, ...]:
    """Evaluate every bounded RETURN assignment for one chromosome.

    ABI7's forecast guard selects a *complete* evaluated value: chromosome,
    RETURN assignment and derived RESEATs.  Keeping the cohort construction in
    one oracle function prevents the ordinary exact search and the downstream
    current-physics guard from drifting to different assignment semantics.
    """
    chromosome = _rich_normalize(problem, raw_chromosome)
    option_indices = _rich_decode(problem, chromosome)
    if option_indices is None:
        fitness = _infeasible_rich(
            chromosome, "gate menu cannot form an injection")
        return (_RichEvaluated(
            fitness, (), (), (), (), inf, inf, (), {}, 0, 0, 0),)
    gate_count = len(problem.gate_domains)
    returners = tuple(index for index in range(len(problem.eligible))
                      if chromosome[gate_count + index])
    assignment_candidates = _rich_return_assignments(
        problem, config, returners)
    if not assignment_candidates:
        fitness = _infeasible_rich(
            chromosome, "RETURN matching has no bounded injective assignment")
        return (_RichEvaluated(
            fitness, option_indices, (), (), (), inf, inf, (), {}, 0, 0, 0),)
    evaluated = []
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
            if problem.future_layers:
                forecast = _evaluate_native_future_rollout(
                    problem, config, option_indices, assignments, reseats,
                    fitness.candidate_idle_time_us)
            else:
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
    return tuple(evaluated)


def evaluate_rich_exact_candidate(
        problem: RichH0Problem,
        config: RichSearchConfig,
        raw_chromosome: Sequence[int],
) -> _RichEvaluated:
    """Evaluate one rich chromosome independently of the C++ implementation."""
    evaluated = _evaluate_rich_assignment_cohort(
        problem, config, raw_chromosome)
    winner = min(evaluated, key=lambda value: value.objective)
    return replace(
        winner,
        current_ghost_rejections=sum(
            value.current_ghost_rejections for value in evaluated))


def _rich_objective_bucket(value: float, quantum: float) -> int:
    if not isfinite(value):
        return (1 << 63) - 1
    return int(floor(value / quantum + 0.5))


def _rich_current_physical_key(value: _RichEvaluated) -> tuple:
    fitness = value.fitness
    return (
        0 if fitness.feasible else 1,
        _rich_objective_bucket(fitness.negative_log_fidelity, 1e-12),
        fitness.move_batches,
        _rich_objective_bucket(fitness.move_time_us, 1e-6),
        _rich_objective_bucket(fitness.total_distance_um, 1e-6),
        fitness.chromosome,
        value.assignment_key,
    )


def _rich_same_current_primary_bucket(
        first: _RichEvaluated, second: _RichEvaluated) -> bool:
    return _rich_current_physical_key(first)[1:5] == \
        _rich_current_physical_key(second)[1:5]


def _rich_current_primary_dominates(
        first: _RichEvaluated, second: _RichEvaluated) -> bool:
    first_nll = _rich_objective_bucket(
        first.fitness.negative_log_fidelity, 1e-12)
    second_nll = _rich_objective_bucket(
        second.fitness.negative_log_fidelity, 1e-12)
    first_time = _rich_objective_bucket(first.fitness.move_time_us, 1e-6)
    second_time = _rich_objective_bucket(second.fitness.move_time_us, 1e-6)
    weakly_better = (
        first_nll <= second_nll
        and first.fitness.move_batches <= second.fitness.move_batches
        and first_time <= second_time
    )
    strictly_better = (
        first_nll < second_nll
        or first.fitness.move_batches < second.fitness.move_batches
        or first_time < second_time
    )
    return weakly_better and strictly_better


def _guard_rich_forecast_gate_projection(
        problem: RichH0Problem,
        config: RichSearchConfig,
        provisional: _RichEvaluated,
        complete_values: Sequence[_RichEvaluated],
) -> tuple[_RichEvaluated, dict]:
    """Independent Python truth for ABI7's current-physics forecast guard."""
    inactive = {
        "current_gate_anchor": (),
        "current_gate_anchor_assignment_site_ids": (),
        "current_gate_final_assignment_site_ids": (),
        "current_gate_guard_branch": "inactive",
        "current_gate_guard_cohort_size": 0,
        "current_gate_guard_admitted_size": 0,
        "current_gate_projection_source": "inactive",
        "current_gate_projection_evaluated": 0,
    }
    active = (
        config.max_horizon != 0
        and config.alpha_lookahead > 0.0
        and bool(problem.forecast_terms or problem.future_layers)
    )
    if not active:
        return provisional, inactive

    gate_count = len(problem.gate_domains)
    cohort_cache: dict[tuple[int, ...], tuple[_RichEvaluated, ...]] = {}

    def values_for(raw: Sequence[int]) -> tuple[_RichEvaluated, ...]:
        chromosome = _rich_normalize(problem, raw)
        if chromosome not in cohort_cache:
            cohort_cache[chromosome] = _evaluate_rich_assignment_cohort(
                problem, config, chromosome)
        return cohort_cache[chromosome]

    def current_min(raw: Sequence[int]) -> _RichEvaluated:
        return min(values_for(raw), key=_rich_current_physical_key)

    projected = provisional.fitness.chromosome
    projection_source = "no-current-gates"
    if gate_count:
        projection_source = (
            "single-gate-full-domain" if gate_count == 1
            else "coordinate-full-domain-2-sweep")
        projected_value = current_min(projected)
        if not projected_value.fitness.feasible:
            return provisional, {
                **inactive,
                "current_gate_guard_branch": "projection-infeasible-fallback",
                "current_gate_projection_source": projection_source,
                "current_gate_projection_evaluated": len(cohort_cache),
            }
        sweeps = 1 if gate_count == 1 else 2
        for _sweep in range(sweeps):
            changed = False
            for gate in range(gate_count):
                best = projected_value
                for option in range(len(problem.gate_domains[gate])):
                    trial = list(projected)
                    trial[gate] = option
                    value = current_min(trial)
                    if _rich_current_physical_key(value) < \
                            _rich_current_physical_key(best):
                        best = value
                if best.fitness.chromosome != projected:
                    projected = best.fitness.chromosome
                    projected_value = best
                    changed = True
            if not changed:
                break

    suffix = projected[gate_count:]
    guard_chromosomes = {
        value.fitness.chromosome
        for value in complete_values
        if value.fitness.chromosome[gate_count:] == suffix
    }
    guard_chromosomes.update(
        chromosome for chromosome in cohort_cache
        if chromosome[gate_count:] == suffix)
    guard_chromosomes.add(tuple(projected))
    cohort = tuple(
        value
        for chromosome in sorted(guard_chromosomes)
        for value in values_for(chromosome)
        if value.fitness.feasible
    )
    if not cohort:
        return provisional, {
            **inactive,
            "current_gate_anchor": tuple(projected),
            "current_gate_projection_source": projection_source,
            "current_gate_projection_evaluated": len(cohort_cache),
        }

    anchor = min(cohort, key=_rich_current_physical_key)
    if not problem.eligible:
        branch = "eligible-empty-exact-tie"
        admitted = tuple(
            value for value in cohort
            if _rich_same_current_primary_bucket(value, anchor))
    else:
        branch = "residency-pareto-envelope"
        transfer_limit = anchor.fitness.transfers + 2
        current_nll_limit = (
            anchor.fitness.negative_log_fidelity - 2.0 * log(F_TRANSFER))
        admitted = tuple(
            value for value in cohort
            if value.fitness.transfers <= transfer_limit
            and value.fitness.negative_log_fidelity <=
                current_nll_limit + 1e-12
            and not any(
                challenger is not value
                and _rich_current_primary_dominates(challenger, value)
                for challenger in cohort)
        )
    selected = min(admitted, key=lambda value: value.objective) \
        if admitted else anchor
    return selected, {
        "current_gate_anchor": anchor.fitness.chromosome,
        "current_gate_anchor_assignment_site_ids": tuple(
            assignment[1] for assignment in anchor.assignments),
        "current_gate_final_assignment_site_ids": tuple(
            assignment[1] for assignment in selected.assignments),
        "current_gate_guard_branch": branch,
        "current_gate_guard_cohort_size": len(cohort),
        "current_gate_guard_admitted_size": len(admitted),
        "current_gate_projection_source": projection_source,
        "current_gate_projection_evaluated": len(cohort_cache),
    }


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
    winner, guard = _guard_rich_forecast_gate_projection(
        problem, config, winner, feasible)
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
        current_gate_anchor=guard["current_gate_anchor"],
        current_gate_anchor_assignment_site_ids=
            guard["current_gate_anchor_assignment_site_ids"],
        current_gate_final_assignment_site_ids=
            guard["current_gate_final_assignment_site_ids"],
        current_gate_guard_branch=guard["current_gate_guard_branch"],
        current_gate_guard_cohort_size=
            guard["current_gate_guard_cohort_size"],
        current_gate_guard_admitted_size=
            guard["current_gate_guard_admitted_size"],
        current_gate_projection_source=
            guard["current_gate_projection_source"],
        current_gate_projection_evaluated=
            guard["current_gate_projection_evaluated"],
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
