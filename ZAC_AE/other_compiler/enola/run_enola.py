import os
from enola.enola import Enola
import argparse
import json
from qiskit import transpile, QuantumCircuit
from simulator import Simulator

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

    dict_arch = dict()

    tmp_directory =  exp_spec["dir"]+"code"
    if not os.path.exists(tmp_directory):
        os.makedirs(tmp_directory)
    tmp_directory =  exp_spec["dir"]+"time"
    if not os.path.exists(tmp_directory):
        os.makedirs(tmp_directory)
    tmp_directory =  exp_spec["dir"]+"fidelity"
    if not os.path.exists(tmp_directory):
        os.makedirs(tmp_directory)


    for benchmark in benchmark_set:
        print("==============================================")
        print("[INFO] Compile circuit {}".format(benchmark))
        
        filename = benchmark.split('/')[-1]
        filename = filename.split('.')[0]
        
        g_q = []
        dict_g_1q_parent = {-1: []}
        if benchmark.split('.')[-1] == "qasm":
            with open(benchmark, 'r') as f:
                qasm_str = f.read()
                circuit = QuantumCircuit.from_qasm_str(qasm_str)
                cz_circuit = transpile(circuit, basis_gates=["cz", "id", "u2", "u1", "u3"],
                                        optimization_level=3,
                                        seed_transpiler=0)
                n_q = cz_circuit.num_qubits
                list_qubit_last_2q_gate = [-1 for i in range(0, n_q)]
                register_idx = dict()
                idx_begin = 0
                for qubit in cz_circuit.qubits:
                    if qubit._register not in register_idx:
                        register_idx[qubit._register] = idx_begin
                        idx_begin += qubit._register.size
                instruction = cz_circuit.data
                for ins in instruction:
                    if ins.operation.num_qubits == 2:
                        list_qubit_last_2q_gate[ins.qubits[0]._index] = len(g_q)
                        list_qubit_last_2q_gate[ins.qubits[1]._index] = len(g_q)
                        q0 = register_idx[ins.qubits[0]._register] + ins.qubits[0]._index
                        q1 = register_idx[ins.qubits[1]._register] + ins.qubits[1]._index
                        if q0 < q1:
                            g_q.append([q0, q1])
                        else:
                            g_q.append([q1, q0])
                    elif ins.operation.name != "measure":
                        q0 = register_idx[ins.qubits[0]._register] + ins.qubits[0]._index
                        if list_qubit_last_2q_gate[q0] not in dict_g_1q_parent:
                            dict_g_1q_parent[list_qubit_last_2q_gate[q0]] = []    
                        dict_g_1q_parent[list_qubit_last_2q_gate[q0]].append((ins.operation.name, q0))
        
        directory = exp_spec["dir"]

        tmp = Enola(filename,
            dir=directory,
            trivial_layout = False,
            routing_strategy = "maximalSortIs",
            reverse_to_initial = True,
            dependency = True
        )

        tmp.setArchitecture([10, 10, 10, 10])
        tmp.setProgram(g_q)
        program_list = tmp.solve(save_file=False)

        # postprocessing to insert single qubit gates
        tmp = []
        for inst in program_list:
            tmp.append(inst)
            if inst["type"] == "Init":
                gate_1q_scheduling = [{"name": gate_1q[0], "q": gate_1q[1]} for gate_1q in dict_g_1q_parent[-1]]
                if len(gate_1q_scheduling) > 0:
                    inst_1q = {
                        "type": "single_qubit_gate",
                        "name": "single_qubit_gate",
                        "gates": gate_1q_scheduling,
                        "duration": 0.625,
                        "state": {}
                    }
                    tmp.append(inst_1q)
            elif inst["type"] == "Rydberg":
                gates = inst["gates"]
                gate_1q_scheduling = []
                for g in gates: 
                    gate_idx = g["id"]
                    if gate_idx in dict_g_1q_parent:
                        for gate_1q in dict_g_1q_parent[gate_idx]:
                            gate_1q_scheduling.append({"name": gate_1q[0], "q": gate_1q[1]})
                if len(gate_1q_scheduling) > 0:
                    inst_1q = {
                        "type": "single_qubit_gate",
                        "name": "single_qubit_gate",
                        "gates": gate_1q_scheduling,
                        "duration": 0.625,
                        "state": {}
                    }
                    tmp.append(inst_1q)
        
        code_filename = directory + f"code/{filename}_code.json"
        with open(code_filename, 'w') as f:
                json.dump(tmp, f)
        
        param_fidelity = {
            "2QG": 0.995,
            "1QG": 0.9997,
            "AT": 0.999,
            "T": 1.5e6
        }

        simulator = Simulator(code_filename, param_fidelity)
        fideilty_result = simulator.simulate()
        fidelity_filename = directory + f"fidelity/{filename}_fidelity.json"
        with open(fidelity_filename, 'w') as f:  
            json.dump(fideilty_result, f, indent = 2)
    print("[INFO] Finish Compilation")