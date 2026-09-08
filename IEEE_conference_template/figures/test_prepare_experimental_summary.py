from __future__ import annotations

from pathlib import Path

import pytest

import prepare_experimental_summary as summary


def _sha(index: int) -> str:
    return f"{index:064x}"


def _gain_row(
    dataset: str,
    circuit: str,
    canonical: str,
    delta: float,
    *,
    cluster_n: int = 1,
    baseline_fidelity: float = 0.5,
) -> dict[str, str]:
    normalized = summary._dataset(dataset)
    return {
        "dataset": dataset,
        "circuit": circuit,
        "canonical_sha256": canonical,
        "independent_analysis_unit": (
            "circuit_file" if normalized == "ZAC18"
            else "canonical_sha256_cluster_mean"
        ),
        "canonical_cluster_file_N": str(cluster_n),
        "strict_paired": "true",
        "baseline_method": "M1",
        "baseline_log_fidelity": "-0.6931471805599453",
        "baseline_fidelity": str(baseline_fidelity),
        "M4_log_fidelity": str(-0.6931471805599453 + delta),
        "M4_fidelity": str(baseline_fidelity * (1.0 + delta)),
        "M4_minus_Bstar_delta_logF": str(delta),
        "M4_over_Bstar_ratio": str(1.0 + delta),
        "M4_valid_over_N": "3/3",
    }


def _detail_row(circuit: str, canonical: str) -> dict[str, str]:
    return {
        "circuit": circuit,
        "canonical_sha256": canonical,
        "qubits": "8",
        "gates_2q": "24",
        "M1__fidelity": "0.50",
        "M2__fidelity": "0.49",
        "M3__fidelity": "0.51",
        "M4__fidelity": "0.55",
    }


def _stage_row(*, status: str = "success", valid: str = "3",
               expected: str = "3", full: str = "12") -> dict[str, str]:
    return {
        "dataset": "zac18",
        "circuit": "c",
        "status": status,
        "valid": valid,
        "N": expected,
        "full_compile_s": full,
        "initial_placement_s": "1",
        "problem_preparation_s": "2",
        "search_kernel_s": "6",
        "result_commit_s": "2",
        "routing_s": "1",
    }


def test_ablation_identity_includes_dataset_and_rejects_duplicates() -> None:
    rows = []
    for comparison in ("H8_vs_H0", "GA_vs_greedy"):
        rows.extend((
            {"dataset": "zac18", "circuit": "same", "comparison": comparison,
             "paired_valid": "true", "delta_logF": "0.1"},
            {"dataset": "qmap154", "circuit": "same", "comparison": comparison,
             "paired_valid": "true", "delta_logF": "0.3"},
        ))
    result = summary._ablation(rows, "ablation.csv")
    assert result["H8/H0"]["n"] == 2
    assert result["H8/H0"]["delta_logF"] == pytest.approx(0.2)

    rows.append(dict(rows[0]))
    with pytest.raises(ValueError, match="duplicate paired rows"):
        summary._ablation(rows, "ablation.csv")


def test_stage_requires_complete_three_seed_rows() -> None:
    partial = _stage_row(status="partial", valid="2")
    complete = _stage_row()
    complete["circuit"] = "complete"
    result = summary._stage([partial, complete], "stage.csv")
    assert result["n"] == 1
    assert result["additive_sum_s"] == pytest.approx(12.0)

    inconsistent = _stage_row(status="success", valid="2")
    with pytest.raises(ValueError, match="success/seed-completeness mismatch"):
        summary._stage([inconsistent], "stage.csv")


def test_stage_rejects_double_counted_or_overlapping_totals() -> None:
    row = _stage_row(full="11")
    with pytest.raises(ValueError, match="additive stages exceed"):
        summary._stage([row], "stage.csv")


def test_stage_allows_small_independent_median_discrepancy() -> None:
    row = _stage_row(full="11.95")
    result = summary._stage([row], "stage.csv")
    assert result["n"] == 1
    assert result["additive_sum_s"] == pytest.approx(12.0)


