# Portable workflow validation

This record describes checks completed on 2026-09-08. It is an engineering
validation of a source snapshot, not a new compiler-performance study or a
claim of complete historical recomputation.

## Clean-source build and execution

The tested export was created without `.git`, existing environments, ignored
archives, historical raw runs or third-party PDFs. The 293-file source manifest
SHA-256 was
`f2f7e9fb4d5819b26b2c3313d4b034937ca4248513455b998a4d6636524e7abc`.
It records actual uncommitted file bytes, not an invented release commit.

| Check | Observed result |
|---|---|
| Platform | CPython 3.10.19, macOS 26.6.2 arm64, AppleClang 21 |
| Environment | New venv without system-site packages; dependencies installed anew |
| Native extension | Current version 0.5.35, ABI9; built wheel and installed extension bytes verified |
| Public parser | Implicit `physical_prefix` default: H=2, K=4, rho=0.7, 32 rollout evaluations |
| Toy compilation | Completed the bundled 14-qubit circuit |
| Physical checks | Raw ZAIR replay and independent normalized validation passed; zero ghost hits |
| Logical check | QASM-derived per-qubit 1Q/CZ order and CZ partners matched; not a unitary-equivalence proof |
| Independent score | Valid model score; retained in the smoke receipt, not used as a performance claim |

The new wheel SHA-256 is
`574dba72f23e41acb0d0c0d64f38d7112fe47358d8498f02f128ccdf0e4039f5`.
No historical wheel or installed environment was modified. An earlier attempt
failed because Ninja was missing from the declared build dependencies; that
attempt was retained, the dependency declaration corrected, and a new export
and environment used for the successful check.

Local receipts are under
`IEEE_conference_template/build/portable/fresh-export-20260908-v2/source/IEEE_conference_template/build/portable/`:
`runtime-v1/bootstrap.json`, `runtime-v1/resolved-packages.txt`,
`smoke-v1/smoke.json` and the full trace. These generated directories are ignored
and are not represented as publicly distributed raw evidence.

## Inputs, processed artifacts and manuscript

- All **18 ZAC + 154 QMAP inputs** were read from the original local source
  directories and normalized with Qiskit 1.2.4, optimization level 3 and seed 0.
  Every original and canonical SHA-256 matched
  [the acquisition manifest](benchmark_acquisition_manifest.json). No compiler
  experiment was rerun by this input check. The local receipt is
  `IEEE_conference_template/build/benchmark-inputs/local-inputs-20260908-v1/inputs.json`.
- The processed historical package has a separate read-only **14-file**
  byte/hash audit. It does not validate undistributed raw traces.
- The clean-export **source-only Chinese paper build produced nine pages**,
  with a separately generated one-page Fig. 3 and no detected overfull boxes or
  undefined references. The resulting PDF SHA-256 was
  `7669c4f4520364f60fc34d141153e526c08e757d5d5581f129c4226196727cdd`.
  This is the tested export snapshot, not a guarantee that later author edits
  have identical rendering. Visual and scientific review remain separate.
- The portable scripts have **20 standard-library offline tests**, covering
  path/overwrite protection, source drift, export boundaries, identity checks,
  artifact integrity, benchmark metadata and current-suite preparation.

## Current manuscript packaging regression

The afternoon GitHub preparation used a new **310-file clean export**, including
the exact six files in `ZAC_zzx/results/default_initial_v1/paper_exports/`.
Its source manifest SHA-256 was
`681f46e5e7175ae318176aea088afe51017e6d8c6c1c1f823ac04b904071ae2a`.
This is a separate snapshot from the morning compiler bootstrap above; native
experiments were not rerun for this packaging check.

The source-only renderer copied all six companion files, verified their hashes
against the export, and adapted the two manuscript inputs only in its new
render directory. It produced nine pages and a one-page vector Fig. 3 without
overfull boxes or undefined references. The resulting PDF SHA-256 was
`bf8d05d30f406269ce8fc6045a4eb973f0b0ff5f6439bfe9fcbc8f17250a6180`.
All nine pages were pixel-identical to the author's verified PDF when rendered
at 90 dpi, and the contact sheet was visually reviewed. The original 14-file
processed-artifact audit also passed inside this clean export.

Local receipts are under
`IEEE_conference_template/build/portable/github-release-20260908-v1/source/`:
`portable_source_manifest.json` and
`IEEE_conference_template/build/bundled-render/render-v1/`.
The visual comparison is under
`IEEE_conference_template/build/tmp/github-release-20260908-4rdub3/visual/`.
These local checks establish package completeness and layout, not independent
recomputation of the historical or updated scientific results.

The updated portable contract suite has 29 passing standard-library tests.
The full repository `scripts/test_*.py` suite has 141 passing tests, including
README value/link checks and archive-preservation checks. The author-side
paper audit and native CTest checks also passed. The separate broader selected
Python compiler tests reported 126 passes and six explicit skips; this is not
a claim that every repository test was executed.

Overleaf export remains a separate integration task: it must package the new
companion inputs before the latest manuscript can be synchronized. This GitHub
publication does not update the online manuscript.

## Scope not yet validated or distributed

1. **Complete live acquisition:** all 172 local-source transformations passed;
   the complete network-fetch route has not been run end to end. Historical
   input records omitted upstream commits. Content pins are exact, but refs
   such as `main` are only location hints and may stop matching.
2. **Full formal runs:** current-suite preparation emits a fixed configuration
   and protocol. The ordinary compiler frontend is fail-fast, not a complete
   bounded, resumable, independently verified multi-method study coordinator.
3. **Historical closure:** old raw traces, frozen native binaries/environments
   and machine-bound protocol dependencies remain local. Processed manifests
   and hashes are not substitutes for distributing or rebuilding that closure.
4. **Routing-aware baseline:** a fresh QMAP baseline source/patch/build/run
   workflow has not been independently completed by the portable demo. The
   demo does not use or claim to reproduce that baseline.
5. **Rights and release:** upstream notices are retained, but the author has
   not selected a license for newly authored code or data. A stable archival
   deposit and release identifier are not yet assigned.
6. **Portability envelope:** other operating systems, Python versions within
   the declared range, complete transitive hash locking and cross-platform
   numerical equivalence have not been established by this single-platform
   check. English-manuscript alignment is also a separate task.

See [the reproduction guide](REPRODUCIBILITY.md) for commands and
[the availability inventory](DATA_AND_CODE.md) for provenance and rights.
