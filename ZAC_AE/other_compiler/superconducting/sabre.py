from time import time
import json
import qiskit
from qiskit.transpiler import CouplingMap
from qiskit import transpile
from qiskit import QuantumCircuit
import argparse
import os

class SC_Compiler:
    def __init__(self, setting):
        self.setting = setting

    def sim_circuit(self, benchmark):
        with open(benchmark, 'r') as f:
            qasm_str = f.read()
            circuit = QuantumCircuit.from_qasm_str(qasm_str)
            cx_circuit = transpile(circuit, coupling_map=self.cmap,
                                   basis_gates=["cx", "id", "u2", "u1", "u3"],
                                    optimization_level=3,
                                    routing_method="sabre",
                                    layout_method="sabre",
                                    seed_transpiler=0)
            n_q = cx_circuit.num_qubits
            cir_fidelity = 1
            cir_fidelity_2q_gate = 1
            cir_fidelity_2q_gate_for_idle = 1
            cir_fidelity_1q_gate = 1
            cir_fidelity_atom_transfer = 1
            cir_fidelity_coherence = 1
            fidelity_2q_gate = 0.999
            fidelity_1q_gate = 0.9997 
            time_1q_gate = 0.025
            if self.setting["arch_spec"] == "grid":
                time_2q_gate = 0.042 
                time_coherence = 89
            else:
                time_2q_gate = 0.068
                time_coherence = 311
            print(self.setting["arch_spec"])
            # time_2q_gate = 0.026 # https://arxiv.org/pdf/2102.06132 # 0.48 # us
            # time_1q_gate = 0.00625 # https://journals.aps.org/prxquantum/abstract/10.1103/PRXQuantum.5.030353 #0.0352
            # time_coherence = 600 # Systematic improvements in transmon qubit coherence enabled by niobium surface encapsulation # 450
            count_2q = 0
            count_1q = 0
            list_qubit_last_time = [0 for i in range(0, n_q)]
            cir_qubit_busy_time = [0 for i in range(0, n_q)]
            register_idx = dict()
            idx_begin = 0
            for qubit in cx_circuit.qubits:
                if qubit._register not in register_idx:
                    register_idx[qubit._register] = idx_begin
                    idx_begin += qubit._register.size
            instruction = cx_circuit.data
            for ins in instruction:
                if ins.operation.num_qubits == 2:
                    # print(ins.operation.name)
                    count_2q += 1
                    q0 = register_idx[ins.qubits[0]._register] + ins.qubits[0]._index
                    q1 = register_idx[ins.qubits[1]._register] + ins.qubits[1]._index
                    op_time = max(list_qubit_last_time[q0], list_qubit_last_time[q1]) + time_2q_gate
                    list_qubit_last_time[q0] = op_time
                    list_qubit_last_time[q1] = op_time
                    cir_qubit_busy_time[q0] += time_2q_gate
                    cir_qubit_busy_time[q1] += time_2q_gate
                    # print(q0)
                    # print(q1)
                elif ins.operation.name != "measure":
                    q0 = register_idx[ins.qubits[0]._register] + ins.qubits[0]._index
                    list_qubit_last_time[q0] += time_1q_gate
                    cir_qubit_busy_time[q0] += time_1q_gate
                    count_1q += 1
        #             print(q0)
        #         print(list_qubit_last_time)
        #         print(cir_qubit_busy_time)
        #         input()
        # print(circuit_end_time)
        circuit_end_time = max(list_qubit_last_time)
        cir_fidelity_1q_gate = pow(fidelity_1q_gate, count_1q)
        cir_fidelity_2q_gate = pow(fidelity_2q_gate, count_2q)
        for t in cir_qubit_busy_time:
            if t > 0:
                idle_t = circuit_end_time - t
                cir_fidelity_coherence *= (1 - idle_t/time_coherence)
        cir_fidelity = cir_fidelity_1q_gate * cir_fidelity_2q_gate * cir_fidelity_coherence
        results = { "cir_fidelity" : cir_fidelity,
                    "cir_fidelity_1q_gate": cir_fidelity_1q_gate,
                    "cir_fidelity_2q_gate": cir_fidelity_2q_gate,
                    "cir_fidelity_2q_gate_for_idle": 1,
                    "cir_fidelity_atom_transfer": cir_fidelity_atom_transfer,
                    "cir_fidelity_coherence": cir_fidelity_coherence,
                    "cir_duration": circuit_end_time,
                    "num_2q_gate": count_2q}
        return results
    
    def parse_device(self):
        if self.setting["arch_spec"] == "grid":
            # print("use grid")
            self.cmap = CouplingMap().from_grid(11, 11)
        else:
            eagle = [(0, 1), (1, 2), (2, 3), (3, 4), (4, 5), (5, 6), (6, 7), (7, 8), (9, 10), (10, 11), (11, 12), (12, 13), \
                    (0, 14), (14, 18), (4, 15), (15, 22), (8, 16), (16, 26), (12, 17), (17, 30), \
                    (18, 19), (19, 20), (20, 21), (21, 22), (22, 23), (23, 24), (24, 25), (25, 26), (26, 27), (27, 28), (28, 29), (29, 30), (30, 31), (31, 32), \
                    (20, 33), (33, 39), (24, 34), (34, 43), (28, 35), (35, 47), (32, 36), (36, 51), \
                    (37, 38), (38, 39), (39, 40), (40, 41), (41, 42), (42, 43), (43, 44), (44, 45), (45, 46), (46, 47), (47, 48), (48, 49), (49, 50), (50, 51), \
                    (37, 52), (52, 56), (41, 53), (53, 60), (45, 54), (54, 64), (49, 55), (55, 68), \
                    (56, 57), (57, 58), (58, 59), (59, 60), (60, 61), (61, 62), (62, 63), (63, 64), (64, 65), (65, 66), (66, 67), (67, 68), (68, 69), (69, 70), \
                    (58, 71), (71, 77), (62, 72), (72, 81), (66, 73), (73, 85), (70, 74), (74, 89), \
                    (75, 76), (76, 77), (77, 78), (78, 79), (79, 80), (80, 81), (81, 82), (82, 83), (83, 84), (84, 85), (85, 86), (86, 87), (87, 88), (88, 89), \
                    (75, 90), (90, 94), (79, 91), (91, 98), (83, 92), (92, 102), (87, 93), (93, 106), \
                    (94, 95), (95, 96), (96, 97), (97, 98), (98, 99), (99, 100), (100, 101), (101, 102), (102, 103), (103, 104), (104, 105), (105, 106), (106, 107), (107, 108), \
                    (96, 109), (100, 110), (110, 118), (104, 111), (111, 112), (108, 112), (112, 126), \
                    (113, 114), (114, 115), (115, 116), (116, 117), (117, 118), (118, 119), (119, 120), (120, 121), (121, 122), (122, 123), (123, 124), (124, 125), (125, 126)]
            self.cmap = CouplingMap(eagle)

    def run(self, benchmark_sets):
        self.parse_device()
        # res_list = []
        for benchmark in benchmark_sets.all_benchmarks:
            # start = time()
            sim_result = self.sim_circuit(benchmark)
            # compiled_time = time() - start
            filename = benchmark.split('/')[-1]
            filename = filename.split('.')[0]
            filename = self.setting["dir"] + filename + "_fidelity.json"
            with open(filename, "w") as f:
                json.dump(sim_result, f, indent = 2)
            
            # todo: run simulation
            # directly prepare CSV here
        # return res_list
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('exp_spec', metavar='S', type=str, help='experiment specficiation')
    args = parser.parse_args()
    with open(args.exp_spec, 'r') as f:
        exp_spec = json.load(f)
    benchmark_set = []
    for name in exp_spec["qasm_list"]:
        if os.path.isfile(name):
            # file exists
            benchmark_set.append(name)
        if os.path.isdir(name):
            # directory exists
            for filename in os.listdir(name):
                f = os.path.join(name, filename)
                # checking if it is a file
                if os.path.isfile(f):
                    benchmark_set.append(f)
    for benchmark in benchmark_set:
        for sc_setting in exp_spec["sc_setting"]:
            sc_compiler = SC_Compiler(sc_setting)
            sc_compiler.parse_device()
            # print(benchmark)
            sim_result = sc_compiler.sim_circuit(benchmark)
            # compiled_time = time() - start
            filename = benchmark.split('/')[-1]
            filename = filename.split('.')[0]
            filename = sc_setting["dir"] + filename + "_fidelity.json"
            # print(filename)
            if not os.path.exists(sc_setting["dir"]):
                os.makedirs(sc_setting["dir"])
            with open(filename, "w") as f:
                json.dump(sim_result, f, indent = 2)
    print("[INFO] Finish Compilation")