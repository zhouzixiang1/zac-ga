import json
import math
from copy import deepcopy
from heapq import heapify, heappop, heappush
from io import StringIO
from time import time

import numpy as np
import qiskit
from qiskit import QuantumCircuit, transpile

from utils import Position, circuit_to_dag, count_layer, move_qubit_to_line

from .gen_coupling_map import gen_fpqa_coupling_map


def get_row_col_aod_from_idx(idx, n_rows, n_cols):
    # obtain the row and column location according to the input index
    i = 0
    while True:
        if (n_rows[0:i] * n_cols[0:i]).sum() <= idx:
            i += 1
            continue
        else:
            return [
                i - 1,
                (idx - (n_rows[0 : i - 1] * n_cols[0 : i - 1]).sum()) // n_cols[i - 1],
                (idx - (n_rows[0 : i - 1] * n_cols[0 : i - 1]).sum()) % n_cols[i - 1],
            ]


def get_p2v(circ):
    # obtain the physical to virtual qubit mapping
    if getattr(circ, "_layout", None) is not None:
        try:
            p2v_orig = circ._layout.final_layout.get_physical_bits().copy()
        except:
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
    return p2v


def get_all_2q_gates(circ):
    # obtain all the 2-Q gate in the input circuit
    all_2q_gates = []
    for gate in circ.data:
        wires = list(map(lambda x: x._index, gate[1]))
        if len(wires) == 2:
            if wires[0] > wires[1]:
                all_2q_gates.append((wires[1], wires[0]))
            else:
                all_2q_gates.append((wires[0], wires[1]))
    return all_2q_gates


def get_initial_mapping_array(all_2q_gates, n_rows, n_cols, n_arrays):
    # obtain which array is the each qubit assigned to
    mappings = {}
    for k in range(n_arrays):
        mappings[k] = set()

    for gate in all_2q_gates:
        for idx in gate:
            aod_id, row, col = get_row_col_aod_from_idx(idx, n_rows, n_cols)
            mappings[aod_id].add(idx)
    return mappings


def count_2q_gate_frequency(all_2q_gates):
    # obtain the frequency of the 2Q gate, useful for qubit-atom mapper
    freq = {}
    for gate in all_2q_gates:
        if gate[0] > gate[1]:
            gate = (gate[1], gate[0])
        if gate not in freq:
            freq[gate] = 1
        else:
            freq[gate] += 1
    return freq


def count_2q_qubit_frequency(all_2q_gates):
    # obtain the number of 2Q gates on each qubit, useful for qubit-atom mapper
    freq = {}
    for gate in all_2q_gates:
        for qubit in gate:
            if qubit not in freq:
                freq[qubit] = 1
            else:
                freq[qubit] += 1
    return freq


def get_mapped_position(n_rows, n_cols):
    # obtain the mapping position of the SLM array
    all_pos = []
    current_pos = [0, 0]
    current_id = 0
    current_direction = "bottom_right"
    while current_id < n_rows * n_cols:
        all_pos.append(current_pos.copy())
        if current_direction == "bottom_right":
            if current_pos[0] == n_rows - 1:
                current_direction = "top_left"
                if current_pos[1] == n_cols - 1:
                    current_pos[0] = n_rows - 2
                else:
                    current_pos[0] = current_pos[1] - 1
                current_pos[1] = n_cols - 1
            else:
                current_pos[0] += 1
                current_pos[1] += 1
        elif current_direction == "top_left":
            if current_pos[0] == 0:
                current_direction = "bottom_right"
                current_pos[0] = current_pos[1]
                current_pos[1] = 0
            else:
                current_pos[0] -= 1
                current_pos[1] -= 1
        current_id += 1
    return all_pos


def show_tree(tree, total_width=60, fill=" "):
    """Pretty-print a tree.
    total_width depends on your input size"""
    output = StringIO()
    last_row = -1
    for i, n in enumerate(tree):
        if i:
            row = int(math.floor(math.log(i + 1, 2)))
        else:
            row = 0
        if row != last_row:
            output.write("\n")
        columns = 2**row
        col_width = int(math.floor((total_width * 1.0) / columns))
        output.write(str(n).center(col_width, fill))
        last_row = row
    # print(output.getvalue())
    # print("-" * total_width)
    return


