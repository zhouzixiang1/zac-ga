# Code and data inventory

This is an availability record for the IEEE manuscript, not a claim that a
complete public archival deposit already exists. No DOI, license, embargo or
access commitment is invented here.

## Materials and access routes

| Material | Location / route | Scope and status |
|---|---|---|
| Current compiler | `ZAC_zzx/zzx`, `native`, `zac`, `evaluation`; source repository | Current-source toy workflow; `physical_prefix` default and explicit `physical_prefix_ga` are distinct from frozen historical binaries |
| Toy circuit and architecture | `ZAC_zzx/benchmark/toy_example`, `hardware_spec` | Included in demo; upstream notice retained at `ZAC/LICENSE` |
| Original accepted processed results | `ZAC_zzx/results/paper_zh_v2` | Unmodified CSV/JSON/TeX/XLSX package; 14-file manifest audit |
| Current bilingual main-result inputs | `ZAC_zzx/results/physical_ga_main_v1/paper_exports` | Eleven derived files for the abstract, overall table and physical-component discussion; explicit GA initialization with dynamic H=8 |
| Previous main-result inputs | `ZAC_zzx/results/default_initial_v1/paper_exports` | Earlier SA-plus-candidate publication bundle, retained separately and not used for current headline values |
| Physical-GA initialization pilot | `ZAC_zzx/results/physical_ga_initial_v1/corrected_driver_v1` | Complete 108-job matrix with eight complete paired circuits; original driver failures remain in the parent directory |
| Earlier initialization ablation | `ZAC_zzx/results/initial_lookahead_v1` | Complete paired comparisons and retained incomplete outcomes; independent from the full-suite quality study |
| Horizon and refinement evidence | `ZAC_zzx/results/horizon_extension_v2` and `refinement_v1` | Horizon quality stage completed, timing stage disabled; rejected refinements retained as diagnostics |
| Figure source data | `IEEE_conference_template/figures/data` and value files | Active TikZ/PGFPlots inputs; retired source/data and provenance are preserved under `docs/paper` |
| Manuscript | `IEEE_conference_template` | Aligned anonymous Chinese and English clean sources; four figures, one table and one algorithm |
| Paper tools and records | `scripts/paper` and `docs/paper` | Generators/checkers/tests and writing/provenance records; not part of the minimal Overleaf source export |
| Full canonical ZAC18/QMAP154 inputs | `docs/benchmark_acquisition_manifest.json` and `scripts/acquire_benchmarks.py` | 172 original/canonical hashes; official fetch or local-source normalization, without inventing missing historical commits |
| Historical native builds | Local wheel/environment/build records | Not included in Git or demo; hashes identify but do not distribute them |
| Current raw-run dependencies and historical records | Ignored `fidelity-lookahead-v2` and local study run/build directories | Current GA/baseline/control evidence retained; obsolete payloads pruned on 2026-10-01; not a public downloadable archival deposit |
| Baseline dependencies | `ZAC` and QMAP patch/metadata under `ZAC_zzx/third_party/qmap32_streaming` | Full QMAP source/environment not vendored |
| Third-party papers | Local `documents` copies | Reference use; excluded from portable exports |

Repository access is controlled by its owner. A commit URL is not an archival
data DOI. The export does not change access controls or upload material.

The [2026-10-01 cleanup](repository-maintenance-20261001.md) removed an approved
list of obsolete traces, detailed statistics, successful logs and an unused
QASMBench checkout. Current experiment dependencies remain local. Historical
whole-archive seals are unchanged inventories of the earlier state, not promises
that every old raw file is still available. Retained scores and protocols
support the [development history](EXPERIMENT_HISTORY.md); they cannot replace
physical revalidation of a deleted trace.

The current main-result bundle separates per-file summaries (`main_rows.csv`),
canonical comparison units (`analysis_units.csv`), physical components
(`mechanism.csv`), case rows (`representative_cases.csv`), two display `.tex`
files, aggregate values (`ga_main_values.json`), all canonical run outcomes
(`canonical_runs.csv`), failures (`failures.csv`), completed but out-of-domain
scores (`completed_ood.csv`), and source/output hashes (`provenance.json`).
Both manuscript entry points import `ga_main_values.tex` and
`representative_cases.tex` from this bundle. The original accepted package and
previous `default_initial_v1` publication bundle are not overwritten.

