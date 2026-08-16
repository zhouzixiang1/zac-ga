from pathlib import Path

from qiskit import QuantumCircuit

from fable_compiler import (
    ENTANGLEMENT,
    STORAGE,
    HardwareSpec,
    batch_feasible,
    compile_circuit,
    compile_circuit_search,
    default_hardware,
    diagnose_batch,
    plan_batches,
    to_zair,
)
from fable_compiler.ir import Gate1Q, Gate2Q, LogicalCircuit, Measure, from_qiskit
from fable_compiler.operations import INIT, HardwareProgram, InitOp, MoveOp, Stage, TwoQubitGate
from fable_compiler.scheduler import schedule
from fable_compiler.compiler import Compiler
from fable_compiler.compiler_search import SearchCompiler
from fable_compiler.placement import place
from fable_compiler.search_emit import (
    EmissionCandidate,
    EmissionScore,
    _select_ranked,
    find_conflicting_move_pairs,
    score_move_batches,
    score_move_graph,
    search_2q_emission,
    shifted_target_neighbors,
    swapped_target_neighbors,
    validate_2q_targets,
)


def test_storage_indexing_matches_prompt():
    hw = default_hardware()
    assert hw.storage_index_to_site(0) == (STORAGE, 0, 0)
    assert hw.storage_index_to_site(19) == (STORAGE, 0, 19)
    assert hw.storage_index_to_site(20) == (STORAGE, 1, 0)
    assert hw.coordinate((STORAGE, 0, 0))[0] > hw.coordinate((STORAGE, 1, 0))[0]
    assert hw.coordinate((ENTANGLEMENT, 0, 0))[0] > hw.coordinate((STORAGE, 0, 0))[0]


def test_shared_x_tone_requires_identical_x_motion():
    hw = default_hardware()
    moves = [
        MoveOp(0, (STORAGE, 0, 0), (ENTANGLEMENT, 0, 0)),
        MoveOp(1, (STORAGE, 0, 1), (ENTANGLEMENT, 1, 1)),
    ]
    ok, reason, _ = batch_feasible(hw, moves)
    assert not ok
    assert reason == "aod_shared_line"


def test_shared_y_tone_requires_identical_y_motion():
    hw = default_hardware()
    moves = [
        MoveOp(0, (STORAGE, 0, 3), (ENTANGLEMENT, 0, 3)),
        MoveOp(1, (STORAGE, 2, 3), (ENTANGLEMENT, 0, 4)),
    ]
    ok, reason, _ = batch_feasible(hw, moves)
    assert not ok
    assert reason == "aod_shared_line"


def test_ghost_trap_over_stationary_atom_splits_batch():
    hw = default_hardware()
    moves = [
        MoveOp(0, (STORAGE, 0, 0), (STORAGE, 1, 0)),
        MoveOp(1, (STORAGE, 2, 2), (STORAGE, 3, 2)),
    ]
    stationary = {(STORAGE, 1, 2)}
    ok, reason, _ = batch_feasible(hw, moves, stationary)
    assert not ok
    assert reason == "ghost_trap"
    batches = plan_batches(hw, moves, stationary)
    assert len(batches) == 2


def test_landing_on_pending_move_source_batches_together():
    hw = default_hardware()
    moves = [
        MoveOp(0, (STORAGE, 0, 13), (ENTANGLEMENT, 1, 12)),
        MoveOp(2, (ENTANGLEMENT, 1, 12), (ENTANGLEMENT, 1, 11)),
    ]
    ok, reason, _ = batch_feasible(hw, [moves[0]], {moves[1].src})
    assert not ok
    assert reason == "stationary_overlap"
    batches = plan_batches(hw, [moves[1], moves[0]])
    assert batches == [[moves[1]], [moves[0]]]


def test_multiple_rows_and_columns_move_in_one_aod_batch():
    hw = default_hardware()
    moves = []
    for row in range(6):
        moves.append(MoveOp(2 * row, (STORAGE, 1, row), (ENTANGLEMENT, 0, row)))
        moves.append(MoveOp(2 * row + 1, (STORAGE, 0, row), (ENTANGLEMENT, 1, row)))

    ok, reason, detail = batch_feasible(hw, moves)
    assert ok, (reason, detail)
    batches = plan_batches(hw, moves)
    assert batches == [moves]


