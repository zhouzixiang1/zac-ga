from pathlib import Path

from qiskit import QuantumCircuit

from fable_compiler import ENTANGLEMENT, STORAGE, default_hardware, diagnose_batch
from fable_compiler.compiler import compile_circuit as compile_baseline
from fable_compiler.compiler_zwq import compile_circuit as compile_zwq, Compiler
from fable_compiler.ir import Gate1Q, Gate2Q, LogicalCircuit, Measure, from_qiskit
from fable_compiler.movement_zwq import MoveCall, plan_group_batches
from fable_compiler.operations import INIT, HardwareProgram, InitOp, MoveOp, Stage, TwoQubitGate
from fable_compiler.scheduler import schedule
from fable_compiler.placement import place


def test_group_batches_independent_calls_together():
    hw = default_hardware()
    calls = [
        MoveCall(0, (MoveOp(0, (STORAGE, 1, 0), (ENTANGLEMENT, 0, 0)),), frozenset()),
        MoveCall(1, (MoveOp(1, (STORAGE, 1, 1), (ENTANGLEMENT, 0, 1)),), frozenset()),
    ]
    initial = {0: calls[0].moves[0].src, 1: calls[1].moves[0].src}

    plan = plan_group_batches(hw, calls, initial)

    assert not plan.used_fallback, plan.fallback_reason
    assert plan.batches == [[calls[0].moves[0], calls[1].moves[0]]]


def test_group_batches_preserve_same_atom_precedence():
    hw = default_hardware()
    first = MoveOp(0, (STORAGE, 0, 0), (ENTANGLEMENT, 0, 0))
    second = MoveOp(0, (ENTANGLEMENT, 0, 0), (STORAGE, 0, 0))
    calls = [MoveCall(0, (first,), frozenset()), MoveCall(1, (second,), frozenset())]

    plan = plan_group_batches(hw, calls, {0: first.src})

    assert not plan.used_fallback, plan.fallback_reason
    assert plan.batches == [[first], [second]]


def test_group_batches_keep_shared_tone_conflicts_separate():
    hw = default_hardware()
    first = MoveOp(0, (STORAGE, 0, 0), (ENTANGLEMENT, 0, 0))
    second = MoveOp(1, (STORAGE, 0, 1), (ENTANGLEMENT, 1, 1))
    calls = [MoveCall(0, (first,), frozenset()), MoveCall(1, (second,), frozenset())]

    plan = plan_group_batches(hw, calls, {0: first.src, 1: second.src})

    assert all(len(batch) == 1 for batch in plan.batches)


def test_group_batches_allow_non_crossing_x_tones():
    hw = default_hardware()
    first = MoveOp(atom=40, src=(STORAGE, 2, 0), dst=(ENTANGLEMENT, 1, 0))
    second = MoveOp(atom=41, src=(STORAGE, 3, 0), dst=(ENTANGLEMENT, 0, 0))
    calls = [MoveCall(0, (first,), frozenset()), MoveCall(1, (second,), frozenset())]

    plan = plan_group_batches(hw, calls, {40: first.src, 41: second.src})

    assert not plan.used_fallback, plan.fallback_reason
    assert plan.batches == [[first, second]]


def test_zwq_release_moves_do_not_treat_own_destinations_as_stationary():
    hw = default_hardware()
    compiler = Compiler(hw)
    program = HardwareProgram(100)
    homes = {}
    current = {}
    for index, row in enumerate(range(1, 9)):
        atom = 42 + 2 * index
        homes[atom] = (STORAGE, 2, row)
        current[atom] = (ENTANGLEMENT, 1, row)
    homes[81] = (STORAGE, 5, 0)
    current[81] = (ENTANGLEMENT, 0, 0)

    gate_pairs = [(82, 83), (84, 85), (86, 87), (88, 89), (90, 91), (92, 93), (94, 95), (96, 97), (79, 80)]
    for row, (left, right) in enumerate(gate_pairs):
        homes[left] = (STORAGE, 0, row)
        current[left] = (STORAGE, 0, row)
        homes[right] = (STORAGE, 1, row)
        current[right] = (STORAGE, 1, row)

    compiler._emit_2q(program, [Gate2Q("cz", left, right) for left, right in gate_pairs], homes, current)

    first_move_stage = next(stage for stage in program.stages if stage.kind == "move")
    assert len(first_move_stage.ops) == 9
    assert any(move.atom == 42 and move.dst == (STORAGE, 2, 1) for move in first_move_stage.ops)


