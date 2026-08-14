import numpy as np
from qiskit import QuantumCircuit, transpile


class Benchmark(object):
    def __init__(self):
        pass

    def __repr__(self) -> str:
        return self.path


class ArbitraryDesignedBenchmark(Benchmark):
    def __init__(self, n_qubits, n_gates, max_distance_2q, n_interact_q, i):
        super().__init__()
        self.n_qubits = n_qubits
        self.n_gates = n_gates
        self.i = i
        self.path = f"benchmarks/arbitrary/designed/q{n_qubits}/ng{n_gates}/maxd{max_distance_2q}_ninter{n_interact_q}/i{i}.qasm"
        print(self.path)
        self.circ = QuantumCircuit.from_qasm_file(self.path)
        self.circ.remove_final_measurements()


class ArbitraryBenchmark(Benchmark):
    def __init__(self, n_qubits, n_gates, i):
        super().__init__()
        self.n_qubits = n_qubits
        self.n_gates = n_gates
        self.i = i
        self.path = f"benchmarks/arbitrary/rand/q{n_qubits}_g{n_gates}/i{i}.qasm"
        print(self.path)
        self.circ = QuantumCircuit.from_qasm_file(self.path)
        self.circ.remove_final_measurements()


class SupermarqBenchmark(Benchmark):
    def __init__(self, type, n_qubits):
        super().__init__()
        self.type = type
        self.path = f"benchmarks/supermarq/supermarq_{type}_n{n_qubits}.qasm"
        print(self.path)
        self.circ = QuantumCircuit.from_qasm_file(self.path)
        self.n_qubits = self.circ.num_qubits
        self.circ.remove_final_measurements()


class GeyserBenchmark(Benchmark):
    def __init__(self, type, n_qubits):
        super().__init__()
        self.type = type
        self.path = f"benchmarks/geyser/{type}_n{n_qubits}.qasm"
        print(self.path)
        self.circ = QuantumCircuit.from_qasm_file(self.path)
        self.n_qubits = self.circ.num_qubits
        self.circ.remove_final_measurements()


class QASMBenchmark(Benchmark):
    def __init__(self, type, n_qubits):
        super().__init__()
        self.type = type
        self.path = f"benchmarks/QASMBench/{type}_n{n_qubits}.qasm"
        print(self.path)
        self.circ = QuantumCircuit.from_qasm_file(self.path)
        self.n_qubits = self.circ.num_qubits
        self.circ.remove_final_measurements()


class AlgorithmBenchmark(Benchmark):
    def __init__(self, type, n_qubits):
        super().__init__()
        self.n_qubits = n_qubits
        self.path = f"benchmarks/algorithm/{type}_n{n_qubits}.qasm"
        # print(self.path)
        self.circ = QuantumCircuit.from_qasm_file(self.path)
        self.circ.remove_final_measurements()


def pauli_strings_to_qiskit_circuit(pauli_strings, keep_length=None):
    if keep_length is not None:
        pauli_strings = pauli_strings[:keep_length]
    circ = QuantumCircuit(len(pauli_strings[0]))
    for paulis in pauli_strings:
        plist = []
        for i in range(len(paulis)):
            if paulis[i] == "X":
                circ.h(i)
                plist.append(i)
            elif paulis[i] == "Y":
                circ.h(i)
                circ.sdg(i)
                plist.append(i)
            elif paulis[i] == "Z":
                plist.append(i)
        if len(plist) > 1:
            for i in plist[1:]:
                circ.cx(i, plist[0])
            circ.rz(np.pi / 8, plist[0])  # change to an rather arbitrary angle
            for i in plist[1:]:
                circ.cx(i, plist[0])
        elif len(plist) == 1:
            circ.rz(np.pi / 8, plist[0])
        for i in range(len(paulis)):
            if paulis[i] == "X":
                circ.h(i)
            elif paulis[i] == "Y":
                circ.s(i)
                circ.h(i)
            elif paulis[i] == "Z":
                pass
    circ = transpile(circ, basis_gates=["u3", "id", "cz"])
    return circ


class QsimRandBenchmark(Benchmark):
    def __init__(self, n_qubits, keep_length, p, i):
        super().__init__()
        self.n_qubits = n_qubits
        self.keep_length = keep_length
        self.p = p
        self.i = i
        self.path = f"benchmarks/qsim/rand/q{n_qubits}_{keep_length}_p{p}/i{i}.txt"
        print(self.path)

        with open(self.path, "r") as fid:
            self.pauli_strings = eval(fid.read())[0:keep_length]

        self.circ = pauli_strings_to_qiskit_circuit(
            self.pauli_strings, keep_length=self.keep_length
        )


