# Compiler Workspace Development Manual

This workspace is a collection of neutral-atom quantum compilation tools. The
folders are related, but they are not one monolithic Python package. A fresher
should think of the workspace as a small toolchain:

```text
input circuit / QASM
        |
        v
forward compiler
        |
        v
ZAIR hardware schedule JSON
        |
        +--> reverse compiler: rebuild a circuit and check semantics
        |
        +--> simulator: replay, validate, and visualize atom movement
        |
        +--> metrics / comparison against ZAC
```

The shared contract is the ZAIR schedule format. Most development work becomes
easier once you know which folder produces ZAIR, which folder consumes ZAIR,
and which hardware geometry that folder assumes.

## Project Map

| Folder | Role | Start here |
| --- | --- | --- |
| [Fable5_coded_compiler](Fable5_coded_compiler/) | The prompt-specific forward compiler. It compiles circuits into ZAIR while enforcing the crossed-AOD tone model and entanglement-zone reuse. This is usually the best place to study the latest compiler design. | [Fable5_coded_compiler/README.md](Fable5_coded_compiler/README.md), [Fable5_coded_compiler/fable_compiler/compiler.py](Fable5_coded_compiler/fable_compiler/compiler.py) |
| [100%_VIbe_Coding_Compiler](100%25_VIbe_Coding_Compiler/) | A fuller experimental forward compiler named `natam_compiler`. It includes placement, scheduling, movement batching, animation, ZAIR export, reverse-compiler adapters, verification, and benchmarks. | [100%_VIbe_Coding_Compiler/README.md](100%25_VIbe_Coding_Compiler/README.md), [100%_VIbe_Coding_Compiler/natam_compiler/compiler.py](100%25_VIbe_Coding_Compiler/natam_compiler/compiler.py) |
| [Reverse_Compiler](Reverse_Compiler/) | Consumes ZAIR schedules and reconstructs a physical-qubit circuit. It distinguishes hardware movement from quantum operations, so atom movement does not become fake `SWAP` gates. | [Reverse_Compiler/README.md](Reverse_Compiler/README.md), [Reverse_Compiler/reverse_compiler/reverse_compiler.py](Reverse_Compiler/reverse_compiler/reverse_compiler.py) |
| [Simulator](Simulator/) | Forward-replays a ZAIR schedule, validates each parallel step, and renders GUI/MP4/GIF visualizations of atoms, movement, gates, and conflicts. | [Simulator/README.md](Simulator/README.md), [Simulator/simulator/engine.py](Simulator/simulator/engine.py) |
| [ZAC-main](ZAC-main/) | The original ZAC compiler baseline and benchmark data. Treat it as the reference implementation/baseline, not as the main place for new prompt-specific code. | [ZAC-main/README.md](ZAC-main/README.md), [ZAC-main/run.py](ZAC-main/run.py) |
| [Examples](Examples/) | Scratch/example area. Use the package-specific `examples/` folders first when possible. | [Examples](Examples/) |

## The Main Concepts

### ZAIR schedule

ZAIR is the JSON-like hardware instruction format passed between projects. A
typical schedule has a top-level `instructions` list. The common instruction
types are:

| Type | Meaning |
| --- | --- |
| `init` | Initial atom placement. |
| `1qGate` | A parallel layer of single-qubit gates. |
| `rydberg` | A parallel layer of two-qubit entangling gates, usually CZ. |
| `rearrangeJob` | Parallel atom movement. This is hardware logistics, not a quantum gate. |
| `measure` | Measurement layer. |

Locations are generally encoded as `[id, array, row, col]`. The `id` is the
atom/qubit identifier in the default identity mapping. `array` identifies the
zone, commonly `0` for storage and `1` for entanglement.

### Hardware model

The projects model a zoned neutral-atom machine:

- storage zone: where idle atoms live,
- entanglement zone: where Rydberg CZ gates happen,
- AOD movement: transports atoms between sites/zones,
- Rydberg pulse: can execute multiple independent CZ gates in one parallel
  layer when atoms are positioned correctly.

