#!/usr/bin/env python3
"""Read-only publication gate for the separately exported H_init=2 bundle.

The original accepted paper_zh_v2 closure remains the main verifier's separate
responsibility. This gate reruns the independent exporter with --check, including
every pinned result, source, recovery audit and execution amendment.
"""
from __future__ import annotations

import argparse
import csv
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys


BUNDLE_RELATIVE = Path("ZAC_zzx/results/default_initial_v1/paper_exports")
AMENDMENT_RELATIVE = Path("IEEE_conference_template/build/default_initial_v1/expansions/extra-two-54csbfpr/amendment.json")
BUNDLE_FILES = ("default_initial_values.json", "default_initial_values.tex", "main_rows.csv",
                "analysis_units.csv", "mechanism.csv", "representative_cases.tex")
QFT_FIELDS = ("transfers", "idle_exposures", "log_atom_transfer",
              "log_idle_excitation", "log_coherence_linear")


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def manuscript_inputs(paper_root):
    main = re.sub(r"(?<!\\)%[^\n]*", "", (paper_root / "paper_zh.tex").read_text(encoding="utf-8"))
    paths = []
    for value in re.findall(r"\\input\s*\{([^}]+)\}", main):
        path = paper_root / value
        paths.append(path.with_suffix(".tex").resolve() if not path.suffix else path.resolve())
    return paths


def qft_legacy_alias_audit(paper_root, bundle, values):
    """Keep an unchanged panel only when its raw mechanism row is identical.

    Old macros retain CSV precision; Default macros use the exporter's .15g
    serialization. Compare raw values exactly before checking that rendering
    convention, never use a tolerance to excuse a changed experiment result.
    """
    figure = paper_root / "figures/experimental_summary.tex"
    source = re.sub(r"(?<!\\)%[^\n]*", "", figure.read_text(encoding="utf-8")) if figure.is_file() else ""
    if not re.search(r"\\MechanismQFT[A-Za-z]+\b", source):
        return {"status": "not_used"}
    accepted = paper_root.parent / "ZAC_zzx/results/paper_zh_v2/fig6_mechanism.csv"
    with accepted.open(newline="", encoding="utf-8") as stream:
        old_rows = [row for row in csv.DictReader(stream)
                    if row["dataset"] == "zac18" and row["circuit"] == "qft_n18_transpiled"]
    with (bundle / "mechanism.csv").open(newline="", encoding="utf-8") as stream:
        new_rows = [row for row in csv.DictReader(stream)
                    if row["dataset"] == "zac18" and row["baseline_method"] == "M1"
                    and "qft_n18_transpiled" in json.loads(row["circuits"])]
    if len(old_rows) != 1 or len(new_rows) != 1:
        raise ValueError("legacy QFT panel requires exactly one accepted and Default mechanism row")
    old, new = old_rows[0], new_rows[0]
    if (old["baseline_method"] != "M1" or old["strict_paired"] != "True"
            or old["canonical_sha256"] != new["canonical_sha256"]):
        raise ValueError("legacy QFT panel does not share the exact Default circuit/baseline")
    for field in QFT_FIELDS:
        for old_prefix, new_prefix in (("Bstar_", "baseline_"), ("M4_", "default_"),
                                       ("M4_minus_Bstar_", "delta_")):
            a, b = Decimal(old[old_prefix + field]), Decimal(new[new_prefix + field])
            if not a.is_finite() or not b.is_finite() or a != b:
                raise ValueError("legacy QFT mechanism differs from Default: " + new_prefix + field)
    macro_path = paper_root / "method_argument_values.tex"
    pairs = re.findall(r"\\newcommand\{\\(MechanismQFT[A-Za-z]+)\}\{([^{}]+)\}",
                       macro_path.read_text(encoding="utf-8"))
    macros = dict(pairs)
    if len(pairs) != len(macros):
        raise ValueError("duplicate legacy QFT mechanism macro")
    expected = {"MechanismQFTBaseTransfers": old["Bstar_transfers"],
                "MechanismQFTGATransfers": old["M4_transfers"]}
    for component, field in (("Transfer", "log_atom_transfer"),
                             ("Excitation", "log_idle_excitation"),
                             ("Coherence", "log_coherence_linear")):
        key = "MechanismQFT" + component + "Gain"
        expected[key] = old["M4_minus_Bstar_" + field]
        expected[key + "Rounded"] = format(Decimal(expected[key]), ".4f")
    for name, raw in expected.items():
        if Decimal(macros[name]) != Decimal(raw):
            raise ValueError("legacy QFT macro is not bound to its accepted row: " + name)
        canonical = raw if name.endswith("Rounded") else format(float(raw), ".15g")
        if values["macros"]["Default" + name] != canonical:
            raise ValueError("Default QFT macro does not serialize the identical accepted row: " + name)
    return {"status": "pass", "exact_raw_fields": 15, "equivalent_macros": sorted(expected),
            "accepted_mechanism_sha256": sha256(accepted),
            "default_mechanism_sha256": sha256(bundle / "mechanism.csv"),
            "legacy_macro_sha256": sha256(macro_path), "numeric_tolerance_used": False}


