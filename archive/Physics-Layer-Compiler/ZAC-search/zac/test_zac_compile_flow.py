from pathlib import Path
import sys

from qiskit import QuantumCircuit


ROOT = Path(__file__).resolve().parents[2]
ZAC_MAIN = ROOT / "ZAC-main"

if str(ZAC_MAIN) not in sys.path:
    sys.path.insert(0, str(ZAC_MAIN))

from zac.zac import ZAC


def test_print_bv_n19_compile_flow():
    qasm_path = ROOT / "ZAC-main" / "benchmark" / "hpca" / "ising_n42.qasm"
    qasm_source = qasm_path.read_text(encoding="utf-8")

    initial_circuit = QuantumCircuit.from_qasm_str(qasm_source)

    zac_compiler = ZAC()
    zac_compiler.set_program(str(qasm_path))
    asap_slots = zac_compiler.asap()

    print("\n=== Initial QuantumCircuit: ZAC-main/benchmark/hpca/bv_n19_transpiled.qasm ===")
    print(f"num_qubits={initial_circuit.num_qubits}, num_clbits={initial_circuit.num_clbits}")
    print(f"count_ops={dict(initial_circuit.count_ops())}")
    print(qasm_source)

    print("\n=== ZAC parsed circuit summary ===")
    print(f"n_q={zac_compiler.n_q}")
    print(f"n_g={zac_compiler.n_g}")
    print(f"g_s={zac_compiler.g_s!r}")

    print("\n=== ZAC LogicalCircuit equivalent: g_q two-qubit dependency stream ===")
    print(zac_compiler.g_q)

    print("\n=== ZAC Logicalops equivalent: indexed g_q plus dependent 1q gates ===")
    initial_1q_gates = zac_compiler.dict_g_1q_parent.get(-1, [])
    print(f"initial 1q gates before any 2q gate: {initial_1q_gates!r}")
    for index, gate in enumerate(zac_compiler.g_q):
        dependent_1q_gates = zac_compiler.dict_g_1q_parent.get(index, [])
        print(f"{index:04d}: 2q={gate!r}, dependent_1q_after_this_gate={dependent_1q_gates!r}")

    print("\n=== ZAC ASAP slots ===")
    for slot_index, slot in enumerate(asap_slots):
        print(f"slot {slot_index:04d}: gate_indices={slot!r}")
        for gate_index in slot:
            print(f"  {gate_index:04d}: {zac_compiler.g_q[gate_index]!r}")

    # assert initial_circuit.num_qubits == 19
    # assert zac_compiler.n_q == 19
    assert zac_compiler.n_g == len(zac_compiler.g_q)
    assert zac_compiler.g_q
    assert asap_slots
