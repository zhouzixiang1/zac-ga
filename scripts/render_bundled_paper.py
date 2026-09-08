"""Render distributed Chinese TeX without claiming historical evidence QA.

Only a new manuscript-local build subdirectory is writable. The source copy and
all bundled macro hashes are recorded; no numerical generator or experiment is
run. This is deliberately separate from verify_paper_zh.py and make paper.
"""
import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess

from portable_reproduce import (ROOT, PUBLICATION_INPUTS, local_file,
                                publication_files, sha, write)


SUFFIXES = {".tex", ".bib", ".cls", ".sty", ".bst", ".dat", ".csv"}
FIGURE_LOCAL = "build/paper_zh/figures/overall_framework.pdf"
FIGURE_RENDER = "figures/overall_framework.pdf"


def render(root, name):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", name):
        raise ValueError("a simple new render name is required")
    for tool in ("xelatex", "bibtex", "pdfinfo"):
        if not shutil.which(tool):
            raise ValueError(f"required TeX/PDF tool is missing: {tool}")
    paper = local_file(root, "IEEE_conference_template")
    output = local_file(root, Path("IEEE_conference_template/build/bundled-render") / name)
    if output.exists():
        raise FileExistsError("render outputs are immutable; use a new name")
    files = []
    for path in paper.rglob("*"):
        relative = path.relative_to(paper)
        if "build" in relative.parts:
            continue
        if path.is_symlink():
            raise ValueError(f"source symlinks are not supported: {relative}")
        if path.is_file() and path.suffix in SUFFIXES:
            files.append(relative.as_posix())
    source_hashes = {name: sha(paper / name) for name in sorted(files)}
    companion_files = publication_files(root)
    companion_hashes = {name: sha(local_file(root, name)) for name in companion_files}
    if "paper_zh.tex" not in files or "figures/overall_framework_standalone.tex" not in files:
        raise ValueError("main paper or standalone figure source is missing")
    # The source export's manifest is an additional content-binding check. It
    # establishes which macro bytes were bundled, not correctness of raw runs.
    export_manifest = local_file(root, "portable_source_manifest.json")
    export_manifest_hash = sha(export_manifest) if export_manifest.exists() else None
    if export_manifest.exists():
        exported = json.loads(export_manifest.read_text())["files"]
        for name, digest in source_hashes.items():
            if exported.get("IEEE_conference_template/" + name) != digest:
                raise ValueError("paper differs from its source export manifest")
        for name, digest in companion_hashes.items():
            if exported.get(name) != digest:
                raise ValueError("publication bundle differs from its source export manifest")
    output.mkdir(parents=True)
    tex = output / "source"
    tex.mkdir()
    adapted = []
    adapted_publication = []
    for name in files:
        target = tex / name
        target.parent.mkdir(parents=True, exist_ok=True)
        data = (paper / name).read_bytes()
        if name.endswith(".tex") and FIGURE_LOCAL.encode() in data:
            data = data.replace(FIGURE_LOCAL.encode(), FIGURE_RENDER.encode())
            adapted.append(name)
        if name == "paper_zh.tex" and companion_files:
            for original, replacement in PUBLICATION_INPUTS.items():
                pattern = rb"(\\input\s*\{)" + re.escape(original.encode()) + rb"(\})"
                data, count = re.subn(pattern, lambda match: match[1] + replacement.encode() + match[2], data)
                if count != 1:
                    raise ValueError("each publication input must be adapted exactly once")
                adapted_publication.append({"source": original, "render": replacement})
        with target.open("xb") as stream:
            stream.write(data)
    for name in companion_files:
        target = tex / "publication/default_initial_v1" / Path(name).name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(local_file(root, name).read_bytes())
        if sha(target) != companion_hashes[name] or sha(local_file(root, name)) != companion_hashes[name]:
            raise ValueError("publication bundle changed during copying")
    if adapted != ["sections/03_method.tex"]:
        # The English snapshot may legitimately contain the same path; it is
        # not compiled here. Require the Chinese path and prohibit other edits.
        if sorted(adapted) != ["sections/03_method.tex", "sections_en/03_method.tex"]:
            raise ValueError(f"unexpected standalone figure path locations: {adapted}")
    write(output / "render_input.json", {"schema": "zac-bundled-paper-render-v1",
          "source_hashes": source_hashes,
          "publication_source_hashes": companion_hashes,
          "source_export_manifest_sha256": export_manifest_hash,
          "macro_sources": [p for p in files if p.endswith("_values.tex") or p == "results_values_zh.tex"],
          "adapted_figure_paths": adapted, "numerical_generators_run": False,
          "adapted_publication_inputs": adapted_publication,
          "historical_evidence_validated": False})
    commands = [
        ["xelatex", "-no-shell-escape", "-interaction=nonstopmode", "-halt-on-error", "-output-directory=figures", "-jobname=overall_framework", "figures/overall_framework_standalone.tex"],
        ["xelatex", "-no-shell-escape", "-interaction=nonstopmode", "-halt-on-error", "paper_zh.tex"],
        ["bibtex", "paper_zh"],
        ["xelatex", "-no-shell-escape", "-interaction=nonstopmode", "-halt-on-error", "paper_zh.tex"],
        ["xelatex", "-no-shell-escape", "-interaction=nonstopmode", "-halt-on-error", "paper_zh.tex"],
    ]
    for index, command in enumerate(commands):
        print(f"render step {index + 1}/{len(commands)}: {command[0]}", flush=True)
        with (output / f"step-{index + 1}.log").open("x") as stream:
            subprocess.run(command, cwd=tex, stdout=stream, stderr=subprocess.STDOUT,
                           timeout=180, check=True)
    if {name: sha(paper / name) for name in files} != source_hashes:
        raise ValueError("source changed during rendering")
    if {name: sha(local_file(root, name)) for name in companion_files} != companion_hashes:
        raise ValueError("publication bundle changed during rendering")
    if {name: sha(tex / "publication/default_initial_v1" / Path(name).name)
            for name in companion_files} != companion_hashes:
        raise ValueError("copied publication bundle changed during rendering")
    if (sha(export_manifest) if export_manifest.exists() else None) != export_manifest_hash:
        raise ValueError("source export manifest changed during rendering")
    log = (tex / "paper_zh.log").read_text(errors="replace")
    problems = [pattern for pattern in ("Overfull", "There were undefined references", "Citation `")
                if pattern in log]
    if problems:
        raise ValueError(f"render requires correction: {problems}")
    info = subprocess.check_output(["pdfinfo", str(tex / "paper_zh.pdf")], text=True)
    pages = int(re.search(r"^Pages:\s+(\d+)", info, re.M)[1])
    figure_info = subprocess.check_output(["pdfinfo", str(tex / FIGURE_RENDER)], text=True)
    if int(re.search(r"^Pages:\s+(\d+)", figure_info, re.M)[1]) != 1:
        raise ValueError("standalone figure is not one page")
    receipt = {"schema": "zac-bundled-paper-render-v1", "status": "rendered",
               "pages": pages, "pdf": "source/paper_zh.pdf", "pdf_sha256": sha(tex / "paper_zh.pdf"),
               "input_manifest_sha256": sha(output / "render_input.json"),
               "historical_evidence_validated": False, "visual_review_completed": False,
               "scope": "source rendering and TeX diagnostics only; not strict paper evidence QA"}
    write(output / "render_report.json", receipt)
    return {**receipt, "output": str(output)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--name", required=True)
    args = parser.parse_args()
    print(json.dumps(render(args.root.resolve(), args.name), indent=2))
