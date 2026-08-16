"""Tests for AOD movement constraints, batch planning, and the animator."""

import math

import pytest

from natam_compiler import (
    compile_circuit,
    default_hardware,
    diagnose_batch,
    moves_compatible,
    plan_batches,
    validate_move,
)
from natam_compiler.benchmarks import ghz, linear_entangler
from natam_compiler.hardware import ENTANGLEMENT, STORAGE, HardwareSpec
from natam_compiler.movement import (
    aod_ghost_traps,
    aod_tone_layout,
    batch_duration,
    batch_feasible,
    segment_min_separation,
    move_hits_stationary,
)
from natam_compiler.operations import MoveOp


# --------------------------------------------------------------------------- #
#  Kinematics / smooth trajectories
# --------------------------------------------------------------------------- #
def test_move_duration_monotonic_and_smooth():
    hw = default_hardware()
    d1 = hw.move_duration(1.0)
    d5 = hw.move_duration(5.0)
    d20 = hw.move_duration(20.0)
    assert 0 < d1 < d5 < d20            # longer moves take longer
    assert hw.move_duration(0.0) == 0.0  # a zero move is free


def test_move_duration_respects_velocity_limit():
    # For a long move the trapezoidal profile is limited by top velocity.
    hw = HardwareSpec(aod_max_velocity=2.0, aod_max_acceleration=1.0)
    long_distance = 100.0
    dur = hw.move_duration(long_distance)
    avg_velocity = long_distance / dur
    assert avg_velocity <= hw.aod_max_velocity + 1e-9


# --------------------------------------------------------------------------- #
#  Per-move validation (range / distance / duration)
# --------------------------------------------------------------------------- #
def test_validate_move_flags_out_of_range():
    hw = default_hardware()
    m = MoveOp(atom=0, src=(STORAGE, 0, 0), dst=(STORAGE, 999, 999))
    reasons = {v.reason for v in validate_move(hw, m)}
    assert "out_of_range" in reasons


def test_validate_move_flags_max_distance():
    hw = HardwareSpec(aod_max_distance=2.0)
    m = MoveOp(atom=0, src=(STORAGE, 0, 0), dst=(STORAGE, 0, 10))
    reasons = {v.reason for v in validate_move(hw, m)}
    assert "max_distance" in reasons


def test_validate_move_flags_max_duration():
    hw = HardwareSpec(aod_max_move_duration=0.5)
    m = MoveOp(atom=0, src=(STORAGE, 0, 0), dst=(STORAGE, 0, 10))
    reasons = {v.reason for v in validate_move(hw, m)}
    assert "max_duration" in reasons


def test_default_moves_are_valid():
    hw = default_hardware()
    m = MoveOp(atom=0, src=(STORAGE, 0, 0), dst=(ENTANGLEMENT, 0, 0))
    assert validate_move(hw, m) == []


# --------------------------------------------------------------------------- #
#  Pairwise compatibility (separation + crossing)
# --------------------------------------------------------------------------- #
def test_parallel_translation_is_compatible():
    hw = default_hardware()
    m1 = MoveOp(0, (STORAGE, 0, 0), (STORAGE, 5, 0))
    m2 = MoveOp(1, (STORAGE, 0, 5), (STORAGE, 5, 5))  # same shift, offset column
    ok, reason = moves_compatible(hw, m1, m2)
    assert ok, reason


def test_min_separation_violation_detected():
    hw = default_hardware()
    # Two atoms swap columns -> they pass through each other.
    m1 = MoveOp(0, (STORAGE, 0, 0), (STORAGE, 0, 2))
    m2 = MoveOp(1, (STORAGE, 0, 2), (STORAGE, 0, 0))
    ok, reason = moves_compatible(hw, m1, m2)
    assert not ok
    assert reason in {"min_separation", "trap_crossing"}


def test_shared_column_must_translate_together():
    hw = default_hardware()
    # Two atoms share source column 5 (one AOD column tone) but are asked to end
    # in different columns -> physically impossible for a single AOD.
    m1 = MoveOp(0, (STORAGE, 0, 5), (STORAGE, 10, 5))
    m2 = MoveOp(1, (STORAGE, 1, 5), (STORAGE, 10, 7))
    ok, reason = moves_compatible(hw, m1, m2)
    assert not ok
    assert reason in {"aod_shared_line", "min_separation"}


def test_shared_row_must_translate_together():
    hw = default_hardware()
    # Two atoms share source row (same y) but diverge in y -> infeasible tone.
    m1 = MoveOp(0, (STORAGE, 3, 2), (STORAGE, 3, 9))
    m2 = MoveOp(1, (STORAGE, 3, 4), (STORAGE, 6, 11))
    ok, reason = moves_compatible(hw, m1, m2)
    assert not ok and reason == "aod_shared_line"