def test_score_move_batches_uses_conflict_scores():
    hw = default_hardware()
    first = MoveOp(0, (STORAGE, 0, 0), (ENTANGLEMENT, 0, 0))
    second = MoveOp(1, (STORAGE, 1, 0), (ENTANGLEMENT, 1, 0))

    score = score_move_batches(hw, [[first], [second]])

    assert score.badness == 3 * 1
    assert score.max_stage_distance == max(hw.distance(first.src, first.dst), hw.distance(second.src, second.dst))


def test_score_move_batches_same_atom_chain_uses_only_conflicts():
    hw = default_hardware()
    first = MoveOp(0, (STORAGE, 0, 0), (ENTANGLEMENT, 0, 0))
    second = MoveOp(0, (ENTANGLEMENT, 0, 0), (STORAGE, 0, 0))

    score = score_move_batches(hw, [[first], [second]])

    assert score.badness == 3 * 1


def test_score_move_graph_incremental_update_matches_full_rebuild():
    hw = default_hardware()
    parent_moves = (
        MoveOp(0, (STORAGE, 0, 0), (ENTANGLEMENT, 0, 0)),
        MoveOp(1, (STORAGE, 1, 0), (ENTANGLEMENT, 1, 0)),
        MoveOp(2, (STORAGE, 0, 1), (ENTANGLEMENT, 0, 1)),
    )
    child_moves = (
        parent_moves[0],
        MoveOp(1, (STORAGE, 1, 0), (ENTANGLEMENT, 1, 1)),
        parent_moves[2],
    )

    _, parent_cache = score_move_graph(hw, parent_moves)
    incremental_score, incremental_cache = score_move_graph(hw, child_moves, parent_cache=parent_cache)
    full_score, full_cache = score_move_graph(hw, child_moves)

    assert incremental_score == full_score
    assert incremental_cache == full_cache


def test_score_move_batches_empty_input_scores_zero():
    score = score_move_batches(default_hardware(), [])

    assert score.badness == 0.0
    assert score.max_stage_distance == 0.0


def test_select_ranked_keeps_best_unique_candidates():
    gates = (Gate2Q("cz", 0, 1),)

    def candidate(atom0_col: int, badness: float) -> EmissionCandidate:
        targets = {
            0: (ENTANGLEMENT, atom0_col, 0),
            1: (ENTANGLEMENT, 1 - atom0_col, 0),
        }
        return EmissionCandidate(
            gates,
            targets,
            (),
            (),
            frozenset(),
            EmissionScore(badness, badness),
        )

    ranked = _select_ranked([candidate(0, 3.0), candidate(1, 1.0), candidate(0, 2.0)], 2)

    assert [item.score.badness for item in ranked] == [1.0, 2.0]


def test_conflicting_move_pairs_find_shared_tone_conflict():
    hw = default_hardware()
    moves = [
        MoveOp(0, (STORAGE, 0, 0), (ENTANGLEMENT, 0, 0)),
        MoveOp(1, (STORAGE, 0, 1), (ENTANGLEMENT, 1, 0)),
    ]

    conflicts = find_conflicting_move_pairs(hw, moves, set())

    assert len(conflicts) == 1
    assert conflicts[0].reason == "aod_shared_line"


def test_swapped_target_neighbors_preserve_2q_target_invariants():
    hw = default_hardware()
    gates = [Gate2Q("cz", 0, 1)]
    targets = {0: (ENTANGLEMENT, 0, 0), 1: (ENTANGLEMENT, 1, 0)}
    moves = [
        MoveOp(0, (STORAGE, 0, 0), targets[0]),
        MoveOp(1, (STORAGE, 0, 1), targets[1]),
    ]
    conflicts = find_conflicting_move_pairs(hw, moves, set())

    neighbors = swapped_target_neighbors(gates, targets, conflicts)

    assert len(neighbors) == 1
    assert neighbors[0] == {0: (ENTANGLEMENT, 1, 0), 1: (ENTANGLEMENT, 0, 0)}
    assert validate_2q_targets(gates, neighbors[0])


