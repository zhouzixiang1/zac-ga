import sys
from qiskit import QuantumCircuit, transpile, qasm2


original_file = sys.argv[1]
with open(original_file, 'r') as f:
    qasm_str = f.read()
circuit = QuantumCircuit.from_qasm_str(qasm_str)
cz_circuit = transpile(circuit, basis_gates=["cz", "id", "u2", "u1", "u3"],
                        optimization_level=3,
                        seed_transpiler=0)
qasm2.dump(cz_circuit, original_file.replace('bench_original/', 'bench_ucz/').replace('transpiled.', 'ucz.'))