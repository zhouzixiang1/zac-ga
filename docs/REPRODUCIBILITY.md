# Reproduction guide

Current-source execution, historical artifact integrity, paper rendering and
full experiment recomputation answer different questions. This guide does not
equate them.

## Current-source build and smoke

Use CPython 3.10–3.12, CMake 3.20+ and a C++17 toolchain. Python packages are
downloaded into a **new** environment; no system packages or existing
environments are modified.

```bash
python3 scripts/portable_reproduce.py bootstrap --name runtime-v1
python3 scripts/portable_reproduce.py smoke --runtime runtime-v1 --name smoke-v1
```

The build uses [direct dependency pins](../scripts/requirements-current-source.txt),
two native build workers and single-thread numerical libraries during smoke.
The complete resolved package set, interpreter/platform, source hashes and
wheel/extension identity are recorded in
`IEEE_conference_template/build/portable/runtime-v1/`. This is not an offline,
fully hash-locked or bitwise cross-platform build guarantee.

`current_config.json` copies the public example and rebinds its wheel identity
and output directory. Smoke removes explicit initializer fields before calling
the public parser, then requires the actual default to be physical-prefix with
H=2, K=4, rho=0.7 and 32 rollout evaluations. No private paper wrapper is used.
This verifies the public source default, not the current manuscript
configuration: the latter explicitly selects `physical_prefix_ga` with
`init_engine=ga`.

For a current-source run with GA initialization, make a separate copy of the
generated configuration, set `init_strategy=physical_prefix_ga` and
`init_engine=ga`, and remove `initial_lookahead`, `init_pop` and `init_gens`.
The parser accepts initialization controls under `initial_ga`. Its defaults
match the main study's initialization budget: H=2, rho=0.7, population 8, two
elites, crossover probability 0.25, at most 32 unique mappings, six generations,
three non-improving generations and 320 proposals. The physical prefix has an
independent inner budget of 32 encoding evaluations and disables inner
look-ahead. Dynamic H=8 settings remain separate. See
[the configuration class](../ZAC_zzx/zzx/physical_initial_ga.py) and the frozen
[main-study protocol](../ZAC_zzx/results/physical_ga_main_v1/protocol.json).
Selecting those controls on a new wheel does not recreate the historical
native/runtime closure, and the default-checking smoke deliberately does not
serve as a benchmark driver for this alternative.

The actual ZAIR must pass replay, physical and zero-ghost checks, as well as
QASM-derived per-qubit 1Q/CZ ordering. This last check is not a unitary matrix
equivalence proof. Model parameters and scores are retained; an out-of-domain
score fails the smoke. Timing is diagnostic, not a performance claim.

Existing run names, symbolic-link escape paths, changed source and mismatched
native bytes are rejected. Failed attempts stay in place. Fix the cause and use
a new name; none of these commands changes accepted results.

## Clean source export

To demonstrate independence from ignored source trees and installed runtimes:

```bash
python3 scripts/portable_reproduce.py export --name fresh-export-v1
cd IEEE_conference_template/build/portable/fresh-export-v1/source
python3 scripts/portable_reproduce.py bootstrap --name runtime-v1
python3 scripts/portable_reproduce.py smoke --runtime runtime-v1 --name smoke-v1
python3 scripts/portable_reproduce.py audit-artifacts
```

The allowlisted export contains source, toy QASM, architecture, the original
accepted results, manuscript source formats and the six registered companion
publication files used by the active paper. The current bundle is
`ZAC_zzx/results/physical_ga_main_v1/paper_exports/`; the previous
`default_initial_v1` bundle remains supported for historical manuscript inputs.
The current six files are `ga_main_values.json`, `ga_main_values.tex`,
`main_rows.csv`, `analysis_units.csv`, `mechanism.csv` and
`representative_cases.tex`. The export excludes `.git`, build products,
environments, archives, historical raw runs and third-party PDFs.
`portable_source_manifest.json` records relative paths and SHA-256 digests.
Uncommitted source is identified by content, not mislabeled as a published Git
release. Keep the upstream license notice included in the export.

## Audit distributed historical artifacts

```bash
python3 scripts/portable_reproduce.py audit-artifacts
```

