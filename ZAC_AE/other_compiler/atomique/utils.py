from __future__ import annotations

import glob
import json
import os
import subprocess
from time import time

import numpy as np
from qiskit import transpile
from qiskit.converters import circuit_to_dag

# from compilers.FAATriangular.code.gen_qasm import geyser


def count_1q_2q_gates(circ):
    dag = circuit_to_dag(circ)
    n_1q_gate = 0
    n_2q_gate = 0
    for node in dag.topological_op_nodes():
        if node.op.num_qubits == 2:
            n_2q_gate += 1
        else:
            n_1q_gate += 1
    return n_1q_gate, n_2q_gate


def get_n2q_interation_stats(circ):
    circ.remove_final_measurements()
    dag = circuit_to_dag(circ)
    gates_1q = []
    gates = []
    for node in dag.topological_op_nodes():
        if node.op.num_qubits == 2:
            gate_2q = list(map(lambda x: x._index, node.qargs))
            gates.append(gate_2q)
        else:
            gates_1q.append(node.qargs[0]._index)

    stats = {}
    stats["num_qubits"] = circ.num_qubits
    stats["num_1q_gates"] = len(gates_1q)
    stats["num_2q_gates"] = len(gates)

    n_2q_gate_qubit_dict = {}
    interact_qubit_dict = {}

    # for k in range(n_qubits):
    # n_2q_gate_qubit_dict[k] = 0
    # interact_qubit_dict[k] = set()

    for gate in gates:
        q0, q1 = gate
        if q0 not in interact_qubit_dict:
            interact_qubit_dict[q0] = set()
        if q1 not in interact_qubit_dict:
            interact_qubit_dict[q1] = set()

        if q0 not in n_2q_gate_qubit_dict.keys():
            n_2q_gate_qubit_dict[q0] = 0
        if q1 not in n_2q_gate_qubit_dict.keys():
            n_2q_gate_qubit_dict[q1] = 0

        interact_qubit_dict[q0].add(q1)
        interact_qubit_dict[q1].add(q0)
        n_2q_gate_qubit_dict[q0] += 1
        n_2q_gate_qubit_dict[q1] += 1

    interact_qubit_list = [len(set) for q, set in interact_qubit_dict.items()]
    n_2q_gate_qubit_list = [
        n_2q_gate_qubit_dict[q] for q in n_2q_gate_qubit_dict.keys()
    ]

    stats["n_2q_gate_qubit_list"] = n_2q_gate_qubit_list
    stats["interact_qubit_list"] = interact_qubit_list
    stats["n_2q_gate_qubit_list_mean"] = float(np.mean(n_2q_gate_qubit_list))
    stats["interact_qubit_list_mean"] = float(np.mean(interact_qubit_list))

    stats["n_2q_gate_qubit_list_std"] = float(np.std(n_2q_gate_qubit_list))
    stats["interact_qubit_list_std"] = float(np.std(interact_qubit_list))

    return stats


def get_hop_distance_square(circ_orig, circ_transpiled, n_rows, n_cols):
    v2p = get_v2p_mapping(circ_transpiled)

    dag = circuit_to_dag(circ_orig)
    gates = []
    for node in dag.topological_op_nodes():
        if node.op.num_qubits == 2:
            gate_2q = list(map(lambda x: x.index, node.qargs))
            gates.append(gate_2q)

    hop_dists = []
    for gate in gates:
        # obtain the x, y index:
        x0 = v2p[gate[0]] % n_cols
        y0 = v2p[gate[0]] // n_cols
        x1 = v2p[gate[1]] % n_cols
        y1 = v2p[gate[1]] // n_cols

        # compute hop distance
        hop_dist = np.abs(x0 - x1) + np.abs(y0 - y1)
        hop_dists.append(float(hop_dist))
    print(hop_dists)
    return hop_dists, np.mean(hop_dists), np.std(hop_dists)


def get_v2p_mapping(circ):
    layout = circ._layout
    v2p = {}
    for v, p in layout._v2p.items():
        if not v.register.name == "ancilla":
            v2p[v.index] = p
    return v2p


def get_p2v_mapping(circ):
    layout = circ._layout
    p2v = {}
    for v, p in layout._v2p.items():
        if not v.register.name == "ancilla":
            p2v[p] = v.index
    return p2v


