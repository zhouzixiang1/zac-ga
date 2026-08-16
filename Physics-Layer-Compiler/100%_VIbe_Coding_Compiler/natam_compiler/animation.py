"""Animate a compiled neutral-atom program.

:func:`animate_compilation` replays a :class:`CompileResult` (or a bare
:class:`~natam_compiler.operations.HardwareProgram`) as an animation that shows
atoms moving between the storage and entanglement zones.  It visualizes exactly
the AOD behaviour the compiler optimizes for:

* initial, intermediate and final atom positions,
* every atom in a movement batch moving **in the same frame** (true parallel
  movement),
* a colour code distinguishing stationary atoms, moving atoms and atoms taking
  part in the active gate,
* highlights for invalid trajectories, collisions and minimum-distance
  violations (computed with :mod:`natam_compiler.movement`),
* an on-screen read-out of the current timestep, movement batch and the
  duration of the running operation,
* playback (the returned :class:`~matplotlib.animation.FuncAnimation` object)
  and export to MP4 or GIF.

``matplotlib`` is imported lazily so the rest of the compiler has no hard
dependency on it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .hardware import HardwareSpec, Position, default_hardware
from .movement import BatchDiagnostics, diagnose_batch
from .operations import (
    INIT,
    MEASURE,
    MOVE,
    ONEQ,
    TWOQ,
    HardwareProgram,
    InitOp,
    MeasureOp,
    MoveOp,
    OneQubitGate,
    Stage,
    TwoQubitGate,
)

Point = Tuple[float, float]

# Category colours.
_C_STATIONARY = "#3d6fb4"   # parked atoms
_C_MOVING = "#e8892b"       # atoms currently transported by the AOD
_C_GATE = "#2ca02c"         # atoms taking part in the active gate
_C_INVALID = "#d62728"      # atoms whose trajectory violates a constraint


def _smoothstep(t: float) -> float:
    """A smooth (zero-velocity endpoints) easing used for the trajectories."""
    return t * t * (3.0 - 2.0 * t)


def _lerp(a: Point, b: Point, t: float) -> Point:
    return (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))


@dataclass
class _Frame:
    stage_index: int
    kind: str
    label: str
    duration: float
    batch_index: Optional[int]
    xy: Dict[int, Point]                     # atom -> plotted coordinate
    moving: Set[int] = field(default_factory=set)
    gate: Set[int] = field(default_factory=set)
    invalid: Set[int] = field(default_factory=set)
    trails: Dict[int, Tuple[Point, Point]] = field(default_factory=dict)
    conflict_lines: List[Tuple[Point, Point]] = field(default_factory=list)


def _gate_atoms(stage: Stage) -> Set[int]:
    atoms: Set[int] = set()
    for op in stage.ops:
        if isinstance(op, OneQubitGate):
            atoms.add(op.atom)
        elif isinstance(op, TwoQubitGate):
            atoms.update((op.atom0, op.atom1))
        elif isinstance(op, MeasureOp):
            atoms.add(op.atom)
    return atoms


def _conflict_lines(
    hw: HardwareSpec, moves: Sequence[MoveOp], diag: BatchDiagnostics, xy: Dict[int, Point]
) -> List[Tuple[Point, Point]]:
    lines: List[Tuple[Point, Point]] = []
    for a, b in diag.separation_conflicts + diag.crossing_conflicts:
        if a in xy and b in xy:
            lines.append((xy[a], xy[b]))
    return lines


def build_frames(
    program: HardwareProgram,
    hw: HardwareSpec,
    steps_per_move: int = 14,
    hold_frames: int = 5,
) -> List[_Frame]:
    """Expand a program into a flat list of drawable frames."""
    positions: Dict[int, Position] = {}
    frames: List[_Frame] = []
    batch_counter = 0

    for si, stage in enumerate(program.stages):
        if stage.kind == INIT:
            for op in stage.ops:
                if isinstance(op, InitOp):
                    positions[op.atom] = op.position
            xy = {a: hw.coordinate(p) for a, p in positions.items()}
            frames.append(
                _Frame(si, stage.kind, "init", stage.duration, None, xy)
            )

        elif stage.kind == MOVE:
            moves = [op for op in stage.ops if isinstance(op, MoveOp)]
            if not moves:
                continue
            batch_counter += 1
            stationary = {
                positions[a] for a in positions if a not in {m.atom for m in moves}
            }
            diag = diagnose_batch(hw, moves, stationary)
            invalid = diag.flagged_atoms()
            moving_ids = {m.atom for m in moves}
            src_xy = {m.atom: hw.coordinate(m.src) for m in moves}
            dst_xy = {m.atom: hw.coordinate(m.dst) for m in moves}

            for k in range(1, steps_per_move + 1):
                t = _smoothstep(k / steps_per_move)
                xy: Dict[int, Point] = {}
                for a, p in positions.items():
                    if a in moving_ids:
                        xy[a] = _lerp(src_xy[a], dst_xy[a], t)
                    else:
                        xy[a] = hw.coordinate(p)
                trails = {m.atom: (src_xy[m.atom], dst_xy[m.atom]) for m in moves}
                frames.append(
                    _Frame(
                        si,
                        stage.kind,
                        f"move batch {batch_counter}",
                        stage.duration,
                        batch_counter,
                        xy,
                        moving=set(moving_ids),
                        invalid=set(invalid),
                        trails=trails,
                        conflict_lines=_conflict_lines(hw, moves, diag, xy),
                    )
                )
            for m in moves:
                positions[m.atom] = m.dst

        else:  # 1q / 2q / measure -- gates held in place
            gate = _gate_atoms(stage)
            xy = {a: hw.coordinate(p) for a, p in positions.items()}
            label = {ONEQ: "1q gate", TWOQ: "CZ gate", MEASURE: "measure"}.get(
                stage.kind, stage.kind
            )
            for _ in range(hold_frames):
                frames.append(
                    _Frame(
                        si,
                        stage.kind,
                        label,
                        stage.duration,
                        None,
                        dict(xy),
                        gate=set(gate),
                    )
                )

    return frames


def _zone_bounds(hw: HardwareSpec) -> Tuple[float, float, float, float]:
    xs: List[float] = []
    ys: List[float] = []
    for zone in (hw.storage, hw.entanglement):
        for row in (0, zone.rows - 1):
            for col in (0, zone.cols - 1):
                x, y = hw.coordinate((zone.name, row, col))
                xs.append(x)
                ys.append(y)
    margin = 2.0
    return min(xs) - margin, max(xs) + margin, min(ys) - margin, max(ys) + margin


def animate_compilation(
    compilation_result,
    hardware_config: Optional[HardwareSpec] = None,
    output_path: Optional[str] = None,
    format: str = "mp4",
    fps: int = 30,
    steps_per_move: int = 14,
    hold_frames: int = 5,
):
    """Animate a compilation result.

    Parameters
    ----------
    compilation_result:
        A :class:`~natam_compiler.compiler.CompileResult` or a
        :class:`~natam_compiler.operations.HardwareProgram`.
    hardware_config:
        The :class:`HardwareSpec` used to compile; defaults to
        :func:`~natam_compiler.hardware.default_hardware`.
    output_path:
        If given, the animation is written to this path.  If ``None`` the live
        :class:`~matplotlib.animation.FuncAnimation` object is returned so it
        can be displayed with playback controls (e.g. in a notebook).
    format:
        ``"mp4"`` (needs ``ffmpeg``) or ``"gif"`` (uses the Pillow writer).
    fps:
        Frames per second of the exported animation.

    Returns
    -------
    matplotlib.animation.FuncAnimation
        The animation object (also returned when exporting so callers can
        further inspect or replay it).
    """
    try:
        import matplotlib

        if output_path is not None:
            matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.animation import FuncAnimation, PillowWriter
    except ImportError as exc:  # pragma: no cover - exercised only w/o mpl
        raise RuntimeError(
            "animate_compilation requires matplotlib; install it with "
            "`pip install matplotlib`."
        ) from exc

    program = getattr(compilation_result, "program", compilation_result)
    if not isinstance(program, HardwareProgram):
        raise TypeError(
            "compilation_result must be a CompileResult or HardwareProgram"
        )
    hw = hardware_config or default_hardware()

    frames = build_frames(program, hw, steps_per_move, hold_frames)
    if not frames:
        raise ValueError("program has no stages to animate")

    x0, x1, y0, y1 = _zone_bounds(hw)

    fig, ax = plt.subplots(figsize=(10, 6))
    ax.set_xlim(x0, x1)
    ax.set_ylim(y1, y0)  # invert y so the entanglement zone sits at the top
    ax.set_aspect("equal")
    ax.set_xlabel("column")
    ax.set_ylabel("row (storage below, entanglement above)")

    # Draw static zone backdrops.
    for zone, colour in ((hw.storage, "#eef2f7"), (hw.entanglement, "#fff2e0")):
        xs = [hw.coordinate((zone.name, r, c))[0]
              for r in range(zone.rows) for c in range(zone.cols)]
        ys = [hw.coordinate((zone.name, r, c))[1]
              for r in range(zone.rows) for c in range(zone.cols)]
        ax.scatter(xs, ys, s=6, marker="s", color=colour, zorder=0)

    scatter = ax.scatter([], [], s=90, zorder=3, edgecolors="black", linewidths=0.6)
    trail_lines: List = []
    conflict_lines: List = []
    title = ax.set_title("")

    # Legend proxies.
    from matplotlib.lines import Line2D

    legend_handles = [
        Line2D([0], [0], marker="o", color="w", markerfacecolor=_C_STATIONARY,
               markersize=9, label="stationary"),
        Line2D([0], [0], marker="o", color="w", markerfacecolor=_C_MOVING,
               markersize=9, label="moving"),
        Line2D([0], [0], marker="o", color="w", markerfacecolor=_C_GATE,
               markersize=9, label="active gate"),
        Line2D([0], [0], marker="o", color="w", markerfacecolor=_C_INVALID,
               markersize=9, label="constraint violation"),
    ]
    ax.legend(handles=legend_handles, loc="upper right", fontsize=8, framealpha=0.9)

    def _colour(frame: _Frame, atom: int) -> str:
        if atom in frame.invalid:
            return _C_INVALID
        if atom in frame.gate:
            return _C_GATE
        if atom in frame.moving:
            return _C_MOVING
        return _C_STATIONARY

    def _draw(idx: int):
        nonlocal trail_lines, conflict_lines
        frame = frames[idx]
        atoms = sorted(frame.xy)
        offsets = [frame.xy[a] for a in atoms]
        colours = [_colour(frame, a) for a in atoms]
        scatter.set_offsets(offsets if offsets else [(0, 0)])
        scatter.set_color(colours)

        for ln in trail_lines:
            ln.remove()
        trail_lines = []
        for atom, (src, dst) in frame.trails.items():
            colour = _C_INVALID if atom in frame.invalid else _C_MOVING
            (ln,) = ax.plot(
                [src[0], dst[0]], [src[1], dst[1]],
                color=colour, lw=1.0, ls="--", alpha=0.5, zorder=2,
            )
            trail_lines.append(ln)

        for ln in conflict_lines:
            ln.remove()
        conflict_lines = []
        for src, dst in frame.conflict_lines:
            (ln,) = ax.plot(
                [src[0], dst[0]], [src[1], dst[1]],
                color=_C_INVALID, lw=2.0, ls=":", zorder=4,
            )
            conflict_lines.append(ln)

        batch = f"batch {frame.batch_index}" if frame.batch_index else "-"
        title.set_text(
            f"timestep {frame.stage_index}  |  {frame.label}  |  {batch}  |  "
            f"op duration {frame.duration:.2f} us"
        )
        return [scatter, title, *trail_lines, *conflict_lines]

    anim = FuncAnimation(
        fig, _draw, frames=len(frames), interval=1000 / max(fps, 1), blit=False
    )

    if output_path is not None:
        fmt = format.lower()
        if fmt == "gif":
            anim.save(output_path, writer=PillowWriter(fps=fps))
        elif fmt == "mp4":
            try:
                anim.save(output_path, writer="ffmpeg", fps=fps)
            except (RuntimeError, ValueError) as exc:  # ffmpeg missing
                raise RuntimeError(
                    "MP4 export needs ffmpeg on PATH; pass format='gif' to use "
                    "the built-in Pillow writer instead."
                ) from exc
        else:
            raise ValueError(f"unsupported format {format!r}; use 'mp4' or 'gif'")
        plt.close(fig)

    return anim
