"""Strict QASMBench Small/Medium/Large aggregation and CSV contracts.

The module intentionally stops before XLSX authoring.  It consumes the frozen
``qasmbench-source-manifest-v1`` inventory and explicit terminal run manifests,
then produces renderer-independent data for exactly four logical sheets:
``Summary``, ``Small``, ``Medium`` and ``Large``.

Important aggregation rules are fail-closed:

* the row join key is ``(benchmark_scale, benchmark_directory)``;
* a strict paired row requires all four methods to be verified successes and
  to share the canonical input, gate counts and gate-ledger hash;
* M3/M4 use the median of seeds 0/1/2 once all three terminal manifests exist,
  otherwise the seed-0 result remains an explicit interim result;
* linear-model Fidelity is compared in log space against the better of M1/M2
  and is omitted whenever any of the four methods is OOD or lacks usable logF;
* transfer, Move and compiler-time summaries retain legitimate zero values and
  never impute a missing value.

The writer emits only JSON/CSV evidence.  A separate artifact-tool renderer is
responsible for the final workbook and for sealing ``final_manifest.json``.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import statistics
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SOURCE_MANIFEST_SCHEMA = "qasmbench-source-manifest-v1"
AGGREGATE_SCHEMA = "qasmbench-four-method-aggregate-v1"
FINAL_PAYLOAD_SCHEMA = "qasmbench-final-manifest-payload-v1"
QASMBENCH_COMMIT = "357b942396d5c2b7cbc1c229c585a6ef5ccaebac"
QASMBENCH_REPOSITORY = "https://github.com/pnnl/QASMBench.git"
CANONICAL_PROFILE = "qasmbench_standard_expand_v1"
SCALES = ("small", "medium", "large")
SHEET_BY_SCALE = {"small": "Small", "medium": "Medium", "large": "Large"}
SHEET_NAMES = ("Summary", "Small", "Medium", "Large")
METHODS = ("M1", "M2", "M3", "M4")
OURS_METHODS = ("M3", "M4")
EXPECTED_DIRECTORY_COUNTS = {"small": 42, "medium": 25, "large": 70}
EXPECTED_SELECTED_QASM = 131
EXPECTED_NO_QASM_SOURCE = 6
RSS_LIMIT_BYTES = 22 * (1 << 30)
MINIMUM_FREE_BYTES = 4 * (1 << 30)
CONCURRENCY_BY_SCALE = {"small": 4, "medium": 2, "large": 1}
EVENT_HASH_PROTOCOL = "sha256-uncompressed-canonical-jsonl-v1"
SOURCE_STATUSES = {"success", "canonical_error", "no_qasm_source"}
SELECTION_REASONS = {
    "preferred_transpiled",
    "preferred_named",
    "sole_qasm",
    "unique_transpiled_fallback",
    "no_qasm_source",
}
TERMINAL_RUN_STATUSES = {
    "success",
    "timeout",
    "oom",
    "compiler_error",
    "verifier_fail",
    "scorer_error",
}

OURS_TIMING_FIELDS = (
    ("initial_placement_s", "initial_placement_ns"),
    ("problem_preparation_s", "problem_preparation_ns"),
    ("native_search_s", "native_search_wall_ns"),
    ("search_kernel_s", "search_kernel_ns"),
    ("return_match_s", "return_match_ns"),
    ("forecast_s", "forecast_ns"),
    ("result_commit_s", "result_commit_ns"),
    ("routing_s", "routing_ns"),
)

DETAIL_BASE_COLUMNS = (
    "benchmark_scale",
    "benchmark_directory",
    "upstream_path",
    "upstream_git_blob",
    "source_sha256",
    "selection_reason",
    "canonical_profile",
    "canonical_status",
    "canonical_error",
    "canonical_sha256",
    "qubits",
    "gates_1q",
    "gates_2q",
    "strict_paired",
    "pairing_reason",
    "fidelity_paired",
)

METHOD_COLUMNS = (
    "fidelity",
    "log_fidelity",
    "transfers",
    "move_batches",
    "move_time_ms",
    "algorithm_time_s",
    "valid_over_N",
    "status",
    "terminal_status",
    "fidelity_ood",
    "seed_policy",
)

SUMMARY_COLUMNS = (
    "benchmark_scale",
    "method",
    "official_directories",
    "selected_qasm",
    "canonical_success",
    "no_qasm_source",
    "canonical_error",
    "terminal_success",
    "verified_success",
    "timeout",
    "oom",
    "error",
    "missing",
    "success_rate_selected_qasm",
    "strict_paired_count",
    "fidelity_paired_count",
    "paired_fidelity_log_geomean",
    "paired_fidelity_geomean",
    "paired_transfers_mean",
    "paired_move_batches_mean",
    "paired_move_time_ms_mean",
    "paired_algorithm_time_s_mean",
    "fidelity_baseline_log_geomean",
    "fidelity_method_log_geomean",
    "fidelity_geometric_ratio_vs_Bstar",
    "fidelity_gain_pct",
    "fidelity_wins",
    "fidelity_ties",
    "fidelity_losses",
    "transfers_paired_count",
    "transfers_baseline_mean",
    "transfers_method_mean",
    "transfers_reduction_pct",
    "transfers_wins",
    "transfers_ties",
    "transfers_losses",
    "move_batches_paired_count",
    "move_batches_baseline_mean",
    "move_batches_method_mean",
    "move_batches_reduction_pct",
    "move_batches_wins",
    "move_batches_ties",
    "move_batches_losses",
    "move_time_ms_paired_count",
    "move_time_ms_baseline_mean",
    "move_time_ms_method_mean",
    "move_time_ms_reduction_pct",
    "move_time_ms_wins",
    "move_time_ms_ties",
    "move_time_ms_losses",
    "algorithm_time_s_paired_count",
    "algorithm_time_s_baseline_mean",
    "algorithm_time_s_method_mean",
    "algorithm_time_s_reduction_pct",
    "algorithm_time_s_speedup_vs_M2",
    "algorithm_time_s_wins",
    "algorithm_time_s_ties",
    "algorithm_time_s_losses",
)


JsonInput = Mapping[str, Any] | str | os.PathLike[str]
RunInput = Mapping[str, Any] | str | os.PathLike[str]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_hex(value: object, length: int) -> bool:
    return (
        isinstance(value, str)
        and len(value) == length
        and all(character in "0123456789abcdef" for character in value)
    )


def _load_json(value: JsonInput) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    path = Path(value)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return payload


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2,
                  sort_keys=True, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _atomic_csv(path: Path, columns: Sequence[str],
                rows: Sequence[Mapping[str, Any]]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=list(columns),
            extrasaction="raise",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _source_entries(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise ValueError("source manifest entries must be a list")
    if any(not isinstance(entry, Mapping) for entry in entries):
        raise ValueError("every source manifest entry must be an object")
    return [dict(entry) for entry in entries]


def validate_qasmbench_source_manifest(
        manifest: JsonInput, *, require_complete: bool = True) -> dict[str, Any]:
    """Validate and return a normalized frozen source manifest."""
    payload = _load_json(manifest)
    if payload.get("manifest_schema") != SOURCE_MANIFEST_SCHEMA:
        raise ValueError("wrong QASMBench source-manifest schema")
    if payload.get("upstream_repository") != QASMBENCH_REPOSITORY:
        raise ValueError("wrong QASMBench upstream repository")
    if payload.get("upstream_commit") != QASMBENCH_COMMIT:
        raise ValueError("wrong frozen QASMBench commit")
    if payload.get("canonical_profile") != CANONICAL_PROFILE:
        raise ValueError("wrong QASMBench canonical profile")
    if payload.get("trace_retained") is not False:
        raise ValueError("QASMBench source manifest must set trace_retained=false")

    entries = _source_entries(payload)
    seen: set[tuple[str, str]] = set()
    statuses: Counter[str] = Counter()
    scale_counts: Counter[str] = Counter()
    normalized_entries: list[dict[str, Any]] = []
    for raw in entries:
        scale = str(raw.get("benchmark_scale", "")).lower()
        directory = str(raw.get("benchmark_directory", ""))
        status = str(raw.get("status", ""))
        if scale not in SCALES:
            raise ValueError(f"unknown benchmark_scale: {scale!r}")
        if not directory or "/" in directory:
            raise ValueError(f"invalid official benchmark directory: {directory!r}")
        identity = (scale, directory)
        if identity in seen:
            raise ValueError(f"duplicate source entry: {scale}/{directory}")
        seen.add(identity)
        if status not in SOURCE_STATUSES:
            raise ValueError(f"unknown source status for {scale}/{directory}: {status}")
        reason = str(raw.get("selection_reason", ""))
        if reason not in SELECTION_REASONS:
            raise ValueError(
                f"unknown input-selection reason for {scale}/{directory}: {reason}")
        if raw.get("canonical_profile") != CANONICAL_PROFILE:
            raise ValueError(f"canonical profile drift for {scale}/{directory}")

        selected = status != "no_qasm_source"
        if selected:
            if not _is_hex(raw.get("upstream_git_blob"), 40):
                raise ValueError(f"invalid upstream Git blob for {scale}/{directory}")
            if not _is_hex(raw.get("source_sha256"), 64):
                raise ValueError(f"invalid source SHA256 for {scale}/{directory}")
            if reason == "no_qasm_source":
                raise ValueError(f"selected source has no-source reason: {scale}/{directory}")
        else:
            if reason != "no_qasm_source":
                raise ValueError(f"no-source entry has a selected-source reason: {scale}/{directory}")
            if raw.get("upstream_git_blob") or raw.get("source_sha256"):
                raise ValueError(f"no-source entry contains source hashes: {scale}/{directory}")

        if status == "success":
            if not _is_hex(raw.get("canonical_sha256"), 64):
                raise ValueError(f"invalid canonical SHA256 for {scale}/{directory}")
            for field in ("qubits", "gates_1q", "gates_2q"):
                value = raw.get(field)
                minimum = 1 if field == "qubits" else 0
                if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
                    raise ValueError(f"invalid {field} for {scale}/{directory}")
        else:
            if raw.get("canonical_sha256"):
                raise ValueError(
                    f"non-success source contains canonical SHA256: {scale}/{directory}")
            if not raw.get("error"):
                raise ValueError(f"non-success source lacks error: {scale}/{directory}")

        normalized = dict(raw)
        normalized["benchmark_scale"] = scale
        normalized["benchmark_directory"] = directory
        normalized_entries.append(normalized)
        statuses[status] += 1
        scale_counts[scale] += 1

    normalized_entries.sort(
        key=lambda item: (SCALES.index(item["benchmark_scale"]),
                          item["benchmark_directory"]))
    counts = payload.get("counts")
    if not isinstance(counts, Mapping):
        raise ValueError("source manifest counts must be an object")
    declared_directories = counts.get("directories")
    if not isinstance(declared_directories, Mapping):
        raise ValueError("source manifest directory counts must be an object")
    declared_scales = {
        scale: int(declared_directories.get(scale, -1)) for scale in SCALES
    }
    observed_scales = {scale: scale_counts[scale] for scale in SCALES}
    if declared_scales != observed_scales:
        raise ValueError(
            f"source directory counts disagree with entries: "
            f"declared={declared_scales}, observed={observed_scales}")
    declared_selected = int(counts.get("selected_qasm", -1))
    declared_no_source = int(counts.get("no_qasm_source", -1))
    if declared_selected != len(entries) - statuses["no_qasm_source"]:
        raise ValueError("selected_qasm count disagrees with source entries")
    if declared_no_source != statuses["no_qasm_source"]:
        raise ValueError("no_qasm_source count disagrees with source entries")
    for status in ("success", "canonical_error"):
        if int(counts.get(status, -1)) != statuses[status]:
            raise ValueError(f"{status} count disagrees with source entries")

    if require_complete:
        if observed_scales != EXPECTED_DIRECTORY_COUNTS:
            raise ValueError(
                f"official QASMBench directory counts must be "
                f"{EXPECTED_DIRECTORY_COUNTS}, got {observed_scales}")
        if declared_selected != EXPECTED_SELECTED_QASM:
            raise ValueError(
                f"official QASMBench selected input count must be "
                f"{EXPECTED_SELECTED_QASM}")
        if declared_no_source != EXPECTED_NO_QASM_SOURCE:
            raise ValueError(
                f"official QASMBench no-source count must be "
                f"{EXPECTED_NO_QASM_SOURCE}")

    normalized_payload = dict(payload)
    normalized_payload["entries"] = normalized_entries
    normalized_payload["counts"] = {
        **dict(counts),
        "directories": observed_scales,
        "selected_qasm": declared_selected,
        "no_qasm_source": declared_no_source,
        "success": statuses["success"],
        "canonical_error": statuses["canonical_error"],
    }
    return normalized_payload


def _manifest_scale(run: Mapping[str, Any]) -> str:
    scale = str(run.get("benchmark_scale", "")).lower()
    if scale in SCALES:
        return scale
    dataset = str(run.get("dataset", "")).lower()
    for prefix in ("qasmbench_", "qasmbench-", "qasmbench/"):
        if dataset.startswith(prefix) and dataset[len(prefix):] in SCALES:
            return dataset[len(prefix):]
    if dataset in SCALES:
        return dataset
    return scale


def _manifest_directory(run: Mapping[str, Any]) -> str:
    value = run.get("benchmark_directory")
    if value is None:
        value = run.get("circuit")
    return str(value or "")


def _read_run_json(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        values = payload
    elif isinstance(payload, Mapping) and isinstance(payload.get("manifests"), list):
        values = payload["manifests"]
    elif isinstance(payload, Mapping):
        values = [payload]
    else:
        raise ValueError(f"unsupported run-manifest JSON shape: {path}")
    if any(not isinstance(value, Mapping) for value in values):
        raise ValueError(f"run-manifest collection contains a non-object: {path}")
    return [dict(value) for value in values]


def load_qasmbench_run_manifests(
        inputs: Iterable[RunInput] | RunInput) -> list[dict[str, Any]]:
    """Load explicit manifests or recursively load ``manifest.json`` files."""
    if isinstance(inputs, (Mapping, str, os.PathLike)):
        materialized: list[RunInput] = [inputs]
    else:
        materialized = list(inputs)
    result: list[dict[str, Any]] = []
    for item in materialized:
        if isinstance(item, Mapping):
            result.append(dict(item))
            continue
        path = Path(item)
        if path.is_dir():
            paths = [
                candidate for candidate in sorted(path.rglob("manifest.json"))
                if not any(part.startswith(".")
                           for part in candidate.relative_to(path).parts)
            ]
            result.extend(run for candidate in paths
                          for run in _read_run_json(candidate))
        elif path.is_file():
            result.extend(_read_run_json(path))
        else:
            raise FileNotFoundError(path)
    return result


def _finite_number(value: object, *, nonnegative: bool = True) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or (nonnegative and number < 0.0):
        return None
    return number


def _first_number(run: Mapping[str, Any], fields: Sequence[str], *,
                  nonnegative: bool = True) -> float | None:
    for field in fields:
        if field in run:
            value = _finite_number(run.get(field), nonnegative=nonnegative)
            if value is not None:
                return value
    return None


def _transfers(run: Mapping[str, Any]) -> float | None:
    value = _first_number(run, ("transfers", "transfer_count"))
    if value is not None:
        return value
    components = run.get("fidelity_components")
    if isinstance(components, Mapping):
        return _finite_number(components.get("transfers"))
    return None


def _seconds(run: Mapping[str, Any], seconds_field: str,
             nanoseconds_field: str) -> float | None:
    direct = _finite_number(run.get(seconds_field))
    if direct is not None:
        return direct
    value = _finite_number(run.get(nanoseconds_field))
    return None if value is None else value / 1_000_000_000.0


def _algorithm_time(run: Mapping[str, Any]) -> float | None:
    direct = _first_number(
        run, ("algorithm_time_s", "compiler_time_s", "full_compile_s"))
    if direct is not None:
        return direct
    value = _first_number(run, ("compiler_time_ns", "full_compile_ns"))
    return None if value is None else value / 1_000_000_000.0


def _ours_timing_value(run: Mapping[str, Any], suffix: str,
                       nanoseconds_field: str) -> float | None:
    value = _seconds(run, suffix, nanoseconds_field)
    if value is None and suffix == "native_search_s":
        # ``search_kernel`` is the older outer search/orchestration counter.
        # It is a conservative compatibility fallback only when the native
        # C++ wall counter was not emitted by an otherwise valid manifest.
        value = _seconds(run, "search_kernel_s", "search_kernel_ns")
    return value


def _log_fidelity(run: Mapping[str, Any]) -> float | None:
    value = _finite_number(run.get("log_fidelity"), nonnegative=False)
    if value is not None:
        return value
    fidelity = _finite_number(run.get("fidelity"))
    if fidelity is None or fidelity <= 0.0:
        return None
    return math.log(fidelity)


def _exp_log(value: float | None) -> float | None:
    if value is None:
        return None
    if value < math.log(float.fromhex("0x0.0000000000001p-1022")):
        return 0.0
    if value > math.log(float.fromhex("0x1.fffffffffffffp+1023")):
        return None
    return math.exp(value)


def _median(values: Iterable[float | None]) -> float | None:
    materialized = list(values)
    if not materialized or any(value is None for value in materialized):
        return None
    return float(statistics.median(value for value in materialized
                                   if value is not None))


def _attempt_order(run: Mapping[str, Any]) -> tuple[int, int]:
    retry = run.get("attempt", run.get("retry_index", 0))
    repetition = run.get("repetition", 0)
    try:
        return int(retry), int(repetition)
    except (TypeError, ValueError) as error:
        raise ValueError("run attempt/repetition must be integers") from error


def _select_latest_attempts(
        runs: Sequence[Mapping[str, Any]],
        source_keys: set[tuple[str, str]]) -> dict[tuple[str, str, str, int], dict[str, Any]]:
    selected: dict[tuple[str, str, str, int], dict[str, Any]] = {}
    orders: dict[tuple[str, str, str, int], tuple[int, int]] = {}
    for raw in runs:
        run = dict(raw)
        scale = _manifest_scale(run)
        directory = _manifest_directory(run)
        method = str(run.get("method", ""))
        try:
            seed = int(run.get("seed", 0))
        except (TypeError, ValueError) as error:
            raise ValueError(f"invalid run seed for {scale}/{directory}/{method}") from error
        if (scale, directory) not in source_keys:
            raise ValueError(
                f"run manifest does not belong to the frozen source inventory: "
                f"{scale}/{directory}")
        if method not in METHODS:
            raise ValueError(f"unknown QASMBench method: {method!r}")
        if seed not in {0, 1, 2}:
            raise ValueError(f"unsupported QASMBench quality seed: {seed}")
        status = str(run.get("status", ""))
        if status not in TERMINAL_RUN_STATUSES:
            raise ValueError(
                f"non-terminal run manifest for {scale}/{directory}/{method}/seed{seed}: "
                f"{status!r}")
        run["benchmark_scale"] = scale
        run["benchmark_directory"] = directory
        key = (scale, directory, method, seed)
        order = _attempt_order(run)
        if key in selected and order == orders[key]:
            raise ValueError(
                f"duplicate terminal run attempt for "
                f"{scale}/{directory}/{method}/seed{seed}: {order}")
        if key not in selected or order > orders[key]:
            selected[key] = run
            orders[key] = order
    return selected


def _success_contract(
        run: Mapping[str, Any], source: Mapping[str, Any], method: str) -> list[str]:
    reasons: list[str] = []
    if run.get("status") != "success":
        return [str(run.get("status") or "missing")]
    if run.get("run_kind") != "qasmbench":
        reasons.append("non_qasmbench_run_kind")
    if run.get("benchmark_scale") != source.get("benchmark_scale"):
        reasons.append("benchmark_scale_mismatch")
    if run.get("benchmark_directory") != source.get("benchmark_directory"):
        reasons.append("benchmark_directory_mismatch")
    if run.get("upstream_git_blob") != source.get("upstream_git_blob"):
        reasons.append("upstream_git_blob_mismatch")
    if run.get("input_selection_reason") != source.get("selection_reason"):
        reasons.append("input_selection_reason_mismatch")
    if run.get("canonical_profile") != CANONICAL_PROFILE:
        reasons.append("canonical_profile_mismatch")
    if run.get("trace_retained") is not False:
        reasons.append("trace_retained")
    implementation = run.get("implementation_status")
    if not implementation:
        reasons.append("implementation_status_missing")
    if implementation == "development_streaming_proxy_v1":
        reasons.append("development_streaming_proxy_forbidden")
    if run.get("rss_limit_bytes") != RSS_LIMIT_BYTES:
        reasons.append("rss_limit_mismatch")
    if run.get("minimum_free_bytes") != MINIMUM_FREE_BYTES:
        reasons.append("minimum_free_space_mismatch")
    expected_concurrency = CONCURRENCY_BY_SCALE[str(source["benchmark_scale"])]
    if run.get("concurrency_limit") != expected_concurrency:
        reasons.append("concurrency_limit_mismatch")
    if not _is_hex(run.get("event_stream_sha256"), 64):
        reasons.append("event_stream_sha256_missing")
    if run.get("event_stream_hash_protocol") != EVENT_HASH_PROTOCOL:
        reasons.append("event_stream_hash_protocol_mismatch")
    if run.get("verifier_ok") is not True:
        reasons.append("verifier_not_ok")
    canonical_hash = source.get("canonical_sha256")
    run_hash = run.get("input_sha256", run.get("canonical_sha256"))
    if not canonical_hash or run_hash != canonical_hash:
        reasons.append("canonical_sha256_mismatch")
    expected_ledger = run.get("expected_gate_ledger_sha256")
    observed_ledger = run.get("observed_gate_ledger_sha256")
    if (not _is_hex(expected_ledger, 64)
            or expected_ledger != observed_ledger):
        reasons.append("gate_ledger_unverified")
    for metric in ("gates_1q", "gates_2q"):
        expected = source.get(metric)
        run_expected = run.get(f"expected_{metric}")
        run_observed = run.get(f"observed_{metric}")
        if expected is None or run_expected != expected or run_observed != expected:
            reasons.append(f"{metric}_mismatch")
    if method in OURS_METHODS:
        if run.get("backend") != "native":
            reasons.append("non_native_backend")
        abi = run.get("native_abi_version")
        if not isinstance(abi, int) or isinstance(abi, bool) or abi <= 0:
            reasons.append("native_abi_missing")
        if run.get("ghost_hits") != 0:
            reasons.append("ghost_hits_nonzero")
        if run.get("python_fallback") is not False:
            reasons.append("python_fallback_not_false")
        if run.get("fallback_used") is True:
            reasons.append("fallback_used")
        protocol = str(run.get("trace_protocol", ""))
        if protocol == "development_streaming_proxy_v1":
            reasons.append("development_streaming_proxy_forbidden")
    return reasons


def _seed_runs(
        selected: Mapping[tuple[str, str, str, int], Mapping[str, Any]],
        scale: str, directory: str, method: str,
        source_status: str) -> tuple[list[Mapping[str, Any]], int, str, list[int]]:
    if source_status != "success":
        return [], 1, "not_applicable", []
    if method in {"M1", "M2"}:
        run = selected.get((scale, directory, method, 0))
        return ([run] if run else []), 1, "seed0", []
    seeds = {
        seed: selected.get((scale, directory, method, seed))
        for seed in (0, 1, 2)
    }
    if all(seeds.values()):
        return [seeds[seed] for seed in (0, 1, 2) if seeds[seed]], 3, \
            "median_seed0_1_2", []
    ignored = [seed for seed in (1, 2) if seeds[seed] is not None]
    return ([seeds[0]] if seeds[0] else []), 1, "interim_seed0", ignored


def _terminal_status(run: Mapping[str, Any] | None,
                     source_status: str) -> str:
    if source_status != "success":
        return source_status
    return str(run.get("status")) if run else "missing"


def _aggregate_method(
        selected: Mapping[tuple[str, str, str, int], Mapping[str, Any]],
        source: Mapping[str, Any], method: str) -> dict[str, Any]:
    scale = str(source["benchmark_scale"])
    directory = str(source["benchmark_directory"])
    source_status = str(source["status"])
    runs, required, seed_policy, ignored = _seed_runs(
        selected, scale, directory, method, source_status)
    seed0 = selected.get((scale, directory, method, 0))
    terminal = _terminal_status(seed0, source_status)
    if source_status != "success":
        return {
            "valid": False,
            "valid_over_N": f"0/{required}",
            "status": source_status,
            "terminal_status": terminal,
            "seed_policy": seed_policy,
            "ignored_partial_seeds": ignored,
            "fidelity": None,
            "log_fidelity": None,
            "fidelity_ood": False,
            "transfers": None,
            "move_batches": None,
            "move_time_ms": None,
            "algorithm_time_s": None,
            "ledger_hashes": [],
            "input_hashes": [],
            "gate_counts": [],
            "validation_errors": [source_status],
            **({suffix: None for suffix, _field in OURS_TIMING_FIELDS}
               if method in OURS_METHODS else {}),
        }

    validations = [
        _success_contract(run, source, method) for run in runs
    ]
    valid_count = sum(not reasons for reasons in validations)
    complete = len(runs) == required and valid_count == required
    errors = sorted({reason for reasons in validations for reason in reasons})
    if len(runs) < required:
        errors.append("missing")
    if ignored:
        errors.append("partial_seed_extension_ignored")
    valid_runs = runs if complete else []
    fidelity_ood = bool(valid_runs) and any(
        bool(run.get("fidelity_ood")) for run in valid_runs)
    log_fidelity = (
        None if fidelity_ood else _median(_log_fidelity(run) for run in valid_runs)
    )
    status = "success" if complete else (
        str(runs[0].get("status")) if len(runs) == 1 else "incomplete")
    result: dict[str, Any] = {
        "valid": complete,
        "valid_over_N": f"{valid_count}/{required}",
        "status": status,
        "terminal_status": terminal,
        "seed_policy": seed_policy,
        "ignored_partial_seeds": ignored,
        "fidelity": _exp_log(log_fidelity),
        "log_fidelity": log_fidelity,
        "fidelity_ood": fidelity_ood,
        "transfers": _median(_transfers(run) for run in valid_runs),
        "move_batches": _median(
            _first_number(run, ("move_batches",)) for run in valid_runs),
        "move_time_ms": _median(
            (None if (value := _first_number(run, ("move_time_us",))) is None
             else value / 1_000.0)
            for run in valid_runs),
        "algorithm_time_s": _median(_algorithm_time(run) for run in valid_runs),
        "ledger_hashes": sorted({
            str(run.get("expected_gate_ledger_sha256")) for run in valid_runs
        }),
        "input_hashes": sorted({
            str(run.get("input_sha256", run.get("canonical_sha256", "")))
            for run in valid_runs
        }),
        "gate_counts": sorted({
            (run.get("expected_gates_1q"), run.get("expected_gates_2q"))
            for run in valid_runs
        }),
        "validation_errors": errors,
    }
    if method in OURS_METHODS:
        for suffix, field in OURS_TIMING_FIELDS:
            result[suffix] = _median(
                _ours_timing_value(run, suffix, field) for run in valid_runs)
    return result


def _pairing_reason(method_results: Mapping[str, Mapping[str, Any]],
                    source: Mapping[str, Any]) -> tuple[bool, str]:
    if source.get("status") != "success":
        return False, str(source.get("status"))
    invalid = [method for method in METHODS if not method_results[method]["valid"]]
    if invalid:
        return False, "invalid_or_missing:" + ",".join(invalid)
    input_hashes = {
        value for method in METHODS
        for value in method_results[method]["input_hashes"]
    }
    if input_hashes != {source.get("canonical_sha256")}:
        return False, "canonical_sha256_mismatch"
    ledger_hashes = {
        value for method in METHODS
        for value in method_results[method]["ledger_hashes"]
    }
    if len(ledger_hashes) != 1:
        return False, "cross_method_gate_ledger_mismatch"
    gate_counts = {
        value for method in METHODS
        for value in method_results[method]["gate_counts"]
    }
    expected_counts = {(source.get("gates_1q"), source.get("gates_2q"))}
    if gate_counts != expected_counts:
        return False, "cross_method_gate_count_mismatch"
    return True, "paired"


def _detail_columns() -> list[str]:
    columns = list(DETAIL_BASE_COLUMNS)
    for method in METHODS:
        columns.extend(f"{method}__{suffix}" for suffix in METHOD_COLUMNS)
        if method in OURS_METHODS:
            columns.extend(f"{method}__{suffix}"
                           for suffix, _field in OURS_TIMING_FIELDS)
    return columns


def _detail_row(source: Mapping[str, Any],
                selected: Mapping[tuple[str, str, str, int], Mapping[str, Any]]) -> dict[str, Any]:
    method_results = {
        method: _aggregate_method(selected, source, method) for method in METHODS
    }
    strict, reason = _pairing_reason(method_results, source)
    fidelity_paired = strict and all(
        not method_results[method]["fidelity_ood"]
        and method_results[method]["log_fidelity"] is not None
        for method in METHODS
    )
    row: dict[str, Any] = {
        "benchmark_scale": source["benchmark_scale"],
        "benchmark_directory": source["benchmark_directory"],
        "upstream_path": source.get("upstream_path", ""),
        "upstream_git_blob": source.get("upstream_git_blob", ""),
        "source_sha256": source.get("source_sha256", ""),
        "selection_reason": source.get("selection_reason", ""),
        "canonical_profile": source.get("canonical_profile", ""),
        "canonical_status": source.get("status", ""),
        "canonical_error": source.get("error"),
        "canonical_sha256": source.get("canonical_sha256", ""),
        "qubits": source.get("qubits"),
        "gates_1q": source.get("gates_1q"),
        "gates_2q": source.get("gates_2q"),
        "strict_paired": strict,
        "pairing_reason": reason,
        "fidelity_paired": fidelity_paired,
        "_methods": method_results,
    }
    for method, result in method_results.items():
        for suffix in METHOD_COLUMNS:
            row[f"{method}__{suffix}"] = result.get(suffix)
        if method in OURS_METHODS:
            for suffix, _field in OURS_TIMING_FIELDS:
                row[f"{method}__{suffix}"] = result.get(suffix)
    return row


def _compare(left: float, right: float, *, higher_is_better: bool) -> str:
    tolerance = 1e-12 * max(1.0, abs(left), abs(right))
    if abs(left - right) <= tolerance:
        return "tie"
    better = left > right if higher_is_better else left < right
    return "win" if better else "loss"


def _wtl(values: Iterable[tuple[float, float]], *,
         higher_is_better: bool) -> dict[str, int]:
    counts = Counter(
        _compare(method, baseline, higher_is_better=higher_is_better)
        for method, baseline in values
    )
    return {name: counts[name] for name in ("win", "tie", "loss")}


def _mean(values: Iterable[float]) -> float | None:
    materialized = list(values)
    return statistics.fmean(materialized) if materialized else None


def _lower_metric_summary(rows: Sequence[Mapping[str, Any]], method: str,
                          metric: str, *, baseline: str) -> dict[str, Any]:
    pairs: list[tuple[float, float]] = []
    for row in rows:
        if not row["strict_paired"]:
            continue
        ours = row["_methods"][method].get(metric)
        if baseline == "Bmin":
            first = row["_methods"]["M1"].get(metric)
            second = row["_methods"]["M2"].get(metric)
            base = min(first, second) if first is not None and second is not None else None
        elif baseline == "M2":
            base = row["_methods"]["M2"].get(metric)
        else:
            raise ValueError(f"unsupported lower-is-better baseline: {baseline}")
        if ours is not None and base is not None:
            pairs.append((float(ours), float(base)))
    ours_mean = _mean(ours for ours, _base in pairs)
    baseline_mean = _mean(base for _ours, base in pairs)
    if baseline_mean is None or ours_mean is None:
        reduction = None
        speedup = None
    elif baseline_mean == 0.0:
        reduction = 0.0 if ours_mean == 0.0 else None
        speedup = 1.0 if ours_mean == 0.0 else None
    else:
        reduction = (baseline_mean - ours_mean) / baseline_mean
        speedup = (baseline_mean / ours_mean
                   if ours_mean > 0.0 else (None if baseline_mean == 0.0 else None))
    counts = _wtl(pairs, higher_is_better=False)
    return {
        "paired_count": len(pairs),
        "baseline": baseline,
        "baseline_mean": baseline_mean,
        "method_mean": ours_mean,
        "reduction_ratio": reduction,
        "speedup_ratio": speedup,
        "wins": counts["win"],
        "ties": counts["tie"],
        "losses": counts["loss"],
    }


def _fidelity_summary(rows: Sequence[Mapping[str, Any]], method: str) -> dict[str, Any]:
    pairs: list[tuple[float, float]] = []
    for row in rows:
        if not row["fidelity_paired"]:
            continue
        ours = float(row["_methods"][method]["log_fidelity"])
        baseline = max(
            float(row["_methods"]["M1"]["log_fidelity"]),
            float(row["_methods"]["M2"]["log_fidelity"]),
        )
        pairs.append((ours, baseline))
    method_log_mean = _mean(ours for ours, _baseline in pairs)
    baseline_log_mean = _mean(baseline for _ours, baseline in pairs)
    log_ratio = (
        None if method_log_mean is None or baseline_log_mean is None
        else method_log_mean - baseline_log_mean
    )
    ratio = _exp_log(log_ratio)
    counts = _wtl(pairs, higher_is_better=True)
    return {
        "paired_count": len(pairs),
        "baseline": "Bstar=max(M1,M2) per circuit",
        "baseline_log_geometric_mean": baseline_log_mean,
        "method_log_geometric_mean": method_log_mean,
        "baseline_geometric_mean": _exp_log(baseline_log_mean),
        "method_geometric_mean": _exp_log(method_log_mean),
        "geometric_log_ratio": log_ratio,
        "geometric_ratio": ratio,
        "gain_ratio": None if ratio is None else ratio - 1.0,
        "wins": counts["win"],
        "ties": counts["tie"],
        "losses": counts["loss"],
    }


def _coverage_bucket(status: str) -> str:
    if status in {"success", "timeout", "oom", "no_qasm_source", "missing"}:
        return status
    return "error"


def _coverage(rows: Sequence[Mapping[str, Any]], method: str) -> dict[str, Any]:
    exact = Counter(str(row["_methods"][method]["terminal_status"]) for row in rows)
    buckets = Counter(_coverage_bucket(status) for status, count in exact.items()
                      for _unused in range(count))
    verified = sum(bool(row["_methods"][method]["valid"]) for row in rows)
    selected_qasm = sum(row["canonical_status"] != "no_qasm_source" for row in rows)
    return {
        "N": len(rows),
        "selected_qasm": selected_qasm,
        "terminal_success": exact["success"],
        "verified_success": verified,
        "success_rate_official": (exact["success"] / len(rows) if rows else None),
        "success_rate_selected_qasm": (
            exact["success"] / selected_qasm if selected_qasm else None),
        "timeout": buckets["timeout"],
        "oom": buckets["oom"],
        "error": buckets["error"],
        "missing": buckets["missing"],
        "no_qasm_source": buckets["no_qasm_source"],
        "exact_status_counts": dict(sorted(exact.items())),
    }


def _method_paired_means(rows: Sequence[Mapping[str, Any]],
                         method: str) -> dict[str, Any]:
    fidelity_logs = [
        float(row["_methods"][method]["log_fidelity"])
        for row in rows if row["fidelity_paired"]
    ]
    log_mean = _mean(fidelity_logs)
    result: dict[str, Any] = {
        "fidelity_count": len(fidelity_logs),
        "fidelity_log_geometric_mean": log_mean,
        "fidelity_geometric_mean": _exp_log(log_mean),
    }
    for metric in (
            "transfers", "move_batches", "move_time_ms", "algorithm_time_s"):
        values = [
            float(row["_methods"][method][metric])
            for row in rows
            if row["strict_paired"]
            and row["_methods"][method].get(metric) is not None
        ]
        result[f"{metric}_count"] = len(values)
        result[f"{metric}_mean"] = _mean(values)
    return result


def _scale_summary(scale: str, rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    strict_count = sum(bool(row["strict_paired"]) for row in rows)
    fidelity_count = sum(bool(row["fidelity_paired"]) for row in rows)
    payload: dict[str, Any] = {
        "benchmark_scale": scale,
        "official_directories": len(rows),
        "selected_qasm": sum(
            row["canonical_status"] != "no_qasm_source" for row in rows),
        "canonical_success": sum(
            row["canonical_status"] == "success" for row in rows),
        "no_qasm_source": sum(
            row["canonical_status"] == "no_qasm_source" for row in rows),
        "canonical_error": sum(
            row["canonical_status"] == "canonical_error" for row in rows),
        "strict_paired_count": strict_count,
        "fidelity_paired_count": fidelity_count,
        "methods": {},
    }
    for method in METHODS:
        method_payload: dict[str, Any] = {
            "coverage": _coverage(rows, method),
            "paired_means": _method_paired_means(rows, method),
        }
        if method in OURS_METHODS:
            method_payload.update({
                "fidelity_vs_Bstar": _fidelity_summary(rows, method),
                "transfers_vs_Bmin": _lower_metric_summary(
                    rows, method, "transfers", baseline="Bmin"),
                "move_batches_vs_Bmin": _lower_metric_summary(
                    rows, method, "move_batches", baseline="Bmin"),
                "move_time_ms_vs_Bmin": _lower_metric_summary(
                    rows, method, "move_time_ms", baseline="Bmin"),
                "algorithm_time_s_vs_M2": _lower_metric_summary(
                    rows, method, "algorithm_time_s", baseline="M2"),
            })
        payload["methods"][method] = method_payload
    return payload


def _summary_rows(summaries: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for scale in SCALES:
        summary = summaries[scale]
        for method in METHODS:
            coverage = summary["methods"][method]["coverage"]
            paired = summary["methods"][method]["paired_means"]
            row: dict[str, Any] = {
                "benchmark_scale": scale,
                "method": method,
                "official_directories": summary["official_directories"],
                "selected_qasm": summary["selected_qasm"],
                "canonical_success": summary["canonical_success"],
                "no_qasm_source": summary["no_qasm_source"],
                "canonical_error": summary["canonical_error"],
                "terminal_success": coverage["terminal_success"],
                "verified_success": coverage["verified_success"],
                "timeout": coverage["timeout"],
                "oom": coverage["oom"],
                "error": coverage["error"],
                "missing": coverage["missing"],
                "success_rate_selected_qasm": coverage[
                    "success_rate_selected_qasm"],
                "strict_paired_count": summary["strict_paired_count"],
                "fidelity_paired_count": summary["fidelity_paired_count"],
                "paired_fidelity_log_geomean": paired[
                    "fidelity_log_geometric_mean"],
                "paired_fidelity_geomean": paired[
                    "fidelity_geometric_mean"],
                "paired_transfers_mean": paired["transfers_mean"],
                "paired_move_batches_mean": paired["move_batches_mean"],
                "paired_move_time_ms_mean": paired["move_time_ms_mean"],
                "paired_algorithm_time_s_mean": paired[
                    "algorithm_time_s_mean"],
            }
            for column in SUMMARY_COLUMNS:
                row.setdefault(column, None)
            if method in OURS_METHODS:
                ours = summary["methods"][method]
                fidelity = ours["fidelity_vs_Bstar"]
                row.update({
                    "fidelity_baseline_log_geomean": fidelity[
                        "baseline_log_geometric_mean"],
                    "fidelity_method_log_geomean": fidelity[
                        "method_log_geometric_mean"],
                    "fidelity_geometric_ratio_vs_Bstar": fidelity[
                        "geometric_ratio"],
                    "fidelity_gain_pct": (
                        None if fidelity["gain_ratio"] is None
                        else 100.0 * fidelity["gain_ratio"]),
                    "fidelity_wins": fidelity["wins"],
                    "fidelity_ties": fidelity["ties"],
                    "fidelity_losses": fidelity["losses"],
                })
                for prefix, key in (
                    ("transfers", "transfers_vs_Bmin"),
                    ("move_batches", "move_batches_vs_Bmin"),
                    ("move_time_ms", "move_time_ms_vs_Bmin"),
                    ("algorithm_time_s", "algorithm_time_s_vs_M2"),
                ):
                    comparison = ours[key]
                    row.update({
                        f"{prefix}_paired_count": comparison["paired_count"],
                        f"{prefix}_baseline_mean": comparison["baseline_mean"],
                        f"{prefix}_method_mean": comparison["method_mean"],
                        f"{prefix}_reduction_pct": (
                            None if comparison["reduction_ratio"] is None
                            else 100.0 * comparison["reduction_ratio"]),
                        f"{prefix}_wins": comparison["wins"],
                        f"{prefix}_ties": comparison["ties"],
                        f"{prefix}_losses": comparison["losses"],
                    })
                row["algorithm_time_s_speedup_vs_M2"] = ours[
                    "algorithm_time_s_vs_M2"]["speedup_ratio"]
            rows.append(row)
    return rows


def _public_detail_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {column: row.get(column) for column in _detail_columns()}


def aggregate_qasmbench(
        source_manifest: JsonInput,
        run_manifests: Iterable[RunInput] | RunInput,
        *, require_complete_source: bool = True) -> dict[str, Any]:
    """Build Summary/Small/Medium/Large data without writing files."""
    source = validate_qasmbench_source_manifest(
        source_manifest, require_complete=require_complete_source)
    if not isinstance(source_manifest, Mapping):
        source_root = Path(source_manifest).resolve().parent
        normalized_entries = []
        for entry in _source_entries(source):
            normalized = dict(entry)
            canonical_path = normalized.get("canonical_path")
            if canonical_path and not Path(str(canonical_path)).is_absolute():
                normalized["canonical_path"] = str(
                    (source_root / str(canonical_path)).resolve())
            normalized_entries.append(normalized)
        source = {**source, "entries": normalized_entries}
    entries = _source_entries(source)
    source_keys = {
        (str(entry["benchmark_scale"]), str(entry["benchmark_directory"]))
        for entry in entries
    }
    selected = _select_latest_attempts(
        load_qasmbench_run_manifests(run_manifests), source_keys)
    internal_rows: dict[str, list[dict[str, Any]]] = {scale: [] for scale in SCALES}
    for entry in entries:
        internal_rows[str(entry["benchmark_scale"])].append(
            _detail_row(entry, selected))
    summaries = {
        scale: _scale_summary(scale, internal_rows[scale]) for scale in SCALES
    }
    summary_rows = _summary_rows(summaries)
    sheets = {
        "Summary": summary_rows,
        **{
            SHEET_BY_SCALE[scale]: [
                _public_detail_row(row) for row in internal_rows[scale]
            ]
            for scale in SCALES
        },
    }
    return {
        "aggregate_schema": AGGREGATE_SCHEMA,
        "source_manifest": source,
        "sheet_names": list(SHEET_NAMES),
        "exact_sheet_count": 4,
        "columns": {
            "Summary": list(SUMMARY_COLUMNS),
            "Small": _detail_columns(),
            "Medium": _detail_columns(),
            "Large": _detail_columns(),
        },
        "sheets": sheets,
        "summary": summaries,
        "notes": {
            "strict_pairing": (
                "all four methods verified-success on one canonical SHA256, "
                "identical 1Q/2Q counts and one gate-ledger SHA256"),
            "ours_seed_policy": (
                "median seeds 0/1/2 when all three terminal manifests exist; "
                "otherwise explicit interim seed0"),
            "fidelity": (
                "per-circuit Bstar=max(M1,M2), log-domain geometric ratio; "
                "any four-method linear-model OOD or missing logF is excluded"),
            "move": (
                "per-circuit baseline=min(M1,M2); arithmetic means and "
                "mean reduction; zero is retained and None is omitted"),
            "algorithm_time": (
                "implementation-level compiler time relative to M2; "
                "arithmetic means"),
        },
    }


def _final_manifest_payload(output: Path, payload: Mapping[str, Any],
                            files: Mapping[str, str]) -> dict[str, Any]:
    source = payload["source_manifest"]
    return {
        "manifest_schema": FINAL_PAYLOAD_SCHEMA,
        "delivery_id": "qasmbench-four-method-small-medium-large-v1",
        "state": "xlsx_pending",
        "expected_workbook": "qasmbench_four_methods.xlsx",
        "sheet_names": list(SHEET_NAMES),
        "exact_sheet_count": 4,
        "charts": False,
        "trace_retained": False,
        "upstream_commit": source["upstream_commit"],
        "canonical_profile": source["canonical_profile"],
        "row_counts": {
            scale: len(payload["sheets"][SHEET_BY_SCALE[scale]])
            for scale in SCALES
        },
        "strict_paired_counts": {
            scale: payload["summary"][scale]["strict_paired_count"]
            for scale in SCALES
        },
        "fidelity_paired_counts": {
            scale: payload["summary"][scale]["fidelity_paired_count"]
            for scale in SCALES
        },
        "output_directory": str(output.resolve()),
        "files": dict(sorted(files.items())),
    }


def write_qasmbench_aggregation(payload: Mapping[str, Any],
                                output: str | os.PathLike[str]) -> dict[str, Any]:
    """Write JSON/CSV evidence and return an XLSX-finalization payload."""
    if payload.get("aggregate_schema") != AGGREGATE_SCHEMA:
        raise ValueError("wrong QASMBench aggregate schema")
    if payload.get("sheet_names") != list(SHEET_NAMES):
        raise ValueError("QASMBench aggregate has the wrong logical sheets")
    output_path = Path(output)
    output_path.mkdir(parents=True, exist_ok=True)

    for scale in SCALES:
        _atomic_csv(
            output_path / f"{scale}.csv",
            payload["columns"][SHEET_BY_SCALE[scale]],
            payload["sheets"][SHEET_BY_SCALE[scale]],
        )
    _atomic_csv(
        output_path / "summary.csv",
        payload["columns"]["Summary"],
        payload["sheets"]["Summary"],
    )
    _atomic_json(output_path / "source_manifest.json", payload["source_manifest"])
    aggregate_payload = {
        key: value for key, value in payload.items() if key != "source_manifest"
    }
    _atomic_json(output_path / "aggregate_summary.json", aggregate_payload)

    written = (
        "small.csv", "medium.csv", "large.csv", "summary.csv",
        "source_manifest.json", "aggregate_summary.json",
    )
    files = {name: _sha256(output_path / name) for name in written}
    final_payload = _final_manifest_payload(output_path, payload, files)
    _atomic_json(output_path / "final_manifest_payload.json", final_payload)
    return final_payload