def compile_backend(circ, compiler="qiskit", max_col=16, max_row=16, opt=3, cmap=None):
    if compiler == "qiskit":
        from qiskit.providers.fake_provider import FakeWashington
        backend = FakeWashington()
        if cmap is None:
            cmap = backend.configuration().coupling_map
        basis_gates = ["u3", "cx", "id"]
        compiled_circuit = transpile(
            circ,
            coupling_map=cmap,
            basis_gates=basis_gates,
            optimization_level=opt,
            routing_method="sabre",
            layout_method="sabre",
        )

    elif compiler == "geyser":
        compiled_circuit = geyser(circ)

    else:
        return

    return compiled_circuit


def compile_save(circ, results_file, compiler="qiskit", i=None, cmap=None):
    start = time()
    try:
        compiled_circuit = compile_backend(circ, compiler, cmap=cmap)
        n2q, nl2, n1q, nl1, dep = count_layer(compiled_circuit)
        print("Num of 2q gates:", n2q)
        print("Num of 2q layers:", nl2)
        print("Num of 1q gates:", n1q)
        print("Num of 1q layers:", nl1)
        t = time() - start
        print("Time:", t)
        results = open(results_file, "a")
        if i is None:
            results.write(
                str(n2q)
                + ","
                + str(nl2)
                + ","
                + str(n1q)
                + ","
                + str(nl1)
                + ","
                + str(t)
                + ","
                + str(dep)
                + "\n"
            )
        else:
            results.write(
                str(n2q)
                + ","
                + str(nl2)
                + ","
                + str(n1q)
                + ","
                + str(nl1)
                + ","
                + str(t)
                + ","
                + str(dep)
                + ","
                + i
                + "\n"
            )
        results.close()
    except BaseException:
        results = open(results_file, "a")
        results.write("Failed!\n")
        results.close()
        print("Failed!")
        return
    return compiled_circuit


def count_layer(circ):
    dep = circ.depth()
    num_2q_gates = 0
    num_1q_gates = 0
    num_layers_2 = 0
    num_layers_1 = 0
    dag = circuit_to_dag(circ)
    while True:
        add_layer_2 = False
        add_layer_1 = False
        front_layer = dag.front_layer()
        for node in front_layer:
            if node.op.num_qubits == 1:
                dag.remove_op_node(node)
                num_1q_gates += 1
                add_layer_1 = True
        if dag.depth() == 0:
            break
        for node in front_layer:
            if node.op.num_qubits == 2:
                num_2q_gates += 1
                dag.remove_op_node(node)
                add_layer_2 = True
        if add_layer_1:
            num_layers_1 += 1
        if add_layer_2:
            num_layers_2 += 1
        if dag.depth() == 0:
            break

    results = {
        "n_2q_gate": num_2q_gates,
        "n_2q_layer": num_layers_2,
        "n_1q_gate": num_1q_gates,
        "n_1q_layer": num_layers_1,
        "depth": dep,
    }
    return results


def save_physical_to_virtual_mapping(circ, file_name):
    if getattr(circ, "_layout", None) is not None:
        try:
            p2v_orig = circ._layout.final_layout.get_physical_bits().copy()
        except BaseException:
            p2v_orig = circ._layout.get_physical_bits().copy()
        p2v = {}
        for p, v in p2v_orig.items():
            if v.register.name == "q":
                p2v[p] = v.index
            else:
                p2v[p] = f"{v.register.name}.{v.index}"
    else:
        p2v = {}
        for p in range(circ.num_qubits):
            p2v[p] = p

    # create json object from dictionary
    p2vjson = json.dumps(p2v)

    # open file for writing, "w"
    f = open(file_name, "w")

    # write json object to file
    f.write(p2vjson)

    # close file
    f.close()


# def test_logger():
#     n_qubits = 20
#     logger = Logger(n_qubits)
#     logger.set_pos({i: [i % 4, i // 4] for i in range(n_qubits)})
#     logger.create_ancilla([[i, i, i, i] for i in range(4)])
#     logger.move([[i, i] for i in range(4)])
#     logger.gate_2Q([[i, i] for i in range(4)])
#     logger.move([[4, 0], [11, 3]])
#     logger.gate_2Q([[4, 0], [11, 3]])
#     logger.move([[i, i] for i in range(4)])
#     logger.gate_2Q([[i, i] for i in range(4)])
#     logger.destroy_ancilla([i for i in range(4)])
#     logger.gate_1Q([i for i in range(4)])
#     logger.save("test.json")


