# Neutral-Atom Hardware-Aware Compiler (`natam_compiler`)

A complete, modular, runnable compiler that maps a quantum circuit (Qiskit or
OpenQASM) onto an executable **hardware-operation sequence** for a *zoned
neutral-atom* quantum computer, plus an automated **reverse compiler** that
reconstructs the circuit and proves functional equivalence.

```
input circuit (Qiskit / QASM)
        │
        ▼
  ┌─────────────────────────────────────────────────┐
  │  natam_compiler  (forward compiler)              │
  │  IR → placement → scheduling → routing/movement  │
  └─────────────────────────────────────────────────┘
        │  HardwareProgram  ──►  ZAIR schedule (JSON)
        ▼                              │
  executable op sequence               ▼
  (moves, 1q, CZ, measure)   ┌──────────────────────────────┐
        │                    │ Reverse_Compiler package      │
        └──────── verify ───►│ (sibling folder) → Qiskit/Stim │
                             └──────────────────────────────┘
```

Reverse compilation and round-trip verification are delegated to the standalone
**`Reverse_Compiler`** package in the sibling `../Reverse_Compiler` folder — the
forward compiler emits a ZAIR schedule that that package consumes.

---

## Hardware model

| Zone          | Size                | Role                          |
|---------------|---------------------|-------------------------------|
| Storage       | 40 × 20 (800 sites) | idle atoms are parked here    |
| Entanglement  | 2 × 20 (40 sites)   | Rydberg CZ gates execute here |

Two atoms placed in the same **column** of the 2-row entanglement zone (rows 0
and 1) form one interaction site, so up to **20 CZ gates run per Rydberg
pulse**. Atoms are transported between zones by AOD moves. Timing and fidelity
parameters (`HardwareSpec`) are representative and fully overridable.

---

## Architecture / pipeline

| Stage | Module | What it does |
|-------|--------|--------------|
| Front-end / IR | [ir.py](natam_compiler/ir.py) | Transpile to the universal basis `{rz, sx, x, cz}`; Clifford inputs stay Clifford-exact (enables Stim verification). |
| Placement | [placement.py](natam_compiler/placement.py) | Pair-aware layout: heaviest-interacting qubit pairs parked as vertical dominoes (same column, adjacent rows) closest to the entanglement zone. |
| Scheduling | [scheduler.py](natam_compiler/scheduler.py) | ASAP list scheduling into parallel time slots. |
| Routing / movement | [compiler.py](natam_compiler/compiler.py) | Transport CZ pairs into the nearest free entanglement columns (order-preserving assignment DP, ≤20 per pulse), fire CZ, return home. |
| AOD movement planning | [movement.py](natam_compiler/movement.py) | Pack transfers into collision-free parallel batches respecting AOD limits (separation, crossing, capacity, kinematics). |
| Animation | [animation.py](natam_compiler/animation.py) | Replay the compiled program as an MP4/GIF showing parallel moves, gates and constraint violations. |
| Hardware model | [hardware.py](natam_compiler/hardware.py), [operations.py](natam_compiler/operations.py) | `HardwareSpec`, `HardwareProgram`, `Stage`, op data classes. |
| ZAIR export | [zair.py](natam_compiler/zair.py) | `HardwareProgram` → ZAIR schedule dict. |
| Reverse compiler | [reverse.py](natam_compiler/reverse.py) | Adapter onto the sibling `Reverse_Compiler` package. |
| Verification | [verify.py](natam_compiler/verify.py) | Qiskit-unitary (small) or Stim-tableau (large Clifford) equivalence. |
| Metrics | [metrics.py](natam_compiler/metrics.py) | Time, movement distance, parallelism, fidelity. |
| Benchmarks | [benchmarks.py](natam_compiler/benchmarks.py) | Circuit generators + harness. |
| CLI | [cli.py](natam_compiler/cli.py) | `compile` / `benchmark` / `hardware`. |

### Optimization objectives

* **Correctness** — verified by automated round-trip equivalence.
* **Execution time** — ASAP scheduling + parallel moves and Rydberg pulses.
* **Movement distance** — interaction-aware placement minimizes transport.
* **Parallelism** — up to 20 simultaneous CZ gates per pulse; 1q gates batched per slot.
* **Estimated fidelity** — per-stage product of gate / transfer / decoherence factors.

---

## Installation

Requires `qiskit`, `stim`, `numpy`, `pytest` (already present in this environment).
No build step — it is a plain Python package. The reverse compiler is found
automatically; override its location with `NATAM_REVERSE_COMPILER_PATH` if needed.

---

## API usage

```python
from natam_compiler import compile_circuit, verify, to_zair
from natam_compiler import reverse as rev
from natam_compiler.benchmarks import ghz

qc = ghz(5)

# Forward compile -> executable hardware-operation sequence
result = compile_circuit(qc)
print(result.program.to_text())     # human-readable op sequence
print(result.metrics.as_dict())     # performance metrics
zair = to_zair(result.program)      # JSON-serializable schedule

# Reverse compile (via the Reverse_Compiler package)
qiskit_circ = rev.to_qiskit(result.program)
stim_circ   = rev.to_stim(result.program)     # Clifford circuits only

# Automated equivalence verification
assert verify(qc, result.program)
```

### Custom hardware

```python
from natam_compiler import HardwareSpec, compile_circuit
hw = HardwareSpec()
hw.t_move_per_site = 0.8          # faster transport
hw.f_2q = 0.997                   # better CZ fidelity
result = compile_circuit(qc, hw)
```

### AOD movement constraints

