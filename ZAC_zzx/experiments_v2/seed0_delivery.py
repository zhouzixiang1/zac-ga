"""Build the pragmatic two-sheet seed-0 delivery from explicit run roots.

The paper baselines are selected by a fixed source-priority rule.  M3/M4 are
accepted only from the current native result roots.  No metric-based result
selection is performed.  The output is a CSV/workbook contract consumed by
``render_final_workbook.mjs``; the renderer is the only component that writes
the XLSX file.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import statistics
from pathlib import Path
from typing import Any, Iterable, Mapping

from .plan import load_experiment_plan


REPO = Path(__file__).resolve().parents[1]
WORKTREE = REPO.parent
ARTIFACTS = (
    WORKTREE / "fidelity-lookahead-v2" / "artifacts" / "native-ga-v1")
PLAN = REPO / "experiments_v2" / "experiment_plan_v2.json"
METHODS = ("M1", "M2", "M3", "M4")
SHEETS = {"zac18": "ZAC18", "qmap154": "QMAP154"}
OVERALL = "整体汇总"
TERMINAL = {
    "success", "timeout", "oom", "compiler_error", "verifier_fail",
    "scorer_error",
}

M1_ROOTS = (
    ARTIFACTS / "tuning-quality-v1-51583a5/attempts/baselines/M1/paper-original",
    ARTIFACTS / "baseline-m1-paper-native-v1/qmap154",
    ARTIFACTS / "analysis-v18-zac18-seed0/attempts/M1-missing",
)
M2_ROOTS = (ARTIFACTS / "runs/main",)
M3_ROOTS = (
    ARTIFACTS / "m3-zac18-current-final-v26/attempts",
    ARTIFACTS / "m3-qmap154-current-final-v27-urf3-early/attempts",
    ARTIFACTS / "m3-qmap154-current-final-v27/attempts",
)
M4_ROOTS = (
    ARTIFACTS / "m4-zac18-current-final-v25/attempts",
    ARTIFACTS / "m4-qmap154-current-final-v17/attempts",
)

CORE_METRICS = (
    ("fidelity", "Fidelity（线性模型）", "0.000000E+00"),
    ("move_batches", "Move批次", "0.00"),
    ("move_time_ms", "Move时间 (ms)", "0.000"),
    ("algorithm_time_s", "算法时间 (s)", "0.000000"),
    ("valid_over_N", "valid/N", "text"),
    ("status", "状态", "text"),
)

OURS_TIMING = (
    ("initial_placement_s", "初始布局 (s)", "initial_placement_ns"),
    ("transition_decision_s", "逐层决策总计 (s)", "transition_decision_ns"),
    ("problem_preparation_s", "问题构造 (s)", "problem_preparation_ns"),
    ("search_kernel_s", "搜索阶段墙钟 (s)", "search_kernel_ns"),
    ("result_commit_s", "结果提交 (s)", "result_commit_ns"),
    ("transition_residual_s", "决策其余开销 (s)", None),
    ("routing_s", "最终路由 (s)", "routing_ns"),
    ("return_match_s", "RETURN匹配累计·嵌套 (s)", "return_match_ns"),
    ("forecast_s", "前瞻累计·嵌套 (s)", "forecast_ns"),
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_hidden(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    return any(part.startswith(".") for part in relative.parts)


def _read_manifests(root: Path, method: str) -> dict[tuple[str, str], tuple[dict, Path]]:
    selected: dict[tuple[str, str], tuple[float, dict, Path]] = {}
    if not root.exists():
        return {}
    for path in root.rglob("manifest.json"):
        if _is_hidden(path, root):
            continue
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (manifest.get("experiment_schema") != 2
                or manifest.get("method") != method
                or manifest.get("seed") != 0
                or manifest.get("repetition") != 0
                or manifest.get("status") not in TERMINAL):
            continue
        key = (str(manifest.get("dataset")), str(manifest.get("circuit")))
        candidate = (path.stat().st_mtime, manifest, path)
        if key not in selected or candidate[0] > selected[key][0]:
            selected[key] = candidate
    return {key: (value[1], value[2]) for key, value in selected.items()}


def _priority_selection(method: str, roots: Iterable[Path]) -> dict[tuple[str, str], tuple[dict, Path]]:
    result: dict[tuple[str, str], tuple[dict, Path]] = {}
    for root in roots:
        for key, value in _read_manifests(root, method).items():
            result.setdefault(key, value)
    return result


def _valid(manifest: Mapping[str, Any] | None, method: str) -> bool:
    if not manifest or manifest.get("status") != "success":
        return False
    if manifest.get("verifier_ok") is not True:
        return False
    expected = manifest.get("expected_gate_ledger_sha256")
    observed = manifest.get("observed_gate_ledger_sha256")
    if not expected or expected != observed:
        return False
    if method in {"M3", "M4"}:
        if (manifest.get("backend") != "native"
                or manifest.get("ghost_hits") != 0
                or not manifest.get("native_wheel_sha256")):
            return False
    return True


def _seconds(manifest: Mapping[str, Any], field: str) -> float | None:
    value = manifest.get(field)
    if value is None:
        return None
    value = float(value)
    return value / 1_000_000_000.0 if math.isfinite(value) and value >= 0 else None


def _number(manifest: Mapping[str, Any], field: str, scale: float = 1.0) -> float | None:
    value = manifest.get(field)
    if value is None:
        return None
    value = float(value)
    return value * scale if math.isfinite(value) and value >= 0 else None


def _fidelity(manifest: Mapping[str, Any]) -> float | None:
    if manifest.get("fidelity_ood"):
        return None
    value = manifest.get("fidelity")
    if value is None:
        return None
    value = float(value)
    return value if math.isfinite(value) and value > 0 else None


def _es_fidelity(manifest: Mapping[str, Any]) -> float | None:
    value = manifest.get("exponential_sensitivity_fidelity")
    if value is not None:
        value = float(value)
        if math.isfinite(value) and value > 0:
            return value
    log_value = manifest.get("exponential_sensitivity_log_fidelity")
    if log_value is None:
        return None
    log_value = float(log_value)
    if not math.isfinite(log_value) or log_value <= -745:
        return None
    return math.exp(log_value)


def _algorithm_time(manifest: Mapping[str, Any]) -> float | None:
    for field in ("compiler_time_ns", "full_compile_ns"):
        value = _seconds(manifest, field)
        if value is not None:
            return value
    return None


def _gm(values: Iterable[float | None]) -> float | None:
    materialized = [value for value in values if value is not None]
    if not materialized or any(value <= 0 for value in materialized):
        return None
    return math.exp(math.fsum(math.log(value) for value in materialized)
                    / len(materialized))


def _mean(values: Iterable[float | None]) -> float | None:
    materialized = [value for value in values if value is not None]
    return statistics.fmean(materialized) if materialized else None


def _columns() -> list[dict[str, Any]]:
    columns = [
        {"key": "circuit", "label": "电路", "group": None, "type": "text"},
        {"key": "qubits", "label": "量子比特", "group": None,
         "type": "number", "number_format": "0"},
        {"key": "gates_1q", "label": "1Q门数", "group": None,
         "type": "number", "number_format": "0"},
        {"key": "gates_2q", "label": "2Q门数", "group": None,
         "type": "number", "number_format": "0"},
    ]
    for method in METHODS:
        for suffix, label, number_format in CORE_METRICS:
            columns.append({
                "key": f"{method}__{suffix}", "label": label,
                "group": method,
                "type": "text" if number_format == "text" else "number",
                "number_format": number_format,
            })
    # Keep the four primary method blocks adjacent, so the first viewport is a
    # direct four-way table.  The requested M3/M4 time decomposition follows to
    # the right and can be inspected by horizontal scrolling.
    for method in ("M3", "M4"):
        for suffix, label, _field in OURS_TIMING:
            columns.append({
                "key": f"{method}__{suffix}", "label": label,
                "group": method, "type": "number",
                "number_format": "0.000000",
            })
    return columns


def _display_name(circuit: str) -> str:
    return circuit[:-11] if circuit.endswith("_transpiled") else circuit


def _write_csv(path: Path, columns: list[dict[str, Any]], rows: list[dict[str, Any]]) -> None:
    keys = [column["key"] for column in columns]
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=keys, extrasaction="raise")
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _write_summary(path: Path, report: Mapping[str, Any]) -> None:
    lines = [
        "# M1/M2/M3/M4 seed0 对比汇总",
        "",
        "四项主指标均在四方法共同通过验证且线性保真度有效的配对电路上汇总。",
        "Fidelity 使用几何均值；Move 与算法时间使用算术均值。",
        "",
    ]
    for dataset in ("zac18", "qmap154"):
        payload = report["datasets"][dataset]
        lines.extend((
            f"## {SHEETS[dataset]}",
            "",
            f"共同有效电路：{payload['paired_linear_fidelity_count']}/{payload['circuit_count']}",
            "",
            "| 方法 | Fidelity | Move批次 | Move时间(ms) | 算法时间(s) | valid/N |",
            "|---|---:|---:|---:|---:|---:|",
        ))
        for method in METHODS:
            value = payload["methods"][method]
            lines.append(
                f"| {method} | {value['paired_fidelity_geometric_mean']:.9g} | "
                f"{value['paired_move_batches_mean']:.3f} | "
                f"{value['paired_move_time_ms_mean']:.6f} | "
                f"{value['paired_algorithm_time_s_mean']:.6f} | "
                f"{value['success']}/{value['N']} |"
            )
        lines.append("")
        for method in ("M3", "M4"):
            ratio = payload["methods"][method].get(
                "fidelity_ratio_vs_per_circuit_Bstar")
            if ratio is not None:
                lines.append(
                    f"- {method} 相对逐电路最强基线 B* 的 Fidelity 几何均值比："
                    f"{ratio:.6f}（变化 {(ratio - 1.0) * 100:+.2f}%）")
        lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build(output: Path, *, allow_incomplete: bool = False) -> dict[str, Any]:
    plan = load_experiment_plan(PLAN)
    selections = {
        "M1": _priority_selection("M1", M1_ROOTS),
        "M2": _priority_selection("M2", M2_ROOTS),
        "M3": _priority_selection("M3", M3_ROOTS),
        "M4": _priority_selection("M4", M4_ROOTS),
    }
    columns = _columns()
    output.mkdir(parents=True, exist_ok=True)
    sheets = []
    all_rows: dict[str, list[dict[str, Any]]] = {}
    provenance: dict[str, Any] = {method: {} for method in METHODS}
    report: dict[str, Any] = {
        "experiment_schema": 2,
        "report_id": "seed0-four-method-two-sheet-v1",
        "datasets": {},
    }

    for dataset_name, sheet_name in SHEETS.items():
        suite = plan.load_suite(plan.datasets[dataset_name])
        circuit_rows: list[dict[str, Any]] = []
        by_circuit: dict[str, dict[str, Mapping[str, Any] | None]] = {}
        missing: list[str] = []
        for canonical in suite:
            circuit = Path(canonical.canonical_path).stem
            row: dict[str, Any] = {
                "circuit": _display_name(circuit),
                "qubits": canonical.qubits,
                "gates_1q": canonical.gates_1q,
                "gates_2q": canonical.gates_2q,
            }
            by_circuit[circuit] = {}
            for method in METHODS:
                selected = selections[method].get((dataset_name, circuit))
                manifest, path = selected if selected else (None, None)
                by_circuit[circuit][method] = manifest
                valid = _valid(manifest, method)
                if manifest is None:
                    missing.append(f"{dataset_name}/{circuit}/{method}")
                if path is not None:
                    provenance[method][f"{dataset_name}/{circuit}"] = {
                        "path": str(path), "sha256": _sha256(path),
                        "status": manifest.get("status"),
                    }
                row[f"{method}__fidelity"] = _fidelity(manifest) if valid else None
                row[f"{method}__move_batches"] = (
                    _number(manifest, "move_batches") if valid else None)
                row[f"{method}__move_time_ms"] = (
                    _number(manifest, "move_time_us", 0.001) if valid else None)
                row[f"{method}__algorithm_time_s"] = (
                    _algorithm_time(manifest) if valid else None)
                row[f"{method}__valid_over_N"] = "1/1" if valid else "0/1"
                status = manifest.get("status") if manifest else "missing"
                if valid and manifest.get("fidelity_ood"):
                    status = "success（线性F OOD）"
                elif valid:
                    status = "success"
                elif manifest and manifest.get("status") == "success":
                    status = "success但验证契约不满足"
                row[f"{method}__status"] = status
                if method in {"M3", "M4"}:
                    for suffix, _label, field in OURS_TIMING:
                        if not valid:
                            value = None
                        elif field is None:
                            components = (
                                manifest.get("problem_preparation_ns"),
                                manifest.get("search_kernel_ns"),
                                manifest.get("result_commit_ns"),
                            )
                            total = manifest.get("transition_decision_ns")
                            if total is None or any(item is None for item in components):
                                value = None
                            else:
                                residual = int(total) - sum(int(item) for item in components)
                                value = residual / 1_000_000_000.0 if residual >= 0 else None
                        else:
                            value = _seconds(manifest, field)
                        row[f"{method}__{suffix}"] = value
            circuit_rows.append(row)

        if missing and not allow_incomplete:
            raise RuntimeError(
                f"{dataset_name} lacks {len(missing)} required manifests: "
                + ", ".join(missing[:12]))

        paired = []
        for canonical, row in zip(suite, circuit_rows):
            circuit = Path(canonical.canonical_path).stem
            manifests = by_circuit[circuit]
            if all(_valid(manifests[method], method) for method in METHODS) and all(
                    row.get(f"{method}__fidelity") is not None
                    for method in METHODS):
                paired.append(row)

        overall: dict[str, Any] = {
            "circuit": OVERALL,
            "qubits": None,
            "gates_1q": None,
            "gates_2q": None,
        }
        dataset_summary: dict[str, Any] = {
            "circuit_count": len(suite),
            "paired_linear_fidelity_count": len(paired),
            "methods": {},
        }
        for method in METHODS:
            successes = sum(
                _valid(by_circuit[Path(item.canonical_path).stem][method], method)
                for item in suite)
            overall[f"{method}__fidelity"] = _gm(
                row[f"{method}__fidelity"] for row in paired)
            for suffix in ("move_batches", "move_time_ms", "algorithm_time_s"):
                overall[f"{method}__{suffix}"] = _mean(
                    row[f"{method}__{suffix}"] for row in paired)
            overall[f"{method}__valid_over_N"] = f"{successes}/{len(suite)}"
            overall[f"{method}__status"] = f"共同线性F={len(paired)}"
            if method in {"M3", "M4"}:
                for suffix, _label, _field in OURS_TIMING:
                    overall[f"{method}__{suffix}"] = _mean(
                        row[f"{method}__{suffix}"] for row in paired)
            dataset_summary["methods"][method] = {
                "success": successes,
                "N": len(suite),
                "paired_fidelity_geometric_mean": overall[f"{method}__fidelity"],
                "paired_move_batches_mean": overall[f"{method}__move_batches"],
                "paired_move_time_ms_mean": overall[f"{method}__move_time_ms"],
                "paired_algorithm_time_s_mean": overall[
                    f"{method}__algorithm_time_s"],
            }
        for method in ("M3", "M4"):
            baseline = max(
                overall["M1__fidelity"] or 0.0,
                overall["M2__fidelity"] or 0.0,
            )
            dataset_summary["methods"][method]["fidelity_ratio_vs_aggregate_Bstar"] = (
                overall[f"{method}__fidelity"] / baseline
                if baseline > 0 and overall[f"{method}__fidelity"] is not None
                else None)
            per_circuit_ratios = [
                row[f"{method}__fidelity"] / max(
                    row["M1__fidelity"], row["M2__fidelity"])
                for row in paired
            ]
            dataset_summary["methods"][method][
                "fidelity_ratio_vs_per_circuit_Bstar"] = _gm(
                    per_circuit_ratios)
        dataset_summary["per_circuit_Bstar_fidelity_geometric_mean"] = _gm(
            max(row["M1__fidelity"], row["M2__fidelity"])
            for row in paired)
        all_rows[sheet_name] = [overall, *circuit_rows]
        report["datasets"][dataset_name] = dataset_summary
        csv_name = f"{dataset_name}.csv"
        _write_csv(output / csv_name, columns, all_rows[sheet_name])
        sheets.append({
            "name": sheet_name,
            "source_csv": csv_name,
            "header_rows": 2,
            "frozen_row_order": [row["circuit"] for row in all_rows[sheet_name]],
            "columns": columns,
            "preview_last_column": "AB",
            "detail_preview_first_column": "AC",
            "detail_preview_last_column": "AT",
            "freeze_columns": 4,
        })

    contract = {
        "experiment_schema": 2,
        "contract_id": "native-ga-v1-two-sheet-results-v1",
        "exact_sheet_count": 2,
        "sheet_names": ["ZAC18", "QMAP154"],
        "charts": False,
        "notes": {
            "aggregate_scope": (
                "all four methods verified-success with positive in-domain linear "
                "fidelity; all four metrics use the same paired circuit cohort"),
            "fidelity": "geometric mean; higher is better",
            "move_and_time": "arithmetic mean; lower is better",
            "algorithm_time": (
                "implementation-level compiler_time_ns; M3/M4 stage columns provide "
                "the requested internal decomposition"),
            "timing_additivity": (
                "initial placement, transition decision and final routing are major "
                "pipeline stages; problem/search/commit/residual split transition; "
                "RETURN and forecast counters are nested diagnostics and are not added"),
            "baseline_policy": (
                "actual original-method runs selected by fixed source priority; "
                "no ghost modification and no paper-number copying"),
        },
        "sheets": sheets,
    }
    (output / "workbook_contract.json").write_text(
        json.dumps(contract, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    (output / "aggregate_summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    (output / "source_manifest.json").write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    _write_summary(output / "experiment_summary.md", report)
    return report


def finalize(output: Path, workbook: Path) -> dict[str, Any]:
    if not workbook.is_file():
        raise FileNotFoundError(workbook)
    required = (
        "zac18.csv", "qmap154.csv", "workbook_contract.json",
        "aggregate_summary.json", "source_manifest.json",
        "experiment_summary.md",
    )
    files = {name: _sha256(output / name) for name in required}
    files[workbook.name] = _sha256(workbook)
    qa_path = output / "qa" / "workbook_qa.json"
    if qa_path.is_file():
        files[str(qa_path.relative_to(output))] = _sha256(qa_path)
    payload = {
        "experiment_schema": 2,
        "delivery_id": "seed0-four-method-two-sheet-v1",
        "workbook": str(workbook.resolve()),
        "workbook_sha256": files[workbook.name],
        "sheet_names": ["ZAC18", "QMAP154"],
        "exact_sheet_count": 2,
        "files": files,
    }
    (output / "final_manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allow-incomplete", action="store_true")
    parser.add_argument("--workbook", type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    report = build(output, allow_incomplete=args.allow_incomplete)
    result: Mapping[str, Any] = report
    if args.workbook is not None:
        result = finalize(output, args.workbook.resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