class Position:
    def __init__(self, x, y, z):
        self.x = x
        self.y = y
        self.z = z  # z stands for the aod number, slm:-1, aod:0,1,2

    def __hash__(self) -> int:
        return hash((self.x, self.y, self.z))

    def __str__(self) -> str:
        return f"({self.x}, {self.y}, {self.z})"

    def __repr__(self) -> str:
        return f"({self.x}, {self.y}, {self.z})"

    def __eq__(self, other) -> bool:
        return self.x == other.x and self.y == other.y and self.z == other.z

    def __sub__(self, other):
        return Position(self.x - other.x, self.y - other.y, self.z - other.z)

    def __abs__(self):
        return np.sqrt(self.x**2 + self.y**2 + self.z**2)

    def to_pair(self) -> str:
        return (self.x, self.y, self.z)


def fig_to_video():
    if os.path.exists("animation/video/video.mp4"):
        os.remove("animation/video/video.mp4")
    compress = [
        "ffmpeg",
        "-framerate",
        "60",
        "-f",
        "image2",
        "-i",
        "./animation/fig/%d.png",
        "./animation/video/video.mp4",
    ]
    p = subprocess.Popen(compress)
    p.wait()
    files = glob.glob("animation/fig/*")
    for file in files:
        os.remove(file)
    p.kill()


def get_fid_and_time(circ_stats, hyperparams, benchmark, device):
    if device == "sc":
        tc = hyperparams.sc_T2
        time1q = hyperparams["sc_1Q_time"]
        time2q = hyperparams["sc_2Q_time"]
        fid1q = hyperparams["sc_1Q_fidelity"]
        fid2q = hyperparams["sc_2Q_fidelity"]
    elif device == "na":
        tc = hyperparams.na_T1
        time1q = hyperparams["na_1Q_time"]
        time2q = hyperparams["na_2Q_time"]
        fid1q = hyperparams["na_1Q_fidelity"]
        fid2q = hyperparams["na_2Q_fidelity"]
    elif device == "baker":
        tc = hyperparams.na_T1
        time1q = hyperparams["na_1Q_time"]
        time2q = hyperparams["na_2Q_time"]
        fid1q = hyperparams["na_1Q_fidelity"]
        fid2q = hyperparams["na_2Q_fidelity_long_range"]
    else:
        raise ValueError('invalid device, use "na" or "sc" or "baker"')
    n_2q_gates = circ_stats["n_2q_gate"]
    n_2q_layer = circ_stats["n_2q_layer"]
    n_1q_gates = circ_stats["n_1q_gate"]
    n_1q_layer = circ_stats["n_1q_layer"]
    n_qubits = circ_stats["n_qubits"]

    time = {}
    time["1q_gate"] = n_1q_layer * time1q
    time["2q_gate"] = n_2q_layer * time2q
    time["total_time"] = time["1q_gate"] + time["2q_gate"]

    fidelity = {}
    total_fid1q = fid1q**n_1q_gates
    total_fid2q = fid2q**n_2q_gates

    decoherence = float(np.exp(-time["total_time"] * n_qubits / tc))

    total_fidelity = total_fid1q * total_fid2q * decoherence
    fidelity["1q_gate"] = total_fid1q
    fidelity["2q_gate"] = total_fid2q
    fidelity["decoherence"] = decoherence
    fidelity["total_fidelity"] = total_fidelity

    return fidelity, time