def test_shared_line_rigid_translation_is_compatible():
    hw = default_hardware()
    # Same source column, rigid translation along y -> a valid single AOD move.
    m1 = MoveOp(0, (STORAGE, 0, 5), (STORAGE, 10, 5))
    m2 = MoveOp(1, (STORAGE, 1, 5), (STORAGE, 11, 5))
    ok, reason = moves_compatible(hw, m1, m2)
    assert ok, reason


def test_tone_layout_detects_inconsistent_motion():
    hw = default_hardware()
    # two atoms on source column 5 asked to reach different columns
    moves = [
        MoveOp(0, (STORAGE, 0, 5), (STORAGE, 8, 5)),
        MoveOp(1, (STORAGE, 1, 5), (STORAGE, 8, 7)),
    ]
    _x, _y, consistent = aod_tone_layout(hw, moves)
    assert not consistent
    ok, reason, _ = batch_feasible(hw, moves)
    assert not ok and reason == "aod_shared_line"


def test_diagonal_move_creates_ghost_traps():
    hw = default_hardware()
    # distinct X tones {0,1} and Y tones {row0,row1} -> two ghost intersections
    moves = [
        MoveOp(0, (STORAGE, 0, 0), (STORAGE, 5, 0)),
        MoveOp(1, (STORAGE, 1, 1), (STORAGE, 6, 1)),
    ]
    ghosts = aod_ghost_traps(hw, moves)
    assert len(ghosts) == 2


def test_ghost_trap_over_static_atom_is_infeasible():
    hw = default_hardware()
    # distinct X tones {0,1} and distinct, non-colliding Y tones (rows 0 and 2)
    moves = [
        MoveOp(0, (STORAGE, 0, 0), (STORAGE, 5, 0)),
        MoveOp(1, (STORAGE, 2, 1), (STORAGE, 7, 1)),
    ]
    # a static atom sits on the ghost intersection column 0 / row 2
    stationary = {(STORAGE, 2, 0)}
    ok, reason, _ = batch_feasible(hw, moves, stationary)
    assert not ok and reason == "ghost_trap"
    # with the obstacle gone the same batch is realizable
    ok2, _, _ = batch_feasible(hw, moves, set())
    assert ok2


def test_full_grid_translation_has_no_ghost_traps():
    hw = default_hardware()
    moves = [
        MoveOp(0, (STORAGE, 0, 0), (STORAGE, 10, 0)),
        MoveOp(1, (STORAGE, 1, 0), (STORAGE, 11, 0)),
        MoveOp(2, (STORAGE, 0, 1), (STORAGE, 10, 1)),
        MoveOp(3, (STORAGE, 1, 1), (STORAGE, 11, 1)),
    ]
    assert aod_ghost_traps(hw, moves) == []
    ok, _, _ = batch_feasible(hw, moves)
    assert ok


def test_trap_crossing_blocked_but_allowed_when_enabled():
    crossing1 = MoveOp(0, (STORAGE, 0, 0), (STORAGE, 0, 4))
    crossing2 = MoveOp(1, (STORAGE, 2, 4), (STORAGE, 2, 0))  # order flips on x
    hw_no_cross = HardwareSpec(allow_trap_crossing=False)
    ok, reason = moves_compatible(hw_no_cross, crossing1, crossing2)
    assert not ok and reason == "trap_crossing"

    hw_cross = HardwareSpec(allow_trap_crossing=True, min_atom_separation=0.1)
    ok2, _ = moves_compatible(hw_cross, crossing1, crossing2)
    assert ok2


def test_segment_min_separation_zero_when_paths_cross():
    a1, b1 = (0.0, 0.0), (2.0, 0.0)
    a2, b2 = (2.0, 0.0), (0.0, 0.0)
    assert segment_min_separation(a1, b1, a2, b2) == pytest.approx(0.0, abs=1e-6)


# --------------------------------------------------------------------------- #
#  Stationary-obstacle detection
# --------------------------------------------------------------------------- #
def test_move_through_stationary_atom_detected():
    hw = default_hardware()
    m = MoveOp(0, (STORAGE, 0, 0), (STORAGE, 0, 4))
    obstacle = {(STORAGE, 0, 2)}  # sits directly on the path
    assert move_hits_stationary(hw, m, obstacle)


def test_move_clear_of_stationary_atom():
    hw = default_hardware()
    m = MoveOp(0, (STORAGE, 0, 0), (STORAGE, 0, 4))
    far = {(STORAGE, 10, 10)}
    assert not move_hits_stationary(hw, m, far)


# --------------------------------------------------------------------------- #
#  Batch planning
# --------------------------------------------------------------------------- #
def test_plan_batches_respects_max_atoms():
    hw = HardwareSpec(aod_max_atoms=3)
    moves = [
        MoveOp(i, (STORAGE, 0, i), (STORAGE, 5, i)) for i in range(7)
    ]  # all compatible parallel translations
    batches = plan_batches(hw, moves)
    assert all(len(b) <= 3 for b in batches)
    assert sum(len(b) for b in batches) == 7
    assert len(batches) == math.ceil(7 / 3)