def test_zwq_twoq_release_layer_can_batch_entire_row_pair_set():
    hw = default_hardware()
    compiler = Compiler(hw)
    program = HardwareProgram(100)
    homes = {}
    current = {}
    for index, row in enumerate(range(1, 20)):
        left = 42 + 2 * index
        right = left + 1
        homes[left] = (STORAGE, 2, row)
        current[left] = (STORAGE, 2, row)
        homes[right] = (STORAGE, 3, row)
        current[right] = (STORAGE, 3, row)

    gate_pairs = [(42 + 2 * index, 43 + 2 * index) for index in range(19)]
    compiler._emit_2q(program, [Gate2Q("cz", left, right) for left, right in gate_pairs], homes, current)

    move_stages = [stage for stage in program.stages if stage.kind == "move"]
    assert len(move_stages) == 1
    assert len(move_stages[0].ops) == 38


def test_zwq_compiler_move_batches_are_aod_feasible():
    qc = QuantumCircuit(6)
    for q in range(0, 6, 2):
        qc.cz(q, q + 1)

    result = compile_zwq(qc)

    for stage in result.program.stages:
        if stage.kind == "move":
            diag = diagnose_batch(default_hardware(), stage.ops)
            assert diag.ok, (diag.reason, diag.detail)


def test_zwq_compiler_emits_cz_only_in_entanglement_zone():
    qc = QuantumCircuit(4, 4)
    qc.h(0)
    qc.cz(0, 1)
    qc.cx(2, 3)
    qc.measure(range(4), range(4))

    result = compile_zwq(qc)

    assert result.metrics.num_2q == 2
    for stage in result.program.stages:
        for op in stage.ops:
            if isinstance(op, TwoQubitGate):
                assert op.pos0[0] == ENTANGLEMENT
                assert op.pos1[0] == ENTANGLEMENT
                assert op.pos0[2] == op.pos1[2]
                assert {op.pos0[1], op.pos1[1]} == {0, 1}


def test_zwq_compiler_does_not_increase_move_batches_on_simple_layer():
    qc = QuantumCircuit(6)
    for q in range(0, 6, 2):
        qc.cz(q, q + 1)

    baseline = compile_baseline(qc)
    optimized = compile_zwq(qc)

    assert optimized.metrics.num_move_batches <= baseline.metrics.num_move_batches
    assert optimized.metrics.num_2q == baseline.metrics.num_2q


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