def test_qmap_display_profile_preserves_and_marks_long_tails() -> None:
    values = [float(index) / 100.0 for index in range(-60, 61)] + [10.0]
    profile = summary._display_profile(values, robust=True)
    assert profile["true_high"] == 10.0
    assert profile["clip_high"] < profile["true_high"]
    assert profile["high_tail_n"] > 0
    assert profile["axis_low"] < 0 < profile["axis_high"]


def test_small_display_profile_uses_full_range() -> None:
    values = [-0.2, 0.1, 4.0]
    profile = summary._display_profile(values, robust=True)
    assert profile["clip_low"] == -0.2
    assert profile["clip_high"] == 4.0
    assert profile["low_tail_n"] == 0
    assert profile["high_tail_n"] == 0


def test_fidelity_uses_qmap_canonical_units_and_zac_files() -> None:
    duplicate_hash = _sha(20)
    rows = [
        _gain_row("zac18", "z0", _sha(1), -0.1),
        _gain_row("zac18", "z1", _sha(2), 0.1),
        _gain_row("qmap154", "qz", duplicate_hash, 0.2, cluster_n=2),
        _gain_row("qmap154", "qa", duplicate_hash, 0.2, cluster_n=2),
        _gain_row("qmap154", "qb", _sha(21), -0.2),
    ]
    result = summary._fidelity(rows, "gain.csv")
    assert len(result["ZAC18"]) == 2
    assert len(result["QMAP154"]) == 2
    duplicate_unit = next(row for row in result["QMAP154"]
                          if row[1] == duplicate_hash)
    assert duplicate_unit[0] == "qa"
    assert summary._win_tie_loss(result["ZAC18"]) == (1, 0, 1)
    assert summary._win_tie_loss(result["QMAP154"]) == (1, 0, 1)


def test_qmap_cluster_members_must_share_cluster_mean_values() -> None:
    canonical = _sha(30)
    rows = [
        _gain_row("zac18", "z0", _sha(1), 0.1),
        _gain_row("qmap154", "qa", canonical, 0.2, cluster_n=2),
        _gain_row("qmap154", "qb", canonical, 0.3, cluster_n=2),
    ]
    with pytest.raises(ValueError, match="cluster-mean field"):
        summary._fidelity(rows, "gain.csv")


def test_representative_cases_do_not_repeat_qmap_cluster() -> None:
    duplicate_hash = _sha(20)
    rows = [
        _gain_row("zac18", "z0", _sha(1), 0.1),
        _gain_row("zac18", "z1", _sha(2), 0.2),
        _gain_row("zac18", "z2", _sha(3), 0.3),
        _gain_row("qmap154", "q0", _sha(10), 0.1),
        _gain_row("qmap154", "q1", _sha(11), 0.2),
        _gain_row("qmap154", "qz", duplicate_hash, 0.3, cluster_n=2),
        _gain_row("qmap154", "qa", duplicate_hash, 0.3, cluster_n=2),
    ]
    details = {
        "ZAC18": [
            _detail_row("z0", _sha(1)),
            _detail_row("z1", _sha(2)),
            _detail_row("z2", _sha(3)),
        ],
        "QMAP154": [
            _detail_row("q0", _sha(10)),
            _detail_row("q1", _sha(11)),
            _detail_row("qz", duplicate_hash),
            _detail_row("qa", duplicate_hash),
        ],
    }
    selected = summary._representative_cases(rows, details)
    qmap = selected["QMAP154"]
    assert len({row["canonical_sha256"] for row in qmap}) == 3
    assert "qa" in {row["circuit"] for row in qmap}
    assert "qz" not in {row["circuit"] for row in qmap}
    assert [row["role"] for row in qmap] == ["median", "tail-1", "tail-2"]


