"""Scoring and neighbor-search helpers for 2Q emission."""

from __future__ import annotations

from dataclasses import dataclass
from math import hypot
from time import monotonic
from typing import Callable, Mapping, Sequence

from .hardware import ENTANGLEMENT, HardwareSpec, Position
from .movement import batch_feasible, segment_min_separation
from .operations import MoveOp
from .ir import Gate2Q


@dataclass(frozen=True)
class EmissionScore:
    badness: float
    max_stage_distance: float = 0.0


@dataclass(frozen=True)
class MoveConflict:
    first: MoveOp
    second: MoveOp
    reason: str
    detail: str


@dataclass(frozen=True)
class MoveGraphCache:
    moves: tuple[MoveOp, ...]
    stationary: frozenset[Position]
    conflict_details: tuple[tuple[int, int, str, str], ...]
    conflict_edges: int


@dataclass(frozen=True)
class EmissionCandidate:
    ordered_gates: tuple[Gate2Q, ...]
    target_positions: dict[int, Position]
    moves_in: tuple[MoveOp, ...]
    move_batches: tuple[tuple[MoveOp, ...], ...]
    stationary: frozenset[Position]
    score: EmissionScore
    conflicts: tuple[MoveConflict, ...] = ()
    graph_cache: MoveGraphCache | None = None


def _q(value: float) -> float:
    return round(value, 6)


Point = tuple[float, float]


@dataclass(frozen=True)
class _MoveGeometry:
    move: MoveOp
    src: Point
    dst: Point
    q_src: Point
    q_dst: Point
    distance: float
    duration: float
    valid: bool


@dataclass(frozen=True)
class _StationaryGeometry:
    positions: frozenset[Position]
    points: tuple[Point, ...]
    sites: frozenset[Point]


def _max_move_distance(hw: HardwareSpec, moves: Sequence[MoveOp]) -> float:
    return max((hw.distance(move.src, move.dst) for move in moves), default=0.0)


def _point_segment_distance(point: Point, start: Point, end: Point) -> float:
    sx, sy = start
    ex, ey = end
    dx, dy = ex - sx, ey - sy
    denom = dx * dx + dy * dy
    if denom < 1e-12:
        return hypot(point[0] - sx, point[1] - sy)
    t = ((point[0] - sx) * dx + (point[1] - sy) * dy) / denom
    t = max(0.0, min(1.0, t))
    return hypot(point[0] - (sx + t * dx), point[1] - (sy + t * dy))


def _move_geometry(hw: HardwareSpec, move: MoveOp) -> _MoveGeometry:
    valid = hw.valid(move.src) and hw.valid(move.dst)
    src = hw.coordinate(move.src) if hw.valid(move.src) else (0.0, 0.0)
    dst = hw.coordinate(move.dst) if hw.valid(move.dst) else (0.0, 0.0)
    distance = hypot(src[0] - dst[0], src[1] - dst[1]) if valid else 0.0
    return _MoveGeometry(
        move,
        src,
        dst,
        (_q(src[0]), _q(src[1])),
        (_q(dst[0]), _q(dst[1])),
        distance,
        hw.move_duration(distance) if valid else 0.0,
        valid,
    )


def _stationary_geometry(hw: HardwareSpec, stationary: frozenset[Position]) -> _StationaryGeometry:
    points = tuple(hw.coordinate(pos) for pos in stationary)
    return _StationaryGeometry(
        stationary,
        points,
        frozenset((_q(point[0]), _q(point[1])) for point in points),
    )


def _tone_min_gap(start0: float, end0: float, start1: float, end1: float) -> float:
    start_gap = start0 - start1
    end_gap = end0 - end1
    if abs(end_gap - start_gap) < 1e-12:
        return abs(start_gap)
    t = max(0.0, min(1.0, -start_gap / (end_gap - start_gap)))
    return abs(start_gap + t * (end_gap - start_gap))


