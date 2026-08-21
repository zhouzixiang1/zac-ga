"""Publication-ready Python figures for frozen Schema-2 aggregates.

The figures are projections of validated aggregate rows only.  Missing cells
remain missing, and a failed publication gate is visibly labelled diagnostic.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402


METHODS = ("M1", "M2", "M3", "M4")
COLORS = {
    "M1": "#484878",
    "M2": "#7884B4",
    "M3": "#E4CCD8",
    "M4": "#B64342",
}
MARKERS = {"M1": "o", "M2": "s", "M3": "^", "M4": "D"}
STATUS_COLORS = {
    "success": "#3775BA",
    "timeout": "#FFD166",
    "oom": "#B64342",
    "compiler_error": "#9A4D8E",
    "verifier_fail": "#E9A6A1",
    "scorer_error": "#42949E",
    "missing": "#CFCECE",
    "duplicate": "#767676",
}


plt.rcParams["font.family"] = "sans-serif"
plt.rcParams["font.sans-serif"] = ["Arial", "DejaVu Sans", "Liberation Sans"]
plt.rcParams["svg.fonttype"] = "none"
plt.rcParams["svg.hashsalt"] = "zac-schema2-v2"
plt.rcParams["pdf.fonttype"] = 42
plt.rcParams["font.size"] = 7
plt.rcParams["axes.spines.right"] = False
plt.rcParams["axes.spines.top"] = False
plt.rcParams["axes.linewidth"] = 0.8
plt.rcParams["legend.frameon"] = False
plt.rcParams["figure.facecolor"] = "white"
plt.rcParams["axes.facecolor"] = "white"


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _panel(ax: Any, label: str) -> None:
    ax.text(-0.12, 1.04, label, transform=ax.transAxes, fontsize=8,
            fontweight="bold", ha="left", va="bottom")


def _diagnostic_note(fig: Any, text: str | None) -> None:
    if text:
        engine = fig.get_layout_engine()
        if engine is not None and hasattr(engine, "set"):
            engine.set(rect=(0.0, 0.055, 1.0, 0.945))
        fig.text(
            0.995, 0.012, text,
            ha="right", va="bottom", fontsize=6, color="#B64342")


def _save_figure(fig: Any, base: Path, *, claim_passed: bool = True,
                 diagnostic_text: str | None = None) -> list[Path]:
    if diagnostic_text is None and not claim_passed:
        diagnostic_text = (
            "Diagnostic only: publication claim gate failed or is incomplete.")
    _diagnostic_note(fig, diagnostic_text)
    generated: list[Path] = []
    for suffix, dpi in (("svg", 300), ("pdf", 300), ("png", 240)):
        destination = base.with_suffix(f".{suffix}")
        temporary = destination.with_name(destination.stem + ".tmp" + destination.suffix)
        if suffix == "svg":
            metadata = {"Date": None, "Creator": "ZAC Schema-2"}
        elif suffix == "pdf":
            metadata = {
                "CreationDate": None, "ModDate": None,
                "Creator": "ZAC Schema-2", "Producer": "matplotlib",
            }
        else:
            metadata = {"Software": "ZAC Schema-2"}
        fig.savefig(temporary, format=suffix, dpi=dpi, bbox_inches="tight",
                    facecolor="white", metadata=metadata)
        os.replace(temporary, destination)
        generated.append(destination)
    plt.close(fig)
    return generated


def _method_rows(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    lookup = {str(row.get("method")): row for row in rows}
    return [lookup[method] for method in METHODS if method in lookup]


def _plot_coverage(rows: Sequence[Mapping[str, Any]], output: Path,
                   claim_passed: bool) -> list[Path]:
    ordered = _method_rows(rows)
    if not ordered:
        return []
    fig, axes = plt.subplots(1, 2, figsize=(7.2, 2.75), layout="constrained")
    methods = [str(row["method"]) for row in ordered]
    x = np.arange(len(methods))
    rates = np.array([_finite(row.get("rate")) or 0.0 for row in ordered])
    lower = np.array([_finite(row.get("wilson95_low")) or 0.0 for row in ordered])
    upper = np.array([_finite(row.get("wilson95_high")) or 0.0 for row in ordered])
    errors = np.vstack([rates - lower, upper - rates])
    axes[0].bar(x, rates, color=[COLORS[item] for item in methods], width=0.68,
                edgecolor="#272727", linewidth=0.6)
    axes[0].errorbar(x, rates, yerr=errors, fmt="none", color="#272727",
                     capsize=2.5, linewidth=0.8)
    axes[0].set_xticks(x, methods)
    axes[0].set_ylim(0, 1.06)
    axes[0].set_ylabel("Validated coverage")
    axes[0].axhline(1.0, color="#767676", linestyle="--", linewidth=0.7)
    for index, row in enumerate(ordered):
        axes[0].text(index, min(1.025, rates[index] + 0.025),
                     f"{row.get('valid', 0)}/{row.get('N', 0)}",
                     ha="center", va="bottom", fontsize=6)
    _panel(axes[0], "a")

    statuses = tuple(STATUS_COLORS)
    bottom = np.zeros(len(ordered), dtype=float)
    for status in statuses:
        values = np.array([float(row.get(status) or 0) for row in ordered])
        if not np.any(values):
            continue
        axes[1].bar(x, values, bottom=bottom, width=0.68,
                    color=STATUS_COLORS[status], label=status.replace("_", " "))
        bottom += values
    axes[1].set_xticks(x, methods)
    axes[1].set_ylabel("Attempt count")
    axes[1].legend(fontsize=5.5, ncol=2, loc="upper right")
    _panel(axes[1], "b")
    return _save_figure(fig, output / "coverage_status", claim_passed=claim_passed)


def _plot_main_metrics(rows: Sequence[Mapping[str, Any]], output: Path,
                       claim_passed: bool) -> list[Path]:
    ordered = _method_rows(rows)
    if not ordered:
        return []
    specifications = (
        ("fidelity_geometric_mean", "Fidelity geometric mean", "a"),
        ("move_batches_median", "Move batches (median)", "b"),
        ("move_time_us_median", "Move time (µs, median)", "c"),
        ("timed_compiler_seconds_median", "Compiler time (s, median)", "d"),
    )
    fig, axes = plt.subplots(2, 2, figsize=(7.2, 5.0), layout="constrained")
    methods = [str(row["method"]) for row in ordered]
    x = np.arange(len(methods))
    for ax, (field, label, panel) in zip(axes.flat, specifications):
        values = [_finite(row.get(field)) for row in ordered]
        finite_values = [value for value in values if value is not None]
        if finite_values:
            bars = ax.bar(
                x, [value if value is not None else 0.0 for value in values],
                color=[COLORS[item] for item in methods], width=0.68,
                edgecolor="#272727", linewidth=0.6)
            for bar, value in zip(bars, values):
                if value is None:
                    bar.set_alpha(0.15)
                    ax.text(bar.get_x() + bar.get_width() / 2, 0, "NA",
                            ha="center", va="bottom", fontsize=6, rotation=90)
            if field == "fidelity_geometric_mean":
                minimum = min(finite_values)
                maximum = max(finite_values)
                padding = max(5e-4, (maximum - minimum) * 0.25)
                ax.set_ylim(max(0.0, minimum - padding), min(1.0, maximum + padding))
                ax.ticklabel_format(axis="y", style="plain", useOffset=False)
                ax.yaxis.set_major_formatter(
                    matplotlib.ticker.FormatStrFormatter("%.6f"))
        else:
            ax.text(0.5, 0.5, "No eligible paired data", transform=ax.transAxes,
                    ha="center", va="center", color="#767676")
        ax.set_xticks(x, methods)
        ax.set_ylabel(label)
        _panel(ax, panel)
    return _save_figure(fig, output / "four_method_four_metric",
                        claim_passed=claim_passed)


def _ecdf(values: Sequence[float]) -> tuple[np.ndarray, np.ndarray]:
    x = np.sort(np.asarray(values, dtype=float))
    return x, np.arange(1, len(x) + 1, dtype=float) / len(x)


def _plot_ecdf(rows: Sequence[Mapping[str, Any]], output: Path,
               claim_passed: bool) -> list[Path]:
    paired = [row for row in rows if bool(row.get("paired"))]
    if not paired:
        return []
    specifications = (
        ("fidelity", "Fidelity", "a"),
        ("move_batches", "Move batches", "b"),
        ("move_time_us", "Move time (µs)", "c"),
        ("timed_compiler_seconds", "Compiler time (s)", "d"),
    )
    fig, axes = plt.subplots(2, 2, figsize=(7.2, 5.0), layout="constrained")
    legend_handles = []
    legend_labels = []
    for ax, (metric, xlabel, panel) in zip(axes.flat, specifications):
        drew = False
        for method in METHODS:
            values = [value for row in paired if row.get("method") == method
                      if (value := _finite(row.get(metric))) is not None]
            if not values:
                continue
            x, y = _ecdf(values)
            line, = ax.step(
                x, y, where="post", color=COLORS[method], linewidth=1.5,
                marker=MARKERS[method], markersize=3.2, label=method)
            drew = True
            if method not in legend_labels:
                legend_handles.append(line)
                legend_labels.append(method)
        if not drew:
            ax.text(0.5, 0.5, "No eligible paired data", transform=ax.transAxes,
                    ha="center", va="center", color="#767676")
        ax.set_xlabel(xlabel)
        ax.set_ylabel("Empirical CDF")
        ax.set_ylim(0, 1.02)
        _panel(ax, panel)
    if legend_handles:
        fig.legend(legend_handles, legend_labels, loc="upper center", ncol=4,
                   bbox_to_anchor=(0.5, 1.015))
    return _save_figure(fig, output / "paired_ecdf", claim_passed=claim_passed)


def _pareto_front(points: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
    front = []
    for index, point in enumerate(points):
        cost, fidelity = point
        dominated = any(
            other_cost <= cost and other_fidelity >= fidelity and
            (other_cost < cost or other_fidelity > fidelity)
            for other_index, (other_cost, other_fidelity) in enumerate(points)
            if other_index != index)
        if not dominated:
            front.append(point)
    return sorted(front)


def _plot_pareto(rows: Sequence[Mapping[str, Any]], output: Path,
                 claim_passed: bool) -> list[Path]:
    paired = [row for row in rows if bool(row.get("paired"))]
    lookup = {(str(row.get("circuit")), str(row.get("method"))): row
              for row in paired}
    if not lookup:
        return []
    fig, axes = plt.subplots(1, 2, figsize=(7.2, 3.0), layout="constrained")
    specifications = (
        ("move_batches", "Move batches / fidelity B*", "a"),
        ("move_time_us", "Move time / fidelity B*", "b"),
    )
    legend_handles = []
    legend_labels = []
    for ax, (metric, xlabel, panel) in zip(axes, specifications):
        for method in METHODS:
            points: list[tuple[float, float]] = []
            for row in paired:
                if row.get("method") != method:
                    continue
                circuit = str(row.get("circuit"))
                baseline_method = row.get("fidelity_Bstar_method")
                baseline = lookup.get((circuit, str(baseline_method)))
                if baseline is None:
                    continue
                cost = _finite(row.get(metric))
                baseline_cost = _finite(baseline.get(metric))
                fidelity = _finite(row.get("fidelity"))
                baseline_fidelity = _finite(baseline.get("fidelity"))
                if (cost is None or baseline_cost is None or baseline_cost <= 0 or
                        fidelity is None or baseline_fidelity is None or
                        baseline_fidelity <= 0):
                    continue
                points.append((cost / baseline_cost, fidelity / baseline_fidelity))
            if not points:
                continue
            x, y = zip(*points)
            scatter = ax.scatter(x, y, s=13, alpha=0.55, color=COLORS[method],
                                 marker=MARKERS[method], label=method,
                                 edgecolors="none")
            front = _pareto_front(points)
            if len(front) > 1:
                ax.plot([item[0] for item in front], [item[1] for item in front],
                        color=COLORS[method], linewidth=0.9, alpha=0.9)
            if method not in legend_labels:
                legend_handles.append(scatter)
                legend_labels.append(method)
        ax.axvline(1.0, color="#767676", linestyle="--", linewidth=0.7)
        ax.axhline(1.0, color="#767676", linestyle="--", linewidth=0.7)
        ax.set_xlabel(xlabel)
        ax.set_ylabel("Fidelity / fidelity B*")
        _panel(ax, panel)
    if legend_handles:
        fig.legend(legend_handles, legend_labels, loc="upper center", ncol=4,
                   bbox_to_anchor=(0.5, 1.04))
    return _save_figure(fig, output / "paired_pareto", claim_passed=claim_passed)


def _plot_strata(rows: Sequence[Mapping[str, Any]], output: Path,
                 claim_passed: bool) -> list[Path]:
    if not rows:
        return []
    strata = sorted({str(row.get("stratum")) for row in rows})
    lookup = {(str(row.get("stratum")), str(row.get("method"))): row
              for row in rows}
    specifications = (
        ("fidelity_geometric_mean", "Fidelity geometric mean", "a"),
        ("move_batches_median", "Move batches (median)", "b"),
        ("move_time_us_median", "Move time (µs, median)", "c"),
        ("timed_compiler_seconds_median", "Compiler time (s, median)", "d"),
    )
    fig, axes = plt.subplots(2, 2, figsize=(7.2, 5.0), layout="constrained")
    x = np.arange(len(strata), dtype=float)
    width = 0.19
    for ax, (metric, ylabel, panel) in zip(axes.flat, specifications):
        any_value = False
        for index, method in enumerate(METHODS):
            values = [_finite(lookup.get((stratum, method), {}).get(metric))
                      for stratum in strata]
            if any(value is not None for value in values):
                any_value = True
            offset = (index - 1.5) * width
            bars = ax.bar(
                x + offset, [value if value is not None else 0.0 for value in values],
                width=width, color=COLORS[method], label=method,
                edgecolor="#272727", linewidth=0.35)
            for bar, value in zip(bars, values):
                if value is None:
                    bar.set_alpha(0.12)
        if not any_value:
            ax.text(0.5, 0.5, "No eligible stratum data", transform=ax.transAxes,
                    ha="center", va="center", color="#767676")
        if metric == "fidelity_geometric_mean" and any_value:
            available = [
                _finite(row.get(metric)) for row in rows
                if _finite(row.get(metric)) is not None
            ]
            if available:
                minimum, maximum = min(available), max(available)
                padding = max(5e-4, (maximum - minimum) * 0.25)
                ax.set_ylim(max(0.0, minimum - padding), min(1.0, maximum + padding))
                ax.ticklabel_format(axis="y", style="plain", useOffset=False)
                ax.yaxis.set_major_formatter(
                    matplotlib.ticker.FormatStrFormatter("%.6f"))
        ax.set_xticks(x, strata, rotation=18, ha="right")
        ax.set_ylabel(ylabel)
        _panel(ax, panel)
    handles, labels = axes[0, 0].get_legend_handles_labels()
    if handles:
        fig.legend(handles, labels, loc="upper center", ncol=4,
                   bbox_to_anchor=(0.5, 1.015))
    return _save_figure(fig, output / "preregistered_strata",
                        claim_passed=claim_passed)


def _validate_exports(paths: Sequence[Path]) -> list[str]:
    errors: list[str] = []
    for path in paths:
        if not path.is_file() or path.stat().st_size == 0:
            errors.append(f"missing or empty figure: {path.name}")
            continue
        if path.suffix == ".svg":
            text = path.read_text(encoding="utf-8")
            if "<svg" not in text or "<text" not in text:
                errors.append(f"SVG lacks editable text: {path.name}")
        elif path.suffix == ".pdf":
            if not path.read_bytes().startswith(b"%PDF"):
                errors.append(f"invalid PDF signature: {path.name}")
        elif path.suffix == ".png":
            if not path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"):
                errors.append(f"invalid PNG signature: {path.name}")
    return errors


def render_report_figures(
        report: Mapping[str, Any],
        tables: Mapping[str, Sequence[Mapping[str, Any]]],
        output_directory: str | Path) -> Mapping[str, Any]:
    """Render the complete validated-data figure bundle in Python only."""
    output = Path(output_directory).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    claim_passed = bool(report.get("claim_gate", {}).get("passed", False))
    generated: list[Path] = []
    generated.extend(_plot_coverage(tables.get("coverage", []), output, claim_passed))
    generated.extend(_plot_main_metrics(
        tables.get("method_quality", []), output, claim_passed))
    generated.extend(_plot_ecdf(
        tables.get("quality_circuits", []), output, claim_passed))
    generated.extend(_plot_pareto(
        tables.get("quality_circuits", []), output, claim_passed))
    generated.extend(_plot_strata(
        tables.get("quality_strata", []), output, claim_passed))
    errors = _validate_exports(generated)
    contract = {
        "experiment_schema": 2,
        "dataset": report.get("dataset"),
        "core_conclusion": (
            "The bundle reports whether M4 improves paired fidelity while preserving "
            "move cost, coverage, runtime transparency, and preregistered scale robustness; "
            "it does not assert the claim when the formal gate fails."),
        "archetype": "quantitative grid",
        "backend": "Python/matplotlib only",
        "final_size": "double-column, 183 mm maximum width",
        "exports": ["SVG with editable text", "PDF with TrueType text", "PNG QA preview"],
        "panel_map": {
            "coverage_status": "coverage with Wilson intervals and explicit terminal states",
            "four_method_four_metric": "paired four-method fidelity, move, and implementation runtime summary",
            "paired_ecdf": "full paired-circuit distributions without averaging away tails",
            "paired_pareto": "per-circuit fidelity-B* normalized fidelity/move tradeoff",
            "preregistered_strata": "predeclared scale-stratum robustness",
        },
        "statistics": {
            "fidelity_center": "geometric mean after per-circuit seed median",
            "move_center": "median after per-circuit seed median",
            "runtime_center": "median of five-repeat circuit medians",
            "coverage_interval": "Wilson 95% interval",
            "source_data": [
                "coverage.csv", "method_quality.csv", "quality_circuits.csv",
                "quality_strata.csv", "plot_data.csv",
            ],
        },
        "reviewer_risks": [
            "Compiler time is implementation-level because M2 is C++ while the other implementations are mainly Python.",
            "All cross-method quality views use the strict paired cohort.",
            "B* is selected by fidelity, and Pareto ratios reuse that same baseline.",
            "A failed or incomplete gate is marked diagnostic on every figure.",
        ],
        "claim_gate_passed": claim_passed,
        "files": [path.name for path in generated],
        "validation_errors": errors,
    }
    qa_path = output / "figure_qa.json"
    temporary = qa_path.with_name(qa_path.name + ".tmp")
    temporary.write_text(json.dumps(contract, indent=2, sort_keys=True) + "\n",
                         encoding="utf-8")
    os.replace(temporary, qa_path)
    if errors:
        raise RuntimeError("figure QA failed: " + " | ".join(errors))
    return {
        "files": [str(path) for path in generated],
        "qa_path": str(qa_path),
        "backend": "Python/matplotlib",
    }


def _ablation_colors(variants: Sequence[str]) -> dict[str, str]:
    palette = (
        "#484878", "#7884B4", "#B4C0E4", "#E4CCD8",
        "#C7798A", "#B64342", "#42949E", "#767676",
    )
    return {variant: palette[index % len(palette)]
            for index, variant in enumerate(variants)}


def _plot_ablation_summary(rows: Sequence[Mapping[str, Any]], output: Path,
                           diagnostic_text: str) -> list[Path]:
    if not rows:
        return []
    variants = [str(row.get("ablation_variant")) for row in rows]
    colors = _ablation_colors(variants)
    specifications = (
        ("fidelity_geometric_mean", "Fidelity geometric mean", "a"),
        ("move_batches_median", "Move batches (median)", "b"),
        ("move_time_us_median", "Move time (µs, median)", "c"),
        ("compiler_time_seconds_median", "Compiler time (s, median)", "d"),
    )
    fig, axes = plt.subplots(2, 2, figsize=(7.2, 5.1), layout="constrained")
    x = np.arange(len(variants))
    for ax, (metric, ylabel, panel) in zip(axes.flat, specifications):
        values = [_finite(row.get(metric)) for row in rows]
        bars = ax.bar(
            x, [value if value is not None else 0.0 for value in values],
            color=[colors[variant] for variant in variants], width=0.72,
            edgecolor="#272727", linewidth=0.45)
        for bar, value in zip(bars, values):
            if value is None:
                bar.set_alpha(0.12)
        ax.set_xticks(x, variants, rotation=24, ha="right", fontsize=5.8)
        ax.set_ylabel(ylabel)
        if metric == "fidelity_geometric_mean":
            finite = [value for value in values if value is not None]
            if finite:
                padding = max(5e-4, (max(finite) - min(finite)) * 0.25)
                ax.set_ylim(max(0.0, min(finite) - padding),
                            min(1.0, max(finite) + padding))
                ax.ticklabel_format(axis="y", style="plain", useOffset=False)
                ax.yaxis.set_major_formatter(
                    matplotlib.ticker.FormatStrFormatter("%.6f"))
        _panel(ax, panel)
    return _save_figure(
        fig, output / "ablation_four_metric",
        diagnostic_text=diagnostic_text)


def _plot_ablation_ecdf(rows: Sequence[Mapping[str, Any]], output: Path,
                        diagnostic_text: str) -> list[Path]:
    if not rows:
        return []
    variants = sorted({str(row.get("ablation_variant")) for row in rows})
    colors = _ablation_colors(variants)
    specifications = (
        ("fidelity", "Fidelity", "a"),
        ("move_batches", "Move batches", "b"),
        ("move_time_us", "Move time (µs)", "c"),
        ("compiler_time_seconds", "Compiler time (s)", "d"),
    )
    fig, axes = plt.subplots(2, 2, figsize=(7.2, 5.1), layout="constrained")
    handles = []
    labels = []
    for ax, (metric, xlabel, panel) in zip(axes.flat, specifications):
        for index, variant in enumerate(variants):
            values = [value for row in rows
                      if row.get("ablation_variant") == variant
                      if (value := _finite(row.get(metric))) is not None]
            if not values:
                continue
            x, y = _ecdf(values)
            line, = ax.step(
                x, y, where="post", color=colors[variant], linewidth=1.3,
                marker=("o", "s", "^", "D", "v", "P", "X", "*")[index % 8],
                markersize=2.8, label=variant)
            if variant not in labels:
                handles.append(line)
                labels.append(variant)
        ax.set_xlabel(xlabel)
        ax.set_ylabel("Empirical CDF")
        ax.set_ylim(0, 1.02)
        _panel(ax, panel)
    if handles:
        fig.legend(handles, labels, loc="upper center", ncol=min(3, len(labels)),
                   bbox_to_anchor=(0.5, 1.02), fontsize=5.5)
    return _save_figure(
        fig, output / "ablation_ecdf", diagnostic_text=diagnostic_text)


def _plot_ablation_effects(rows: Sequence[Mapping[str, Any]], output: Path,
                           diagnostic_text: str) -> list[Path]:
    if not rows:
        return []
    metrics = ("fidelity", "move_batches", "move_time_us",
               "compiler_time_seconds")
    labels = {
        "fidelity": "Fidelity ratio",
        "move_batches": "Move-batch ratio",
        "move_time_us": "Move-time ratio",
        "compiler_time_seconds": "Compiler-time ratio",
    }
    panels = ("a", "b", "c", "d")
    fig, axes = plt.subplots(2, 2, figsize=(7.2, 5.1), layout="constrained")
    for ax, metric, panel in zip(axes.flat, metrics, panels):
        selected = [row for row in rows if row.get("metric") == metric and
                    _finite(row.get("ratio")) is not None]
        selected.sort(key=lambda row: str(row.get("comparison")))
        y = np.arange(len(selected))[::-1]
        for position, row in zip(y, selected):
            estimate = float(row["ratio"])
            low = _finite(row.get("ci95_low"))
            high = _finite(row.get("ci95_high"))
            color = "#B64342" if row.get("variant") == "h0" else "#484878"
            if low is not None and high is not None:
                ax.plot([low, high], [position, position], color=color,
                        linewidth=1.1)
            ax.plot(estimate, position, marker="o", markersize=3.5, color=color)
        ax.axvline(1.0, color="#767676", linestyle="--", linewidth=0.7)
        ax.set_yticks(y, [str(row.get("variant")) for row in selected], fontsize=5.8)
        ax.set_xlabel(f"{labels[metric]} (variant / H=2 reference)")
        if not selected:
            ax.text(0.5, 0.5, "No eligible paired comparison",
                    transform=ax.transAxes, ha="center", va="center",
                    color="#767676")
        _panel(ax, panel)
    return _save_figure(
        fig, output / "ablation_paired_effects",
        diagnostic_text=diagnostic_text)


def render_ablation_figures(
        report: Mapping[str, Any],
        tables: Mapping[str, Sequence[Mapping[str, Any]]],
        output_directory: str | Path) -> Mapping[str, Any]:
    """Render exploratory ablation figures from five-seed circuit medians."""
    output = Path(output_directory).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    note = "Ablation is exploratory diagnostic only; not eligible for the main claim gate."
    generated: list[Path] = []
    generated.extend(_plot_ablation_summary(
        tables.get("variant_summary", []), output, note))
    generated.extend(_plot_ablation_ecdf(
        tables.get("circuit_ablation", []), output, note))
    generated.extend(_plot_ablation_effects(
        tables.get("comparisons", []), output, note))
    errors = _validate_exports(generated)
    contract = {
        "experiment_schema": 2,
        "dataset": report.get("dataset"),
        "core_conclusion": (
            "The ablation bundle localizes which residency, lookahead, and "
            "ghost-safe scheduling mechanisms change fidelity, move cost, and runtime; "
            "all comparisons remain exploratory."),
        "archetype": "quantitative grid",
        "backend": "Python/matplotlib only",
        "aggregation": report.get("aggregation_order"),
        "reference_variant": report.get("comparisons", {}).get(
            "reference_variant"),
        "statistics": (
            "five-seed within-circuit median, circuit bootstrap 95% CI, "
            "two-sided paired Wilcoxon with per-metric Holm adjustment"),
        "eligible_for_main_claim_gate": False,
        "files": [path.name for path in generated],
        "validation_errors": errors,
    }
    qa_path = output / "figure_qa.json"
    temporary = qa_path.with_name(qa_path.name + ".tmp")
    temporary.write_text(json.dumps(contract, indent=2, sort_keys=True) + "\n",
                         encoding="utf-8")
    os.replace(temporary, qa_path)
    if errors:
        raise RuntimeError("ablation figure QA failed: " + " | ".join(errors))
    return {
        "files": [str(path) for path in generated],
        "qa_path": str(qa_path),
        "backend": "Python/matplotlib",
    }


__all__ = ["render_ablation_figures", "render_report_figures"]
