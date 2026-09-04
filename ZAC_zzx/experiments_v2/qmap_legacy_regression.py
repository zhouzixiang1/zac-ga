"""Freeze and check the historical QMAP M4 quality regression target.

The August 2026 pilot workbook contains 154 QMAP rows, but the published
geometric mean used the 126 circuits for which all four linear-fidelity values
are positive and finite.  This helper freezes that exact cohort and refuses to
compare a new partial or differently hashed cohort as if it were complete.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


METHOD_LABELS = {
    "M1": "M1 ZAC",
    "M2": "M2 ICCAD/QMAP A*",
    "M3": "M3 Ours-NL (H=0)",
    "M4": "M4 Ours-LK (H=2)",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _positive_finite(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value) > 0.0
    )


def _geometric_mean(values: Iterable[float]) -> float:
    numbers = [float(value) for value in values]
    if not numbers or not all(_positive_finite(value) for value in numbers):
        raise ValueError("geometric mean requires positive finite values")
    return math.exp(math.fsum(math.log(value) for value in numbers)
                    / len(numbers))


def _column(metric: str, method: str) -> str:
    label = METHOD_LABELS[method]
    if metric == "fidelity":
        return f"保真度F | {label}"
    if metric == "move_batches":
        return f"Move批次 | {label}"
    if metric == "move_time_ms":
        return f"Move时间(ms) | {label}"
    if metric == "algorithm_runtime_s":
        return f"算法时间(s) | {label}"
    raise KeyError(metric)


def freeze_reference(rows_json: Path, aggregate_json: Path) -> dict[str, Any]:
    extracted = json.loads(rows_json.read_text(encoding="utf-8"))
    values = extracted["QMAP"]["values"]
    header = values[0]
    workbook_rows = {
        str(row[0]): dict(zip(header, row)) for row in values[1:]
    }
    if len(workbook_rows) != 154:
        raise ValueError(
            f"historical QMAP workbook must contain 154 circuits, "
            f"found {len(workbook_rows)}")

    aggregate = json.loads(aggregate_json.read_text(encoding="utf-8"))
    attempts: dict[tuple[str, str], Mapping[str, Any]] = {}
    for row in aggregate["attempt_index"]:
        key = (str(row["circuit"]), str(row["method"]))
        if key in attempts:
            raise ValueError(f"duplicate legacy attempt {key!r}")
        attempts[key] = row

    cohort = []
    for circuit in sorted(workbook_rows):
        row = workbook_rows[circuit]
        fidelities = {
            method: row[_column("fidelity", method)]
            for method in METHOD_LABELS
        }
        if not all(_positive_finite(value) for value in fidelities.values()):
            continue
        method_rows = {}
        input_hashes = set()
        for method in METHOD_LABELS:
            attempt = attempts[(circuit, method)]
            input_hashes.add(str(attempt["input_sha256"]))
            method_rows[method] = {
                "fidelity": float(fidelities[method]),
                "move_batches": int(row[_column("move_batches", method)]),
                "move_time_ms": float(row[_column("move_time_ms", method)]),
                "algorithm_runtime_s": float(
                    row[_column("algorithm_runtime_s", method)]),
                "config_sha256": str(attempt["config_sha256"]),
            }
        if len(input_hashes) != 1:
            raise ValueError(f"legacy input hashes disagree for {circuit!r}")
        cohort.append({
            "circuit": circuit,
            "input_sha256": input_hashes.pop(),
            "methods": method_rows,
        })

    if len(cohort) != 126:
        raise ValueError(
            f"historical common finite QMAP cohort must contain 126 circuits, "
            f"found {len(cohort)}")

    fidelity_gm = {
        method: _geometric_mean(
            row["methods"][method]["fidelity"] for row in cohort)
        for method in METHOD_LABELS
    }
    all_suite_arithmetic_means = {
        metric: {
            method: math.fsum(
                float(row[_column(metric, method)])
                for row in workbook_rows.values())
            / len(workbook_rows)
            for method in METHOD_LABELS
        }
        for metric in (
            "move_batches", "move_time_ms", "algorithm_runtime_s")
    }
    per_circuit_bstar_ratio = _geometric_mean(
        row["methods"]["M4"]["fidelity"]
        / max(row["methods"]["M1"]["fidelity"],
              row["methods"]["M2"]["fidelity"])
        for row in cohort
    )
    strongest_aggregate_baseline = max(fidelity_gm["M1"], fidelity_gm["M2"])
    m4_config_hashes = sorted({
        row["methods"]["M4"]["config_sha256"] for row in cohort
    })
    if len(m4_config_hashes) != 1:
        raise ValueError("legacy M4 cohort contains multiple resolved configs")

    return {
        "schema": "qmap-m4-legacy-regression-v1",
        "dataset": "QMAP154",
        "legacy_git_commit": (
            "1efee0b607cf2108d00e6fbecbfb75c3bdad9a15"),
        "legacy_method": "Python resident GA with fixed lookahead_horizon=2",
        "legacy_m4_config_sha256": m4_config_hashes[0],
        "cohort_definition": (
            "the 126 workbook circuits with positive finite M1/M2/M3/M4 "
            "linear-model fidelity"),
        "cohort_n": len(cohort),
        "suite_n": len(workbook_rows),
        "source_sha256": {
            "rows_json": _sha256(rows_json),
            "aggregate_json": _sha256(aggregate_json),
        },
        "targets": {
            "fidelity_geometric_mean": fidelity_gm,
            "strongest_aggregate_baseline_fidelity": (
                strongest_aggregate_baseline),
            "m4_vs_strongest_aggregate_baseline_ratio": (
                fidelity_gm["M4"] / strongest_aggregate_baseline),
            "m4_vs_per_circuit_bstar_gm_ratio": per_circuit_bstar_ratio,
            "m4_vs_m3_gm_ratio": fidelity_gm["M4"] / fidelity_gm["M3"],
            "all_154_arithmetic_mean": all_suite_arithmetic_means,
        },
        "primary_gate": {
            "definition": (
                "new M4 must complete the exact 126-circuit cohort with "
                "matching input hashes and strictly exceed the legacy M4 "
                "fidelity geometric mean"),
            "minimum_exclusive": fidelity_gm["M4"],
        },
        "circuits": cohort,
    }


def _manifest_timestamp(manifest: Mapping[str, Any], path: Path) -> tuple[int, int, str]:
    """Return a deterministic newest-first key for an attempt manifest."""
    for field in ("ended_at_utc", "started_at_utc"):
        raw = manifest.get(field)
        if not raw:
            continue
        try:
            parsed = dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            return (int(parsed.timestamp() * 1_000_000_000),
                    path.stat().st_mtime_ns, str(path))
        except (OSError, ValueError):
            pass
    try:
        modified = path.stat().st_mtime_ns
    except OSError:
        modified = 0
    return (modified, modified, str(path))


def _attempt_integrity_issues(manifest: Mapping[str, Any],
                              expected_input_sha256: str) -> list[str]:
    """Return reasons a successful M4 attempt is not formal evidence."""
    issues = []
    if str(manifest.get("input_sha256", "")) != expected_input_sha256:
        issues.append("input_sha256_mismatch")
    if manifest.get("fidelity_ood") is True:
        issues.append("linear_fidelity_ood")
    if manifest.get("backend") != "native":
        issues.append("backend_not_native")
    if manifest.get("ghost_hits") != 0:
        issues.append("ghost_hits_not_zero")
    if manifest.get("verifier_ok") is not True:
        issues.append("verifier_not_ok")
    expected_gate = str(manifest.get("expected_gate_ledger_sha256", ""))
    observed_gate = str(manifest.get("observed_gate_ledger_sha256", ""))
    if not expected_gate or not observed_gate or expected_gate != observed_gate:
        issues.append("gate_ledger_unproven_or_mismatch")
    canonical_layer = str(
        manifest.get("canonical_input_layer_ledger_sha256", ""))
    observed_layer = str(
        manifest.get("observed_transition_layer_ledger_sha256", ""))
    if (not canonical_layer or not observed_layer
            or canonical_layer != observed_layer):
        issues.append("transition_layer_ledger_unproven_or_mismatch")
    return issues


def _manifest_paths(roots: Sequence[Path]) -> list[Path]:
    paths: set[Path] = set()
    for root in roots:
        root = root.expanduser().resolve()
        if root.is_file():
            if root.name == "manifest.json":
                paths.add(root)
            continue
        if root.is_dir():
            paths.update(path.resolve() for path in root.rglob("manifest.json"))
    return sorted(paths, key=str)


def collect_current_report(
    reference: Mapping[str, Any],
    attempt_roots: Sequence[Path],
    *,
    selection: str = "best",
    formal: bool = False,
) -> dict[str, Any]:
    """Collect M4 seed-0 manifests for the frozen 126-circuit cohort.

    ``best`` is intentionally an exploratory, per-circuit upper envelope and
    may mix revisions/configurations.  ``latest`` selects the newest manifest.
    Both modes expose that identity mixing.  ``formal=True`` does not hide or
    silently repair a mixed cohort: it marks the report ineligible unless all
    selected successful rows share one git commit/config/backend and satisfy
    the native, ghost, verifier, and ledger contracts.
    """
    if selection not in {"best", "latest"}:
        raise ValueError("selection must be 'best' or 'latest'")
    expected = {
        str(row["circuit"]): str(row["input_sha256"])
        for row in reference["circuits"]
    }
    candidates: dict[str, list[dict[str, Any]]] = {
        circuit: [] for circuit in expected
    }
    scan_errors = []
    paths = _manifest_paths(attempt_roots)
    for path in paths:
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            scan_errors.append({"path": str(path), "error": str(exc)})
            continue
        circuit = str(manifest.get("circuit", ""))
        try:
            seed = int(manifest.get("seed", -1))
            repetition = int(manifest.get("repetition", 0))
        except (TypeError, ValueError):
            continue
        if (circuit not in expected or manifest.get("method") != "M4"
                or seed != 0 or repetition != 0
                or str(manifest.get("dataset", "")).lower() != "qmap154"):
            continue
        manifest = dict(manifest)
        manifest["_source_manifest"] = str(path)
        manifest["_selection_timestamp"] = _manifest_timestamp(manifest, path)
        manifest["_integrity_issues"] = _attempt_integrity_issues(
            manifest, expected[circuit])
        candidates[circuit].append(manifest)

    def selected(candidate_rows: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
        if not candidate_rows:
            return None
        if selection == "latest":
            return max(candidate_rows, key=lambda row: row["_selection_timestamp"])

        def best_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
            fidelity = row.get("fidelity")
            success = row.get("status") == "success" and _positive_finite(fidelity)
            # In formal mode prefer a contract-valid success over an invalid
            # numerically larger result.  Cross-circuit identity is audited
            # after selection and is never silently stitched together.
            integrity = success and not row["_integrity_issues"]
            return (int(integrity) if formal else int(success), int(success),
                    float(fidelity) if success else -math.inf,
                    row["_selection_timestamp"])

        return max(candidate_rows, key=best_key)

    circuit_rows = []
    selected_successes = []
    for old_row in reference["circuits"]:
        circuit = str(old_row["circuit"])
        row = selected(candidates[circuit])
        if row is None:
            circuit_rows.append({
                "circuit": circuit,
                "input_sha256": old_row["input_sha256"],
                "methods": {"M4": {"status": "missing", "fidelity": None}},
                "candidate_n": 0,
            })
            continue
        success = (row.get("status") == "success"
                   and _positive_finite(row.get("fidelity")))
        if success:
            selected_successes.append(row)
        circuit_rows.append({
            "circuit": circuit,
            "input_sha256": str(row.get("input_sha256", "")),
            "candidate_n": len(candidates[circuit]),
            "methods": {"M4": {
                "status": str(row.get("status", "")),
                "fidelity": row.get("fidelity"),
                "log_fidelity": row.get("log_fidelity"),
                "git_commit": str(row.get("git_commit", "")),
                "config_sha256": str(row.get("config_sha256", "")),
                "backend": str(row.get("backend", "")),
                "native_abi_version": row.get("native_abi_version"),
                "ghost_hits": row.get("ghost_hits"),
                "verifier_ok": row.get("verifier_ok"),
                "integrity_ok": not row["_integrity_issues"],
                "integrity_issues": list(row["_integrity_issues"]),
                "source_manifest": row["_source_manifest"],
            }},
        })

    identity_fields = ("git_commit", "config_sha256", "backend")
    identity_values = {
        field: sorted({str(row.get(field, "")) for row in selected_successes})
        for field in identity_fields
    }
    mixed_identity_fields = [
        field for field, values in identity_values.items() if len(values) > 1
    ]
    formal_issues = []
    if (len(identity_values["git_commit"]) != 1
            or identity_values["git_commit"][0] in {"", "unknown"}):
        formal_issues.append("selected successes do not share one git_commit")
    if (len(identity_values["config_sha256"]) != 1
            or not identity_values["config_sha256"][0]):
        formal_issues.append("selected successes do not share one config_sha256")
    if identity_values["backend"] != ["native"]:
        formal_issues.append("selected successes do not all use backend=native")
    invalid_rows = [
        {
            "circuit": row["circuit"],
            "issues": row["methods"]["M4"].get("integrity_issues", []),
        }
        for row in circuit_rows
        if (row["methods"]["M4"].get("status") == "success"
            and row["methods"]["M4"].get("integrity_issues"))
    ]
    if invalid_rows:
        formal_issues.append(
            f"{len(invalid_rows)} selected successes violate native/ghost/"
            "verifier/ledger/input integrity")
    formal_eligible = not formal_issues
    return {
        "schema": "qmap-m4-current-manifest-collection-v1",
        "dataset": str(reference["dataset"]),
        "selection": selection,
        "formal_requested": bool(formal),
        "formal_eligible": formal_eligible,
        "formal_issues": formal_issues,
        "attempt_roots": [str(path.expanduser().resolve())
                          for path in attempt_roots],
        "scanned_manifest_n": len(paths),
        "candidate_manifest_n": sum(len(rows) for rows in candidates.values()),
        "selected_manifest_n": sum(bool(rows) for rows in candidates.values()),
        "selected_success_n": len(selected_successes),
        "cohort_n": int(reference["cohort_n"]),
        "mixed_identity": bool(mixed_identity_fields),
        "mixed_identity_fields": mixed_identity_fields,
        "identity_values": identity_values,
        "invalid_success_rows": invalid_rows,
        "scan_errors": scan_errors,
        "circuit_rows": circuit_rows,
    }


def check_current(reference: Mapping[str, Any],
                  current_report: Mapping[str, Any]) -> dict[str, Any]:
    current_rows = {
        str(row["circuit"]): row for row in current_report["circuit_rows"]
    }
    completed = []
    missing = []
    hash_mismatches = []
    for old_row in reference["circuits"]:
        circuit = str(old_row["circuit"])
        current = current_rows.get(circuit)
        method = None if current is None else current.get("methods", {}).get("M4")
        if (method is None or method.get("status") != "success"
                or not _positive_finite(method.get("fidelity"))):
            missing.append(circuit)
            continue
        if current.get("input_sha256") != old_row["input_sha256"]:
            hash_mismatches.append({
                "circuit": circuit,
                "legacy": old_row["input_sha256"],
                "current": current.get("input_sha256"),
            })
            continue
        completed.append((old_row, method))

    current_values = [float(method["fidelity"]) for _old, method in completed]
    legacy_values = [
        float(old["methods"]["M4"]["fidelity"])
        for old, _method in completed
    ]
    current_gm = _geometric_mean(current_values) if current_values else None
    legacy_same_gm = _geometric_mean(legacy_values) if legacy_values else None
    same_cohort_ratio = (
        current_gm / legacy_same_gm if current_gm is not None else None)
    cumulative_log_headroom = (
        math.fsum(math.log(current / legacy)
                  for current, legacy in zip(current_values, legacy_values))
        if current_values else 0.0)
    remaining = int(reference["cohort_n"]) - len(completed)
    required_remaining_ratio = (
        math.exp(-cumulative_log_headroom / remaining)
        if remaining else None)
    full_current_gm = current_gm if remaining == 0 else None
    threshold = float(reference["primary_gate"]["minimum_exclusive"])
    formal_requested = bool(current_report.get("formal_requested", False))
    formal_eligible = bool(current_report.get("formal_eligible", True))
    passed = bool(
        remaining == 0
        and not hash_mismatches
        and (not formal_requested or formal_eligible)
        and full_current_gm is not None
        and full_current_gm > threshold
    )
    return {
        "schema": "qmap-m4-legacy-regression-check-v1",
        "dataset": reference["dataset"],
        "status": "pass" if passed else ("partial" if remaining else "fail"),
        "passed": passed,
        "cohort_n": int(reference["cohort_n"]),
        "completed_n": len(completed),
        "missing_n": len(missing),
        "hash_mismatch_n": len(hash_mismatches),
        "formal_requested": formal_requested,
        "formal_eligible": formal_eligible,
        "formal_issues": list(current_report.get("formal_issues", [])),
        "current_m4_fidelity_geometric_mean": full_current_gm,
        "legacy_m4_fidelity_geometric_mean": threshold,
        "completed_current_m4_fidelity_geometric_mean": current_gm,
        "completed_legacy_m4_fidelity_geometric_mean": legacy_same_gm,
        "completed_current_vs_legacy_m4_gm_ratio": same_cohort_ratio,
        "cumulative_log_headroom": cumulative_log_headroom,
        "minimum_remaining_current_vs_legacy_gm_ratio_to_pass": (
            required_remaining_ratio),
        "missing_circuits": missing,
        "hash_mismatches": hash_mismatches,
    }


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    freeze = subparsers.add_parser("freeze")
    freeze.add_argument("--rows-json", type=Path, required=True)
    freeze.add_argument("--aggregate-json", type=Path, required=True)
    freeze.add_argument("--output", type=Path, required=True)
    check = subparsers.add_parser("check")
    check.add_argument("--reference", type=Path, required=True)
    check.add_argument("--current-report", type=Path, required=True)
    check.add_argument("--output", type=Path, required=True)
    collect = subparsers.add_parser("collect-check")
    collect.add_argument("--reference", type=Path, required=True)
    collect.add_argument("--attempt-root", type=Path, action="append",
                         required=True)
    collect.add_argument("--selection", choices=("best", "latest"),
                         default="best")
    collect.add_argument("--formal", action="store_true")
    collect.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "freeze":
        payload = freeze_reference(args.rows_json, args.aggregate_json)
    elif args.command == "check":
        reference = json.loads(args.reference.read_text(encoding="utf-8"))
        current = json.loads(args.current_report.read_text(encoding="utf-8"))
        payload = check_current(reference, current)
    else:
        reference = json.loads(args.reference.read_text(encoding="utf-8"))
        collection = collect_current_report(
            reference, args.attempt_root, selection=args.selection,
            formal=args.formal)
        payload = {
            "schema": "qmap-m4-current-manifest-regression-v1",
            "collection": collection,
            "regression": check_current(reference, collection),
        }
    _write_json(args.output, payload)
    if args.command == "freeze":
        summary = {
            key: payload[key]
            for key in ("schema", "dataset", "suite_n", "cohort_n",
                        "legacy_git_commit", "legacy_m4_config_sha256",
                        "targets", "primary_gate", "source_sha256")
        }
    elif args.command == "check":
        summary = {
            key: payload[key]
            for key in ("schema", "dataset", "status", "passed", "cohort_n",
                        "completed_n", "missing_n", "hash_mismatch_n",
                        "current_m4_fidelity_geometric_mean",
                        "legacy_m4_fidelity_geometric_mean",
                        "completed_current_vs_legacy_m4_gm_ratio",
                        "minimum_remaining_current_vs_legacy_gm_ratio_to_pass")
        }
    else:
        regression = payload["regression"]
        summary = {
            "schema": payload["schema"],
            "selection": payload["collection"]["selection"],
            "formal_requested": payload["collection"]["formal_requested"],
            "formal_eligible": payload["collection"]["formal_eligible"],
            "mixed_identity": payload["collection"]["mixed_identity"],
            "candidate_manifest_n": payload["collection"][
                "candidate_manifest_n"],
            "selected_success_n": payload["collection"]["selected_success_n"],
            "regression": {
                key: regression[key]
                for key in ("status", "passed", "cohort_n", "completed_n",
                            "missing_n", "hash_mismatch_n",
                            "completed_current_vs_legacy_m4_gm_ratio",
                            "minimum_remaining_current_vs_legacy_gm_ratio_to_pass")
            },
        }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
