"""Tests for the forward simulator engine and validation."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from simulator import (  # noqa: E402
    ENTANGLE_ARRAY,
    STORAGE_ARRAY,
    SimulationEngine,
    Site,
    load_instructions,
    sample_program,
    storage_index_to_site,
)


# --------------------------------------------------------------- layout tests
def test_storage_indexing_column_major():
    # atom 0 at the top of the first column
    assert storage_index_to_site(0) == Site(STORAGE_ARRAY, 0, 0)
    assert storage_index_to_site(39) == Site(STORAGE_ARRAY, 39, 0)
    # next column over
    assert storage_index_to_site(40) == Site(STORAGE_ARRAY, 0, 1)


# --------------------------------------------------------------- valid demo
def test_demo_program_is_valid():
    engine = SimulationEngine(sample_program()["instructions"])
    assert engine.num_steps > 0
    assert engine.is_valid(), engine.all_errors()
    # atoms preserved throughout
    assert len(engine.positions_at(0)) == 4
    assert set(engine.positions_at(engine.num_steps)) == {0, 1, 2, 3}


def test_demo_atoms_return_to_storage():
    engine = SimulationEngine(sample_program()["instructions"])
    final = engine.positions_at(engine.num_steps)
    for a in range(4):
        assert final[a].array == STORAGE_ARRAY


# --------------------------------------------------------- invalid detection
def test_invalid_demo_flags_destination_conflict():
    engine = SimulationEngine(sample_program(invalid=True)["instructions"])
    assert not engine.is_valid()
    errs = " ".join(engine.all_errors()).lower()
    assert "destination conflict" in errs


def test_atom_overlap_detected():
    # atom 1 sits still on a storage site; atom 0 tries to move onto it
    s0 = storage_index_to_site(0)
    s1 = storage_index_to_site(1)
    instrs = [
        {"type": "init", "init_locs": [s0.as_loc(0), s1.as_loc(1)]},
        {"type": "rearrangeJob", "aod_qubits": [0],
         "begin_locs": [s0.as_loc(0)], "end_locs": [s1.as_loc(0)]},
    ]
    engine = SimulationEngine(instrs)
    assert not engine.is_valid()
    assert any("overlap" in e for e in engine.all_errors())


def test_path_collision_detected():
    # two atoms swap positions simultaneously -> they cross / collide
    a = Site(STORAGE_ARRAY, 0, 10)
    b = Site(STORAGE_ARRAY, 0, 12)
    instrs = [
        {"type": "init", "init_locs": [a.as_loc(0), b.as_loc(1)]},
        {"type": "rearrangeJob", "aod_qubits": [0, 1],
         "begin_locs": [a.as_loc(0), b.as_loc(1)],
         "end_locs": [b.as_loc(0), a.as_loc(1)]},
    ]
    engine = SimulationEngine(instrs)
    assert not engine.is_valid()
    joined = " ".join(engine.all_errors()).lower()
    assert "path collision" in joined or "order not preserved" in joined


def test_shared_column_tone_split_detected():
    # two atoms on the same source column (one X tone) driven to different
    # columns -> a single AOD cannot do it.
    a = Site(STORAGE_ARRAY, 0, 3)
    b = Site(STORAGE_ARRAY, 1, 3)
    da = Site(ENTANGLE_ARRAY, 0, 3)
    db = Site(ENTANGLE_ARRAY, 1, 5)  # different destination column
    instrs = [
        {"type": "init", "init_locs": [a.as_loc(0), b.as_loc(1)]},
        {"type": "rearrangeJob", "aod_qubits": [0, 1],
         "begin_locs": [a.as_loc(0), b.as_loc(1)],
         "end_locs": [da.as_loc(0), db.as_loc(1)]},
    ]
    engine = SimulationEngine(instrs)
    assert not engine.is_valid()
    assert any("share one AOD column" in e for e in engine.all_errors())


def test_ghost_tweezer_over_static_atom_detected():
    # A diagonal move activates two X tones and two Y tones; the tone product
    # creates a ghost tweezer that sweeps over a stationary atom.
    a0 = Site(STORAGE_ARRAY, 0, 0)
    a1 = Site(STORAGE_ARRAY, 1, 1)
    d0 = Site(ENTANGLE_ARRAY, 0, 0)
    d1 = Site(ENTANGLE_ARRAY, 1, 1)
    victim = Site(STORAGE_ARRAY, 1, 0)  # sits on ghost intersection (col0, row1)
    instrs = [
        {"type": "init",
         "init_locs": [a0.as_loc(0), a1.as_loc(1), victim.as_loc(2)]},
        {"type": "rearrangeJob", "aod_qubits": [0, 1],
         "begin_locs": [a0.as_loc(0), a1.as_loc(1)],
         "end_locs": [d0.as_loc(0), d1.as_loc(1)]},
    ]
    engine = SimulationEngine(instrs)
    step = engine.steps[0]
    assert len(step.ghost_traps) == 2      # two non-target intersections
    assert step.ghost_bad                  # one sweeps over the victim
    assert not engine.is_valid()
    assert any("ghost AOD tweezer" in e for e in engine.all_errors())


def test_clean_grid_move_has_no_ghosts():
    # A full 2x2 grid translation activates 2 X tones x 2 Y tones = 4 traps,
    # which exactly cover the 4 atoms -> no ghost tweezers, no conflict.
    src = [Site(STORAGE_ARRAY, r, c) for c in (0, 1) for r in (0, 1)]
    dst = [Site(ENTANGLE_ARRAY, r, c) for c in (0, 1) for r in (0, 1)]
    instrs = [
        {"type": "init", "init_locs": [s.as_loc(i) for i, s in enumerate(src)]},
        {"type": "rearrangeJob", "aod_qubits": [0, 1, 2, 3],
         "begin_locs": [s.as_loc(i) for i, s in enumerate(src)],
         "end_locs": [d.as_loc(i) for i, d in enumerate(dst)]},
    ]
    engine = SimulationEngine(instrs)
    assert engine.is_valid(), engine.all_errors()
    assert engine.steps[0].ghost_traps == []


def test_unsupported_mixed_array_batch_detected():
    # one batch mixing a storage-internal move with an entanglement-internal move
    a = Site(STORAGE_ARRAY, 0, 0)
    a2 = Site(STORAGE_ARRAY, 1, 0)
    e = Site(ENTANGLE_ARRAY, 0, 0)
    e2 = Site(ENTANGLE_ARRAY, 0, 1)
    instrs = [
        {"type": "init", "init_locs": [a.as_loc(0), e.as_loc(1)]},
        {"type": "rearrangeJob", "aod_qubits": [0, 1],
         "begin_locs": [a.as_loc(0), e.as_loc(1)],
         "end_locs": [a2.as_loc(0), e2.as_loc(1)]},
    ]
    engine = SimulationEngine(instrs)
    assert not engine.is_valid()
    assert any("unsupported simultaneous move" in e for e in engine.all_errors())


def test_invalid_rydberg_pair_detected():
    # both atoms in storage -> not a valid entanglement pair
    s0 = storage_index_to_site(0)
    s1 = storage_index_to_site(1)
    instrs = [
        {"type": "init", "init_locs": [s0.as_loc(0), s1.as_loc(1)]},
        {"type": "rydberg", "zone_id": 0, "gates": [{"q0": 0, "q1": 1, "name": "cz"}]},
    ]
    engine = SimulationEngine(instrs)
    assert not engine.is_valid()
    assert any("rydberg pair" in e.lower() for e in engine.all_errors())


def test_parallel_1q_conflict_detected():
    s0 = storage_index_to_site(0)
    instrs = [
        {"type": "init", "init_locs": [s0.as_loc(0)]},
        {"type": "1qGate", "gates": [{"name": "sx", "q": 0}, {"name": "rz", "q": 0, "params": [0.5]}]},
    ]
    engine = SimulationEngine(instrs)
    assert not engine.is_valid()
    assert any("parallel conflict" in e for e in engine.all_errors())


# --------------------------------------------------------------- round-trip
def test_load_instructions_from_dict_and_list():
    prog = sample_program()
    from_dict = load_instructions(prog)
    from_list = load_instructions(prog["instructions"])
    assert from_dict == from_list == prog["instructions"]


def test_valid_parallel_move_preserves_identity():
    engine = SimulationEngine(sample_program()["instructions"])
    # find the first move step and check identities preserved
    move_steps = [s for s in engine.steps if s.kind == "MOVE"]
    assert move_steps
    s = move_steps[0]
    assert {m.atom for m in s.moves} == s.atoms
    for m in s.moves:
        assert s.positions_after[m.atom] == m.dst
