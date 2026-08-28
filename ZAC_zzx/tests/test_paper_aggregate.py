from __future__ import annotations

import csv
import gzip
import json
import math
from types import SimpleNamespace
from pathlib import Path

import pytest

from experiments_v2.contracts import RunManifest, RunStatus, sha256_file
from experiments_v2.paper_aggregate import (
    FIG6_DERIVED_OUTPUTS,
    FIG6_RAW_EXPORTS,
    METHODS,
    _exact_matrix,
    _figure_ablation_rows,
    _ga_applicable_count,
    _legacy_seed0_fallback_exception,
    _publish_paper_fig6_data,
    _validate_frozen_evidence,
    aggregate_paper,
    build_main_rows,
    command_aggregate_paper,
    render_results_values_tex,
    summarize_ablation,
    summarize_main,
    summarize_runtime,
    summarize_sensitivity,
)
from experiments_v2.paper_protocol import (
    PAPER_ABLATION_VARIANTS,
    SENSITIVITY_PROFILE_IDS,
)


def _run(dataset: str, circuit: str, method: str, seed: int, log_f: float,
         *, status: str = RunStatus.SUCCESS.value, repetition: int = 0,
         variant: str = "", artifact_dir: str = "") -> RunManifest:
    success = status == RunStatus.SUCCESS.value
    native_abi = 9 if variant else 8
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
        backend="native" if method in {"M3", "M4"} and success else "",
        native_abi_version=(
            native_abi if method in {"M3", "M4"} and success else None),
        native_wheel_sha256=(
            str(native_abi) * 64
            if method in {"M3", "M4"} and success else ""),
        python_fallback=(False if method in {"M3", "M4"} and success else None),
        input_sha256="b" * 64,
        expected_gate_ledger_sha256="c" * 64,
        observed_gate_ledger_sha256="c" * 64 if success else "",
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
        verifier_ok=True if success else None,
        ghost_hits=0 if success else None,
        artifact_dir=artifact_dir,
    )


def _write_fake_fig6_converter(
        paper: Path, outputs: tuple[str, ...] = FIG6_DERIVED_OUTPUTS) -> Path:
    figures = paper / "figures"
    figures.mkdir(parents=True, exist_ok=True)
    converter = figures / "prepare_experimental_summary.py"
    converter.write_text(
        "import argparse\n"
        "from pathlib import Path\n"
        "parser = argparse.ArgumentParser()\n"
        "parser.add_argument('--input-root', type=Path, required=True)\n"
        "parser.add_argument('--output-root', type=Path, required=True)\n"
        "args = parser.parse_args()\n"
        f"outputs = {list(outputs)!r}\n"
        "args.output_root.mkdir(parents=True, exist_ok=True)\n"
        "for name in outputs:\n"
        "    (args.output_root / name).write_text(name + '\\n', encoding='utf-8')\n",
        encoding="utf-8")
    return converter


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
    values = aggregate_paper(
        manifests, frozen_suites=suites,
        bootstrap_iterations=20)["paper_values"]["macros"]
    assert values["TimeFullCompile"] == "21.000"
    assert values["QMAPMOneV"] == "1/1"
    assert values["QMAPMTwoV"] == "1/1"
    assert values["QMAPMThreeV"] == "1/1"
    assert values["QMAPMFourV"] == "0/1"


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


