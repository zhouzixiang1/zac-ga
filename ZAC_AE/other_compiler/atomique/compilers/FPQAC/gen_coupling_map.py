import numpy as np


def gen_fpqa_coupling_map(n_rows, n_cols, n_aods, n_qubits_max, bidirectional=True):
    """Generate a coupling map for the FPQA.
    Args:
        n_rows (int): Number of rows in the FPQA.
        n_cols (int): Number of columns in the FPQA.
        n_aods (int): Number of AODs arrays in the FPQA.
        bidirectional (bool): Whether the coupling map is bidirectional.
    Returns:
        list: A coupling map.
    """

    coupling_map = []

    # n_qubits = n_rows * n_cols * (n_aods + 1)
    # n_qubits = n_qubits_max

    # the ids of slms goes from [0, n_rows * n_cols - 1]
    # the ids of aods goes from [n_rows * n_cols, n_qubits - 1]

    # every slm is connected to all aods
    # for slm_id in range(n_rows * n_cols):
    #     for aod_id in range(n_rows * n_cols, n_qubits):
    #         coupling_map.append([slm_id, aod_id])

    # # for aod, each aod is connected to the atoms in other aod arrays
    # if n_aods > 1:
    #     for aod_id in range(n_rows * n_cols, n_qubits):
    #         for other_aod_id in range(n_rows * n_cols, n_qubits):
    #             if other_aod_id // (n_rows * n_cols) == aod_id // (n_rows * n_cols):
    #                 continue
    #             elif other_aod_id <= aod_id:
    #                 continue
    #             else:
    #                 coupling_map.append([aod_id, other_aod_id])
    for layer_id_1 in range(n_aods + 1):
        for layer_id_2 in range(n_aods + 1):
            if layer_id_1 == layer_id_2:
                continue
            for qubit_id_1 in range(n_rows[layer_id_1] * n_cols[layer_id_1]):
                for qubit_id_2 in range(n_rows[layer_id_2] * n_cols[layer_id_2]):
                    coupling_map.append(
                        [
                            (n_rows[0:layer_id_1] * n_cols[0:layer_id_1]).sum()
                            + qubit_id_1,
                            (n_rows[0:layer_id_2] * n_cols[0:layer_id_2]).sum()
                            + qubit_id_2,
                        ]
                    )

    # for aod, each aod is connected to the neighboring aods in the same row and column
    # this is not easy to implement, becaues other atoms on the same row will be forced to do gate so removed
    # for aod_id in range(n_rows * n_cols, n_qubits):
    #     # connect to the aod in the same row
    #     if aod_id % n_cols != n_cols - 1:
    #         coupling_map.append([aod_id, aod_id + 1])
    #     # connect to the aod in the same column
    #     if aod_id % (n_rows * n_cols) < (n_rows - 1) * n_cols:
    #         coupling_map.append([aod_id, aod_id + n_cols])

    if bidirectional:
        coupling_map = coupling_map + [[y, x] for x, y in coupling_map]

    return coupling_map


if __name__ == "__main__":
    # print(gen_fpqa_coupling_map(2, 2, 2, bidirectional=False))
    gen_fpqa_coupling_map(2, 2, 2, bidirectional=False)