def gen_res_dict(hyperparams, benchmark, compiled_circuit, device):
    res_dict = {}
    res_dict["path"] = benchmark.path
    res_dict["circ_stats"] = {}
    try:
        res_dict["circ_stats"]["n_qubits"] = benchmark.n_qubits
    except BaseException:
        pass

    res_dict["circ_stats"].update(count_layer(compiled_circuit))
    fid, time = get_fid_and_time(
        res_dict["circ_stats"], hyperparams, benchmark, device=device
    )
    res_dict["fidelity"] = fid
    res_dict["time"] = time
    benchmark_keys = ["i", "p", "keep_length", "type"]
    benchmark_dict = benchmark.__dict__.copy()
    hyperparams_dict = hyperparams.__dict__.copy()

    res_dict.update(
        {key: benchmark_dict[key] for key in benchmark_keys if key in benchmark_dict}
    )

    sc_keys = [
        "sc_T1",
        "sc_T2",
        "sc_1Q_time",
        "sc_2Q_time",
        "sc_1Q_fidelity",
        "sc_2Q_fidelity",
        "sc_simultaneous_1q_gate",
    ]

    if device == "sc":
        res_dict.update(
            {key: hyperparams_dict[key] for key in sc_keys if key in hyperparams_dict}
        )
    elif device == "na":
        res_dict.update(
            {
                key: hyperparams_dict[key]
                for key in hyperparams_dict
                if key not in sc_keys
            }
        )

    return res_dict


def move_qubit_to_line(my_list, hyperparams, ancilla_abs_pos_x, ancilla_abs_pos_y):
    # my_list=[(new_abs_position: Position, ancilla_rel Position)...]
    #! it is compiler's responsibility to make sure the ancilla movement are compatible, i.e. same row move together
    #! logger will output the distance (in unit of atoms)
    #! logger will calculate delta_N and pass to analyzer
    #! when delta_N is too large, execute cooling
    def max_list_int(my_list, my_int):
        return np.where(
            my_list < my_int,
            my_int,
            my_list,
        )

    def min_list_int(my_list, my_int):
        return np.where(
            my_list > my_int,
            my_int,
            my_list,
        )

    if type(hyperparams["n_cols"]) == int:
        n_X = hyperparams["n_cols"]
    else:
        n_X = max(hyperparams["n_cols"])
    if type(hyperparams["n_rows"]) == int:
        n_Y = hyperparams["n_rows"]
    else:
        n_Y = max(hyperparams["n_rows"])
    n_aods = hyperparams["n_aods"]
    min_x_list = np.zeros((n_aods, n_X))
    max_x_list = np.zeros((n_aods, n_X)) + 9999
    min_y_list = np.zeros((n_aods, n_Y))
    max_y_list = np.zeros((n_aods, n_Y)) + 9999
    for item in my_list:
        ancilla_coordinate = item[1]
        min_x_list[ancilla_coordinate.z, ancilla_coordinate.x :] = max_list_int(
            min_x_list[ancilla_coordinate.z, ancilla_coordinate.x :],
            item[0].x,
        )

        min_y_list[ancilla_coordinate.z, ancilla_coordinate.y :] = max_list_int(
            min_y_list[ancilla_coordinate.z, ancilla_coordinate.y :],
            item[0].y,
        )

        max_x_list[ancilla_coordinate.z, 0 : ancilla_coordinate.x + 1] = min_list_int(
            max_x_list[ancilla_coordinate.z, 0 : ancilla_coordinate.x + 1],
            item[0].x,
        )
        max_y_list[ancilla_coordinate.z, 0 : ancilla_coordinate.y + 1] = min_list_int(
            max_y_list[ancilla_coordinate.z, 0 : ancilla_coordinate.y + 1],
            item[0].y,
        )
    # print(min_x_list)
    # print(max_x_list)
    # print("+++")
    # print(min_y_list)
    # print(max_y_list)

    for aod_idx in range(n_aods):
        for i in range(n_X):
            min_x = min_x_list[aod_idx][i]
            max_x = max_x_list[aod_idx][i]
            orig_x = ancilla_abs_pos_x[aod_idx][i]
            if orig_x <= min_x:
                new_x = min_x
            elif orig_x >= max_x:
                new_x = max_x
            else:
                new_x = orig_x
            ancilla_abs_pos_x[aod_idx][i] = new_x
        for i in range(n_Y):
            min_y = min_y_list[aod_idx][i]
            max_y = max_y_list[aod_idx][i]
            orig_y = ancilla_abs_pos_y[aod_idx][i]
            if orig_y <= min_y:
                new_y = min_y
            elif orig_y >= max_y:
                new_y = max_y
            else:
                new_y = orig_y
            ancilla_abs_pos_y[aod_idx][i] = new_y
    return ancilla_abs_pos_x, ancilla_abs_pos_y
