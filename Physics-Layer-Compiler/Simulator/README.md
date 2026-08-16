# Forward Neutral-Atom Quantum Circuit Simulator

A GUI simulator that takes a **compiler-generated instruction list** and
forward-simulates it on a zoned neutral-atom architecture — animating parallel
atom movement and parallel gate layers, tracking every atom's identity, and
validating each time step. Visually consistent with the sibling Reverse
Compiler interface.

## Run

```bash
python run.py                       # GUI with the built-in demo
python run.py path/to/schedule.json # GUI loaded with a compiler schedule
```

Command line:

```bash
python -m simulator.cli --demo --check          # validate the demo
python -m simulator.cli schedule.json --check   # validate a schedule (exit 1 if invalid)
python -m simulator.cli schedule.json --mp4 out.mp4
```

## Input format

The simulator consumes the **exact ZAIR schedule** emitted by `natam_compiler`
(and understood by the reverse compiler) — no new format:

| `type`         | meaning                          | key fields |
|----------------|----------------------------------|------------|
| `init`         | initial layout                   | `init_locs = [[id, a, r, c], ...]` |
| `rearrangeJob` | parallel atom movement (no gate) | `begin_locs`, `end_locs`, `aod_qubits` |
| `1qGate`       | parallel single-qubit layer      | `gates = [{name, q, params?}]` |
| `rydberg`      | parallel two-qubit CZ layer      | `gates = [{q0, q1, name?}]` |
| `measure`      | measurement                      | `qubits = [...]` |

Everything grouped inside one instruction is one **parallel time step**.
Locations are `[id, array, row, col]` with `array 0 = storage`, `array 1 = entanglement`.

## Hardware layout

Geometry matches the compiler's ZAIR output exactly, so compiled schedules
simulate with no translation:

* **Storage zone** — 40 rows × 20 columns (array 0).
* **Entanglement zone** — 2 rows × 20 columns (array 1), sharing the storage
  column axis and drawn directly above the storage zone.
* A **Rydberg (CZ) pair** is the two atoms in the **same column** of the
  entanglement zone, one in row 0 and one in row 1 — up to 20 parallel pairs.
* Storage indices are assigned column by column (`atom k → col = k // 40,
  row = k % 40`).

## End-to-end example (circuit → compiler → simulator)

```bash
python examples/pipeline_30q.py                 # 30 qubits, 50 gates
python examples/pipeline_30q.py --mp4 out.mp4   # also render the animation
python run.py examples/demo30.json              # open the result in the GUI
```

The example builds a 30-qubit / 50-gate circuit, compiles it with
`natam_compiler` (the "vibe coding" compiler), saves the ZAIR schedule to
`examples/demo30.json`, then forward-simulates and validates every parallel
step. The GUI's **From Compiler** button does the same for a built-in circuit.

## Simulation & validation

Parallel movements are animated **simultaneously**; atom identities are
preserved throughout. Each parallel move step is validated for:

* **destination conflict** — two moving atoms target the same site,
* **atom overlap** — a moving atom lands on a stationary atom,
* **path collision** — two simultaneously-moving atoms pass too close,
* **unsupported simultaneous move** — a single AOD batch mixes source/target
  zones or exceeds the AOD atom capacity (parallel min-separation / crossing
  conflicts are caught by the path-collision check).

Gate layers are validated for parallel consistency (no atom reused, valid
Rydberg column pairing). The full atom position/state is retained after every
step.

## GUI

* **Left** — instruction timeline, parallel operations grouped into one step,
  each showing its validation status.
* **Center** — animated storage + entanglement zones with a playback bar:
  reset, previous, play/pause, next, fit view, playback speed, jump-to-step.
* **Right** — selected step details: atom IDs, start/end coordinates, gate
  type, parallel-group size, and validation status/errors.
* Moving atoms, trajectories, active single-qubit gates, active two-qubit
  pairs and validation conflicts are visually distinguished.
* **Export MP4** (and GIF) renders the full animation headlessly.

## Modules

```
simulator/
  hardware.py     zone dimensions, Site, indexing, coordinate mapping
  schema.py       parse/classify the compiler ZAIR instruction list
  engine.py       forward-replay + parallel-step validation (the core)
  frames.py       engine steps -> renderable SceneSpecs
  renderer.py     matplotlib scene drawing + interactive canvas
  video_export.py headless MP4 / GIF export via FFmpeg / Pillow
  sample.py       built-in demo schedule + optional compiler bridge
  app.py          three-pane CustomTkinter GUI
  cli.py          command-line entry point
```

## Tests

```bash
python -m pytest tests/ -q
```