def test_shifted_target_neighbors_move_gate_to_free_adjacent_row():
    hw = default_hardware()
    gates = [Gate2Q("cz", 0, 1), Gate2Q("cz", 2, 3)]
    targets = {
        0: (ENTANGLEMENT, 0, 1),
        1: (ENTANGLEMENT, 1, 1),
        2: (ENTANGLEMENT, 0, 3),
        3: (ENTANGLEMENT, 1, 3),
    }

    neighbors = shifted_target_neighbors(hw, gates, targets)

    assert {0: (ENTANGLEMENT, 0, 0), 1: (ENTANGLEMENT, 1, 0), 2: (ENTANGLEMENT, 0, 3), 3: (ENTANGLEMENT, 1, 3)} in neighbors
    assert {0: (ENTANGLEMENT, 0, 2), 1: (ENTANGLEMENT, 1, 2), 2: (ENTANGLEMENT, 0, 3), 3: (ENTANGLEMENT, 1, 3)} in neighbors
    assert all(validate_2q_targets(gates, neighbor) for neighbor in neighbors)


def test_search_timeout_returns_best_result_so_far():
    gates = (Gate2Q("cz", 0, 1),)
    initial = EmissionCandidate(
        gates,
        {0: (ENTANGLEMENT, 0, 0), 1: (ENTANGLEMENT, 1, 0)},
        (),
        (),
        frozenset(),
        EmissionScore(10.0, 10.0),
    )
    better = EmissionCandidate(
        gates,
        {0: (ENTANGLEMENT, 0, 1), 1: (ENTANGLEMENT, 1, 1)},
        (),
        (),
        frozenset(),
        EmissionScore(1.0, 1.0),
    )

    result = search_2q_emission(
        default_hardware(),
        initial,
        lambda targets, parent_graph_cache=None: (_ for _ in ()).throw(AssertionError("timeout should skip neighbor eval")),
        population_size=2,
        iterations=10,
        initial_candidates=(better,),
        timeout=0.0,
    )

    assert result == better


    hw = default_hardware()
    compiler = SearchCompiler(hw, population_size=4, random_initial=True, random_seed=7)
    gates = [Gate2Q("cz", 0, 1), Gate2Q("cz", 2, 3), Gate2Q("cz", 4, 5)]
    current = {
        0: (STORAGE, 0, 0),
        1: (STORAGE, 0, 1),
        2: (STORAGE, 0, 2),
        3: (STORAGE, 0, 3),
        4: (STORAGE, 0, 4),
        5: (STORAGE, 0, 5),
    }
    homes = dict(current)
    ordered = compiler._ordered_2q_gates(gates, current)

    first = compiler._random_initial_candidates(homes, current, ordered)
    second = compiler._random_initial_candidates(homes, current, ordered)

    assert len(first) == 4
    assert [item.target_positions for item in first] == [item.target_positions for item in second]
    for candidate in first:
        assert validate_2q_targets(ordered, candidate.target_positions)
        for gate in ordered:
            q0_pos = candidate.target_positions[gate.q0]
            q1_pos = candidate.target_positions[gate.q1]
            assert q0_pos[1] == 0
            assert q1_pos[1] == 1


def test_search_compiler_random_initial_emits_feasible_moves_and_entanglement_cz():
    hw = default_hardware()
    qc = QuantumCircuit(4)
    qc.cz(0, 1)
    qc.cz(2, 3)

    result = compile_circuit_search(qc, hw, population_size=4, random_initial=True, random_seed=3)

    assert result.metrics.num_2q == 2
    for stage in result.program.stages:
        if stage.kind == "move":
            diag = diagnose_batch(hw, stage.ops)
            assert diag.ok, (diag.reason, diag.detail)
        for op in stage.ops:
            if isinstance(op, TwoQubitGate):
                assert op.pos0[0] == ENTANGLEMENT
                assert op.pos1[0] == ENTANGLEMENT
                assert op.pos0[2] == op.pos1[2]
                assert {op.pos0[1], op.pos1[1]} == {0, 1}