Parallel atom transport respects the acousto-optic-deflector (AOD) limits below.
The router packs transfers into the fewest **collision-free parallel batches**,
maximizing parallelism while honouring every constraint:

```python
hw = HardwareSpec()
hw.aod_max_atoms = 20           # max atoms moved simultaneously per batch
hw.min_atom_separation = 1.0    # trajectories never come closer than this
hw.aod_max_distance = 60.0      # max travel per move (site units)
hw.aod_max_velocity = 1.0       # sites / us
hw.aod_max_acceleration = 1.0   # sites / us^2 (smooth trapezoidal profile)
hw.aod_max_move_duration = 1000 # max move duration (us)
hw.allow_trap_crossing = False  # preserve atom ordering (no crossing)
result = compile_circuit(qc, hw)
print(result.metrics.num_move_batches, result.metrics.move_parallelism)
```

Batches never cross, collide, breach the minimum separation, or exceed capacity;
incompatible moves are split into separate batches
(see [movement.py](natam_compiler/movement.py): `plan_batches`, `moves_compatible`,
`validate_move`, `diagnose_batch`).

### Animating a compilation

```python
from natam_compiler import animate_compilation, compile_circuit, default_hardware
from natam_compiler.benchmarks import ghz

result = compile_circuit(ghz(5))
animate_compilation(
    result,
    default_hardware(),
    output_path="ghz5.gif",   # or "ghz5.mp4" (needs ffmpeg)
    format="gif",
    fps=30,
)
```

The animation shows initial/intermediate/final positions, draws all atoms of a
movement batch in the same frame, colour-codes stationary / moving / active-gate
atoms, highlights invalid trajectories and minimum-distance violations, and
prints the current timestep, movement batch and operation duration. Omit
`output_path` to get back a live `FuncAnimation` with playback controls.

### Compiler / reverse-compiler APIs

| Function | Purpose |
|----------|---------|
| `compile_circuit(circuit, hardware=None, optimization_level=1) -> CompileResult` | Forward compile (accepts `QuantumCircuit`, QASM string, or `LogicalCircuit`). |
| `Compiler(hardware).compile(circuit)` | Object-oriented entry point. |
| `to_zair(program) -> dict` | Export the ZAIR hardware schedule. |
| `reverse.to_qiskit(program)` / `reverse.to_stim(program)` | Reverse compile via `Reverse_Compiler`. |
| `verify(original, program=None, prefer="auto") -> VerificationResult` | Round-trip equivalence check. |

---

## Command line

```bash
# Compile a QASM file, print metrics, and verify
python -m natam_compiler.cli compile examples/ghz4.qasm --verify --text

# Also write the reverse-compiled circuit back out as QASM
python -m natam_compiler.cli compile examples/ghz4.qasm --reverse-qasm out.qasm

# Run the benchmark suite
python -m natam_compiler.cli benchmark

# Print the hardware spec
python -m natam_compiler.cli hardware
```

---

## Benchmarks & metrics

`python examples/demo.py` (or the CLI `benchmark` command) produces:

```
circuit          q  stages    time(us)      move    2q    par   fidelity  verify
--------------------------------------------------------------------------------
ghz_5            5      28       272.7     169.0     4   1.00    0.96513  OK (qiskit-unitary)
ghz_10          10      58       632.9     401.5     9   1.00    0.92495  OK (qiskit-unitary)
qft_5            5     147      1696.9    1106.9    26   1.04    0.81141  OK (qiskit-unitary)
bv_6             6      18       202.7     130.2     3   1.00    0.97288  OK (qiskit-unitary)
linear_8         8      40       406.6     842.6    21   3.50    0.84159  OK (qiskit-unitary)
clifford_6       6      78       960.3     652.2    15   1.07    0.88647  OK (qiskit-unitary)
clifford_20     20     799     11783.1    9097.0   205   1.22    0.22485  OK (stim-tableau)
```

Columns: `stages` = scheduled hardware stages, `time` = estimated execution
time, `move` = total transport distance, `2q` = CZ count, `par` = average CZ
gates per Rydberg pulse, `fidelity` = estimated success probability.

---

## Tests

```bash
python -m pytest tests/ -q
```

The suite covers the hardware model, compilation structure, ZAIR export, QASM
input, metrics, and — most importantly — **automated round-trip equivalence**
for GHZ, QFT, Bernstein–Vazirani, linear-entangler, random Clifford (Stim path)
and a non-Clifford circuit (unitary path).

---

## Assumptions

* Native two-qubit gate is the Rydberg **CZ**; CX/others are decomposed to CZ + 1q.
* One atom per physical qubit; atom id ≡ qubit id (identity binding in ZAIR).
* Single-qubit gates and measurements are performed locally at the atom's home site.
* Each CZ transports its pair into the entanglement zone and back (correct and
  simple); keeping atoms resident across consecutive CZ layers is a natural
  future optimization.
* Timing/fidelity constants are representative defaults and are configurable.

---

## Original problem statement

> Develop and optimize a hardware-aware compiler for a neutral-atom quantum
> computer with a 40×20 storage zone and a 2×20 entanglement zone, gates
> executed in the entanglement zone, and atoms movable between zones. Input is a
> Qiskit/OpenQASM circuit; output is an executable hardware-operation sequence
> (moves, 1q gates, 2q gates, measurements). Optimize for correctness,
> execution time, movement distance, parallelism, and estimated fidelity. Also
> implement a reverse compiler that converts the hardware sequence back into a
> Qiskit/Stim circuit and use it to automatically verify functional equivalence.
