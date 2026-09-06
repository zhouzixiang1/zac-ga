#!/usr/bin/env python3
"""Check manuscript numbers against frozen evidence and the rendered abstract.

This is a read-only presentation audit, not an experiment runner. Run with:
  python3 writing/audit_numerical_presentation.py --paper-root . \
      --project-root /path/to/zac \
      --pdf-path /path/to/zac/build/paper_zh/paper_zh.pdf
The standard output is one JSON report; a failed check returns exit status 1.
"""

from __future__ import annotations

import argparse
import csv
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import json
from pathlib import Path
import re
import subprocess
import sys
import unicodedata


PAIR_NAMES = {
    ("ZACMFourF", "ZACMOneF"): "ZAC18 / ZAC",
    ("ZACMFourF", "ZACMTwoF"): "ZAC18 / ICCAD-QMAP",
    ("QMAPMFourF", "QMAPMOneF"): "QMAP154 / ZAC",
    ("QMAPMFourF", "QMAPMTwoF"): "QMAP154 / ICCAD-QMAP",
}
CONFIG_RELATIVE = Path(
    "fidelity-lookahead-v2/artifacts/native-ga-v1/paper-zh-v1/configs/ablation"
)
NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"


def uncomment(tex: str) -> str:
    return re.sub(r"(?<!\\)%[^\n]*", "", tex)


