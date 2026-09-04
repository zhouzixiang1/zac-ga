# ZAC_zzx: GA-LK compiler and experiment pipeline

`ZAC_zzx` contains the current GA-LK implementation used by the manuscript on
zoned neutral-atom compilation. All maintained development and experiment
history has been consolidated into the repository's default `main` branch.

## Current scope

GA-LK optimizes each two-qubit layer boundary by jointly selecting gate sites,
cross-layer atom residency, and storage sites. Complete candidates undergo the
same AOD-aware physical transition checks before ranking. The objective combines
the current transition cost with a decayed finite-horizon physical estimate.
Small decision spaces use exact enumeration; larger spaces use a bounded genetic
search.

The manuscript evaluation is restricted to ZAC18 and QMAP154. It compares
GA-LK with the original ZAC compiler and the routing-aware ICCAD/QMAP method.
Controlled comparisons separately examine the finite-horizon configuration and
the search strategy.

## Maintained entry points

| Path | Purpose |
|---|---|
| [`zzx/`](zzx/) | Python compiler integration and state management |
| [`native/`](native/) | C++17 layer-boundary solver and native tests |
| [`experiments_v2/`](experiments_v2/) | Schema-v2 runners, aggregation, provenance, and validation |
| [`exp_setting/native_ga_v1/`](exp_setting/native_ga_v1/) | Frozen paper experiment configurations |
| [`tests/`](tests/) | Python regression and evidence-contract tests |
| [`results/paper_zh_v2/`](results/paper_zh_v2/) | Current paper results and final evidence manifest |
| [`third_party/qmap32_streaming/`](third_party/qmap32_streaming/) | Frozen QMAP 3.2 patch and verification material |

The authoritative result package is
[`results/paper_zh_v2/final_manifest.json`](results/paper_zh_v2/final_manifest.json).
Only this current result directory is tracked at the repository tip. Earlier
pilot, diagnostic, and intermediate result trees remain available through Git
history and are not valid substitutes for the current manuscript evidence.

## Result interpretation

The final package reports complete per-circuit results, independent analysis
units, aggregate statistics, controlled comparisons, timing summaries, and a
two-sheet workbook for ZAC18 and QMAP154. Individual QMAP154 circuits exhibit
particularly large fidelity gains. Aggregate ratios, per-circuit statistics,
and compilation time are reported separately so that result quality and
compiler cost retain their respective meanings.

## Regression checks

From `ZAC_zzx/`, run the maintained paper-facing checks in an environment with
the ZAC and experiment dependencies:

```bash
python -m pytest -q -p no:cacheprovider \
  tests/test_final_results.py \
  tests/test_cli_v2.py \
  tests/test_native_resident_integration.py \
  tests/test_native_rich_solver.py \
  tests/test_resident_rent_guard.py \
  tests/test_qmap_legacy_regression.py
```

Native build instructions and the ABI contract are documented in
[`native/README.md`](native/README.md). Experiment schemas and provenance rules
are documented in [`experiments_v2/README.md`](experiments_v2/README.md).

## Reproducibility boundary

The tracked result package contains the paper-facing aggregates and hashes.
Some raw executions, environments, and build products are intentionally kept
outside Git because of their size. The final manifest records their provenance;
it does not make a fresh clone a one-command reproduction of every raw run.

See the repository-level [`README.md`](../README.md) for the method overview,
reported result snapshot, baseline attribution, and release constraints.
