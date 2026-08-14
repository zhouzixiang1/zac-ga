import json
import pickle
import random
import string
from copy import deepcopy

import numpy as np
import scipy as sp

from utils import Position
import math

class Analyzer:
    def __init__(self, log, hyperparams, benchmark):
        self.log = log
        self.benchmark = benchmark
        self.hyperparams = hyperparams
        self.x_zpf = self.hyperparams["x_zpf"]
        self.omega_0 = self.hyperparams["omega_0"]
        # self.move_speed = self.hyperparams["move_speed"]
        self.T_per_move = self.hyperparams["T_per_move"]
        self.atom_distance = self.hyperparams["atom_distance"]
        self.error_2q_per_deltaN = self.hyperparams["two_q_error_delta_N"]
        self.T1 = self.hyperparams["na_T1"]
        self.fidelity_2q = self.hyperparams["na_2Q_fidelity"]
        self.fidelity_1q = self.hyperparams["na_1Q_fidelity"]
        self.time_2q = self.hyperparams["na_2Q_time"]
        self.time_1q = self.hyperparams["na_1Q_time"]
        self.fidelity_transfer = 1 - self.hyperparams["atom_loss_prob"]
        self.time_transfer = self.hyperparams["atom_transfer_time"]
        self.N_max = self.hyperparams["na_N_max"]
        self.n_aods = self.hyperparams["n_aods"]
        self.n_Y = self.hyperparams["n_rows"]
        self.n_X = self.hyperparams["n_cols"]
        # is self.n_Y is type int
        if type(self.n_Y) == int:
            self.n_Y = [self.n_Y] * (self.n_aods + 1)
        if type(self.n_X) == int:
            self.n_X = [self.n_X] * (self.n_aods + 1)
        self.ancilla_occupation = {}
        self.delta_N = np.zeros((self.n_aods, max(self.n_X), max(self.n_Y)))
        self.cooling_deltaN_thres = self.hyperparams["cooling_deltaN_thres"]

        self.aod_trajectory = []
        self.aod_move_distance_detail = []

        self.ancilla_abs_pos_x = np.zeros((self.n_aods, max(self.n_X)))
        self.ancilla_abs_pos_y = np.zeros((self.n_aods, max(self.n_Y)))

        self.move_speed_sum = 0
        self.circ_stats = {}
        #! update circ_stats

        self.circ_stats["depth"] = 0
        self.circ_stats["n_2q_gate"] = 0
        self.circ_stats["n_1q_gate"] = 0
        self.circ_stats["n_transfer"] = 0
        self.circ_stats["n_move"] = 0
        self.circ_stats["n_cooling"] = 0
        self.circ_stats["compilation_time"] = log["compilation_time"]
        self.circ_stats["max_ancilla"] = log["prop"]["max_ancilla"]
        self.circ_stats["avg_move_speed"] = 0

        self.fidelity_result = {}
        self.fidelity_result["2q_gate"] = 1
        self.fidelity_result["1q_gate"] = 1
        self.fidelity_result["2q_decoherence"] = 1
        self.fidelity_result["1q_decoherence"] = 1
        self.fidelity_result["additional_2Q_error_by_heating"] = 1
        self.fidelity_result["additional_2Q_by_cooling"] = 1
        self.fidelity_result["movement_decoherence"] = 1
        self.fidelity_result["movement_atomloss"] = 1
        self.fidelity_result["transfer"] = 1
        self.fidelity_result["transfer_decoherence"] = 1

        self.time_result = {}
        self.time_result["2q_gate"] = 0
        self.time_result["1q_gate"] = 0
        self.time_result["movement"] = 0
        self.time_result["transfer"] = 0

        self.all_distances = []
        if hyperparams["log_path"] is not None:
            pickle.dump(log, open(hyperparams["log_path"], "wb"))

    def analyze(self):
        # prop = self.basic_info()

        # self.n_transfer = prop.get("transfer", 0)
        results = self.calc_fidelity_and_time()

        params_keys = [
            "atom_distance",
            "na_1Q_fidelity",
            "na_2Q_fidelity",
            "n_rows",
            "n_cols",
            "n_aods",
            "T_per_move",
            "atom_distance",
            "na_N_max",
            "cooling_deltaN_thres",
            "na_1Q_fidelity",
            "na_2Q_fidelity",
            "flying_search_method",
            "flying_square_mapping",
            "flying_QSIM_max_ancilla",
            "flying_1gate_per_stage_for_QAOA",
        ]
        if "others" in self.log:
            others = self.log["others"]
        else:
            others = {}
        self.report = {
            "time": self.time_result,
            "fidelity": self.fidelity_result,
            "circ_stats": self.circ_stats,
            "params": {key: self.hyperparams[key] for key in params_keys},
            "benchmark": self.benchmark.path,
            "others": others,
        }

        if self.hyperparams["dump_full_report"]:
            self.report_full = deepcopy(self.report)
            self.report_full["aod_trajectory"] = self.aod_trajectory
            self.report_full["aod_move_distance_detail"] = self.aod_move_distance_detail
            fingerprint = "".join(
                random.choices(string.ascii_uppercase + string.digits, k=10)
            )

            pickle.dump(
                self.report_full,
                open(
                    f"{self.hyperparams.configs['result_path']}/full_report_{fingerprint}.pkl",
                    "wb",
                ),
            )

            self.report["full_report_path"] = (
                f"{self.hyperparams.configs['result_path']}/full_report_{fingerprint}.pkl"
            )

        return results

    def update_delta_N(self, code):
        if code["type"] == "move":
            self.aod_trajectory.append({})
            self.aod_move_distance_detail.append({})
            ancilla_abs_pos_x_new = np.array(code["data"]["ancilla_abs_pos_x"])
            ancilla_abs_pos_y_new = np.array(code["data"]["ancilla_abs_pos_y"])

            for key in self.ancilla_occupation:
                self.aod_trajectory[-1][key] = Position(
                    self.ancilla_abs_pos_x[key.z][key.x],
                    self.ancilla_abs_pos_y[key.z][key.y],
                    key.z,
                )
                x = key.x
                y = key.y
                z = key.z
                distance = np.sqrt(
                    (self.ancilla_abs_pos_x[z][x] - ancilla_abs_pos_x_new[z][x]) ** 2
                    + (self.ancilla_abs_pos_y[z][y] - ancilla_abs_pos_y_new[z][y]) ** 2
                )
                self.all_distances.append(distance)
                self.aod_move_distance_detail[-1][key] = distance
                self.delta_N[z][x][y] += (
                    0.5
                    * (
                        6
                        * (distance * self.atom_distance)
                        / self.x_zpf
                        / (self.omega_0**2)
                        / (self.T_per_move**2)
                    )
                    ** 2
                )
                self.move_speed_sum += (
                    distance
                    * self.atom_distance
                    / self.T_per_move
                    / len(self.ancilla_occupation)
                )
            self.ancilla_abs_pos_x = ancilla_abs_pos_x_new
            self.ancilla_abs_pos_y = ancilla_abs_pos_y_new
        if np.max(self.delta_N) > self.cooling_deltaN_thres:
            return True
        else:
            return False  # False for no need of cooling

    # def calc_move_time(self, distance):
    # return distance * self.atom_distance / self.move_speed

    # def calc_fidelity_and_time(self):
    #     n_qubit_slm = len(self.log["prop"]["slm_rel_pos"])
    #     for code_idx, code in enumerate(self.log["code"]):
    #         if code["type"] == "move":
    #             need_cooling = self.update_delta_N(code)
    #             self.time_result["movement"] += self.T_per_move
    #             self.circ_stats["n_move"] += 1
    #             self.fidelity_result["movement_atomloss"] *= np.prod(
    #                 (
    #                     0.5
    #                     + 0.5
    #                     * sp.special.erf(
    #                         (self.N_max - self.delta_N)
    #                         / np.sqrt(1e-8 + 2 * self.delta_N)
    #                     )
    #                 )
    #             )
    #             self.fidelity_result["movement_decoherence"] *= float(
    #                 np.exp(
    #                     -(self.T_per_move)
    #                     * (n_qubit_slm + len(self.ancilla_occupation))
    #                     / self.T1
    #                 )
    #             )
    #             if need_cooling:
    #                 self.circ_stats["n_cooling"] += 1
    #                 n_ancilla_using = len(self.ancilla_occupation)
    #                 self.fidelity_result["additional_2Q_by_cooling"] *= (
    #                     self.fidelity_2q
    #                 ) ** (n_ancilla_using * 2)
    #                 self.delta_N = np.zeros((self.n_aods, max(self.n_X), max(self.n_Y)))
    #         elif code["type"] == "prepare":
    #             for item in code["data"]:
    #                 self.ancilla_occupation[item["ancilla_rel_pos"]] = item[
    #                     "slm_rel_pos"
    #                 ]
    #         elif code["type"] == "destroy":
    #             for item in code["data"]:
    #                 del self.ancilla_occupation[item["ancilla_rel_pos"]]
    #                 self.delta_N[item["ancilla_rel_pos"].z][item["ancilla_rel_pos"].x][
    #                     item["ancilla_rel_pos"].y
    #                 ] = 0
    #         elif code["type"] == "gate_2Q":
    #             n_ancilla_using = len(code["data"])
    #             self.circ_stats["depth"] += 1 + code["additional_stage"]
    #             self.circ_stats["n_2q_gate"] += n_ancilla_using
    #             self.fidelity_result["2q_gate"] *= (self.fidelity_2q) ** n_ancilla_using
    #             for pair in code["data"]:
    #                 source = pair["source"]
    #                 ancilla = pair["ancilla"]
    #                 if source.z == -1:
    #                     self.fidelity_result["additional_2Q_error_by_heating"] *= (
    #                         1
    #                         - self.delta_N[ancilla.z, ancilla.x, ancilla.y]
    #                         * self.error_2q_per_deltaN
    #                     )
    #                 elif ancilla.z == -1:
    #                     self.fidelity_result["additional_2Q_error_by_heating"] *= (
    #                         1
    #                         - self.delta_N[source.z, source.x, source.y]
    #                         * self.error_2q_per_deltaN
    #                     )
    #                 else:
    #                     self.fidelity_result["additional_2Q_error_by_heating"] *= (
    #                         1
    #                         - (
    #                             self.delta_N[source.z, source.x, source.y]
    #                             + self.delta_N[ancilla.z, ancilla.x, ancilla.y]
    #                         )
    #                         * self.error_2q_per_deltaN
    #                     )

    #             self.time_result["2q_gate"] += self.time_2q * (
    #                 1 + code["additional_stage"]
    #             )
    #             self.fidelity_result["2q_decoherence"] *= np.exp(
    #                 -self.time_2q
    #                 * (1 + code["additional_stage"])
    #                 * (n_qubit_slm + len(self.ancilla_occupation))
    #                 / self.T1
    #             )
    #         elif code["type"] == "gate_1Q":
    #             n_slm_used = len(code["data"])
    #             self.circ_stats["n_1q_gate"] += n_slm_used
    #             self.fidelity_result["1q_gate"] *= self.fidelity_1q**n_slm_used
    #             self.time_result["1q_gate"] += self.time_1q
    #             self.fidelity_result["1q_decoherence"] *= np.exp(
    #                 -self.time_1q
    #                 * (n_qubit_slm + len(self.ancilla_occupation))
    #                 / self.T1
    #             )

    #         elif code["type"] == "transfer":
    #             self.circ_stats["n_transfer"] += 1
    #             n_transferred_qubit = len(code["data"])
    #             self.fidelity_result["transfer"] *= (
    #                 self.fidelity_transfer**n_transferred_qubit
    #             )
    #             self.time_result["transfer"] += self.time_transfer
    #             self.fidelity_result["transfer_decoherence"] *= np.exp(
    #                 -self.time_transfer
    #                 * (n_qubit_slm + len(self.ancilla_occupation))
    #                 / self.T1
    #             )

    #     self.time_result["total_time"] = (
    #         self.time_result["2q_gate"]
    #         + self.time_result["1q_gate"]
    #         + self.time_result["movement"]
    #         + self.time_result["transfer"]
    #     )

    #     self.fidelity_result["total_fidelity"] = (
    #         self.fidelity_result["1q_gate"]
    #         * self.fidelity_result["1q_decoherence"]
    #         * self.fidelity_result["2q_gate"]
    #         * self.fidelity_result["2q_decoherence"]
    #         * self.fidelity_result["additional_2Q_by_cooling"]
    #         * self.fidelity_result["additional_2Q_error_by_heating"]
    #         * self.fidelity_result["movement_decoherence"]
    #         * self.fidelity_result["movement_atomloss"]
    #         * self.fidelity_result["transfer"]
    #         * self.fidelity_result["transfer_decoherence"]
    #     )
    #     self.fidelity_result["1q_total"] = (
    #         self.fidelity_result["1q_gate"] * self.fidelity_result["1q_decoherence"]
    #     )
    #     self.fidelity_result["2q_total"] = (
    #         self.fidelity_result["2q_gate"] * self.fidelity_result["2q_decoherence"]
    #     )
    #     self.fidelity_result["move_total"] = (
    #         self.fidelity_result["movement_decoherence"]
    #         * self.fidelity_result["movement_atomloss"]
    #         * self.fidelity_result["additional_2Q_by_cooling"]
    #         * self.fidelity_result["additional_2Q_error_by_heating"]
    #     )
    #     self.fidelity_result["transfer_total"] = (
    #         self.fidelity_result["transfer"]
    #         * self.fidelity_result["transfer_decoherence"]
    #     )

    #     self.circ_stats["avg_move_speed"] = float(
    #         self.move_speed_sum / (1e-8 + self.circ_stats["n_move"])
    #     )

    #     self.circ_stats["avg_move_distance"] = float(
    #         self.circ_stats["avg_move_speed"]
    #         * self.T_per_move
    #         * self.circ_stats["n_move"]
    #     )

    #     self.circ_stats["each_move_distance_mean"] = float(np.mean(self.all_distances))
    #     self.circ_stats["each_move_distance_std"] = float(np.std(self.all_distances))

    #     self.circ_stats["n_2q_layer"] = self.circ_stats["depth"]

    #     # print(self.all_distances)

    #     for k, v in self.fidelity_result.items():
    #         self.fidelity_result[k] = float(v)

    #     for k, v in self.time_result.items():
    #         self.time_result[k] = float(v)

    def calc_fidelity_and_time(self):
        self.fidelity_2q_gate = 0.995
        self.fidelity_2q_gate_for_idle = 1 - (1-self.fidelity_2q_gate)/2
        self.fidelity_1q_gate = 0.9997
        self.fidelity_atom_transfer = 0.999
        self.time_coherence = 1.5e6 # us
        self.time_atom_transfer = 15 # us
        self.time_rydberg = 0.36 # us
        self.time_1q_gate = 52 # 0.625 # us

        self.entanglement_zone = set()
        
        self.n_qubit = self.log["prop"]["n_qubits"]
        self.list_instrcution = []

        # data members for fidelity computation
        self.cir_fidelity = 1
        self.cir_fidelity_2q_gate = 1
        self.cir_fidelity_2q_gate_for_idle = 1
        self.cir_fidelity_1q_gate = 1
        self.cir_fidelity_atom_transfer = 1
        self.cir_fidelity_coherence = 1
        self.circuit_duration = 0

        self.cir_qubit_busy_time = dict()
        sepration = 10
        a = 0.00275
        for q in self.log["prop"]["slm_rel_pos"]:
            self.cir_qubit_busy_time[q.to_pair()] = 0 

        aod_current_location_x = [[j for j in range(self.n_X[i])] for i in range(self.n_aods) ]
        aod_current_location_y = [[j for j in range(self.n_Y[i])] for i in range(self.n_aods) ]
        for code_idx, code in enumerate(self.log["code"]):
            if code["type"] == "move":
                # todo
                # calculate new location
                aod_new_location_x = code['data']['ancilla_abs_pos_x']
                aod_new_location_y = code['data']['ancilla_abs_pos_y']
                max_dis = 0
                for aod_idx in range(self.n_aods):
                    for old_x, old_y, new_x, new_y in zip(aod_current_location_x[aod_idx], aod_current_location_y[aod_idx], \
                                                          aod_new_location_x[aod_idx], aod_new_location_y[aod_idx]):
                        max_dis = max(max_dis, math.dist((old_x * 10, old_y * 10), (new_x * 10, new_y * 10)))
                    d = max_dis
                    t = math.sqrt(d/a)
                    self.circuit_duration += t
                
            elif code["type"] == "prepare":
                for item in code["data"]:
                    self.ancilla_occupation[item["ancilla_rel_pos"]] = item[
                        "slm_rel_pos"
                    ]
                    self.cir_qubit_busy_time[item["ancilla_rel_pos"].to_pair()] = 0 
                    self.n_qubit = len(self.cir_qubit_busy_time)
            elif code["type"] == "destroy":
                assert(0)
            elif code["type"] == "gate_2Q":
                n_ancilla_using = len(code["data"])
                self.cir_fidelity_2q_gate *= pow(self.fidelity_2q_gate, n_ancilla_using)
                self.cir_fidelity_2q_gate_for_idle *= pow(self.fidelity_2q_gate_for_idle, self.n_qubit - 2*n_ancilla_using)
                for gate in code["data"]:
                    self.cir_qubit_busy_time[gate["source"].to_pair()] += self.time_rydberg
                    self.cir_qubit_busy_time[gate["ancilla"].to_pair()] += self.time_rydberg
                self.circuit_duration += self.time_rydberg
                
            elif code["type"] == "gate_1Q":
                n_slm_used = len(code["data"])
                self.cir_fidelity_1q_gate *= pow(self.fidelity_1q_gate, n_slm_used)
                for gate in code["data"]:
                    self.cir_qubit_busy_time[gate["source"].to_pair()] += self.time_1q_gate
                self.circuit_duration += (self.time_1q_gate * n_slm_used)

            elif code["type"] == "transfer":
                assert(0)
            
        
        for q in self.cir_qubit_busy_time:
            idle_t = self.circuit_duration - self.cir_qubit_busy_time[q]
            self.cir_fidelity_coherence *= (1 - idle_t/self.time_coherence)
        self.cir_fidelity = self.cir_fidelity_1q_gate * self.cir_fidelity_2q_gate * self.cir_fidelity_2q_gate_for_idle \
                            * self.cir_fidelity_atom_transfer * self.cir_fidelity_coherence
        results = { "cir_fidelity" : self.cir_fidelity,
                    "cir_fidelity_1q_gate": self.cir_fidelity_1q_gate,
                    "cir_fidelity_2q_gate": self.cir_fidelity_2q_gate,
                    "cir_fidelity_2q_gate_for_idle": self.cir_fidelity_2q_gate_for_idle,
                    "cir_fidelity_atom_transfer": self.cir_fidelity_atom_transfer,
                    "cir_fidelity_coherence": self.cir_fidelity_coherence,
                    "cir_duration": self.circuit_duration}
        return results
        

        