def test_partial_ablation_seed_is_not_used_in_controlled_comparison(
        tmp_path: Path) -> None:
    artifact = tmp_path / "attempt"
    artifact.mkdir()
    with gzip.open(artifact / "compiler_stats.json.gz", "wt", encoding="utf-8") as handle:
        json.dump({"ga_applicable_boundaries": 2}, handle)
    manifests = []
    for seed in (0, 1, 2):
        manifests.append(_run(
            "zac18", "c", "M4", seed, -0.9, variant="H0",
            artifact_dir=str(artifact)))
        manifests.append(_run(
            "zac18", "c", "M4", seed, -0.7,
            status=(RunStatus.TIMEOUT.value if seed == 2 else
                    RunStatus.SUCCESS.value),
            variant="H8", artifact_dir=str(artifact)))
    manifests.append(_run(
        "zac18", "c", "M4", 0, -0.8, variant="greedy_only",
        artifact_dir=str(artifact)))
    summary, rows = summarize_ablation(
        manifests, bootstrap_iterations=20, bootstrap_seed=3)
    assert summary["lookahead_H8_vs_H0"]["N"] == 0
    assert summary["GA_vs_greedy"]["N"] == 0
    figure = _figure_ablation_rows(
        rows, h0_variant="H0", h8_variant="H8",
        greedy_variant="greedy_only")
    assert all(row["paired_valid"] is False for row in figure)


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
    assert summary["cohort_N"] == 1
    assert summary["methods"]["M4"]["cohort_N"] == 1


def test_runtime_macros_report_method_coverage_and_verifier_failure() -> None:
    identities = [("zac18", f"circuit_{index}") for index in range(12)]
    timing: list[RunManifest] = []
    for dataset, circuit in identities:
        for method in METHODS:
            for repetition in range(3):
                status = (
                    RunStatus.VERIFIER_FAIL.value
                    if method == "M1" and circuit == "circuit_11"
                    else RunStatus.SUCCESS.value)
                timing.append(_run(
                    dataset, circuit, method, 0, -0.8,
                    status=status, repetition=repetition))
    report = aggregate_paper(
        _complete_main(),
        frozen_suites={"zac18": ["zac_c"], "qmap154": ["qmap_c"]},
        timing_manifest_inputs=timing,
        timing_identities=identities,
        bootstrap_iterations=20)
    runtime = report["runtime_summary"]
    assert runtime["cohort_N"] == 12
    assert runtime["methods"]["M1"]["circuit_N"] == 11
    assert runtime["methods"]["M1"]["verifier_fail_circuit_N"] == 1
    assert runtime["methods"]["M1"]["incomplete_status_counts"] == {
        RunStatus.VERIFIER_FAIL.value: 3}
    assert runtime["methods"]["M2"]["circuit_N"] == 12
    macros = report["paper_values"]["macros"]
    assert macros["StrictMOneV"] == "11/12"
    assert macros["StrictMTwoV"] == "12/12"
    assert macros["StrictMThreeV"] == "12/12"
    assert macros["StrictMFourV"] == "12/12"
    assert "M1 20.000s（有效11/12，1个电路验证失败）" in (
        macros["StrictRuntimeStatement"])


def test_ablation_label_macros_name_the_controlled_m4_variants() -> None:
    ablation: list[RunManifest] = []
    for seed in (0, 1, 2):
        ablation.append(_run(
            "zac18", "zac_c", "M4", seed, -0.9, variant="H0"))
        ablation.append(_run(
            "zac18", "zac_c", "M4", seed, -0.7, variant="H8"))
    ablation.append(_run(
        "zac18", "zac_c", "M4", 0, -0.8,
        variant="greedy_only"))
    report = aggregate_paper(
        _complete_main(),
        frozen_suites={"zac18": ["zac_c"], "qmap154": ["qmap_c"]},
        ablation_manifest_inputs=ablation,
        ablation_identities=[("zac18", "zac_c")],
        bootstrap_iterations=20)
    macros = report["paper_values"]["macros"]
    assert macros["AblationHZero"] == "M4-H0"
    assert macros["AblationHMulti"] == "M4-H8"
    assert macros["AblationGA"] == "M4-GA"
    assert macros["AblationGreedy"] == "M4-greedy"


def test_runtime_partial_repetitions_do_not_enter_strict_summary_or_ratio() -> None:
    manifests = [
        _run("zac18", "c", method, 0, -0.8, repetition=rep,
             status=(RunStatus.TIMEOUT.value
                     if method == "M4" and rep == 2 else
                     RunStatus.SUCCESS.value))
        for method in ("M2", "M4") for rep in range(3)
    ]
    summary, rows = summarize_runtime(
        manifests, expected_identities=[("zac18", "c")])
    m4 = next(row for row in rows if row["method"] == "M4")
    assert m4["valid_over_N"] == "2/3"
    assert m4["strict_complete"] is False
    assert summary["methods"]["M4"]["circuit_N"] == 0
    assert summary["paired_ratios"]["M4_vs_M2"]["N"] == 0