def test_incompatible_moves_split_into_separate_batches():
    hw = default_hardware()
    m1 = MoveOp(0, (STORAGE, 0, 0), (STORAGE, 0, 2))
    m2 = MoveOp(1, (STORAGE, 0, 2), (STORAGE, 0, 0))  # crosses m1
    batches = plan_batches(hw, [m1, m2])
    assert len(batches) == 2


def test_compatible_moves_share_one_batch():
    hw = default_hardware()
    moves = [MoveOp(i, (STORAGE, 0, i), (ENTANGLEMENT, 0, i)) for i in range(4)]
    batches = plan_batches(hw, moves)
    assert len(batches) == 1
    assert len(batches[0]) == 4


def test_batch_duration_is_longest_move():
    hw = default_hardware()
    short = MoveOp(0, (STORAGE, 0, 0), (STORAGE, 1, 0))
    long = MoveOp(1, (STORAGE, 0, 5), (STORAGE, 10, 5))
    expected = hw.move_duration(hw.distance(long.src, long.dst))
    assert batch_duration(hw, [short, long]) == pytest.approx(expected)


def test_diagnose_batch_reports_all_conflicts():
    hw = default_hardware()
    m1 = MoveOp(0, (STORAGE, 0, 0), (STORAGE, 0, 2))
    m2 = MoveOp(1, (STORAGE, 0, 2), (STORAGE, 0, 0))
    diag = diagnose_batch(hw, [m1, m2])
    assert not diag.ok
    assert diag.flagged_atoms() == {0, 1}


# --------------------------------------------------------------------------- #
#  Compiler integration
# --------------------------------------------------------------------------- #
def test_compiled_move_stages_are_valid_batches():
    hw = default_hardware()
    result = compile_circuit(linear_entangler(8, layers=2), hw)
    from natam_compiler.operations import MoveOp as _MoveOp

    for stage in result.program.stages:
        if stage.kind != "move" or not stage.ops:
            continue
        moves = [op for op in stage.ops if isinstance(op, _MoveOp)]
        assert len(moves) <= hw.aod_max_atoms
        diag = diagnose_batch(hw, moves)
        # No separation or crossing conflicts inside a compiler-emitted batch.
        assert not diag.separation_conflicts
        assert not diag.crossing_conflicts


def test_metrics_expose_movement_parallelism():
    result = compile_circuit(linear_entangler(6, layers=2))
    m = result.metrics
    assert m.num_move_batches > 0
    assert m.move_parallelism >= 1.0


def test_small_max_atoms_increases_batch_count():
    circuit = linear_entangler(6, layers=2)
    wide = compile_circuit(circuit, HardwareSpec(aod_max_atoms=40)).metrics
    narrow = compile_circuit(circuit, HardwareSpec(aod_max_atoms=1)).metrics
    assert narrow.num_move_batches > wide.num_move_batches


# --------------------------------------------------------------------------- #
#  Animation
# --------------------------------------------------------------------------- #
def test_build_frames_shape_and_categories():
    from natam_compiler.animation import build_frames

    hw = default_hardware()
    result = compile_circuit(ghz(3), hw)
    frames = build_frames(result.program, hw, steps_per_move=4, hold_frames=2)
    assert frames, "expected some frames"
    # first frame is the init layout with every atom stationary
    assert frames[0].kind == "init"
    assert not frames[0].moving
    # at least one frame shows parallel movement
    assert any(len(f.moving) >= 1 for f in frames)
    # at least one frame shows an active gate
    assert any(f.gate for f in frames)


def test_animate_compilation_exports_gif(tmp_path):
    pytest.importorskip("matplotlib")
    from natam_compiler import animate_compilation

    result = compile_circuit(ghz(3))
    out = tmp_path / "compilation.gif"
    anim = animate_compilation(
        result,
        default_hardware(),
        output_path=str(out),
        format="gif",
        fps=10,
        steps_per_move=3,
        hold_frames=1,
    )
    assert anim is not None
    assert out.exists() and out.stat().st_size > 0


def test_animate_compilation_returns_object_without_output():
    pytest.importorskip("matplotlib")
    import matplotlib

    matplotlib.use("Agg")
    from natam_compiler import animate_compilation

    result = compile_circuit(ghz(3))
    anim = animate_compilation(result, output_path=None, steps_per_move=2, hold_frames=1)
    assert anim is not None


def test_animate_rejects_bad_format(tmp_path):
    pytest.importorskip("matplotlib")
    from natam_compiler import animate_compilation

    result = compile_circuit(ghz(3))
    with pytest.raises(ValueError):
        animate_compilation(
            result, output_path=str(tmp_path / "x.avi"), format="avi"
        )