def get_occupation_positions_sequential(
    circ, n_rows, n_cols, n_aods, original_n_rows, original_n_cols
):
    time_start = time()
    all_2q_gates = get_all_2q_gates(circ)
    initial_mapping_array = get_initial_mapping_array(
        all_2q_gates, original_n_rows, original_n_cols, n_aods + 1
    )

    final_pos_mapping = {}
    occupied_positions = np.ones((n_aods + 1, n_rows, n_cols), dtype=np.int32) * (-1)

    for array_id, atom_orig_ids in initial_mapping_array.items():
        for k, atom_orig_id in enumerate(atom_orig_ids):
            final_pos_mapping[atom_orig_id] = [array_id, k // n_cols, k % n_cols]
            occupied_positions[array_id, k // n_cols, k % n_cols] = atom_orig_id

    return occupied_positions, final_pos_mapping, time() - time_start


def get_occupation_positions_random(
    circ, n_rows, n_cols, n_aods, original_n_rows, original_n_cols
):
    time_start = time()
    all_2q_gates = get_all_2q_gates(circ)
    initial_mapping_array = get_initial_mapping_array(
        all_2q_gates, original_n_rows, original_n_cols, n_aods + 1
    )

    final_pos_mapping = {}
    occupied_positions = np.ones(
        (n_aods + 1, max(n_rows), max(n_cols)), dtype=np.int32
    ) * (-1)
    orig_pos = get_mapped_position(max(n_rows[0], n_cols[0]), max(n_rows[0], n_cols[0]))
    all_pos = []
    if not (n_rows == n_cols).all():
        for pos in orig_pos:
            if pos[0] < n_rows[0] and pos[1] < n_cols[0]:
                all_pos.append(pos)
    else:
        all_pos = orig_pos

    for array_id, atom_orig_ids in initial_mapping_array.items():
        shuffled_all_pos = np.random.permutation(all_pos)
        for k, atom_orig_id in enumerate(atom_orig_ids):
            final_pos_mapping[atom_orig_id] = [
                array_id,
                shuffled_all_pos[k][0],
                shuffled_all_pos[k][1],
            ]
            occupied_positions[
                array_id, shuffled_all_pos[k][0], shuffled_all_pos[k][1]
            ] = atom_orig_id

    return occupied_positions, final_pos_mapping, time() - time_start


def get_occupation_positions(
    circ, n_rows, n_cols, n_aods, original_n_rows, original_n_cols
):
    # obtain the position of all the AOD atoms according to the 2q gate frequency
    time_start = time()
    original_array_size = original_n_rows * original_n_cols
    all_2q_gates = get_all_2q_gates(circ)
    # print(f"number of 2q gates: {len(all_2q_gates)}")
    initial_mapping_array = get_initial_mapping_array(
        all_2q_gates, original_n_rows, original_n_cols, n_aods + 1
    )
    freq = count_2q_gate_frequency(all_2q_gates)
    freq_qubit = count_2q_qubit_frequency(all_2q_gates)

    sorted_all_qubit = sorted(
        list(freq_qubit.keys()), key=lambda x: freq_qubit[x], reverse=True
    )

    all_2q_gates_sorted = sorted(list(freq.keys()), key=lambda x: freq[x], reverse=True)
    # firstly assign the locations of SLM, or the first array
    qubit_first = list(initial_mapping_array[0])
    sorted_qubit_first = sorted(
        list(qubit_first), key=lambda x: freq_qubit[x], reverse=True
    )

    orig_pos = get_mapped_position(max(n_rows[0], n_cols[0]), max(n_rows[0], n_cols[0]))
    all_pos = []
    if not n_rows[0] == n_cols[0]:
        for pos in orig_pos:
            if pos[0] < n_rows[0] and pos[1] < n_cols[0]:
                all_pos.append(pos)
    else:
        all_pos = orig_pos

    final_pos_mapping = {}
    already_mapped_qubit = set()
    occupied_positions = np.ones(
        (n_aods + 1, max(n_rows), max(n_cols)), dtype=np.int32
    ) * (-1)

    for idx, atom in enumerate(sorted_qubit_first):
        already_mapped_qubit.add(atom)
        final_pos_mapping[atom] = [0, *all_pos[idx]]
        occupied_positions[0, all_pos[idx][0], all_pos[idx][1]] = atom
    # for r in range(n_rows):
    # for c in range(n_cols):
    # occupied_positions[0, r, c] = 1

    heap = []
    for gate in all_2q_gates_sorted:
        # use heap to get the most frequent 2Q gate
        heappush(heap, (-freq[gate], gate))

    while ((occupied_positions >= 0).sum() - len(qubit_first)) < len(
        freq_qubit.keys()
    ) - len(qubit_first):
        gate_need_push_back = []
        # then assign the positino of other arrays, this is according to the frequency of all the 2q gates, high pairs will be assign to the same location in diffent arrays
        while (
            ((occupied_positions >= 0).sum() - len(qubit_first))
            < len(freq_qubit.keys()) - len(qubit_first)
        ) and len(
            heap
        ) > 0:  # not all aod qubit are mapped yet
            gate = heappop(heap)[1]
            if (gate[0] in already_mapped_qubit) and (
                gate[1] not in already_mapped_qubit
            ):
                # map the first gate position:
                array_id = np.where(np.cumsum(n_rows * n_cols) > gate[1])[0][0]
                gate0_pos = final_pos_mapping[gate[0]]
                if (
                    occupied_positions[array_id, gate0_pos[1], gate0_pos[2]] < 0
                ):  # empty
                    # print(f"map {gate[1]} to {array_id, *gate0_pos[1:]} in situation 0")
                    final_pos_mapping[gate[1]] = [array_id, *gate0_pos[1:]]
                    already_mapped_qubit.add(gate[1])
                    occupied_positions[array_id, gate0_pos[1], gate0_pos[2]] = gate[1]
                    for gate in gate_need_push_back:
                        heappush(heap, (-freq[gate], gate))
                    gate_need_push_back = []
                    continue
                else:
                    gate_need_push_back.append(gate)

            elif (gate[0] not in already_mapped_qubit) and (
                gate[1] in already_mapped_qubit
            ):
                array_id = np.where(np.cumsum(n_rows * n_cols) > gate[0])[0][0]

                gate1_pos = final_pos_mapping[gate[1]]
                if (
                    occupied_positions[array_id, gate1_pos[1], gate1_pos[2]] < 0
                ):  # empty
                    # print(f"map {gate[0]} to {array_id, *gate0_pos[1:]} in situation 1")
                    final_pos_mapping[gate[0]] = [array_id, *gate1_pos[1:]]
                    already_mapped_qubit.add(gate[0])
                    occupied_positions[array_id, gate1_pos[1], gate1_pos[2]] = gate[0]
                    for gate in gate_need_push_back:
                        heappush(heap, (-freq[gate], gate))
                    gate_need_push_back = []
                    continue
                else:
                    gate_need_push_back.append(gate)
            elif (gate[0] in already_mapped_qubit) and (
                gate[1] in already_mapped_qubit
            ):
                # print(f"({gate[0]}, {gate[1]}) are already mapped")
                # both qubits are already mapped, nothing need to be done
                pass
            else:
                gate_need_push_back.append(gate)
                # the two qubits of the gate are not mapped yet, we skip this gate to avoid that it occupies the location of good pairs
        for gate in gate_need_push_back:
            heappush(heap, (-freq[gate], gate))
        gate_need_push_back = []

        # need to assign at least one to continue the process
        for qubit in sorted_all_qubit:
            if not qubit in already_mapped_qubit:
                array_id = np.where(np.cumsum(n_rows * n_cols) > qubit)[0][0]
                for pos in all_pos:
                    if occupied_positions[array_id, pos[0], pos[1]] < 0:  # empty
                        # print(f"map {qubit} to {array_id, *pos} in situation 2")
                        final_pos_mapping[qubit] = [array_id, *pos]
                        already_mapped_qubit.add(qubit)
                        occupied_positions[array_id, pos[0], pos[1]] = qubit
                        break
                break
    # print(occupied_positions)
    compile_time = time() - time_start
    return occupied_positions, final_pos_mapping, compile_time


def non_decreasing(L):
    if len(L) == 1:
        return True
    return all(x <= y for x, y in zip(L, L[1:]))


# def fill_grid_placements(grid_placements, n_aod):
# firstly build the reverse grid_placements
# reverse_grid_placements = {}
# for array_id_small in range(n_aod):
# for array_id_big in range(array_id_small+1, n_aod):
# reverse_grid_placements[f'{array_id_big}_to_{array_id_small}'] = {'rows':{}, 'cols': {}}
# for
# pass


def fill_grid_placement(grid_placements, n_aod):
    # firstly check if the target grid is mapped to
    pass


def find_maximun_parallel_gates(
    gates,
    pos,
    occ,
    n_aods,
    individual_addressable,
    allow_order_violation,
    allow_overlap_violation,
):
    this_overlap = 0
    n_array = n_aods + 1
    # firstly have a look a the displacement between two locations
    displacement = {}
    # grid_placements = {}
    grid_placements = {
        "rows": [],
        "cols": [],
    }  # the key is the index of second grid row / cols, the value is the index of the first grid row / cols
    # a list of sets, each set tells what row or cols should be together

    # for array_id0 in range(n_aod+1):
    #     for array_id1 in range(n_aod+1):
    #         if array_id0 == array_id1:
    #             continue
    #         grid_placements[f'{array_id0}_to_{array_id1}'] = {'rows':{}, 'cols': {}}

    gates_displacement_sorted = []
    for gate in gates:
        displacement[tuple(gate)] = abs(pos[gate[0]][1] - pos[gate[1]][1]) + abs(
            pos[gate[0]][2] - pos[gate[1]][2]
        )
    gates_displacement_sorted = sorted(gates, key=lambda x: displacement[tuple(x)])

    parallel_gate_ids = []
    parallel_gates = []
    for gate_id, gate in enumerate(
        gates_displacement_sorted
    ):  # starts to add gates according to the displacement
        # if gate == [3, 48]:
        # print("here")
        conflict = False
        array_id0, row0, col0 = pos[gate[0]]
        array_id1, row1, col1 = pos[gate[1]]

        # firstly add the current to the sets, later we will check if there is any conflict
        grid_place_now = deepcopy(grid_placements)
        for row_set in grid_place_now["rows"]:
            if f"array_{array_id0}_row_{row0}" in row_set:
                row_set.add(f"array_{array_id1}_row_{row1}")
                break
            if f"array_{array_id1}_row_{row1}" in row_set:
                row_set.add(f"array_{array_id0}_row_{row0}")
                break
        else:
            grid_place_now["rows"].append(
                {f"array_{array_id0}_row_{row0}", f"array_{array_id1}_row_{row1}"}
            )

        for col_set in grid_place_now["cols"]:
            if f"array_{array_id0}_col_{col0}" in col_set:
                col_set.add(f"array_{array_id1}_col_{col1}")
                break
            if f"array_{array_id1}_col_{col1}" in col_set:
                col_set.add(f"array_{array_id0}_col_{col0}")
                break
        else:
            grid_place_now["cols"].append(
                {f"array_{array_id0}_col_{col0}", f"array_{array_id1}_col_{col1}"}
            )

        # fistly check that in each set, the array_id are unique, no repeated array_id
        if not allow_overlap_violation:
            for row_set in grid_place_now["rows"]:

                if len(set([x.split("_")[1] for x in row_set])) != len(row_set):
                    # print(f"{gate} row conflict")
                    conflict = True
                    this_overlap += 1
                    # break
            for col_set in grid_place_now["cols"]:
                if len(set([x.split("_")[1] for x in col_set])) != len(col_set):
                    # print(f"{gate} col conflict")
                    conflict = True
                    this_overlap += 1
                    # break
        else:
            for row_set in grid_place_now["rows"]:
                if [x.split("_")[1] for x in row_set].count("0") > 1:
                    conflict = True
            for col_set in grid_place_now["cols"]:
                if [x.split("_")[1] for x in col_set].count("0") > 1:
                    conflict = True

        if conflict:
            continue
        #!
        # for array0 in range(n_array):
        #     for array1 in range(n_array):
        #         if array0 == array1:
        #             continue
        #         # check row
        #         array0_row_ids = []
        #         array1_row_ids = []

        #         for row_set in grid_place_now["rows"]:
        #             if array0 in [int(x.split("_")[1]) for x in row_set] and array1 in [
        #                 int(x.split("_")[1]) for x in row_set
        #             ]:
        #                 for element in row_set:
        #                     if element.split("_")[1] == str(array0):
        #                         array0_row_ids.append(int(element.split("_")[3]))
        #                     elif element.split("_")[1] == str(array1):
        #                         array1_row_ids.append(int(element.split("_")[3]))
        #         # check whether the two have the same order
        #         if any(np.argsort(array0_row_ids) != np.argsort(array1_row_ids)):
        #             # print(f"{gate} row order conflict")
        #             conflict = True
        #             break
        #         # check col
        #         array0_col_ids = []
        #         array1_col_ids = []

        #         for col_set in grid_place_now["cols"]:
        #             if array0 in [int(x.split("_")[1]) for x in col_set] and array1 in [
        #                 int(x.split("_")[1]) for x in col_set
        #             ]:
        #                 for element in col_set:
        #                     if element.split("_")[1] == str(array0):
        #                         array0_col_ids.append(int(element.split("_")[3]))
        #                     elif element.split("_")[1] == str(array1):
        #                         array1_col_ids.append(int(element.split("_")[3]))
        #         # check whether the two have the same order
        #         # print(len(array0_col_ids), len(array1_col_ids), flush=1)
        #         if any(np.argsort(array0_col_ids) != np.argsort(array1_col_ids)):
        #             # print(f"{gate} col order conflict")
        #             conflict = True
        #             break
        #     if conflict:
        #         break
        # if conflict:
        #     continue

        #!
        if not allow_order_violation:
            # secondly check that across the sets, the order of row and col are non-decreasing
            for array0 in range(n_array):
                for array1 in range(n_array):
                    if array0 == array1:
                        continue
                    # check row
                    array0_row_ids = []
                    array1_row_ids = []

                    for row_set in grid_place_now["rows"]:
                        if array0 in [
                            int(x.split("_")[1]) for x in row_set
                        ] and array1 in [int(x.split("_")[1]) for x in row_set]:
                            array0_row_ids.append([])
                            array1_row_ids.append([])
                            for element in row_set:
                                if element.split("_")[1] == str(array0):
                                    array0_row_ids[-1].append(
                                        int(element.split("_")[3])
                                    )
                                elif element.split("_")[1] == str(array1):
                                    array1_row_ids[-1].append(
                                        int(element.split("_")[3])
                                    )
                    # check whether the two have the same order
                    sorted_indices_0 = np.array(
                        sorted(
                            range(len(array0_row_ids)),
                            key=lambda i: min(array0_row_ids[i]),
                        )
                    )
                    sorted_indices_1 = np.array(
                        sorted(
                            range(len(array1_row_ids)),
                            key=lambda i: min(array1_row_ids[i]),
                        )
                    )
                    if any(sorted_indices_0 != sorted_indices_1):
                        conflict = True
                        break

                    for i in range(len(array0_row_ids) - 1):
                        if min(array0_row_ids[sorted_indices_0[i + 1]]) < max(
                            array0_row_ids[sorted_indices_0[i]]
                        ):
                            conflict = True
                            break
                    for i in range(len(array1_row_ids) - 1):
                        if min(array1_row_ids[sorted_indices_1[i + 1]]) < max(
                            array1_row_ids[sorted_indices_1[i]]
                        ):
                            conflict = True
                            break
                    # check col
                    array0_col_ids = []
                    array1_col_ids = []

                    for col_set in grid_place_now["cols"]:
                        if array0 in [
                            int(x.split("_")[1]) for x in col_set
                        ] and array1 in [int(x.split("_")[1]) for x in col_set]:
                            array0_col_ids.append([])
                            array1_col_ids.append([])
                            for element in col_set:
                                if element.split("_")[1] == str(array0):
                                    array0_col_ids[-1].append(
                                        int(element.split("_")[3])
                                    )
                                elif element.split("_")[1] == str(array1):
                                    array1_col_ids[-1].append(
                                        int(element.split("_")[3])
                                    )
                    # check whether the two have the same order
                    # print(len(array0_col_ids), len(array1_col_ids), flush=1)
                    sorted_indices_0 = np.array(
                        sorted(
                            range(len(array0_col_ids)),
                            key=lambda i: min(array0_col_ids[i]),
                        )
                    )
                    sorted_indices_1 = np.array(
                        sorted(
                            range(len(array1_col_ids)),
                            key=lambda i: min(array1_col_ids[i]),
                        )
                    )
                    if any(sorted_indices_0 != sorted_indices_1):
                        # print(f"{gate} col order conflict")
                        conflict = True
                        break
                    for i in range(len(array0_col_ids) - 1):
                        if min(array0_col_ids[sorted_indices_0[i + 1]]) < max(
                            array0_col_ids[sorted_indices_0[i]]
                        ):
                            conflict = True
                            break
                    for i in range(len(array1_col_ids) - 1):
                        if min(array1_col_ids[sorted_indices_1[i + 1]]) < max(
                            array1_col_ids[sorted_indices_1[i]]
                        ):
                            conflict = True
                            break
                if conflict:
                    break
        if conflict:
            continue
        if not individual_addressable:
            # thirdly check that all the vertices have gates
            for row_set in grid_place_now["rows"]:
                for col_set in grid_place_now["cols"]:
                    for a_id0 in range(n_array):
                        for a_id1 in range(n_array):
                            if a_id0 == a_id1:
                                continue
                            if (
                                a_id0 in [int(x.split("_")[1]) for x in row_set]
                                and a_id1 in [int(x.split("_")[1]) for x in row_set]
                                and a_id0 in [int(x.split("_")[1]) for x in col_set]
                                and a_id1 in [int(x.split("_")[1]) for x in col_set]
                            ):
                                # check the vertices
                                for element in row_set:
                                    if element.split("_")[1] == str(a_id0):
                                        r_id0 = int(element.split("_")[3])
                                    elif element.split("_")[1] == str(a_id1):
                                        r_id1 = int(element.split("_")[3])
                                for element in col_set:
                                    if element.split("_")[1] == str(a_id0):
                                        c_id0 = int(element.split("_")[3])
                                    elif element.split("_")[1] == str(a_id1):
                                        c_id1 = int(element.split("_")[3])
                                qubit0 = occ[a_id0, r_id0, c_id0]
                                qubit1 = occ[a_id1, r_id1, c_id1]
                                if qubit0 == -1 or qubit1 == -1:
                                    # print(f"vertix ({qubit0}, {qubit1}) contains no atom")
                                    continue
                                if ([qubit0, qubit1] in gates) or (
                                    [qubit1, qubit0] in gates
                                ):
                                    continue
                                else:
                                    # print(
                                    #     f"vertix ({qubit0}, {qubit1}) not in the gates"
                                    # )
                                    conflict = True
                                    break
                        if conflict:
                            break
                    if conflict:
                        break
                if conflict:
                    break

        if conflict:
            # print(f"vertix conflict")
            continue

        # continue
        # second need to make sure the order is not violated
        # check the rows
        # grid1_rows_sorted = sorted(list(grid_place_now['rows'].keys()), key=lambda x: grid_place_now['rows'][x])
        # grid1_cols_sorted = sorted(list(grid_place_now['cols'].keys()), key=lambda x: grid_place_now['cols'][x])
        # if (not non_decreasing(grid1_rows_sorted)) or (not non_decreasing(grid1_cols_sorted)):
        #     # print(f"incompatible order")
        #     continue
        # # third, check if all the vertices contains gates
        # for grid_1_row, grid_0_row in grid_place_now['rows'].items():
        #     for grid_1_col, grid_0_col in grid_place_now['cols'].items():
        #         # need to make sure all the vertices have gates or have no atom
        #         qubit0 = occ[first_array_id, grid_0_row, grid_0_col]
        #         qubit1 = occ[second_array_id, grid_1_row, grid_1_col]
        #         if qubit0 == -1 or qubit1 == -1:
        #             # print(f"vertix ({qubit0}, {qubit1}) contains no atom")
        #             continue
        #         if [qubit0, qubit1] in gates:
        #             continue
        #         else:
        #             # print(f"vertix ({qubit0}, {qubit1}) not in the gates")
        #             break
        #     else:
        #         continue
        #     break

        # update the grid_placements and parallel gates info
        grid_placements = grid_place_now
        parallel_gate_ids.append(gate_id)
        parallel_gates.append(gate)
        # print(f"new grid_placements {grid_placements}")

    return (
        grid_placements,
        parallel_gate_ids,
        parallel_gates,
        this_overlap,
    )


def find_maximun_parallel_gates_v1(
    gates, pos, occ, first_array_id, second_array_id, n_aods
):
    # firstly have a look a the displacement between two locations
    displacement = {}
    grid_placements = {
        "rows": {},
        "cols": {},
    }  # the key is the index of second grid row / cols, the value is the index of the first grid row / cols
    gates_displacement_sorted = []
    for gate in gates:
        displacement[tuple(gate)] = abs(pos[gate[0]][1] - pos[gate[1]][1]) + abs(
            pos[gate[0]][2] - pos[gate[1]][2]
        )
    gates_displacement_sorted = sorted(gates, key=lambda x: displacement[tuple(x)])
    parallel_gate_ids = []
    parallel_gates = []
    for gate_id, gate in enumerate(
        gates_displacement_sorted
    ):  # starts to add gates according to the displacement
        conflict = False
        grid_place_now = deepcopy(grid_placements)
        # firstly need to make sure, the current one will not change the grid position of previous decided atoms
        if pos[gate[1]][1] in grid_place_now["rows"].keys():
            if pos[gate[0]][1] != grid_place_now["rows"][pos[gate[1]][1]]:
                print(f"{gate} v1: incompatible row")
                continue
            # else:
            # for grid_1_row, grid_0_row in grid_place_now['rows'].items():
            # if grid_0_row == pos[gate[0]][1]:
            # conflict =True
            # print(f"{gate} v1: incompatible row here")
            # break

        if pos[gate[1]][2] in grid_place_now["cols"].keys():
            if pos[gate[0]][2] != grid_place_now["cols"][pos[gate[1]][2]]:
                print(f"{gate} v1: incompatible col")
                continue
            # else:
            # for grid_1_col, grid_0_col in grid_place_now['cols'].items():
            # if grid_0_col == pos[gate[0]][2]:
            # conflict =True
            # print(f"{gate} v1: incompatible col here")
            # break

        for grid_1_row, grid_0_row in grid_place_now["rows"].items():
            if grid_0_row == pos[gate[0]][1] and grid_1_row != pos[gate[1]][1]:
                conflict = True
                print(f"{gate} v1: incompatible row here")
                break

        for grid_1_col, grid_0_col in grid_place_now["cols"].items():
            if grid_0_col == pos[gate[0]][2] and grid_1_col != pos[gate[1]][2]:
                conflict = True
                print(f"{gate} v1: incompatible col here")
                break

        if conflict:
            continue

        grid_place_now["rows"][pos[gate[1]][1]] = pos[gate[0]][1]
        grid_place_now["cols"][pos[gate[1]][2]] = pos[gate[0]][2]

        # print(f"checking new grid_place_now {grid_place_now}")
        # second need to make sure the order is not violated
        # check the rows
        grid1_rows_sorted = sorted(
            list(grid_place_now["rows"].keys()), key=lambda x: grid_place_now["rows"][x]
        )
        grid1_cols_sorted = sorted(
            list(grid_place_now["cols"].keys()), key=lambda x: grid_place_now["cols"][x]
        )
        if (not non_decreasing(grid1_rows_sorted)) or (
            not non_decreasing(grid1_cols_sorted)
        ):
            print(f"{gate} v1: incompatible order")
            continue
        # third, check if all the vertices contains gates
        for grid_1_row, grid_0_row in grid_place_now["rows"].items():
            for grid_1_col, grid_0_col in grid_place_now["cols"].items():
                # need to make sure all the vertices have gates or have no atom
                qubit0 = occ[first_array_id, grid_0_row, grid_0_col]
                qubit1 = occ[second_array_id, grid_1_row, grid_1_col]
                if qubit0 == -1 or qubit1 == -1:
                    # print(f"vertix ({qubit0}, {qubit1}) contains no atom")
                    continue
                if ([qubit0, qubit1] in gates) or ([qubit1, qubit0] in gates):
                    continue
                else:
                    print(f"{gate} v1: vertix ({qubit0}, {qubit1}) not in the gates")
                    break
            else:
                continue
            break
        else:
            # update the grid_placements and parallel gates info
            grid_placements = grid_place_now
            parallel_gate_ids.append(gate_id)
            parallel_gates.append(gate)
            # print(f"new grid_placements {grid_placements}")

    return grid_placements, parallel_gate_ids, parallel_gates


class CompilerLogger(object):
    def __init__(self, circ, n_rows, n_cols, n_aods, pos, occ, backend_config):
        self.circ = circ
        self.n_rows = n_rows
        self.n_cols = n_cols
        self.n_aods = n_aods
        self.pos = pos
        self.occ = occ
        self.backend_config = backend_config
        self.log = {"prop": {}, "code": [], "others": {}}

        self.n_X = n_cols
        self.n_Y = n_rows

        self.n_current_ancilla = 0

        self.ancilla_occupation = {}

        self.ancilla_abs_pos_x = np.zeros((self.n_aods, max(self.n_X)))
        self.ancilla_abs_pos_y = np.zeros((self.n_aods, max(self.n_Y)))

        self.log["prop"]["slm_rel_pos"] = []

    def new_code(self, type):
        self.log["code"].append({})
        code = self.log["code"][-1]
        code["data"] = []
        code["type"] = type
        # code["additional_args"] = {}
        return code

    def pos2Position(self, pos):
        position = Position(x=pos[2], y=pos[1], z=pos[0] - 1)
        return position

    def qubitidx2Position(self, idx):
        return self.pos2Position(self.pos[idx])

    def initialize_aod(self, aod_id_list):
        # move all lines to zero
        for aod_id in aod_id_list:
            for x in range(max(self.n_X)):
                self.ancilla_abs_pos_x[aod_id][x] = x
            for y in range(max(self.n_Y)):
                self.ancilla_abs_pos_y[aod_id][y] = y

    def move_aod_to_aod(self, my_list):
        # my_list=[(new_target_rel_position: Position, ancilla_rel: Position)...]
        new_list = {i: [] for i in range(self.n_aods)}
        for item in my_list:
            if item[0].z > item[1].z:
                item[0], item[1] = item[1], item[0]
            new_list[item[1].z].append([item[0], item[1]])
        code = self.new_code("move")
        for i in range(self.n_aods):
            if len(new_list[i]) == 0:
                continue
            for item in new_list[i]:
                if item[0].z != -1:
                    item[0] = Position(
                        self.ancilla_abs_pos_x[item[0].z][item[0].x],
                        self.ancilla_abs_pos_y[item[0].z][item[0].y],
                        i,
                    )
            self.ancilla_abs_pos_x, self.ancilla_abs_pos_y = move_qubit_to_line(
                new_list[i],
                self.backend_config,
                self.ancilla_abs_pos_x,
                self.ancilla_abs_pos_y,
            )
        code["data"] = {
            "ancilla_abs_pos_x": self.ancilla_abs_pos_x.tolist(),
            "ancilla_abs_pos_y": self.ancilla_abs_pos_y.tolist(),
        }

    def gate_2Q(self, my_list, additional_stage=0):
        # my_list=[(source_rel_pos: Position,target_rel_pos: Position)...]
        # self.log["prop"]["n_gate_2q"] += len(my_list)
        # self.log["prop"]["depth"] += 1
        code = self.new_code("gate_2Q")
        for item in my_list:
            code["data"].append({"source": item[0], "ancilla": item[1]})
        code["additional_stage"] = additional_stage

    def gate_1Q(self, my_list):
        # my_list=[source_rel_pos: Position...]
        code = self.new_code("gate_1Q")
        for item in my_list:
            code["data"].append({"source": item})

    def prepare(self):
        code = self.new_code("prepare")
        aod_touched = {}
        for key, val in self.pos.items():
            if val[0] == 0:  # this is slm
                self.log["prop"]["slm_rel_pos"].append(self.pos2Position(val))
            else:  # this is aod
                ancilla_rel_pos = self.pos2Position(val)
                slm_rel_pos = Position(x=ancilla_rel_pos.x, y=ancilla_rel_pos.y, z=-1)
                code["data"].append(
                    {"slm_rel_pos": slm_rel_pos, "ancilla_rel_pos": ancilla_rel_pos}
                )
                self.ancilla_occupation[ancilla_rel_pos] = slm_rel_pos
                self.n_current_ancilla += 1
                aod_touched[ancilla_rel_pos.z] = True
        self.initialize_aod(aod_touched.keys())
        self.log["prop"]["max_ancilla"] = int(
            (self.occ > -1).flatten().sum() - (self.occ[0] > -1).flatten().sum()
        )

        self.log["prop"]["n_qubits"] = int((self.occ > -1).flatten().sum())

    def get_log(self):
        self.prepare()
        total_overlap = 0
        num_2q_gates = 0
        num_1q_gates = 0
        num_layers_2 = 0
        num_layers_1 = 0
        dag = circuit_to_dag(self.circ)

        while True:
            add_layer_1 = False
            add_layer_2 = False
            front_layer = dag.front_layer()

            data_1q_source = []
            for node in front_layer:
                if node.op.num_qubits == 1:
                    dag.remove_op_node(node)
                    num_1q_gates += 1
                    add_layer_1 = True
                    data_1q_source.append(self.qubitidx2Position(node.qargs[0]._index))

            if add_layer_1:
                self.gate_1Q(data_1q_source)

            if dag.depth() == 0:
                break

            # count 2q
            gates_this_layer = []
            nodes_this_layer = []
            for node in front_layer:
                if node.op.num_qubits == 2:
                    nodes_this_layer.append(node)
                    gate_2q = list(map(lambda x: x._index, node.qargs))
                    if gate_2q[0] > gate_2q[1]:
                        gate_2q = [gate_2q[1], gate_2q[0]]
                    gates_this_layer.append(gate_2q)

            if self.backend_config.fpqac_router == "serial":
                if len(gates_this_layer) > 0:
                    for gate in gates_this_layer:
                        source_ancilla_data = [
                            (
                                self.qubitidx2Position(gate[0]),
                                self.qubitidx2Position(gate[1]),
                            )
                        ]
                        self.move_aod_to_aod(source_ancilla_data)
                        self.gate_2Q(source_ancilla_data)

                    for node in nodes_this_layer:
                        dag.remove_op_node(node)

                    add_layer_2 = True
                    num_2q_gates += len(gates_this_layer)

            elif self.backend_config.fpqac_router == "default":
                if len(gates_this_layer) > 0:
                    # print(f"gates this layer {gates_this_layer}")
                    (
                        grid_placements,
                        parallel_gate_ids,
                        parallel_gates,
                        this_overlap,
                    ) = find_maximun_parallel_gates(
                        gates_this_layer,
                        self.pos,
                        self.occ,
                        self.n_aods,
                        self.backend_config.fpqac_individual_addressable,
                        self.backend_config.fpqac_allow_order_violation,
                        self.backend_config.fpqac_allow_overlap_violation,
                    )
                    total_overlap += this_overlap
                    # print(parallel_gates, parallel_gate_ids)
                    # need to compute the heating issue

                    source_ancilla_data = []
                    for gate in parallel_gates:
                        source_ancilla_data.append(
                            (
                                self.qubitidx2Position(gate[0]),
                                self.qubitidx2Position(gate[1]),
                            )
                        )

                    self.move_aod_to_aod(source_ancilla_data)
                    self.gate_2Q(source_ancilla_data)

                    for gate_id in parallel_gate_ids:
                        dag.remove_op_node(nodes_this_layer[gate_id])
                    if len(parallel_gates) > 0:
                        add_layer_2 = True
                    num_2q_gates += len(parallel_gates)
            else:
                raise Exception("fpqac_router not recognized")

            if add_layer_1:
                num_layers_1 += 1
            if add_layer_2:
                num_layers_2 += 1
            if dag.depth() == 0:
                break
        res = {
            "n_1q_gates": num_1q_gates,
            "n_2q_gates": num_2q_gates,
            "n_2q_layers": num_layers_2,
            "n_1q_layers": num_layers_1,
            "n_move": num_layers_2 - 1,
        }
        self.log["others"]["total_overlap"] = total_overlap
        # print(res)
        # self.log['prop']['depth'] = num_layers_2
        # log['prop']['gate_1q'] = num_1q_gates
        # log['prop']['gate_2q'] = num_2q_gates
        # log['code'] = code
        # log['prop']['compilation_time'] = time() - time_start
        # log['prop']['max_ancilla'] = n_qubits_aod
        return self.log


def compiler_log(circ, n_rows, n_cols, n_aods, pos, occ, backend_config):
    # count the number of layers of the circuit
    time_start = time()

    log = {}

    num_2q_gates = 0
    num_1q_gates = 0
    num_layers_2 = 0
    num_layers_1 = 0
    dag = circuit_to_dag(circ)
    n_qubits_aod = (occ > -1).flatten().sum() - (occ[0] > -1).flatten().sum()
    log["prop"] = {"n_qubits": (occ > -1).flatten().sum()}

    code = {}
    code_id = 0

    aod_flatten = occ[1:].flatten()
    aod_qubit_index = aod_flatten[np.where(aod_flatten > -1)]

    info = {
        "data": [{"source": inde} for inde in aod_qubit_index],
        "type": "create",
    }
    code[str(code_id)] = info
    code_id += 1

    grid_placements_last = None

    def get_row_col_dict(grid_placements, row_col, rc_dict):
        # rc_dict = {}

        for rc_set in grid_placements[row_col]:
            for rc in rc_set:
                if rc.split("_")[1] == "0":
                    array0_rc_id = int(rc.split("_")[3])
                    break
            else:
                continue
            for rc in rc_set:
                if rc.split("_")[1] == "0":
                    continue
                rc_dict[rc] = array0_rc_id

        return rc_dict

    def get_max_movement(row_dict, row_dict_last, col_dict, col_dict_last):
        max_row_movement = 0
        max_col_movement = 0
        # row_dict = get_row_col_dict(grid_placements, 'rows')
        # row_last_dict = get_row_col_dict(grid_placements_last, 'rows')
        # col_dict = get_row_col_dict(grid_placements, 'cols')
        # col_last_dict = get_row_col_dict(grid_placements_last, 'cols')

        for row in row_dict.keys():
            if row not in row_dict_last.keys():
                continue
            max_row_movement = max(
                max_row_movement, abs(row_dict[row] - row_dict_last[row])
            )
        for col in col_dict.keys():
            if col not in col_dict_last.keys():
                continue
            max_col_movement = max(
                max_col_movement, abs(col_dict[col] - col_dict_last[col])
            )

        return np.sqrt(max_row_movement**2 + max_col_movement**2)

    x_zpf = backend_config["x_zpf"]
    omega_0 = backend_config["omega_0"]
    # self.move_speed = self.backend_config["move_speed"]
    T_per_move = backend_config["T_per_move"]
    atom_distance = backend_config["atom_distance"]
    error_2q_per_deltaN = backend_config["two_q_error_delta_N"]
    n_heating_reset_cycle = backend_config["na_n_move_per_cooling"]

    def calc_delta_N(distance):
        return (
            0.5
            * (6 * (distance * atom_distance) / x_zpf / (omega_0**2) / (T_per_move**2))
            ** 2
        )

    delta_N = 0
    f_heating = 1
    heating_reset_cycle_count = 0
    row_dict = {}
    col_dict = {}

    total_distance = 0
    while True:
        add_layer_2 = False
        add_layer_1 = False
        front_layer = dag.front_layer()

        data_1q_source = []
        for node in front_layer:
            if node.op.num_qubits == 1:
                dag.remove_op_node(node)
                num_1q_gates += 1
                add_layer_1 = True
                data_1q_source.append({"source": node.qargs[0].index})

        if add_layer_1:
            info = {
                "data": data_1q_source,
                "type": "gate_1Q",
                "distance": 0,
            }
            code[str(code_id)] = info
            code_id += 1

        if dag.depth() == 0:
            break

        # count 2q
        gates_this_layer = []
        indexes_this_layer = []
        nodes_this_layer = []
        for node in front_layer:
            if node.op.num_qubits == 2:
                nodes_this_layer.append(node)
                gate_2q = list(map(lambda x: x.index, node.qargs))
                if gate_2q[0] > gate_2q[1]:
                    gate_2q = [gate_2q[1], gate_2q[0]]
                gates_this_layer.append(gate_2q)
                # num_2q_gates += 1
                # dag.remove_op_node(node)
                # add_layer_2 = True

        # now we need to check if all the 2q gates in this layer can be executed together

        index_pair = []
        # need to sepqare the 2q gates into diffdderent groups
        gate_array_groups = []
        # for k in n_aods:

        if len(gates_this_layer) > 0:
            heating_reset_cycle_count += 1
            if heating_reset_cycle_count > n_heating_reset_cycle:
                # apply cooling
                info = {
                    "data": n_qubits_aod,
                    "type": "cooling",
                    "distance": 0,
                }
                code[str(code_id)] = info
                code_id += 1

                delta_N = 0
                heating_reset_cycle_count = 0

            # print(f"gates this layer {gates_this_layer}")
            grid_placements, parallel_gate_ids, parallel_gates = (
                find_maximun_parallel_gates(gates_this_layer, pos, occ, n_aods)
            )
            # print(parallel_gates, parallel_gate_ids)
            # need to compute the heating issue
            row_dict_last = deepcopy(row_dict)
            row_dict = get_row_col_dict(grid_placements, "rows", row_dict)

            col_dict_last = deepcopy(col_dict)
            col_dict = get_row_col_dict(grid_placements, "cols", col_dict)

            if grid_placements_last is not None:
                max_distance = get_max_movement(
                    row_dict, row_dict_last, col_dict, col_dict_last
                )
            else:
                max_distance = 0

            source_ancilla_data = []
            for gate in parallel_gates:
                source_ancilla_data.append({"source": gate[0], "ancilla": gate[1]})

            info = {
                "data": source_ancilla_data,
                "type": "move",
                "distance": max_distance,
            }
            # print(f'move {max_distance}')
            code[str(code_id)] = info
            code_id += 1

            total_distance += max_distance
            # print(max_distance)
            grid_placements_last = grid_placements

            # grid_placements_v1, parallel_gate_ids_v1, parallel_gates_v1 = find_maximun_parallel_gates_v1(gates_this_layer, pos, occ, 0, 1, n_aods)
            # grid_placements, parallel_gate_ids, parallel_gates = find_maximun_parallel_gates_v1(gates_this_layer, pos, occ, 0, 1, n_aods)
            # if len(parallel_gate_ids) != len(parallel_gate_ids_v1) or  not all(np.array(parallel_gate_ids) == np.array(parallel_gate_ids_v1)):
            #     print("not equal")
            #     print(parallel_gate_ids)
            #     print(parallel_gate_ids_v1)

            # print(f"parallelism this layer {len(parallel_gates)}")
            # print(grid_placements, parallel_gate_ids, parallel_gates)
            for gate_id in parallel_gate_ids:
                dag.remove_op_node(nodes_this_layer[gate_id])

            for gate in parallel_gates:
                info = {
                    "data": [{"source": gate[0], "target": gate[1]}],
                    "type": "gate_2Q",
                    "distance": 0,
                }
                code[str(code_id)] = info
                code_id += 1

            add_layer_2 = True
            num_2q_gates += len(parallel_gates)

            delta_N += calc_delta_N(max_distance)

            f_heating *= (1 - delta_N * error_2q_per_deltaN) ** len(parallel_gates)

        # now check if all the 2q gates in this layer can be executed together
        # print(wires_this_layer)
        # print(indexes_this_layer)
        # for k, indexes in enumerate(indexes_this_layer):
        # this is similar to previous fpqa, need to fine the same order

        # for node in twoq_nodes_this_layer:
        # dag.remove_op_node(node)

        if add_layer_1:
            num_layers_1 += 1
        if add_layer_2:
            num_layers_2 += 1
        if dag.depth() == 0:
            break
    res = {
        "compilation time": time() - time_start,
        "n_1q_gates": num_1q_gates,
        "n_2q_gates": num_2q_gates,
        "n_2q_layers": num_layers_2,
        "n_1q_layers": num_layers_1,
        "f_heating": f_heating,
        "n_move": num_layers_2 - 1,
        "n_qubits_aod": n_qubits_aod,
        "n_heating_reset_cycle": n_heating_reset_cycle,
        "distance": total_distance,
    }

    print(res)
    log["prop"]["depth"] = num_layers_2
    log["prop"]["gate_1q"] = num_1q_gates
    log["prop"]["gate_2q"] = num_2q_gates
    log["code"] = code
    log["prop"]["compilation_time"] = time() - time_start
    log["prop"]["max_ancilla"] = n_qubits_aod
    # print(log)
    return log


def post_processing(info, n_qubits):
    res = deepcopy(info)
    n_1q_gates = info["n_1q_gates"]
    n_2q_gates = info["n_2q_gates"]
    n_1q_layer = info["n_1q_layers"]
    n_2q_layer = info["n_2q_layers"]
    n_transfer = 0
    f_transfer = 1.0

    n_qubits_aod = info["n_qubits_aod"]

    time = {}

    # caution here, layer or gate number??
    time["1q gate"] = n_1q_layer * backend_config["1Q_time"]
    time["2q gate"] = n_2q_layer * backend_config["2Q_time"]
    time["transfer"] = 0
    time["movement"] = info["n_move"] * backend_config["T per move"]
    time["total_time"] = (
        time["1q gate"] + time["2q gate"] + time["transfer"] + time["movement"]
    )

    fid = {}
    fid["1q gate"] = backend_config["1Q_fidelity"] ** n_1q_gates
    fid["2q gate"] = backend_config["2Q_fidelity"] ** n_2q_gates
    n_2q_swap = 2
    fid["2q recycle"] = backend_config["2Q_fidelity"] ** (
        info["n_move"] // info["n_heating_reset_cycle"] * n_qubits_aod * n_2q_swap
    )

    fid["decoherence"] = np.exp(-time["total_time"] * n_qubits / backend_config["T1"])
    fid["decoherence_movement"] = np.exp(
        -time["movement"] * n_qubits / backend_config["T1"]
    )

    fid["transfer"] = 1.0
    fid["movement"] = fid["2q recycle"] * info["f_heating"]

    fid["total_fidelity"] = (
        fid["1q gate"]
        * fid["2q gate"]
        * fid["transfer"]
        * fid["decoherence"]
        * fid["movement"]
    )
    res["1Q gate count"] = n_1q_gates
    res["2Q gate count"] = n_2q_gates

    res["depth"] = info["n_2q_layers"]

    res["time"] = time
    res["fidelity"] = fid
    res["transfer count"] = 0
    res["distance"] = info["distance"] * backend_config["atom distance"]
    res["t/t1"] = time["total_time"] / backend_config["T1"]

    return res


def print_latex(res):
    fid = res["fidelity"]
    time = res["time"]
    print(
        f"  {res['compilation time']:.2f}  & {res['1Q gate count']:.1f}  &  {res['2Q gate count']:.1f} & {res['depth']:.1f}  &  {fid['total_fidelity']:.3f}  &  {res['t/t1']:.4f}"
    )
    print(
        f"  {res['compilation time']:.2f}  &  {res['depth']:.1f}  &  {fid['total_fidelity']:.3f}  &  {time['total_time']*1000:.2f} & {res['1Q gate count']:.1f}  &  {res['2Q gate count']:.1f}  &  {res['depth']-1:.1f}  &   {res['distance']*1000:.2f}  &  {int(res['transfer count'])}"
    )
    print(
        f"  {fid['1q gate']:.3f}  &  {fid['2q gate']:.3f}  &  {fid['movement']:.3f}  &  {fid['transfer']:.3f}  &  {fid['decoherence']:.3f}  &  {time['1q gate']*1000:.2f}  &  {time['2q gate']*1000:.2f}  &  {time['movement']*1000:.2f}  &  {int(time['transfer']*1000)}"
    )


def print_latex2(res):
    fid = res["fidelity"]
    time = res["time"]
    print(
        f"  {res['compilation time']:.2f}  & {res['1Q gate count']:.1f}  &  {res['2Q gate count']:.1f} & {res['depth']:.1f}  &  {fid['total_fidelity']:.3f}  &  {res['t/t1']:.4f}"
    )


if __name__ == "__main__":
    import pdb

    pdb.set_trace()
    n_rows = n_cols = 6
    n_aods = 2
    n_qubits = 100
    n_gates = 10
    idx = 2

    # circ = QuantumCircuit.from_qasm_file(f"benchmarks/arbitrary/q{n_qubits}_g{n_gates}/i{idx}.qasm")
    cmap = gen_fpqa_coupling_map(n_rows, n_cols, n_aods, bidirectional=True)
    # compiled_circuit = transpile(
    #         circ,
    #         coupling_map=cmap,
    #         basis_gates=["u3", "cz", "id"],
    #         optimization_level=3,
    #         layout_method="sabre",
    #         routing_method="sabre",
    #     )
    # print(compiled_circuit)

    # print(get_p2v(compiled_circuit))

    with open("compiler/backend_config.json", "r") as f:
        backend_config = json.load(f)

    circ = QuantumCircuit.from_qasm_file(
        f"baseline/qiskit_sabre_fpqa/qiskit_fpqa{n_rows}_{n_aods}/arb/q{n_qubits}_g{n_gates}/i{idx}.qasm"
    )
    print(len(get_all_2q_gates(circ)))
    circ = transpile(
        circ,
        basis_gates=["cz", "id", "u2", "u1", "u3"],
        coupling_map=cmap,
        optimization_level=2,
        layout_method="sabre",
        routing_method="sabre",
        seed_transpiler=0,
    )
    print(len(get_all_2q_gates(circ)))

    node_2q_gates = []
    dag = circuit_to_dag(circ)
    for node in dag.nodes():
        if hasattr(node, "op") and node.op.num_qubits == 2:
            # nodes_this_layer.append(node)
            gate_2q = list(map(lambda x: x.index, node.qargs))
            node_2q_gates.append(gate_2q)
            # gates_this_layer.append(gate_2q)
            if gate_2q == [52, 58]:
                print("here")

    # print(f"{[52, 58] in get_all_2q_gates(circ)}")
    # exit(0)

    occ, pos = get_occupation_positions(
        circ, 10, 10, n_aods, original_n_rows=n_rows, original_n_cols=n_cols
    )
    # print(f"{[52, 58] in get_all_2q_gates(circ)}")
    # exit(0)
    print(occ)
    print(
        count_layer_multiaod(
            circ,
            n_rows,
            n_cols,
            n_aods,
            pos,
            occ,
            backend_config,
            n_heating_reset_cycle=20,
        )
    )
    print(count_layer(circ))

    # firstly see how many qubits are used in each of the array

    # circ = QuantumCircuit.from_qasm_file(f"results_multiaod/results_qiskit_fpqa{n_rows}_{n_aods}/arb/q{n_qubits}_g{n_gates}/i{idx}.qasm")

    # count_layer_multiaod(circ, n_cols, n_rows, n_aods)
    # coup_map = gen_fpqa_coupling_map(n_rows, n_cols, n_aods, bidirectional=True)

    # print(circ)
    # for gate in circ.data:
    #     wires = list(map(lambda x: x.index, gate[1]))
    #     if len(wires) == 2:
    #         print(wires)
    #         print(wires in coup_map)

    print("finished")

    # for element in row_set:
    # if element.split('_')[1] == str(array0):
    # row0 = int(element.split('_')[3])
    # elif element.split('_')[1] == str(array1):
    # row1 = int(element.split('_')[3])

    # firstly need to make sure, the current one will not change the grid position of previous decided atoms
    # here only the large id to small can overlap, for example, both array1-2 and array1-3 can be mapped to array0-1
    # but array0-2 and array0-3 cannot be both mapped to array1-2 because array0 may be SLM
    # if pos[gate[1]][1] in grid_place_now[place_key1to0]['rows'].keys():
    #     if pos[gate[0]][1] != grid_place_now[place_key1to0]['rows'][pos[gate[1]][1]]:
    #         # print(f"incompatible row")
    #         continue

    # if pos[gate[1]][2] in grid_place_now[place_key1to0]['cols'].keys():
    #     if pos[gate[0]][2] != grid_place_now[place_key1to0]['cols'][pos[gate[1]][2]]:
    #         # print(f"incompatible col")
    #         continue

    # # add this new gate to the grid
    # grid_place_now[place_key1to0]['rows'][pos[gate[1]][1]] = pos[gate[0]][1]
    # grid_place_now[place_key1to0]['cols'][pos[gate[1]][2]] = pos[gate[0]][2]

    # grid_place_now[place_key0to1]['rows'][pos[gate[0]][1]] = pos[gate[1]][1]
    # grid_place_now[place_key0to1]['cols'][pos[gate[0]][2]] = pos[gate[1]][2]

    # make sure all the pairs are in the grid, for example, if array2to1 has 2:1, array3to1 has 3:1 and array3to2 must have 3:2 and array2to3 much have 2:3
    # for grid_place_now[place_key0to1]['rows'][pos[gate[0]][1]]

    # print(f"checking new grid_place_now {grid_place_now}")