def _pair_conflict(
    hw: HardwareSpec,
    left: _MoveGeometry,
    right: _MoveGeometry,
    stationary: _StationaryGeometry,
) -> tuple[bool, str, str]:
    """Check whether two moves can share one AOD batch."""
    if not left.valid:
        return False, "out_of_range", f"{left.move.src}->{left.move.dst}"
    if not right.valid:
        return False, "out_of_range", f"{right.move.src}->{right.move.dst}"
    for item in (left, right):
        if item.distance > hw.aod_max_distance + 1e-9:
            return False, "max_distance", f"{item.distance:.3f} > {hw.aod_max_distance}"
        if item.duration > hw.aod_max_move_duration + 1e-9:
            return False, "max_duration", f"{item.duration:.3f} > {hw.aod_max_move_duration}"

    sx0, sy0 = left.q_src
    dx0, dy0 = left.q_dst
    sx1, sy1 = right.q_src
    dx1, dy1 = right.q_dst
    if (sx0 == sx1 and abs(dx0 - dx1) > 1e-6) or (sy0 == sy1 and abs(dy0 - dy1) > 1e-6):
        return False, "aod_shared_line", "atoms sharing an X/Y tone need different motion"
    if not hw.allow_tone_crossing and ((sx0 - sx1) * (dx0 - dx1) < -1e-9 or (sy0 - sy1) * (dy0 - dy1) < -1e-9):
        return False, "tone_crossing", "AOD tones cross"
    if sx0 != sx1 and _tone_min_gap(sx0, dx0, sx1, dx1) < hw.min_atom_separation - 1e-9:
        return False, "min_separation", "AOD tones collide"
    if sy0 != sy1 and _tone_min_gap(sy0, dy0, sy1, dy1) < hw.min_atom_separation - 1e-9:
        return False, "min_separation", "AOD tones collide"
    if left.q_dst == right.q_dst:
        return False, "dest_overlap", f"atoms {left.move.atom} and {right.move.atom}"
    if left.q_dst in stationary.sites:
        return False, "stationary_overlap", f"atom {left.move.atom} lands on an occupied site"
    if right.q_dst in stationary.sites:
        return False, "stationary_overlap", f"atom {right.move.atom} lands on an occupied site"

    intended = {left.q_src, right.q_src}
    x_tones = {sx0: dx0}
    y_tones = {sy0: dy0}
    x_tones.setdefault(sx1, dx1)
    y_tones.setdefault(sy1, dy1)
    for sx, dx in x_tones.items():
        for sy, dy in y_tones.items():
            if (sx, sy) in intended:
                continue
            for point in stationary.points:
                if _point_segment_distance(point, (sx, sy), (dx, dy)) < hw.min_atom_separation - 1e-9:
                    return False, "ghost_trap", "ghost trap disturbs stationary atom"

    if segment_min_separation(left.src, left.dst, right.src, right.dst) < hw.min_atom_separation - 1e-9:
        return False, "path_collision", f"atoms {left.move.atom} and {right.move.atom}"
    return True, "", ""


def _build_move_graph_cache(
    hw: HardwareSpec,
    moves: Sequence[MoveOp],
    stationary: set[Position] | frozenset[Position],
) -> MoveGraphCache:
    nodes = tuple(moves)
    stationary_set = frozenset(stationary)
    conflict_details: list[tuple[int, int, str, str]] = []
    stationary_geometry = _stationary_geometry(hw, stationary_set)
    geometries = tuple(_move_geometry(hw, move) for move in nodes)
    for left_index, left in enumerate(geometries):
        for right_index in range(left_index + 1, len(geometries)):
            right = geometries[right_index]
            ok, reason, detail = _pair_conflict(hw, left, right, stationary_geometry)
            if not ok:
                conflict_details.append((left_index, right_index, reason, detail))
    return MoveGraphCache(
        nodes,
        stationary_set,
        tuple(conflict_details),
        len(conflict_details),
    )


