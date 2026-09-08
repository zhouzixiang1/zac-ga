#!/usr/bin/env python3
"""Check manuscript numbers against frozen evidence and the rendered abstract.

This is a read-only presentation audit, not an experiment runner. Run with:
  python3 writing/audit_numerical_presentation.py --paper-root . \
      --project-root /path/to/zac \
      --pdf-path /path/to/zac/IEEE_conference_template/build/paper_zh/paper_zh.pdf
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
SUMMARY_PAIRS = {
    "Gain": PAIR_NAMES,
    "Reduction": {
        (numerator[:-1] + "B", denominator[:-1] + "B"): label
        for (numerator, denominator), label in PAIR_NAMES.items()
    },
}
SUMMARY_MACRO = re.compile(
    r"\\PaperPercent(Gain|Reduction)\s*\{\s*\\(\w+)\s*\}\s*\{\s*\\(\w+)\s*\}"
)
DEFAULT_EXPORT_RELATIVE = Path("ZAC_zzx/results/default_initial_v1/paper_exports")
DEFAULT_MAXIMA = {
    "Gain": ("DefaultMaxDatasetFidelityGain", "fidelity_gain_percent"),
    "Reduction": ("DefaultMaxDatasetBatchReduction", "mean_batch_reduction_percent"),
}
DEFAULT_DIRECT_MACRO = re.compile(
    r"\\(DefaultMaxDatasetFidelityGain|DefaultMaxDatasetBatchReduction)\b(\s*\\%)?"
)
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


def improvement(numerator: Decimal, denominator: Decimal, kind: str) -> Decimal:
    """Unrounded relative improvement, with lower batches treated as better."""
    if numerator <= 0 or denominator <= 0:
        raise ValueError("Summary ratios require positive numerator and denominator")
    with localcontext() as context:
        context.prec = 40
        ratio_change = numerator / denominator - 1
        return ratio_change if kind == "Gain" else -ratio_change


def summary_percent(numerator: Decimal, denominator: Decimal, kind: str) -> Decimal:
    return (100 * improvement(numerator, denominator, kind)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


def summary_scope(text: str, metric_starts: list[int]) -> dict[str, bool]:
    """Require both maxima to be described as benchmark-set summary metrics.

    The words must qualify the actual metric, not occur elsewhere in the abstract.
    This Chinese-paper audit also checks the corresponding extracted PDF text.
    """
    if len(metric_starts) != 2:
        return {"two_metric_positions": False}
    prefixes = [compact(text[:start]) for start in metric_starts]
    clauses = [re.split(r"[。；;，,]", prefix)[-1] for prefix in prefixes]
    fidelity, batches = clauses
    return {
        "benchmark_set_summary": bool(re.search(
            r"基准集.{0,12}汇总|汇总.{0,12}基准集", prefixes[0][-100:]
        )) or bool(re.search(r"基准集.{0,12}保真度", fidelity)),
        "geometric_mean_fidelity": "保真度" in fidelity and "几何均值" in fidelity,
        "maximum_fidelity_gain": "最高" in fidelity and bool(re.search(
            r"提高|提升", fidelity
        )) and not bool(re.search(r"减少|降低|下降", fidelity)),
        "mean_rearrangement_batches": "平均" in batches and "重排批次" in batches,
        "maximum_batch_reduction": "最多" in batches and "减少" in batches
        and not bool(re.search(r"提高|提升|增加", batches)),
    }


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


def declared_ranges(text: str) -> set[tuple[int, int]]:
    """Extract integer inventory ranges, without requiring an abstract to give them."""
    text = compact(text).replace("$", "")
    text = text.replace(r"\textendash", "-").replace(r"\textemdash", "-")
    return {tuple(map(int, pair)) for pair in re.findall(
        r"(?<![\d.])(\d+)(?:-+|–|—|−|至|到)(\d+)(?![\d.])", text
    )}


def declared_inventory_counts(text: str, dataset: str) -> list[int]:
    """Associate an explicit input count with its benchmark name, not its digits."""
    text = compact(text)
    name = dataset.upper()
    unit = r"(?:基准)?电路" if dataset == "zac18" else r"(?:示例)?输入"
    patterns = (
        rf"{name}[^。；]{{0,40}}?(?<!\d)(\d+)个{unit}",
        rf"(?<!\d)(\d+)个{unit}[（(]{name}[）)]",
    )
    return [int(count) for pattern in patterns for count in re.findall(pattern, text)]


def read_macros(path: Path) -> dict[str, str]:
    definitions = re.findall(
        r"^\\(?:newcommand|renewcommand|providecommand)\{\\([^}]+)\}\{(.*)\}$",
        uncomment(path.read_text(encoding="utf-8")), re.M,
    )
    if len(dict(definitions)) != len(definitions):
        raise ValueError(f"Duplicate result macro definitions: {path}")
    return dict(definitions)


def default_summary(source: str, inputs: list[str], paper_root: Path,
                    project_root: Path, check):
    """Check presentation against the declared new bundle, not its raw traces."""
    expected_path = project_root.resolve() / DEFAULT_EXPORT_RELATIVE / "default_initial_values.tex"
    paths = [(paper_root / item).with_suffix(".tex").resolve() for item in inputs]
    check("default_result_input", paths == [expected_path],
          expected=str(expected_path), observed=[str(path) for path in paths])
    if paths != [expected_path]:
        raise ValueError("Default results require exactly the declared default_initial_v1 bundle input")
    macros = read_macros(expected_path)
    payload = json.loads(expected_path.with_suffix(".json").read_text(encoding="utf-8"))
    evidence_macros = payload["macros"]
    required = {f"Default{dataset}{method}{field}" for dataset in ("ZAC", "QMAP")
                for method in ("MOne", "MTwo", "MFour") for field in ("F", "B", "T", "V")}
    required |= {f"Default{dataset}{field}" for dataset in ("ZAC", "QMAP")
                 for field in ("StrictN", "StrictFileN")}
    required |= {name for name, _ in DEFAULT_MAXIMA.values()}
    mismatches = {name: {"paper": macros.get(name), "evidence": evidence_macros.get(name)}
                  for name in sorted(set(macros) | set(evidence_macros) | required)
                  if name not in macros or name not in evidence_macros
                  or macros[name] != evidence_macros[name]}
    check("default_macro_evidence_consistency", not mismatches, mismatches=mismatches)

    pairs = {kind: {("Default" + a, "Default" + b): label for (a, b), label in entries.items()}
             for kind, entries in SUMMARY_PAIRS.items()}
    metrics = [(match.start(), *match.groups()) for match in SUMMARY_MACRO.finditer(source)]
    for match in DEFAULT_DIRECT_MACRO.finditer(source):
        kind = next(kind for kind, (name, _) in DEFAULT_MAXIMA.items() if name == match.group(1))
        metrics.append((match.start(), kind, match.group(1), None if match.group(2) else "missing_percent"))
    metrics.sort()
    valid = len(metrics) == 2 and [metric[1] for metric in metrics] == ["Gain", "Reduction"]
    valid = valid and all((a == DEFAULT_MAXIMA[kind][0] and b is None)
                          or (a, b) in pairs[kind] for _, kind, a, b in metrics)
    check("abstract_two_summary_metrics", valid, observed=metrics)
    if not valid:
        raise ValueError("The Default abstract requires two declared macro-derived summary metrics")

    datasets = payload["datasets"]
    if set(datasets) != {"zac18", "qmap154"} or any(
            set(dataset["comparisons"]) != {"M1", "M2"} for dataset in datasets.values()):
        raise ValueError("Default summary must retain both datasets and both baseline comparisons")
    maxima, expected, order = {}, [], []
    for _, kind, a, b in metrics:
        maximum_macro, field = DEFAULT_MAXIMA[kind]
        candidates = {f"{dataset_name} / {baseline_name}": decimal(dataset["comparisons"][baseline][field])
                      for dataset, dataset_name in ((datasets["zac18"], "ZAC18"), (datasets["qmap154"], "QMAP154"))
                      for baseline, baseline_name in (("M1", "ZAC"), ("M2", "ICCAD-QMAP"))
                      if kind != "Reduction" or dataset["comparisons"][baseline][field] is not None}
        if not candidates:
            raise ValueError(f"Default summary has no numeric candidates for {kind}")
        maximum = max(candidates.values())
        rounded = maximum.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        selected_label = "maximum over both datasets and baselines" if b is None else pairs[kind][(a, b)]
        displayed = decimal(macros[a]) if b is None else summary_percent(decimal(macros[a]), decimal(macros[b]), kind)
        maximum_matches = decimal(evidence_macros[maximum_macro]) == rounded
        selected_is_maximum = b is None or candidates[selected_label] == maximum
        maxima[kind] = {"selected": selected_label, "maximum_percent": str(maximum),
                        "displayed_percent": str(displayed),
                        "is_positive_maximum": maximum > 0 and maximum_matches
                        and selected_is_maximum and displayed == rounded,
                        "candidates": {label: str(value) for label, value in candidates.items()}}
        expected.append(displayed)
        order.append(f"{kind}: {selected_label}")
    check("abstract_summary_maxima", all(item["is_positive_maximum"] for item in maxima.values()),
          metrics=maxima)
    timing = {f"DefaultZAC{method}T": decimal(macros[f"DefaultZAC{method}T"])
              for method in ("MOne", "MTwo", "MFour")}
    return expected, [metric[0] for metric in metrics], order, timing


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
        all_inputs = re.findall(r"\\input\s*\{([^}]+)\}", main)
        inputs = [item for item in all_inputs if "results" in Path(item).name]
        if len(inputs) != 1:
            raise ValueError("Expected exactly one results-macro input in the main TeX")
        macro_path = paper_root / inputs[0]
        if macro_path.suffix != ".tex":
            macro_path = macro_path.with_suffix(".tex")
        macros = read_macros(macro_path)
        evidence_macros = json.loads(
            (evidence_root / "paper_values.json").read_text(encoding="utf-8")
        )["macros"]
        literal_percentages = re.findall(rf"({NUMBER})\s*\\%", source_abstract)
        check("abstract_no_literal_percentages", not literal_percentages,
              observed=literal_percentages)
        # Read every candidate, including unselected summaries, so the maxima are
        # checked against both benchmark sets and both baselines, not assumed.
        required = {macro for pairs in SUMMARY_PAIRS.values() for pair in pairs for macro in pair}
        required |= {f"ZAC{method}T" for method in ("MOne", "MTwo", "MFour")}
        values = {name: decimal(macros[name]) for name in required}
        mismatches = {
            name: {"paper": macros[name], "evidence": evidence_macros.get(name)}
            for name in sorted(required)
            if values[name] != decimal(evidence_macros[name])
        }
        check("macro_evidence_consistency", not mismatches, mismatches=mismatches)
        table = uncomment(
            (paper_root / "sections/05_main_table.tex").read_text(encoding="utf-8")
        )
        default_inputs = [item for item in all_inputs if Path(item).stem == "default_initial_values"]
        default_mode = bool(default_inputs or re.search(r"\\Default(?:ZAC|QMAP|MaxDataset)", main + table))
        report["quality_study"] = "default_initial_v1" if default_mode else "paper_zh_v2"
        if default_mode:
            expected, metric_starts, metric_order, timing = default_summary(
                source_abstract, default_inputs, paper_root, project_root, check)
            expected_table = {f"Default{dataset}{method}{field}" for dataset in ("ZAC", "QMAP")
                              for method in ("MOne", "MTwo", "MFour") for field in ("F", "B", "T", "V")}
            observed_table = set(re.findall(
                r"\\((?:Default)?(?:ZAC|QMAP)(?:MOne|MTwo|MFour)[FBTV])\b", table))
            check("default_main_table_macros", observed_table == expected_table,
                  missing=sorted(expected_table - observed_table),
                  unexpected=sorted(observed_table - expected_table))
        else:
            metric_matches = list(SUMMARY_MACRO.finditer(source_abstract))
            metrics = [match.groups() for match in metric_matches]
            check("abstract_two_summary_metrics", len(metrics) == 2
                  and [metric[0] for metric in metrics] == ["Gain", "Reduction"]
                  and all((a, b) in SUMMARY_PAIRS[kind] for kind, a, b in metrics),
                  observed=metrics, expected_metrics=[
                      "maximum benchmark-set geometric-mean fidelity gain",
                      "maximum benchmark-set mean rearrangement-batch reduction",
                  ])
            if not report["checks"][-1]["passed"]:
                raise ValueError("The abstract must contain exactly two macro-derived summary metrics")
            metric_starts = [match.start() for match in metric_matches]
            maxima = {}
            for kind, numerator, denominator in metrics:
                candidates = {
                    label: improvement(decimal(evidence_macros[a]), decimal(evidence_macros[b]), kind)
                    for (a, b), label in SUMMARY_PAIRS[kind].items()
                }
                selected = improvement(
                    decimal(evidence_macros[numerator]), decimal(evidence_macros[denominator]), kind
                )
                maxima[kind] = {
                    "selected": SUMMARY_PAIRS[kind][(numerator, denominator)],
                    "relative_improvement": str(selected),
                    "maximum_relative_improvement": str(max(candidates.values())),
                    "is_positive_maximum": selected > 0 and selected == max(candidates.values()),
                    "candidates": {label: str(value) for label, value in candidates.items()},
                }
            check("abstract_summary_maxima", all(item["is_positive_maximum"]
                  for item in maxima.values()), metrics=maxima)
            expected = [summary_percent(values[a], values[b], kind) for kind, a, b in metrics]
            metric_order = [f"{kind}: {SUMMARY_PAIRS[kind][(a, b)]}" for kind, a, b in metrics]
            timing = {name: values[name] for name in required if name.endswith("T")}
        scope = summary_scope(source_abstract, metric_starts)
        check("abstract_summary_scope", all(scope.values()), **scope)

        completed = subprocess.run(
            ["pdftotext", "-raw", "-f", "1", "-l", "1", str(pdf_path), "-"],
            check=True, text=True, capture_output=True,
        )
        rendered_abstract = abstract_pdf(completed.stdout)
        rendered_compact = compact(rendered_abstract)
        rendered_matches = list(re.finditer(rf"({NUMBER})%", rendered_compact))
        observed = [decimal(match.group(1)) for match in rendered_matches]
        check("rendered_abstract_percentages", observed == expected,
              metric_order=metric_order,
              expected=[str(value) for value in expected],
              observed=[str(value) for value in observed])
        rendered_scope = summary_scope(
            rendered_compact, [match.start() for match in rendered_matches]
        )
        check("rendered_abstract_summary_scope", all(rendered_scope.values()), **rendered_scope)

        evaluation = uncomment(
            (paper_root / "sections/05_evaluation.tex").read_text(encoding="utf-8")
        )
        inventories = {}
        for dataset in ("zac18", "qmap154"):
            with (evidence_root / f"{dataset}.csv").open(encoding="utf-8", newline="") as file:
                rows = list(csv.DictReader(file))
            qubits = [int(row["qubits"]) for row in rows]
            if not qubits:
                raise ValueError(f"Empty circuit inventory: {dataset}")
            inventories[dataset] = (min(qubits), max(qubits))
            counts = declared_inventory_counts(evaluation, dataset)
            check(f"{dataset}_evaluation_input_count", bool(counts)
                  and all(count == len(rows) for count in counts),
                  expected=len(rows), observed=counts)
            if dataset == "qmap154":
                identities = [row["canonical_sha256"] for row in rows]
                if not all(identities):
                    raise ValueError("Missing canonical circuit identity in qmap154")
                unique_counts = [int(value) for value in re.findall(
                    r"规范化[^。；]{0,30}?(\d+)个不同电路", compact(evaluation)
                )]
                check("qmap154_evaluation_unique_count", bool(unique_counts)
                      and all(count == len(set(identities)) for count in unique_counts),
                      expected=len(set(identities)), observed=unique_counts)

        source_ranges = declared_ranges(source_abstract)
        pdf_ranges = declared_ranges(rendered_abstract)
        allowed_ranges = set(inventories.values())
        unknown_ranges = (source_ranges | pdf_ranges) - allowed_ranges
        for dataset, bounds in inventories.items():
            source_present = bounds in source_ranges
            pdf_present = bounds in pdf_ranges
            # The abstract may omit inventory details. If it states them,
            # they must be accurate and agree with the rendered abstract.
            check(f"{dataset}_abstract_qubit_range", not unknown_ranges
                  and source_present == pdf_present,
                  expected=list(bounds), required_in_abstract=False,
                  source_present=source_present, pdf_present=pdf_present,
                  unrecognized_ranges=[list(pair) for pair in sorted(unknown_ranges)])
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
        # Named values are easier to read than a three-parameter tuple. Keep
        # each complete declaration within one sentence and validate every
        # occurrence, including conflicting repeated declarations.
        named_pattern = (rf"\\alpha=({NUMBER})[^。]*?"
                         rf"\\rho=({NUMBER})[^。]*?\\epsilon=({NUMBER})")
        named_values = [tuple(decimal(v) for v in item)
                        for item in re.findall(named_pattern, compact(evaluation))]
        tuples.extend(named_values)
        check("stated_ablation_parameters", bool(tuples) and len(shared) == 1
              and all(item in shared for item in tuples),
              observed=[[str(v) for v in item] for item in tuples],
              expected=[[str(v) for v in item] for item in sorted(shared)])
        check("selection_uses_content_not_name", "电路名" not in evaluation)

        # This regression previously marked ICCAD's larger time as the best.
        best = min(timing.values())
        expected_bold = sorted(name for name, value in timing.items() if value == best)
        observed_bold = sorted(re.findall(
            r"\\textbf\{\s*\\((?:Default)?ZAC(?:MOne|MTwo|MFour)T)\s*\}", table
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