def compact(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


def decimal(value: object) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError(f"Non-finite numeric value: {value}")
    return result


def percent_gain(numerator: Decimal, denominator: Decimal) -> Decimal:
    if numerator <= 0 or denominator <= 0:
        raise ValueError("Fidelity ratios require positive numerator and denominator")
    with localcontext() as context:
        context.prec = 40
        return (100 * (numerator / denominator - 1)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )


def abstract_tex(tex: str) -> str:
    match = re.search(r"\\begin\{abstract\}(.*?)\\end\{abstract\}", tex, re.S)
    if not match:
        raise ValueError("The main TeX file has no abstract environment")
    return uncomment(match.group(1))


def abstract_pdf(text: str) -> str:
    start = re.search(r"Abstract|摘\s*要", text, re.I)
    if not start:
        raise ValueError("PDF text has no Abstract/摘要 marker")
    rest = text[start.end():]
    end = re.search(r"Index\s+Terms|Key\s*words|关\s*键\s*词", rest, re.I)
    if not end:
        raise ValueError("PDF text has no Index Terms/关键词 end marker")
    return rest[:end.start()]


def range_present(text: str, lower: int, upper: int) -> bool:
    text = compact(text).replace("$", "")
    text = text.replace(r"\textendash", "-").replace(r"\textemdash", "-")
    return bool(re.search(
        rf"(?<!\d){lower}(?:-+|–|—|−|至|到){upper}(?!\d)", text
    ))


def audit(paper_root: Path, evidence_root: Path, project_root: Path, *,
          pdf_path: Path) -> dict:
    report = {
        "status": "fail", "paper_root": str(paper_root),
        "evidence_root": str(evidence_root), "pdf_path": str(pdf_path),
        "checks": [], "errors": [],
    }

    def check(name: str, passed: bool, **details: object) -> None:
        report["checks"].append({"name": name, "passed": passed, **details})
        if not passed:
            report["errors"].append(name)

    try:
        main_path = paper_root / "paper_zh.tex"
        if not main_path.is_file():
            main_path = paper_root / "paper.tex"
        main = uncomment(main_path.read_text(encoding="utf-8"))
        source_abstract = abstract_tex(main)
        pdf_path = pdf_path.resolve()
        if not pdf_path.is_file():
            raise FileNotFoundError(f"Missing rendered paper: {pdf_path}")

        # Resolve the result macros actually included by the manuscript.
        inputs = re.findall(r"\\input\{([^}]*results[^}]*)\}", main)
        if len(inputs) != 1:
            raise ValueError("Expected exactly one results-macro input in the main TeX")
        macro_path = paper_root / inputs[0]
        if macro_path.suffix != ".tex":
            macro_path = macro_path.with_suffix(".tex")
        macros = dict(re.findall(
            r"^\\(?:newcommand|renewcommand|providecommand)\{\\([^}]+)\}\{(.*)\}$",
            macro_path.read_text(encoding="utf-8"), re.M,
        ))
        evidence_macros = json.loads(
            (evidence_root / "paper_values.json").read_text(encoding="utf-8")
        )["macros"]
        pairs = re.findall(
            r"\\PaperPercentGain\s*\{\s*\\(\w+)\s*\}\s*\{\s*\\(\w+)\s*\}",
            source_abstract,
        )
        check("abstract_four_comparisons", len(pairs) == 4 and set(pairs) == set(PAIR_NAMES),
              observed=pairs, expected_comparisons=list(PAIR_NAMES.values()))
        if not report["checks"][-1]["passed"]:
            raise ValueError("The abstract must contain each of the four main comparisons once")
        required = {macro for pair in pairs for macro in pair}
        required |= {f"ZAC{method}T" for method in ("MOne", "MTwo", "MFour")}
        values = {name: decimal(macros[name]) for name in required}
        mismatches = {
            name: {"paper": macros[name], "evidence": evidence_macros.get(name)}
            for name in sorted(required)
            if values[name] != decimal(evidence_macros[name])
        }
        check("macro_evidence_consistency", not mismatches, mismatches=mismatches)

        completed = subprocess.run(
            ["pdftotext", "-raw", "-f", "1", "-l", "1", str(pdf_path), "-"],
            check=True, text=True, capture_output=True,
        )
        rendered_abstract = abstract_pdf(completed.stdout)
        expected = [percent_gain(values[a], values[b]) for a, b in pairs]
        observed = [decimal(value) for value in re.findall(
            rf"({NUMBER})%", compact(rendered_abstract)
        )]
        check("rendered_abstract_percentages", observed == expected,
              comparison_order=[PAIR_NAMES[pair] for pair in pairs],
              expected=[str(value) for value in expected],
              observed=[str(value) for value in observed])

        for dataset in ("zac18", "qmap154"):
            with (evidence_root / f"{dataset}.csv").open(encoding="utf-8", newline="") as file:
                qubits = [int(row["qubits"]) for row in csv.DictReader(file)]
            if not qubits:
                raise ValueError(f"Empty circuit inventory: {dataset}")
            bounds = min(qubits), max(qubits)
            source_ok = range_present(source_abstract, *bounds)
            pdf_ok = range_present(rendered_abstract, *bounds)
            check(f"{dataset}_abstract_qubit_range", source_ok and pdf_ok,
                  expected=list(bounds), source_present=source_ok, pdf_present=pdf_ok)

        evaluation = uncomment(
            (paper_root / "sections/05_evaluation.tex").read_text(encoding="utf-8")
        )
        configuration_values = {}
        for variant in ("h0", "h8"):
            for seed in range(3):
                filename = f"{variant}-seed{seed}.json"
                payload = json.loads(
                    (project_root / CONFIG_RELATIVE / filename).read_text(encoding="utf-8")
                )
                setting = payload["base_config"]["zac_setting"][0]
                horizon = setting["lookahead_horizon"]
                if int(horizon["max_horizon"]) != int(variant[1:]):
                    raise ValueError(f"Unexpected horizon in {filename}")
                configuration_values[filename] = (
                    decimal(setting["alpha_lookahead"]), decimal(horizon["rho"]),
                    decimal(horizon["epsilon"]),
                )
        greedy_filename = "greedy-seed0.json"
        greedy = json.loads(
            (project_root / CONFIG_RELATIVE / greedy_filename).read_text(encoding="utf-8")
        )["base_config"]["zac_setting"][0]
        configuration_values[greedy_filename] = (
            decimal(greedy["alpha_lookahead"]), decimal(greedy["lookahead_horizon"]["rho"]),
            decimal(greedy["lookahead_horizon"]["epsilon"]),
        )
        shared = set(configuration_values.values())
        check("shared_ablation_configuration", len(shared) == 1,
              configurations={key: [str(v) for v in value]
                              for key, value in configuration_values.items()})
        tuple_pattern = (rf"\(\\alpha,\\rho,\\epsilon\)=\("
                         rf"({NUMBER}),({NUMBER}),({NUMBER})\)")
        tuples = [tuple(decimal(v) for v in item)
                  for item in re.findall(tuple_pattern, compact(evaluation))]
        check("stated_ablation_parameters", bool(tuples) and len(shared) == 1
              and all(item in shared for item in tuples),
              observed=[[str(v) for v in item] for item in tuples],
              expected=[[str(v) for v in item] for item in sorted(shared)])
        check("selection_uses_content_not_name", "电路名" not in evaluation)

        # This regression previously marked ICCAD's larger time as the best.
        timing = {name: values[name] for name in required if name.endswith("T")}
        best = min(timing.values())
        expected_bold = sorted(name for name, value in timing.items() if value == best)
        table = uncomment(
            (paper_root / "sections/05_main_table.tex").read_text(encoding="utf-8")
        )
        observed_bold = sorted(re.findall(
            r"\\textbf\{\s*\\(ZAC(?:MOne|MTwo|MFour)T)\s*\}", table
        ))
        check("zac18_rearrangement_time_emphasis", observed_bold == expected_bold,
              expected=expected_bold, observed=observed_bold,
              times_ms={key: str(value) for key, value in sorted(timing.items())})
        stale_paragraphs = []
        for path in sorted((paper_root / "sections").glob("*.tex")):
            for paragraph in re.split(r"\n\s*\n", uncomment(path.read_text(encoding="utf-8"))):
                if "ZAC18" in paragraph and "重排时延" in paragraph and "介于" in paragraph:
                    stale_paragraphs.append(path.name)
        check("no_stale_between_baselines_claim", not stale_paragraphs,
              files=stale_paragraphs)
    except (OSError, ValueError, KeyError, IndexError, InvalidOperation,
            subprocess.CalledProcessError) as error:
        report["errors"].append(f"input_or_extraction_error: {error}")
    report["status"] = "pass" if not report["errors"] else "fail"
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paper-root", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path)
    parser.add_argument("--project-root", type=Path)
    parser.add_argument(
        "--pdf-path", type=Path, required=True,
        help="Exact rendered PDF to audit; no source-directory fallback")
    args = parser.parse_args(argv)
    paper_root = args.paper_root.resolve()
    evidence_root = args.evidence_root.resolve() if args.evidence_root else None
    project_root = args.project_root.resolve() if args.project_root else None
    if project_root is None and evidence_root is not None:
        project_root = next((ancestor for ancestor in evidence_root.parents
                             if (ancestor / CONFIG_RELATIVE).is_dir()), None)
    if project_root is None:
        print(json.dumps({"status": "fail", "errors": [
            "Provide --project-root or an --evidence-root within the ZAC project"
        ]}, ensure_ascii=False, indent=2))
        return 1
    if evidence_root is None:
        evidence_root = project_root / "ZAC_zzx/results/paper_zh_v2"
    report = audit(paper_root, evidence_root, project_root,
                   pdf_path=args.pdf_path.resolve())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
