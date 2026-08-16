"""Turn engine steps into renderable scene frames.

Two entry points:

* :func:`step_scene` — build a single :class:`SceneSpec` for a given step at a
  chosen move ``progress`` (used by the live GUI for interactive playback).
* :func:`build_frame_specs` — expand the whole schedule into a flat list of
  per-video-frame specs (used by the MP4 exporter).
"""

from __future__ import annotations

from dataclasses import dataclass

from .engine import SimulationEngine, StepInfo
from .renderer import SceneSpec

_SUBTITLE = {
    "INIT": "Initial atom layout",
    "MOVE": "Parallel atom movement (hardware transport, not a gate)",
    "1Q": "Single-qubit gate layer (parallel)",
    "2Q": "Two-qubit Rydberg entangling layer (parallel)",
    "SWAP": "Explicit quantum SWAP",
    "MEASURE": "Measurement",
}


def _title(engine: SimulationEngine, step_index: int) -> str:
    total = engine.num_steps
    if step_index <= 0:
        return "Initial layout"
    s = engine.steps[step_index - 1]
    return f"Step {step_index}/{total}: {s.kind}  ·  {s.validation.status}"


def initial_scene(engine: SimulationEngine) -> SceneSpec:
    pos = engine.positions_at(0)
    return SceneSpec(
        positions=pos,
        title="Initial layout",
        subtitle=f"{len(pos)} atoms placed in the storage zone",
        measured=set(),
    )


def step_scene(engine: SimulationEngine, step_index: int, progress: float = 1.0,
               *, measured: set[int] | None = None) -> SceneSpec:
    """Scene for ``step_index`` (1-based). ``progress`` animates MOVE steps.

    ``step_index == 0`` renders the initial layout.
    """
    if step_index <= 0 or engine.num_steps == 0:
        sc = initial_scene(engine)
        sc.measured = set(measured or set())
        return sc

    step_index = min(step_index, engine.num_steps)
    s: StepInfo = engine.steps[step_index - 1]
    report = s.validation
    measured = set(measured if measured is not None else _measured_through(engine, step_index))

    if s.kind == "MOVE":
        moves = [(m.atom, m.src, m.dst, progress) for m in s.moves]
        positions = dict(s.positions_before)
        return SceneSpec(
            positions=positions,
            highlight=set(s.atoms),
            moves=moves,
            conflict_atoms=set(report.conflict_atoms),
            conflict_pairs=list(report.conflict_pairs),
            ghost_traps=list(s.ghost_traps),
            ghost_bad=list(s.ghost_bad),
            ghost_progress=progress,
            measured=measured,
            title=_title(engine, step_index),
            subtitle=_subtitle(s),
        )

    gate_atoms: set[int] = set()
    entangle_pairs = list(s.entangle_pairs)
    if s.kind == "1Q":
        gate_atoms = set(s.atoms)
    return SceneSpec(
        positions=dict(s.positions_after),
        highlight=set(s.atoms),
        entangle_pairs=entangle_pairs,
        gate_atoms=gate_atoms,
        conflict_atoms=set(report.conflict_atoms),
        conflict_pairs=list(report.conflict_pairs),
        measured=measured,
        title=_title(engine, step_index),
        subtitle=_subtitle(s),
    )


def _subtitle(s: StepInfo) -> str:
    base = _SUBTITLE.get(s.kind, s.kind)
    if s.parallel_size > 1:
        base += f"  ·  parallel group of {s.parallel_size}"
    if not s.validation.ok:
        base += "  ·  ⚠ " + s.validation.errors[0]
    return base


def _measured_through(engine: SimulationEngine, step_index: int) -> set[int]:
    out: set[int] = set()
    for s in engine.steps[:step_index]:
        out.update(s.measured)
    return out


# --------------------------------------------------------------------- export
@dataclass
class ExportSettings:
    fps: int = 30
    width: int = 1280
    height: int = 720
    hold_seconds: float = 0.6
    move_seconds: float = 0.8

    @property
    def hold_frames(self) -> int:
        return max(1, round(self.hold_seconds * self.fps))

    @property
    def move_frames(self) -> int:
        return max(2, round(self.move_seconds * self.fps))


def build_frame_specs(engine: SimulationEngine, settings: ExportSettings) -> list[SceneSpec]:
    """Expand the schedule into per-frame scene specs for video export."""
    frames: list[SceneSpec] = []
    frames.extend([initial_scene(engine)] * settings.hold_frames)

    measured: set[int] = set()
    for i, s in enumerate(engine.steps, start=1):
        if s.kind == "MOVE":
            n = settings.move_frames
            for f in range(n):
                progress = f / (n - 1) if n > 1 else 1.0
                frames.append(step_scene(engine, i, progress, measured=measured))
            frames.extend([step_scene(engine, i, 1.0, measured=measured)]
                          * max(1, settings.hold_frames // 2))
        else:
            measured = measured | set(s.measured)
            frames.extend([step_scene(engine, i, 1.0, measured=measured)]
                          * settings.hold_frames)

    if not frames:
        frames.append(initial_scene(engine))
    return frames
