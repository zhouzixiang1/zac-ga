from __future__ import annotations

import csv
import json
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from experiments_v2.paper_workbook import export_paper_workbook


METHODS = ("M1", "M2", "M3", "M4")
CORE = (
    "fidelity", "transfers", "idle_exposures", "move_batches",
    "move_time_us", "algorithm_time_s", "status", "valid_over_N",
)
TIMING = (
    "initial_placement_s", "transition_decision_s",
    "problem_preparation_s", "search_kernel_s", "return_match_s",
    "forecast_s", "result_commit_s", "routing_s",
)


def _write_fixture(root: Path) -> None:
    datasets = (("zac18", "ZAC18", 18), ("qmap154", "QMAP154", 154))
    summary = {
        "protocol": "paper-zh-v1-main-summary-v1",
        "datasets": {},
    }
    fieldnames = ["dataset", "circuit", "qubits", "gates_1q", "gates_2q"]
    for method in METHODS:
        fieldnames.extend(f"{method}__{field}" for field in CORE)
        fieldnames.extend((f"{method}__valid", f"{method}__N"))
    for method in ("M3", "M4"):
        fieldnames.extend(f"{method}__{field}" for field in TIMING)
    for dataset, _sheet, count in datasets:
        rows = []
        for index in range(count):
            row = {
                "dataset": dataset,
                "circuit": f"{dataset}_circuit_{index:03d}",
                "qubits": 10 + index % 5,
                "gates_1q": 20 + index,
                "gates_2q": 30 + index,
            }
            for method_index, method in enumerate(METHODS):
                expected = 1 if method in {"M1", "M2"} else 3
                row.update({
                    f"{method}__fidelity": 0.5 + method_index * 0.01,
                    f"{method}__transfers": 100 + method_index,
                    f"{method}__idle_exposures": 20 + method_index,
                    f"{method}__move_batches": 10 + method_index,
                    f"{method}__move_time_us": 1000 + 100 * method_index,
                    f"{method}__algorithm_time_s": 2 + method_index,
                    f"{method}__status": "success",
                    f"{method}__valid_over_N": f"{expected}/{expected}",
                    f"{method}__valid": expected,
                    f"{method}__N": expected,
                })
            for method_index, method in enumerate(("M3", "M4"), start=3):
                for timing_index, field in enumerate(TIMING, start=1):
                    row[f"{method}__{field}"] = method_index + timing_index / 10
            rows.append(row)
        with (root / f"{dataset}.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        methods = {}
        for method_index, method in enumerate(METHODS):
            methods[method] = {
                "fidelity_geometric_mean": 0.5 + method_index * 0.01,
                "transfers_arithmetic_mean": 100 + method_index,
                "idle_exposures_arithmetic_mean": 20 + method_index,
                "move_batches_arithmetic_mean": 10 + method_index,
                "move_time_us_arithmetic_mean": 1000 + 100 * method_index,
                "algorithm_time_s_arithmetic_mean": 2 + method_index,
                "coverage": {
                    "success_circuits": count,
                    "complete_seed_circuits": count,
                    "N": count,
                },
            }
        comparisons = {}
        for method, ratio in (("M3", 1.01), ("M4", 1.04)):
            comparisons[f"{method}_vs_Bstar"] = {
                "geometric_mean_ratio": ratio,
                "bootstrap": {"ci95_low": ratio - 0.01, "ci95_high": ratio + 0.01},
                "wins": count - 2, "ties": 1, "losses": 1,
                "strict_common_linear_N": count,
            }
        summary["datasets"][dataset] = {
            "circuit_N": count,
            "strict_common_linear_N": count,
            "methods": methods,
            "comparisons": comparisons,
        }
    (root / "main_summary.json").write_text(
        json.dumps(summary), encoding="utf-8")
    (root / "unrelated_large.csv").write_text("ignored\n1\n", encoding="utf-8")


def test_artifact_tool_workbook_has_exact_two_sheets_and_52_columns(
        tmp_path: Path) -> None:
    aggregate = tmp_path / "aggregate"
    aggregate.mkdir()
    _write_fixture(aggregate)
    output = tmp_path / "four_methods_results.xlsx"
    qa = tmp_path / "qa"
    result = export_paper_workbook(aggregate, output, qa_directory=qa)
    assert result["sheet_names"] == ["ZAC18", "QMAP154"]
    assert result["row_counts"] == {"ZAC18": 18, "QMAP154": 154}
    assert result["column_count"] == 52
    assert output.is_file()
    assert len(result["preview_paths"]) == 4
    qa_payload = json.loads(Path(result["qa_path"]).read_text(encoding="utf-8"))
    assert "M1/M2 seed0一次；M3/M4三种子中位数" in qa_payload["sheets"][0][
        "inspection_ndjson"]
    with zipfile.ZipFile(output) as archive:
        workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        namespace = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        names = [node.attrib["name"] for node in workbook.findall(".//x:sheet", namespace)]
        assert names == ["ZAC18", "QMAP154"]


def test_renderer_is_artifact_tool_only() -> None:
    renderer = (Path(__file__).resolve().parents[1] / "experiments_v2" /
                "render_paper_workbook.mjs")
    source = renderer.read_text(encoding="utf-8")
    assert "@oai/artifact-tool" in source
    assert "openpyxl" not in source
    assert "xlsxwriter" not in source
    assert "freezeRows(14)" in source
    assert "freezeColumns(4)" in source