def test_exact_matrix_rejects_duplicate_that_replaces_missing() -> None:
    runs = [
        _run("zac18", "a", "M4", 0, -0.8),
        _run("zac18", "a", "M4", 0, -0.8),
    ]
    with pytest.raises(ValueError, match="identity matrix drift"):
        _exact_matrix(
            "test", runs,
            {("zac18", "a", "M4", 0), ("zac18", "b", "M4", 0)},
            key=lambda run: (run.dataset, run.circuit, run.method, run.seed))


def test_legacy_unspecified_fallback_is_limited_to_named_seed0() -> None:
    seed0 = _run("zac18", "c", "M4", 0, -0.8)
    seed0.python_fallback = None
    identity = ("zac18", "c", "M4", 0, 0)
    evidence = _validate_frozen_evidence(
        "quality", [seed0], frozen_inputs={"zac18": {"c": "b" * 64}},
        expected_native_abi=8, expected_native_wheel_sha256="8" * 64,
        legacy_unspecified_fallback={identity})
    assert evidence["legacy_python_fallback_exception_N"] == 1
    assert evidence["successful_observed_gate_ledgers_verified"] is True

    with pytest.raises(ValueError, match="lacks explicit no-fallback"):
        _validate_frozen_evidence(
            "quality", [seed0],
            frozen_inputs={"zac18": {"c": "b" * 64}},
            expected_native_abi=8,
            expected_native_wheel_sha256="8" * 64)
    seed0.python_fallback = True
    with pytest.raises(ValueError, match="used Python fallback"):
        _validate_frozen_evidence(
            "quality", [seed0],
            frozen_inputs={"zac18": {"c": "b" * 64}},
            expected_native_abi=8,
            expected_native_wheel_sha256="8" * 64,
            legacy_unspecified_fallback={identity})
    seed0.python_fallback = False
    seed0.observed_gate_ledger_sha256 = "d" * 64
    with pytest.raises(ValueError, match="observed gate-ledger mismatch"):
        _validate_frozen_evidence(
            "quality", [seed0],
            frozen_inputs={"zac18": {"c": "b" * 64}},
            expected_native_abi=8,
            expected_native_wheel_sha256="8" * 64)


def test_legacy_fallback_exception_requires_exact_parity_matrix(
        tmp_path: Path) -> None:
    source = {"M3": {}, "M4": {}}
    quality = {
        "accepted_old_seed0": True,
        "ours": {"M3": {}, "M4": {}},
    }
    comparisons = []
    for method in ("M3", "M4"):
        old_manifest = tmp_path / f"{method}-old.json"
        old_manifest.write_text("{}\n", encoding="utf-8")
        row = {
            "path": str(old_manifest.resolve()),
            "sha256": method.lower()[1] * 64,
            "status": "success",
        }
        source[method]["zac18/c"] = row
        quality["ours"][method]["zac18/c"] = {
            "0": dict(row), "1": {}, "2": {}}
        comparisons.append({
            "dataset": "zac18", "circuit": "c", "method": method,
            "old_manifest": str(old_manifest.resolve()), "passed": True,
        })
    source_path = tmp_path / "seed0.json"
    source_path.write_text(json.dumps(source), encoding="utf-8")
    parity_path = tmp_path / "parity.json"
    quality["parity_report"] = str(parity_path.resolve())
    freeze = {
        "freeze_id": "f" * 64,
        "seed0_source_manifest": {
            "path": str(source_path.resolve()),
            "sha256": sha256_file(source_path),
        },
        "parity_timing_cohort": {"identities": [["zac18", "c"]]},
    }
    parity = {
        "freeze_id": freeze["freeze_id"], "passed": True, "status": "passed",
        "compared": 2, "comparisons": comparisons,
    }
    parity_path.write_text(json.dumps(parity), encoding="utf-8")
    allowed, policy = _legacy_seed0_fallback_exception(quality, freeze)
    assert allowed == {
        ("zac18", "c", "M3", 0, 0),
        ("zac18", "c", "M4", 0, 0),
    }
    assert policy["allowed_identity_N"] == 2

    parity["comparisons"] = parity["comparisons"][:1]
    parity_path.write_text(json.dumps(parity), encoding="utf-8")
    with pytest.raises(ValueError, match="passed parity gate"):
        _legacy_seed0_fallback_exception(quality, freeze)


