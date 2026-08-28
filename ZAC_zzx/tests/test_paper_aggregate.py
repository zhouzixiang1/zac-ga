from __future__ import annotations

import gzip
import json
import math
from types import SimpleNamespace
from pathlib import Path

import pytest

from experiments_v2.contracts import RunManifest, RunStatus
from experiments_v2.paper_aggregate import (
    _ga_applicable_count,
    aggregate_paper,
    build_main_rows,
    command_aggregate_paper,
    render_results_values_tex,
    summarize_ablation,
    summarize_main,
    summarize_runtime,
)


def _run(dataset: str, circuit: str, method: str, seed: int, log_f: float,
         *, status: str = RunStatus.SUCCESS.value, repetition: int = 0,
         variant: str = "", artifact_dir: str = "") -> RunManifest:
    success = status == RunStatus.SUCCESS.value
    return RunManifest(
        run_id=f"{dataset}-{circuit}-{method}-s{seed}-r{repetition}-{variant or 'main'}",
        dataset=dataset,
        circuit=circuit,
        method=method,
        seed=seed,
        repetition=repetition,
        run_kind="ablation" if variant else "main",
        ablation_variant=variant,
        experiment_id="a" * 64,
        status=status,
        input_sha256="b" * 64,
        expected_gate_ledger_sha256="c" * 64,
        qubits=10,
        expected_gates_1q=20,
        expected_gates_2q=30,
        log_fidelity=log_f if success else None,
        fidelity=math.exp(log_f) if success else None,
        fidelity_ood=False,
        fidelity_components={
            "transfers": 100.0 + seed,
            "log_atom_transfer": -0.10 - seed * 0.001,
            "log_idle_excitation": -0.20 - seed * 0.001,
            "log_coherence_linear": -0.30 - seed * 0.001,
        } if success else {},
        transfers=100 + seed if success else None,
        idle_exposures=20 + seed if success else None,
        move_batches=10 + seed if success else None,
        move_time_us=1000.0 + seed if success else None,
        compiler_time_ns=10_000_000_000 if success else None,
        full_compile_ns=(20 + seed) * 1_000_000_000 if success else None,
        initial_placement_ns=1_000_000_000 if success else None,
        transition_decision_ns=10_000_000_000 if success else None,
        problem_preparation_ns=2_000_000_000 if success else None,
        search_kernel_ns=6_000_000_000 if success else None,
        result_commit_ns=2_000_000_000 if success else None,
        routing_ns=1_000_000_000 if success else None,
        return_match_ns=500_000_000 if success else None,
        forecast_ns=1_500_000_000 if success else None,
        artifact_dir=artifact_dir,
    )


def _complete_main() -> list[RunManifest]:
    rows: list[RunManifest] = []
    for dataset, circuit in (("zac18", "zac_c"), ("qmap154", "qmap_c")):
        rows.extend((
            _run(dataset, circuit, "M1", 0, -1.00),
            _run(dataset, circuit, "M2", 0, -0.95),
        ))
        rows.extend(_run(dataset, circuit, "M3", seed, log_f)
                    for seed, log_f in enumerate((-0.92, -0.90, -0.88)))
        rows.extend(_run(dataset, circuit, "M4", seed, log_f)
                    for seed, log_f in enumerate((-0.82, -0.80, -0.78)))
    return rows


def test_main_uses_three_seed_median_and_separates_qmap_coverage() -> None:
    suites = {"zac18": ["zac_c"], "qmap154": ["qmap_c"]}
    rows = build_main_rows(_complete_main(), frozen_suites=suites)
    assert rows["zac18"][0]["M3"]["log_fidelity"] == pytest.approx(-0.90)
    assert rows["zac18"][0]["M4"]["algorithm_time_s"] == 21.0
    assert rows["zac18"][0]["M4"]["valid_over_N"] == "3/3"
    semantics = rows["zac18"][0]["M4"]["timing_semantics"]
    assert semantics["nested_in_search_kernel"] == ["return_match_s", "forecast_s"]
    assert semantics["nested_fields_must_not_be_summed"] is True

    summary = summarize_main(rows, bootstrap_iterations=100, bootstrap_seed=7)
    zac = summary["datasets"]["zac18"]
    qmap = summary["datasets"]["qmap154"]
    assert zac["strict_common_linear_N"] == 1
    assert qmap["strict_common_linear_N"] == 1
    assert qmap["methods"]["M4"]["coverage"]["success_circuits"] == 1
    assert qmap["comparisons"]["M4_vs_Bstar"]["geometric_mean_ratio"] == pytest.approx(
        math.exp(0.15))
    mechanism = qmap["mechanism_delta_vs_strongest_baseline"]["M4"]
    assert mechanism["N"] == 1
    assert mechanism["log_coherence_linear_mean_delta"] == pytest.approx(-0.001)