Read each package's `hardware.py` before changing geometry. Some READMEs use
human-friendly descriptions such as `40 x 20`, but code always decides the
actual row/column convention.

### Crossed-AOD tone model

The Fable compiler, the newer `natam_compiler` movement planner, and the
simulator enforce the important physical rule that a 2D AOD tweezer array is a
product of X tones and Y tones:

- an X tone controls a movable column,
- a Y tone controls a movable row,
- atoms sharing a source X tone must share the same X motion,
- atoms sharing a source Y tone must share the same Y motion,
- extra X/Y intersections create ghost traps that must not collide with
  stationary atoms.

If a movement batch violates these rules, the planner should split it into
multiple `rearrangeJob` batches. If a schedule still violates them, the
simulator should report the invalid step.

## Development Environment

Use Python 3.12 if possible, because this workspace has been exercised in a
Python 3.12 environment. From the root:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install qiskit stim numpy pytest matplotlib pillow customtkinter imageio imageio-ffmpeg pandas scipy rustworkx
```

For exact ZAC reproduction, use [ZAC-main/requirements.txt](ZAC-main/requirements.txt). It pins older versions, so keep a separate virtual environment if those pins conflict with the other packages.

There is no top-level `pyproject.toml`; each folder is run directly with
`python -m ...` from inside that folder.

## First Commands To Run

Run the project tests independently:

```bash
cd Fable5_coded_compiler
python -m pytest tests -q

cd ../100%_VIbe_Coding_Compiler
python -m pytest tests -q

cd ../Reverse_Compiler
python -m pytest tests -q

cd ../Simulator
python -m pytest tests -q
```

Try the latest prompt-specific compiler:

```bash
cd Fable5_coded_compiler
python -m fable_compiler.cli ../100%_VIbe_Coding_Compiler/examples/ghz4.qasm --output ../Simulator/examples/fable_ghz4.json --metrics
```

Open that schedule in the simulator GUI:

```bash
cd ../Simulator
python run.py examples/fable_ghz4.json
```

Validate a schedule without the GUI:

```bash
python -m simulator.cli examples/fable_ghz4.json --check
```

Run the original ZAC baseline example:

```bash
cd ../ZAC-main
python run.py exp_setting/test.json
```

## End-To-End Workflows

### Compile a circuit with Fable

Use this when you want the current prompt-specific compiler:

```python
from qiskit import QuantumCircuit
from fable_compiler import compile_circuit, to_zair

qc = QuantumCircuit(3)
qc.h(0)
qc.cz(0, 1)
qc.cx(1, 2)
qc.measure_all()

result = compile_circuit(qc)
print(result.metrics.as_dict())
schedule = to_zair(result.program)
```

Important files:

- [Fable5_coded_compiler/fable_compiler/ir.py](Fable5_coded_compiler/fable_compiler/ir.py): converts Qiskit/QASM into the compiler's logical circuit.
- [Fable5_coded_compiler/fable_compiler/placement.py](Fable5_coded_compiler/fable_compiler/placement.py): initial atom layout.
- [Fable5_coded_compiler/fable_compiler/scheduler.py](Fable5_coded_compiler/fable_compiler/scheduler.py): logical gate layering.
- [Fable5_coded_compiler/fable_compiler/movement.py](Fable5_coded_compiler/fable_compiler/movement.py): crossed-AOD movement constraints.
- [Fable5_coded_compiler/fable_compiler/compiler.py](Fable5_coded_compiler/fable_compiler/compiler.py): core compile pipeline.
- [Fable5_coded_compiler/fable_compiler/zair.py](Fable5_coded_compiler/fable_compiler/zair.py): ZAIR export.

### Compile and verify with `natam_compiler`

Use this when you want the larger experimental compiler with verification and
animation helpers:

```python
from natam_compiler import compile_circuit, to_zair, verify
from natam_compiler.benchmarks import ghz

qc = ghz(5)
result = compile_circuit(qc)
assert verify(qc, result.program)
schedule = to_zair(result.program)
```

Important files mirror the Fable layout under
[100%_VIbe_Coding_Compiler/natam_compiler](100%25_VIbe_Coding_Compiler/natam_compiler/), with extra modules for verification, benchmarks, reverse compilation, and animation.

### Reverse compile ZAIR back into a circuit

Use [Reverse_Compiler](Reverse_Compiler/) when you need to prove that a hardware
schedule still represents the intended circuit:

```python
from reverse_compiler import reverse_compile, to_qiskit

