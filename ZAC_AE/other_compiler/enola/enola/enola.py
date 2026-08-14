from enola.scheduler.gate_scheduler import gate_scheduling
from enola.placer.placer import place_qubit
from enola.router.router import route_qubit
from typing import Sequence
import time
import json
import math

class Enola:
    """class to solve QLS problem."""

    def __init__(self, name: str, dir: str = None, print_detail: bool = False, trivial_layout = False, routing_strategy = "mis",\
                to_verify: bool = False, reverse_to_initial:bool = False, move_back_to_initial_layout_after_circuit_execution: bool = False,\
                initial_mapping: list = None, dependency: bool = False, l2: bool = False, use_window: bool = False):
        self.dir = dir
        self.n_q = 0 # number of qubits
        self.n_g = 0 # number of gates
        self.n_t = 1 # number of Rydberg stage
        self.n_x = 0 # dim for arch
        self.n_y = 0 # dim for arch
        self.n_c = 0 # dim for arch
        self.n_r = 0 # dim for arch
        self.print_detail = print_detail
        self.all_commutable = False
        self.result_json = {}
        self.result_json['name'] = name
        self.result_json['layers'] = []
        self.non_front_g_q = []
        self.non_front_g_s = []
        self.non_front_g_i = []
        self.row_per_site = 1
        self.dict_gate_index = {}
        self.to_verify = to_verify
        self.trivial_layout = trivial_layout
        self.routing_strategy = routing_strategy
        self.reverse_to_initial = reverse_to_initial
        self.move_back_to_initial_layout_after_circuit_execution = move_back_to_initial_layout_after_circuit_execution
        self.given_initial_mapping = initial_mapping
        self.has_dependency = dependency
        self.l2 = l2
        self.use_window = use_window

    def setArchitecture(self, bounds: Sequence[int]):
        # bounds = [number of X, number of Y, number of C, number of R]
        self.n_x, self.n_y, self.n_c, self.n_r = bounds

    def setProgram(self, program: Sequence[Sequence[int]], nqubit: int = None):
        # assume program is a iterable of pairs of qubits in 2Q gate
        # assume that the qubit indices used are consecutively 0, 1, ...
        self.n_g = len(program)
        self.g_i = [i for i in range(self.n_g)]
        self.g_q = [(min(pair), max(pair)) for pair in program]
        self.g_s = tuple(['CRZ' for _ in range(self.n_g)])
        if not nqubit:
            for gate in program:
                self.n_q = max(gate[0], self.n_q)
                self.n_q = max(gate[1], self.n_q)
            self.n_q += 1
        else:
            self.n_q = nqubit

    # def writeSettingJson(self):
    #     self.result_json['n_t'] = self.n_t
    #     self.result_json['n_q'] = self.n_q
    #     self.result_json['all_commutable'] = self.all_commutable
    #     self.result_json['n_c'] = self.n_c
    #     self.result_json['n_r'] = self.n_r
    #     self.result_json['n_x'] = self.n_x
    #     self.result_json['n_y'] = self.n_y
    #     self.result_json['row_per_site'] = self.row_per_site
    #     self.result_json['n_g'] = self.n_g
    #     self.result_json['g_q'] = self.g_q
    #     self.result_json['g_s'] = self.g_s

    def solve(self, save_file: bool = True):
        runtime_analysis = {}
        print("[INFO] Enola: Start Solving")
        if self.n_q > self.n_x * self.n_y:
            print("[Error] #qubits > #sites. There may be a problem.")
        # self.writeSettingJson()
        t_s = time.time()
        # gate scheduling with graph coloring
        print("[INFO] Enola: Run scheduling")
        if self.has_dependency:
            result_scheduling = self.asap()
        else:
            result_scheduling = gate_scheduling(self.n_q, self.g_q)
        if self.to_verify:
            self.verify_scheduling(result_scheduling)
        # print("result_scheduling")
        # print(result_scheduling)
        list_gates = []
        for gates in result_scheduling:
            tmp = [self.g_q[i] for i in gates]
            list_gates.append(tmp)
        runtime_analysis["scheduling"] = time.time()- t_s
        print("[INFO] Time for scheduling: {}s".format(runtime_analysis["scheduling"]))

        t_p = time.time()
        if self.given_initial_mapping is not None:
            qubit_mapping = self.given_initial_mapping
        else:
            # qubit placement for layout
            qubit_mapping = []
            if self.trivial_layout:
                length = self.n_x
                print(length)
                x = 0
                y = 0
                for i in range(self.n_q):
                    qubit_mapping.append((x, y))
                    x += 1
                    if x % length == 0:
                        x = 0
                        y += 1
                # return
            else:
                qubit_mapping = place_qubit((self.n_x, self.n_y), self.n_q, list_gates, self.l2)
        list_qate_distance = []
        # tmp_list_gate_distance = [[ math.dist(qubit_mapping[gate[0]], qubit_mapping[gate[1]]) for gate in tmp_list_gate] for tmp_list_gate in list_gates]
        # list_qate_distance.append(tmp_list_gate_distance)
        runtime_analysis["placement"] = time.time()- t_p
        print("[INFO] Time for placement: {}s".format(runtime_analysis["placement"]))
        # print("qubit_mapping")
        # print(qubit_mapping)
        # print("qubit_mapping")
        # print(qubit_mapping)
        if self.to_verify:
            self.verify_qubit_mapping(qubit_mapping)
        # qubit movement between layers
        program_list, time_mis, time_codeGen, time_placement, list_qubit_move_distance, tmp_list_gate_distance = route_qubit(self.n_x, self.n_y, self.n_q, list_gates, qubit_mapping, self.routing_strategy, self.reverse_to_initial, self.move_back_to_initial_layout_after_circuit_execution, self.l2, self.use_window)
        # list_qate_distance += tmp_list_gate_distance
        # import pickle
        # if self.l2:
        #     filename1 = f"{self.result_json['name']}_movement_l2_10000.pkl"
        #     filename2 = f"{self.result_json['name']}_gate_distance_l2_10000.pkl"
        # else:
        #     filename1 = f"{self.result_json['name']}_movement_l1.pkl"
        #     filename2 = f"{self.result_json['name']}_gate_distance_l1.pkl"
        # with open(filename1, "wb") as fp:   #Pickling
        #     pickle.dump(list_qubit_move_distance, fp)
        # with open(filename2, "wb") as fp:   #Pickling
        #     pickle.dump(list_qate_distance, fp)
        runtime_analysis["routing"] = time_mis
        runtime_analysis["codegen"] = time_codeGen
        runtime_analysis["placement"] = runtime_analysis["placement"] + time_placement
        runtime_analysis["total"] = time.time()- t_s
        print("[INFO] Time for routing: {}s".format(runtime_analysis["routing"]))
        print("[INFO] Toal Time: {}s".format(runtime_analysis["total"]))
        if save_file:
            if not self.dir:
                self.dir = "./test/code/"
            with open(self.dir + f"{self.result_json['name']}_code_full.json", 'w') as f:
                json.dump(program_list, f)
        if not self.dir:
            self.dir = "./test/time/"
        with open(self.dir + f"time/{self.result_json['name']}_time.json", 'w') as f:
            json.dump(runtime_analysis, f)
        return program_list
    
    def asap(self):
        # as soon as possible algorithm for self.g_q
        list_scheduling = []
        list_qubit_time = [0 for i in range(self.n_q)]
        for i, gate in enumerate(self.g_q):
            tq0 = list_qubit_time[gate[0]]
            tq1 = list_qubit_time[gate[1]]
            tg = max(tq0, tq1)
            if tg >= len(list_scheduling):
                list_scheduling.append([])
            list_scheduling[tg].append(i)

            tg += 1
            list_qubit_time[gate[0]] = tg
            list_qubit_time[gate[1]] = tg
        return list_scheduling
    def verify_scheduling(self, result_scheduling: list):
        time_gate_scheduled = [-1 for i in range(len(self.g_q))]
        success = True
        for i, stage in enumerate(result_scheduling):
            qubit_gate = [-1 for i in range(self.n_q)]
            for gate in stage:
                if time_gate_scheduled[gate] > -1:
                    success = False
                    print("[Error] Gate {} is already scheduled in stage {}, but is assigned to stage {} again.".format(gate, time_gate_scheduled[gate], i))
                time_gate_scheduled[gate] = i
                q0 = self.g_q[gate][0]
                q1 = self.g_q[gate][1]
                if qubit_gate[q0] > -1:
                    success = False
                    print("[Error] Qubit {} is already used in gate {}, but is involved in gate {} at the same stage ({}) again.".format(q0, qubit_gate[q0], gate, i))
                if qubit_gate[q1] > -1:
                    success = False
                    print("[Error] Qubit {} is already used in gate {}, but is involved in gate {} at the same stage ({}) again.".format(q1, qubit_gate[q1], gate, i))
        
        for i, t in enumerate(time_gate_scheduled):
            if t == -1:
                success = False
                print("[Error] Gate {} is not scheduled.".format(i))
        if success:
            print(f"[INFO] Gate Scheduling Verification: Pass.")

    def verify_qubit_mapping(self, result_mapping: list):
        success = True
        physical_qubit_idx = [[-1 for j in range(self.n_y)] for i in range(self.n_x)]
        for i, location in enumerate(result_mapping):
            if location[0] >= self.n_x or location[0] < 0:
                print("[Error] Qubit {} is mapped outside the chip ({},{}).".format(i, location[0], location[1]))
                success = False
            if location[1] >= self.n_y or location[1] < 0:
                print("[Error] Qubit {} is mapped outside the chip ({},{}).".format(i, location[0], location[1]))
                success = False
            if physical_qubit_idx[location[0]][location[1]] > -1:
                print("[Error] Qubit {} is overlapped with qubit {} at ({},{}).".format(i, physical_qubit_idx[location[0]][location[1]], location[0], location[1]))
            physical_qubit_idx[location[0]][location[1]] = i
        if len(result_mapping) != self.n_q:
            print("[Error] Not all qubits are mapped: len(result_mapping)= {}, #qubit={}.".format(len(result_mapping), self.n_q))
            success = False
        if success:
            print(f"[INFO] Qubit Placement Verification: Pass.")
    
    def verify_code(self, program_list: list):
        raise NotImplementedError