This read-only check resolves the 14 processed files relative to
`ZAC_zzx/results/paper_zh_v2/final_manifest.json`. It checks bytes and sizes without
following machine-specific raw-run paths. It does not rerun compilers, verify
every original trace or independently re-establish all statistical claims.
The published commit/release identity is the external trust anchor; checksums
are not signatures.

The current bilingual main results use the separate `physical_ga_main_v1`
publication bundle. Source export carries the six companion files needed for
the paper and records their hashes; `audit-artifacts` still checks only the
original 14-file accepted package. Neither check replaces the author's strict
raw-evidence audit of the new study. The full current bundle includes
provenance, per-run outcomes, failures and model-domain exclusions; see the
[data inventory](DATA_AND_CODE.md). The original accepted package and previous
`default_initial_v1` exports remain unchanged.

## Render the bundled paper

The source-only renderer is separate from the author's strict `make paper`
pipeline. The strict pipeline retains its raw-evidence and frozen-environment
requirements; absence of these dependencies is never silently waived.

```bash
python3 scripts/render_bundled_paper.py --name render-v1
```

Install XeLaTeX, BibTeX and Poppler (`pdfinfo`) first. Sources use TeX Gyre,
Fandol, TikZ/PGFPlots, IEEEtran and their declared LaTeX packages. The renderer
copies the sources into a new `IEEE_conference_template/build/bundled-render/`
directory, records bundled macro hashes, carries the six registered publication
inputs, adapts their paths and the generated framework figure (current Fig. 2),
and builds the figure, bibliography and Chinese main PDF. It checks the
source-export hashes
when present. It never runs numerical generators or replaces original sources.
The report says `historical_evidence_validated=false`; visual inspection and
scientific QA remain separate. Consult the dated [validation record](PORTABLE_VALIDATION.md)
for the exact source and tested scope, rather than treating an earlier render as
validation of later changes. This source-only helper currently builds Chinese
only; the English manuscript is aligned, but its author-side build is
`make paper-en`, with shared Chinese and historical-evidence checks.

