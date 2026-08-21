"""Complete diagnostic delivery bundle for Schema-2 ablation reports."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from .export import (
    _atomic_json, _atomic_text, _csv_text, _latex_table, _markdown_table,
    _sha256,
)
from .figures import render_ablation_figures
from .workbook_renderer import render_workbook


ABLATION_TABLE_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "overview": ("key", "value"),
    "variant_summary": (
        "ablation_variant", "base_method", "valid", "N", "valid_over_N",
        "fidelity_valid", "fidelity_geometric_mean", "move_batches_median",
        "move_time_us_median", "compiler_time_seconds_median",
        "attempts_observed", "attempts_expected", "success", "timeout", "oom",
        "compiler_error", "verifier_fail", "scorer_error", "controls",
    ),
    "circuit_ablation": (
        "circuit", "ablation_variant", "base_method", "success_valid",
        "fidelity_valid", "log_fidelity", "fidelity", "move_batches",
        "move_time_us", "compiler_time_seconds", "invalid_reasons",
    ),
    "seed_attempts": (
        "circuit", "ablation_variant", "seed", "status", "method",
        "repetition", "fidelity_ood", "log_fidelity", "move_batches",
        "move_time_us", "compiler_time_seconds", "run_ids", "error",
    ),
    "comparisons": (
        "metric", "comparison", "variant", "reference_variant", "n", "ratio",
        "ci95_low", "ci95_high", "p_value", "holm_p_value", "rank_biserial",
        "wins", "ties", "losses", "ratio_definition", "status",
    ),
    "attempts": (
        "run_id", "circuit", "ablation_variant", "method", "seed",
        "repetition", "status", "fidelity_ood", "log_fidelity",
        "move_batches", "move_time_us", "compiler_time_seconds",
        "artifact_dir", "error",
    ),
}


def _median(summary: Any) -> Any:
    return summary.get("median") if isinstance(summary, Mapping) else None


def _build_ablation_tables(
        report: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    tables = {name: [] for name in ABLATION_TABLE_COLUMNS}
    frozen = report.get("frozen_cohort", {})
    paired_success = report.get("fully_paired_success", {})
    paired_fidelity = report.get("fully_paired_fidelity", {})
    tables["overview"] = [
        {"key": "experiment_schema", "value": report.get("experiment_schema")},
        {"key": "experiment_id", "value": report.get("experiment_id")},
        {"key": "dataset", "value": report.get("dataset")},
        {"key": "diagnostic_only", "value": report.get("diagnostic_only")},
        {"key": "eligible_for_main_claim_gate",
         "value": report.get("eligible_for_main_claim_gate")},
        {"key": "frozen_cohort_sha256", "value": frozen.get("sha256")},
        {"key": "frozen_cohort_N", "value": frozen.get("N")},
        {"key": "fully_paired_success",
         "value": f"{paired_success.get('valid')}/{paired_success.get('N')}"},
        {"key": "fully_paired_fidelity",
         "value": f"{paired_fidelity.get('valid')}/{paired_fidelity.get('N')}"},
        {"key": "integrity_passed",
         "value": report.get("integrity", {}).get("passed")},
        {"key": "aggregation_order", "value": report.get("aggregation_order")},
    ]
    errors = report.get("integrity", {}).get("errors", [])
    if errors:
        tables["overview"].append({
            "key": "integrity_errors", "value": " | ".join(map(str, errors))})

    for variant, payload in report.get("variants", {}).items():
        success = payload.get("success", {})
        fidelity = payload.get("fidelity", {})
        attempts = payload.get("attempts", {})
        statuses = attempts.get("status_counts", {})
        valid, denominator = success.get("valid"), success.get("N")
        tables["variant_summary"].append({
            "ablation_variant": variant,
            "base_method": payload.get("base_method"),
            "valid": valid, "N": denominator,
            "valid_over_N": f"{valid}/{denominator}",
            "fidelity_valid": fidelity.get("valid"),
            "fidelity_geometric_mean": fidelity.get(
                "geometric_mean_fidelity"),
            "move_batches_median": _median(
                payload.get("move_batches", {}).get("summary")),
            "move_time_us_median": _median(
                payload.get("move_time_us", {}).get("summary")),
            "compiler_time_seconds_median": _median(
                payload.get("compiler_time_seconds", {}).get("summary")),
            "attempts_observed": attempts.get("observed"),
            "attempts_expected": attempts.get("expected"),
            "success": statuses.get("success", 0),
            "timeout": statuses.get("timeout", 0),
            "oom": statuses.get("oom", 0),
            "compiler_error": statuses.get("compiler_error", 0),
            "verifier_fail": statuses.get("verifier_fail", 0),
            "scorer_error": statuses.get("scorer_error", 0),
            "controls": payload.get("controls"),
        })
        for circuit in payload.get("circuits", []):
            metrics = circuit.get("seed_median", {})
            tables["circuit_ablation"].append({
                "circuit": circuit.get("circuit"),
                "ablation_variant": variant,
                "base_method": circuit.get("base_method"),
                "success_valid": circuit.get("success_valid"),
                "fidelity_valid": circuit.get("fidelity_valid"),
                "log_fidelity": metrics.get("log_fidelity"),
                "fidelity": metrics.get("fidelity"),
                "move_batches": metrics.get("move_batches"),
                "move_time_us": metrics.get("move_time_us"),
                "compiler_time_seconds": metrics.get("compiler_time_seconds"),
                "invalid_reasons": circuit.get("invalid_reasons"),
            })
            for seed in circuit.get("seed_runs", []):
                tables["seed_attempts"].append({
                    "circuit": circuit.get("circuit"),
                    "ablation_variant": variant,
                    **seed,
                })

    for metric, comparisons in report.get("comparisons", {}).get(
            "metrics", {}).items():
        for name, payload in comparisons.items():
            tables["comparisons"].append({
                "metric": metric, "comparison": name,
                **{column: payload.get(column)
                   for column in ABLATION_TABLE_COLUMNS["comparisons"][2:]},
            })
    tables["attempts"] = [dict(row) for row in report.get("attempt_index", [])]
    return tables


def _markdown(report: Mapping[str, Any],
              tables: Mapping[str, Sequence[Mapping[str, Any]]]) -> str:
    return "\n".join([
        f"# Schema 2 ablation: {report.get('dataset', '')}", "",
        "Exploratory diagnostic only; this report is not eligible for the main claim gate.", "",
        "## Traceability", "",
        _markdown_table(tables["overview"], ABLATION_TABLE_COLUMNS["overview"]),
        "## Variant four-metric summary", "",
        _markdown_table(
            tables["variant_summary"], ABLATION_TABLE_COLUMNS["variant_summary"]),
        "## Paired exploratory comparisons", "",
        _markdown_table(
            tables["comparisons"], ABLATION_TABLE_COLUMNS["comparisons"]),
        "All statistics use five-seed within-circuit medians. Missing, failed, OOD, "
        "or duplicate cells remain invalid and are never imputed.", "",
    ])


def _latex(report: Mapping[str, Any],
           tables: Mapping[str, Sequence[Mapping[str, Any]]]) -> str:
    dataset = str(report.get("dataset", "dataset"))
    return "\n".join([
        "% Exploratory Schema-2 ablation; requires \\usepackage{booktabs}.",
        "% Not eligible for the main publication claim gate.",
        _latex_table(
            f"{dataset} ablation summary", f"{dataset}-ablation-summary",
            tables["variant_summary"], (
                "ablation_variant", "valid_over_N", "fidelity_geometric_mean",
                "move_batches_median", "move_time_us_median",
                "compiler_time_seconds_median")),
        _latex_table(
            f"{dataset} exploratory paired ablation comparisons",
            f"{dataset}-ablation-comparisons", tables["comparisons"], (
                "metric", "variant", "n", "ratio", "ci95_low", "ci95_high",
                "holm_p_value", "wins", "ties", "losses")),
    ])


def export_ablation_report(
        report: Mapping[str, Any], output_directory: str | Path) -> Mapping[str, Any]:
    """Export the ablation JSON into tables, verified XLSX, and vector figures."""
    if report.get("experiment_schema") != 2 or report.get("run_kind") != "ablation":
        raise ValueError("ablation export accepts only Schema-2 ablation reports")
    if report.get("eligible_for_main_claim_gate") is not False:
        raise ValueError("ablation export requires an explicitly diagnostic report")
    output = Path(output_directory).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    tables = _build_ablation_tables(report)
    generated: list[Path] = []
    report_path = output / "report.json"
    _atomic_json(report_path, report)
    generated.append(report_path)
    markdown_path = output / "report.md"
    _atomic_text(markdown_path, _markdown(report, tables))
    generated.append(markdown_path)
    latex_path = output / "tables.tex"
    _atomic_text(latex_path, _latex(report, tables))
    generated.append(latex_path)
    for name, rows in tables.items():
        path = output / f"{name}.csv"
        _atomic_text(path, _csv_text(rows, ABLATION_TABLE_COLUMNS[name]))
        generated.append(path)

    sheet_names = {
        "overview": "Overview", "variant_summary": "Variant Summary",
        "circuit_ablation": "Circuit Ablation", "seed_attempts": "Seed Attempts",
        "comparisons": "Comparisons", "attempts": "Attempt Index",
    }
    contract = {
        "contract_schema": 1,
        "artifact_kind": "ablation_workbook_source_contract",
        "experiment_schema": 2,
        "experiment_id": report.get("experiment_id"),
        "dataset": report.get("dataset"),
        "source_contract": True,
        "rendered_xlsx": "ablation.xlsx",
        "render_policy": (
            "Rendered from five-seed circuit medians with no imputation; "
            "diagnostic only"),
        "sheets": [
            {
                "sheet_name": sheet_names[name],
                "source_csv": f"{name}.csv",
                "columns": list(ABLATION_TABLE_COLUMNS[name]),
                "rows": list(rows),
                "freeze_panes": "A2", "auto_filter": True,
            }
            for name, rows in tables.items()
        ],
    }
    contract_path = output / "workbook_contract.json"
    _atomic_json(contract_path, contract)
    generated.append(contract_path)
    workbook = render_workbook(
        contract_path, output / "ablation.xlsx",
        qa_directory=output / "workbook_qa")
    generated.append(Path(workbook["xlsx_path"]))
    generated.extend(Path(path) for path in workbook["qa_artifact_paths"])
    generated.extend(Path(path) for path in workbook["preview_paths"])
    figures = render_ablation_figures(report, tables, output / "figures")
    generated.extend(Path(path) for path in figures["files"])
    generated.append(Path(figures["qa_path"]))

    row_counts = {f"{name}.csv": len(rows) for name, rows in tables.items()}
    files = []
    for path in sorted(set(generated), key=lambda item: str(item.relative_to(output))):
        files.append({
            "name": path.relative_to(output).as_posix(),
            "path": str(path), "sha256": _sha256(path),
            "bytes": path.stat().st_size,
            "rows": row_counts.get(path.name),
        })
    manifest = {
        "experiment_schema": 2,
        "run_kind": "ablation",
        "experiment_id": report.get("experiment_id"),
        "dataset": report.get("dataset"),
        "diagnostic_only": True,
        "eligible_for_main_claim_gate": False,
        "no_imputation": True,
        "xlsx_rendered": True,
        "xlsx_path": workbook["xlsx_path"],
        "artifact_tool_version": workbook["artifact_tool_version"],
        "figure_backend": figures["backend"],
        "files": files,
    }
    manifest_path = output / "export_manifest.json"
    _atomic_json(manifest_path, manifest)
    return {
        **manifest, "manifest_path": str(manifest_path),
        "output_directory": str(output),
    }


__all__ = ["ABLATION_TABLE_COLUMNS", "export_ablation_report"]
