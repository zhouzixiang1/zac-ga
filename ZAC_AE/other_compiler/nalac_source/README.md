# Reproducing NALAC Data

## Some Notes
- The arXiv version of the paper is at this [link](https://arxiv.org/abs/2405.08068).
- The NALAC code included in this directory is an early version shared by the developers to us in Aug 2024.
- NALAC uses randomness, so it may not be possible to exactly reproduce the data. 
- The data used in our paper is at `benchmarks/result/`. These may be copied to other places in the repo to draw figures.

## Environment Requirement
According to the developers, make sure that your setup satisfies the requirements described [here](https://mqt.readthedocs.io/projects/qmap/en/latest/DevelopmentGuide.html).
We tried using a server using Ubuntu 22.04 and a desktop PC using Deepin 20.9.
In both of these cases, we don't need to do anything to satisfy the requirement.

## QMAP
NALAC depends on [QMAP](https://github.com/cda-tum/mqt-qmap).
The `CmakeLists.txt` in this directory fetches a fork of QMAP because we need to change a slight setting.
This commit is based on QMAP commit `fdbd4d227b144f0b31e8ff1f9a7a8fdd44cd5312`.
We just comment out [line 83](https://github.com/cda-tum/mqt-qmap/blob/fdbd4d227b144f0b31e8ff1f9a7a8fdd44cd5312/include/na/NAMapper.hpp#L83) in `mqt-qmap/include/na/NAMapper.hpp`.
In our paper, we assume the atoms are moved directly from the source location to the destination while in NALAC, they travel in a zig-zag way so that all movements are either horizontal or vertical.
By commenting out this line, NALAC also moves the atoms directly.

## Preprocessing of QASM Files
NALAC takes QASM files as input.
The files we used are not directly the files of the benchmark circuits, but rather proxy files that needs the same atom movements.
We explain how we generated them.
- The original QASM files are in `benchmarks/bench_original/`.
- We use Qiskit to redecompose the circuits into gate set 1Q gate and CZ. The conversion script is `benchmarks/rebase_cz.py`. The results are in `benchmarks/bench_ucz/`.
- We assumed arbitrary 1Q gates can be applied to any atom anywhere in our paper. In NALAC, they consider some constraints in applying 1Q gates. Thus, we replaced each 1Q gate to `rz(pi/2)` so that NALAC won't process them particularly. The script is `benchmarks/utorz.py` and the results are in `benchmarks/bench_using/`, which are the final input to NALAC we used.

## Building the Executable
In this directory, run `cmake -S . -B cmake-build-debug -DCMAKE_BUILD_TYPE=Debug` and then `cmake --build cmake-build-debug --parallel 2 --target mqt-qmap-na-test-exe`.
This should result in an executable `cmake-build-debug/src/mqt-qmap-na-test-exe`.
These instructions are given to us from the NALAC developers.
If you run into any issues, please contact them directly.

## Configuration Files
There are three architecture configuration files in `config/`: `myarch.json`, `myconfig.json`, and `mylayout.csv`.
Please check with NALAC developers if you are interested in the exact meaning of them.
We took some of their default settings and customized these files so that they represent the architecture specification in our paper.
The file `mylayout.py` is used to generate `mylayout.csv`.

## Generating the Data
Run the command in `test` in this directory.
It applies NALAC to the QASM files in `benchmarks/bench_using/` and saves the results in `benchmarks/result/` which are what we need in our paper.
Note that this command will overwrite the results now in the directory.

In these results, there are `load`, `move`, and `store` commands.
Each one has some starting 2D coordinates, followed by a `to` and then some finishing 2D coordinates.
