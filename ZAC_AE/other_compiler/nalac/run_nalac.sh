#!/bin/bash
for filename in results/*.out; do
    srun python3 nalac_simulate.py ../../hardware_spec/full_architecture.json "$filename"
done