This local source-only export is separate from the minimal Overleaf export
prepared by `scripts/prepare_overleaf_sync.py`. Overleaf adapts registered
figure/data inputs without changing desktop source paths. The 2026-09-20
synchronization was verified at `5cd7151`; this is a dated record, not a claim
about subsequent online edits. See
[Overleaf synchronization](REPOSITORY_MAP.md#overleaf-同步).

Paper generators, verifiers and tests live in `scripts/paper/`. Writing notes,
provenance metadata, retired figures/tables and template references live in
`docs/paper/`. The manuscript directory contains only required source inputs,
one README and ignored `build/` outputs. Framework PDFs are built independently
for Chinese and English before the corresponding main paper. The current
clean, anonymous layout is six English body pages plus one references page;
Chinese uses six pages with natural pagination.

## Recompute experiments

The toy demo is not the ZAC18/QMAP154 benchmark matrix. Full recomputation needs:

1. Exact original benchmark revisions and canonical QASM identities.
2. Qiskit 1.2.4 and the registered normalization parameters; the maintained
   transform is `ZAC_zzx/experiments_v2/canonicalize.py`.
3. Explicit compiler settings and a newly sealed output protocol.
4. The appropriate native runtime and verifier. Historical accepted runs
   require their ABI8/ABI9 identities, not a new wheel labeled with old hashes.
5. ZAC and the specific routing-aware QMAP source/patch/environment for baseline
   comparisons. The current physical-GA main study reuses the declared baseline
   records; it does not relabel newly compiled baselines as the accepted runs.
6. Every outcome, including failures/timeouts, with the prescribed cohort and
   scoring rules. Do not replace failed inputs with aliases or tune on validation.

### Obtain and normalize the exact inputs

The distributed [172-file acquisition manifest](benchmark_acquisition_manifest.json)
records original and canonical SHA-256 values, Qiskit 1.2.4, opt-level 3 and
transpiler seed 0. Historical input records did not record upstream commits;
this absence is explicit. A locator ref is not falsely presented as a historic
revision. Both raw and normalized bytes must match the content pins.

```bash
python3 scripts/acquire_benchmarks.py              # read-only preview
IEEE_conference_template/build/portable/runtime-v1/venv/bin/python -B \
  scripts/acquire_benchmarks.py --execute --name inputs-v1 --download \
  --zac-ref main --qmap-ref main
```

This obtains QASM and license notices only from the official
[ZAC](https://github.com/UCLA-VAST/ZAC) and
[QMAP](https://github.com/munich-quantum-toolkit/qmap/tree/main/examples)
repositories. If upstream changes, the hashes reject the result. Specify a
known upstream ref containing the matching bytes; do not change expected hashes
to make a mismatch pass. All files land under manuscript-local
`build/benchmark-inputs/inputs-v1/`. Download mode retains upstream notices.
The complete network acquisition still needs a live end-to-end availability
check; a working URL interface does not guarantee future upstream availability.

If original source directories have already been obtained legitimately, use
`--source-zac /path/to/ZAC/benchmark/hpca --source-qmap /path/to/qmap/examples`
instead of `--download`. The normalization/check pipeline is the same, and
outputs are new files; originals are never modified. Source-directory mode does
not confer redistribution rights.

The local-source route was checked for all 18 ZAC and 154 QMAP circuits: both
original and regenerated canonical hashes matched the acquisition manifest.
This tested input preparation, not a new compiler experiment. The complete
network-download route has not yet received the same end-to-end check; see the
[validation record](PORTABLE_VALIDATION.md).

### Prepare the current default on the suite

```bash
python3 scripts/prepare_current_suite.py --runtime runtime-v1 \
  --inputs inputs-v1 --name current-zac18-s0 --dataset zac18 --seed 0
```

This verifies all canonical inputs and the built runtime, then creates an
immutable configuration/protocol and prints the regular `ZAC_zzx/run.py`
command. It **does not run** the benchmark. `--dataset qmap154` or `all` selects
the other fixed queue. The public frontend is fail-fast, not a fault-tolerant
formal coordinator; every emitted trace needs independent verification against
its corresponding canonical QASM. A current-default run uses `physical_prefix`,
so it is neither a replay of the
current paper's explicit GA initialization nor the original accepted historical
configuration. Preparation never promotes results.

The current manuscript main study is defined by
`ZAC_zzx/experiments_v2/physical_ga_main_study.py` and its frozen protocol under
`ZAC_zzx/results/physical_ga_main_v1/`; the independent initialization pilot has
a separate corrected-driver protocol. Their reports preserve failed and
out-of-domain outcomes and distinguish parallel quality runs from historical
serial timing. Full benchmark inputs, raw traces and frozen environments are
local dependencies rather than a complete public deposit.

Historical formal drivers remain under `ZAC_zzx/experiments_v2/`. Their
machine-bound native/baseline/runtime closure and a fully portable bounded
multi-method statistical pipeline remain separate release work. See the
[availability inventory](DATA_AND_CODE.md).

## Troubleshooting

| Failure | Action |
|---|---|
| Python/CMake/compiler unsupported | Install the declared platform prerequisites first |
| Package download/resolution error | Inspect `install-dependencies.log`; fix access and choose a new runtime name |
| Native build error | Retain `build-native.log` with compiler/platform information |
| Wheel hash mismatch | Use the new `current_config.json`; never falsify a frozen hash |
| Source drift | Create a new bootstrap; old receipts stay bound to old source |
| Smoke failure or timeout | Inspect `compiler.log` and retained trace; do not silently retry |
| Strict paper check lacks evidence | Obtain the declared closure or explicitly use source-only rendering |

Only newly generated material inside named build directories belongs to this
workflow. Current experiment inputs, frozen environments and evidence are not
ordinary cleanup targets. The separately authorized
[2026-10-01 cleanup](repository-maintenance-20261001.md) removed a fixed list of
obsolete payloads; it does not authorize further deletion. The current strict
paper checks and one frozen GA-initialization/H8 circuit replay passed after
deletion. This is not a full-matrix rerun, and old runs whose traces were removed
can no longer be physically revalidated from their saved scores alone. See the
[development history](EXPERIMENT_HISTORY.md) for retained outcomes and next steps.