def test_search_compiler_emits_feasible_moves_and_entanglement_cz():
    hw = default_hardware()
    qc = QuantumCircuit(4)
    qc.cz(0, 1)
    qc.cz(2, 3)

    result = compile_circuit_search(qc, hw)

    assert result.metrics.num_2q == 2
    for stage in result.program.stages:
        if stage.kind == "move":
            diag = diagnose_batch(hw, stage.ops)
            assert diag.ok, (diag.reason, diag.detail)
        for op in stage.ops:
            if isinstance(op, TwoQubitGate):
                assert op.pos0[0] == ENTANGLEMENT
                assert op.pos1[0] == ENTANGLEMENT
                assert op.pos0[2] == op.pos1[2]
                assert {op.pos0[1], op.pos1[1]} == {0, 1}


def test_compiler_detects_entanglement_move_cycles():
    compiler = Compiler(default_hardware())
    current = {
        38: (ENTANGLEMENT, 1, 19),
        40: (ENTANGLEMENT, 1, 0),
    }
    targets = {
        38: (ENTANGLEMENT, 1, 0),
        40: (ENTANGLEMENT, 1, 19),
    }
    assert compiler._cycle_break_atoms(current, targets, {38, 40}) == {40}


def test_compile_emits_cz_only_in_entanglement_zone():
    qc = QuantumCircuit(4, 4)
    qc.h(0)
    qc.cz(0, 1)
    qc.cx(2, 3)
    qc.measure(range(4), range(4))

    result = compile_circuit(qc)
    assert result.metrics.num_2q == 2
    for stage in result.program.stages:
        for op in stage.ops:
            if isinstance(op, TwoQubitGate):
                assert op.pos0[0] == ENTANGLEMENT
                assert op.pos1[0] == ENTANGLEMENT
                assert op.pos0[2] == op.pos1[2]
                assert {op.pos0[1], op.pos1[1]} == {0, 1}


def test_compiler_move_batches_are_aod_feasible():
    qc = QuantumCircuit(6)
    for q in range(0, 6, 2):
        qc.cz(q, q + 1)
    result = compile_circuit(qc)
    for stage in result.program.stages:
        if stage.kind == "move":
            diag = diagnose_batch(default_hardware(), stage.ops)
            assert diag.ok, (diag.reason, diag.detail)


def test_zair_export_shape_and_locations():
    qc = QuantumCircuit(2, 2)
    qc.cz(0, 1)
    qc.measure([0, 1], [0, 1])
    schedule = to_zair(compile_circuit(qc).program)
    assert schedule["instructions"][0]["type"] == "init"
    assert schedule["instructions"][0]["init_locs"][:2] == [[0, 0, 0, 0], [1, 0, 0, 1]]
    assert any(inst["type"] == "rearrangeJob" for inst in schedule["instructions"])
    assert any(inst["type"] == "rydberg" for inst in schedule["instructions"])


def test_scheduler_preserves_qubit_dependencies_when_earlier_slot_is_free():
    ops = [
        Gate1Q("u3", 0, (0.0, 0.0, 0.0)),
        Gate1Q("u3", 0, (1.0, 0.0, 0.0)),
        Gate2Q("cz", 0, 1),
        Gate1Q("u3", 1, (2.0, 0.0, 0.0)),
    ]
    logical = LogicalCircuit(num_qubits=2, ops=ops)

    assert schedule(logical) == [[ops[0]], [ops[1]], [ops[2]], [ops[3]]]


def _slot_qubits(slot):
    qubits = set()
    for op in slot:
        if isinstance(op, Gate1Q):
            qubits.add(op.qubit)
        elif isinstance(op, Gate2Q):
            qubits.update({op.q0, op.q1})
        elif isinstance(op, Measure):
            qubits.add(op.qubit)
    return qubits


def _print_positions(label, hw, positions):
    print(label)
    for atom, pos in sorted(positions.items()):
        print(f"  q{atom:04d}: site={pos!r}, coordinate={hw.coordinate(pos)!r}")


