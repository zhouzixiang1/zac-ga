"""Deterministic publication-delivery exports for Schema-2 aggregates.

The statistical report remains the single source of truth.  This module only
projects fields already present in that report into stable tabular contracts;
it never estimates missing observations or turns a failed claim gate into a
positive conclusion.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any, Mapping, Sequence


TABLE_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "summary": ("key", "value"),
    "coverage": (
        "method", "valid", "N", "rate", "wilson95_low", "wilson95_high",
        "status", "meets_dataset_threshold", "success", "timeout", "oom",
        "compiler_error", "verifier_fail", "scorer_error", "missing",
        "duplicate",
    ),
    "method_quality": (
        "method", "valid", "N", "status", "success", "timeout", "oom",
        "compiler_error", "verifier_fail", "scorer_error", "missing",
        "duplicate",
    ),
    "quality_circuits": (
        "circuit", "method", "paired", "protocol", "log_fidelity",
        "fidelity", "move_batches", "move_time_us",
        "quality_compiler_seconds", "timed_compiler_seconds",
        "fidelity_Bstar_method", "move_batches_best_method",
        "move_time_us_best_method",
    ),
    "fidelity": (
        "comparison", "n", "ratio", "ci95_low", "ci95_high",
        "simultaneous95_low", "simultaneous95_high", "p_value",
        "holm_p_value", "rank_biserial", "wins", "ties", "losses",
        "point_gain_at_least_2pct", "simultaneous_lower_above_1",
        "holm_significant_0.05", "stratum_consistent",
    ),
    "fidelity_strata": ("comparison", "stratum", "n", "ratio"),
    "move": (
        "metric", "comparison", "n", "ratio", "ci95_low", "ci95_high",
        "simultaneous95_low", "simultaneous95_high", "p_value",
        "rank_biserial", "wins", "ties", "losses",
    ),
    "timing": (
        "method", "valid", "N", "status", "PAR2_seconds", "success",
        "timeout", "oom", "compiler_error", "verifier_fail",
        "scorer_error", "missing", "duplicate",
    ),
    "cohort": (
        "circuit", "paired", "fidelity_Bstar_method",
        "move_batches_best_method", "move_time_us_best_method",
        "M1_protocol", "M2_protocol", "M3_protocol", "M4_protocol",
    ),
    "timing_circuits": (
        "circuit", "method", "protocol_complete", "attempts", "successful",
        "median_success_seconds", "PAR2_seconds",
    ),
    "attempts": (
        "run_id", "run_kind", "circuit", "method", "seed", "repetition",
        "status", "git_commit", "input_sha256", "config_sha256",
        "fidelity_ood", "log_fidelity", "fidelity", "move_batches",
        "move_time_us", "compiler_time_seconds", "artifact_dir", "error",
    ),
    "plot_data": (
        "section", "metric", "series", "x", "value", "lower", "upper",
        "n", "reference", "notes",
    ),
}


def _json_safe(value: Any) -> Any:
    """Return strict-JSON-compatible data without silently clipping values."""
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        if math.isnan(value):
            return "NaN"
        return "Infinity" if value > 0 else "-Infinity"
    return value


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        with open(temporary, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _atomic_json(path: Path, payload: Any) -> None:
    _atomic_text(
        path,
        json.dumps(_json_safe(payload), indent=2, sort_keys=True,
                   ensure_ascii=False, allow_nan=False) + "\n",
    )


def _status_columns(payload: Mapping[str, Any]) -> dict[str, Any]:
    counts = payload.get("status_counts", {})
    counts = counts if isinstance(counts, Mapping) else {}
    return {
        name: counts.get(name, 0)
        for name in (
            "success", "timeout", "oom", "compiler_error", "verifier_fail",
            "scorer_error", "missing", "duplicate",
        )
    }


def _summary_rows(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    suite = report.get("frozen_suite", {})
    integrity = report.get("integrity", {})
    claim = report.get("claim_gate", {})
    rows = [
        {"key": "experiment_schema", "value": report.get("experiment_schema")},
        {"key": "experiment_id", "value": report.get("experiment_id")},
        {"key": "dataset", "value": report.get("dataset")},
        {"key": "frozen_suite_sha256", "value": suite.get("sha256")},
        {"key": "frozen_suite_N", "value": suite.get("N")},
        {"key": "frozen_suite_explicit", "value": suite.get("explicit")},
        {"key": "integrity_passed", "value": integrity.get("passed")},
        {"key": "claim_gate_status", "value": claim.get("status")},
        {"key": "claim_gate_passed", "value": claim.get("passed")},
    ]
    for section_name in ("coverage", "main", "timing"):
        section = report.get(section_name, {})
        for field in ("available", "valid", "N", "status"):
            rows.append({
                "key": f"{section_name}_{field}",
                "value": section.get(field),
            })
    errors = integrity.get("errors", [])
    if errors:
        rows.append({"key": "integrity_errors", "value": " | ".join(map(str, errors))})
    missing = claim.get("missing_sections", [])
    if missing:
        rows.append({"key": "claim_missing_sections", "value": ",".join(map(str, missing))})
    return rows


def _build_tables(report: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    tables: dict[str, list[dict[str, Any]]] = {
        name: [] for name in TABLE_COLUMNS
    }
    tables["summary"] = _summary_rows(report)

    coverage = report.get("coverage", {})
    for method, payload in sorted(coverage.get("methods", {}).items()):
        interval = payload.get("wilson95", [None, None])
        tables["coverage"].append({
            "method": method,
            "valid": payload.get("valid"), "N": payload.get("N"),
            "rate": payload.get("rate"),
            "wilson95_low": interval[0] if len(interval) > 0 else None,
            "wilson95_high": interval[1] if len(interval) > 1 else None,
            "status": payload.get("status"),
            "meets_dataset_threshold": payload.get("meets_dataset_threshold"),
            **_status_columns(payload),
        })

    main = report.get("main", {})
    for method, payload in sorted(main.get("methods", {}).items()):
        tables["method_quality"].append({
            "method": method,
            "valid": payload.get("valid"), "N": payload.get("N"),
            "status": payload.get("status"), **_status_columns(payload),
        })

    timing_lookup: dict[tuple[str, str], Any] = {}
    for method, payload in report.get("timing", {}).get("methods", {}).items():
        for circuit, item in payload.get("circuits", {}).items():
            timing_lookup[(circuit, method)] = item.get("median_success_seconds")
    fidelity_best = main.get("fidelity", {}).get("Bstar_method_by_circuit", {})
    move_best = main.get("move", {}).get("metric_best_method_by_circuit", {})
    for payload in main.get("per_circuit_quality", []):
        circuit, method = payload.get("circuit"), payload.get("method")
        tables["quality_circuits"].append({
            **payload,
            "timed_compiler_seconds": timing_lookup.get((circuit, method)),
            "fidelity_Bstar_method": fidelity_best.get(circuit),
            "move_batches_best_method": move_best.get(
                "move_batches", {}).get(circuit),
            "move_time_us_best_method": move_best.get(
                "move_time_us", {}).get(circuit),
        })

    comparisons = main.get("fidelity", {}).get("comparisons", {})
    fidelity_fields = TABLE_COLUMNS["fidelity"][1:]
    for comparison, payload in sorted(comparisons.items()):
        tables["fidelity"].append({
            "comparison": comparison,
            **{field: payload.get(field) for field in fidelity_fields},
        })
        for stratum, item in sorted(payload.get("strata", {}).items()):
            tables["fidelity_strata"].append({
                "comparison": comparison, "stratum": stratum,
                "n": item.get("n"), "ratio": item.get("ratio"),
            })

    move_metrics = main.get("move", {}).get("metrics", {})
    move_fields = TABLE_COLUMNS["move"][2:]
    for metric, payload in sorted(move_metrics.items()):
        for comparison, item in sorted(payload.items()):
            if not isinstance(item, Mapping):
                continue
            tables["move"].append({
                "metric": metric, "comparison": comparison,
                **{field: item.get(field) for field in move_fields},
            })

    timing = report.get("timing", {})
    for method, payload in sorted(timing.get("methods", {}).items()):
        tables["timing"].append({
            "method": method, "valid": payload.get("valid"),
            "N": payload.get("N"), "status": payload.get("status"),
            "PAR2_seconds": payload.get("PAR2_seconds"),
            **_status_columns(payload),
        })
        for circuit, item in sorted(payload.get("circuits", {}).items()):
            tables["timing_circuits"].append({
                "circuit": circuit, "method": method,
                **{field: item.get(field)
                   for field in TABLE_COLUMNS["timing_circuits"][2:]},
            })

    suite_circuits = report.get("frozen_suite", {}).get("circuits", [])
    paired = set(main.get("paired_cohort", []))
    main_methods = main.get("methods", {})
    for circuit in suite_circuits:
        row = {
            "circuit": circuit, "paired": circuit in paired,
            "fidelity_Bstar_method": fidelity_best.get(circuit),
            "move_batches_best_method": move_best.get("move_batches", {}).get(circuit),
            "move_time_us_best_method": move_best.get("move_time_us", {}).get(circuit),
        }
        for method in ("M1", "M2", "M3", "M4"):
            invalid = main_methods.get(method, {}).get("invalid_reasons", {})
            row[f"{method}_protocol"] = invalid.get(
                circuit, "valid" if circuit in paired else None)
        tables["cohort"].append(row)

    for payload in report.get("attempt_index", []):
        tables["attempts"].append(dict(payload))

    for row in tables["coverage"]:
        tables["plot_data"].append({
            "section": "coverage", "metric": "success_rate",
            "series": row["method"], "x": row["method"],
            "value": row["rate"], "lower": row["wilson95_low"],
            "upper": row["wilson95_high"], "n": row["N"],
            "reference": coverage.get("dataset_threshold"),
            "notes": "Wilson 95% CI",
        })
    for row in tables["fidelity"]:
        tables["plot_data"].append({
            "section": "fidelity", "metric": "paired_ratio",
            "series": row["comparison"], "x": row["comparison"],
            "value": row["ratio"], "lower": row["simultaneous95_low"],
            "upper": row["simultaneous95_high"], "n": row["n"],
            "reference": 1.02,
            "notes": "simultaneous family-wise interval; target ratio 1.02",
        })
    for row in tables["move"]:
        tables["plot_data"].append({
            "section": "move", "metric": row["metric"],
            "series": row["comparison"], "x": row["metric"],
            "value": row["ratio"], "lower": row["simultaneous95_low"],
            "upper": row["simultaneous95_high"], "n": row["n"],
            "reference": 1.0,
            "notes": "lower is better; simultaneous family-wise interval",
        })
    for row in tables["timing"]:
        tables["plot_data"].append({
            "section": "timing", "metric": "PAR2_seconds",
            "series": row["method"], "x": row["method"],
            "value": row["PAR2_seconds"], "lower": None, "upper": None,
            "n": row["N"], "reference": None,
            "notes": "implementation-level compiler time",
        })
    for row in tables["quality_circuits"]:
        if row.get("fidelity") is None:
            continue
        base = {
            "series": row["method"], "n": 1, "reference": None,
            "lower": None, "upper": None,
        }
        tables["plot_data"].append({
            **base, "section": "ecdf", "metric": "fidelity",
            "x": row["circuit"], "value": row["fidelity"],
            "notes": "validated per-circuit quality point",
        })
        for metric in ("move_batches", "move_time_us"):
            if row.get(metric) is None:
                continue
            tables["plot_data"].append({
                **base, "section": "pareto",
                "metric": f"fidelity_vs_{metric}",
                "x": row[metric], "value": row["fidelity"],
                "notes": str(row["circuit"]),
            })
        if row.get("timed_compiler_seconds") is not None:
            tables["plot_data"].append({
                **base, "section": "ecdf", "metric": "compiler_seconds",
                "x": row["circuit"], "value": row["timed_compiler_seconds"],
                "notes": "five-repeat median; implementation-level",
            })
    return tables


def _cell_text(value: Any) -> str:
    safe = _json_safe(value)
    if safe is None:
        return ""
    if isinstance(safe, bool):
        return "true" if safe else "false"
    if isinstance(safe, (dict, list)):
        return json.dumps(safe, sort_keys=True, ensure_ascii=False)
    return str(safe)


def _csv_text(rows: Sequence[Mapping[str, Any]], columns: Sequence[str]) -> str:
    import io
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(columns), extrasaction="ignore",
                            lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({column: _cell_text(row.get(column)) for column in columns})
    return buffer.getvalue()


def _markdown_cell(value: Any) -> str:
    return _cell_text(value).replace("|", "\\|").replace("\n", "<br>")


def _markdown_table(rows: Sequence[Mapping[str, Any]], columns: Sequence[str]) -> str:
    if not rows:
        return "_No eligible rows._\n"
    lines = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    lines.extend(
        "| " + " | ".join(_markdown_cell(row.get(column)) for column in columns) + " |"
        for row in rows
    )
    return "\n".join(lines) + "\n"


def _markdown_report(report: Mapping[str, Any],
                     tables: Mapping[str, Sequence[Mapping[str, Any]]]) -> str:
    claim_passed = bool(report.get("claim_gate", {}).get("passed", False))
    disposition = (
        "Publication claim gate passed."
        if claim_passed else
        "Publication claim gate failed or is incomplete; these outputs are diagnostic only."
    )
    sections = [
        f"# Schema 2 aggregate: {report.get('dataset', '')}", "", disposition, "",
        "## Traceability", "", _markdown_table(
            tables["summary"], TABLE_COLUMNS["summary"]),
        "## Coverage", "", _markdown_table(
            tables["coverage"], TABLE_COLUMNS["coverage"]),
        "## Fidelity comparisons", "", _markdown_table(
            tables["fidelity"], TABLE_COLUMNS["fidelity"]),
        "## Move comparisons", "", _markdown_table(
            tables["move"], TABLE_COLUMNS["move"]),
        "## Implementation-level compiler time", "", _markdown_table(
            tables["timing"], TABLE_COLUMNS["timing"]),
        "## Data contract", "",
        "`plot_data.csv` contains only aggregate values and confidence intervals already "
        "present in the statistical report. Per-circuit ECDF or Pareto points require the "
        "validated per-circuit manifests and are intentionally not inferred here.", "",
    ]
    return "\n".join(sections)


_LATEX_ESCAPES = {
    "\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$",
    "#": r"\#", "_": r"\_", "{": r"\{", "}": r"\}",
    "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
}


def _latex_cell(value: Any) -> str:
    text = _cell_text(value)
    return "".join(_LATEX_ESCAPES.get(character, character) for character in text)


def _latex_table(title: str, label: str, rows: Sequence[Mapping[str, Any]],
                 columns: Sequence[str]) -> str:
    if not rows:
        return f"% {title}: no eligible rows.\n"
    alignment = "l" + "r" * (len(columns) - 1)
    lines = [
        r"\begin{table}[tb]", r"\centering", r"\small",
        f"\\caption{{{_latex_cell(title)}}}",
        f"\\label{{tab:{_latex_cell(label)}}}",
        f"\\begin{{tabular}}{{{alignment}}}", r"\toprule",
        " & ".join(_latex_cell(column) for column in columns) + r" \\",
        r"\midrule",
    ]
    lines.extend(
        " & ".join(_latex_cell(row.get(column)) for column in columns) + " \\\\"
        for row in rows
    )
    lines.extend([r"\bottomrule", r"\end{tabular}", r"\end{table}", ""])
    return "\n".join(lines)


def _latex_report(report: Mapping[str, Any],
                  tables: Mapping[str, Sequence[Mapping[str, Any]]]) -> str:
    dataset = str(report.get("dataset", "dataset"))
    coverage_columns = (
        "method", "valid", "N", "rate", "wilson95_low", "wilson95_high", "status")
    fidelity_columns = (
        "comparison", "n", "ratio", "simultaneous95_low",
        "simultaneous95_high", "holm_p_value", "wins", "ties", "losses")
    move_columns = (
        "metric", "comparison", "n", "ratio", "simultaneous95_low",
        "simultaneous95_high", "wins", "ties", "losses")
    timing_columns = ("method", "valid", "N", "PAR2_seconds", "status")
    claim = "passed" if report.get("claim_gate", {}).get("passed") else "failed-or-incomplete"
    return "\n".join([
        "% Generated from the frozen Schema-2 aggregate; requires \\usepackage{booktabs}.",
        f"% Publication claim gate: {claim}. No missing result was imputed.",
        _latex_table(f"{dataset} coverage", f"{dataset}-coverage",
                     tables["coverage"], coverage_columns),
        _latex_table(f"{dataset} fidelity comparisons", f"{dataset}-fidelity",
                     tables["fidelity"], fidelity_columns),
        _latex_table(f"{dataset} move comparisons", f"{dataset}-move",
                     tables["move"], move_columns),
        _latex_table(f"{dataset} implementation-level compiler time",
                     f"{dataset}-timing", tables["timing"], timing_columns),
    ])


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def export_experiment_report(
        report: Mapping[str, Any], output_directory: str | Path) -> Mapping[str, Any]:
    """Export one aggregate to deterministic, non-imputing delivery formats.

    A normalized workbook source contract is emitted instead of pretending to
    produce an XLSX.  A later artifact-tool rendering step can consume the
    contract and CSV files after real experiment results exist.
    """
    if report.get("experiment_schema") != 2:
        raise ValueError("export accepts only experiment_schema=2 reports")
    if not isinstance(report.get("dataset"), str) or not report["dataset"]:
        raise ValueError("Schema-2 report has no dataset")
    output = Path(output_directory).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    tables = _build_tables(report)
    generated: list[Path] = []

    report_path = output / "report.json"
    _atomic_json(report_path, report)
    generated.append(report_path)
    markdown_path = output / "report.md"
    _atomic_text(markdown_path, _markdown_report(report, tables))
    generated.append(markdown_path)
    latex_path = output / "tables.tex"
    _atomic_text(latex_path, _latex_report(report, tables))
    generated.append(latex_path)
    for name, rows in tables.items():
        path = output / f"{name}.csv"
        _atomic_text(path, _csv_text(rows, TABLE_COLUMNS[name]))
        generated.append(path)

    sheet_names = {
        "summary": "Overview", "coverage": "Coverage",
        "method_quality": "Quality Methods", "fidelity": "Fidelity",
        "quality_circuits": "Circuit Quality",
        "fidelity_strata": "Fidelity Strata", "move": "Move",
        "timing": "Timing", "cohort": "Cohort",
        "timing_circuits": "Timing Circuits", "plot_data": "Plot Data",
        "attempts": "Attempts",
    }
    workbook_contract = {
        "contract_schema": 1,
        "artifact_kind": "workbook_source_contract",
        "experiment_schema": 2,
        "experiment_id": report.get("experiment_id"),
        "dataset": report.get("dataset"),
        "not_an_xlsx": True,
        "render_policy": (
            "Render with the approved spreadsheet artifact tool only after "
            "validated experiment results are available"),
        "sheets": [
            {
                "sheet_name": sheet_names[name],
                "source_csv": f"{name}.csv",
                "columns": list(TABLE_COLUMNS[name]),
                "rows": [_json_safe(row) for row in rows],
                "freeze_panes": "A2",
                "auto_filter": True,
            }
            for name, rows in tables.items()
        ],
    }
    workbook_contract_path = output / "workbook_contract.json"
    _atomic_json(workbook_contract_path, workbook_contract)
    generated.append(workbook_contract_path)

    file_rows = []
    row_counts = {f"{name}.csv": len(rows) for name, rows in tables.items()}
    for path in sorted(generated, key=lambda item: item.name):
        file_rows.append({
            "name": path.name, "path": str(path), "sha256": _sha256(path),
            "bytes": path.stat().st_size,
            "rows": row_counts.get(path.name),
        })
    manifest = {
        "experiment_schema": 2,
        "experiment_id": report.get("experiment_id"),
        "dataset": report.get("dataset"),
        "claim_gate_passed": bool(report.get("claim_gate", {}).get("passed", False)),
        "no_imputation": True,
        "xlsx_pending_artifact_render": True,
        "workbook_contract_path": str(workbook_contract_path),
        "files": file_rows,
    }
    manifest_path = output / "export_manifest.json"
    _atomic_json(manifest_path, manifest)
    return {**manifest, "manifest_path": str(manifest_path),
            "output_directory": str(output)}


__all__ = ["TABLE_COLUMNS", "export_experiment_report"]
