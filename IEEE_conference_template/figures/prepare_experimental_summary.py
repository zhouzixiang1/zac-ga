#!/usr/bin/env python3
"""Validate aggregate-paper Fig. 6 CSVs and emit compact PGFPlots tables.

The converter is read-only with respect to the raw experiment CSVs. It writes
only derived ``fig6_*.dat`` files and ``fig6_meta.tex`` into ``--output-root``
(the input root by default). Missing schemas, empty paired cohorts, or non-finite
metrics terminate before any derived output is replaced; values are never
silently filled with zero.  The fidelity tables retain every strict-paired
independent unit in deterministic delta-log-fidelity order: ZAC18 uses input
files, whereas QMAP154 uses canonical SHA-256 clusters.  The complete profiles
remain available for audit and the main results table; Fig. 6 uses the same
frozen aggregate values together with explicitly labelled high-gain examples.
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import re
import statistics
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Iterable, Mapping, Sequence


RAW_FILES = {
    "fidelity": "fig6_fidelity_gain.csv",
    "mechanism": "fig6_mechanism.csv",
    "ablation": "fig6_ablation.csv",
    "stage": "fig6_m4_stage_time.csv",
}

DETAIL_FILES = {
    "ZAC18": "zac18.csv",
    "QMAP154": "qmap154.csv",
}

MECHANISM_KEYS = (
    ("transfer", "log_atom_transfer"),
    ("idle", "log_idle_excitation"),
    ("coherence", "log_coherence_linear"),
)

STAGE_KEYS = (
    "full_compile_s",
    "initial_placement_s",
    "problem_preparation_s",
    "search_kernel_s",
    "result_commit_s",
    "routing_s",
)

CANONICAL_SHA256 = re.compile(r"[0-9a-fA-F]{64}")
CLUSTER_MEAN_FIELDS = (
    "baseline_log_fidelity",
    "baseline_fidelity",
    "M4_log_fidelity",
    "M4_fidelity",
    "M4_minus_Bstar_delta_logF",
    "M4_over_Bstar_ratio",
)


def _norm(text: str) -> str:
    return "".join(ch.lower() for ch in text.strip() if ch.isalnum())


def _dataset(text: str) -> str:
    aliases = {
        "zac": "ZAC18",
        "zac18": "ZAC18",
        "qmap": "QMAP154",
        "qmap154": "QMAP154",
        "iccad": "QMAP154",
        "iccad154": "QMAP154",
    }
    key = _norm(text)
    if key not in aliases:
        raise ValueError(f"unknown dataset label: {text!r}")
    return aliases[key]


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "y", "valid", "success"}


def _finite(value: str, *, field: str, row_number: int) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"row {row_number}: {field} is not numeric: {value!r}") from exc
    if not math.isfinite(result):
        raise ValueError(f"row {row_number}: {field} is not finite: {value!r}")
    return result


def _header(row: Mapping[str, str], candidates: Sequence[str]) -> str | None:
    by_norm = {_norm(key): key for key in row.keys()}
    for candidate in candidates:
        if _norm(candidate) in by_norm:
            return by_norm[_norm(candidate)]
    return None


def _require_header(row: Mapping[str, str], candidates: Sequence[str], *, source: str) -> str:
    result = _header(row, candidates)
    if result is None:
        raise ValueError(f"{source}: missing column; expected one of {list(candidates)}")
    return result


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise FileNotFoundError(f"missing raw Fig. 6 input: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError(f"{path}: empty CSV header")
        rows = [dict(row) for row in reader]
    if not rows:
        raise ValueError(f"{path}: no data rows")
    return rows


def _median_by_circuit(
    rows: Iterable[tuple[str, str, Mapping[str, float]]]
) -> list[tuple[str, str, dict[str, float]]]:
    grouped: dict[tuple[str, str], dict[str, list[float]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for dataset, circuit, values in rows:
        for key, value in values.items():
            grouped[(dataset, circuit)][key].append(value)
    result = []
    for (dataset, circuit), values in sorted(grouped.items()):
        result.append(
            (
                dataset,
                circuit,
                {key: statistics.median(samples) for key, samples in values.items()},
            )
        )
    return result


def _quantile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        raise ValueError("cannot compute a quantile of an empty sequence")
    if not 0.0 <= probability <= 1.0:
        raise ValueError("quantile probability must be in [0, 1]")
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _display_profile(values: Sequence[float], *, robust: bool) -> dict[str, float | int]:
    """Return transparent plot limits without discarding long-tail circuits.

    QMAP uses a 5--95% body scale.  Tail values are clamped only for display
    and marked explicitly by triangles; all statistics retain the raw values.
    """
    if not values:
        raise ValueError("cannot build a display profile for no values")
    true_low = min(values)
    true_high = max(values)
    if robust and len(values) >= 20:
        clip_low = min(0.0, _quantile(values, 0.05))
        clip_high = max(0.0, _quantile(values, 0.95))
    else:
        clip_low = min(0.0, true_low)
        clip_high = max(0.0, true_high)
    span = max(clip_high - clip_low, 1e-6)
    return {
        "clip_low": clip_low,
        "clip_high": clip_high,
        "axis_low": clip_low - 0.08 * span,
        "axis_high": clip_high + 0.08 * span,
        "true_low": true_low,
        "true_high": true_high,
        "low_tail_n": sum(value < clip_low for value in values),
        "high_tail_n": sum(value > clip_high for value in values),
    }


def _independent_gain_rows(
    rows: list[dict[str, str]], source: str
) -> dict[str, list[dict[str, str]]]:
    """Return one row per declared independent analysis unit.

    ZAC18 is analysed by input file.  QMAP154 is analysed by canonical SHA-256
    cluster because distinct filenames can encode the same normalized circuit.
    The v2 aggregate already stores cluster-mean metrics on every member row;
    repeated members must therefore agree, and the lexicographically first
    circuit name is retained only as a deterministic display label.
    """
    first = rows[0]
    dataset_col = _require_header(first, ["dataset"], source=source)
    circuit_col = _require_header(first, ["circuit"], source=source)
    paired_col = _require_header(first, ["strict_paired"], source=source)
    canonical_col = _require_header(first, ["canonical_sha256"], source=source)
    unit_col = _require_header(
        first, ["independent_analysis_unit"], source=source)
    cluster_n_col = _require_header(
        first, ["canonical_cluster_file_N"], source=source)
    for field in CLUSTER_MEAN_FIELDS:
        _require_header(first, [field], source=source)

    parsed: dict[str, list[dict[str, str]]] = defaultdict(list)
    seen_files: set[tuple[str, str]] = set()
    for index, raw_row in enumerate(rows, start=2):
        if not _truthy(raw_row[paired_col]):
            continue
        dataset = _dataset(raw_row[dataset_col])
        circuit = raw_row[circuit_col].strip()
        if not circuit:
            raise ValueError(f"{source} row {index}: empty circuit")
        file_key = (dataset, circuit)
        if file_key in seen_files:
            raise ValueError(
                f"{source} row {index}: duplicate paired circuit {dataset}/{circuit}")
        seen_files.add(file_key)

        canonical = raw_row[canonical_col].strip().lower()
        if not CANONICAL_SHA256.fullmatch(canonical):
            raise ValueError(
                f"{source} row {index}: invalid canonical_sha256 for {circuit}")
        expected_unit = (
            "circuit_file" if dataset == "ZAC18"
            else "canonical_sha256_cluster_mean"
        )
        if raw_row[unit_col].strip() != expected_unit:
            raise ValueError(
                f"{source} row {index}: {dataset} independent_analysis_unit "
                f"must be {expected_unit!r}")
        try:
            cluster_file_n = int(raw_row[cluster_n_col])
        except ValueError as exc:
            raise ValueError(
                f"{source} row {index}: canonical_cluster_file_N is not an integer"
            ) from exc
        if cluster_file_n < 1:
            raise ValueError(
                f"{source} row {index}: canonical_cluster_file_N must be positive")

        row = dict(raw_row)
        row["_dataset"] = dataset
        row["_circuit"] = circuit
        row["_canonical_sha256"] = canonical
        row["_cluster_file_n"] = str(cluster_file_n)
        parsed[dataset].append(row)

    if not parsed["ZAC18"] or not parsed["QMAP154"]:
        raise ValueError(f"{source}: both data sets require strict-paired rows")

    independent = {
        "ZAC18": sorted(parsed["ZAC18"], key=lambda row: row["_circuit"]),
        "QMAP154": [],
    }
    qmap_clusters: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in parsed["QMAP154"]:
        qmap_clusters[row["_canonical_sha256"]].append(row)
    for canonical, members in sorted(qmap_clusters.items()):
        members.sort(key=lambda row: row["_circuit"])
        declared_sizes = {int(row["_cluster_file_n"]) for row in members}
        if len(declared_sizes) != 1 or next(iter(declared_sizes)) < len(members):
            raise ValueError(
                f"{source}: inconsistent cluster size for canonical {canonical}")
        if len(members) > 1:
            for field in CLUSTER_MEAN_FIELDS:
                values = [
                    _finite(row[field], field=field, row_number=0)
                    for row in members
                ]
                if not all(math.isclose(
                        value, values[0], rel_tol=0.0, abs_tol=1e-12)
                        for value in values[1:]):
                    raise ValueError(
                        f"{source}: cluster-mean field {field} disagrees for "
                        f"canonical {canonical}")
            methods = {row.get("baseline_method", "").strip() for row in members}
            if len(methods) != 1:
                raise ValueError(
                    f"{source}: baseline method disagrees for canonical {canonical}")
        independent["QMAP154"].append(members[0])
    return independent


def _fidelity(rows: list[dict[str, str]], source: str):
    first = rows[0]
    delta_col = _require_header(
        first, ["M4_minus_Bstar_delta_logF", "delta_logF"], source=source
    )
    ratio_col = _header(first, ["M4_over_Bstar_ratio", "fidelity_ratio"])

    independent = _independent_gain_rows(rows, source)
    grouped: dict[str, list[tuple[str, str, float, float]]] = defaultdict(list)
    for dataset in ("ZAC18", "QMAP154"):
        for row in independent[dataset]:
            delta = _finite(row[delta_col], field=delta_col, row_number=0)
            ratio = (
                _finite(row[ratio_col], field=ratio_col, row_number=0)
                if ratio_col and row.get(ratio_col, "").strip()
                else math.exp(delta)
            )
            grouped[dataset].append((
                row["_circuit"], row["_canonical_sha256"], delta, ratio))
        grouped[dataset].sort(key=lambda item: (item[2], item[1], item[0]))
    return grouped


def _validate_ranked_fidelity(fidelity) -> None:
    """Require one complete, ordered record per plotted analysis unit.

    Panel (b) maps the integer rank to a within-dataset percentile at render
    time.  Duplicate circuit identifiers, a non-monotone order, or a
    non-positive fidelity ratio would make that distribution misleading.
    """
    for dataset in ("ZAC18", "QMAP154"):
        records = fidelity.get(dataset, [])
        if not records:
            raise ValueError(f"ranked fidelity data are empty for {dataset}")
        circuits = [
            str(circuit) for circuit, _canonical, _delta, _ratio in records]
        if len(set(circuits)) != len(circuits):
            raise ValueError(
                f"ranked fidelity data contain duplicate circuits for {dataset}")
        canonical = [
            str(value) for _circuit, value, _delta, _ratio in records]
        if dataset == "QMAP154" and len(set(canonical)) != len(canonical):
            raise ValueError(
                "ranked fidelity data contain duplicate canonical QMAP clusters")
        deltas = [
            float(delta) for _circuit, _canonical, delta, _ratio in records]
        if any(left > right for left, right in zip(deltas, deltas[1:])):
            raise ValueError(
                f"ranked fidelity data are not sorted for {dataset}")
        if any(float(ratio) <= 0
               for _circuit, _canonical, _delta, ratio in records):
            raise ValueError(
                f"ranked fidelity data contain a non-positive ratio for {dataset}")


def _win_tie_loss(records, *, tolerance: float = 1e-15) -> tuple[int, int, int]:
    deltas = [
        float(delta) for _circuit, _canonical, delta, _ratio in records]
    wins = sum(delta > tolerance for delta in deltas)
    losses = sum(delta < -tolerance for delta in deltas)
    ties = len(deltas) - wins - losses
    return wins, ties, losses


def _representative_cases(
    gain_rows: list[dict[str, str]],
    detail_rows: Mapping[str, list[dict[str, str]]],
):
    """Select one median and two non-degenerate right-tail cases per dataset.

    The median is the nearest-rank P50 observation of delta log fidelity.  The
    right-tail cases are the two largest deltas among units whose composite
    baseline fidelity is at least 0.05.  The floor avoids presenting enormous
    ratios that arise only because both fidelities are numerically near zero.
    """
    gain_by_dataset = _independent_gain_rows(
        gain_rows, RAW_FILES["fidelity"])

    details: dict[str, dict[str, dict[str, str]]] = {}
    for dataset, rows in detail_rows.items():
        by_circuit: dict[str, dict[str, str]] = {}
        for index, row in enumerate(rows, start=2):
            circuit = row.get("circuit", "").strip()
            if not circuit:
                raise ValueError(
                    f"{DETAIL_FILES[dataset]} row {index}: empty circuit")
            if circuit in by_circuit:
                raise ValueError(
                    f"{DETAIL_FILES[dataset]}: duplicate circuit {circuit}")
            by_circuit[circuit] = row
        details[dataset] = by_circuit

    selected: dict[str, list[dict[str, object]]] = {}
    for dataset in ("ZAC18", "QMAP154"):
        candidates = gain_by_dataset[dataset]
        if not candidates:
            raise ValueError(f"no strict-paired gain rows for {dataset}")

        def enriched(row: dict[str, str]) -> dict[str, object]:
            circuit = row["_circuit"]
            detail = details[dataset].get(circuit)
            if detail is None:
                raise ValueError(
                    f"{DETAIL_FILES[dataset]}: missing detail row for {circuit}")
            canonical = row["_canonical_sha256"]
            if detail.get("canonical_sha256", "").strip().lower() != canonical:
                raise ValueError(
                    f"{DETAIL_FILES[dataset]}: canonical_sha256 mismatch for {circuit}")
            fidelities = {
                method: _finite(
                    detail[f"M{index}__fidelity"],
                    field=f"M{index}__fidelity", row_number=0,
                )
                for index, method in enumerate(
                    ("ZAC", "ICCAD/QMAP", "GA-NL", "GA-LK"), start=1)
            }
            return {
                "dataset": dataset,
                "circuit": circuit,
                "canonical_sha256": canonical,
                "qubits": int(float(detail["qubits"])),
                "gates_2q": int(float(detail["gates_2q"])),
                "delta_logF": _finite(
                    row["M4_minus_Bstar_delta_logF"],
                    field="M4_minus_Bstar_delta_logF", row_number=0),
                "ratio": _finite(
                    row["M4_over_Bstar_ratio"],
                    field="M4_over_Bstar_ratio", row_number=0),
                "baseline_fidelity": _finite(
                    row["baseline_fidelity"],
                    field="baseline_fidelity", row_number=0),
                "fidelities": fidelities,
            }

        records = [enriched(row) for row in candidates]
        records.sort(
            key=lambda row: (
                float(row["delta_logF"]), str(row["canonical_sha256"]),
                str(row["circuit"])))
        median = records[math.ceil(0.5 * len(records)) - 1]
        eligible = [
            row for row in records
            if float(row["baseline_fidelity"]) >= 0.05
            and float(row["delta_logF"]) > 0
            and row["canonical_sha256"] != median["canonical_sha256"]
        ]
        eligible.sort(
            key=lambda row: (
                -float(row["delta_logF"]), str(row["canonical_sha256"]),
                str(row["circuit"])))
        if len(eligible) < 2:
            raise ValueError(
                f"{dataset}: fewer than two right-tail cases above fidelity floor")
        median = dict(median)
        median["role"] = "median"
        gains = []
        for index, row in enumerate(eligible[:2], start=1):
            item = dict(row)
            item["role"] = f"tail-{index}"
            gains.append(item)
        selected[dataset] = [median, *gains]
        if len({row["canonical_sha256"] for row in selected[dataset]}) != 3:
            raise ValueError(
                f"{dataset}: representative cases repeat an analysis unit")
    return selected


def _mechanism(rows: list[dict[str, str]], source: str):
    first = rows[0]
    dataset_col = _require_header(first, ["dataset"], source=source)
    circuit_col = _require_header(first, ["circuit"], source=source)
    paired_col = _require_header(first, ["strict_paired"], source=source)
    columns: dict[str, tuple[str | None, str | None, str | None]] = {}
    for output_name, raw_name in MECHANISM_KEYS:
        direct = _header(
            first,
            [
                f"M4_minus_Bstar_{raw_name}",
                f"delta_{raw_name}",
                f"{raw_name}_delta",
            ],
        )
        m4 = _header(first, [f"M4_{raw_name}"])
        baseline = _header(first, [f"Bstar_{raw_name}"])
        if direct is None and (m4 is None or baseline is None):
            raise ValueError(
                f"{source}: cannot derive {output_name}; need M4_minus_Bstar_{raw_name} "
                f"or both M4_{raw_name} and Bstar_{raw_name}"
            )
        columns[output_name] = (direct, m4, baseline)

    parsed = []
    for index, row in enumerate(rows, start=2):
        if not _truthy(row[paired_col]):
            continue
        dataset = _dataset(row[dataset_col])
        circuit = row[circuit_col].strip()
        if not circuit:
            raise ValueError(f"{source} row {index}: empty circuit")
        values = {}
        for output_name, (direct, m4, baseline) in columns.items():
            if direct:
                values[output_name] = _finite(row[direct], field=direct, row_number=index)
            else:
                assert m4 is not None and baseline is not None
                values[output_name] = _finite(row[m4], field=m4, row_number=index) - _finite(
                    row[baseline], field=baseline, row_number=index
                )
        parsed.append((dataset, circuit, values))

    per_circuit = _median_by_circuit(parsed)
    grouped: dict[str, list[dict[str, float]]] = defaultdict(list)
    for dataset, _circuit, values in per_circuit:
        grouped[dataset].append(values)
    result = {}
    for dataset in ("ZAC18", "QMAP154"):
        if not grouped[dataset]:
            raise ValueError(f"{source}: no strict-paired mechanism rows for {dataset}")
        result[dataset] = {
            key: statistics.fmean(item[key] for item in grouped[dataset])
            for key, _raw in MECHANISM_KEYS
        }
        result[dataset]["n"] = len(grouped[dataset])
    return result


def _ablation(rows: list[dict[str, str]], source: str):
    first = rows[0]
    dataset_col = _require_header(first, ["dataset"], source=source)
    circuit_col = _require_header(first, ["circuit"], source=source)
    comparison_col = _require_header(first, ["comparison"], source=source)
    paired_col = _require_header(first, ["paired_valid"], source=source)
    delta_col = _require_header(first, ["delta_logF"], source=source)
    aliases = {
        "h8vsh0": "H8/H0",
        "h8h0": "H8/H0",
        "gavsgreedy": "GA/greedy",
        "gagreedy": "GA/greedy",
    }
    parsed: dict[str, list[tuple[str, str, float]]] = defaultdict(list)
    for index, row in enumerate(rows, start=2):
        if not _truthy(row[paired_col]):
            continue
        raw_comparison = _norm(row[comparison_col])
        if raw_comparison not in aliases:
            raise ValueError(
                f"{source} row {index}: unknown comparison {row[comparison_col]!r}"
            )
        comparison = aliases[raw_comparison]
        dataset = _dataset(row[dataset_col])
        circuit = row[circuit_col].strip()
        if not circuit:
            raise ValueError(f"{source} row {index}: empty circuit")
        delta = _finite(row[delta_col], field=delta_col, row_number=index)
        parsed[comparison].append((dataset, circuit, delta))

    result = {}
    for comparison in ("H8/H0", "GA/greedy"):
        if not parsed[comparison]:
            raise ValueError(f"{source}: no paired-valid rows for {comparison}")
        by_circuit: dict[tuple[str, str], list[float]] = defaultdict(list)
        for dataset, circuit, value in parsed[comparison]:
            by_circuit[(dataset, circuit)].append(value)
        duplicates = sorted(identity for identity, values in by_circuit.items()
                            if len(values) != 1)
        if duplicates:
            raise ValueError(
                f"{source}: duplicate paired rows for {comparison}: {duplicates[:5]}")
        circuit_values = [values[0] for values in by_circuit.values()]
        mean_delta = statistics.fmean(circuit_values)
        result[comparison] = {
            "delta_logF": mean_delta,
            "ratio": math.exp(mean_delta),
            "n": len(circuit_values),
        }
    return result


def _stage(rows: list[dict[str, str]], source: str):
    first = rows[0]
    columns = {key: _require_header(first, [key], source=source) for key in STAGE_KEYS}
    dataset_col = _header(first, ["dataset"])
    circuit_col = _header(first, ["circuit"])
    status_col = _require_header(first, ["status"], source=source)
    valid_col = _require_header(first, ["valid"], source=source)
    expected_col = _require_header(first, ["N"], source=source)

    parsed = []
    for index, row in enumerate(rows, start=2):
        valid_number = _finite(row[valid_col], field=valid_col, row_number=index)
        expected_number = _finite(
            row[expected_col], field=expected_col, row_number=index)
        if (not valid_number.is_integer() or not expected_number.is_integer() or
                valid_number < 0 or expected_number <= 0):
            raise ValueError(
                f"{source} row {index}: valid/N must be non-negative integers")
        valid = int(valid_number)
        expected = int(expected_number)
        complete = valid == expected == 3
        if _truthy(row[status_col]) != complete:
            raise ValueError(
                f"{source} row {index}: success/seed-completeness mismatch "
                f"(status={row[status_col]!r}, valid={valid}, N={expected})")
        if not complete:
            continue
        dataset = _dataset(row[dataset_col]) if dataset_col else "ALL"
        circuit = row[circuit_col].strip() if circuit_col else f"row-{index}"
        values = {
            key: _finite(row[column], field=column, row_number=index)
            for key, column in columns.items()
        }
        if any(value < 0 for value in values.values()):
            raise ValueError(f"{source} row {index}: negative timing value")
        additive_sum = sum(values[key] for key in STAGE_KEYS[1:])
        # Each paper row stores the per-stage median across three seeds, while
        # full_compile_s is the independently computed median of the total.
        # Median is not additive, so a small positive discrepancy is valid.
        # Keep a 1% guard to reject genuine overlap/double counting.
        if additive_sum > values["full_compile_s"] * 1.01 + 1e-9:
            raise ValueError(
                f"{source} row {index}: additive stages exceed full compile time")
        parsed.append((dataset, circuit, values))
    if not parsed:
        raise ValueError(f"{source}: no valid M4 timing rows")

    per_circuit = _median_by_circuit(parsed)
    means = {
        key: statistics.fmean(values[key] for _dataset_name, _circuit, values in per_circuit)
        for key in STAGE_KEYS
    }
    additive_keys = STAGE_KEYS[1:]
    additive_sum = sum(means[key] for key in additive_keys)
    if additive_sum <= 0:
        raise ValueError(f"{source}: additive M4 stage time is not positive")
    means.update(
        {
            "initial_pct": 100.0 * means["initial_placement_s"] / additive_sum,
            "prepare_pct": 100.0 * means["problem_preparation_s"] / additive_sum,
            "search_pct": 100.0 * means["search_kernel_s"] / additive_sum,
            "commit_pct": 100.0 * means["result_commit_s"] / additive_sum,
            "routing_pct": 100.0 * means["routing_s"] / additive_sum,
            "additive_sum_s": additive_sum,
            "n": len(per_circuit),
        }
    )
    return means


def _fmt(value: float) -> str:
    return f"{value:.12g}"


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _tex_circuit_name(name: str) -> str:
    return r"\texttt{\detokenize{" + name + "}}"


def _short_circuit_name(name: str) -> str:
    if name.endswith("_transpiled"):
        name = name[:-len("_transpiled")]
    return name


def _figure_circuit_name(name: str) -> str:
    """Return a compact, family-preserving label for the Fig. 6 x axis."""
    name = _short_circuit_name(name)
    match = re.fullmatch(r"qft_n(\d+)", name)
    if match:
        return f"QFT-{match.group(1)}"
    match = re.fullmatch(r"ising_n(\d+)", name)
    if match:
        return f"Ising-{match.group(1)}"
    match = re.fullmatch(r"ising_model_(\d+)", name)
    if match:
        return f"Ising-{match.group(1)}"
    return name.replace("_", "-")


def _format_fidelity(value: float) -> str:
    return f"{value:.4f}"


def _emit(output_dir: Path, fidelity, mechanism, ablation, stage,
          representative) -> None:
    _validate_ranked_fidelity(fidelity)
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="fig6-derived-", dir=output_dir) as tmp:
        tmp_dir = Path(tmp)
        fidelity_profiles = {
            dataset: _display_profile(
                [item[2] for item in fidelity[dataset]],
                robust=(dataset == "QMAP154"),
            )
            for dataset in ("ZAC18", "QMAP154")
        }
        for dataset, filename in (
            ("ZAC18", "fig6_zac_fidelity.dat"),
            ("QMAP154", "fig6_qmap_fidelity.dat"),
        ):
            profile = fidelity_profiles[dataset]
            # Ranks are converted to within-dataset percentiles in PGFPlots.
            # Keep raw and display deltas together so clipped tail markers
            # remain traceable to their true values without dropping records.
            lines = ["rank delta_logF display_delta tail ratio"]
            for rank, (_circuit, _canonical, delta, ratio) in enumerate(
                    fidelity[dataset], start=1):
                displayed = min(
                    float(profile["clip_high"]),
                    max(float(profile["clip_low"]), delta),
                )
                tail = -1 if delta < float(profile["clip_low"]) else (
                    1 if delta > float(profile["clip_high"]) else 0
                )
                lines.append(
                    f"{rank} {_fmt(delta)} {_fmt(displayed)} {tail} {_fmt(ratio)}"
                )
            _write(tmp_dir / filename, "\n".join(lines) + "\n")

        mechanism_lines = ["dataset transfer idle coherence"]
        for dataset in ("ZAC18", "QMAP154"):
            values = mechanism[dataset]
            mechanism_lines.append(
                f"{dataset} {_fmt(values['transfer'])} {_fmt(values['idle'])} "
                f"{_fmt(values['coherence'])}"
            )
        _write(tmp_dir / "fig6_mechanism.dat", "\n".join(mechanism_lines) + "\n")

        ablation_lines = ["comparison delta_logF ratio n"]
        for comparison in ("H8/H0", "GA/greedy"):
            values = ablation[comparison]
            ablation_lines.append(
                f"{comparison} {_fmt(values['delta_logF'])} {_fmt(values['ratio'])} {values['n']}"
            )
        _write(tmp_dir / "fig6_ablation.dat", "\n".join(ablation_lines) + "\n")

        stage_lines = [
            "label initial_pct prepare_pct search_pct commit_pct routing_pct full_compile_s additive_sum_s"
        ]
        stage_lines.append(
            "M4 "
            + " ".join(
                _fmt(stage[key])
                for key in (
                    "initial_pct",
                    "prepare_pct",
                    "search_pct",
                    "commit_pct",
                    "routing_pct",
                    "full_compile_s",
                    "additive_sum_s",
                )
            )
        )
        _write(tmp_dir / "fig6_stage_time.dat", "\n".join(stage_lines) + "\n")

        high_gain = [
            row for dataset in ("ZAC18", "QMAP154")
            for row in representative[dataset]
            if str(row["role"]).startswith("tail-")
        ]
        selected_lines = [
            "x gain_pct ratio dataset ratio_zac ratio_iccad "
            "gain_zac_pct gain_iccad_pct"
        ]
        for index, row in enumerate(high_gain, start=1):
            fidelities = dict(row["fidelities"])
            ga_lk = float(fidelities["GA-LK"])
            zac = float(fidelities["ZAC"])
            iccad = float(fidelities["ICCAD/QMAP"])
            if min(ga_lk, zac, iccad) <= 0:
                raise ValueError(
                    "selected circuit fidelities must be strictly positive")
            ratio_zac = ga_lk / zac
            ratio_iccad = ga_lk / iccad
            selected_lines.append(
                f"{index} {_fmt(100.0 * (float(row['ratio']) - 1.0))} "
                f"{_fmt(float(row['ratio']))} "
                f"{0 if row['dataset'] == 'ZAC18' else 1} "
                f"{_fmt(ratio_zac)} {_fmt(ratio_iccad)} "
                f"{_fmt(100.0 * (ratio_zac - 1.0))} "
                f"{_fmt(100.0 * (ratio_iccad - 1.0))}"
            )
        _write(
            tmp_dir / "fig6_selected_cases.dat",
            "\n".join(selected_lines) + "\n",
        )

        selected_tex = [
            "% Auto-generated by prepare_experimental_summary.py; do not edit.",
        ]
        label_suffixes = ("A", "B", "C", "D")
        if len(high_gain) != len(label_suffixes):
            raise ValueError("the representative table requires four high-gain cases")
        for suffix, row in zip(label_suffixes, high_gain, strict=True):
            selected_tex.append(
                f"\\def\\FigSixLabel{suffix}{{\\texttt{{{_figure_circuit_name(str(row['circuit']))}}}}}"
            )
        selected_tex.append(r"\def\RepresentativeCircuitRows{%")
        for dataset in ("ZAC18", "QMAP154"):
            for row in representative[dataset]:
                if row["role"] == "median":
                    continue
                values = dict(row["fidelities"])
                displayed = {
                    method: _format_fidelity(float(value))
                    for method, value in values.items()
                }
                best = max(
                    ("ZAC", "ICCAD/QMAP", "GA-LK"),
                    key=lambda method: float(values[method]),
                )
                displayed[best] = r"\textbf{" + displayed[best] + "}"
                selected_tex.append(
                    f"{dataset} & "
                    f"{_tex_circuit_name(_short_circuit_name(str(row['circuit'])))} & "
                    f"{row['qubits']}/{row['gates_2q']} & {displayed['ZAC']} & "
                    f"{displayed['ICCAD/QMAP']} & {displayed['GA-LK']} \\\\%"
                )
            if dataset == "ZAC18":
                selected_tex.append(r"\addlinespace[1pt]")
        selected_tex.append("}")
        selected_tex.append("")
        _write(
            tmp_dir / "fig6_selected_cases.tex",
            "\n".join(selected_tex),
        )

        zac_n = len(fidelity["ZAC18"])
        qmap_n = len(fidelity["QMAP154"])
        zac_wtl = _win_tie_loss(fidelity["ZAC18"])
        qmap_wtl = _win_tie_loss(fidelity["QMAP154"])
        zac_profile = fidelity_profiles["ZAC18"]
        qmap_profile = fidelity_profiles["QMAP154"]
        meta = "\n".join(
            [
                "% Auto-generated by prepare_experimental_summary.py; do not edit.",
                f"\\def\\FigSixZACN{{{zac_n}}}",
                f"\\def\\FigSixQMAPN{{{qmap_n}}}",
                f"\\def\\FigSixZACWTL{{{zac_wtl[0]}/{zac_wtl[1]}/{zac_wtl[2]}}}",
                f"\\def\\FigSixQMAPWTL{{{qmap_wtl[0]}/{qmap_wtl[1]}/{qmap_wtl[2]}}}",
                f"\\def\\FigSixZACXMax{{{zac_n + 0.5}}}",
                f"\\def\\FigSixQMAPXMax{{{qmap_n + 0.5}}}",
                f"\\def\\FigSixZACYMin{{{_fmt(float(zac_profile['axis_low']))}}}",
                f"\\def\\FigSixZACYMax{{{_fmt(float(zac_profile['axis_high']))}}}",
                f"\\def\\FigSixQMAPYMin{{{_fmt(float(qmap_profile['axis_low']))}}}",
                f"\\def\\FigSixQMAPYMax{{{_fmt(float(qmap_profile['axis_high']))}}}",
                f"\\def\\FigSixQMAPTrueMin{{{_fmt(float(qmap_profile['true_low']))}}}",
                f"\\def\\FigSixQMAPTrueMax{{{_fmt(float(qmap_profile['true_high']))}}}",
                f"\\def\\FigSixQMAPLowTailN{{{qmap_profile['low_tail_n']}}}",
                f"\\def\\FigSixQMAPHighTailN{{{qmap_profile['high_tail_n']}}}",
                f"\\def\\FigSixMechanismZACN{{{mechanism['ZAC18']['n']}}}",
                f"\\def\\FigSixMechanismQMAPN{{{mechanism['QMAP154']['n']}}}",
                f"\\def\\FigSixAblationHN{{{ablation['H8/H0']['n']}}}",
                f"\\def\\FigSixAblationGAN{{{ablation['GA/greedy']['n']}}}",
                f"\\def\\FigSixStageN{{{stage['n']}}}",
                f"\\def\\FigSixFullCompileMean{{{stage['full_compile_s']:.3f}}}",
                f"\\def\\FigSixAdditiveMean{{{stage['additive_sum_s']:.3f}}}",
                "",
            ]
        )
        _write(tmp_dir / "fig6_meta.tex", meta)

        for derived in sorted(tmp_dir.iterdir()):
            os.replace(derived, output_dir / derived.name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input-root",
        type=Path,
        default=None,
        help="directory containing the four raw aggregate-paper CSV files",
    )
    parser.add_argument(
        "--output-root",
        "--output-dir",
        dest="output_root",
        type=Path,
        default=None,
        help="derived table directory (defaults to the input root)",
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=None,
        help="compatibility alias for an input root that also defaults the output root",
    )
    args = parser.parse_args()
    if args.input_root is not None and args.data_root is not None:
        if args.input_root.resolve() != args.data_root.resolve():
            parser.error("--input-root and --data-root identify different directories")
    input_root = args.input_root or args.data_root or Path("figures/data")
    root = input_root.resolve()
    output_dir = (args.output_root or input_root).resolve()

    raw = {name: _read_csv(root / filename) for name, filename in RAW_FILES.items()}
    detail = {
        dataset: _read_csv(root / filename)
        for dataset, filename in DETAIL_FILES.items()
    }
    fidelity = _fidelity(raw["fidelity"], RAW_FILES["fidelity"])
    representative = _representative_cases(raw["fidelity"], detail)
    mechanism = _mechanism(raw["mechanism"], RAW_FILES["mechanism"])
    ablation = _ablation(raw["ablation"], RAW_FILES["ablation"])
    stage = _stage(raw["stage"], RAW_FILES["stage"])
    _emit(output_dir, fidelity, mechanism, ablation, stage, representative)
    print(
        "Fig. 6 derived data ready: "
        f"ZAC18={len(fidelity['ZAC18'])}, QMAP154={len(fidelity['QMAP154'])}, "
        f"H8/H0={ablation['H8/H0']['n']}, GA/greedy={ablation['GA/greedy']['n']}, "
        f"stage={stage['n']} -> {output_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
