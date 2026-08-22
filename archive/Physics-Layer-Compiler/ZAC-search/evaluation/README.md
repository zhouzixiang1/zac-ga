# HPCA evaluation

This directory compares the two local compiler implementations in `ZAC-search/zac` and `ZAC-search/zac_search` on QASM benchmarks.

The parent evaluator launches a fresh Python subprocess for every `compiler x circuit` job. The worker aliases the selected compiler tree to the top-level `zac` package inside that subprocess, so the two implementations do not share Python module state.

## Quick start

From `ZAC-search`:

```bash
python evaluation/evaluate_hpca.py --limit 1
```

Full HPCA run:

```bash
python evaluation/evaluate_hpca.py
```

Useful options:

- `--compiler zac` or `--compiler zac_search` runs only one compiler.
- `--limit N` runs the first `N` QASM files.
- `--max-ops 0` disables Qiskit's operation-count skip.
- `--bench-dir benchmark/toy_example --arch-spec hardware_spec/toy_architecture.json` can be used for smaller smoke tests.
- `--output result/evaluation/<name>` chooses a run directory.

## Outputs

Each run writes to `result/evaluation/hpca` by default:

- `zac/<circuit>/code`, `time`, `fidelity`, and `metrics.json`
- `zac_search/<circuit>/code`, `time`, `fidelity`, and `metrics.json`
- `summary.csv`
- `summary.json`

The shared compiler settings live in `exp_setting/hpca_evaluation.json`. Keep those settings identical between compilers for fair comparison.
