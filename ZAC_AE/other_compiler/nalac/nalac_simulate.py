import sys
import json
import ast

def list_from_nested_tuple(a):
    if isinstance(a[0], tuple):
        return a
    return [a]

if __name__ == '__main__':
    arch_spec_file = sys.argv[1]
    with open(arch_spec_file, 'r') as f:
        arch_spec = json.load(f)
    storage_slm =  arch_spec['storage_zones'][0]['slms'][0]
    storage_yhigh = storage_slm['site_seperation'][1] * storage_slm['r']

    nalac_out_file = sys.argv[2]
    with open(nalac_out_file, 'r') as f:
        nalac_lines = f.readlines()
    
    qasm_file = sys.argv[3]
    n_ryd = 0
    with open(qasm_file, 'r') as f:
        qasm_lines = f.readlines()
        for line in qasm_lines:
            if line.startswith('cz'):
                n_ryd += 1

    qubit_locs = []
    idling_time = []
    total_time = 0
    n_raman = 0
    n_exc = 0
    n_trans = 0
    for line in nalac_lines:
        if line.startswith('init'):
            qubit_init_locs = list_from_nested_tuple(ast.literal_eval(
                line.split('at')[1].split(';')[0].strip()))
            n_qubit = len(qubit_init_locs)
            for i in range(n_qubit):
                qubit_locs.append(qubit_init_locs[i])
                idling_time.append(0)

        if line.startswith('rz'):
            qubit_addressed = list_from_nested_tuple(ast.literal_eval(
                line.split('at')[1].split(';')[0].strip()))
            n_raman += len(qubit_addressed)
            total_time += arch_spec['operation_duration']['raman'] * len(qubit_addressed)
            for i in range(n_qubit):
                idling_time[i] += arch_spec['operation_duration']['raman'] * len(qubit_addressed)
                if qubit_locs[i] in qubit_addressed:
                    idling_time[i] -= arch_spec['operation_duration']['raman']

        if line.startswith('cz'):
            total_time += arch_spec['operation_duration']['rydberg']
            for i, loc in enumerate(qubit_locs):
                if loc[1] > storage_yhigh:
                    n_exc += 1
                else:
                    idling_time[i] += arch_spec['operation_duration']['rydberg']

        if line.startswith('load'):
            total_time += arch_spec['operation_duration']['atom_transfer']
            locs_before = list_from_nested_tuple(ast.literal_eval(
                line.split('to')[0].split('load')[1].strip()))
            locs_after = list_from_nested_tuple(ast.literal_eval(
                line.split('to')[1].split(';')[0].strip()))
            
            for i in range(n_qubit):
                found = 0
                for loc_before, loc_after in zip(locs_before, locs_after):
                    if qubit_locs[i] == loc_before:
                        qubit_locs[i] = loc_after
                        found = 1
                        n_trans += 1
                        break
                if not found:
                    idling_time[i] += arch_spec['operation_duration']['atom_transfer']

        if line.startswith('store'):
            total_time += arch_spec['operation_duration']['atom_transfer']
            locs_before = list_from_nested_tuple(ast.literal_eval(
                line.split('to ')[0].split('store')[1].strip()))
            locs_after = list_from_nested_tuple(ast.literal_eval(
                line.split('to ')[1].split(';')[0].strip()))
            
            for i in range(n_qubit):
                found = 0
                for loc_before, loc_after in zip(locs_before, locs_after):
                    if qubit_locs[i] == loc_before:
                        qubit_locs[i] = loc_after
                        found = 1
                        n_trans += 1
                        break
                if found:
                    idling_time[i] += arch_spec['operation_duration']['atom_transfer']

        if line.startswith('move'):
            longest_move = 0
            locs_before = list_from_nested_tuple(ast.literal_eval(
                line.split('to')[0].split('move')[1].strip()))
            locs_after = list_from_nested_tuple(ast.literal_eval(
                line.split('to')[1].split(';')[0].strip()))
            for i in range(n_qubit):
                for loc_before, loc_after in zip(locs_before, locs_after):
                    if qubit_locs[i] == loc_before:
                        qubit_locs[i] = loc_after
                        longest_move = max(
                            longest_move, 
                            (
                                (loc_before[0]-loc_after[0])**2 + \
                                    (loc_before[1]-loc_after[1])**2 )**0.5 )
                        break

            for i in range(n_qubit):
                idling_time[i] += (longest_move / 0.00275)**0.5
            total_time += longest_move

    n_exc -= n_ryd * 2
    fidelities = {}
    fidelity_total = 1
    fidelities['cir_fidelity_1q_gate'] = arch_spec['operation_fidelity']['single_qubit_gate']**n_raman
    fidelity_total *= fidelities['cir_fidelity_1q_gate']
    fidelities['cir_fidelity_2q_gate'] = arch_spec['operation_fidelity']['two_qubit_gate']**n_ryd
    fidelity_total *= fidelities['cir_fidelity_2q_gate']
    fidelities['cir_fidelity_2q_gate_for_idle'] = (1 - (1 - arch_spec['operation_fidelity']['two_qubit_gate']) / 2 )**n_exc
    fidelity_total *= fidelities['cir_fidelity_2q_gate_for_idle']
    fidelities['cir_fidelity_atom_transfer'] = arch_spec['operation_fidelity']['atom_transfer']**n_trans
    fidelity_total *= fidelities['cir_fidelity_atom_transfer']
    decohere = 1
    for itime in idling_time:
        decohere *= (1 - itime / arch_spec['qubit_spec']['T']) 
    fidelities['cir_fidelity_coherence'] = decohere
    fidelity_total *= fidelities['cir_fidelity_coherence']
    fidelities['cir_fidelity'] = fidelity_total
    fidelities['cir_duration'] = total_time
    
    filename = nalac_out_file.split('/')[-1]
    with open("results/fidelity/" + filename + '_fidelity.json', 'w') as f:
        json.dump(fidelities, f)