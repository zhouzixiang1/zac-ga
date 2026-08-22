"""Crossed-AOD movement planning."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from math import hypot
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .hardware import HardwareSpec, Position
from .operations import MoveOp

Point = Tuple[float, float]


@dataclass(frozen=True)
class MoveCall:
    call_index: int
    moves: Tuple[MoveOp, ...]
    stationary: frozenset[Position] = frozenset()


@dataclass(frozen=True)
class GroupBatchPlan:
    batches: List[List[MoveOp]]
    used_fallback: bool = False
    fallback_reason: str = ""


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


def _build_move_graph(
    hw: HardwareSpec,
    moves: Sequence[MoveOp],
    stationary: Optional[Set[Position]] = None,
) -> Tuple[Dict[MoveOp, Set[MoveOp]], Dict[MoveOp, Set[MoveOp]], Dict[MoveOp, Set[MoveOp]]]:
    nodes = list(moves)
    moving_sources = {move.src for move in nodes}
    stationary = set(stationary or set()) - moving_sources
    conflicts: Dict[MoveOp, Set[MoveOp]] = {move: set() for move in nodes}
    dag: Dict[MoveOp, Set[MoveOp]] = {move: set() for move in nodes}
    same_batch: Dict[MoveOp, Set[MoveOp]] = {move: set() for move in nodes}

    for index, move in enumerate(nodes):
        for other in nodes[:index]:
            ok, _, _ = batch_feasible(hw, [other, move], stationary)
            if not ok:
                conflicts[other].add(move)
                conflicts[move].add(other)

            if move.atom == other.atom:
                dag[other].add(move)

            if move.dst == other.src or other.dst == move.src:
                if ok:
                    same_batch[other].add(move)
                    same_batch[move].add(other)
                else:
                    dag[other].add(move)

    for node in nodes:
        ancestors: Set[MoveOp] = set()
        stack = list(dag[node])
        while stack:
            current = stack.pop()
            if current in ancestors:
                continue
            ancestors.add(current)
            stack.extend(dag[current])
        for ancestor in ancestors:
            conflicts[node].add(ancestor)
            conflicts[ancestor].add(node)

    return conflicts, dag, same_batch


def _color_components(
    hw: HardwareSpec,
    moves: Sequence[MoveOp],
    stationary: Optional[Set[Position]] = None,
) -> List[List[MoveOp]]:
    nodes = list(moves)
    if not nodes:
        return []

    moving_sources = {move.src for move in nodes}
    stationary = set(stationary or set()) - moving_sources
    conflicts, dag, same_batch = _build_move_graph(hw, nodes, stationary)

    parent: Dict[MoveOp, MoveOp] = {}
    rank: Dict[MoveOp, int] = {}

    def find(node: MoveOp) -> MoveOp:
        parent_node = parent.get(node)
        if parent_node is None:
            return node
        root = find(parent_node)
        parent[node] = root
        return root

    def union(left: MoveOp, right: MoveOp) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return
        if rank.get(left_root, 0) < rank.get(right_root, 0):
            left_root, right_root = right_root, left_root
        parent[right_root] = left_root
        if rank.get(left_root, 0) == rank.get(right_root, 0):
            rank[left_root] = rank.get(left_root, 0) + 1

    for left in nodes:
        for right in same_batch[left]:
            union(left, right)

    components: Dict[MoveOp, int] = {}
    component_members: Dict[int, List[MoveOp]] = {}
    for node in nodes:
        root = find(node)
        component_id = components.setdefault(root, len(component_members))
        component_members.setdefault(component_id, []).append(node)

    component_ids = list(component_members)
    component_order = {component_id: index for index, component_id in enumerate(component_ids)}
    component_conflicts: Dict[int, Set[int]] = {component_id: set() for component_id in component_ids}
    for left in nodes:
        left_component = components[find(left)]
        for right in conflicts[left]:
            right_component = components[find(right)]
            if right_component != left_component:
                component_conflicts[left_component].add(right_component)
                component_conflicts[right_component].add(left_component)

    color_map: Dict[int, int] = {}
    uncolored = set(component_ids)
    while uncolored:
        next_component = None
        best_key: Tuple[int, int, int] | None = None
        for component_id in uncolored:
            saturation = len({color_map[other] for other in component_conflicts[component_id] if other in color_map})
            degree = len(component_conflicts[component_id])
            order = component_order[component_id]
            key = (-saturation, -degree, order)
            if best_key is None or key > best_key:
                best_key = key
                next_component = component_id
        if next_component is None:
            break

        used_colors = {color_map[other] for other in component_conflicts[next_component] if other in color_map}
        color = 0
        while color in used_colors:
            color += 1
        color_map[next_component] = color
        uncolored.remove(next_component)

    component_topo_order: Dict[int, int] = {}
    indegree: Dict[int, int] = {component_id: 0 for component_id in component_ids}
    successors: Dict[int, Set[int]] = {component_id: set() for component_id in component_ids}
    for left in nodes:
        left_component = components[find(left)]
        for right in dag.get(left, set()):
            right_component = components[find(right)]
            if right_component == left_component:
                continue
            if left_component not in successors[right_component]:
                successors[left_component].add(right_component)
                indegree[right_component] += 1

    queue = [component_id for component_id, degree in indegree.items() if degree == 0]
    queue.sort(key=lambda component_id: component_order[component_id])
    topo_position = 0
    visited_topo: Set[int] = set()
    while queue:
        component_id = queue.pop(0)
        component_topo_order[component_id] = topo_position
        visited_topo.add(component_id)
        topo_position += 1
        for successor in sorted(successors[component_id], key=lambda item: component_order[item]):
            indegree[successor] -= 1
            if indegree[successor] == 0:
                queue.append(successor)
        queue.sort(key=lambda item: component_order[item])

    for component_id in component_ids:
        if component_id not in visited_topo:
            component_topo_order[component_id] = topo_position
            topo_position += 1

    color_groups: Dict[int, List[int]] = defaultdict(list)
    for component_id in component_ids:
        color_groups[color_map[component_id]].append(component_id)

    batches: List[List[MoveOp]] = []
    for color in sorted(color_groups, key=lambda item: min(component_topo_order.get(component_id, component_order[component_id]) for component_id in color_groups[item])):
        batch_moves: List[MoveOp] = []
        for component_id in sorted(color_groups[color], key=lambda item: component_topo_order[item]):
            batch_moves.extend(sorted(component_members[component_id], key=lambda move: nodes.index(move)))
        batch = sorted(batch_moves, key=lambda move: nodes.index(move))
        if len(batch) == 1:
            batches.append(batch)
            continue
        if not batch_feasible(hw, batch, stationary)[0]:
            batches.extend([[move] for move in batch])
        else:
            batches.append(batch)
    return batches


def plan_batches(
    hw: HardwareSpec,
    moves: Sequence[MoveOp],
    stationary: Optional[Set[Position]] = None,
) -> List[List[MoveOp]]:
    return _color_components(hw, moves, stationary)


def plan_group_batches(
    hw: HardwareSpec,
    calls: Sequence[MoveCall],
    initial: Optional[Dict[int, Position]] = None,
) -> GroupBatchPlan:
    moves = [move for call in calls for move in call.moves]
    if not moves:
        return GroupBatchPlan([], False, "")

    stationary = set(initial.values()) if initial else set()
    batches = _color_components(hw, moves, stationary)
    return GroupBatchPlan(batches, False, "")


def batch_duration(hw: HardwareSpec, moves: Sequence[MoveOp]) -> float:
    if not moves:
        return 0.0
    return max(hw.move_duration(hw.distance(move.src, move.dst)) for move in moves)