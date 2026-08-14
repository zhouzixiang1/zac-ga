import os
import pdb

from qiskit import QuantumCircuit, transpile

# pdb.set_trace()


if __name__ == "__main__":
    # benchmarks = [
    #     'vqe_uccsd_n6',
    #     'ising_n34',
    #     'adder_n10',
    #     'adder_n64',
    #     'bv_n14',
    #     'gcm_n13',
    #     'hhl_n7',
    #     'multiplier_n15',
    #     'multiplier_n45',
    #     'multiplier_n75',
    #     'qft_n18',
    #     'qft_n29',
    #     'qft_n63',
    #     'sat_n11',
    #     'shor_n5',
    #     'swap_test_n41',
    # ]

    benchmarks = [
        # 'bv_n70',
        # 'swap_test_n83',
        # "ising_n98",
        # "adder_n118",
        # "knn_n129",
        # "knn_n67",
        # "qugan_n39",
        # "qugan_n71",
        # "qugan_n111",
        # "swap_test_n115",
        # "qpe_n9",
        # "dnn_n8",
        "bv_n280"
    ]

    for benchmark in benchmarks:
        print("Preprocessing {}...".format(benchmark))
        n_qubits = int(benchmark.split("_")[-1][1:])
        if n_qubits < 11:
            size = "small"
        elif n_qubits < 28:
            size = "medium"
        else:
            size = "large"

        prefix = "raw/qasmbench"
        if benchmark == "gcm_n13":
            path = f"{prefix}/{size}/{benchmark}/gcm_h6.qasm"
        elif os.path.exists(f"{prefix}/{size}/{benchmark}/{benchmark}_transpiled.qasm"):
            path = f"{prefix}/{size}/{benchmark}/{benchmark}_transpiled.qasm"
        else:
            path = f"{prefix}/{size}/{benchmark}/{benchmark}.qasm"

        with open(path, "r") as f:
            lines = f.readlines()

        with open(f"{benchmark}.qasm", "w") as fid:
            for line in lines:
                if (
                    line.startswith("barrier")
                    or line.startswith("measure")
                    or line.startswith("reset")
                ):
                    # line.startswith('snapshot') or line.startswith('save') or line.startswith('load') or line.startswith('creg') or line.startswith('qreg') or line.startswith('include') or line.startswith('OPENQASM') or line.startswith('pragma') or line.startswith('opaque') or line.startswith('if') or line.startswith('else') or line.startswith('endif') or line.startswith('gate') or line.startswith('circuit') or line.startswith('end') or line.startswith('version') or line.startswith('output') or line.startswith('decl'):
                    continue
                fid.write(line)
        print("Done.")

        circ = QuantumCircuit.from_qasm_file(f"{benchmark}.qasm")
        circ = transpile(circ, basis_gates=["u1", "u2", "u3", "cx", "id"])
        res = circ.count_ops()
        print(
            f"{benchmark}\n 1Q gates: {res.get('u1', 0) + res.get('u2', 0) + res.get('u3', 0)} \n 2Q gates: {res['cx']}"
        )
