from enola.router.router_mis import route_qubit_mis
from enola.router.codegen import CodeGen
import time


# @profile
def route_qubit(n_x: int, n_y: int, n_q: int, list_full_gates: list, qubit_mapping: list, routing_strategy: str, \
                reverse_to_initial: bool, move_back_to_initial_layout_after_circuit_execution: bool, l2: bool, use_window: bool):
    """
    generate rearrangement layers between two Rydberg layers
    """
    program_list = []
    final_mapping = qubit_mapping
    time_mis = 0
    time_codeGen = 0
    time_placement = 0
    list_qubit_move_distance = []
    list_gate_distance = []
    for index_list_gate in range(len(list_full_gates)):
        # extract sets of movement that can be perform simultaneously
        data = []
        # t_sc = t_s
        t_s = time.time()
        data, final_mapping, time_placement_tmp, tmp_qubit_move_distance, tmp_gate_distance = route_qubit_mis((n_x, n_y), n_q, index_list_gate, list_full_gates, list(final_mapping), routing_strategy, reverse_to_initial, move_back_to_initial_layout_after_circuit_execution, l2, use_window)
        time_mis += (time.time() - t_s - time_placement_tmp)
        # list_qubit_move_distance += tmp_qubit_move_distance
        # list_qubit_move_distance.append(tmp_qubit_move_distance)
        # list_gate_distance.append(tmp_gate_distance)
        time_placement += time_placement_tmp
        data['n_x'] = n_x
        data['n_y'] = n_y
        data['n_r'] = n_y
        data['n_c'] = n_x
        # for i,d in enumerate(data["layers"]):
        # print("#layers: {}".format(len(data["layers"])))
        t_s = time.time()
        codegen = CodeGen(data)
        # print("time-CodeGen_init: {}".format(time.time() - t_s))
        # t_s = time.time()
        program = codegen.builder(no_transfer=False)
        # print("time-CodeGen_builder: {}".format(time.time() - t_s))
        # t_s = time.time()
        # program_list_partial = []
        tmp = program.emit_full()
        if index_list_gate == 0:
            program_list += tmp
        else:
            program_list += tmp[2:]
        time_codeGen += (time.time() - t_s)
        # print("time-CodeGen_emit_full: {}".format(time.time() - t_s))
        # t_s = time.time()
        # tmp_partial = program.emit()
        # gate_count = 0
        # list_gate_code = []
        # for inst in tmp:
        # # for inst, inst_partial in zip(tmp, tmp_partial):
        #     if inst["type"]== "Rydberg":
        #         gate_count += len(inst["gates"])
        #         if gate_count == len(list_gates):
        #             # insert Ryberg here
        #             inst["gates"] += list_gate_code
        #             gate_count += 1
        #             program_list.append(inst)
        #         else:
        #             list_gate_code += inst["gates"]
        #         # program_list_partial.append(inst_partial)
        #     else:
        #         program_list.append(inst)
                # program_list_partial.append(inst_partial)
        # print("time-postprocessing codeGen: {}".format(time.time() - t_s))
        # t_s = time.time()
        # print("[INFO] Enola: solve one slice. mis time={:2f}, codeGen time={:2f}".format(time_mis, time_codeGen))
    return program_list, time_mis, time_codeGen, time_placement, list_qubit_move_distance, list_gate_distance
        
        # TODO: for each set of movement, collect the qubits that can be transfered simultaneously