def _target_positions_for_2q_chunk(compiler, chunk, current):
    target_positions = {}
    assigned_rows = compiler._assign_rows(chunk, current)
    for gate, row in zip(chunk, assigned_rows):
        pos0, pos1 = (ENTANGLEMENT, 0, row), (ENTANGLEMENT, 1, row)
        q0_pos, q1_pos = current[gate.q0], current[gate.q1]
        if q0_pos[0] == ENTANGLEMENT and q0_pos[2] == row:
            target_positions[gate.q0] = q0_pos
            target_positions[gate.q1] = pos1 if q0_pos[1] == 0 else pos0
        elif q1_pos[0] == ENTANGLEMENT and q1_pos[2] == row:
            target_positions[gate.q1] = q1_pos
            target_positions[gate.q0] = pos1 if q1_pos[1] == 0 else pos0
        elif compiler.hw.coordinate(q0_pos)[0] <= compiler.hw.coordinate(q1_pos)[0]:
            target_positions[gate.q0] = pos0
            target_positions[gate.q1] = pos1
        else:
            target_positions[gate.q0] = pos1
            target_positions[gate.q1] = pos0
    return assigned_rows, target_positions


def _print_emit_slot_flow(logical, hw, homes, asap_slots):
    compiler = Compiler(hw)
    program = HardwareProgram(logical.num_qubits, logical.num_clbits)
    program.add(
        Stage(
            INIT,
            [InitOp(atom, position) for atom, position in sorted(homes.items())],
            duration=0.0,
            fidelity=1.0,
        )
    )
    current = dict(homes)

    print("\n=== Compiler._emit_slot details ===")
    print("stage 0000: INIT")
    print(f"  ops={len(program.stages[0].ops)}, duration={program.stages[0].duration}, fidelity={program.stages[0].fidelity}")

    for slot_index, slot in enumerate(asap_slots[:]):
        future_2q = compiler._future_2q_qubits(asap_slots[slot_index + 1 :])
        oneq = [op for op in slot if isinstance(op, Gate1Q)]
        twoq = [op for op in slot if isinstance(op, Gate2Q)]
        meas = [op for op in slot if isinstance(op, Measure)]
        slot_qubits = _slot_qubits(slot)
        relevant_atoms = slot_qubits | future_2q

        print(f"\n--- logical slot {slot_index:04d} -> Compiler._emit_slot ---")
        # print(f"slot_ops={slot!r}")
        # print(f"oneq={oneq!r}")
        # print(f"twoq={twoq!r}")
        # print(f"meas={meas!r}")
        # print(f"future_2q_qubits={sorted(future_2q)!r}")
        # _print_positions(
        #     "current positions before slot for slot/future atoms:",
        #     hw,
        #     {atom: current[atom] for atom in relevant_atoms if atom in current},
        # )

        if twoq:
            ordered = sorted(twoq, key=lambda gate: compiler._pair_key(gate, current))
            capacity = hw.interaction_rows()
            # print(f"2q ordered by pair_key={ordered!r}")
            print(f"2q chunk capacity=interaction_rows={capacity}")
            for chunk_index, start in enumerate(range(0, len(ordered), capacity)):
                chunk = ordered[start : start + capacity]
                rows, targets = _target_positions_for_2q_chunk(compiler, chunk, current)
                participating = {q for gate in chunk for q in (gate.q0, gate.q1)}
                cycle_breakers = compiler._cycle_break_atoms(current, targets, participating)
                target_blockers = {
                    atom
                    for atom, pos in current.items()
                    if atom not in participating and pos in set(targets.values()) and pos != homes[atom]
                }
                preview_current = dict(current)
                preview_program = HardwareProgram(logical.num_qubits, logical.num_clbits)
                if cycle_breakers:
                    compiler._release_atoms(preview_program, homes, preview_current, cycle_breakers)
                compiler._evict_target_blockers(
                    preview_program,
                    homes,
                    preview_current,
                    participating,
                    set(targets.values()),
                )
                moves_in = [
                    MoveOp(atom, preview_current[atom], targets[atom])
                    for atom in sorted(participating)
                    if preview_current[atom] != targets[atom]
                ]
                ordered_moves_in = compiler._order_moves_to_clear_destinations(moves_in)
                stationary = {pos for atom, pos in preview_current.items() if atom not in participating}
                move_batches = plan_batches(hw, ordered_moves_in, stationary)

                print(f"  2q chunk {chunk_index:04d}: gates={chunk!r}")
                # print(f"    assigned_rows={rows!r}")
                # _print_positions("    target entanglement positions:", hw, targets)
                # print(f"    participating={sorted(participating)!r}")
                # print(f"    cycle_breakers={sorted(cycle_breakers)!r}")
                # print(f"    target_blockers_before_release_evict={sorted(target_blockers)!r}")
                # print("    release/evict stages before moves_in:")
                # for stage in preview_program.stages:
                #     print(
                #         f"      kind={stage.kind!r}, duration={stage.duration}, "
                #         f"fidelity={stage.fidelity}, ops={stage.ops!r}"
                #     )
                # _print_positions(
                #     "    positions after release/evict for participating/blocking atoms:",
                #     hw,
                #     {
                #         atom: preview_current[atom]
                #         for atom in sorted(set(participating) | set(cycle_breakers) | set(target_blockers))
                #     },
                # )
                # print(f"    moves_in_before_order={moves_in!r}")
                # print(f"    moves_in_after_order={ordered_moves_in!r}")
                # print(f"    planned_move_batches={move_batches!r}")
                # for move_batch in move_batches:
                #     print(f"    planned_move_batch={move_batch!r}")

        stages_before = len(program.stages)
        compiler._emit_slot(program, slot, homes, current, future_2q)
        new_stages = program.stages[stages_before:]

        print("hardware stages emitted by this slot:")
        for stage_offset, stage in enumerate(new_stages, start=stages_before):
            # if stage.kind == "move":
            #     print(
            #         f"  stage {stage_offset:04d}: kind={stage.kind!r}, "
            #         f"duration={stage.duration}, fidelity={stage.fidelity}, ops={stage.ops!r}"
            #     )
            print(
                f"  stage {stage_offset:04d}: kind={stage.kind!r}, "
                f"duration={stage.duration}, fidelity={stage.fidelity}, ops={stage.ops!r}"
            )
        # _print_positions(
        #     "current positions after slot for slot/future atoms:",
        #     hw,
        #     {atom: current[atom] for atom in relevant_atoms if atom in current},
        # )

    return program


