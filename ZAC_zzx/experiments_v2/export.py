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
import statistics
from pathlib import Path
from typing import Any, Mapping, Sequence

from .figures import render_report_figures
from .workbook_renderer import render_workbook


TABLE_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "summary": ("key", "value"),
    "coverage": (
        "method", "valid", "N", "rate", "wilson95_low", "wilson95_high",
        "status", "meets_dataset_threshold", "success", "timeout", "oom",
        "compiler_error", "verifier_fail", "scorer_error", "missing",
        "duplicate",
    ),
    "method_quality": (
        "method", "valid", "N", "valid_over_N", "paired_valid",
        "paired_N", "paired_valid_over_N", "status",
        "fidelity_geometric_mean", "move_batches_median",
        "move_time_us_median", "timed_compiler_seconds_median",
        "timed_valid", "PAR2_seconds", "success", "timeout", "oom",
        "compiler_error", "verifier_fail", "scorer_error", "missing", "duplicate",
    ),
    "quality_strata": (
        "stratum", "method", "n", "fidelity_geometric_mean",
        "move_batches_median", "move_time_us_median",
        "timed_compiler_seconds_median", "timed_valid",
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
        "runtime_observed", "median_success_seconds", "PAR2_seconds",
    ),
    "attempts": (
        "run_id", "run_kind", "circuit", "method", "seed", "repetition",
        "status", "git_commit", "input_sha256", "config_sha256",
        "fidelity_ood", "log_fidelity", "fidelity", "move_batches",
        "move_time_us", "compiler_time_seconds", "compiler_process_wall_seconds",
        "end_to_end_time_seconds", "cpu_time_seconds", "peak_rss_bytes",
        "log_one_qubit_gate", "log_two_qubit_gate", "log_idle_excitation",
        "log_atom_transfer", "log_coherence_linear",
        "exponential_sensitivity_log_fidelity",
        "exponential_sensitivity_fidelity", "duration_us", "idle_exposures",
        "stay_count", "return_count", "reseat_count", "ghost_repairs",
        "ghost_splits", "ghost_hits", "trace_protocol", "ghost_policy",
        "physicalization_policy", "verifier_ok", "artifact_dir", "error",
    ),
    "plot_data": (
        "section", "metric", "series", "x", "value", "lower", "upper",
        "n", "reference", "is_pareto", "notes",
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
    timing = report.get("timing", {})
    timing_lookup: dict[tuple[str, str], Any] = {}
    timing_protocol: dict[tuple[str, str], bool] = {}
    timing_methods = timing.get("methods", {})
    for method, payload in sorted(timing_methods.items()):
        tables["timing"].append({
            "method": method, "valid": payload.get("valid"),
            "N": payload.get("N"), "status": payload.get("status"),
            "PAR2_seconds": payload.get("PAR2_seconds"),
            **_status_columns(payload),
        })
        for circuit, item in sorted(payload.get("circuits", {}).items()):
            protocol_ok = bool(item.get("protocol_complete"))
            runtime_ok = bool(item.get("runtime_observed",
                                       item.get("successful", 0) > 0))
            timing_protocol[(circuit, method)] = protocol_ok and runtime_ok
            timing_lookup[(circuit, method)] = (
                item.get("median_success_seconds")
                if protocol_ok and runtime_ok else None)
            tables["timing_circuits"].append({
                "circuit": circuit, "method": method,
                **{field: item.get(field)
                   for field in TABLE_COLUMNS["timing_circuits"][2:]},
            })

    paired = set(main.get("paired_cohort", []))
    paired_summaries = main.get("paired_method_summary", {})
    quality_rows = main.get("per_circuit_quality", [])
    for method, payload in sorted(main.get("methods", {}).items()):
        summary = dict(paired_summaries.get(method, {}))
        if not summary and paired:
            rows = [row for row in quality_rows
                    if row.get("method") == method and row.get("circuit") in paired
                    and row.get("log_fidelity") is not None]
            if rows:
                summary = {
                    "valid": len(rows), "N": main.get("N"),
                    "fidelity_geometric_mean": math.exp(math.fsum(
                        float(row["log_fidelity"]) for row in rows) / len(rows)),
                    "move_batches_median": statistics.median(
                        float(row["move_batches"]) for row in rows),
                    "move_time_us_median": statistics.median(
                        float(row["move_time_us"]) for row in rows),
                }
        timed_values = [
            float(timing_lookup[(circuit, method)])
            for circuit in paired
            if timing_lookup.get((circuit, method)) is not None
        ]
        valid = payload.get("valid")
        denominator = payload.get("N")
        paired_valid = summary.get("valid", len(paired) if summary else 0)
        paired_n = summary.get("N", main.get("N"))
        tables["method_quality"].append({
            "method": method,
            "valid": valid, "N": denominator,
            "valid_over_N": (
                f"{valid}/{denominator}" if valid is not None and denominator is not None
                else None),
            "paired_valid": paired_valid, "paired_N": paired_n,
            "paired_valid_over_N": (
                f"{paired_valid}/{paired_n}"
                if paired_valid is not None and paired_n is not None else None),
            "status": payload.get("status"),
            "fidelity_geometric_mean": summary.get("fidelity_geometric_mean"),
            "move_batches_median": summary.get("move_batches_median"),
            "move_time_us_median": summary.get("move_time_us_median"),
            "timed_compiler_seconds_median": (
                statistics.median(timed_values) if timed_values else None),
            "timed_valid": len(timed_values),
            "PAR2_seconds": timing_methods.get(method, {}).get("PAR2_seconds"),
            **_status_columns(payload),
        })

    stratum_by_circuit = main.get("stratum_by_circuit", {})
    for stratum, method_payloads in sorted(
            main.get("stratum_method_summary", {}).items()):
        members = {circuit for circuit, label in stratum_by_circuit.items()
                   if label == stratum}
        for method, payload in sorted(method_payloads.items()):
            timed_values = [
                float(timing_lookup[(circuit, method)])
                for circuit in members
                if timing_lookup.get((circuit, method)) is not None
            ]
            tables["quality_strata"].append({
                "stratum": stratum, "method": method,
                "n": payload.get("n"),
                "fidelity_geometric_mean": payload.get(
                    "fidelity_geometric_mean"),
                "move_batches_median": payload.get("move_batches_median"),
                "move_time_us_median": payload.get("move_time_us_median"),
                "timed_compiler_seconds_median": (
                    statistics.median(timed_values) if timed_values else None),
                "timed_valid": len(timed_values),
            })

    fidelity_best = main.get("fidelity", {}).get("Bstar_method_by_circuit", {})
    move_best = main.get("move", {}).get("metric_best_method_by_circuit", {})
    for payload in quality_rows:
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

    suite_circuits = report.get("frozen_suite", {}).get("circuits", [])
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
            "is_pareto": None,
            "notes": "Wilson 95% CI",
        })
    for row in tables["fidelity"]:
        tables["plot_data"].append({
            "section": "fidelity", "metric": "paired_ratio",
            "series": row["comparison"], "x": row["comparison"],
            "value": row["ratio"], "lower": row["simultaneous95_low"],
            "upper": row["simultaneous95_high"], "n": row["n"],
            "reference": 1.02,
            "is_pareto": None,
            "notes": "simultaneous family-wise interval; target ratio 1.02",
        })
    for row in tables["move"]:
        tables["plot_data"].append({
            "section": "move", "metric": row["metric"],
            "series": row["comparison"], "x": row["metric"],
            "value": row["ratio"], "lower": row["simultaneous95_low"],
            "upper": row["simultaneous95_high"], "n": row["n"],
            "reference": 1.0,
            "is_pareto": None,
            "notes": "lower is better; simultaneous family-wise interval",
        })
    for row in tables["timing"]:
        tables["plot_data"].append({
            "section": "timing", "metric": "PAR2_seconds",
            "series": row["method"], "x": row["method"],
            "value": row["PAR2_seconds"], "lower": None, "upper": None,
            "n": row["N"], "reference": None,
            "is_pareto": None,
            "notes": "implementation-level compiler time",
        })

    paired_rows = [row for row in tables["quality_circuits"]
                   if bool(row.get("paired"))]
    for metric in ("fidelity", "move_batches", "move_time_us",
                   "timed_compiler_seconds"):
        for method in ("M1", "M2", "M3", "M4"):
            values = sorted(
                (float(row[metric]), str(row.get("circuit")))
                for row in paired_rows
                if row.get("method") == method and
                isinstance(row.get(metric), (int, float)) and
                math.isfinite(float(row[metric])))
            for rank, (value, circuit) in enumerate(values, start=1):
                tables["plot_data"].append({
                    "section": "ecdf", "metric": metric, "series": method,
                    "x": value, "value": rank / len(values),
                    "lower": None, "upper": None, "n": len(values),
                    "reference": None, "is_pareto": None,
                    "notes": circuit,
                })

    quality_lookup = {
        (str(row.get("circuit")), str(row.get("method"))): row
        for row in paired_rows
    }
    for metric in ("move_batches", "move_time_us"):
        for method in ("M1", "M2", "M3", "M4"):
            points: list[tuple[str, float, float]] = []
            for row in paired_rows:
                if row.get("method") != method:
                    continue
                circuit = str(row.get("circuit"))
                baseline = quality_lookup.get(
                    (circuit, str(row.get("fidelity_Bstar_method"))))
                if baseline is None:
                    continue
                cost, base_cost = row.get(metric), baseline.get(metric)
                fidelity, base_fidelity = row.get("fidelity"), baseline.get("fidelity")
                numeric = (cost, base_cost, fidelity, base_fidelity)
                if (not all(isinstance(value, (int, float)) and
                            math.isfinite(float(value)) for value in numeric) or
                        float(base_cost) <= 0 or float(base_fidelity) <= 0):
                    continue
                points.append((
                    circuit, float(cost) / float(base_cost),
                    float(fidelity) / float(base_fidelity)))
            for circuit, cost_ratio, fidelity_ratio in points:
                pareto = not any(
                    other_cost <= cost_ratio and other_fidelity >= fidelity_ratio and
                    (other_cost < cost_ratio or other_fidelity > fidelity_ratio)
                    for other_circuit, other_cost, other_fidelity in points
                    if other_circuit != circuit)
                tables["plot_data"].append({
                    "section": "pareto", "metric": f"fidelity_vs_{metric}",
                    "series": method, "x": cost_ratio,
                    "value": fidelity_ratio, "lower": None, "upper": None,
                    "n": len(points), "reference": 1.0,
                    "is_pareto": pareto, "notes": circuit,
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
        "## Four-method four-metric main table", "", _markdown_table(
            tables["method_quality"], TABLE_COLUMNS["method_quality"]),
        "## Preregistered scale strata", "", _markdown_table(
            tables["quality_strata"], TABLE_COLUMNS["quality_strata"]),
        "## Fidelity comparisons", "", _markdown_table(
            tables["fidelity"], TABLE_COLUMNS["fidelity"]),
        "## Move comparisons", "", _markdown_table(
            tables["move"], TABLE_COLUMNS["move"]),
        "## Implementation-level compiler time", "", _markdown_table(
            tables["timing"], TABLE_COLUMNS["timing"]),
        "## Data contract", "",
        "`plot_data.csv` contains explicit ECDF coordinates, fidelity-B*-normalized "
        "Pareto points, and aggregate confidence intervals derived only from the strict "
        "paired cohort. Missing observations are not imputed.", "",
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
    main_columns = (
        "method", "paired_valid_over_N", "fidelity_geometric_mean",
        "move_batches_median", "move_time_us_median",
        "timed_compiler_seconds_median")
    strata_columns = (
        "stratum", "method", "n", "fidelity_geometric_mean",
        "move_batches_median", "move_time_us_median",
        "timed_compiler_seconds_median")
    claim = "passed" if report.get("claim_gate", {}).get("passed") else "failed-or-incomplete"
    return "\n".join([
        "% Generated from the frozen Schema-2 aggregate; requires \\usepackage{booktabs}.",
        f"% Publication claim gate: {claim}. No missing result was imputed.",
        _latex_table(f"{dataset} coverage", f"{dataset}-coverage",
                     tables["coverage"], coverage_columns),
        _latex_table(f"{dataset} four-method four-metric paired results",
                     f"{dataset}-main", tables["method_quality"], main_columns),
        _latex_table(f"{dataset} preregistered scale strata",
                     f"{dataset}-strata", tables["quality_strata"], strata_columns),
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
    """Export one aggregate to traceable tables, XLSX, and vector figures."""
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
        "method_quality": "Quality Methods", "quality_strata": "Quality Strata",
        "fidelity": "Fidelity",
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
        "source_contract": True,
        "rendered_xlsx": "report.xlsx",
        "render_policy": (
            "Rendered with the approved spreadsheet artifact tool from the "
            "validated Schema-2 aggregate; missing observations stay blank"),
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

    workbook = render_workbook(
        workbook_contract_path, output / "report.xlsx",
        qa_directory=output / "workbook_qa")
    generated.append(Path(workbook["xlsx_path"]))
    generated.extend(Path(path) for path in workbook["qa_artifact_paths"])
    generated.extend(Path(path) for path in workbook["preview_paths"])

    figures = render_report_figures(report, tables, output / "figures")
    generated.extend(Path(path) for path in figures["files"])
    generated.append(Path(figures["qa_path"]))

    file_rows = []
    row_counts = {f"{name}.csv": len(rows) for name, rows in tables.items()}
    for path in sorted(set(generated), key=lambda item: str(item.relative_to(output))):
        relative = path.relative_to(output).as_posix()
        file_rows.append({
            "name": relative, "path": str(path), "sha256": _sha256(path),
            "bytes": path.stat().st_size,
            "rows": row_counts.get(path.name),
        })
    manifest = {
        "experiment_schema": 2,
        "experiment_id": report.get("experiment_id"),
        "dataset": report.get("dataset"),
        "claim_gate_passed": bool(report.get("claim_gate", {}).get("passed", False)),
        "no_imputation": True,
        "xlsx_pending_artifact_render": False,
        "xlsx_rendered": True,
        "xlsx_path": workbook["xlsx_path"],
        "workbook_qa_path": workbook["qa_path"],
        "artifact_tool_version": workbook["artifact_tool_version"],
        "workbook_contract_path": str(workbook_contract_path),
        "figure_backend": figures["backend"],
        "figure_qa_path": figures["qa_path"],
        "files": file_rows,
    }
    manifest_path = output / "export_manifest.json"
    _atomic_json(manifest_path, manifest)
    return {**manifest, "manifest_path": str(manifest_path),
            "output_directory": str(output)}


__all__ = ["TABLE_COLUMNS", "export_experiment_report"]
