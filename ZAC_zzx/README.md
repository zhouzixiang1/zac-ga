# GA-LK compiler

`ZAC_zzx` contains the Python compiler, C++ search backend, independent physical
evaluation and experiment protocols. Run the portable quick start from the
repository root:

```bash
python3 scripts/portable_reproduce.py bootstrap --name runtime-v1
python3 scripts/portable_reproduce.py smoke --runtime runtime-v1 --name smoke-v1
python3 scripts/portable_reproduce.py audit-artifacts
```

The [reproduction guide](../docs/REPRODUCIBILITY.md) distinguishes a working
current-source demo from historical experiment reproduction. All new output
belongs in `../IEEE_conference_template/build/`.

## Public interface

[ga_lk_default.json](exp_setting/ga_lk_default.json) is the public template.
Bootstrap produces a copy bound to its actually built wheel. Use that copy with
[run.py](run.py), or pass `zac_setting[0]` to
`ZAC_zzx().parse_setting(...)`. A historical wheel hash is not an optional label.

Standard ABI9 GA-LK defaults to physical-prefix initialization with
H_init=2, K=4, rho=0.7 and 32 rollout evaluations. Explicit
`init_strategy="legacy"` selects the original initializer; remove the
`initial_lookahead` object in that case. Layer-boundary look-ahead is separate.
Historical ABI8 and explicitly frozen configurations retain their semantics.
The public path needs no private paper-ablation parser contract.

## Implementation map

| Location | Role |
|---|---|
| [zzx/initial_lookahead.py](zzx/initial_lookahead.py) | Initial candidate pool and physical-prefix evaluation |
| [zzx/zac_zzx.py](zzx/zac_zzx.py) | Scheduling, initialization, optimization and routing |
| [zzx/native_backend.py](zzx/native_backend.py) | Typed native interface and byte-verified wheel registration |
| [native](native/) | C++ search and physical candidate evaluation |
| [verify_batches.py](verify_batches.py) | Independent replay of actual ZAIR instructions |
| [evaluation](evaluation/) | Canonical events, physical checks and fidelity model |
| [experiments_v2](experiments_v2/) | Canonicalization, runners and provenance |
| [results/paper_zh_v2](results/paper_zh_v2/) | Unmodified accepted processed results |
| [results/default_initial_v1/paper_exports](results/default_initial_v1/paper_exports/) | Audited current Chinese main-result inputs |
| [results/initial_lookahead_v1](results/initial_lookahead_v1/) | Paired initialization ablation and complete outcomes |
| [results/horizon_extension_v2](results/horizon_extension_v2/) | Completed horizon quality study; serial timing stage disabled |

Smoke checks actual instructions, physical feasibility and per-qubit operation
ordering; it is not a full matrix-equivalence proof or a performance benchmark.

## Evidence and tests

The main comparison covers ZAC18 and QMAP154, with reuse-aware ZAC and
routing-aware placement as baselines. The [final manifest](results/paper_zh_v2/final_manifest.json)
indexes the original accepted workbook and provenance, which remain unchanged.
The completed full-suite initialization study now supplies the Chinese
abstract, Table II, Table III and Fig. 7(a) through the six-file
[publication bundle](results/default_initial_v1/paper_exports/). Its common
comparison cohorts and baseline aggregates are recorded in that bundle.

Initialization, layer-horizon and genetic-search ablations, plus strict serial
timing, retain their respective evidence sources. Failed and incomplete runs
remain visible in the study records. Rejected refinements never overwrite the
accepted package or justify a changed default. The older horizon v1 queue was
superseded by v2 and is not an active resume target.

Portable contract tests use only the Python standard library:

```bash
python3 -B -m unittest discover -s scripts -p 'test_portable_reproduce.py' -v
```

Run from the repository root. Broader compiler tests need the runtime and,
for some historical checks, frozen evidence. See [native build documentation](native/README.md)
and the [code/data inventory](../docs/DATA_AND_CODE.md). Do not replace an
accepted environment while testing a new extension.

Upstream ZAC's BSD-3-Clause notice is retained at [../ZAC/LICENSE](../ZAC/LICENSE).
The author has not yet assigned a license to newly authored additions or data.
