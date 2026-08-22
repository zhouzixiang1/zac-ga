"""Crossed-AOD movement planning."""

from __future__ import annotations

from dataclasses import dataclass, field
from math import hypot
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .hardware import HardwareSpec, Position
from .operations import MoveOp

Point = Tuple[float, float]


def _q(value: float) -> float:
    return round(value, 6)


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


def segment_min_separation(a0: Point, a1: Point, b0: Point, b1: Point) -> float:
    w0 = (a0[0] - b0[0], a0[1] - b0[1])
    w1 = (a1[0] - b1[0], a1[1] - b1[1])
    dw = (w1[0] - w0[0], w1[1] - w0[1])
    denom = dw[0] * dw[0] + dw[1] * dw[1]
    t = 0.0 if denom < 1e-12 else -(w0[0] * dw[0] + w0[1] * dw[1]) / denom
    t = max(0.0, min(1.0, t))
    return hypot(w0[0] + t * dw[0], w0[1] + t * dw[1])


def _endpoints(hw: HardwareSpec, move: MoveOp) -> Tuple[Point, Point]:
    return hw.coordinate(move.src), hw.coordinate(move.dst)


def aod_tone_layout(
    hw: HardwareSpec, moves: Sequence[MoveOp]
) -> Tuple[Dict[float, float], Dict[float, float], bool]:
    x_tones: Dict[float, float] = {}
    y_tones: Dict[float, float] = {}
    consistent = True
    for move in moves:
        sx, sy = hw.coordinate(move.src)
        dx, dy = hw.coordinate(move.dst)
        sx, sy, dx, dy = _q(sx), _q(sy), _q(dx), _q(dy)
        if sx in x_tones and abs(x_tones[sx] - dx) > 1e-6:
            consistent = False
        if sy in y_tones and abs(y_tones[sy] - dy) > 1e-6:
            consistent = False
        x_tones.setdefault(sx, dx)
        y_tones.setdefault(sy, dy)
    return x_tones, y_tones, consistent


def _tones_cross(tones: Dict[float, float]) -> bool:
    items = sorted(tones.items())
    for i, (s0, d0) in enumerate(items):
        for s1, d1 in items[i + 1 :]:
            if (s0 - s1) * (d0 - d1) < -1e-9:
                return True
    return False


def _tones_min_gap(tones: Dict[float, float]) -> float:
    items = sorted(tones.items())
    if len(items) < 2:
        return float("inf")
    best = float("inf")
    for i, (s0, d0) in enumerate(items):
        for s1, d1 in items[i + 1 :]:
            start_gap = s0 - s1
            end_gap = d0 - d1
            if abs(end_gap - start_gap) < 1e-12:
                gap = abs(start_gap)
            else:
                t = max(0.0, min(1.0, -start_gap / (end_gap - start_gap)))
                gap = abs(start_gap + t * (end_gap - start_gap))
            best = min(best, gap)
    return best


def aod_ghost_traps(hw: HardwareSpec, moves: Sequence[MoveOp]) -> List[Tuple[Point, Point]]:
    x_tones, y_tones, _ = aod_tone_layout(hw, moves)
    intended = {tuple(_q(v) for v in hw.coordinate(move.src)) for move in moves}
    ghosts: List[Tuple[Point, Point]] = []
    for sx, dx in x_tones.items():
        for sy, dy in y_tones.items():
            if (sx, sy) not in intended:
                ghosts.append(((sx, sy), (dx, dy)))
    return ghosts