def _print_emit_slot_flow_zwq(logical, hw, homes, asap_slots):
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

    print("\n=== CompilerZWQ._emit_slot details ===")
    print("stage 0000: INIT")
    print(f"  ops={len(program.stages[0].ops)}, duration={program.stages[0].duration}, fidelity={program.stages[0].fidelity}")

    for slot_index, slot in enumerate(asap_slots[:20]):
        future_2q = compiler._future_2q_qubits(asap_slots[slot_index + 1 :])
        oneq = [op for op in slot if isinstance(op, Gate1Q)]
        twoq = [op for op in slot if isinstance(op, Gate2Q)]
        meas = [op for op in slot if isinstance(op, Measure)]
        slot_qubits = _slot_qubits(slot)
        relevant_atoms = slot_qubits | future_2q

        print(f"\n--- logical slot {slot_index:04d} -> CompilerZWQ._emit_slot ---")
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
            print(f"2q row-capacity layers={capacity}")
            layer_current = dict(current)
            for layer_index, start in enumerate(range(0, len(ordered), capacity)):
                layer = ordered[start : start + capacity]
                rows, targets = _target_positions_for_2q_chunk(compiler, layer, layer_current)
                participating = {q for gate in layer for q in (gate.q0, gate.q1)}
                cycle_breakers = compiler._cycle_break_atoms(layer_current, targets, participating)
                target_blockers = {
                    atom
                    for atom, pos in layer_current.items()
                    if atom not in participating and pos in set(targets.values()) and pos != homes[atom]
                }
                preview_program = HardwareProgram(logical.num_qubits, logical.num_clbits)

                # print(f"  2q layer {layer_index:04d}: gates={layer!r}")
                # print(f"    assigned_rows={rows!r}")
                # _print_positions("    target entanglement positions:", hw, targets)
                # print(f"    participating={sorted(participating)!r}")
                # print(f"    cycle_breakers={sorted(cycle_breakers)!r}")
                # print(f"    target_blockers_before_release_evict={sorted(target_blockers)!r}")

                movement_initial = {}
                movement_calls = []
                with compiler._movement_group(preview_program, layer_current):
                    if cycle_breakers:
                        compiler._release_atoms(preview_program, homes, layer_current, cycle_breakers)
                    compiler._evict_target_blockers(preview_program, homes, layer_current, participating, set(targets.values()))
                    moves_in = [
                        MoveOp(atom, layer_current[atom], targets[atom])
                        for atom in sorted(participating)
                        if layer_current[atom] != targets[atom]
                    ]
                    ordered_moves_in = compiler._order_moves_to_clear_destinations(moves_in)
                    moving_atoms = set(cycle_breakers) | target_blockers | {move.atom for move in ordered_moves_in}
                    stationary = {
                        pos
                        for atom, pos in layer_current.items()
                        if atom not in participating and atom not in moving_atoms
                    }
                    compiler._emit_moves(preview_program, ordered_moves_in, stationary)
                    for move in ordered_moves_in:
                        layer_current[move.atom] = move.dst
                    movement_group = compiler._active_movement_group
                    if movement_group is not None:
                        movement_initial = dict(movement_group.initial_current)
                        movement_calls = list(movement_group.calls)

                # print("    movement group _emit_moves calls:")
                # for call in movement_calls:
                #     print(f"      call {call.call_index:04d}: moves={list(call.moves)!r}, stationary={sorted(call.stationary)!r}")
                # group_plan = plan_group_batches(hw, movement_calls, movement_initial)
                # print(
                #     "    plan_group_batches summary: "
                #     f"used_fallback={group_plan.used_fallback}, "
                #     f"fallback_reason={group_plan.fallback_reason!r}, "
                #     f"num_nodes={group_plan.num_nodes}, "
                #     f"num_precedence_edges={group_plan.num_precedence_edges}, "
                #     f"num_conflict_edges={group_plan.num_conflict_edges}"
                # )
                # print("    plan_group_batches batches:")
                # for batch_index, batch in enumerate(group_plan.batches):
                #     print(f"      optimized batch {batch_index:04d}: {batch!r}")

                # print("    movement group emitted stages:")
                # for stage_offset, stage in enumerate(preview_program.stages):
                #     print(
                #         f"      stage {stage_offset:04d}: kind={stage.kind!r}, "
                #         f"duration={stage.duration}, fidelity={stage.fidelity}, ops={stage.ops!r}"
                #     )
                # print(f"    planned_move_batches={[stage.ops for stage in preview_program.stages if stage.kind == 'move']!r}")

        stages_before = len(program.stages)
        compiler._emit_slot(program, slot, homes, current, future_2q)
        new_stages = program.stages[stages_before:]

        print("hardware stages emitted by this slot:")
        for stage_offset, stage in enumerate(new_stages, start=stages_before):
            if stage.kind == "move":
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


def test_print_zwq_compile_flow():
    qasm_path = (
        Path(__file__).resolve().parents[2]
        / "ZAC-main"
        / "benchmark"
        / "hpca"
        / "ising_n98_transpiled.qasm"
    )
    qasm_source = qasm_path.read_text(encoding="utf-8")

    initial_circuit = QuantumCircuit.from_qasm_str(qasm_source)
    logical = from_qiskit(initial_circuit)
    asap_slots = schedule(logical)
    hw = default_hardware()
    homes = place(logical, hw)
    result = compile_zwq(logical, hw)

    print("\n=== Initial QuantumCircuit: ZAC-main/benchmark/hpca/ising_n98_transpiled.qasm ===")
    print("initial_circuit", initial_circuit)

    print("\n=== LogicalCircuit ===")
    print(logical)

    print("\n=== Logicalops ===")
    for index, op in enumerate(logical.ops):
        print(f"{index:04d}: {op!r}")

    print("\n=== ASAP slots ===")
    for slot_index, slot in enumerate(asap_slots[:1]):
        print(f"slot {slot_index:04d}:")
        for op in slot:
            print(f"  {op!r}")

    # print("\n=== HardwareSpec ===")
    # print(hw.as_dict())

    # print("\n=== Placement homes ===")
    # for qubit, position in sorted(homes.items()):
    #     print(f"q{qubit:04d}: site={position!r}, coordinate={hw.coordinate(position)!r}")

    debug_program = _print_emit_slot_flow_zwq(logical, hw, homes, asap_slots)

    assert logical.ops
    assert asap_slots
    assert homes == result.homes
    # assert debug_program.stages