def audit(paper_root):
    paper_root = Path(paper_root).resolve()
    project_root = paper_root.parent
    bundle = project_root / BUNDLE_RELATIVE
    report = {"status": "fail", "read_only": True, "errors": [],
              "bundle": str(BUNDLE_RELATIVE), "verification": "full_exporter_check"}
    try:
        included = manuscript_inputs(paper_root)
        for name in ("default_initial_values.tex", "representative_cases.tex"):
            expected = bundle / name
            if included.count(expected) != 1:
                raise ValueError("manuscript must input the exact publication file once: " + name)
        for name in BUNDLE_FILES:
            path = bundle / name
            if path.is_symlink() or not path.is_file():
                raise ValueError("missing or symlinked publication file: " + name)
        before = {name: sha256(bundle / name) for name in BUNDLE_FILES}
        command = [sys.executable, "-B", str(paper_root / "writing/generate_default_initial_values.py"),
                   "--execution-amendment", str(project_root / AMENDMENT_RELATIVE),
                   "--output-root", str(bundle), "--check"]
        result = subprocess.run(command, cwd=project_root, capture_output=True, text=True)
        try:
            checked = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ValueError("exporter --check returned no valid report: " + result.stderr[-1500:]) from exc
        if (result.returncode != 0 or checked.get("status") != "pass"
                or checked.get("read_only") is not True or checked.get("different_exports") != []):
            raise ValueError("independent publication export differs: " + json.dumps(checked))
        after = {name: sha256(bundle / name) for name in BUNDLE_FILES}
        if before != after:
            raise ValueError("publication files changed during their verification")
        values = json.loads((bundle / "default_initial_values.json").read_text(encoding="utf-8"))
        study = values["provenance"]["new_default"]
        if (study.get("complete") is not True or study["execution_conditions"].get("complete") is not True
                or values.get("publication_status") != "independently_verified_complete_amended_quality_matrix"):
            raise ValueError("publication bundle is not a complete amended quality result")
        report.update(status="pass", files=after, source_file_count=len(study["verified_files"]),
                      actual_status_counts=study["actual_status_counts"],
                      execution_conditions_complete=True, timing_comparison_eligible=False,
                      does_not_replace_old_accepted_audit=True)
        report["legacy_qft_panel_equivalence"] = qft_legacy_alias_audit(paper_root, bundle, values)
    except (OSError, ValueError, KeyError, TypeError, InvalidOperation) as exc:
        report["status"] = "fail"
        report["errors"].append(str(exc))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paper-root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    report = audit(args.paper_root)
    print(json.dumps(report, sort_keys=True, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
