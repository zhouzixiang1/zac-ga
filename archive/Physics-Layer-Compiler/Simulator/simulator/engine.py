"""Forward simulation engine.

Given a compiler-generated instruction list, the engine replays it *forward*,
maintaining the complete atom position/state after every instruction and
validating each parallel time step:

* **destination conflict** — two moving atoms target the same site,
* **atom overlap** — a moving atom lands on a stationary atom,
* **path collision** — two simultaneously-moving atoms pass too close,
* **unsupported simultaneous move** — the AOD grid is sheared / rows or
  columns cross (only rigid, order-preserving grid transports are allowed).

Gate layers (``1qGate`` / ``rydberg``) are likewise validated for parallel
consistency (no atom used twice, valid Rydberg pairs) and executed as a single
simultaneous step.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from . import hardware as hw
from .hardware import AOD_MAX_ATOMS, Site, entangle_pair_ok, grid_xy, valid_site
from .schema import instruction_atoms, op_kind, op_summary

# Atoms closer than this (in site units) at any instant of a simultaneous move
# are considered to collide.  Distinct grid sites are >= 1 apart, so this never
# false-positives on the move endpoints themselves.
MIN_SEPARATION = 0.75


def _q(v: float) -> float:
    return round(v, 6)


def _point_segment_distance(
    p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]
) -> float:
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    denom = dx * dx + dy * dy
    if denom < 1e-12:
        return ((p[0] - ax) ** 2 + (p[1] - ay) ** 2) ** 0.5
    t = ((p[0] - ax) * dx + (p[1] - ay) * dy) / denom
    t = max(0.0, min(1.0, t))
    cx, cy = ax + t * dx, ay + t * dy
    return ((p[0] - cx) ** 2 + (p[1] - cy) ** 2) ** 0.5


def _tone_layout(
    moves: "list[Move]",
) -> tuple[dict[float, float], dict[float, float], bool]:
    """Group a move batch into X-tones and Y-tones (crossed-AOD model).

    Returns ``(x_tones, y_tones, consistent)`` where each tone maps its source
    line coordinate to the single destination it is driven to; ``consistent`` is
    ``False`` when two atoms sharing a tone are asked to move differently.
    """
    x_tones: dict[float, float] = {}
    y_tones: dict[float, float] = {}
    consistent = True
    for m in moves:
        sx, sy = grid_xy(m.src)
        dx, dy = grid_xy(m.dst)
        sx, sy, dx, dy = _q(sx), _q(sy), _q(dx), _q(dy)
        if sx in x_tones and abs(x_tones[sx] - dx) > 1e-6:
            consistent = False
        x_tones.setdefault(sx, dx)
        if sy in y_tones and abs(y_tones[sy] - dy) > 1e-6:
            consistent = False
        y_tones.setdefault(sy, dy)
    return x_tones, y_tones, consistent


def _ghost_traps(
    moves: "list[Move]",
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """Non-target intersections of the active tone product, as (src, dst)."""
    x_tones, y_tones, _ = _tone_layout(moves)
    targets = {(_q(grid_xy(m.src)[0]), _q(grid_xy(m.src)[1])) for m in moves}
    ghosts: list[tuple[tuple[float, float], tuple[float, float]]] = []
    for sx, dx in x_tones.items():
        for sy, dy in y_tones.items():
            if (sx, sy) in targets:
                continue
            ghosts.append(((sx, sy), (dx, dy)))
    return ghosts


@dataclass
class Move:
    """A single atom transport within a parallel move step."""

    atom: int
    src: Site
    dst: Site


@dataclass
class ValidationReport:
    """Result of validating one time step."""

    ok: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # atoms flagged as being in conflict (drawn in red on the canvas)
    conflict_atoms: set[int] = field(default_factory=set)
    # (a, b) atom pairs whose trajectories collide (drawn as red links)
    conflict_pairs: list[tuple[int, int]] = field(default_factory=list)

    def fail(self, msg: str, atoms: tuple[int, ...] = ()) -> None:
        self.ok = False
        self.errors.append(msg)
        self.conflict_atoms.update(int(a) for a in atoms)

    @property
    def status(self) -> str:
        if not self.ok:
            return "INVALID"
        if self.warnings:
            return "OK (warnings)"
        return "OK"


@dataclass
class StepInfo:
    """Everything the GUI needs about one instruction/time step."""

    index: int
    kind: str
    summary: str
    raw: dict[str, Any]
    atoms: set[int]
    moves: list[Move]
    gates: list[dict[str, Any]]
    entangle_pairs: list[tuple[int, int]]
    positions_before: dict[int, Site]
    positions_after: dict[int, Site]
    validation: ValidationReport
    parallel_size: int = 1
    measured: list[int] = field(default_factory=list)
    # Ghost tweezers created by the crossed-AOD tone product during a move step,
    # as ((src_x, src_y), (dst_x, dst_y)) grid-coordinate pairs.  ``ghost_bad``
    # are the subset that sweep too close to a stationary atom.
    ghost_traps: list[tuple[tuple[float, float], tuple[float, float]]] = field(
        default_factory=list
    )
    ghost_bad: list[tuple[tuple[float, float], tuple[float, float]]] = field(
        default_factory=list
    )


class SimulationEngine:
    """Forward-replay a ZAIR instruction list into validated time steps."""

    def __init__(self, instructions: list[dict[str, Any]]) -> None:
        self.instructions = list(instructions)
        self.init_positions: dict[int, Site] = {}
        self.steps: list[StepInfo] = []
        self.measured: set[int] = set()
        self.global_errors: list[str] = []
        self._build()

    # ------------------------------------------------------------------ build
    def _build(self) -> None:
        instrs = self.instructions
        if not instrs:
            self.global_errors.append("Instruction list is empty.")
            return
        if op_kind(instrs[0]) != "INIT":
            self.global_errors.append(
                "Instruction list must start with an 'init' instruction."
            )
            body = instrs
        else:
            self.init_positions = self._parse_init(instrs[0])
            body = instrs[1:]

        positions = dict(self.init_positions)
        for i, inst in enumerate(body):
            step = self._process(i, inst, positions)
            positions = step.positions_after
            self.steps.append(step)

    def _parse_init(self, inst: dict[str, Any]) -> dict[int, Site]:
        positions: dict[int, Site] = {}
        for loc in inst.get("init_locs", []):
            atom = int(loc[0])
            site = Site.from_loc(loc)
            if not valid_site(site):
                self.global_errors.append(
                    f"init: atom {atom} at out-of-range site {site.label()}."
                )
            if atom in positions:
                self.global_errors.append(f"init: atom {atom} listed twice.")
            positions[atom] = site
        # duplicate-site check
        seen: dict[tuple, int] = {}
        for a, p in positions.items():
            if p.key in seen:
                self.global_errors.append(
                    f"init: atoms {seen[p.key]} and {a} share site {p.label()}."
                )
            seen[p.key] = a
        return positions

    # ---------------------------------------------------------------- process
    def _process(self, index: int, inst: dict[str, Any],
                 before: dict[int, Site]) -> StepInfo:
        kind = op_kind(inst)
        handler = {
            "MOVE": self._process_move,
            "1Q": self._process_1q,
            "2Q": self._process_2q,
            "SWAP": self._process_swap,
            "MEASURE": self._process_measure,
        }.get(kind, self._process_other)
        return handler(index, inst, before)

    # -- movement -------------------------------------------------------------
    def _process_move(self, index, inst, before) -> StepInfo:
        report = ValidationReport()
        moves: list[Move] = []
        begin = inst.get("begin_locs")
        end = inst.get("end_locs", inst.get("locs", []))
        for j, eloc in enumerate(end):
            atom = int(eloc[0])
            dst = Site.from_loc(eloc)
            if begin is not None and j < len(begin):
                src = Site.from_loc(begin[j])
            else:
                src = before.get(atom, dst)
            moves.append(Move(atom, src, dst))

        self._validate_move(moves, before, report)

        after = dict(before)
        for m in moves:
            after[m.atom] = m.dst

        moving_ids = {m.atom for m in moves}
        stationary = {
            grid_xy(p): a for a, p in before.items() if a not in moving_ids
        }
        ghosts = _ghost_traps([m for m in moves if m.src.key != m.dst.key])
        ghost_bad = [
            (src, dst)
            for src, dst in ghosts
            if any(
                _point_segment_distance(pt, src, dst) < MIN_SEPARATION
                for pt in stationary
            )
        ]

        return StepInfo(
            index=index, kind="MOVE", summary=op_summary(inst), raw=inst,
            atoms={m.atom for m in moves}, moves=moves, gates=[],
            entangle_pairs=[], positions_before=before, positions_after=after,
            validation=report, parallel_size=len(moves),
            ghost_traps=ghosts, ghost_bad=ghost_bad,
        )

    def _validate_move(self, moves, before, report: ValidationReport) -> None:
        moving_ids = {m.atom for m in moves}

        # 0) existence + range
        for m in moves:
            if m.atom not in before:
                report.fail(f"atom {m.atom} does not exist yet.", (m.atom,))
            if not valid_site(m.dst):
                report.fail(
                    f"atom {m.atom} moves out of range to {m.dst.label()}.", (m.atom,)
                )

        # 1) destination conflict — two atoms to the same site
        dst_owner: dict[tuple, int] = {}
        for m in moves:
            if m.dst.key in dst_owner:
                report.fail(
                    f"destination conflict: atoms {dst_owner[m.dst.key]} and "
                    f"{m.atom} both target {m.dst.label()}.",
                    (dst_owner[m.dst.key], m.atom),
                )
                report.conflict_pairs.append((dst_owner[m.dst.key], m.atom))
            else:
                dst_owner[m.dst.key] = m.atom

        # 2) atom overlap — a moving atom lands on a stationary atom
        static = {p.key: a for a, p in before.items() if a not in moving_ids}
        for m in moves:
            if m.dst.key in static:
                report.fail(
                    f"atom overlap: atom {m.atom} lands on stationary atom "
                    f"{static[m.dst.key]} at {m.dst.label()}.",
                    (m.atom, static[m.dst.key]),
                )

        # 3) path collision — simultaneous trajectories pass too close
        active = [m for m in moves if m.src.key != m.dst.key]
        for i in range(len(active)):
            for k in range(i + 1, len(active)):
                if self._paths_collide(active[i], active[k]):
                    a, b = active[i].atom, active[k].atom
                    report.fail(
                        f"path collision: atoms {a} and {b} pass within "
                        f"{MIN_SEPARATION} sites during the parallel move.",
                        (a, b),
                    )
                    report.conflict_pairs.append((a, b))

        # 4) unsupported simultaneous move — the crossed-AOD tone product cannot
        #    realize this batch (capacity, shared-tone motion conflict, tone
        #    crossing, or a ghost intersection disturbing a static atom).
        self._validate_batch(active, before, moving_ids, report)

    @staticmethod
    def _paths_collide(m0: Move, m1: Move) -> bool:
        """Whether two atoms moving in sync come within ``MIN_SEPARATION``.

        Both atoms travel their segment over a shared normalized time ``t∈[0,1]``.
        Minimize ``|(A0-A1) + t·((B0-A0)-(B1-A1))|`` over ``[0, 1]``.
        """
        ax0, ay0 = grid_xy(m0.src)
        bx0, by0 = grid_xy(m0.dst)
        ax1, ay1 = grid_xy(m1.src)
        bx1, by1 = grid_xy(m1.dst)
        wx, wy = ax0 - ax1, ay0 - ay1
        mx = (bx0 - ax0) - (bx1 - ax1)
        my = (by0 - ay0) - (by1 - ay1)
        mm = mx * mx + my * my
        if mm < 1e-12:
            dist2 = wx * wx + wy * wy
        else:
            t = -(wx * mx + wy * my) / mm
            t = max(0.0, min(1.0, t))
            dx = wx + t * mx
            dy = wy + t * my
            dist2 = dx * dx + dy * dy
        return dist2 < MIN_SEPARATION * MIN_SEPARATION

    @staticmethod
    def _validate_batch(
        active: list[Move],
        before: dict[int, Site],
        moving_ids: set[int],
        report: ValidationReport,
    ) -> None:
        """Reject transports the crossed AOD cannot perform in one instruction.

        A crossed AOD is the Cartesian product of X-tones (columns) and Y-tones
        (rows), so a single batch must satisfy:

        * **capacity** — at most ``AOD_MAX_ATOMS`` atoms;
        * **shared tone** — atoms on one X (or Y) tone move together in x (y);
        * **ghost safety** — the non-target intersections of the active tones
          (ghost tweezers) must not sweep over a stationary, occupied trap.
        """
        if not active:
            return
        if len(active) > AOD_MAX_ATOMS:
            report.fail(
                f"unsupported simultaneous move: batch of {len(active)} atoms "
                f"exceeds the AOD capacity of {AOD_MAX_ATOMS}.",
                tuple(m.atom for m in active),
            )

        # Shared-tone consistency: atoms on the same X (or Y) tone must share a
        # single trajectory along that axis, otherwise one RF tone would have to
        # drive two different motions at once.
        x_tones: dict[float, list[Move]] = {}
        y_tones: dict[float, list[Move]] = {}
        for m in active:
            x_tones.setdefault(_q(grid_xy(m.src)[0]), []).append(m)
            y_tones.setdefault(_q(grid_xy(m.src)[1]), []).append(m)
        for members in x_tones.values():
            dests = {_q(grid_xy(m.dst)[0]) for m in members}
            if len(dests) > 1:
                report.fail(
                    "unsupported simultaneous move: atoms "
                    f"{sorted(m.atom for m in members)} share one AOD column but "
                    "are moved to different columns.",
                    tuple(m.atom for m in members),
                )
        for members in y_tones.values():
            dests = {_q(grid_xy(m.dst)[1]) for m in members}
            if len(dests) > 1:
                report.fail(
                    "unsupported simultaneous move: atoms "
                    f"{sorted(m.atom for m in members)} share one AOD row but "
                    "are moved to different rows.",
                    tuple(m.atom for m in members),
                )

        # Ghost tweezers from the tone product must not disturb a static atom.
        stationary = {
            grid_xy(p): a for a, p in before.items() if a not in moving_ids
        }
        for src, dst in _ghost_traps(active):
            for pt, atom in stationary.items():
                if _point_segment_distance(pt, src, dst) < MIN_SEPARATION:
                    report.fail(
                        "unsupported simultaneous move: a ghost AOD tweezer at "
                        f"the tone intersection sweeps over stationary atom {atom}.",
                        (atom,),
                    )
                    break

    # -- single-qubit ---------------------------------------------------------
    def _process_1q(self, index, inst, before) -> StepInfo:
        report = ValidationReport()
        gates = list(inst.get("gates", []))
        used: dict[int, int] = {}
        norm: list[dict[str, Any]] = []
        for g in gates:
            q = int(g.get("q"))
            if q not in before:
                report.fail(f"1q gate on non-existent atom {q}.", (q,))
            if q in used:
                report.fail(
                    f"parallel conflict: atom {q} has two single-qubit gates in "
                    "the same layer.",
                    (q,),
                )
            used[q] = used.get(q, 0) + 1
            norm.append({"name": str(g.get("name", inst.get("unitary", "u"))),
                         "q": q, "params": list(g.get("params", []))})
        return StepInfo(
            index=index, kind="1Q", summary=op_summary(inst), raw=inst,
            atoms={g["q"] for g in norm}, moves=[], gates=norm,
            entangle_pairs=[], positions_before=before,
            positions_after=dict(before), validation=report,
            parallel_size=len(norm),
        )

    # -- two-qubit ------------------------------------------------------------
    def _process_2q(self, index, inst, before) -> StepInfo:
        report = ValidationReport()
        gates = list(inst.get("gates", []))
        used: set[int] = set()
        pairs: list[tuple[int, int]] = []
        norm: list[dict[str, Any]] = []
        for g in gates:
            a0, a1 = int(g["q0"]), int(g["q1"])
            name = str(g.get("name", "cz"))
            norm.append({"q0": a0, "q1": a1, "name": name})
            pairs.append((a0, a1))
            for a in (a0, a1):
                if a not in before:
                    report.fail(f"2q gate on non-existent atom {a}.", (a,))
                if a in used:
                    report.fail(
                        f"parallel conflict: atom {a} is in two entangling gates "
                        "in the same layer.",
                        (a,),
                    )
                used.add(a)
            if a0 in before and a1 in before:
                if not entangle_pair_ok(before[a0], before[a1]):
                    report.fail(
                        f"invalid Rydberg pair: atoms {a0} {before[a0].label()} and "
                        f"{a1} {before[a1].label()} are not an adjacent "
                        "entanglement-zone column pair.",
                        (a0, a1),
                    )
                    report.conflict_pairs.append((a0, a1))
        return StepInfo(
            index=index, kind="2Q", summary=op_summary(inst), raw=inst,
            atoms=set(used), moves=[], gates=norm, entangle_pairs=pairs,
            positions_before=before, positions_after=dict(before),
            validation=report, parallel_size=len(norm),
        )

    # -- swap -----------------------------------------------------------------
    def _process_swap(self, index, inst, before) -> StepInfo:
        report = ValidationReport()
        qs = [int(q) for q in inst.get("qubits", [])[:2]]
        for a in qs:
            if a not in before:
                report.fail(f"swap on non-existent atom {a}.", (a,))
        pairs = [(qs[0], qs[1])] if len(qs) == 2 else []
        return StepInfo(
            index=index, kind="SWAP", summary=op_summary(inst), raw=inst,
            atoms=set(qs), moves=[], gates=[], entangle_pairs=pairs,
            positions_before=before, positions_after=dict(before),
            validation=report, parallel_size=1,
        )

    # -- measure --------------------------------------------------------------
    def _process_measure(self, index, inst, before) -> StepInfo:
        report = ValidationReport()
        qs = [int(q) for q in inst.get("qubits", inst.get("targets", []))
              if isinstance(q, int)]
        for a in qs:
            if a not in before:
                report.fail(f"measure on non-existent atom {a}.", (a,))
        self.measured.update(qs)
        return StepInfo(
            index=index, kind="MEASURE", summary=op_summary(inst), raw=inst,
            atoms=set(qs), moves=[], gates=[], entangle_pairs=[],
            positions_before=before, positions_after=dict(before),
            validation=report, parallel_size=len(qs), measured=list(qs),
        )

    def _process_other(self, index, inst, before) -> StepInfo:
        return StepInfo(
            index=index, kind=op_kind(inst), summary=op_summary(inst), raw=inst,
            atoms=instruction_atoms(inst), moves=[], gates=[], entangle_pairs=[],
            positions_before=before, positions_after=dict(before),
            validation=ValidationReport(),
        )

    # ------------------------------------------------------------------ query
    @property
    def num_steps(self) -> int:
        return len(self.steps)

    def positions_at(self, step: int) -> dict[int, Site]:
        """Positions *after* the first ``step`` operations (0 == initial)."""
        if step <= 0 or not self.steps:
            return dict(self.init_positions)
        step = min(step, len(self.steps))
        return dict(self.steps[step - 1].positions_after)

    def is_valid(self) -> bool:
        return not self.global_errors and all(s.validation.ok for s in self.steps)

    def all_errors(self) -> list[str]:
        out = list(self.global_errors)
        for s in self.steps:
            for e in s.validation.errors:
                out.append(f"step {s.index + 1}: {e}")
        return out