def test_print_bv_n19_compile_flow():
    qasm_path = (
        Path(__file__).resolve().parents[2]
        / "ZAC-main"
        / "benchmark"
        / "hpca"
        / "multiply_n13_transpiled.qasm"
    )
    qasm_source = qasm_path.read_text(encoding="utf-8")

    initial_circuit = QuantumCircuit.from_qasm_str(qasm_source)
    logical = from_qiskit(initial_circuit)
    asap_slots = schedule(logical)
    hw = default_hardware()
    homes = place(logical, hw)
    result = compile_circuit(logical, hw)

    print("\n=== Initial QuantumCircuit: ZAC-main/benchmark/hpca/multiply_n13_transpiled.qasm ===")
    print("initial_circuit", initial_circuit)
    # print(f"num_qubits={initial_circuit.num_qubits}, num_clbits={initial_circuit.num_clbits}")
    # print(f"count_ops={dict(initial_circuit.count_ops())}")

    print("\n=== LogicalCircuit ===")
    # print(f"num_qubits={logical.num_qubits}, num_clbits={logical.num_clbits}, num_ops={len(logical.ops)}")

    print("\n=== Logicalops ===")
    # for index, op in enumerate(logical.ops):
    #     print(f"{index:04d}: {op!r}")

    print("\n=== ASAP slots ===")
    for slot_index, slot in enumerate(asap_slots[:]):
        print(f"slot {slot_index:04d}:")
        for op in slot:
            print(f"  {op!r}")

    # print("\n=== HardwareSpec ===")
    # print(hw.as_dict())

    # print("\n=== Placement homes ===")
    # for qubit, position in sorted(homes.items()):
    #     print(f"q{qubit:04d}: site={position!r}, coordinate={hw.coordinate(position)!r}")

    debug_program = _print_emit_slot_flow(logical, hw, homes, asap_slots)

    assert logical.ops
    assert asap_slots
    assert homes == result.homes
    # assert result.logical == logical
    # assert debug_program.stages == result.program.stages