def test_sensitivity_uses_log_fidelity_without_linear_underflow() -> None:
    run = _run(
        "zac18", "c", "M4", 0, -1000.0,
        variant="paper_sensitivity_default")
    run.fidelity = 0.0
    summary, _rows = summarize_sensitivity([run])
    assert summary["settings"]["paper_sensitivity_default"][
        "fidelity_geometric_mean"] == 0.0


def test_sensitivity_pairs_every_setting_to_default_and_drives_wording() -> None:
    fidelity_ratios = {
        "default": 1.0,
        "budget_192": 1.0012,
        "budget_1152": 1.0,
        "return_4_2": 1.0011,
        "return_10_8": 0.9994,
        "horizon_2": 0.984,
        "horizon_4": 0.99955,
        "decay_0p2_0p5": 0.96,
        "decay_0p35_0p6": 0.989,
    }
    time_ratios = {
        "default": 1.0,
        "budget_192": 0.81,
        "budget_1152": 1.14,
        "return_4_2": 0.80,
        "return_10_8": 1.34,
        "horizon_2": 0.91,
        "horizon_4": 0.95,
        "decay_0p2_0p5": 1.0,
        "decay_0p35_0p6": 0.98,
    }
    identities = [
        ("zac18" if index < 6 else "qmap154", f"sensitivity_{index}")
        for index in range(12)
    ]
    manifests: list[RunManifest] = []
    settings = [f"paper_sensitivity_{profile}"
                for profile in fidelity_ratios]
    for index, (dataset, circuit) in enumerate(identities):
        default_log = -1.0 - 0.1 * index
        for profile, fidelity_ratio in fidelity_ratios.items():
            run = _run(
                dataset, circuit, "M4", 0,
                default_log + math.log(fidelity_ratio),
                variant=f"paper_sensitivity_{profile}")
            run.full_compile_ns = round(
                20_000_000_000 * time_ratios[profile])
            manifests.append(run)

    sensitivity, rows = summarize_sensitivity(
        manifests, expected_identities=identities,
        expected_settings=settings)
    assert sensitivity["cohort_N"] == 12
    assert len(rows) == 108
    low = sensitivity["settings"]["paper_sensitivity_budget_192"]
    assert low["paired_vs_default"][
        "fidelity_geometric_mean_ratio"] == pytest.approx(1.0012)
    assert low["paired_vs_default"][
        "algorithm_time_geometric_mean_ratio"] == pytest.approx(0.81)
    assert low["paired_vs_default_by_dataset"]["zac18"][
        "fidelity_N"] == 6
    assert low["paired_vs_default_by_dataset"]["qmap154"][
        "fidelity_N"] == 6
    low_rows = [row for row in rows
                if row["setting"] == "paper_sensitivity_budget_192"]
    assert all(row["paired_fidelity_ratio_vs_default"] == pytest.approx(1.0012)
               for row in low_rows)
    assert all(row["paired_algorithm_time_ratio_vs_default"] ==
               pytest.approx(0.81) for row in low_rows)

    report = aggregate_paper(
        _complete_main(),
        frozen_suites={"zac18": ["zac_c"], "qmap154": ["qmap_c"]},
        sensitivity_manifest_inputs=manifests,
        sensitivity_identities=identities,
        sensitivity_settings=settings,
        bootstrap_iterations=20)
    macros = report["paper_values"]["macros"]
    statement = macros["SensitivityStatement"]
    assert "共12个电路、9组单因素设置" in statement
    assert "低预算192/RETURN 4/2的Fidelity近似持平且更快" in statement
    assert "Fidelity +0.12\\%/+0.11\\%" in statement
    assert "高预算1152/RETURN 10/8质量近似但更慢" in statement
    assert (r"$H_{\max}=2$及衰减$(0.2,0.5)/(0.35,0.6)$的Fidelity分别"
            in statement)
    assert r"下降1.60\%/4.00\%/1.10\%" in statement
    assert r"$H_{\max}=4$近似不变（Fidelity -0.04\%" in statement
    assert macros["SensitivityBudgetLowFidelityRatio"] == "1.0012"
    assert macros["SensitivityBudgetLowTimeRatio"] == "0.8100"
    assert macros["SensitivityHorizonFourFidelityRatio"] == "0.9996"
    assert not statement.endswith("。")