def batch_feasible(
    hw: HardwareSpec,
    moves: Sequence[MoveOp],
    stationary: Optional[Set[Position]] = None,
) -> Tuple[bool, str, str]:
    stationary = stationary or set()
    if len(moves) > hw.aod_max_atoms:
        return False, "capacity", f"{len(moves)} > {hw.aod_max_atoms}"

    for move in moves:
        if not hw.valid(move.src) or not hw.valid(move.dst):
            return False, "out_of_range", f"{move.src}->{move.dst}"
        distance = hw.distance(move.src, move.dst)
        if distance > hw.aod_max_distance + 1e-9:
            return False, "max_distance", f"{distance:.3f} > {hw.aod_max_distance}"
        duration = hw.move_duration(distance)
        if duration > hw.aod_max_move_duration + 1e-9:
            return False, "max_duration", f"{duration:.3f} > {hw.aod_max_move_duration}"

    x_tones, y_tones, consistent = aod_tone_layout(hw, moves)
    if not consistent:
        return False, "aod_shared_line", "atoms sharing an X/Y tone need different motion"
    if not hw.allow_tone_crossing and (_tones_cross(x_tones) or _tones_cross(y_tones)):
        return False, "tone_crossing", "AOD tones cross"
    if (
        _tones_min_gap(x_tones) < hw.min_atom_separation - 1e-9
        or _tones_min_gap(y_tones) < hw.min_atom_separation - 1e-9
    ):
        return False, "min_separation", "AOD tones collide"

    destinations: Dict[Point, int] = {}
    for move in moves:
        dst = tuple(_q(v) for v in hw.coordinate(move.dst))
        if dst in destinations:
            return False, "dest_overlap", f"atoms {destinations[dst]} and {move.atom}"
        destinations[dst] = move.atom

    stationary_points = [hw.coordinate(pos) for pos in stationary]
    stationary_sites = {tuple(_q(v) for v in point) for point in stationary_points}
    for move in moves:
        dst = tuple(_q(v) for v in hw.coordinate(move.dst))
        if dst in stationary_sites:
            return False, "stationary_overlap", f"atom {move.atom} lands on an occupied site"

    for start, end in aod_ghost_traps(hw, moves):
        for point in stationary_points:
            if _point_segment_distance(point, start, end) < hw.min_atom_separation - 1e-9:
                return False, "ghost_trap", "ghost trap disturbs stationary atom"

    for i, first in enumerate(moves):
        first_start, first_end = _endpoints(hw, first)
        for second in moves[i + 1 :]:
            second_start, second_end = _endpoints(hw, second)
            if (
                segment_min_separation(first_start, first_end, second_start, second_end)
                < hw.min_atom_separation - 1e-9
            ):
                return False, "path_collision", f"atoms {first.atom} and {second.atom}"

    return True, "", ""


@dataclass
class BatchDiagnostics:
    ok: bool
    reason: str = ""
    detail: str = ""
    ghost_traps: List[Tuple[Point, Point]] = field(default_factory=list)


def diagnose_batch(
    hw: HardwareSpec,
    moves: Sequence[MoveOp],
    stationary: Optional[Set[Position]] = None,
) -> BatchDiagnostics:
    ok, reason, detail = batch_feasible(hw, moves, stationary)
    return BatchDiagnostics(ok, reason, detail, aod_ghost_traps(hw, moves))


@dataclass(frozen=True)
class _MoveGeometry:
    move: MoveOp
    src: Point
    dst: Point
    q_src: Point
    q_dst: Point
    distance: float
    duration: float


@dataclass(frozen=True)
class _StationaryGeometry:
    positions: Set[Position]
    points: List[Point]
    sites: Set[Point]


def _move_geometry(hw: HardwareSpec, move: MoveOp) -> _MoveGeometry:
    src, dst = _endpoints(hw, move)
    distance = hypot(src[0] - dst[0], src[1] - dst[1])
    return _MoveGeometry(
        move=move,
        src=src,
        dst=dst,
        q_src=(_q(src[0]), _q(src[1])),
        q_dst=(_q(dst[0]), _q(dst[1])),
        distance=distance,
        duration=hw.move_duration(distance),
    )


def _stationary_geometry(hw: HardwareSpec, positions: Set[Position]) -> _StationaryGeometry:
    points = [hw.coordinate(pos) for pos in positions]
    return _StationaryGeometry(
        positions=positions,
        points=points,
        sites={(_q(point[0]), _q(point[1])) for point in points},
    )


