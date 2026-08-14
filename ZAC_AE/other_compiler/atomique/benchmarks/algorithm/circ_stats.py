import os

import yaml
from qiskit import QuantumCircuit, transpile

from utils import get_n2q_interation_stats

if __name__ == "__main__":
    import pdb

    # pdb.set_trace()
    # dirs = ['raw/qasmbench/small', 'raw/qasmbench/medium', 'raw/qasmbench/large']
    ben = "large"
    dirs = [f"benchmarks/QASMBench/"]

    stats = {}
    # list all folders in the dir
    for dir in dirs:
        for file in os.listdir(os.path.join(dir)):
            # if "transpiled" in file:
            print(file)
            circ = QuantumCircuit.from_qasm_file(os.path.join(dir, file))
            counts = circ.count_ops()
            if counts.get("cx", 0) > 1000000 or counts.get("cz", 0) > 1000000:
                continue
            stats[file] = get_n2q_interation_stats(circ)
            print(stats[file])

    # yaml.dump(stats, open(f"benchmarks/algorithm/stats/stats_{ben}.yaml", "w"))
