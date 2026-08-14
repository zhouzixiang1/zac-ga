import time

import numpy as np
import yaml
from qiskit import QuantumCircuit, transpile
from qiskit.converters import circuit_to_dag

from compilers.analyzer import Analyzer

from .gen_coupling_map import gen_fpqa_coupling_map
from .generic_utils import (
    CompilerLogger,
    compiler_log,
    get_all_2q_gates,
    get_occupation_positions,
    get_occupation_positions_random,
    get_occupation_positions_sequential,
    post_processing,
    print_latex,
)
import json
# from pymetis import part_graph


class FPQACGenericCompiler(object):
    def __init__(self, configs):
        self.configs = configs
        self.last_transpiled_circ = None

    def run(self, benchmark_sets, hyperparam_sets):
        res_list = []

        for benchmark in benchmark_sets.all_benchmarks:
            for hyperparams in hyperparam_sets.all_sets:
                start = time.time()
                log = self.compile(benchmark.circ, hyperparams)
                compilation_time = time.time() - start
                tmp = benchmark.path.split("/")[-1]
                tmp = tmp.split(".")[0]
                # print(log)
                # print(len(log['code']))
                # print("hi")
                filename = f"../../result/atomique/{tmp}_fidelity.json"
                # for l in log:
                #     print(l)
                #     if l != 'code':
                #         print(log[l])
                # for l in log['code']:
                #     print(l)
                analyzer = Analyzer(log, hyperparams, benchmark)
                report = analyzer.analyze()
                report["compilation_time"] = compilation_time
                with open(filename, 'w') as f:  
                    json.dump(report, f, indent = 2)
                # report["hyperparams"] = hyperparams.__dict__.copy()
                # report["path"] = benchmark.path
                # res_list.append(report)

        def numpy_array_representer(dumper, data):
            return dumper.represent_list(data.tolist())

        yaml.add_representer(np.ndarray, numpy_array_representer)
        yaml.dump(res_list, open(f"{hyperparam_sets.configs.result_path}/res.yml", "w"))

        return res_list

    def heuristic_partition(
        self, circ, n_aods, n_rows, n_cols, fpqac_generic_partition_factor, seed=0
    ):
        n = circ.num_qubits
        new_circ = QuantumCircuit(n)

        for gate in circ:
            if gate[0].num_qubits == 2:
                qubit_index = [gate[1][0]._index, gate[1][1]._index]
                new_circ.append(gate[0], qubit_index)

        dag = circuit_to_dag(new_circ)
        matrix = np.zeros((n, n))
        decay_factor = 1
        for layer in dag.layers():
            for gate in layer["partition"]:
                q1 = gate[0]._index
                q2 = gate[1]._index
                matrix[q1][q2] += 1 * decay_factor
                matrix[q2][q1] += 1 * decay_factor
            decay_factor *= fpqac_generic_partition_factor
        k = n_aods + 1
        set_list = [[] for _ in range(k)]
        rng = np.random.default_rng(seed=seed)
        qubit_list = rng.permutation(n)
        for i in qubit_list:
            best = 1e10
            best_index = 0
            for j in range(k):
                if len(set_list[j]) >= (n_rows[j] * n_cols[j]):
                    continue
                new = 0
                for node in set_list[j]:
                    new += matrix[i][node]
                if new < best:
                    best = new
                    best_index = j
            set_list[best_index].append(i)
        mapping = {}
        for i in range(k):
            current_occupy = 0
            for node in set_list[i]:
                mapping[node] = current_occupy + (n_rows[0:i] * n_cols[0:i]).sum()
                current_occupy += 1
        mapping_list = [mapping[i] for i in range(n)]
        return mapping_list

    def SDP_partition(
        self, circ, n_aods, n_rows, n_cols, fpqac_generic_partition_factor, seed=0
    ):
        n = circ.num_qubits
        new_circ = QuantumCircuit(n)

        for gate in circ:
            if gate[0].num_qubits == 2:
                qubit_index = [gate[1][0].index, gate[1][1].index]
                new_circ.append(gate[0], qubit_index)

        dag = circuit_to_dag(new_circ)
        matrix = np.zeros((n, n))
        decay_factor = 1
        for layer in dag.layers():
            for gate in layer["partition"]:
                q1 = gate[0].index
                q2 = gate[1].index
                matrix[q1][q2] += 1 * decay_factor
                matrix[q2][q1] += 1 * decay_factor
            decay_factor *= fpqac_generic_partition_factor
        k = n_aods + 1
        import networkx as nx
        from maxcut import MaxCutSDP

        mapping = {}
        mapping[0] = list(range(n))
        for i in range(k - 1):
            # find the set that has the largest weight
            largest_set = 0
            largest_weight = 0
            for j in range(i):
                weight = 0
                for q1 in mapping[j]:
                    for q2 in mapping[j]:
                        weight += matrix[q1][q2]
                if weight > largest_weight:
                    largest_weight = weight
                    largest_set = j
            remaining_qubits = np.array(mapping[largest_set])
            if len(remaining_qubits) == 0:
                mapping[i + 1] = []
                continue
            G = nx.Graph()
            G.add_nodes_from(list(range(len(remaining_qubits))))
            G.add_edges_from(
                [
                    (i, j, {"weight": matrix[remaining_qubits[i]][remaining_qubits[j]]})
                    for i in range(len(remaining_qubits))
                    for j in range(len(remaining_qubits))
                ]
            )
            result = MaxCutSDP(G).get_results()
            mapping[largest_set] = remaining_qubits[np.where(result == 1)[0]]
            mapping[i + 1] = remaining_qubits[np.where(result == -1)[0]]

        for _ in range(10):
            n_1 = int(np.random.randint(0, k))
            n_2 = int(np.random.randint(0, k))
            if n_1 == n_2:
                continue
            remaining_qubits = np.concatenate((mapping[n_1], mapping[n_2]))
            G = nx.Graph()
            G.add_nodes_from(list(range(len(remaining_qubits))))
            G.add_edges_from(
                [
                    (i, j, {"weight": matrix[remaining_qubits[i]][remaining_qubits[j]]})
                    for i in range(len(remaining_qubits))
                    for j in range(len(remaining_qubits))
                ]
            )
            result = MaxCutSDP(G).get_results()
            mapping[n_1] = remaining_qubits[np.where(result == 1)[0]]
            mapping[n_2] = remaining_qubits[np.where(result == -1)[0]]

        current_occupy = np.zeros(k)
        mapping_per_qubit = {}
        # print(mapping)
        for i in range(k):
            for qubit in mapping[i]:
                aod_idx = i
                while current_occupy[aod_idx] >= n_rows[aod_idx] * n_cols[aod_idx]:
                    aod_idx = (aod_idx + 1) % k
                mapping_per_qubit[qubit] = (
                    current_occupy[aod_idx]
                    + (n_rows[0:aod_idx] * n_cols[0:aod_idx]).sum()
                )
                current_occupy[aod_idx] += 1

        mapping_list = [mapping_per_qubit[i] for i in range(n)]
        return mapping_list

    def get_circ_stats(self, circ):
        n_2q_gates = 0
        n_1q_gates = 0
        unique_2q_gates = {}
        n_qubits = circ.num_qubits
        for i in range(n_qubits):
            unique_2q_gates[i] = {}
        for gate in circ.data:
            wires = list(map(lambda x: x._index, gate[1]))
            if len(wires) == 2:
                n_2q_gates += 1
                unique_2q_gates[wires[0]][wires[1]] = 1
                unique_2q_gates[wires[1]][wires[0]] = 1
            elif len(wires) == 1:
                n_1q_gates += 1
        degree = 0
        for i in range(n_qubits):
            degree += len(unique_2q_gates[i])
        degree = degree / n_qubits
        return {
            "n_2q_gates": n_2q_gates,
            "degree": degree,
            "n_qubits": n_qubits,
            "n_1q_gates": n_1q_gates,
        }

    def compile(self, circ, hyperparams):
        n_qubits = circ.num_qubits
        start_time = time.time()
        n_rows = hyperparams.n_rows
        n_cols = hyperparams.n_cols
        n_aods = hyperparams.n_aods
        bidirect = hyperparams.fpqac_generic_cmap_bidirect
        seed_transpiler = hyperparams.fpqac_generic_seed_transpiler

        n_rows_cmap = hyperparams.fpqac_generic_n_rows_cmap
        n_cols_cmap = hyperparams.fpqac_generic_n_cols_cmap

        n_atoms_per_array = hyperparams.n_atoms_per_array
        n_qubits_max = n_atoms_per_array * (n_aods + 1)

        cmap = gen_fpqa_coupling_map(
            n_rows_cmap, n_cols_cmap, n_aods, n_qubits_max, bidirectional=bidirect
        )

        if (not hyperparams.retranspile) and self.last_transpiled_circ:
            circ = self.last_transpiled_circ
            print("reusing last transpiled circ")
        else:
            if hyperparams.fpqac_generic_partition_method == "sabre":
                self.circ_stats_before_transpilation = self.get_circ_stats(circ)
                circ = transpile(
                    circ,
                    basis_gates=["cz", "id", "u2", "u1", "u3"],
                    optimization_level=3,
                    seed_transpiler=seed_transpiler,
                )
                self.circ_stats = self.get_circ_stats(circ)
                circ = transpile(
                    circ,
                    basis_gates=["cz", "id", "u2", "u1", "u3"],
                    coupling_map=cmap,
                    optimization_level=3,
                    layout_method="sabre",
                    routing_method="sabre",
                    seed_transpiler=seed_transpiler,
                )
                self.last_transpiled_circ = circ
            elif hyperparams.fpqac_generic_partition_method == "heuristic":
                self.circ_stats_before_transpilation = self.get_circ_stats(circ)
                circ = transpile(
                    circ,
                    basis_gates=["cz", "id", "u2", "u1", "u3"],
                    optimization_level=3,
                    seed_transpiler=seed_transpiler,
                )
                self.circ_stats = self.get_circ_stats(circ)
                mapping = self.heuristic_partition(
                    circ,
                    n_aods,
                    n_rows,
                    n_cols,
                    hyperparams.fpqac_generic_partition_factor,
                    seed_transpiler,
                )
                circ = transpile(
                    circ,
                    basis_gates=["cz", "id", "u2", "u1", "u3"],
                    coupling_map=cmap,
                    optimization_level=3,
                    initial_layout=mapping,
                    routing_method="sabre",
                    seed_transpiler=seed_transpiler,
                )
                self.last_transpiled_circ = circ
            elif hyperparams.fpqac_generic_partition_method == "SDP":
                self.circ_stats_before_transpilation = self.get_circ_stats(circ)
                mapping = self.SDP_partition(
                    circ,
                    n_aods,
                    n_rows,
                    n_cols,
                    hyperparams.fpqac_generic_partition_factor,
                    seed_transpiler,
                )
                self.circ_stats = self.get_circ_stats(circ)
                circ = transpile(
                    circ,
                    basis_gates=["cz", "id", "u2", "u1", "u3"],
                    coupling_map=cmap,
                    optimization_level=3,
                    initial_layout=mapping,
                    routing_method="sabre",
                    seed_transpiler=seed_transpiler,
                )

            elif hyperparams.fpqac_generic_partition_method == "dense":
                self.circ_stats_before_transpilation = self.get_circ_stats(circ)
                circ = transpile(
                    circ,
                    basis_gates=["cz", "id", "u2", "u1", "u3"],
                    optimization_level=3,
                    seed_transpiler=seed_transpiler,
                )
                self.circ_stats = self.get_circ_stats(circ)
                circ = transpile(
                    circ,
                    basis_gates=["cz", "id", "u2", "u1", "u3"],
                    coupling_map=cmap,
                    optimization_level=3,
                    layout_method="dense",
                    routing_method="sabre",
                    seed_transpiler=seed_transpiler,
                )
                self.last_transpiled_circ = circ
            else:
                raise NotImplementedError(
                    f"Unknown partition method: {hyperparams.fpqac_generic_partition_method}"
                )
        if hyperparams.fpqac_qubit_atom_mapper == "default":
            occ, pos, occ_compile_time = get_occupation_positions(
                circ,
                n_rows,
                n_cols,
                n_aods,
                original_n_rows=n_rows_cmap,
                original_n_cols=n_cols_cmap,
            )
        elif hyperparams.fpqac_qubit_atom_mapper == "random":
            occ, pos, occ_compile_time = get_occupation_positions_random(
                circ,
                n_rows,
                n_cols,
                n_aods,
                original_n_rows=n_rows_cmap,
                original_n_cols=n_cols_cmap,
            )
        elif hyperparams.fpqac_qubit_atom_mapper == "sequantial":
            occ, pos, occ_compile_time = get_occupation_positions_sequential(
                circ,
                n_rows,
                n_cols,
                n_aods,
                original_n_rows=n_rows_cmap,
                original_n_cols=n_cols_cmap,
            )
        else:
            raise NotImplementedError(
                f"Unknown qubit atom mapper: {hyperparams.fpqac_qubit_atom_mapper}"
            )

        # log = compiler_log(
        # circ,
        # n_rows,
        # n_cols,
        # n_aods,
        # pos,
        # occ,
        # hyperparams,
        # )
        for i in range(n_qubits):
            if i not in pos:
                for j in range(n_rows[0]):
                    for k in range(n_cols[0]):
                        if [0, j, k] not in pos.values():
                            pos[i] = [0, j, k]

        compiler_logger = CompilerLogger(
            circ, n_rows, n_cols, n_aods, pos, occ, hyperparams
        )
        log = compiler_logger.get_log()
        # print(log)

        log["compilation_time"] = float(time.time() - start_time)
        log["others"]["original_n_2q_gates"] = self.circ_stats["n_2q_gates"]
        log["others"]["n_2q_gates_before_transpilation"] = (
            self.circ_stats_before_transpilation["n_2q_gates"]
        )
        log["others"]["n_1q_gates_before_transpilation"] = (
            self.circ_stats_before_transpilation["n_1q_gates"]
        )
        log["others"]["degree"] = self.circ_stats["degree"]
        log["others"]["n_qubits"] = self.circ_stats["n_qubits"]

        # print(res)
        return log