class _BatchBuilder:
    def __init__(
        self,
        hw: HardwareSpec,
        stationary: _StationaryGeometry,
        geometry: Dict[MoveOp, _MoveGeometry],
    ) -> None:
        self.hw = hw
        self.stationary = stationary
        self.geometry = geometry
        self.moves: List[MoveOp] = []
        self.x_tones: Dict[float, float] = {}
        self.y_tones: Dict[float, float] = {}
        self.intended: Set[Point] = set()
        self.destinations: Dict[Point, int] = {}

    def can_add(self, move: MoveOp) -> bool:
        ok, _, _ = self._check_add(move)
        return ok

    def add(self, move: MoveOp) -> None:
        ok, reason, detail = self._check_add(move)
        if not ok:
            raise ValueError(f"move {move.atom} cannot be added: {reason} {detail}")
        self._commit(move)

    def feasible(self) -> bool:
        return self._ghosts_clear(self.x_tones, self.y_tones, self.intended)

    def _check_add(self, move: MoveOp) -> Tuple[bool, str, str]:
        if len(self.moves) + 1 > self.hw.aod_max_atoms:
            return False, "capacity", f"{len(self.moves) + 1} > {self.hw.aod_max_atoms}"
        if not self.hw.valid(move.src) or not self.hw.valid(move.dst):
            return False, "out_of_range", f"{move.src}->{move.dst}"

        geom = self.geometry[move]
        if geom.distance > self.hw.aod_max_distance + 1e-9:
            return False, "max_distance", f"{geom.distance:.3f} > {self.hw.aod_max_distance}"
        if geom.duration > self.hw.aod_max_move_duration + 1e-9:
            return False, "max_duration", f"{geom.duration:.3f} > {self.hw.aod_max_move_duration}"
        if geom.q_dst in self.destinations:
            return False, "dest_overlap", f"atoms {self.destinations[geom.q_dst]} and {move.atom}"
        if geom.q_dst in self.stationary.sites:
            return False, "stationary_overlap", f"atom {move.atom} lands on an occupied site"

        sx, sy = geom.q_src
        dx, dy = geom.q_dst
        if sx in self.x_tones and abs(self.x_tones[sx] - dx) > 1e-6:
            return False, "aod_shared_line", "atoms sharing an X/Y tone need different motion"
        if sy in self.y_tones and abs(self.y_tones[sy] - dy) > 1e-6:
            return False, "aod_shared_line", "atoms sharing an X/Y tone need different motion"

        x_tones = dict(self.x_tones)
        y_tones = dict(self.y_tones)
        intended = set(self.intended)
        x_tones.setdefault(sx, dx)
        y_tones.setdefault(sy, dy)
        intended.add(geom.q_src)
        if not self.hw.allow_tone_crossing and (_tones_cross(x_tones) or _tones_cross(y_tones)):
            return False, "tone_crossing", "AOD tones cross"
        if (
            _tones_min_gap(x_tones) < self.hw.min_atom_separation - 1e-9
            or _tones_min_gap(y_tones) < self.hw.min_atom_separation - 1e-9
        ):
            return False, "min_separation", "AOD tones collide"
        if not self._ghosts_clear(x_tones, y_tones, intended):
            return False, "ghost_trap", "ghost trap disturbs stationary atom"

        for other in self.moves:
            other_geom = self.geometry[other]
            if (
                segment_min_separation(geom.src, geom.dst, other_geom.src, other_geom.dst)
                < self.hw.min_atom_separation - 1e-9
            ):
                return False, "path_collision", f"atoms {other.atom} and {move.atom}"
        return True, "", ""

    def _commit(self, move: MoveOp) -> None:
        geom = self.geometry[move]
        sx, sy = geom.q_src
        dx, dy = geom.q_dst
        self.moves.append(move)
        self.x_tones.setdefault(sx, dx)
        self.y_tones.setdefault(sy, dy)
        self.intended.add(geom.q_src)
        self.destinations[geom.q_dst] = move.atom

    def _ghosts_clear(
        self,
        x_tones: Dict[float, float],
        y_tones: Dict[float, float],
        intended: Set[Point],
    ) -> bool:
        for sx, dx in x_tones.items():
            for sy, dy in y_tones.items():
                if (sx, sy) in intended:
                    continue
                start, end = (sx, sy), (dx, dy)
                for point in self.stationary.points:
                    if _point_segment_distance(point, start, end) < self.hw.min_atom_separation - 1e-9:
                        return False
        return True


