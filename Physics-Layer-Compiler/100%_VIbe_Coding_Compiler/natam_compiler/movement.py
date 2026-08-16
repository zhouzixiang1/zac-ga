"""AOD-aware parallel movement planning and constraint checking.

Neutral-atom hardware transports qubits with an acousto-optic deflector (AOD).
An AOD can move *many* atoms at once, but only when their trajectories are
mutually compatible.  This module turns a bag of desired :class:`MoveOp`
transfers into an ordered list of *movement batches*, where every batch is a
set of moves that the machine can safely execute in parallel.

The constraints enforced here (all configurable through
:class:`~natam_compiler.hardware.HardwareSpec`) are:

* **Batch capacity** -- at most ``aod_max_atoms`` atoms move per batch.
* **Minimum separation** -- two parallel trajectories may never come closer
  than ``min_atom_separation`` at any instant (no collisions / crossings).
* **Trap-crossing / ordering** -- when ``allow_trap_crossing`` is ``False`` the
  relative ordering of atoms along each axis is preserved (no swapping).
* **Stationary obstacles** -- a moving atom may not pass through (or too close
  to) an occupied, stationary site.
* **Movement range & kinematics** -- every move stays on the grid and respects
  the maximum distance, velocity, acceleration and duration.

The planner greedily packs moves into as few batches as possible, which
directly optimizes for *maximum parallelism* and a *minimum number of
movement batches*.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import hypot
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .hardware import HardwareSpec, Position
from .operations import MoveOp

Point = Tuple[float, float]


@dataclass
class MoveViolation:
    """A single constraint that a move (or a pair of moves) violates."""

    reason: str          # e.g. "min_separation", "trap_crossing", "max_distance"
    detail: str = ""
    atoms: Tuple[int, ...] = ()


# --------------------------------------------------------------------------- #
#  Geometry helpers
# --------------------------------------------------------------------------- #
def _endpoints(hw: HardwareSpec, move: MoveOp) -> Tuple[Point, Point]:
    return hw.coordinate(move.src), hw.coordinate(move.dst)


def _lerp(a: Point, b: Point, t: float) -> Point:
    return (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))


def segment_min_separation(a1: Point, b1: Point, a2: Point, b2: Point) -> float:
    """Minimum distance between two atoms that move synchronously.

    Both atoms travel their straight segment over a shared, normalized time
    ``t in [0, 1]`` so the relative position is linear in ``t`` and the closest
    approach has a closed-form solution.
    """
    w0 = (a1[0] - a2[0], a1[1] - a2[1])
    w1 = (b1[0] - b2[0], b1[1] - b2[1])
    dw = (w1[0] - w0[0], w1[1] - w0[1])
    denom = dw[0] * dw[0] + dw[1] * dw[1]
    if denom < 1e-12:
        t = 0.0
    else:
        t = -(w0[0] * dw[0] + w0[1] * dw[1]) / denom
        t = max(0.0, min(1.0, t))
    rx = w0[0] + t * dw[0]
    ry = w0[1] + t * dw[1]
    return hypot(rx, ry)


def _point_segment_distance(p: Point, a: Point, b: Point) -> float:
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    denom = dx * dx + dy * dy
    if denom < 1e-12:
        return hypot(p[0] - ax, p[1] - ay)
    t = ((p[0] - ax) * dx + (p[1] - ay) * dy) / denom
    t = max(0.0, min(1.0, t))
    cx, cy = ax + t * dx, ay + t * dy
    return hypot(p[0] - cx, p[1] - cy)


def _aod_grid_conflict(
    a1: Point, b1: Point, a2: Point, b2: Point, allow_crossing: bool
) -> Tuple[bool, str]:
    """AOD grid feasibility for two atoms moved by the same AOD batch.

    An AOD is a *crossed pair of 1-D acousto-optic deflectors*: every column is
    a single vertical deflection tone and every row a single horizontal tone.
    Two consequences constrain which moves may share one batch:

    * **Shared line** -- atoms that start on the same AOD line (identical source
      ``x`` for a column, or identical source ``y`` for a row) are driven by one
      tone and therefore *must translate together* along that axis.  They cannot
      end on different lines.
    * **Order preservation** -- atoms on distinct lines may not swap their
      relative order along an axis (the tones cannot cross), unless
      ``allow_trap_crossing`` permits it.

    Returns ``(conflict, reason)`` where ``reason`` is ``"aod_shared_line"`` or
    ``"trap_crossing"`` (empty when the pair is compatible).
    """
    eps = 1e-9
    axes = (
        (a1[0], b1[0], a2[0], b2[0]),  # x / column tone
        (a1[1], b1[1], a2[1], b2[1]),  # y / row tone
    )
    for s_i, d_i, s_j, d_j in axes:
        if abs(s_i - s_j) <= eps:
            # Same source line -> the shared tone moves both atoms identically.
            if abs(d_i - d_j) > 1e-6:
                return True, "aod_shared_line"
        elif not allow_crossing and (s_i - s_j) * (d_i - d_j) < -eps:
            # Distinct lines swapped their relative order -> tones would cross.
            return True, "trap_crossing"
    return False, ""


# --------------------------------------------------------------------------- #
#  Per-move and pairwise constraint checks
# --------------------------------------------------------------------------- #
def validate_move(hw: HardwareSpec, move: MoveOp) -> List[MoveViolation]:
    """Check a single move against range and kinematic limits."""
    violations: List[MoveViolation] = []
    if not hw.valid(move.src) or not hw.valid(move.dst):
        violations.append(
            MoveViolation("out_of_range", f"{move.src}->{move.dst}", (move.atom,))
        )
    distance = hw.distance(move.src, move.dst)
    if distance > hw.aod_max_distance + 1e-9:
        violations.append(
            MoveViolation(
                "max_distance",
                f"{distance:.2f} > {hw.aod_max_distance}",
                (move.atom,),
            )
        )
    duration = hw.move_duration(distance)
    if duration > hw.aod_max_move_duration + 1e-9:
        violations.append(
            MoveViolation(
                "max_duration",
                f"{duration:.2f} > {hw.aod_max_move_duration}",
                (move.atom,),
            )
        )
    return violations


def moves_compatible(
    hw: HardwareSpec, m1: MoveOp, m2: MoveOp
) -> Tuple[bool, str]:
    """Return ``(compatible, reason)`` for two moves sharing one batch."""
    a1, b1 = _endpoints(hw, m1)
    a2, b2 = _endpoints(hw, m2)
    if segment_min_separation(a1, b1, a2, b2) < hw.min_atom_separation - 1e-9:
        return False, "min_separation"
    conflict, reason = _aod_grid_conflict(a1, b1, a2, b2, hw.allow_trap_crossing)
    if conflict:
        return False, reason
    return True, ""


def move_hits_stationary(
    hw: HardwareSpec, move: MoveOp, stationary: Set[Position]
) -> bool:
    """Return ``True`` if a moving atom passes too close to a stationary site."""
    a, b = _endpoints(hw, move)
    for pos in stationary:
        if pos == move.src or pos == move.dst:
            continue
        if _point_segment_distance(hw.coordinate(pos), a, b) < (
            hw.min_atom_separation - 1e-9
        ):
            return True
    return False


# --------------------------------------------------------------------------- #
#  2-D crossed-AOD tone model
# --------------------------------------------------------------------------- #
#  A crossed AOD is *not* a bag of independent 2-D tweezers.  The array is the
#  Cartesian product of the active X-axis RF tones (each fixes one column) and
#  Y-axis RF tones (each fixes one row): a tweezer can appear at *every*
#  intersection of an active X tone and an active Y tone.  Consequences:
#
#    * atoms sharing an X tone (identical source x) must move together in x;
#    * atoms sharing a Y tone (identical source y) must move together in y;
#    * the product of the active tones creates extra "ghost" tweezers at
#      intersections that hold no intended atom -- these must not disturb other
#      occupied traps or violate the minimum spacing.
def _q(v: float) -> float:
    return round(v, 6)


def aod_tone_layout(
    hw: HardwareSpec, moves: Sequence[MoveOp]
) -> Tuple[Dict[float, float], Dict[float, float], bool]:
    """Group a batch's moves into X- and Y-tones.

    Returns ``(x_tones, y_tones, consistent)`` where ``x_tones`` maps each
    source column coordinate to its (single) destination column, ``y_tones``
    likewise for rows, and ``consistent`` is ``False`` when two atoms driven by
    one tone are asked to follow different trajectories along that axis.
    """
    x_tones: Dict[float, float] = {}
    y_tones: Dict[float, float] = {}
    consistent = True
    for move in moves:
        sx, sy = hw.coordinate(move.src)
        dx, dy = hw.coordinate(move.dst)
        sx, sy, dx, dy = _q(sx), _q(sy), _q(dx), _q(dy)
        if sx in x_tones and abs(x_tones[sx] - dx) > 1e-6:
            consistent = False
        x_tones.setdefault(sx, dx)
        if sy in y_tones and abs(y_tones[sy] - dy) > 1e-6:
            consistent = False
        y_tones.setdefault(sy, dy)
    return x_tones, y_tones, consistent


def _tones_cross(tones: Dict[float, float]) -> bool:
    """True if any two 1-D tones swap their relative order during the move."""
    items = sorted(tones.items())
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if (items[i][0] - items[j][0]) * (items[i][1] - items[j][1]) < -1e-9:
                return True
    return False


def _tones_min_gap(tones: Dict[float, float]) -> float:
    """Smallest separation reached between any two 1-D tones over ``t∈[0,1]``."""
    items = sorted(tones.items())
    best = float("inf")
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            s = items[i][0] - items[j][0]
            d = items[i][1] - items[j][1]
            if abs(d - s) < 1e-12:
                gap = abs(s)
            else:
                t = max(0.0, min(1.0, -s / (d - s)))
                gap = abs(s + t * (d - s))
            best = min(best, gap)
    return best


def aod_ghost_traps(
    hw: HardwareSpec, moves: Sequence[MoveOp]
) -> List[Tuple[Point, Point]]:
    """Non-target tweezers created by the tone product, as ``(src, dst)`` pairs.

    Every intersection of an active X tone and Y tone is a tweezer; the ones
    that hold no intended atom are *ghost traps* which nonetheless sweep from
    their source intersection to their destination intersection.
    """
    x_tones, y_tones, _ = aod_tone_layout(hw, moves)
    target_pts = set()
    for m in moves:
        sx, sy = hw.coordinate(m.src)
        target_pts.add((_q(sx), _q(sy)))
    ghosts: List[Tuple[Point, Point]] = []
    for sx, dx in x_tones.items():
        for sy, dy in y_tones.items():
            if (sx, sy) in target_pts:
                continue
            ghosts.append(((sx, sy), (dx, dy)))
    return ghosts


def batch_feasible(
    hw: HardwareSpec,
    moves: Sequence[MoveOp],
    stationary: Optional[Set[Position]] = None,
) -> Tuple[bool, str, str]:
    """Whether one AOD instruction can perform every move in ``moves`` at once.

    Returns ``(ok, reason, detail)``.  Enforces the full crossed-AOD model:
    per-tone motion consistency, no tone crossing (unless allowed), minimum
    row/column spacing, unique destinations, and ghost-trap safety against the
    supplied stationary (SLM-trapped) atoms.
    """
    stationary = stationary or set()
    if len(moves) > hw.aod_max_atoms:
        return False, "capacity", f"{len(moves)} > {hw.aod_max_atoms}"

    x_tones, y_tones, consistent = aod_tone_layout(hw, moves)
    if not consistent:
        return False, "aod_shared_line", "atoms on one tone need different motion"
    if not hw.allow_trap_crossing and (_tones_cross(x_tones) or _tones_cross(y_tones)):
        return False, "trap_crossing", "row/column tones cross"
    if (
        _tones_min_gap(x_tones) < hw.min_atom_separation - 1e-9
        or _tones_min_gap(y_tones) < hw.min_atom_separation - 1e-9
    ):
        return False, "min_separation", "rows/columns collide"

    dst_seen: Dict[Point, int] = {}
    for m in moves:
        key = (_q(hw.coordinate(m.dst)[0]), _q(hw.coordinate(m.dst)[1]))
        if key in dst_seen:
            return False, "dest_overlap", f"atoms {dst_seen[key]} and {m.atom}"
        dst_seen[key] = m.atom

    stat_pts = [hw.coordinate(p) for p in stationary]
    for src, dst in aod_ghost_traps(hw, moves):
        for p in stat_pts:
            if _point_segment_distance(p, src, dst) < hw.min_atom_separation - 1e-9:
                return False, "ghost_trap", "ghost tweezer disturbs a static atom"
    return True, "", ""


# --------------------------------------------------------------------------- #
#  Batch planning
# --------------------------------------------------------------------------- #
def plan_batches(
    hw: HardwareSpec,
    moves: Sequence[MoveOp],
    stationary: Optional[Set[Position]] = None,
) -> List[List[MoveOp]]:
    """Pack ``moves`` into the fewest AOD-realizable parallel batches.

    A greedy first-fit strategy adds each move to the earliest existing batch
    that remains a valid single crossed-AOD instruction (see
    :func:`batch_feasible`); otherwise a new batch is opened.  Every move not in
    the trial batch is treated as a stationary obstacle sitting at its source,
    so ghost tweezers created by the tone product cannot silently disturb atoms
    waiting for a later batch.  Ordering by start coordinate keeps spatially
    coherent moves together for tight packing and deterministic output.
    """
    base_stationary = set(stationary or set())
    ordered = sorted(moves, key=lambda m: (hw.coordinate(m.src), hw.coordinate(m.dst)))
    batches: List[List[MoveOp]] = []
    for move in ordered:
        placed = False
        for batch in batches:
            if len(batch) >= hw.aod_max_atoms:
                continue
            trial = batch + [move]
            trial_atoms = {m.atom for m in trial}
            obstacles = set(base_stationary)
            obstacles.update(m.src for m in ordered if m.atom not in trial_atoms)
            if batch_feasible(hw, trial, obstacles)[0]:
                batch.append(move)
                placed = True
                break
        if not placed:
            batches.append([move])
    return batches


@dataclass
class BatchDiagnostics:
    """Constraint report for one movement batch (used by the animator)."""

    invalid_moves: Dict[int, List[str]] = field(default_factory=dict)
    separation_conflicts: List[Tuple[int, int]] = field(default_factory=list)
    crossing_conflicts: List[Tuple[int, int]] = field(default_factory=list)
    stationary_conflicts: List[int] = field(default_factory=list)
    # Ghost tweezers created by the tone product: every (src, dst) intersection
    # that holds no intended atom, plus the subset that disturb a static atom.
    ghost_traps: List[Tuple[Point, Point]] = field(default_factory=list)
    ghost_conflicts: List[Tuple[Point, Point]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (
            self.invalid_moves
            or self.separation_conflicts
            or self.crossing_conflicts
            or self.stationary_conflicts
            or self.ghost_conflicts
        )

    def flagged_atoms(self) -> Set[int]:
        atoms: Set[int] = set(self.invalid_moves)
        atoms.update(self.stationary_conflicts)
        for a, b in self.separation_conflicts + self.crossing_conflicts:
            atoms.update((a, b))
        return atoms


def diagnose_batch(
    hw: HardwareSpec,
    moves: Sequence[MoveOp],
    stationary: Optional[Set[Position]] = None,
) -> BatchDiagnostics:
    """Analyze a batch and report every constraint it violates."""
    stationary = stationary or set()
    diag = BatchDiagnostics()
    for move in moves:
        problems = validate_move(hw, move)
        if problems:
            diag.invalid_moves[move.atom] = [p.reason for p in problems]
        if move_hits_stationary(hw, move, stationary):
            diag.stationary_conflicts.append(move.atom)
    for i in range(len(moves)):
        for j in range(i + 1, len(moves)):
            ok, reason = moves_compatible(hw, moves[i], moves[j])
            if ok:
                continue
            pair = (moves[i].atom, moves[j].atom)
            if reason == "min_separation":
                diag.separation_conflicts.append(pair)
            elif reason in ("trap_crossing", "aod_shared_line"):
                diag.crossing_conflicts.append(pair)

    # Ghost tweezers from the crossed-AOD tone product, and the ones that sweep
    # too close to a static (SLM-trapped) atom.
    stat_pts = [hw.coordinate(p) for p in stationary]
    for src, dst in aod_ghost_traps(hw, moves):
        diag.ghost_traps.append((src, dst))
        if any(
            _point_segment_distance(p, src, dst) < hw.min_atom_separation - 1e-9
            for p in stat_pts
        ):
            diag.ghost_conflicts.append((src, dst))
    return diag


def batch_duration(hw: HardwareSpec, moves: Sequence[MoveOp]) -> float:
    """Smooth-trajectory duration of a batch (limited by its longest move)."""
    if not moves:
        return 0.0
    return max(hw.move_duration(hw.distance(m.src, m.dst)) for m in moves)