def main_arb():

    n_rows = n_cols = 10
    n_g = 10
    final_n_rows = final_n_cols = 16
    for n_aods in [2]:
        cmap = gen_fpqa_coupling_map(n_rows, n_cols, n_aods, bidirectional=True)
        for n_q in [5, 10, 20, 50, 100]:
            # for n_q in [5, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100]:
            res_all = []
            # print(n_q)

            for i in range(10):
                circ_name = f"baseline/qiskit_fpqa/qiskit_fpqa{n_rows}_{n_aods}/arb/q{n_q}_g{n_g}/i{i}.qasm"
                circ = QuantumCircuit.from_qasm_file(circ_name)
                # print(len(get_all_2q_gates(circ)))
                circ = transpile(
                    circ,
                    basis_gates=["cz", "id", "u2", "u1", "u3"],
                    coupling_map=cmap,
                    optimization_level=2,
                    layout_method="sabre",
                    routing_method="sabre",
                    seed_transpiler=0,
                )
                n_orig_2q_gates = len(get_all_2q_gates(circ))
                occ, pos, occ_compile_time = get_occupation_positions(
                    circ,
                    final_n_rows,
                    final_n_cols,
                    n_aods,
                    original_n_rows=n_rows,
                    original_n_cols=n_cols,
                )

                res = count_layer_multiaod(
                    circ,
                    n_rows,
                    n_cols,
                    n_aods,
                    pos,
                    occ,
                    backend_config,
                    n_heating_reset_cycle=20,
                )
                res["compilation time"] += occ_compile_time
                compile_time_file = f"baseline/qiskit_fpqa/qiskit_fpqa{n_rows}_{n_aods}/arb/q{n_q}_g{n_g}.csv"
                with open(compile_time_file) as f:
                    reader = csv.reader(f)
                    for k, row in enumerate(reader):
                        if k == i:
                            res["compilation time"] += float(row[4])

                res_all.append(post_processing(res, n_q))

            avg_res = deepcopy(res_all[0])
            # print(res_all)

            for key in avg_res:
                if key == "fidelity":
                    for key2 in avg_res[key]:
                        avg_res[key][key2] = sum(
                            [res[key][key2] for res in res_all]
                        ) / len(res_all)
                elif key == "time":
                    for key2 in avg_res[key]:
                        avg_res[key][key2] = sum(
                            [res[key][key2] for res in res_all]
                        ) / len(res_all)
                else:
                    avg_res[key] = sum([res[key] for res in res_all]) / len(res_all)
            # print(avg_res)
            # print(f" {avg_res["fidelity"]["movement"]} & {avg_res['n_2q_gates']} & {avg_res['n_2q_layer']} ")

            # print(avg_res["fidelity"]["movement"])
            # print(avg_res["fidelity"]["decoherence_movement"])
            print_latex(avg_res)