def test_ranked_fidelity_rejects_duplicates_unsorted_and_nonpositive() -> None:
    valid = {
        "ZAC18": [
            ("z0", _sha(1), -0.1, 0.9),
            ("z1", _sha(2), 0.2, 1.2),
        ],
        "QMAP154": [
            ("q0", _sha(10), -0.2, 0.8),
            ("q1", _sha(11), 0.3, 1.3),
        ],
    }
    summary._validate_ranked_fidelity(valid)

    duplicate = {**valid, "ZAC18": [
        ("z0", _sha(1), -0.1, 0.9),
        ("z0", _sha(2), 0.2, 1.2),
    ]}
    with pytest.raises(ValueError, match="duplicate circuits"):
        summary._validate_ranked_fidelity(duplicate)

    repeated_cluster = {**valid, "QMAP154": [
        ("q0", _sha(10), -0.2, 0.8),
        ("q1", _sha(10), 0.3, 1.3),
    ]}
    with pytest.raises(ValueError, match="duplicate canonical QMAP clusters"):
        summary._validate_ranked_fidelity(repeated_cluster)

    unsorted = {**valid, "ZAC18": [
        ("z0", _sha(1), 0.2, 1.2),
        ("z1", _sha(2), -0.1, 0.9),
    ]}
    with pytest.raises(ValueError, match="not sorted"):
        summary._validate_ranked_fidelity(unsorted)

    nonpositive = {**valid, "ZAC18": [("z0", _sha(1), -0.1, 0.0)]}
    with pytest.raises(ValueError, match="non-positive ratio"):
        summary._validate_ranked_fidelity(nonpositive)


def test_emit_records_display_values_and_tail_metadata(tmp_path) -> None:
    fidelity = {
        "ZAC18": [
            ("z0", _sha(1), -0.1, 0.9),
            ("z1", _sha(2), 0.2, 1.2),
        ],
        "QMAP154": [
            (f"q{index}", _sha(index + 100), float(index) / 20.0, 1.0)
            for index in range(-60, 61)
        ] + [("outlier", _sha(999), 20.0, 1.0)],
    }
    mechanism = {
        dataset: {"transfer": 0.1, "idle": -0.2, "coherence": 0.3, "n": 2}
        for dataset in ("ZAC18", "QMAP154")
    }
    ablation = {
        comparison: {"delta_logF": 0.1, "ratio": 1.1, "n": 2}
        for comparison in ("H8/H0", "GA/greedy")
    }
    stage = {
        "initial_pct": 10.0, "prepare_pct": 10.0, "search_pct": 60.0,
        "commit_pct": 10.0, "routing_pct": 10.0,
        "full_compile_s": 12.0, "additive_sum_s": 10.0, "n": 2,
    }
    representative = {}
    for dataset in ("ZAC18", "QMAP154"):
        representative[dataset] = []
        for index, role in enumerate(("median", "tail-1", "tail-2")):
            representative[dataset].append({
                "dataset": dataset,
                "circuit": f"{dataset.lower()}_{index}",
                "canonical_sha256": _sha(index + 200),
                "role": role,
                "qubits": 4 + index,
                "gates_2q": 10 + index,
                "ratio": 1.0 + 0.1 * index,
                "fidelities": {
                    "ZAC": 0.8,
                    "ICCAD/QMAP": 0.82,
                    "GA-NL": 0.81,
                    "GA-LK": 0.84,
                },
            })
    summary._emit(
        tmp_path, fidelity, mechanism, ablation, stage, representative)
    qmap = (tmp_path / "fig6_qmap_fidelity.dat").read_text()
    zac = (tmp_path / "fig6_zac_fidelity.dat").read_text()
    meta = (tmp_path / "fig6_meta.tex").read_text()
    selected = (tmp_path / "fig6_selected_cases.tex").read_text()
    selected_data = (
        tmp_path / "fig6_selected_cases.dat").read_text().splitlines()
    assert qmap.splitlines()[0] == "rank delta_logF display_delta tail ratio"
    assert len(zac.splitlines()) == 3
    assert len(qmap.splitlines()) == 123
    assert qmap.splitlines()[-1].split()[1] == "20"
    assert "\\def\\FigSixQMAPHighTailN{" in meta
    assert "\\def\\FigSixQMAPTrueMax{20}" in meta
    assert "\\def\\FigSixZACWTL{1/0/1}" in meta
    assert "\\def\\FigSixQMAPWTL{61/1/60}" in meta
    assert selected_data[0] == (
        "x gain_pct ratio dataset ratio_zac ratio_iccad "
        "gain_zac_pct gain_iccad_pct")
    assert len(selected_data) == 5
    assert all(float(line.split()[4]) == pytest.approx(1.05)
               for line in selected_data[1:])
    assert all(float(line.split()[5]) == pytest.approx(0.84 / 0.82)
               for line in selected_data[1:])
    assert "高增益" not in selected
    assert "中位" not in selected