ir = reverse_compile(schedule)
qc = to_qiskit(ir)
```

The core invariant is simple and crucial: an atom is bound to a physical qubit.
Moving the atom changes position only. It does not create a quantum operation.
Only explicit gate instructions create circuit operations.

### Simulate and visualize a schedule

Use [Simulator](Simulator/) to validate ZAIR step by step:

```python
from simulator import SimulationEngine, load_instructions

instructions = load_instructions("examples/fable_ghz4.json")
engine = SimulationEngine(instructions)
steps = engine.run()
assert all(step.validation.ok for step in steps)
```

The GUI entry point is:

```bash
cd Simulator
python run.py path/to/schedule.json
```

## How To Change The Code Safely

1. Identify the layer you are changing.
   - Circuit parsing or supported gates: edit `ir.py`.
   - Initial layout: edit `placement.py`.
   - Parallel logical ordering: edit `scheduler.py`.
   - Atom transport validity: edit `movement.py` and mirror simulator checks if needed.
   - Hardware schedule emission: edit `compiler.py` and `zair.py`.
   - Schedule replay semantics: edit reverse compiler or simulator, depending on direction.

2. Preserve ZAIR compatibility.
   - If a compiler emits a new field, make sure the simulator and reverse compiler either understand it or safely ignore it.
   - If a new instruction type is added, update docs and tests in every consumer.

3. Keep movement and validation in sync.
   - The compiler planner should avoid invalid movement batches.
   - The simulator should detect invalid batches if they appear.
   - Tests should cover both a feasible batch and at least one infeasible batch.

4. Run the narrowest relevant tests first.
   - Fable compiler change: `cd Fable5_coded_compiler && python -m pytest tests -q`
   - `natam_compiler` change: `cd 100%_VIbe_Coding_Compiler && python -m pytest tests -q`
   - reverse semantics change: `cd Reverse_Compiler && python -m pytest tests -q`
   - simulator validation/render model change: `cd Simulator && python -m pytest tests -q`

## Testing Checklist

Before considering a compiler change done, try to answer these questions:

- Does the package test suite pass?
- Does the compiler still emit a valid ZAIR schedule?
- Can the simulator load and validate that schedule?
- If the change affects quantum semantics, can the reverse compiler reconstruct the expected circuit?
- Did you update the relevant README if public behavior, commands, or formats changed?

## Common Pitfalls

- Do not treat atom movement as a quantum `SWAP`. Movement changes where an atom is; it does not exchange quantum states.
- Do not hand-edit ZAIR location order casually. The common encoding is `[id, array, row, col]`.
- Do not change geometry in only one place. Compiler, simulator, and reverse tooling must agree on locations.
- Do not assume each atom has an independently steerable 2D tweezer. The crossed-AOD tone model creates shared motion and ghost traps.
- Do not run all projects from the root and expect imports to work automatically. Most commands assume you `cd` into the target project folder.
- Do not use the original ZAC dependency pins blindly in the same environment as newer tools unless you intend to reproduce ZAC exactly.

## Suggested Reading Order For A Fresher

1. Read this file once to understand the workspace shape.
2. Read [Fable5_coded_compiler/README.md](Fable5_coded_compiler/README.md) for the current compiler target.
3. Read [Simulator/README.md](Simulator/README.md) to learn what makes a schedule valid or invalid.
4. Read [Reverse_Compiler/README.md](Reverse_Compiler/README.md) to understand the quantum semantics of replay.
5. Skim [ZAC-main/README.md](ZAC-main/README.md) to understand the baseline and publication context.
6. Step through [Fable5_coded_compiler/tests/test_fable_compiler.py](Fable5_coded_compiler/tests/test_fable_compiler.py) and [Simulator/tests/test_engine.py](Simulator/tests/test_engine.py). Tests are the quickest executable explanation of expected behavior.
