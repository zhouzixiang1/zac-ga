import csv
import os
import json
import statistics

circuit_in_use = {"bv_n14", "bv_n19", "bv_n30", "bv_n70",
                  "cat_n22", "cat_n35",
                  "ghz_n23", "ghz_n40", "ghz_n78",
                  "ising_n42", "ising_n98",
                  "knn_n31", "multiply_n13", "qft_n18", "seca_n11", "swap_test_n25", "wstate_n27"}

def write_qasm_csv(generate_data, working_directory = None, compiler_name = None):
    header = [["qubit number", "index"], ["", ""]]
    filename = 'csv/{}.csv'.format(generate_data)
    list_element = ["cir_fidelity", "cir_fidelity_2q_gate", \
        "cir_fidelity_2q_gate_for_idle", "cir_fidelity_atom_transfer", \
        "cir_fidelity_coherence", "cir_duration"]
    header = [["filename"], ["x"]]
    for data_name in compiler_name:
        for fidelity_term in list_element:
            header[0].append(data_name)
            header[1].append(fidelity_term)

    data = dict()
    for file in sorted(os.listdir(working_directory[0])):
        full_filename = os.fsdecode(file)
        tmp = full_filename.split("/")[-1]
        tmp = tmp.split("_fidelity.")[0]
        tmp = tmp.split("_transpiled")[0]
        data[tmp] = [0 for i in range((len(working_directory)) * len(list_element))]
    begin_idx = 0
    for directory in working_directory:
        for file in sorted(os.listdir(directory)):
            full_filename = os.fsdecode(file)
            tmp = full_filename.split("/")[-1]
            tmp = tmp.split("_fidelity.")[0]
            tmp = tmp.split("_transpiled")[0]
            with open("{}/{}".format(directory, full_filename), 'r') as file:
                data_per_file = json.load(file)
                tmp_idx = begin_idx
                if tmp in data:
                    for element in list_element:
                        if element in data_per_file:
                            if element == "movement_time_ratio":
                                data[tmp][tmp_idx] = statistics.mean(data_per_file[element])
                            else:
                                data[tmp][tmp_idx] = data_per_file[element]
                        tmp_idx += 1
        begin_idx += len(list_element)
                    
        
    with open(filename, 'w', newline="") as file:
        csvwriter = csv.writer(file) # 2. create a csvwriter object
        csvwriter.writerows(header) # 4. write the header
        dictlist = []
        for key, value in data.items():
            temp = [key] + value
            dictlist.append(temp)
        data = dictlist
        csvwriter.writerows(data) # 5
    return filename

def write_runtime_csv(working_directory = None, compiler_name = None):
    header = [["qubit number", "index"], ["", ""]]
    filename = 'csv/time.csv'
    list_element = ["total", "cir_fidelity"]
    header = [["filename"], ["x"]]
    for data_name in compiler_name:
        for fidelity_term in list_element:
            header[0].append(data_name)
            header[1].append(fidelity_term)

    data = dict()
    for file in sorted(os.listdir(working_directory[0][-1])):
        full_filename = os.fsdecode(file)
        tmp = full_filename.split("/")[-1]
        tmp = tmp.split("_transpiled")[0]
        tmp = tmp.split("_time")[0]
        if tmp in circuit_in_use:
            data[tmp] = [0 for i in range((len(working_directory[0])* len(list_element)))]
    begin_idx = 0
    for directory1, directory2 in zip(working_directory[0], working_directory[1]):
        if directory1 != "result/nalac/" and directory1 != "result_static/nalac/":
            for file in sorted(os.listdir(directory1)):
                full_filename = os.fsdecode(file)
                tmp = full_filename.split("/")[-1]
                tmp = tmp.split("_transpiled")[0]
                tmp = tmp.split("_state")[0]
                tmp = tmp.split("_fidelity")[0]
                tmp = tmp.split("_time")[0]
                with open("{}/{}".format(directory1, full_filename), 'r') as file:
                    data_per_file = json.load(file)
                    if tmp in data:
                        if list_element[0] in data_per_file:
                            # print(directory1, full_filename)
                            # print("add runtime at ", begin_idx)
                            data[tmp][begin_idx] = data_per_file[list_element[0]]
                        elif "compilation_time" in data_per_file:
                            data[tmp][begin_idx] = data_per_file["compilation_time"]
        else:
            full_filename = "result/nalac/time.uu"
            with open(full_filename, 'r') as f:
                data_name = None
                for line in f:
                    tmp_line = line.strip()
                    if data_name == None:
                        tmp_line = tmp_line.split('.')[0]
                        data_name = tmp_line.split('/')[-1]
                    elif data_name in circuit_in_use:
                        data[data_name][begin_idx] = float(tmp_line)
                        data_name = None
        for file in sorted(os.listdir(directory2)):
            full_filename = os.fsdecode(file)
            tmp = full_filename.split("/")[-1]
            tmp = tmp.split("_transpiled")[0]
            tmp = tmp.split("_state")[0]
            tmp = tmp.split("_fidelity")[0]
            with open("{}/{}".format(directory2, full_filename), 'r') as file:
                data_per_file = json.load(file)
                if tmp in data and list_element[1] in data_per_file:
                    # print(directory2, full_filename)
                    # print("add fidelity at ", begin_idx + 1)
                    data[tmp][begin_idx + 1] = data_per_file[list_element[1]]
        begin_idx += len(list_element)

                    
        
    with open(filename, 'w', newline="") as file:
        csvwriter = csv.writer(file) # 2. create a csvwriter object
        csvwriter.writerows(header) # 4. write the header
        dictlist = []
        for key, value in data.items():
            temp = [key] + value
            dictlist.append(temp)
        data = dictlist
        csvwriter.writerows(data) # 5
    return filename