@pytest.mark.parametrize(
    ("m4_logs", "wording"),
    [((-1.07, -1.05, -1.03), "降低"),
     ((-0.95002, -0.95, -0.94998), "基本持平")],
)
def test_result_statement_uses_directional_non_significance_wording(
        m4_logs: tuple[float, float, float], wording: str) -> None:
    manifests = _complete_main()
    for run in manifests:
        if run.method == "M4":
            run.log_fidelity = m4_logs[run.seed]
            run.fidelity = math.exp(run.log_fidelity)
    report = aggregate_paper(
        manifests,
        frozen_suites={"zac18": ["zac_c"], "qmap154": ["qmap_c"]},
        bootstrap_iterations=20)
    statement = report["paper_values"]["macros"]["ResultStatement"]
    assert wording in statement
    assert "点估计" in statement
    assert "严格共同集合$N=" in statement
    assert "中位比" in statement
    assert "胜/平/负" in statement
    assert "提高-" not in statement
    assert "显著" not in statement
    assert not statement.endswith("。")
    coverage = report["paper_values"]["macros"]["ZACCoverageStatement"]
    assert "M3 成功1/1（完整种子1/1）" in coverage


def test_result_statement_labels_long_tail_instead_of_majority_improvement() -> None:
    suites = {
        "zac18": [f"zac_{index}" for index in range(11)],
        "qmap154": [f"qmap_{index}" for index in range(11)],
    }
    manifests: list[RunManifest] = []
    for dataset, circuits in suites.items():
        for index, circuit in enumerate(circuits):
            manifests.extend((
                _run(dataset, circuit, "M1", 0, -3.0),
                _run(dataset, circuit, "M2", 0, -3.0),
            ))
            manifests.extend(
                _run(dataset, circuit, "M3", seed, -2.99)
                for seed in (0, 1, 2))
            m4_log = -1.0 if index == 0 else -3.1
            manifests.extend(
                _run(dataset, circuit, "M4", seed, m4_log)
                for seed in (0, 1, 2))
    report = aggregate_paper(
        manifests, frozen_suites=suites, bootstrap_iterations=20)
    statement = report["paper_values"]["macros"]["ResultStatement"]
    assert "中位比和胜负分布不支持多数电路改善" in statement
    assert "去长尾后总体增益不再保持" in statement
    assert "显著" not in statement