def test_partial_seed_is_coverage_but_not_strict_fidelity() -> None:
    manifests = _complete_main()
    for index, row in enumerate(manifests):
        if row.dataset == "qmap154" and row.method == "M4" and row.seed == 2:
            manifests[index] = _run(
                "qmap154", "qmap_c", "M4", 2, -0.78,
                status=RunStatus.TIMEOUT.value)
    suites = {"zac18": ["zac_c"], "qmap154": ["qmap_c"]}
    rows = build_main_rows(manifests, frozen_suites=suites)
    summary = summarize_main(rows, bootstrap_iterations=50)
    qmap = summary["datasets"]["qmap154"]
    assert rows["qmap154"][0]["M4"]["valid_over_N"] == "2/3"
    assert qmap["methods"]["M4"]["coverage"]["success_circuits"] == 1
    assert qmap["methods"]["M4"]["coverage"]["complete_seed_circuits"] == 0
    assert qmap["strict_common_linear_N"] == 0
    assert qmap["comparisons"]["M4_vs_Bstar"]["strict_common_linear_N"] == 0


def test_ood_seed_is_complete_coverage_but_excluded_from_strict_fidelity() -> None:
    manifests = _complete_main()
    row = next(run for run in manifests
               if run.dataset == "qmap154" and run.method == "M4" and run.seed == 2)
    row.fidelity_ood = True
    suites = {"zac18": ["zac_c"], "qmap154": ["qmap_c"]}
    rows = build_main_rows(manifests, frozen_suites=suites)
    summary = summarize_main(rows, bootstrap_iterations=50)
    cell = rows["qmap154"][0]["M4"]
    assert cell["valid_over_N"] == "3/3"
    assert cell["fidelity_valid"] == 2
    assert summary["datasets"]["qmap154"]["strict_common_linear_N"] == 0


def test_ga_applicability_is_derived_from_compiler_stats(tmp_path: Path) -> None:
    artifact = tmp_path / "attempt"
    artifact.mkdir()
    with gzip.open(artifact / "compiler_stats.json.gz", "wt", encoding="utf-8") as handle:
        json.dump({"ga_applicable_boundaries": 3, "decision_log": [
            {"search_mode": "enumerate"},
            {"search_mode": "ga"},
            {"search_mode": "ga-early-stop"},
        ]}, handle)
    manifests: list[RunManifest] = []
    for seed in (0, 1, 2):
        manifests.append(_run(
            "zac18", "c", "M4", seed, -0.9,
            variant="H0", artifact_dir=str(artifact)))
        manifests.append(_run(
            "zac18", "c", "M4", seed, -0.7,
            variant="H8", artifact_dir=str(artifact)))
    manifests.append(_run(
        "zac18", "c", "M4", 0, -0.8,
        variant="greedy_only", artifact_dir=str(artifact)))
    summary, rows = summarize_ablation(
        manifests, bootstrap_iterations=50, bootstrap_seed=2)
    assert summary["available"] is True
    assert summary["ga_applicable_circuit_N"] == 1
    assert summary["GA_vs_greedy"]["N"] == 1
    assert summary["lookahead_H8_vs_H0"]["geometric_mean_ratio"] == pytest.approx(
        math.exp(0.2))
    h8_row = next(row for row in rows if row["variant"] == "H8")
    assert h8_row["ga_applicable_boundaries"] == 3
    assert h8_row["ga_applicability_source"].startswith("artifact.compiler_stats")


def test_ga_applicability_fallback_recognizes_greedy_only(tmp_path: Path) -> None:
    artifact = tmp_path / "attempt"
    artifact.mkdir()
    with gzip.open(artifact / "compiler_stats.json.gz", "wt", encoding="utf-8") as handle:
        json.dump({"decision_log": [
            {"search_mode": "enumerate"},
            {"search_mode": "greedy-only"},
            {"search_mode": "greedy-only-current-recovery"},
        ]}, handle)
    run = _run("zac18", "c", "M4", 0, -0.8, artifact_dir=str(artifact))
    count, source = _ga_applicable_count([run])
    assert count == 2
    assert source.startswith("artifact.compiler_stats")


