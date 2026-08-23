# ZAC native resident backend

This directory builds the deterministic C++17 resident-search backend used by
M3/M4.  Python owns QASM parsing and compiler state, then sends one typed,
contiguous boundary payload.  C++ owns normalization, ghost-safe decode, RETURN
matching, physical scoring, exact/GA search, stable selection and the bounded
decay heuristic in that single call.

The scorer-only nested and flat-v1 wires remain for migration tests.  Formal
resident calls use rich-boundary wire v2.  Its indexed mode resolves current,
gate-target and RETURN coordinates from a persistent `ArchitectureSnapshot`, so
repeated point/leg dictionaries never cross the language boundary.

Both methods carry the same geometric decay specification.  M3 sets
`max_horizon=0`, which makes future data structurally impossible.  M4 sets a
bounded maximum (currently eight).  Python deterministically replays the
visible geometry for STAY, each bounded RETURN site, and each current gate-site
choice; those replays include single-leg ghost avoidance, conflict coloring,
transfer, idle excitation, coherence, and terminal RETURN.  Their physical
marginals are passed as an auditable term table.  C++ applies
`alpha_lookahead * rho ** (offset - 1)` and stops terms when the bare decay
factor falls below epsilon.  Current physical NLL is never discounted or mixed
into the forecast breakdown.

For each chromosome, RETURN is a bounded joint assignment rather than one
nearest-site Hungarian result.  The solver enumerates the first K injective
assignments in deterministic cost order, performs any necessary derived RESEAT,
replays the real `back -> out` phases with single-leg ghosts as hard failures,
and only then compares candidates.  The selected assignment rank, evaluated
assignment count, rejected ghost assignments, geometric routing forecast, and
pre-score RESEAT count are returned in the boundary audit.

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
ABI version is `3`, flat scorer wire is `1`, rich-boundary wire is `2`, and RNG
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