def _feasible_with_context(
    hw: HardwareSpec,
    moves: Sequence[MoveOp],
    stationary: _StationaryGeometry,
    geometry: Dict[MoveOp, _MoveGeometry],
) -> bool:
    builder = _BatchBuilder(hw, stationary, geometry)
    for move in moves:
        if not builder.can_add(move):
            return False
        builder.add(move)
    return builder.feasible()


def _seed_indices(
    hw: HardwareSpec,
    remaining: Sequence[MoveOp],
    stationary: _StationaryGeometry,
    geometry: Dict[MoveOp, _MoveGeometry],
) -> List[int]:
    if len(remaining) <= 4:
        return list(range(len(remaining)))

    scores: List[Tuple[int, int, int]] = []
    for index, seed in enumerate(remaining):
        rest = [move for move in remaining if move is not seed]
        pending_sources = {pending.src for pending in rest}
        seed_obstacles = _stationary_geometry(hw, stationary.positions | pending_sources)
        if not _feasible_with_context(hw, [seed], seed_obstacles, geometry):
            scores.append((10**9, index, index))
            continue

        builder = _BatchBuilder(hw, stationary, geometry)
        builder.add(seed)
        compatible = 0
        for move in remaining:
            if move is seed:
                continue
            if builder.can_add(move):
                compatible += 1
        scores.append((compatible, index, index))

    limit = min(len(remaining), 4)
    return [index for _, _, index in sorted(scores)[:limit]]


def plan_batches(
    hw: HardwareSpec,
    moves: Sequence[MoveOp],
    stationary: Optional[Set[Position]] = None,
) -> List[List[MoveOp]]:
    batches: List[List[MoveOp]] = []
    base_stationary = set(stationary or set())
    remaining = list(moves)
    completed_destinations: Set[Position] = set()
    geometry = {move: _move_geometry(hw, move) for move in moves}

    while remaining:
        best_batch: List[MoveOp] = []
        best_remaining: List[MoveOp] = []
        build_obstacles = _stationary_geometry(hw, base_stationary | completed_destinations)
        for seed_index in _seed_indices(hw, remaining, build_obstacles, geometry):
            candidate_remaining = list(remaining)
            candidate_batch = [candidate_remaining.pop(seed_index)]
            builder = _BatchBuilder(hw, build_obstacles, geometry)
            if not builder.can_add(candidate_batch[0]):
                continue
            builder.add(candidate_batch[0])

            changed = True
            while changed:
                changed = False
                for move in list(candidate_remaining):
                    if builder.can_add(move):
                        candidate_batch.append(move)
                        candidate_remaining.remove(move)
                        builder.add(move)
                        changed = True

            for cut in range(len(candidate_batch), 0, -1):
                batch = candidate_batch[:cut]
                rest = [move for move in remaining if move not in batch]
                pending_sources = {pending.src for pending in rest}
                cut_obstacles = _stationary_geometry(
                    hw,
                    base_stationary | completed_destinations | pending_sources,
                )
                if _feasible_with_context(hw, batch, cut_obstacles, geometry):
                    if len(batch) > len(best_batch):
                        best_batch = batch
                        best_remaining = rest
                    break

        if not best_batch:
            move = remaining[0]
            _, reason, detail = batch_feasible(hw, [move], base_stationary | completed_destinations)
            atoms = str(move.atom)
            raise ValueError(f"moves [{atoms}] cannot be scheduled: {reason} {detail}")
        batches.append(best_batch)
        remaining = best_remaining
        completed_destinations.update(move.dst for move in best_batch)
    return batches


def batch_duration(hw: HardwareSpec, moves: Sequence[MoveOp]) -> float:
    if not moves:
        return 0.0
    return max(hw.move_duration(hw.distance(move.src, move.dst)) for move in moves)