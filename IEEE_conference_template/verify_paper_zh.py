#!/usr/bin/env python3
"""Fail-closed QA for the Chinese IEEE manuscript.

The script deliberately distinguishes the current drafting state from the
final paper state.  ``--allow-experiment-placeholders`` is only for work in
progress; the default rejects every remaining experiment placeholder while
still allowing the explicitly authorised author/affiliation/funding fields.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


CORE_FIGURES = (
    "architecture_preliminaries.tex",
    "baseline_motivation.tex",
    "overall_framework.tex",
    "joint_ga.tex",
    "physical_lookahead.tex",
    "experimental_summary.tex",
)
CORE_FIGURE_PANEL_MARKERS = {
    "architecture_preliminaries.tex": ("(a)", "(b)", "(c)", "(d)"),
    "baseline_motivation.tex": ("(a)", "(b)", "(c)"),
    "overall_framework.tex": ("(a)", "(b)", "(c)", "(d)", "(e)", "(f)"),
    "joint_ga.tex": ("(a)", "(b)", "(c)"),
    "physical_lookahead.tex": ("(a)", "(b)", "(c)"),
    "experimental_summary.tex": (
        "(a) Overall fidelity improvement",
        "(b) Selected circuit improvements"),
}
CORE_FIGURE_SEMANTIC_MARKERS = {
    "architecture_preliminaries.tex": {
        "atom_identities": ("q_0", "q_1", "q_2", "q_3"),
        "three_constraints": (
            "Non-crossing constraint", "Preservation constraint",
            "Ghost-spot constraint"),
        "serialized_batches": ("$B_1$", "$B_2$"),
    },
    "baseline_motivation.tex": {
        "four_real_gate_layers": (
            r"\mathrm{CZ}(q,p)", r"\mathrm{CZ}(u,v)",
            r"\mathrm{CZ}(w,x)", r"\mathrm{CZ}(q,r)"),
        "storage_sites": ("s_1", "s_2"),
        "path_outcomes": (
            "direct path infeasible", "reselect site or add waypoint",
            "direct path feasible"),
    },
    "overall_framework.tex": {
        "five_atom_identities": ("q_0", "q_1", "q_2", "q_3", "q_4"),
        "physical_states": (r"\pi_0", r"\pi_\ell", r"\pi'_\ell"),
        "real_zair_sequence": (
            "init", "1qGate q0,q3", "rearrangeJob B1",
            "rearrangeJob B2", "rydberg G1", "1qGate q2,q3"),
    },
    "joint_ga.tex": {
        "tracked_atoms": ("q_3", "q_4"),
        "joint_variables": (r"z_{\ell,1}", r"z_{\ell,2}", r"b_{\ell,1}"),
        "solver_regimes": ("Exact enumeration", "Budgeted genetic search"),
        "ordered_execution": (
            "Ordered physical evaluation", "phase I", "phase II",
            r"s_\ell^{\rm sto}", r"\chi_{\rm AOD}"),
    },
    "physical_lookahead.tex": {
        "five_atom_context": ("q_0", "q_1", "q_2", "q_3", "q_4"),
        "compatible_batches": (r"\mathcal B_1", r"\mathcal B_2"),
        "candidate_states": (r"A:\ s_{\ell+1}", r"B:\ s_{\ell+1}"),
        "real_gate_pairs": (r"\mathrm{CZ}(q,r)", r"\mathrm{CZ}(q,p)"),
        "decay_weight": (r"w_d=\alpha\rho^{d-1}",),
    },
    "experimental_summary.tex": {
        "direct_aggregate_ratios": (
            r"\FigSixZACvsZAC", r"\FigSixZACvsICCAD",
            r"\FigSixQMAPvsZAC", r"\FigSixQMAPvsICCAD"),
        "selected_circuit_ratios": (
            "fig6_selected_cases.dat", "ratio_zac", "ratio_iccad"),
        "benchmark_identity": ("ZAC18", "QMAP154"),
    },
}
CORE_FIGURE_FORBIDDEN_MARKERS = {
    "overall_framework.tex": (r"d_{\min}", r"d_{\mathrm{safe}}"),
    "joint_ga.tex": (r"d_{\min}", r"d_{\mathrm{safe}}"),
}
FIG_SIX_SOURCE = "experimental_summary.tex"
FIG_SIX_FORBIDDEN_RENDER_PATTERNS = {
    "includegraphics": re.compile(r"\\includegraphics\b"),
    "addplot_graphics": re.compile(
        r"\\addplot(?:3)?\+?(?:\s*\[[^\]]*\])?\s+graphics\b"),
    "pgf_image": re.compile(
        r"\\pgf(?:declareimage|useimage|image)\b"),
    "svg_or_pdf_include": re.compile(r"\\include(?:svg|pdf)\b"),
    "tikz_externalization": re.compile(
        r"\\tikz(?:external(?:ize|enable|disable)|setexternalprefix)\b"),
    "external_library": re.compile(
        r"\\use(?:tikz|pgfplots)library\s*\{[^}]*\bexternal\b[^}]*\}"),
    "external_image_path": re.compile(
        r"\{[^{}\r\n]*\.(?:pdf(?:_tex)?|png|jpe?g|eps|svg)\}"),
}
FIG_SIX_REQUIRED_STRUCTURE_PATTERNS = {
    "tikz_container": re.compile(
        r"\\begin\s*\{\s*tikzpicture\s*\}|\\tikz(?![A-Za-z@])"),
    "pgfplots_axis": re.compile(
        r"\\begin\s*\{\s*(?:axis|groupplot|semilogxaxis|semilogyaxis|"
        r"loglogaxis|polaraxis|smithchart|ternaryaxis)\s*\}"),
    "pgfplots_plot": re.compile(r"\\addplot(?:3)?\+?(?![A-Za-z@])"),
}
OVERALL_FIGURE_SOURCE = Path("figures/overall_framework.tex")
OVERALL_FIGURE_WRAPPER = Path("figures/overall_framework_standalone.tex")
BUILD_DIRECTORY = Path("../build/paper_zh")
OVERALL_FIGURE_PDF = BUILD_DIRECTORY / "figures/overall_framework.pdf"
SOURCE_MANIFEST_NAME = "source_build_manifest.json"
SCIENTIFIC_SOURCE_SUFFIXES = {".tex", ".bib", ".cls", ".sty", ".dat", ".bst"}
SOURCE_EXCLUDED_DIRECTORIES = {
    ".git", "build", "tmp", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".venv", "venv", "node_modules",
}
OVERALL_FIGURE_ZAIR_TOKENS = (
    "init",
    "1qGate",
    "rearrangeJob",
    "rydberg",
)
FIG_SIX_DERIVED = (
    "fig6_zac_fidelity.dat",
    "fig6_qmap_fidelity.dat",
    "fig6_mechanism.dat",
    "fig6_ablation.dat",
    "fig6_stage_time.dat",
    "fig6_meta.tex",
)
FIG_SIX_AUX_DERIVED = (
    "fig6_selected_cases.dat",
    "fig6_selected_cases.tex",
)
EXPECTED_EVIDENCE_PROTOCOL = "paper-zh-v2-final-manifest-v1"
EXPECTED_VALUES_PROTOCOL = "paper-zh-v2-values-v1"
EVIDENCE_ROOT_LABEL = "$EVIDENCE_ROOT"
EVIDENCE_MANIFEST_LABEL = "$EVIDENCE_MANIFEST"
FORBIDDEN_EXPERIMENT_SCOPES = ("QASMBench", "Large")
FORBIDDEN_FINAL_PDF_TOKENS = ("Fig. 6待补", "待补实验")
FORBIDDEN_PDF_METHOD_LABEL = re.compile(
    r"(?<!Apple )(?<![A-Za-z0-9_])M[ \t\u00a0]*([1-4])"
    r"(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
FORBIDDEN_PDF_TONE_PATTERNS = {
    "result_label": re.compile(r"结\s*果\s*标\s*签\s*为"),
    "current_manifest": re.compile(r"当\s*前\s*manifest", re.IGNORECASE),
    "pre_experiment_freeze": re.compile(r"补\s*实\s*验\s*前\s*冻\s*结"),
    "coverage_terminal": re.compile(r"覆\s*盖\s*终\s*态"),
    "paper_reporting_instruction": re.compile(r"论\s*文\s*只\s*报\s*告"),
    "stale_artifact": re.compile(r"旧\s*产\s*物"),
    "silent_fallback": re.compile(r"不\s*静\s*默"),
    "strict_timing_conclusion": re.compile(
        r"严\s*格\s*计\s*时\s*结\s*论\s*为"),
    "implementation_event_code": re.compile(
        r"(?<![A-Za-z])(?:LOAD|MOVE|STORE|STAY|RETURN|RESEAT)"
        r"(?![A-Za-z])", re.IGNORECASE),
    "implementation_replay_term": re.compile(
        r"(?:物\s*理|真\s*实|候\s*选)\s*重\s*放|ghost_hits|"
        r"ghost-safe|cost-to-go",
        re.IGNORECASE),
    "defensive_novelty_disclaimer": re.compile(
        r"本\s*文\s*不\s*重\s*新\s*提\s*出|"
        r"而\s*是\s*在\s*同\s*一\s*候\s*选"),
    "meta_supports_effect": re.compile(
        r"(?:结\s*果|对\s*照|比\s*较).{0,24}支\s*持.{0,24}作\s*用"),
    "meta_provides_evidence": re.compile(
        r"(?:结\s*果|对\s*照|比\s*较).{0,24}提\s*供.{0,16}证\s*据"),
    "report_results_listed": re.compile(
        r"(?:两|各)\s*组\s*结\s*果.{0,12}列\s*于\s*表"),
    "report_remaining_settings": re.compile(
        r"其\s*余\s*设\s*置.{0,20}列\s*于.{0,8}表"),
    "report_examines_effect": re.compile(
        r"分\s*别\s*考\s*察.{0,24}作\s*用"),
    "empty_visual_inference": re.compile(
        r"由\s*此\s*可\s*见|可\s*以\s*看\s*出|"
        r"值\s*得\s*注\s*意\s*的\s*是|"
        r"进\s*一\s*步\s*验\s*证|充\s*分\s*说\s*明"),
}
PARAGRAPH_LEADING_VISUAL_REFERENCE = re.compile(
    r"(?:\A|\n[ \t]*\n)[ \t]*(?P<lead>"
    r"(?:如[ \t]*)?(?:(?:图|表|算法)[ \t]*~?[ \t]*\\ref\b|"
    r"(?:图|表)[ \t]*中))")
LOG_FAILURE_PATTERNS = {
    "undefined_reference": r"There were undefined references|Reference .* undefined",
    "undefined_citation": r"Citation .* undefined|There were undefined citations",
    "overfull_box": r"Overfull \\hbox|Overfull \\vbox",
    "float_too_large": r"Float too large",
}


def _run(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, cwd=cwd, check=False, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def _pdf_pages(pdf: Path) -> int:
    result = _run(["pdfinfo", str(pdf)], cwd=pdf.parent)
    if result.returncode != 0:
        raise RuntimeError(f"pdfinfo failed: {result.stdout[-1000:]}")
    match = re.search(r"^Pages:\s+(\d+)\s*$", result.stdout, re.MULTILINE)
    if not match:
        raise RuntimeError("pdfinfo did not report a page count")
    return int(match.group(1))


def _build_overall_figure(root: Path) -> tuple[dict[str, Any], bool]:
    """Force a fresh XeLaTeX build of the externally included overview PDF."""
    wrapper = root / OVERALL_FIGURE_WRAPPER
    pdf = (root / OVERALL_FIGURE_PDF).resolve()
    command = [
        "latexmk", "-g", "-xelatex", "-interaction=nonstopmode",
        "-halt-on-error", "-jobname=overall_framework",
        f"-outdir={pdf.parent}", f"-auxdir={pdf.parent}",
        str(OVERALL_FIGURE_WRAPPER),
    ]
    audit: dict[str, Any] = {
        "requested": True,
        "wrapper": str(OVERALL_FIGURE_WRAPPER),
        "command": command,
        "returncode": None,
        "output_tail": "",
        "pdf_exists_after_build": False,
    }
    # This generated target belongs to this build. Remove it before invoking
    # XeLaTeX so a failed rebuild cannot satisfy the existence check with an
    # earlier figure, even when the wrapper itself is missing.
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.unlink(missing_ok=True)
    if not wrapper.is_file():
        audit["output_tail"] = "standalone wrapper is missing"
        return audit, False

    result = _run(command, cwd=root)
    audit["returncode"] = result.returncode
    audit["output_tail"] = result.stdout[-3000:]
    audit["pdf_exists_after_build"] = pdf.is_file()
    if result.returncode != 0:
        pdf.unlink(missing_ok=True)
    return audit, result.returncode == 0 and pdf.is_file()


def _audit_overall_figure_pdf(root: Path, *,
                              errors: list[str]) -> dict[str, Any]:
    """Audit the standalone vector PDF and its explicit manuscript inclusion."""
    source = root / OVERALL_FIGURE_SOURCE
    wrapper = root / OVERALL_FIGURE_WRAPPER
    pdf = (root / OVERALL_FIGURE_PDF).resolve()
    method = root / "sections" / "03_method.tex"
    method_text = (
        method.read_text(encoding="utf-8") if method.is_file() else "")
    uses_external_pdf = bool(re.search(
        r"\\includegraphics(?:\[[^\]]*\])?\s*"
        r"\{\.\./build/paper_zh/figures/overall_framework\.pdf\}", method_text))
    directly_inputs_source = bool(re.search(
        r"\\input\s*\{figures/overall_framework(?:\.tex)?\}", method_text))

    audit: dict[str, Any] = {
        "source": str(OVERALL_FIGURE_SOURCE),
        "source_exists": source.is_file(),
        "wrapper": str(OVERALL_FIGURE_WRAPPER),
        "wrapper_exists": wrapper.is_file(),
        "pdf": str(OVERALL_FIGURE_PDF),
        "pdf_exists": pdf.is_file(),
        "method_uses_external_pdf": uses_external_pdf,
        "method_directly_inputs_source": directly_inputs_source,
        "pages": None,
        "raster_image_count": None,
        "font_count": None,
        "unembedded_fonts": [],
        "contains_han": None,
        "required_zair_tokens": list(OVERALL_FIGURE_ZAIR_TOKENS),
        "missing_zair_tokens": list(OVERALL_FIGURE_ZAIR_TOKENS),
    }
    for label, path in (
            ("source", source), ("wrapper", wrapper), ("pdf", pdf)):
        if not path.is_file():
            errors.append(f"missing_overall_figure_{label}:{path.name}")
    if not method.is_file():
        errors.append("missing_method_section:03_method.tex")
    else:
        if not uses_external_pdf:
            errors.append("overall_figure_pdf_not_included_by_method")
        if directly_inputs_source:
            errors.append("overall_figure_source_still_directly_input_by_method")
    if not pdf.is_file():
        return audit

    try:
        pages = _pdf_pages(pdf)
    except RuntimeError as error:
        errors.append(f"overall_figure_{error}")
    else:
        audit["pages"] = pages
        if pages != 1:
            errors.append(f"overall_figure_unexpected_page_count:{pages}!=1")

    images = _run(["pdfimages", "-list", str(pdf)], cwd=root)
    if images.returncode != 0:
        errors.append("overall_figure_pdfimages_failed")
    else:
        image_rows = [
            line for line in images.stdout.splitlines()
            if re.match(r"^\s*\d+\s+\d+\s+\S+", line)
        ]
        audit["raster_image_count"] = len(image_rows)
        if image_rows:
            errors.append(
                f"overall_figure_contains_raster_images:{len(image_rows)}")

    fonts = _run(["pdffonts", str(pdf)], cwd=root)
    if fonts.returncode != 0:
        errors.append("overall_figure_pdffonts_failed")
    else:
        font_rows = [
            line for line in fonts.stdout.splitlines()[2:] if line.strip()
        ]
        unembedded: list[str] = []
        malformed_rows: list[str] = []
        for line in font_rows:
            match = re.search(
                r"\s+(yes|no)\s+(yes|no)\s+(yes|no)\s+\d+\s+\d+\s*$",
                line)
            if match is None:
                malformed_rows.append(line)
            elif match.group(1) != "yes":
                unembedded.append(line.split()[0])
        audit["font_count"] = len(font_rows)
        audit["unembedded_fonts"] = unembedded
        if not font_rows:
            errors.append("overall_figure_has_no_fonts")
        if malformed_rows:
            errors.append(
                f"overall_figure_pdffonts_unparsed_rows:{len(malformed_rows)}")
        if unembedded:
            errors.append(
                "overall_figure_unembedded_fonts:" + ",".join(unembedded))

    extracted = _run(["pdftotext", "-layout", str(pdf), "-"], cwd=root)
    if extracted.returncode != 0:
        errors.append("overall_figure_pdftotext_failed")
    else:
        contains_han = bool(re.search(r"[\u3400-\u9fff]", extracted.stdout))
        missing_tokens = [
            token for token in OVERALL_FIGURE_ZAIR_TOKENS
            if token not in extracted.stdout
        ]
        audit["contains_han"] = contains_han
        audit["missing_zair_tokens"] = missing_tokens
        if contains_han:
            errors.append("overall_figure_pdf_contains_han")
        for token in missing_tokens:
            errors.append(f"overall_figure_missing_zair_token:{token}")
    return audit


def _tex_integer(text: str, name: str, *, definition: bool) -> int | None:
    """Read a simple integer result macro from generated TeX."""
    if definition:
        pattern = rf"\\def\\{re.escape(name)}\{{(\d+)\}}"
    else:
        pattern = rf"\\newcommand\{{\\{re.escape(name)}\}}\{{(\d+)\}}"
    match = re.search(pattern, text)
    return int(match.group(1)) if match else None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _scientific_source_hashes(root: Path) -> dict[str, str]:
    """Content identity of every local scientific source, independent of mtime."""
    sources: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if (path.suffix.lower() not in SCIENTIFIC_SOURCE_SUFFIXES
                or any(part in SOURCE_EXCLUDED_DIRECTORIES for part in relative.parts)):
            continue
        if path.is_symlink():
            raise ValueError(f"symbolic_link_scientific_source:{relative.as_posix()}")
        if path.is_file():
            sources[relative.as_posix()] = _sha256(path)
    return sources


def _source_build_manifest(root: Path, *, compile_pdf: bool,
                           source_before: dict[str, str] | None,
                           errors: list[str]) -> dict[str, Any]:
    """Only a successful, source-stable compile may create a source/PDF binding."""
    build = (root / BUILD_DIRECTORY).resolve()
    manifest = build / SOURCE_MANIFEST_NAME
    audit: dict[str, Any] = {
        "path": _reported_relative_path(manifest, root), "status": "fail",
        "written_by_this_compile": False,
    }
    try:
        current_sources = _scientific_source_hashes(root)
        current_artifacts = {
            name: {"sha256": _sha256(build / name)}
            for name in ("paper_zh.pdf", "figures/overall_framework.pdf")
            if (build / name).is_file()
        }
        audit["source_count"] = len(current_sources)
        if compile_pdf:
            if source_before != current_sources:
                errors.append("scientific_sources_changed_during_compile")
            # Never preserve an earlier binding after a failed compile.
            manifest.unlink(missing_ok=True)
            if not errors:
                if len(current_artifacts) != 2:
                    errors.append("source_manifest_missing_built_pdf")
                else:
                    payload = {
                        "protocol": "paper-source-build-v1",
                        "source_sha256": current_sources,
                        "artifacts": current_artifacts,
                    }
                    temporary = manifest.with_suffix(".json.tmp")
                    temporary.write_text(
                        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                        encoding="utf-8")
                    temporary.replace(manifest)
                    audit["written_by_this_compile"] = True
        if not manifest.is_file():
            errors.append("source_build_manifest_missing")
            return audit
        recorded = json.loads(manifest.read_text(encoding="utf-8"))
        if (recorded.get("protocol") != "paper-source-build-v1"
                or recorded.get("source_sha256") != current_sources
                or recorded.get("artifacts") != current_artifacts
                or len(current_artifacts) != 2):
            errors.append("source_build_manifest_mismatch")
            return audit
        audit["status"] = "pass"
        audit["sha256"] = _sha256(manifest)
    except (OSError, ValueError, TypeError, AttributeError) as error:
        errors.append(f"source_build_manifest_error:{error}")
    return audit


def _is_escaped(text: str, index: int) -> bool:
    backslashes = 0
    index -= 1
    while index >= 0 and text[index] == "\\":
        backslashes += 1
        index -= 1
    return bool(backslashes % 2)


def _strip_tex_comments(text: str) -> str:
    """Remove active TeX comments while preserving escaped percent signs."""
    active_lines: list[str] = []
    for line in text.splitlines():
        for index, char in enumerate(line):
            if char == "%" and not _is_escaped(line, index):
                line = line[:index]
                break
        active_lines.append(line)
    return "\n".join(active_lines)


def _parse_result_macros(text: str) -> tuple[dict[str, str], list[str], list[str]]:
    """Parse generated ``\\newcommand{\\Name}{value}`` definitions.

    Values can contain nested TeX groups (for example ``\\textemdash{}``), so a
    single regular expression is not sufficient for the value body.
    """
    prefix = re.compile(r"\\newcommand\s*\{\s*\\([A-Za-z@]+)\s*\}\s*\{")
    macros: dict[str, str] = {}
    duplicates: list[str] = []
    malformed: list[str] = []
    for match in prefix.finditer(text):
        name = match.group(1)
        start = match.end()
        depth = 1
        end: int | None = None
        for index in range(start, len(text)):
            char = text[index]
            if char == "{" and not _is_escaped(text, index):
                depth += 1
            elif char == "}" and not _is_escaped(text, index):
                depth -= 1
                if depth == 0:
                    end = index
                    break
        if end is None:
            malformed.append(name)
            continue
        if name in macros:
            duplicates.append(name)
        macros[name] = text[start:end]
    return macros, sorted(set(duplicates)), sorted(set(malformed))


def _compare_macros(expected: dict[str, str], actual: dict[str, str]) -> dict[str, Any]:
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    mismatches = [
        {
            "name": name,
            "expected": expected[name],
            "actual": actual[name],
        }
        for name in sorted(set(expected) & set(actual))
        if expected[name] != actual[name]
    ]
    return {
        "expected_count": len(expected),
        "actual_count": len(actual),
        "missing": missing,
        "extra": extra,
        "value_mismatches": mismatches,
        "matched": not missing and not extra and not mismatches,
    }


def _reported_relative_path(path: Path, root: Path) -> str:
    """Return a portable display path without changing the path used for QA."""
    return Path(os.path.relpath(path.resolve(), root.resolve())).as_posix()


def _reported_evidence_path(path: Path, evidence_root: Path) -> str:
    """Describe evidence paths without recording a machine-specific prefix."""
    try:
        relative = path.resolve().relative_to(evidence_root.resolve())
    except ValueError:
        return EVIDENCE_MANIFEST_LABEL
    if relative == Path("."):
        return EVIDENCE_ROOT_LABEL
    return f"{EVIDENCE_ROOT_LABEL}/{relative.as_posix()}"


def _discover_evidence_root(manuscript_root: Path) -> tuple[Path | None, list[str]]:
    """Locate the companion paper_zh_v2 bundle without a machine-specific path."""
    candidates: list[Path] = []
    for ancestor in (manuscript_root, *manuscript_root.parents):
        candidates.extend((
            ancestor / "results" / "paper_zh_v2",
            ancestor / "ZAC_zzx" / "results" / "paper_zh_v2",
        ))
    unique_candidates: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique_candidates.append(resolved)
    checked_candidates: list[str] = []
    for candidate in unique_candidates:
        checked_candidates.append(
            _reported_relative_path(candidate, manuscript_root))
        if ((candidate / "final_manifest.json").is_file() and
                (candidate / "paper_values.json").is_file()):
            return candidate, checked_candidates
    return None, checked_candidates


def _audit_declared_hash(path: Path, declaration: Any, *, label: str,
                         reported_path: str,
                         errors: list[str]) -> dict[str, Any]:
    declared_sha256 = (
        declaration.get("sha256") if isinstance(declaration, dict) else None)
    declared_bytes = (
        declaration.get("bytes") if isinstance(declaration, dict) else None)
    audit: dict[str, Any] = {
        "path": reported_path,
        "exists": path.is_file(),
        "declared_sha256": declared_sha256,
        "actual_sha256": None,
        "hash_matched": False,
    }
    if not isinstance(declared_sha256, str):
        errors.append(f"manifest_sha256_missing:{label}")
    if not path.is_file():
        errors.append(f"evidence_file_missing:{label}")
        return audit
    actual_sha256 = _sha256(path)
    audit["actual_sha256"] = actual_sha256
    audit["hash_matched"] = (
        isinstance(declared_sha256, str) and
        actual_sha256.lower() == declared_sha256.lower())
    if not audit["hash_matched"]:
        errors.append(f"evidence_hash_mismatch:{label}")
    if isinstance(declared_bytes, int):
        actual_bytes = path.stat().st_size
        audit.update({
            "declared_bytes": declared_bytes,
            "actual_bytes": actual_bytes,
            "bytes_matched": actual_bytes == declared_bytes,
        })
        if actual_bytes != declared_bytes:
            errors.append(f"evidence_size_mismatch:{label}")
    return audit


def _audit_evidence(manuscript_root: Path, manuscript_macros: Path, *,
                    evidence_root: Path | None,
                    evidence_manifest: Path | None,
                    errors: list[str]) -> dict[str, Any]:
    initial_error_count = len(errors)
    discovery_candidates: list[str] = []
    discovery = "explicit"
    if evidence_root is None and evidence_manifest is not None:
        evidence_root = evidence_manifest.resolve().parent
    elif evidence_root is None:
        evidence_root, discovery_candidates = _discover_evidence_root(
            manuscript_root)
        discovery = "auto"
    else:
        evidence_root = evidence_root.resolve()

    if evidence_manifest is None and evidence_root is not None:
        evidence_manifest = evidence_root / "final_manifest.json"
    elif evidence_manifest is not None:
        evidence_manifest = evidence_manifest.resolve()

    audit: dict[str, Any] = {
        "status": "fail",
        "discovery": discovery,
        "root": EVIDENCE_ROOT_LABEL if evidence_root is not None else None,
        "manifest": (
            _reported_evidence_path(evidence_manifest, evidence_root)
            if evidence_manifest is not None and evidence_root is not None
            else None),
        "manifest_sha256": None,
        "discovery_candidates": discovery_candidates,
        "manifest_protocol": None,
        "paper_values_protocol": None,
        "hashes": {},
        "macro_consistency": {},
    }
    if evidence_root is None:
        errors.append("paper_evidence_root_not_found")
        return audit
    if evidence_manifest is None or not evidence_manifest.is_file():
        errors.append("paper_evidence_manifest_not_found")
        return audit

    try:
        manifest = json.loads(evidence_manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        errors.append(f"paper_evidence_manifest_invalid:{type(error).__name__}")
        return audit
    if not isinstance(manifest, dict):
        errors.append("paper_evidence_manifest_invalid:root_not_object")
        return audit
    audit["manifest_sha256"] = _sha256(evidence_manifest)
    audit["manifest_protocol"] = manifest.get("protocol")
    if audit["manifest_protocol"] != EXPECTED_EVIDENCE_PROTOCOL:
        errors.append(
            "paper_evidence_manifest_protocol_mismatch:"
            f"{audit['manifest_protocol']}!={EXPECTED_EVIDENCE_PROTOCOL}")

    manifest_files = manifest.get("files")
    if not isinstance(manifest_files, dict):
        manifest_files = {}
        errors.append("paper_evidence_manifest_invalid:files_not_object")
    paper_values_path = evidence_root / "paper_values.json"
    evidence_macros_path = evidence_root / "results_values_zh.tex"
    delivery_hashes: dict[str, Any] = {}
    for name, declaration in sorted(manifest_files.items()):
        if not isinstance(name, str) or Path(name).name != name:
            errors.append(f"paper_evidence_manifest_invalid:file_name:{name}")
            continue
        delivery_hashes[name] = _audit_declared_hash(
            evidence_root / name, declaration,
            label=f"delivery:{name}",
            reported_path=f"{EVIDENCE_ROOT_LABEL}/{name}", errors=errors)
    audit["hashes"]["delivery_files"] = delivery_hashes
    audit["hashes"]["manuscript_results_values_zh.tex"] = (
        _audit_declared_hash(
            manuscript_macros, manifest.get("paper_macro_target"),
            label="manuscript_results_values_zh.tex",
            reported_path=_reported_relative_path(
                manuscript_macros, manuscript_root), errors=errors))

    fig6_declarations = manifest.get("paper_fig6_data")
    if isinstance(fig6_declarations, dict):
        fig6_declarations = fig6_declarations.get("files")
    if not isinstance(fig6_declarations, dict):
        fig6_declarations = {}
        errors.append("paper_evidence_manifest_invalid:fig6_files_not_object")
    fig6_hashes: dict[str, Any] = {}
    for name in (*FIG_SIX_DERIVED, *FIG_SIX_AUX_DERIVED):
        fig6_hashes[name] = _audit_declared_hash(
            manuscript_root / "figures" / "data" / name,
            fig6_declarations.get(name), label=f"fig6:{name}",
            reported_path=f"figures/data/{name}", errors=errors)
    audit["hashes"]["fig6"] = fig6_hashes

    expected_macros: dict[str, str] | None = None
    if paper_values_path.is_file():
        try:
            values_payload = json.loads(
                paper_values_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            errors.append(f"paper_values_invalid:{type(error).__name__}")
        else:
            audit["paper_values_protocol"] = (
                values_payload.get("protocol")
                if isinstance(values_payload, dict) else None)
            if audit["paper_values_protocol"] != EXPECTED_VALUES_PROTOCOL:
                errors.append(
                    "paper_values_protocol_mismatch:"
                    f"{audit['paper_values_protocol']}!="
                    f"{EXPECTED_VALUES_PROTOCOL}")
            raw_macros = (
                values_payload.get("macros")
                if isinstance(values_payload, dict) else None)
            if (not isinstance(raw_macros, dict) or
                    any(not isinstance(name, str) or not isinstance(value, str)
                        for name, value in raw_macros.items())):
                errors.append("paper_values_invalid:macros_not_string_map")
            else:
                expected_macros = raw_macros

    for role, path in (
            ("manuscript", manuscript_macros),
            ("evidence_copy", evidence_macros_path)):
        if expected_macros is None or not path.is_file():
            continue
        parsed, duplicates, malformed = _parse_result_macros(
            path.read_text(encoding="utf-8"))
        comparison = _compare_macros(expected_macros, parsed)
        comparison["duplicates"] = duplicates
        comparison["malformed"] = malformed
        comparison["matched"] = (
            comparison["matched"] and not duplicates and not malformed)
        audit["macro_consistency"][role] = comparison
        if not comparison["matched"]:
            errors.append(f"paper_values_macro_mismatch:{role}")

    audit["status"] = (
        "pass" if len(errors) == initial_error_count else "fail")
    return audit


def _submission_placeholders(root: Path,
                             manuscript_files: list[Path]) -> list[dict[str, Any]]:
    pattern = re.compile(r"\\todoauthor\s*\{([^{}]*)\}")
    placeholders: list[dict[str, Any]] = []
    for path in manuscript_files:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            placeholders.append({
                "field": match.group(1).strip(),
                "file": str(path.relative_to(root)),
                "line": text.count("\n", 0, match.start()) + 1,
            })
    return placeholders


def _paragraph_leading_visual_references(
        root: Path, manuscript_files: list[Path]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for path in manuscript_files:
        if not path.is_file():
            continue
        text = _strip_tex_comments(path.read_text(encoding="utf-8"))
        for match in PARAGRAPH_LEADING_VISUAL_REFERENCE.finditer(text):
            findings.append({
                "lead": match.group("lead"),
                "file": str(path.relative_to(root)),
                "line": text.count("\n", 0, match.start("lead")) + 1,
            })
    return findings


def verify(root: Path, *, compile_pdf: bool,
           allow_experiment_placeholders: bool,
           expected_pages: int | None,
           evidence_root: Path | None = None,
           evidence_manifest: Path | None = None) -> dict[str, Any]:
    root = root.resolve()
    build_directory = (root / BUILD_DIRECTORY).resolve()
    pdf = build_directory / "paper_zh.pdf"
    errors: list[str] = []
    warnings: list[str] = []
    source_before = _scientific_source_hashes(root) if compile_pdf else None
    overall_figure_build: dict[str, Any] = {
        "requested": False,
        "wrapper": str(OVERALL_FIGURE_WRAPPER),
        "pdf": str(OVERALL_FIGURE_PDF),
    }
    if compile_pdf:
        build_directory.mkdir(parents=True, exist_ok=True)
        # A compile request must never leave an earlier main PDF looking like
        # the result of a failed figure or manuscript rebuild.
        pdf.unlink(missing_ok=True)
        (build_directory / SOURCE_MANIFEST_NAME).unlink(missing_ok=True)
        overall_figure_build, figure_build_ok = _build_overall_figure(root)
        if not figure_build_ok:
            errors.append("overall_figure_xelatex_compile_failed")
            warnings.append(overall_figure_build["output_tail"])
        else:
            result = _run([
                "latexmk", "-g", "-xelatex", "-interaction=nonstopmode",
                "-halt-on-error", f"-outdir={build_directory}",
                f"-auxdir={build_directory}", "paper_zh.tex"], cwd=root)
            if result.returncode != 0:
                pdf.unlink(missing_ok=True)
                errors.append("xelatex_compile_failed")
                warnings.append(result.stdout[-3000:])

    main = root / "paper_zh.tex"
    log = build_directory / "paper_zh.log"
    macros = root / "results_values_zh.tex"
    required = [main, log, pdf, macros]
    for path in required:
        if not path.is_file():
            errors.append(f"missing_required_file:{path.name}")

    manuscript_files = [main, *sorted((root / "sections").glob("*.tex"))]
    manuscript_text = "\n".join(
        path.read_text(encoding="utf-8") for path in manuscript_files
        if path.is_file())
    submission_placeholders = _submission_placeholders(
        root, manuscript_files)
    paragraph_leading_visual_references = (
        _paragraph_leading_visual_references(root, manuscript_files))
    for finding in paragraph_leading_visual_references:
        errors.append(
            "paragraph_leading_visual_reference:"
            f"{finding['file']}:{finding['line']}")
    active_manuscript_text = _strip_tex_comments(manuscript_text)
    for token in FORBIDDEN_EXPERIMENT_SCOPES:
        if re.search(
                rf"(?<![A-Za-z]){re.escape(token)}(?![A-Za-z])",
                active_manuscript_text):
            errors.append(f"forbidden_experiment_scope:{token}")

    figure_audit: dict[str, Any] = {}
    for name in CORE_FIGURES:
        path = root / "figures" / name
        if not path.is_file():
            errors.append(f"missing_core_figure:{name}")
            continue
        text = path.read_text(encoding="utf-8")
        active_text = _strip_tex_comments(text)
        is_vector = "\\begin{tikzpicture}" in text
        embeds_raster = "\\includegraphics" in text
        contains_han = bool(re.search(r"[\u3400-\u9fff]", text))
        figure_audit[name] = {
            "tikz": is_vector,
            "includegraphics": embeds_raster,
            "contains_han": contains_han,
        }
        if not is_vector or embeds_raster:
            errors.append(f"non_tikz_core_figure:{name}")
        if contains_han:
            errors.append(f"non_english_core_figure:{name}")
        required_panels = CORE_FIGURE_PANEL_MARKERS.get(name, ())
        missing_panels = [
            marker for marker in required_panels if marker not in active_text
        ]
        semantic_checks = {
            label: {
                "required": list(markers),
                "missing": [
                    marker for marker in markers if marker not in active_text
                ],
            }
            for label, markers in
            CORE_FIGURE_SEMANTIC_MARKERS.get(name, {}).items()
        }
        forbidden_semantics = [
            marker for marker in CORE_FIGURE_FORBIDDEN_MARKERS.get(name, ())
            if marker in active_text
        ]
        figure_audit[name].update({
            "required_panel_markers": list(required_panels),
            "missing_panel_markers": missing_panels,
            "semantic_checks": semantic_checks,
            "forbidden_semantics": forbidden_semantics,
        })
        for marker in missing_panels:
            errors.append(f"core_figure_missing_panel:{name}:{marker}")
        for label, check in semantic_checks.items():
            for marker in check["missing"]:
                errors.append(
                    f"core_figure_missing_semantics:{name}:{label}:{marker}")
        for marker in forbidden_semantics:
            errors.append(f"core_figure_forbidden_semantics:{name}:{marker}")
        if name == FIG_SIX_SOURCE:
            forbidden_rendering = [
                label for label, pattern in
                FIG_SIX_FORBIDDEN_RENDER_PATTERNS.items()
                if pattern.search(active_text)
            ]
            required_structure = {
                label: bool(pattern.search(active_text))
                for label, pattern in
                FIG_SIX_REQUIRED_STRUCTURE_PATTERNS.items()
            }
            figure_audit[name].update({
                "forbidden_rendering": forbidden_rendering,
                "required_structure": required_structure,
            })
            for label in forbidden_rendering:
                errors.append(f"fig6_forbidden_render_path:{label}")
            for label, present in required_structure.items():
                if not present:
                    errors.append(f"fig6_missing_pgfplots_structure:{label}")

    overall_figure_pdf_audit = _audit_overall_figure_pdf(
        root, errors=errors)

    placeholder_count = 0
    if macros.is_file():
        macro_text = macros.read_text(encoding="utf-8")
        placeholder_count = (
            macro_text.count("\\ResultTODO") + macro_text.count("待补实验"))
        if placeholder_count and not allow_experiment_placeholders:
            errors.append(f"experiment_placeholders_remaining:{placeholder_count}")

    fig_six_data_audit: dict[str, bool] = {}
    for name in (*FIG_SIX_DERIVED, *FIG_SIX_AUX_DERIVED):
        exists = (root / "figures" / "data" / name).is_file()
        fig_six_data_audit[name] = exists
        if not exists and not allow_experiment_placeholders:
            errors.append(f"missing_final_fig6_data:{name}")

    fig_six_metadata_consistency: dict[str, Any] = {}
    meta_path = root / "figures" / "data" / "fig6_meta.tex"
    if (not allow_experiment_placeholders and macros.is_file() and
            meta_path.is_file()):
        macro_text = macros.read_text(encoding="utf-8")
        meta_text = meta_path.read_text(encoding="utf-8")
        for result_name, figure_name in (
                ("ZACStrictN", "FigSixZACN"),
                ("QMAPStrictN", "FigSixQMAPN"),
                ("AblationHN", "FigSixAblationHN"),
                ("AblationGAN", "FigSixAblationGAN")):
            result_value = _tex_integer(
                macro_text, result_name, definition=False)
            figure_value = _tex_integer(
                meta_text, figure_name, definition=True)
            matched = (result_value is not None and
                       figure_value is not None and
                       result_value == figure_value)
            fig_six_metadata_consistency[result_name] = {
                "result_macro": result_value,
                "figure_meta": figure_value,
                "matched": matched,
            }
            if not matched:
                errors.append(
                    f"fig6_metadata_mismatch:{result_name}:"
                    f"{result_value}!={figure_value}")

    evidence_audit = _audit_evidence(
        root, macros, evidence_root=evidence_root,
        evidence_manifest=evidence_manifest, errors=errors)

    log_findings: dict[str, int] = {}
    if log.is_file():
        log_text = log.read_text(encoding="utf-8", errors="replace")
        for label, pattern in LOG_FAILURE_PATTERNS.items():
            count = len(re.findall(pattern, log_text, flags=re.IGNORECASE))
            log_findings[label] = count
            if count:
                errors.append(f"latex_log_{label}:{count}")

    page_count = None
    references_on_last_page = False
    forbidden_pdf_tokens: list[str] = []
    forbidden_pdf_method_labels: dict[str, int] = {}
    forbidden_pdf_tone_phrases: list[str] = []
    if pdf.is_file():
        try:
            page_count = _pdf_pages(pdf)
            if expected_pages is not None and page_count != expected_pages:
                errors.append(
                    f"unexpected_page_count:{page_count}!={expected_pages}")
            result = _run([
                "pdftotext", "-f", str(page_count), "-l", str(page_count),
                str(pdf), "-"], cwd=root)
            references_on_last_page = (
                result.returncode == 0 and "REFERENCES" in result.stdout.upper())
            if not references_on_last_page:
                errors.append("references_not_detected_on_last_page")
            full_text = _run(["pdftotext", str(pdf), "-"], cwd=root)
            if full_text.returncode != 0:
                errors.append("pdftotext_full_document_failed")
            else:
                for match in FORBIDDEN_PDF_METHOD_LABEL.finditer(
                        full_text.stdout):
                    label = f"M{match.group(1)}"
                    forbidden_pdf_method_labels[label] = (
                        forbidden_pdf_method_labels.get(label, 0) + 1)
                for label, count in sorted(
                        forbidden_pdf_method_labels.items()):
                    errors.append(
                        f"forbidden_pdf_method_label:{label}:{count}")
                for label, pattern in FORBIDDEN_PDF_TONE_PATTERNS.items():
                    if pattern.search(full_text.stdout):
                        forbidden_pdf_tone_phrases.append(label)
                        errors.append(f"forbidden_pdf_tone_phrase:{label}")
                if not allow_experiment_placeholders:
                    forbidden_pdf_tokens = [
                        token for token in FORBIDDEN_FINAL_PDF_TOKENS
                        if token in full_text.stdout
                    ]
                    for token in forbidden_pdf_tokens:
                        errors.append(f"forbidden_final_pdf_token:{token}")
        except RuntimeError as error:
            errors.append(str(error))

    numerical_presentation: dict[str, Any] = {"status": "skipped"}
    if not allow_experiment_placeholders and pdf.is_file():
        resolved_evidence = evidence_root
        if resolved_evidence is None and evidence_manifest is not None:
            resolved_evidence = evidence_manifest.resolve().parent
        if resolved_evidence is None:
            resolved_evidence, _ = _discover_evidence_root(root)
        numerical_script = root / "writing" / "audit_numerical_presentation.py"
        if resolved_evidence is not None and numerical_script.is_file():
            numeric_result = _run([
                sys.executable, str(numerical_script), "--paper-root", str(root),
                "--evidence-root", str(resolved_evidence.resolve()),
                "--pdf-path", str(pdf)], cwd=root)
            try:
                numerical_presentation = json.loads(numeric_result.stdout)
                numerical_presentation.pop("paper_root", None)
                numerical_presentation.pop("evidence_root", None)
                numerical_presentation["pdf_path"] = _reported_relative_path(pdf, root)
            except json.JSONDecodeError:
                numerical_presentation = {
                    "status": "fail", "errors": ["invalid_audit_output"]}
            if (numeric_result.returncode != 0 or
                    numerical_presentation.get("status") != "pass"):
                errors.append("numerical_presentation_audit_failed")
        else:
            errors.append("numerical_presentation_audit_inputs_missing")

    source_build_manifest = _source_build_manifest(
        root, compile_pdf=compile_pdf, source_before=source_before, errors=errors)

    payload = {
        "protocol": "paper-zh-qa-v1",
        "status": "pass" if not errors else "fail",
        "submission_ready": not errors and not submission_placeholders,
        "submission_placeholder_count": len(submission_placeholders),
        "submission_placeholders": submission_placeholders,
        "paragraph_leading_visual_references": (
            paragraph_leading_visual_references),
        "errors": errors,
        "warnings": warnings,
        "artifacts": {
            _reported_relative_path(path, root): {
                "sha256": _sha256(path), "bytes": path.stat().st_size}
            for path in (pdf, (root / OVERALL_FIGURE_PDF).resolve())
            if path.is_file()
        },
        "page_count": page_count,
        "expected_pages": expected_pages,
        "references_on_last_page": references_on_last_page,
        "experiment_placeholders": placeholder_count,
        "allow_experiment_placeholders": allow_experiment_placeholders,
        "core_figures": figure_audit,
        "overall_figure_build": overall_figure_build,
        "overall_figure_pdf": overall_figure_pdf_audit,
        "fig6_derived_data": fig_six_data_audit,
        "fig6_metadata_consistency": fig_six_metadata_consistency,
        "evidence_audit": evidence_audit,
        "numerical_presentation": numerical_presentation,
        "source_build_manifest": source_build_manifest,
        "forbidden_pdf_tokens": forbidden_pdf_tokens,
        "forbidden_pdf_method_labels": forbidden_pdf_method_labels,
        "forbidden_pdf_tone_phrases": forbidden_pdf_tone_phrases,
        "latex_log_findings": log_findings,
    }
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).parent)
    parser.add_argument("--compile", action="store_true")
    parser.add_argument("--allow-experiment-placeholders", action="store_true")
    parser.add_argument("--expected-pages", type=int)
    parser.add_argument(
        "--evidence-root", type=Path,
        help="paper_zh_v2 directory containing paper_values.json")
    parser.add_argument(
        "--evidence-manifest", "--manifest", dest="evidence_manifest",
        type=Path, help="explicit paper_zh_v2 final_manifest.json")
    parser.add_argument(
        "--json-output", type=Path,
        help="QA report within ../build/paper_zh (default: final_paper_qa.json)")
    args = parser.parse_args()
    build_directory = (args.root.resolve() / BUILD_DIRECTORY).resolve()
    destination = (
        args.json_output.resolve() if args.json_output is not None
        else build_directory / "final_paper_qa.json")
    if not destination.is_relative_to(build_directory):
        parser.error("--json-output must be inside the repository build/paper_zh directory")
    payload = verify(
        args.root, compile_pdf=args.compile,
        allow_experiment_placeholders=args.allow_experiment_placeholders,
        expected_pages=args.expected_pages,
        evidence_root=args.evidence_root,
        evidence_manifest=args.evidence_manifest)
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(rendered + "\n", encoding="utf-8")
    return 0 if payload["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
