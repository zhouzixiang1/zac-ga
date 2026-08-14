# NALAC data from compiled results
This directory provides how we calculate metrics like fidelity from the compiled results by NALAC.

The files in `results/qasm/` are copied from `../nalac_source/benchmarks/bench_using/`.
The files in `results/out/` are copied from `../nalac_source/benchmarks/result/`.
These are compilation results by NALAC.
Please refer to `../nalac_source/` if you want to know how they are generated.

The script `nalac_simulate.py` takes in 3 arguments and produce a JSON file with calculated fidelity etc, e.g., `results/fidelity/bv_n14_fidelity.json`.
These fidelity JSON files are copied to `../../result/nalac/fidelity/` to draw the figures.
To use the calculation script, use command like `python nalac_simulate.py ../../hardware_spec/full_architecture.json  results/out/bv_n14 results/qasm/bv_n14.qasm` in the current directory.