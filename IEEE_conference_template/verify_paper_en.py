#!/usr/bin/env python3
"""Build and audit the complete English counterpart of the Chinese manuscript.

Use `make paper-en`: the Chinese paper first validates shared figures/evidence.
English pagination is natural; references must occupy the separate last page.
This checks structural and mathematical correspondence, not prose translation
quality, which still requires the author's bilingual review.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
from typing import Any

import verify_paper_zh as zh


BUILD_DIRECTORY = Path("build/paper_en")
HAN = re.compile(r"[\u3400-\u9fff]")
FORMULA_TEXT_TRANSLATIONS = {r"\text{存在}": r"\text{exists}"}
DISPLAY_ENVIRONMENT = re.compile(
    r"\\begin\{(equation\*?|align\*?|gather\*?|multline\*?)\}(.*?)"
    r"\\end\{\1\}", re.S)


def commands(text: str, command: str) -> list[str]:
    return re.findall(r"\\" + command + r"\s*\{([^{}]*)\}", text)


def result_macro_names(root: Path) -> set[str]:
    names = {"PaperPercentGain", "PaperPercentReduction"}
    for path in [root / "results_values_zh.tex", root / "initial_lookahead_values.tex",
                 *sorted((root / "figures/data").glob("*.tex"))]:
        if not path.is_file():
            continue
        text = zh._strip_tex_comments(path.read_text(encoding="utf-8"))
        names.update(re.findall(r"\\(?:newcommand|renewcommand|providecommand)\s*\{\\([A-Za-z@]+)\}", text))
        names.update(re.findall(r"\\(?:def|gdef)\\([A-Za-z@]+)", text))
    return names


def canonical_equation(text: str) -> str:
    for original, translated in FORMULA_TEXT_TRANSLATIONS.items():
        text = text.replace(original, translated)
    text = re.sub(r"\\label\s*\{[^{}]+\}", "", text)
    text = re.sub(r"\\(?:begin|end)\{(?:aligned|split|gathered)\}", "", text)
    text = re.sub(r"\\\\(?:\[[^\]]*\])?", "", text)
    text = re.sub(r"\\(?:quad|qquad)(?![A-Za-z])|\\[,!;:]", "", text)
    return re.sub(r"\s+", "", text)


def display_equations(text: str) -> dict[str, str]:
    equations = {}
    for index, match in enumerate(DISPLAY_ENVIRONMENT.finditer(text), 1):
        labels = commands(match.group(2), "label")
        key = labels[0] if len(labels) == 1 else f"unlabelled-display-{index}"
        if key in equations:
            raise ValueError(f"duplicate_display_equation:{key}")
        equations[key] = canonical_equation(match.group(2))
    return equations


def reference_heading_pages(text: str) -> list[int]:
    """Find a heading, including IEEE small caps and two-column extraction.

    With pdftotext -layout, the first right-column bibliography entry may share
    the REFERENCES line. Only a numbered entry after a column-size gap is
    allowed alongside the heading; ordinary prose containing the word is not.
    """
    horizontal = r"[^\S\r\n\f]"
    heading = (horizontal + "*").join("REFERENCES")
    pattern = re.compile(
        rf"^{horizontal}*{heading}(?:{horizontal}*$|{horizontal}{{2,}}\[\d+\]{horizontal}+)",
        re.M | re.I)
    return [index for index, page in enumerate(text.split("\f"), 1)
            if pattern.search(page)]


def audit_translation(root: Path, errors: list[str]) -> dict[str, Any]:
    expected = {path.name for path in (root / "sections").glob("*.tex")}
    actual = {path.name for path in (root / "sections_en").glob("*.tex")}
    if expected != actual:
        errors.append("english_section_file_set_mismatch")
    pairs = [("paper", root / "paper_zh.tex", root / "paper_en.tex")]
    pairs.extend((name, root / "sections" / name, root / "sections_en" / name)
                 for name in sorted(expected))
    names = result_macro_names(root)
    report: dict[str, Any] = {
        "missing_sections": sorted(expected - actual), "extra_sections": sorted(actual - expected),
        "formula_text_whitelist": FORMULA_TEXT_TRANSLATIONS, "files": {},
    }
    float_counts = {"zh": Counter(), "en": Counter()}
    for name, original, translated in pairs:
        if not original.is_file() or not translated.is_file():
            errors.append(f"missing_translation_pair:{name}")
            continue
        source = zh._strip_tex_comments(original.read_text(encoding="utf-8"))
        target = zh._strip_tex_comments(translated.read_text(encoding="utf-8"))
        labels_equal = Counter(commands(source, "label")) == Counter(commands(target, "label"))
        citations = lambda text: {
            key.strip() for group in re.findall(
                r"\\cite(?:[tp]|author|year)?\*?(?:\[[^\]]*\]){0,2}\{([^}]+)\}", text)
            for key in group.split(",") if key.strip()}
        citations_equal = citations(source) == citations(target)
        macro_uses = lambda text: Counter(token for token in re.findall(r"\\([A-Za-z@]+)", text) if token in names)
        source_macros, target_macros = macro_uses(source), macro_uses(target)
        macro_differences = {
            key: {"zh": source_macros[key], "en": target_macros[key]}
            for key in sorted(source_macros.keys() | target_macros.keys())
            if source_macros[key] != target_macros[key]}
        source_inputs = Counter(path.replace("sections/", "sections_en/", 1)
                                if path.startswith("sections/") else path
                                for path in commands(source, "input"))
        inputs_equal = source_inputs == Counter(commands(target, "input"))
        external_figures = lambda text: Counter(re.findall(
            r"\\includegraphics(?:\[[^\]]*\])?\s*\{([^{}]+)\}", text))
        source_equations, target_equations = display_equations(source), display_equations(target)
        equation_differences = {
            key: {"zh": source_equations.get(key), "en": target_equations.get(key)}
            for key in sorted(source_equations.keys() | target_equations.keys())
            if source_equations.get(key) != target_equations.get(key)}
        chinese_count = len(HAN.findall(target))
        checks = {
            "labels_equal": labels_equal, "citations_equal": citations_equal,
            "result_macros_equal": not macro_differences, "input_graph_equal": inputs_equal,
            "external_figure_paths_equal": external_figures(source) == external_figures(target),
            "display_equations_equal": not equation_differences, "no_chinese": chinese_count == 0,
        }
        report["files"][name] = {
            **checks, "result_macro_differences": macro_differences,
            "equation_differences": equation_differences, "equation_count": len(source_equations),
            "chinese_character_count": chinese_count,
        }
        for check, passed in checks.items():
            if not passed:
                errors.append(f"translation_{check}:{name}")
        for language, text in (("zh", source), ("en", target)):
            float_counts[language].update(re.findall(r"\\begin\{(figure|table)\*?\}", text))
    report["floats"] = {language: dict(counts) for language, counts in float_counts.items()}
    if float_counts["en"] != Counter({"figure": 7, "table": 4}) or float_counts["en"] != float_counts["zh"]:
        errors.append("english_requires_seven_figures_four_tables")
    return report


def audit_shared_figure(root: Path, errors: list[str]) -> dict[str, Any]:
    audit = zh._source_build_manifest(root, compile_pdf=False, source_before=None, errors=errors)
    try:
        qa = json.loads((root / zh.BUILD_DIRECTORY / "final_paper_qa.json").read_text(encoding="utf-8"))
        if (qa.get("status") != "pass" or qa.get("page_count") != 9
                or qa.get("overall_figure_pdf", {}).get("pages") != 1):
            errors.append("shared_chinese_build_not_verified")
        figures = qa.get("core_figures", {})
        if (set(figures) != set(zh.CORE_FIGURES)
                or any(not item.get("tikz") or item.get("includegraphics")
                       or item.get("contains_han") for item in figures.values())):
            errors.append("shared_seven_figure_sources_not_verified")
    except (OSError, ValueError, AttributeError):
        errors.append("shared_chinese_build_not_verified")
    return audit


def compile_english(root: Path, build: Path) -> dict[str, Any]:
    pdf = build / "paper_en.pdf"
    pdf.unlink(missing_ok=True)
    command = ["latexmk", "-g", "-xelatex", "-interaction=nonstopmode", "-halt-on-error",
               f"-outdir={build}", f"-auxdir={build}", "paper_en.tex"]
    result = zh._run(command, cwd=root)
    if result.returncode != 0:
        pdf.unlink(missing_ok=True)
    return {"requested": True, "returncode": result.returncode,
            "output_tail": result.stdout[-3000:], "pdf_exists": pdf.is_file()}


def source_binding(root: Path, build: Path, *, compile_pdf: bool,
                   before: dict[str, str] | None, errors: list[str]) -> dict[str, Any]:
    manifest = build / "source_build_manifest.json"
    current = zh._scientific_source_hashes(root)
    files = [build / "paper_en.pdf", (root / zh.OVERALL_FIGURE_PDF).resolve()]
    artifacts = {zh._reported_relative_path(path, root): {"sha256": zh._sha256(path)}
                 for path in files if path.is_file()}
    audit = {"path": zh._reported_relative_path(manifest, root), "written": False, "status": "fail"}
    if compile_pdf and before != current:
        errors.append("english_sources_changed_during_compile")
    expected = {"protocol": "paper-en-source-build-v1", "source_sha256": current, "artifacts": artifacts}
    if compile_pdf and not errors and len(artifacts) == 2:
        manifest.write_text(json.dumps(expected, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        audit["written"] = True
    try:
        recorded = json.loads(manifest.read_text(encoding="utf-8"))
        if recorded != expected or len(artifacts) != 2:
            errors.append("english_source_build_manifest_mismatch")
        else:
            audit["status"] = "pass"
    except (OSError, ValueError):
        errors.append("english_source_build_manifest_missing")
    return audit


def verify(root: Path, *, compile_pdf: bool) -> dict[str, Any]:
    root = root.resolve()
    build = (root / BUILD_DIRECTORY).resolve()
    pdf, log = build / "paper_en.pdf", build / "paper_en.log"
    errors: list[str] = []
    before = zh._scientific_source_hashes(root) if compile_pdf else None
    if compile_pdf:
        build.mkdir(parents=True, exist_ok=True)
        pdf.unlink(missing_ok=True)
        (build / "source_build_manifest.json").unlink(missing_ok=True)
    translation = audit_translation(root, errors)
    shared_figure = audit_shared_figure(root, errors)
    compiled: dict[str, Any] = {"requested": False}
    if compile_pdf and not errors:
        compiled = compile_english(root, build)
        if compiled["returncode"] != 0 or not compiled["pdf_exists"]:
            errors.append("english_xelatex_compile_failed")
    log_findings = {}
    if log.is_file():
        text = log.read_text(encoding="utf-8", errors="replace")
        for name, pattern in zh.LOG_FAILURE_PATTERNS.items():
            count = len(re.findall(pattern, text, re.I))
            log_findings[name] = count
            if count:
                errors.append(f"english_latex_{name}:{count}")
    else:
        errors.append("english_log_missing")
    pages = None
    reference_pages = []
    if pdf.is_file():
        try:
            pages = zh._pdf_pages(pdf)
            result = zh._run(["pdftotext", "-layout", str(pdf), "-"], cwd=root)
            if result.returncode != 0:
                errors.append("english_pdf_text_extraction_failed")
            else:
                if HAN.search(result.stdout):
                    errors.append("english_pdf_contains_chinese")
                reference_pages = reference_heading_pages(result.stdout)
                if reference_pages != [pages]:
                    errors.append("english_references_not_on_last_page")
        except RuntimeError as error:
            errors.append(str(error))
    else:
        errors.append("english_pdf_missing")
    main = root / "paper_en.tex"
    if main.is_file():
        text = zh._strip_tex_comments(main.read_text(encoding="utf-8"))
        if not re.search(r"\\clearpage(?:(?!\\input).)*?\\bibliography\{references\}", text, re.S):
            errors.append("english_references_require_separate_page")
    manifest = source_binding(root, build, compile_pdf=compile_pdf, before=before, errors=errors)
    return {
        "protocol": "paper-en-qa-v1", "status": "pass" if not errors else "fail",
        "errors": errors, "page_count": pages, "references_pages": reference_pages,
        "translation_correspondence": translation, "shared_figure": shared_figure,
        "compile": compiled, "latex_log_findings": log_findings,
        "source_build_manifest": manifest,
        "artifacts": {zh._reported_relative_path(pdf, root): {"sha256": zh._sha256(pdf)}} if pdf.is_file() else {},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--compile", action="store_true")
    args = parser.parse_args()
    payload = verify(args.root, compile_pdf=args.compile)
    output = (args.root.resolve() / BUILD_DIRECTORY).resolve()
    output.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    (output / "final_paper_qa.json").write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if payload["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