class QsimMoleculeBenchmark(Benchmark):
    def __init__(self, type):
        super().__init__()
        n_qubits_dict = {
            "H2": 4,
            "LiH": 8,
            "ch4_jw": 6,
            "ch4_bk": 6,
            "beh2_jw": 14,
            "beh2_bk": 14,
            "ch4new_jw": 10,
            "ch4new_bk": 10,
            "h2o_jw": 6,
            "h2o_bk": 6,
            "lih_jw": 6,
            "lih_bk": 6,
            "h2_jw": 4,
            "h2_bk": 4,
        }
        self.n_qubits = n_qubits_dict[type]
        self.type = type
        self.path = f"benchmarks/qsim/molecule/{type}.txt"
        print(self.path)

        with open(self.path, "r") as fid:
            self.pauli_strings = eval(fid.read())

        self.circ = pauli_strings_to_qiskit_circuit(self.pauli_strings)


class QAOARandBenchmark(Benchmark):
    def __init__(self, n_qubits, p, i):
        super().__init__()
        self.n_qubits = n_qubits
        self.p = p
        self.i = i
        self.path = f"benchmarks/qaoa/rand/q{n_qubits}_p{p}/i{i}.txt"
        print(self.path)
        self.circ = QuantumCircuit(n_qubits)

        with open(self.path, "r") as fid:
            self.edges = eval(fid.read())

        for edge in self.edges:
            self.circ.cz(edge[0], edge[1])

        for n in range(n_qubits):
            self.circ.h(n)

        print(len(self.edges))


class QAOARegularBenchmark(Benchmark):
    def __init__(self, n_qubits, regular, i):
        super().__init__()
        self.n_qubits = n_qubits
        self.regular = regular
        self.i = i
        self.path = f"benchmarks/qaoa/regular/q{n_qubits}_regular{regular}/i{i}.txt"
        print(self.path)
        self.circ = QuantumCircuit(n_qubits)

        with open(self.path, "r") as fid:
            self.edges = eval(fid.read())

        for edge in self.edges:
            self.circ.cz(edge[0], edge[1])

        for n in range(n_qubits):
            self.circ.h(n)

        print(len(self.edges))


class BenchmarkSets(object):
    def __init__(self, configs) -> None:
        self.all_benchmarks = []
        for bench in configs.benchmarks:
            if bench["name"] == "arbitrary":
                for n_qubits in bench["n_qubits"]:
                    for n_gates in bench["n_gates"]:
                        for i in range(bench["i"]):
                            self.all_benchmarks.append(
                                ArbitraryBenchmark(n_qubits, n_gates, i)
                            )

            elif bench["name"] == "arbitrary_designed":
                for n_qubits in bench["n_qubits"]:
                    for n_gates in bench["n_gates"]:
                        for dist in bench["max_distance_2q"]:
                            for n_interact_q in bench["n_interact_q"]:
                                for i in range(bench["i"]):
                                    self.all_benchmarks.append(
                                        ArbitraryDesignedBenchmark(
                                            n_qubits, n_gates, dist, n_interact_q, i
                                        )
                                    )

            elif bench["name"] == "supermarq":
                for type in bench["type"]:
                    for n_qubits in bench["n_qubits"]:
                        self.all_benchmarks.append(SupermarqBenchmark(type, n_qubits))
            elif bench["name"] == "geyser":
                for type in bench["type"]:
                    for n_qubits in bench["n_qubits"]:
                        self.all_benchmarks.append(GeyserBenchmark(type, n_qubits))
            elif bench["name"] == "QASMBench":
                for type in bench["type"]:
                    for n_qubits in bench["n_qubits"]:
                        self.all_benchmarks.append(QASMBenchmark(type, n_qubits))
            elif bench["name"] == "qsim_rand":
                for n_qubits in bench["n_qubits"]:
                    for keep_length in bench["keep_length"]:
                        for p in bench["p"]:
                            for i in range(bench["i"]):
                                self.all_benchmarks.append(
                                    QsimRandBenchmark(n_qubits, keep_length, p, i)
                                )

            elif bench["name"] == "qsim_molecule":
                for type in bench["type"]:
                    self.all_benchmarks.append(QsimMoleculeBenchmark(type))

            elif bench["name"] == "qaoa_rand":
                for n_qubits in bench["n_qubits"]:
                    for p in bench["p"]:
                        for i in range(bench["i"]):
                            self.all_benchmarks.append(
                                QAOARandBenchmark(n_qubits, p, i)
                            )

            elif bench["name"] == "qaoa_regular":
                for n_qubits in bench["n_qubits"]:
                    for regular in bench["regular"]:
                        for i in range(bench["i"]):
                            self.all_benchmarks.append(
                                QAOARegularBenchmark(n_qubits, regular, i)
                            )
            elif bench["name"] == "algorithm":
                for type in bench["type"]:
                    for n_qubits in bench["n_qubits"]:
                        self.all_benchmarks.append(AlgorithmBenchmark(type, n_qubits))

    def __getitem__(self, key):
        return self.all_benchmarks[key]

    def __repr__(self) -> str:
        return self.all_benchmarks.__repr__()
