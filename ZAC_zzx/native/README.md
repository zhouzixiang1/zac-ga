# ZAC native resident backend

This directory builds the deterministic C++17 resident-search backend used by
M3/M4.  Python owns QASM parsing and compiler state, then sends one typed,
contiguous boundary payload.  C++ owns normalization, ghost-safe decode, RETURN
matching, physical scoring, future-layer rollout, exact/GA search and stable
selection in that single call.

The scorer-only nested and flat-v1 wires remain for migration tests.  Formal
resident calls use rich-boundary wire v3.  Its indexed mode resolves current,
gate-target and RETURN coordinates from a persistent `ArchitectureSnapshot`, so
repeated point/leg dictionaries never cross the language boundary.

Both methods carry the same geometric decay specification.  M3 sets
`max_horizon=0`, which makes future data structurally impossible.  M4 sends
only the bounded future 2Q atom ledger (at most eight layers).  C++ starts from
each candidate's real post-boundary positions, selects future gate pairs,
parks endpoint and single-leg ghost blockers, replays the phases, performs
terminal RETURN, and scores transfer, idle excitation and coherence.  It then
applies `alpha_lookahead * rho ** (offset - 1)` and stops when the bare decay
factor falls below epsilon.  Python no longer expands per-candidate forecast
terms on the formal path.

The persistent architecture snapshot contains every entangling pair and
storage site once.  A boundary therefore crosses Python/C++ only once with
indexed current geometry, current gate domains, RETURN domains and raw future
atom pairs.  The final executable trace is still replayed by the independent
Python verifier, so compiler and verifier do not share one implementation.

For each chromosome, RETURN is a bounded joint assignment rather than one
nearest-site Hungarian result.  The solver enumerates the first K injective
assignments in deterministic cost order, performs any necessary derived RESEAT,
replays the real `back -> out` phases with single-leg ghosts as hard failures,
and only then compares candidates.  The selected assignment rank, evaluated
assignment count, rejected ghost assignments, geometric routing forecast, and
pre-score RESEAT count are returned in the boundary audit.

Search uses one total unique-fitness budget shared by greedy seeding,
crossover/mutation and local polishing.  A normalized direct space of at most
`direct_enumeration_limit` (512 by default) is exhaustively enumerated when it
also fits that budget.  Larger spaces use separate gate/residency crossover,
joint high-cost-gate plus related-RETURN mutation, duplicate-free populations,
deterministic elites and up to `local_polish_sweeps` single-gene improvement
passes.  Cache on/off changes evaluation reuse only; winner, mapping, movement
batches and RNG state remain identical for a fixed seed.

The old forecast-term bitset path remains only for differential regression
fixtures.  Formal ABI4 runs use raw future layers and fail if raw layers and
precomputed terms are mixed.

The extension is fail-closed: `zzx.native_backend.NativeResidentBackend` raises
when the wheel is missing, ABI/RNG differs, or a required registered wheel hash
does not match the actually loaded binary.  `register_native_wheel()` verifies
the extension bytes inside the wheel before writing the local registration.

Build and test in an isolated environment:

```sh
python -m pip install build
python -m build --wheel -o /tmp/zac-native-dist \
  -Cbuild-dir=/tmp/zac-native-wheel-build
python -m pip install --force-reinstall /tmp/zac-native-dist/zac_native-*.whl
cmake -S . -B /tmp/zac-native-ctest -DBUILD_TESTING=ON
cmake --build /tmp/zac-native-ctest --config Release
ctest --test-dir /tmp/zac-native-ctest --output-on-failure
```

The primary build intentionally uses neither OpenMP nor fast-math.  The public
ABI version is `4`, flat scorer wire is `1`, rich-boundary wire is `3`, and RNG
semantics are `python-random-mt19937-v1`.

## Formal build freeze

`experiments_v2.native_build_freeze` is the only supported promotion path from
a registered wheel to a formal frozen build.  It must run after the native
sources have been committed and the repository is clean.  `attest` records a
`tracked-tree-v1` digest (stable JSON over sorted native-relative build-input
paths and their SHA256 values) together with the byte-verified registered
wheel/extension and Release compiler flags.  `freeze` replays that attestation,
requires wheel-bound microbenchmark, real-boundary parity, and H0/H8
full-pipeline evidence, then atomically writes `artifact_status=frozen`.

This is a build attestation, not a reversible or reproducible-build proof.  A
legacy candidate without the declared `tracked-tree-v1` algorithm cannot be
promoted in place.  Run `python -m experiments_v2.native_build_freeze --help`
for the exact command-line contract.
