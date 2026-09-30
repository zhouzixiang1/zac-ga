"""Derive Figure 7's paired QMAP distribution from existing publication exports.

This program never runs a compiler or experiment and never changes its inputs.
Normal mode creates a new plotting artifact once.  --check recomputes every
plotted value and checks recorded source hashes without writing any files.
Other publication bundles use the same M1/M2/Default schema; pass their source
paths, --expected-n and --method-label when creating or checking a new artifact.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys

from paper_paths import PAPER_ROOT, artifact_path

PROJECT_ROOT = PAPER_ROOT.parent
BUNDLE = PROJECT_ROOT / "ZAC_zzx/results/default_initial_v1/paper_exports"
DATASET = "qmap154"
EXPECTED_N = 120
DEFAULT_METHOD_LABEL = "GA-LK with lookahead initialization"
METHODS = ("M1", "M2", "Default")
FIELDS = ("rank", "sha", "dlog_vs_zac", "dlog_vs_iccad", "member_N")
DEFAULT_DATA = artifact_path(PAPER_ROOT, "figures/data/default_initial_qmap_distribution.dat")
DEFAULT_PROVENANCE = artifact_path(
    PAPER_ROOT, "figures/data/default_initial_qmap_distribution.provenance.json")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def _log_fidelity(row: dict, method: str) -> float:
    value = float(json.loads(row[method])["log_fidelity"])
    if not math.isfinite(value):
        raise ValueError(f"non-finite log fidelity for {method}")
    return value


def _wins_losses(rows: list[dict], field: str) -> dict[str, int]:
    values = [row[field] for row in rows]
    # Match the accepted export's numerical equality tolerance.
    return {"wins": sum(value > 1e-12 for value in values),
            "ties": sum(abs(value) <= 1e-12 for value in values),
            "losses": sum(value < -1e-12 for value in values)}


def derive_rows(analysis_units: Path, main_rows: Path, values: Path,
                expected_n: int = EXPECTED_N) -> list[dict]:
    """Use one existing canonical unit per point; verify file-level eligibility."""
    if type(expected_n) is not int or expected_n <= 0:
        raise ValueError("expected_n must be a positive integer")
    units = [row for row in _read_csv(analysis_units) if row["dataset"] == DATASET]
    files = [row for row in _read_csv(main_rows) if row["dataset"] == DATASET]
    declared = json.loads(values.read_text(encoding="utf-8"))["datasets"][DATASET]
    if len(units) != expected_n or declared["common_canonical_N"] != expected_n:
        raise ValueError(f"expected {expected_n} canonical units, found {len(units)}; "
                         f"summary declares {declared['common_canonical_N']}")
    eligible: dict[str, list[dict]] = {}
    seen_names = set()
    for row in files:
        if row["circuit"] in seen_names:
            raise ValueError("duplicate input-file identity")
        seen_names.add(row["circuit"])
        if row["common_fidelity"] != "True":
            continue
        if not all(json.loads(row[method])["complete_fidelity"] is True for method in METHODS):
            raise ValueError("file eligibility disagrees with method completeness")
        eligible.setdefault(row["canonical_sha256"], []).append(row)
    if sum(map(len, eligible.values())) != declared["common_file_N"]:
        raise ValueError("common input-file count disagrees with accepted summary")
    unit_ids = [row["canonical_sha256"] for row in units]
    if len(set(unit_ids)) != len(unit_ids):
        raise ValueError("duplicate canonical analysis unit")
    if set(unit_ids) != set(eligible):
        raise ValueError("canonical cohort differs from eligible input files")
    output = []
    for unit in units:
        digest = unit["canonical_sha256"]
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("invalid canonical SHA-256")
        members = eligible[digest]
        if (int(unit["member_N"]) != len(members)
                or sorted(json.loads(unit["circuits"])) != sorted(row["circuit"] for row in members)):
            raise ValueError("canonical alias membership disagrees with main rows")
        logs = {method: _log_fidelity(unit, method) for method in METHODS}
        for method in METHODS:
            reconstructed = statistics.fmean(_log_fidelity(row, method) for row in members)
            if not math.isclose(logs[method], reconstructed, rel_tol=1e-13, abs_tol=1e-13):
                raise ValueError("canonical log fidelity disagrees with eligible alias mean")
        output.append({"sha": digest, "member_N": len(members),
                       "dlog_vs_zac": logs["Default"] - logs["M1"],
                       "dlog_vs_iccad": logs["Default"] - logs["M2"]})
    output.sort(key=lambda row: (row["dlog_vs_zac"], row["sha"]))
    for rank, row in enumerate(output, 1):
        row["rank"] = rank
    for method, field in (("M1", "dlog_vs_zac"), ("M2", "dlog_vs_iccad")):
        if _wins_losses(output, field) != {key: declared["comparisons"][method][key]
                                         for key in ("wins", "ties", "losses")}:
            raise ValueError(f"{method} win/tie/loss counts disagree with accepted summary")
    return output


def render_dat(rows: list[dict]) -> str:
    lines = [" ".join(FIELDS)]
    for row in rows:
        lines.append(f"{row['rank']} {row['sha']} {row['dlog_vs_zac']:.17g} "
                     f"{row['dlog_vs_iccad']:.17g} {row['member_N']}")
    return "\n".join(lines) + "\n"


def _path_label(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path.resolve())


def _provenance(rows: list[dict], sources: dict[str, Path], content: str,
                method_label: str = DEFAULT_METHOD_LABEL) -> dict:
    return {
        "schema_version": 1,
        "generator": "scripts/paper/generate_fidelity_distribution.py",
        "dataset": DATASET,
        "unit_count": len(rows),
        "eligible_input_file_count": sum(row["member_N"] for row in rows),
        "unit": "one already-aggregated canonical circuit from analysis_units.csv",
        "methods": {"M1": "ZAC", "M2": "ICCAD/QMAP", "Default": method_label},
        "formulas": {"dlog_vs_zac": "Default.log_fidelity - M1.log_fidelity",
                     "dlog_vs_iccad": "Default.log_fidelity - M2.log_fidelity"},
        "order": ["dlog_vs_zac ascending", "canonical_sha256 ascending for exact ties"],
        "pairing": "both methods use the same circuit rank; no independent re-sorting",
        "range_policy": "all finite signed values retained; no clipping, tail removal, or fidelity division",
        "equality_tolerance": 1e-12,
        "sources": {name: {"path": _path_label(path), "sha256": sha256(path)}
                    for name, path in sources.items()},
        "data_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "comparisons": {field: {**_wins_losses(rows, field),
                                "minimum": min(row[field] for row in rows),
                                "maximum": max(row[field] for row in rows)}
                        for field in ("dlog_vs_zac", "dlog_vs_iccad")},
    }


def _check_values(path: Path, expected: list[dict]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or tuple(lines[0].split()) != FIELDS or len(lines) != len(expected) + 1:
        raise ValueError("plotting table header or row count changed")
    for line, row in zip(lines[1:], expected):
        tokens = line.split()
        if len(tokens) != len(FIELDS):
            raise ValueError("plotting table column count changed")
        actual = dict(zip(FIELDS, tokens))
        if (int(actual["rank"]) != row["rank"] or actual["sha"] != row["sha"]
                or int(actual["member_N"]) != row["member_N"]
                or any(float(actual[field]) != row[field] for field in ("dlog_vs_zac", "dlog_vs_iccad"))):
            raise ValueError(f"plotting value or pairing changed at rank {row['rank']}")


def produce(analysis_units: Path, main_rows: Path, values: Path, output: Path,
            provenance: Path, *, check: bool, expected_n: int = EXPECTED_N,
            method_label: str = DEFAULT_METHOD_LABEL) -> dict:
    if not isinstance(method_label, str) or not method_label.strip():
        raise ValueError("method_label must be a non-empty string")
    sources = {"analysis_units": analysis_units, "main_rows": main_rows, "accepted_values": values}
    initial_hashes = {name: sha256(path) for name, path in sources.items()}
    if check:
        recorded = json.loads(provenance.read_text(encoding="utf-8"))
        if any(recorded.get("sources", {}).get(name, {}).get("sha256") != digest
               for name, digest in initial_hashes.items()):
            raise ValueError("source hash changed since this plotting artifact was frozen")
    elif output.exists() or provenance.exists():
        raise ValueError("plotting artifact already exists; use --check for read-only verification")
    rows = derive_rows(analysis_units, main_rows, values, expected_n)
    content = render_dat(rows)
    manifest = _provenance(rows, sources, content, method_label)
    if initial_hashes != {name: sha256(path) for name, path in sources.items()}:
        raise ValueError("source changed during plotting-data derivation")
    if check:
        _check_values(output, rows)
        if recorded != manifest or sha256(output) != manifest["data_sha256"]:
            raise ValueError("plotting provenance or data checksum changed")
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        provenance.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(content, encoding="utf-8")
        provenance.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                              encoding="utf-8")
    return {"status": "pass", "mode": "check" if check else "create", "unit_count": len(rows),
            "eligible_input_file_count": manifest["eligible_input_file_count"],
            "data_sha256": manifest["data_sha256"], "comparisons": manifest["comparisons"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analysis-units", type=Path, default=BUNDLE / "analysis_units.csv")
    parser.add_argument("--main-rows", type=Path, default=BUNDLE / "main_rows.csv")
    parser.add_argument("--values", type=Path, default=BUNDLE / "default_initial_values.json")
    parser.add_argument("--output", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--provenance", type=Path, default=DEFAULT_PROVENANCE)
    parser.add_argument("--expected-n", type=int, default=EXPECTED_N,
                        help="expected common canonical circuit count (default: 120); "
                             "must match both analysis units and the values summary")
    parser.add_argument("--method-label", default=DEFAULT_METHOD_LABEL,
                        help="provenance label for the internal Default method; use the same "
                             "label when running --check")
    parser.add_argument("--check", action="store_true", help="recompute and compare without writing any files")
    args = parser.parse_args(argv)
    try:
        report = produce(args.analysis_units, args.main_rows, args.values, args.output,
                         args.provenance, check=args.check, expected_n=args.expected_n,
                         method_label=args.method_label)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "fail", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
