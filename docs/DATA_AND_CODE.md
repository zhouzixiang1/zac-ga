# Code and data inventory

This is an availability record for the IEEE manuscript, not a claim that a
complete public archival deposit already exists. No DOI, license, embargo or
access commitment is invented here.

## Materials and access routes

| Material | Location / route | Scope and status |
|---|---|---|
| Current compiler | `ZAC_zzx/zzx`, `native`, `zac`, `evaluation`; source repository | Current-source toy workflow; distinct from historical binaries |
| Toy circuit and architecture | `ZAC_zzx/benchmark/toy_example`, `hardware_spec` | Included in demo; upstream notice retained at `ZAC/LICENSE` |
| Original accepted processed results | `ZAC_zzx/results/paper_zh_v2` | Unmodified CSV/JSON/TeX/XLSX package; 14-file manifest audit |
| Current Chinese main-result inputs | `ZAC_zzx/results/default_initial_v1/paper_exports` | Six audited derived files for the abstract, Table II, Table III and Fig. 7(a); separate from the original accepted package |
| Initialization ablation | `ZAC_zzx/results/initial_lookahead_v1` | Complete paired comparisons and retained incomplete outcomes; independent from the full-suite quality study |
| Horizon and refinement evidence | `ZAC_zzx/results/horizon_extension_v2` and `refinement_v1` | Horizon quality stage completed, timing stage disabled; rejected refinements retained as diagnostics |
| Figure source data | `IEEE_conference_template/figures/data` and value files | TikZ/PGFPlots and table inputs with separate provenance |
| Manuscript | `IEEE_conference_template` | Chinese active; English pending alignment |
| Full canonical ZAC18/QMAP154 inputs | `docs/benchmark_acquisition_manifest.json` and `scripts/acquire_benchmarks.py` | 172 original/canonical hashes; official fetch or local-source normalization, without inventing missing historical commits |
| Historical native builds | Local wheel/environment/build records | Not included in Git or demo; hashes identify but do not distribute them |
| Original raw-run closure | Ignored `fidelity-lookahead-v2` and local study run/build directories | Preserved locally; not a public downloadable archival deposit |
| Baseline dependencies | `ZAC` and QMAP patch/metadata under `ZAC_zzx/third_party/qmap32_streaming` | Full QMAP source/environment not vendored |
| Third-party papers | Local `documents` copies | Reference use; excluded from portable exports |

Repository access is controlled by its owner. A commit URL is not an archival
data DOI. The export does not change access controls or upload material.

The current main-result bundle separates per-input summaries (`main_rows.csv`),
canonical comparison units (`analysis_units.csv`), mechanism values
(`mechanism.csv`), display macros and selected circuit rows (the two `.tex`
files), and provenance/cohort definitions (`default_initial_values.json`).
Initialization, ordered-horizon and genetic-search ablations, plus strict
serial timing, retain their respective evidence sources. Parallel full-suite
quality runs are not serial timing measurements.

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