def test_figure_uses_zero_based_gains_from_direct_baseline_ratios() -> None:
    figure = (Path(__file__).with_name("experimental_summary.tex")
              .read_text(encoding="utf-8"))
    for panel in ("(a) Overall fidelity improvement", "(b) Controlled comparisons",
                  "(c) QFT-18 vs. ZAC"):
        assert panel in figure
    for macro, formula in {
            "FigSixZACvsZAC": r"100*(\DefaultZACMFourF/\DefaultZACMOneF-1)",
            "FigSixZACvsICCAD": r"100*(\DefaultZACMFourF/\DefaultZACMTwoF-1)",
            "FigSixQMAPvsZAC": r"100*(\DefaultQMAPMFourF/\DefaultQMAPMOneF-1)",
            "FigSixQMAPvsICCAD": r"100*(\DefaultQMAPMFourF/\DefaultQMAPMTwoF-1)",
            "ArgumentGAGain": r"100*(\AblationGARatio-1)",
            "ArgumentLookaheadGain": r"100*(\AblationHRatio-1)"}.items():
        assert rf"\edef\{macro}{{\fpeval{{{formula}}}}}" in figure
    axes = figure.split(r"\begin{axis}")[1:]
    assert len(axes) == 3
    assert all("ymin=0,ymax=30" in axis.split(r"\end{axis}", 1)[0]
               for axis in axes[:2])
    for point in (r"(1,\ArgumentGAGain)", r"(2,\ArgumentLookaheadGain)"):
        assert point in axes[1]
    assert "ymin=-.055,ymax=.30" in axes[2]
    assert "Change in log fidelity" in axes[2]
    for point in (r"(1,\MechanismQFTTransferGain)",
                  r"(2,\MechanismQFTExcitationGain)",
                  r"(3,\MechanismQFTCoherenceGain)"):
        assert point in axes[2]
    assert "Selected circuit improvements" not in figure
    assert "fig6_selected_cases.dat" not in figure
    assert "W/T/L" not in figure
    assert "Ordered $\\Delta\\log F$ distribution" not in figure


def test_checked_in_v2_independent_unit_contract() -> None:
    data_root = Path(__file__).with_name("data")
    meta = (data_root / "fig6_meta.tex").read_text(encoding="utf-8")
    zac_lines = (data_root / "fig6_zac_fidelity.dat").read_text(
        encoding="utf-8").splitlines()
    qmap_lines = (data_root / "fig6_qmap_fidelity.dat").read_text(
        encoding="utf-8").splitlines()
    selected_data = (data_root / "fig6_selected_cases.dat").read_text(
        encoding="utf-8").splitlines()
    selected = (data_root / "fig6_selected_cases.tex").read_text(
        encoding="utf-8")

    assert "\\def\\FigSixZACN{18}" in meta
    assert "\\def\\FigSixQMAPN{120}" in meta
    assert "\\def\\FigSixZACWTL{12/0/6}" in meta
    assert "\\def\\FigSixQMAPWTL{49/0/71}" in meta
    assert len(zac_lines) == 19
    assert len(qmap_lines) == 121
    assert selected_data[0].endswith(
        "ratio_zac ratio_iccad gain_zac_pct gain_iccad_pct")
    assert len(selected_data) == 5
    assert all(float(line.split()[4]) > 1.0 for line in selected_data[1:])
    assert all(float(line.split()[5]) > 1.0 for line in selected_data[1:])
    assert selected.count("ZAC18 &") == 2
    assert selected.count("QMAP154 &") == 2
    assert "高增益" not in selected
    assert "中位" not in selected