def test_aggregate_writes_csv_json_and_tex_but_not_xlsx(tmp_path: Path) -> None:
    suites = {"zac18": ["zac_c"], "qmap154": ["qmap_c"]}
    ablation = []
    for seed in (0, 1, 2):
        ablation.append(_run(
            "zac18", "zac_c", "M4", seed, -0.9,
            variant="H0"))
        ablation.append(_run(
            "zac18", "zac_c", "M4", seed, -0.7,
            variant="H8"))
    ablation.append(_run(
        "zac18", "zac_c", "M4", 0, -0.8,
        variant="greedy_only"))
    report = aggregate_paper(
        _complete_main(), frozen_suites=suites, output_dir=tmp_path,
        ablation_manifest_inputs=ablation,
        ablation_identities=[("zac18", "zac_c")],
        bootstrap_iterations=50)
    holm = report["main_summary"]["primary_holm_family"]
    assert set(holm["tests"]) == {
        "zac18:M4_vs_Bstar", "qmap154:M4_vs_Bstar",
        "ablation:H8_vs_H0",
    }
    assert all(
        holm["adjusted_p_values"][name] >= raw
        for name, raw in holm["tests"].items())
    assert report["main_summary"]["datasets"]["zac18"]["comparisons"][
        "M4_vs_Bstar"]["robustness"]["remove_top_1"]["n"] == 0
    expected = {
        "zac18.csv", "qmap154.csv", "ablation.csv", "sensitivity.csv",
        "runtime.csv", "main_summary.json", "paper_values.json",
        "results_values_zh.tex", "final_manifest.json",
        "fig6_fidelity_gain.csv", "fig6_mechanism.csv",
        "fig6_ablation.csv", "fig6_m4_stage_time.csv",
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
    assert "\\newcommand{\\StrictMOneV}" in tex
    assert "\\newcommand{\\StrictMFourV}" in tex
    assert "\\newcommand{\\SensitivityStatement}" in tex
    assert "RETURN匹配和前瞻时间嵌套于搜索核" in tex
    assert render_results_values_tex(report["paper_values"]) == tex
    manifest = json.loads((tmp_path / "final_manifest.json").read_text())
    assert manifest["xlsx_generated_here"] is False
    assert manifest["nested_timing_semantics"]["must_not_be_summed"] is True

    with (tmp_path / "fig6_fidelity_gain.csv").open(
            newline="", encoding="utf-8") as handle:
        fidelity_rows = list(csv.DictReader(handle))
    zac_gain = next(row for row in fidelity_rows if row["dataset"] == "zac18")
    assert zac_gain["baseline_method"] == "M2"
    assert float(zac_gain["M4_minus_Bstar_delta_logF"]) == pytest.approx(0.15)
    assert float(zac_gain["M4_over_Bstar_ratio"]) == pytest.approx(math.exp(0.15))

    with (tmp_path / "fig6_mechanism.csv").open(
            newline="", encoding="utf-8") as handle:
        mechanism_rows = list(csv.DictReader(handle))
    zac_mechanism = next(
        row for row in mechanism_rows if row["dataset"] == "zac18")
    assert float(zac_mechanism["M4_minus_Bstar_transfers"]) == pytest.approx(1.0)

    with (tmp_path / "fig6_ablation.csv").open(
            newline="", encoding="utf-8") as handle:
        ablation_rows = list(csv.DictReader(handle))
    lookahead = next(
        row for row in ablation_rows if row["comparison"] == "H8_vs_H0")
    assert float(lookahead["delta_logF"]) == pytest.approx(0.2)

    with (tmp_path / "fig6_m4_stage_time.csv").open(
            newline="", encoding="utf-8") as handle:
        stage_rows = list(csv.DictReader(handle))
    zac_stage = next(row for row in stage_rows if row["dataset"] == "zac18")
    assert float(zac_stage["search_kernel_s"]) == pytest.approx(6.0)
    assert zac_stage["valid"] == "3"
    assert zac_stage["N"] == "3"
    assert zac_stage["return_match_and_forecast_nested_in_search_kernel"] == "True"


def test_publish_fig6_data_fails_closed_then_hashes_all_outputs(
        tmp_path: Path) -> None:
    delivery = tmp_path / "delivery"
    paper = tmp_path / "paper"
    delivery.mkdir()
    for name in FIG6_RAW_EXPORTS:
        (delivery / name).write_text(f"source={name}\n", encoding="utf-8")

    _write_fake_fig6_converter(paper, FIG6_DERIVED_OUTPUTS[:-1])
    with pytest.raises(ValueError, match="derived output set drift"):
        _publish_paper_fig6_data(delivery, paper)
    published = paper / "figures" / "data"
    assert not list(published.iterdir())

    converter = _write_fake_fig6_converter(paper)
    result = _publish_paper_fig6_data(delivery, paper)
    assert result["protocol"] == "paper-fig6-derived-v1"
    assert result["converter"]["sha256"] == sha256_file(converter)
    assert set(result["raw_inputs"]) == set(FIG6_RAW_EXPORTS)
    assert set(result["files"]) == set(FIG6_DERIVED_OUTPUTS)
    for name, row in result["files"].items():
        path = published / name
        assert row["sha256"] == sha256_file(path)
        assert row["bytes"] == path.stat().st_size


def test_command_aggregate_paper_integrates_frozen_sources(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    artifact = tmp_path / "artifacts"
    delivery = tmp_path / "delivery"
    paper = tmp_path / "paper"
    freeze_path = tmp_path / "freeze.json"
    freeze_path.write_text("{}\n", encoding="utf-8")
    run_map: dict[Path, RunManifest] = {}
    source = {"accepted_old_seed0": False,
              "protocol_id": "paper-protocol", "freeze_id": "f" * 64,
              "baselines": {"M1": {}, "M2": {}},
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

    def add_track(track: str, run: RunManifest) -> None:
        run.run_kind = (
            "timing" if track in {"timing", "timing-warmup"}
            else "ablation")
        path = artifact / "runs" / track / run.run_id / "manifest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(run.run_id, encoding="utf-8")
        run_map[path.resolve()] = run

    for seed in (0, 1, 2):
        add_track("ablation", _run(
            "zac18", "zac_c", "M4", seed, -0.9,
            variant=PAPER_ABLATION_VARIANTS["h0"]))
        add_track("ablation", _run(
            "zac18", "zac_c", "M4", seed, -0.8,
            variant=PAPER_ABLATION_VARIANTS["h8"]))
    add_track("ablation", _run(
        "zac18", "zac_c", "M4", 0, -0.85,
        variant=PAPER_ABLATION_VARIANTS["greedy"]))
    for dataset, circuit in (("zac18", "zac_c"),
                             ("qmap154", "qmap_c")):
        for profile in SENSITIVITY_PROFILE_IDS:
            variant = f"paper_sensitivity_{profile}"
            add_track("sensitivity", _run(
                dataset, circuit, "M4", 0, -0.8, variant=variant))
        for method in ("M1", "M2", "M3", "M4"):
            for repetition in range(3):
                add_track("timing", _run(
                    dataset, circuit, method, 0, -0.8,
                    repetition=repetition))
    hidden = artifact / "runs" / "timing" / ".incomplete.tmp" / "manifest.json"
    hidden.parent.mkdir(parents=True, exist_ok=True)
    hidden.write_text("must be ignored", encoding="utf-8")
    freeze = {
        "freeze_id": "f" * 64,
        "protocol_id": "paper-protocol",
        "abi8_wheel": {"sha256": "8" * 64},
        "canonical_suites": {
            "zac18": {"canonical_inputs": {"zac_c": "b" * 64}},
            "qmap154": {"canonical_inputs": {"qmap_c": "b" * 64}},
        },
        "ablation_cohort": {"identities": [["zac18", "zac_c"]]},
        "parity_timing_cohort": {"identities": [
            ["zac18", "zac_c"], ["qmap154", "qmap_c"]]},
    }
    monkeypatch.setattr(
        "experiments_v2.paper_protocol.load_paper_freeze",
        lambda _path: freeze)
    monkeypatch.setattr(
        "experiments_v2.paper_aggregate.load_run_manifest",
        lambda path, require_success_metrics=True: run_map[Path(path).resolve()])

    def fake_workbook(_aggregate, output, *, qa_directory, **_kwargs):
        output = Path(output)
        qa_directory = Path(qa_directory)
        output.write_bytes(b"xlsx")
        qa_directory.mkdir(parents=True)
        qa_path = qa_directory / "workbook_qa.json"
        qa_path.write_text("{}\n", encoding="utf-8")
        previews = []
        for index in range(4):
            preview = qa_directory / f"preview-{index}.png"
            preview.write_bytes(b"png")
            previews.append(str(preview))
        return {
            "xlsx_path": str(output), "qa_path": str(qa_path),
            "preview_paths": previews,
            "sheet_names": ["ZAC18", "QMAP154"],
            "row_counts": {"ZAC18": 18, "QMAP154": 154},
            "column_count": 52,
            "freeze_panes": {
                "rows": 14, "columns": 4, "top_left_cell": "E15",
                "verified": True,
            },
        }

    monkeypatch.setattr(
        "experiments_v2.paper_workbook.export_paper_workbook", fake_workbook)
    converter = _write_fake_fig6_converter(paper)
    call = dict(
        plan=SimpleNamespace(path=tmp_path / "plan.json"),
        freeze_path=freeze_path, artifact_root=artifact,
        output_root=delivery, paper_directory=paper)
    with pytest.raises(
            ValueError, match=r"timing warmup.*expected=8, found=0"):
        command_aggregate_paper(**call)

    warmups = [
        _run(dataset, circuit, method, 0, -0.8)
        for dataset, circuit in (("zac18", "zac_c"),
                                 ("qmap154", "qmap_c"))
        for method in ("M1", "M2", "M3", "M4")
    ]
    for run in warmups[:7]:
        add_track("timing-warmup", run)
    with pytest.raises(
            ValueError, match=r"timing warmup.*expected=8, found=7"):
        command_aggregate_paper(**call)
    failed_warmup = _run(
        "qmap154", "qmap_c", "M4", 0, -0.8,
        status=RunStatus.TIMEOUT.value)
    add_track("timing-warmup", failed_warmup)
    with pytest.raises(
            ValueError, match=r"requires eight successful compilations"):
        command_aggregate_paper(**call)
    add_track("timing-warmup", warmups[7])
    result = command_aggregate_paper(
        **call)
    assert result["strict_common_linear_N"] == {"zac18": 1, "qmap154": 1}
    assert (paper / "results_values_zh.tex").is_file()
    assert (delivery / "four_methods_results.xlsx").is_file()
    final = json.loads((delivery / "final_manifest.json").read_text())
    assert final["input_manifest_counts"]["main"] == 16
    assert final["input_manifest_counts"]["timing"] == 24
    assert final["input_manifest_counts"]["timing_warmup"] == 8
    assert final["timing_warmup"] == {
        "manifest_count": 8,
        "status_counts": {"success": 8},
        "circuits": {"zac18": "zac_c", "qmap154": "qmap_c"},
        "excluded_from_runtime_statistics": True,
    }
    assert set(final["git_commit_sets"]) == {
        "main", "ablation", "sensitivity", "timing", "timing_warmup"}
    assert final["xlsx_generated_here"] is True
    assert final["paper_workbook"]["sheet_names"] == ["ZAC18", "QMAP154"]
    assert final["paper_workbook"]["freeze_panes"]["verified"] is True
    assert final["paper_workbook"]["qa_artifact_sha256"] == {
        "workbook_qa.json": sha256_file(
            Path(final["paper_workbook"]["qa_path"]))
    }
    assert final["evidence_validation"]["quality"][
        "canonical_inputs_verified"] is True
    fig6 = final["paper_fig6_data"]
    assert fig6["converter"]["sha256"] == sha256_file(converter)
    assert set(fig6["files"]) == set(FIG6_DERIVED_OUTPUTS)
    assert result["paper_fig6_data"] == fig6
    for row in fig6["files"].values():
        assert row["sha256"] == sha256_file(Path(row["path"]))

    source["freeze_id"] = "0" * 64
    (artifact / "quality_source_manifest.json").write_text(
        json.dumps(source), encoding="utf-8")
    with pytest.raises(ValueError, match="does not belong to the loaded freeze"):
        command_aggregate_paper(
            plan=SimpleNamespace(path=tmp_path / "plan.json"),
            freeze_path=freeze_path, artifact_root=artifact,
            output_root=delivery, paper_directory=paper)