The physical-GA main study has 507 canonical circuit/seed jobs: 480 successful
compilations, 18 timeouts, six program errors and three memory-limit outcomes.
Of the successful runs, 64 are outside the fidelity model's validity domain.
The current aggregate comparisons use 16 ZAC18 and 119 QMAP canonical circuit
units after the prescribed three-seed and alias handling. Complete outcome
records retain the inputs excluded from those quality aggregates.

Initialization, ordered-horizon and genetic-search ablations, plus strict
serial timing, retain their respective evidence sources. Parallel full-suite
quality runs are not serial timing measurements. The initialization pilot
supports within-study comparisons; neither a changed main-study cohort nor a
current-source toy run establishes an initialization-only improvement.

## Formats, variables and units

- Canonical circuits are OpenQASM 2 with input hashes; original and normalized
  circuits are distinct objects.
- ZAIR is JSON. Physical timing uses microseconds; positions resolve against
  the architecture's SLM/trap coordinates.
- Fidelity/log fidelity are dimensionless. Transfers, idle exposures and MOVE
  batches are counts. `move_time_us` is microseconds; `algorithm_time_s` seconds.
- Timing records may use `*_ns`. Nested stage timings must not be added as
  independent totals.
- Inspect status and valid/N fields. Missing/out-of-domain scores are not zero
  and do not erase failed inputs from compiler coverage.
- XLSX has separate ZAC18/QMAP154 sheets; use CSVs and the final manifest for
  programmatic checks. The workbook is not raw trace data.

Protocols define normalization, cohort/alias handling, scoring and acceptance.
Rejected refinement outcomes remain diagnostic and are not promoted into an
improvement. Current-source smoke metrics are not paper performance results.
The source parser and public example still default to `physical_prefix`; the
manuscript main study explicitly selects `physical_prefix_ga` with
`init_engine=ga`. The configuration difference is recorded in the
[reproduction guide](REPRODUCIBILITY.md#current-source-build-and-smoke).

## Provenance and identifiers

Identify source by repository commit and evidence by its manifest/hash. Keep
historical manifests unchanged even when they contain old machine paths;
relocation indices describe moved local files. Do not edit frozen paths to make
missing dependencies appear available.

A deposit should state creator/title/version, exact source revision, dictionary
and units, normalization provenance, complete outcomes, checksums, rights/access
terms and related manuscript identifiers. Populate identifiers only when real.

## Rights and release decisions

- Upstream ZAC is BSD-3-Clause, copyright UCLA VAST Lab. Preserve `ZAC/LICENSE`
  with derived source and follow its notice requirements.
- A license for newly authored code is **not yet chosen**. This record does not
  grant one on the author's behalf.
- Dataset rights differ from software rights; confirm benchmark origin and
  permissions before distributing the complete inputs.
- Acquiring a paper PDF is not permission to republish it. PDFs are excluded
  from the portable export.
- Local availability is not a stable access procedure. Do not claim all raw
  runs are publicly available until a real download/deposit route exists.

## Remaining release work

1. Confirm software/data license decisions without relicensing third parties.
2. Validate the complete live benchmark download route and retain third-party
   notices; content pins are provided, but missing historical upstream commit
   identifiers cannot be reconstructed by assertion.
3. Publish raw-evidence/runtime dependencies or a documented reconstruction
   route, identifying what can and cannot be recomputed.
4. Validate the complete public benchmark queue and advertised platforms.
5. Create a versioned release/deposit and add its actual identifier; keep it
   aligned with README and manuscript availability wording.

## 中文核对

源码运行与处理后结果完整性核对，不等于全部历史实验的一键重跑。新增代码
许可证、完整基准输入分发权限、原始证据稳定获取地址及正式归档标识仍须落实；
本机绝对路径或哈希不能替代数据发布。