def _update_move_graph_cache(
    hw: HardwareSpec,
    parent: MoveGraphCache,
    moves: Sequence[MoveOp],
    stationary: set[Position] | frozenset[Position],
) -> MoveGraphCache:
    nodes = tuple(moves)
    stationary_set = frozenset(stationary)
    if parent.stationary != stationary_set or len(parent.moves) != len(nodes):
        return _build_move_graph_cache(hw, nodes, stationary_set)

    changed = {index for index, (old, new) in enumerate(zip(parent.moves, nodes)) if old != new}
    if not changed:
        return parent
    if len(changed) > max(4, len(nodes) // 3):
        return _build_move_graph_cache(hw, nodes, stationary_set)

    conflict_details = [
        edge
        for edge in parent.conflict_details
        if edge[0] not in changed and edge[1] not in changed
    ]

    stationary_geometry = _stationary_geometry(hw, stationary_set)
    geometries = tuple(_move_geometry(hw, move) for move in nodes)
    for left_index, left in enumerate(geometries):
        for right_index in range(left_index + 1, len(geometries)):
            if left_index not in changed and right_index not in changed:
                continue
            right = geometries[right_index]
            ok, reason, detail = _pair_conflict(hw, left, right, stationary_geometry)
            if not ok:
                conflict_details.append((left_index, right_index, reason, detail))

    return MoveGraphCache(
        nodes,
        stationary_set,
        tuple(sorted(conflict_details)),
        len(conflict_details),
    )


def _score_from_cache(cache: MoveGraphCache, max_stage_distance: float = 0.0) -> EmissionScore:
    return EmissionScore(3 * cache.conflict_edges, max_stage_distance)


def score_move_batches(
    hw: HardwareSpec,
    batches: Sequence[Sequence[MoveOp]],
    stationary: set[Position] | frozenset[Position] = frozenset(),
) -> EmissionScore:
    """Score candidate moves by pairwise conflict count."""
    max_stage_distance = max((_max_move_distance(hw, batch) for batch in batches), default=0.0)
    moves = tuple(move for batch in batches for move in batch)
    cache = _build_move_graph_cache(hw, moves, stationary)
    return _score_from_cache(cache, max_stage_distance)


def score_move_graph(
    hw: HardwareSpec,
    moves: Sequence[MoveOp],
    stationary: set[Position] | frozenset[Position] = frozenset(),
    parent_cache: MoveGraphCache | None = None,
) -> tuple[EmissionScore, MoveGraphCache]:
    """Score moves and return the reusable graph cache."""
    max_stage_distance = _max_move_distance(hw, moves)
    if parent_cache is None:
        cache = _build_move_graph_cache(hw, moves, stationary)
    else:
        cache = _update_move_graph_cache(hw, parent_cache, moves, stationary)
    return _score_from_cache(cache, max_stage_distance), cache


def target_signature(target_positions: Mapping[int, Position]) -> tuple[tuple[int, Position], ...]:
    return tuple(sorted(target_positions.items()))


def validate_2q_targets(gates: Sequence[Gate2Q], target_positions: Mapping[int, Position]) -> bool:
    seen: set[Position] = set()
    for gate in gates:
        if gate.q0 not in target_positions or gate.q1 not in target_positions:
            return False
        pos0 = target_positions[gate.q0]
        pos1 = target_positions[gate.q1]
        if pos0 in seen or pos1 in seen or pos0 == pos1:
            return False
        if pos0[0] != ENTANGLEMENT or pos1[0] != ENTANGLEMENT:
            return False
        if pos0[2] != pos1[2] or {pos0[1], pos1[1]} != {0, 1}:
            return False
        seen.update({pos0, pos1})
    return True


def find_conflicting_move_pairs(
    hw: HardwareSpec,
    moves: Sequence[MoveOp],
    stationary: set[Position] | frozenset[Position],
) -> tuple[MoveConflict, ...]:
    conflicts: list[MoveConflict] = []
    for index, first in enumerate(moves):
        first_ok, _, _ = batch_feasible(hw, [first], set(stationary))
        if not first_ok:
            continue
        for second in moves[index + 1 :]:
            second_ok, _, _ = batch_feasible(hw, [second], set(stationary))
            if not second_ok:
                continue
            ok, reason, detail = batch_feasible(hw, [first, second], set(stationary))
            if not ok:
                conflicts.append(MoveConflict(first, second, reason, detail))
    return tuple(conflicts)


def swapped_target_neighbors(
    gates: Sequence[Gate2Q],
    target_positions: Mapping[int, Position],
    conflicts: Sequence[MoveConflict],
    limit: int | None = None,
) -> tuple[dict[int, Position], ...]:
    seen: set[tuple[tuple[int, Position], ...]] = {target_signature(target_positions)}
    neighbors: list[dict[int, Position]] = []
    for conflict in conflicts:
        first_atom = conflict.first.atom
        second_atom = conflict.second.atom
        if first_atom not in target_positions or second_atom not in target_positions:
            continue
        neighbor = dict(target_positions)
        neighbor[first_atom], neighbor[second_atom] = neighbor[second_atom], neighbor[first_atom]
        signature = target_signature(neighbor)
        if signature in seen or not validate_2q_targets(gates, neighbor):
            continue
        seen.add(signature)
        neighbors.append(neighbor)
        if limit is not None and len(neighbors) >= limit:
            break
    return tuple(neighbors)


def shifted_target_neighbors(
    hw: HardwareSpec,
    gates: Sequence[Gate2Q],
    target_positions: Mapping[int, Position],
    limit: int | None = None,
) -> tuple[dict[int, Position], ...]:
    seen: set[tuple[tuple[int, Position], ...]] = {target_signature(target_positions)}
    occupied = set(target_positions.values())
    neighbors: list[dict[int, Position]] = []
    for gate in gates:
        if gate.q0 not in target_positions or gate.q1 not in target_positions:
            continue
        pos0 = target_positions[gate.q0]
        pos1 = target_positions[gate.q1]
        if pos0[0] != ENTANGLEMENT or pos1[0] != ENTANGLEMENT or pos0[2] != pos1[2]:
            continue
        for next_row in (pos0[2] - 1, pos0[2] + 1):
            if not 0 <= next_row < hw.entanglement.rows:
                continue
            next0 = (ENTANGLEMENT, pos0[1], next_row)
            next1 = (ENTANGLEMENT, pos1[1], next_row)
            if next0 in occupied or next1 in occupied:
                continue
            neighbor = dict(target_positions)
            neighbor[gate.q0] = next0
            neighbor[gate.q1] = next1
            signature = target_signature(neighbor)
            if signature in seen or not validate_2q_targets(gates, neighbor):
                continue
            seen.add(signature)
            neighbors.append(neighbor)
            if limit is not None and len(neighbors) >= limit:
                return tuple(neighbors)
    return tuple(neighbors)


def _candidate_key(candidate: EmissionCandidate) -> tuple[float, int, float, tuple[tuple[int, Position], ...]]:
    return (
        candidate.score.badness,
        len(candidate.move_batches),
        candidate.score.max_stage_distance,
        target_signature(candidate.target_positions),
    )


def _conflicts_from_cache(cache: MoveGraphCache) -> tuple[MoveConflict, ...]:
    return tuple(
        MoveConflict(cache.moves[first], cache.moves[second], reason, detail)
        for first, second, reason, detail in cache.conflict_details
    )


def _with_conflicts(hw: HardwareSpec, candidate: EmissionCandidate) -> EmissionCandidate:
    if candidate.conflicts:
        return candidate
    if candidate.graph_cache is not None:
        conflicts = _conflicts_from_cache(candidate.graph_cache)
    else:
        conflicts = find_conflicting_move_pairs(hw, candidate.moves_in, candidate.stationary)
    return EmissionCandidate(
        candidate.ordered_gates,
        candidate.target_positions,
        candidate.moves_in,
        candidate.move_batches,
        candidate.stationary,
        candidate.score,
        conflicts,
        candidate.graph_cache,
    )


def _select_ranked(candidates: Sequence[EmissionCandidate], limit: int) -> tuple[EmissionCandidate, ...]:
    unique: dict[tuple[tuple[int, Position], ...], EmissionCandidate] = {}
    for candidate in candidates:
        signature = target_signature(candidate.target_positions)
        existing = unique.get(signature)
        if existing is None or _candidate_key(candidate) < _candidate_key(existing):
            unique[signature] = candidate
    return tuple(sorted(unique.values(), key=_candidate_key)[:limit])


def _top_neighbor_candidates(
    hw: HardwareSpec,
    candidate: EmissionCandidate,
    evaluate: Callable[[Mapping[int, Position], MoveGraphCache | None], EmissionCandidate],
    limit: int,
    sample_size: int = 50,
    deadline: float | None = None,
) -> tuple[EmissionCandidate, ...]:
    candidate = _with_conflicts(hw, candidate)
    neighbors: list[EmissionCandidate] = []
    target_neighbors = [
        *swapped_target_neighbors(
            candidate.ordered_gates,
            candidate.target_positions,
            candidate.conflicts,
            sample_size,
        ),
        *shifted_target_neighbors(
            hw,
            candidate.ordered_gates,
            candidate.target_positions,
            sample_size,
        ),
    ][:sample_size]
    for targets in target_neighbors:
        if deadline is not None and monotonic() >= deadline:
            break
        try:
            neighbors.append(_with_conflicts(hw, evaluate(targets, candidate.graph_cache)))
        except ValueError:
            continue
    return _select_ranked(neighbors, limit)


def search_2q_emission(
    hw: HardwareSpec,
    initial: EmissionCandidate,
    evaluate: Callable[[Mapping[int, Position], MoveGraphCache | None], EmissionCandidate],
    *,
    population_size: int = 5,
    iterations: int = 1,
    neighbors_per_solution: int = 2,
    neighbor_sample_size: int = 20,
    elite_parent_fraction: float = 0.2,
    initial_candidates: Sequence[EmissionCandidate] = (),
    timeout: float | None = None,
    convergence_patience: int | None = 3,
    stop_on_zero_badness: bool = True,
) -> EmissionCandidate:
    """Run deterministic population search over conflict-swap 2Q target neighbors."""
    deadline = monotonic() + timeout if timeout is not None else None
    initial = _with_conflicts(hw, initial)
    if initial_candidates:
        population = _select_ranked(
            (initial, *(tuple(_with_conflicts(hw, candidate) for candidate in initial_candidates))),
            population_size,
        )
    else:
        population = _select_ranked(
            [
                initial,
                *_top_neighbor_candidates(
                    hw,
                    initial,
                    evaluate,
                    population_size - 1,
                    neighbor_sample_size,
                    deadline,
                ),
            ],
            population_size,
        )
    if not population:
        return initial
    population = tuple(_with_conflicts(hw, candidate) for candidate in population)
    best_key = _candidate_key(min(population, key=_candidate_key))
    stale_rounds = 0

    elite_count = max(1, int(population_size * elite_parent_fraction))
    for _ in range(iterations):
        if deadline is not None and monotonic() >= deadline:
            break
        if stop_on_zero_badness and best_key[0] == 0.0:
            break
        neighbors: list[EmissionCandidate] = []
        for candidate in population:
            if deadline is not None and monotonic() >= deadline:
                break
            neighbors.extend(
                _top_neighbor_candidates(
                    hw,
                    candidate,
                    evaluate,
                    neighbors_per_solution,
                    neighbor_sample_size,
                    deadline,
                )
            )
        parent_elites = _select_ranked(population, elite_count)
        next_population = _select_ranked([*neighbors, *parent_elites], population_size)
        if not next_population:
            break
        next_best_key = _candidate_key(min(next_population, key=_candidate_key))
        if next_best_key < best_key:
            best_key = next_best_key
            stale_rounds = 0
        else:
            stale_rounds += 1
        population = tuple(_with_conflicts(hw, candidate) for candidate in next_population)
        if convergence_patience is not None and stale_rounds >= convergence_patience:
            break
    return min(population, key=_candidate_key)
