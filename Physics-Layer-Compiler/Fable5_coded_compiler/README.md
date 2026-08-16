# Fable Neutral-Atom Compiler

Fable is a compact hardware-aware compiler for the prompt in this folder. It
maps a Qiskit/OpenQASM-style circuit onto a zoned neutral-atom schedule with a
40 x 20 storage zone and a 2 x 20 entanglement zone placed to the right of
storage.

The compiler emits ZAIR-compatible JSON instructions (`init`, `1qGate`,
`rearrangeJob`, `rydberg`, `measure`) so schedules can be inspected with the
workspace simulator and reverse compiler tooling.

## Hardware model

- Storage zone: 40 columns x 20 rows, array id `0`.
- Entanglement zone: 2 columns x 20 rows, array id `1`, to the right of
  storage.
- Storage atom index `k` is column-major from the storage column nearest the
  entanglement zone toward the farthest column.
- CZ gates run only in the entanglement zone. A CZ pair occupies the two
  entanglement columns at the same row, so up to 20 CZ gates can share one
  Rydberg pulse.

## AOD movement model

Each movement batch is checked as a crossed 2D AOD tone product:

- one X tone drives an entire movable column,
- one Y tone drives an entire movable row,
- atoms sharing an X tone must have identical X motion,
- atoms sharing a Y tone must have identical Y motion,
- all extra tone-product intersections are ghost traps and are checked against
  stationary atoms.

Incompatible moves are split into later `rearrangeJob` batches.

## Usage

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

Command line:

```bash
python -m fable_compiler.cli input.qasm --output schedule.json --metrics
```

## ZAC comparison

Run the HPCA-suite head-to-head comparison against shipped ZAC results with:

```bash
python examples/compare_zac.py
```

The script timestamps Fable ZAIR schedules with ZAC's simulator cost model and
compares them to `ZAC-main/result/zac/tech_eval/qasm_sa_1000_reuse`. Current
headline result on 18 circuits:

- geomean Fable/ZAC time ratio: `0.699`
- mean fidelity: ZAC `0.4781`, Fable `0.5057`
- remaining worst outlier: `ising_n98` at `7.04x`, mainly because the ZAC
  reference architecture uses a much larger entanglement region than the prompt
  hardware (`7 x 20` SLMs versus Fable's required `2 x 20`).

## Tests

```bash
python -m pytest tests -q
```