def test_runtime_reports_median_and_iqr_without_nested_sum() -> None:
    manifests = [_run("zac18", "c", "M4", 0, -0.8, repetition=rep)
                 for rep in range(3)]
    for rep, run in enumerate(manifests):
        run.full_compile_ns = (10 + 10 * rep) * 1_000_000_000
    summary, rows = summarize_runtime(manifests)
    assert summary["available"] is True
    assert rows[0]["algorithm_time_median_s"] == 20.0
    assert rows[0]["algorithm_time_iqr_s"] == 10.0
    assert summary["methods"]["M4"]["median_of_circuit_medians_s"] == 20.0
    assert summary["methods"]["M4"]["median_of_circuit_iqrs_s"] == 10.0
    assert summary["methods"]["M4"]["iqr_of_circuit_medians_s"] == 0.0


def test_aggregate_writes_csv_json_and_tex_but_not_xlsx(tmp_path: Path) -> None:
    suites = {"zac18": ["zac_c"], "qmap154": ["qmap_c"]}
    report = aggregate_paper(
        _complete_main(), frozen_suites=suites, output_dir=tmp_path,
        bootstrap_iterations=50)
    expected = {
        "zac18.csv", "qmap154.csv", "ablation.csv", "sensitivity.csv",
        "runtime.csv", "main_summary.json", "paper_values.json",
        "results_values_zh.tex", "final_manifest.json",
    }
    assert expected == {path.name for path in tmp_path.iterdir()}
    assert not list(tmp_path.glob("*.xlsx"))
    tex = (tmp_path / "results_values_zh.tex").read_text(encoding="utf-8")
    assert "ResultTODO" not in tex
    assert "\\newcommand{\\ZACMFourF}" in tex
    assert "\\newcommand{\\ZACMFourRatio}" in tex
    assert "\\newcommand{\\ZACMFourRobustTen}" in tex
    assert "\\newcommand{\\ZACMFourDeltaTransfer}" in tex
    assert "\\newcommand{\\StrictMFourIQR}" in tex
    assert "\\newcommand{\\SensitivityStatement}" in tex
    assert "RETURN匹配和前瞻时间嵌套于搜索核" in tex
    assert render_results_values_tex(report["paper_values"]) == tex
    manifest = json.loads((tmp_path / "final_manifest.json").read_text())
    assert manifest["xlsx_generated_here"] is False
    assert manifest["nested_timing_semantics"]["must_not_be_summed"] is True


def test_command_aggregate_paper_integrates_frozen_sources(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    artifact = tmp_path / "artifacts"
    delivery = tmp_path / "delivery"
    paper = tmp_path / "paper"
    freeze_path = tmp_path / "freeze.json"
    freeze_path.write_text("{}\n", encoding="utf-8")
    run_map: dict[Path, RunManifest] = {}
    source = {"baselines": {"M1": {}, "M2": {}},
              "ours": {"M3": {}, "M4": {}}}
    for run in _complete_main():
        path = tmp_path / "manifests" / f"{run.run_id}.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(run.run_id, encoding="utf-8")
        run_map[path.resolve()] = run
        identity = f"{run.dataset}/{run.circuit}"
        entry = {"path": str(path),
                 "sha256": __import__("hashlib").sha256(path.read_bytes()).hexdigest(),
                 "status": run.status}
        if run.method in {"M1", "M2"}:
            source["baselines"][run.method][identity] = entry
        else:
            source["ours"][run.method].setdefault(identity, {})[str(run.seed)] = entry
    artifact.mkdir()
    (artifact / "quality_source_manifest.json").write_text(
        json.dumps(source), encoding="utf-8")
    freeze = {
        "freeze_id": "f" * 64,
        "canonical_suites": {
            "zac18": {"canonical_inputs": {"zac_c": "1" * 64}},
            "qmap154": {"canonical_inputs": {"qmap_c": "2" * 64}},
        },
        "ablation_cohort": {"identities": [["zac18", "zac_c"]]},
        "parity_timing_cohort": {"identities": [["zac18", "zac_c"]]},
    }
    monkeypatch.setattr(
        "experiments_v2.paper_protocol.load_paper_freeze",
        lambda _path: freeze)
    monkeypatch.setattr(
        "experiments_v2.paper_aggregate.load_run_manifest",
        lambda path, require_success_metrics=True: run_map[Path(path).resolve()])
    result = command_aggregate_paper(
        plan=SimpleNamespace(path=tmp_path / "plan.json"),
        freeze_path=freeze_path, artifact_root=artifact,
        output_root=delivery, paper_directory=paper)
    assert result["strict_common_linear_N"] == {"zac18": 1, "qmap154": 1}
    assert (paper / "results_values_zh.tex").is_file()
    final = json.loads((delivery / "final_manifest.json").read_text())
    assert final["input_manifest_counts"]["main"] == 16
