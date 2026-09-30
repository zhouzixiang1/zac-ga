#!/usr/bin/env python3
"""Check Chinese review markup against an author-preserving source snapshot.

This is a source audit, not a substitute for inspecting the rendered PDF.
Preamble/build changes are listed separately from visible manuscript changes.
The report records removed text, which cannot itself appear red in the new PDF.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "paper"))
from paper_paths import artifact_path

TOKEN = re.compile(r"\\\\|\\[A-Za-z@]+|[A-Za-z0-9_]+|[^\s]")


def strip_comments(source: str) -> str:
    return re.sub(r"(?<!\\)%[^\n]*", "", source)


def _group(source: str, start: int) -> tuple[str, int]:
    depth, end = 1, start + 1
    while end < len(source) and depth:
        if source[end - 1] != "\\":
            depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    if depth:
        raise ValueError("unbalanced visible text group")
    return source[start + 1:end - 1], end


def figure_labels(source: str) -> str:
    """Read node and plot-label bodies; coordinates/styles are separate changes.

    Dynamic labels remain as macros in this audit and additionally require
    rendered colour inspection. Data macros themselves are never recoloured.
    """
    labels = []
    source = strip_comments(source)
    for match in re.finditer(r"(?<![A-Za-z])(?:\\node\b|node(?=\s*\[))", source):
        square = paren = 0
        i = match.end()
        while i < len(source) and source[i] != ";":
            c = source[i]
            if c == "[": square += 1
            elif c == "]": square -= 1
            elif not square:
                if c == "(": paren += 1
                elif c == ")": paren -= 1
                elif c == "{" and not paren:
                    body, _ = _group(source, i)
                    if body.strip(): labels.append(body)
                    break
            i += 1
    keys = r"(?:[xy](?:label|ticklabels)|title|legend entries|nodes\s+near\s+coords)"
    for match in re.finditer(keys + r"\s*=\s*\{", source):
        body, _ = _group(source, match.end() - 1)
        if body.strip(): labels.append(body)
    return "\n".join(labels)


def visible_scope(source: str, name: str) -> str:
    if name == "paper_zh.tex" or name.endswith("_standalone.tex"):
        start = source.find(r"\begin{document}")
        return source[start:] if start >= 0 else source
    if name.startswith("figures/") and r"\begin{tikzpicture}" in source:
        return figure_labels(source)
    if name.endswith("/05_circuit_table.tex") and r"\ExplSyntaxOn" in source:
        start = source.find(r"\begin{table}")
        if start >= 0: source = source[start:]
    # Build, positioning and type-size controls do not print.
    source = re.sub(r"\\label\{[^}]*\}", "", source)
    source = re.sub(r"\\fontsize\{[^}]*\}\{[^}]*\}\\selectfont", "", source)
    source = re.sub(r"\\setlength\{[^}]*\}\{[^}]*\}", "", source)
    # Build-only indirection does not print in the manuscript.
    source = re.sub(r"\\providecommand\{\\PaperOverallFigurePath\}\{[^}]*\}", "", source)
    source = re.sub(r"\\includegraphics(?:\[[^\]]*\])?\{[^}]*\}", "", source)
    # A moved float changes placement and tabular drivers, not their printed
    # content. Keep captions, cells, numerical macros and note bodies intact.
    source = re.sub(r"\\begin\{(?:figure|table|algorithm)\*?\}(?:\[[^\]]*\])?", "", source)
    source = re.sub(r"\\end\{(?:figure|table|algorithm|tabular)\*?\}", "", source)
    while match := re.search(r"\\begin\{(tabular\*?)\}", source):
        end = match.end()
        for _ in range(2 if match.group(1).endswith('*') else 1):
            while end < len(source) and source[end].isspace(): end += 1
            if source[end:end + 1] != '{':
                raise ValueError('tabular driver is missing an argument')
            _, end = _group(source, end)
        source = source[:match.start()] + source[end:]
    source = re.sub(r"\\PaperTableSetup\b|\\PaperTableNote(?:\[[^\]]*\])?", "", source)
    return source


def marked_content(source: str) -> tuple[str, list[bool]]:
    """Remove revision wrappers while retaining nested TeX and a color mask."""
    output: list[str] = []
    mask: list[bool] = []
    marker = r"\PaperRevision{"

    def emit(text: str, marked: bool = False) -> None:
        cursor = 0
        block = marked
        while cursor < len(text):
            if text.startswith(r"\begin{PaperRevisionBlock}", cursor):
                block = True
                cursor += len(r"\begin{PaperRevisionBlock}")
            elif text.startswith(r"\end{PaperRevisionBlock}", cursor):
                block = marked
                cursor += len(r"\end{PaperRevisionBlock}")
            elif text.startswith(marker, cursor):
                begin = cursor + len(marker)
                end = begin
                depth = 1
                while end < len(text) and depth:
                    if text[end] in "{}" and (end == 0 or text[end-1] != "\\"):
                        depth += 1 if text[end] == "{" else -1
                    end += 1
                if depth:
                    raise ValueError("unbalanced PaperRevision argument")
                emit(text[begin:end-1], True)
                cursor = end
            else:
                output.append(text[cursor])
                mask.append(block)
                cursor += 1

    emit(strip_comments(source))
    return "".join(output), mask


def compare(before: str, after: str) -> dict:
    old, _ = marked_content(before)
    new, mask = marked_content(after)
    a = list(TOKEN.finditer(old))
    b = list(TOKEN.finditer(new))
    edits = []
    failures = []
    matcher = difflib.SequenceMatcher(None, [m.group() for m in a],
                                     [m.group() for m in b], autojunk=False)
    for tag, i, j, k, l in matcher.get_opcodes():
        if tag == "equal":
            continue
        old_text = old[a[i].start():a[j-1].end()] if i < j else ""
        new_text = new[b[k].start():b[l-1].end()] if k < l else ""
        formatting_tokens = {"{", "}", "&", r"\\", r"\par", r"\centering",
                             r"\subsection", r"\subsubsection", r"\caption", r"\shortstack",
                             r"\midrule", r"\toprule", r"\bottomrule"}
        visible_tokens = [m for m in b[k:l] if m.group() not in formatting_tokens]
        marked = all(all(mask[m.start():m.end()]) for m in visible_tokens)
        if k == l:
            # Deleted prose is recorded here; its rewritten surrounding text
            # must be marked, or the deletion needs an explicit human review.
            adjacent = b[max(0, k-1):min(len(b), k+1)]
            marked = any(any(mask[m.start():m.end()]) for m in adjacent)
        # An unchanged block moved elsewhere is a structural edit, not new prose.
        # Pure deletions are retained verbatim for the companion revision record.
        old_tokens = [m.group() for m in a]
        added_tokens = [m.group() for m in b[k:l]]
        moved = len(added_tokens) >= 8 and any(
            old_tokens[pos:pos + len(added_tokens)] == added_tokens
            for pos in range(len(old_tokens) - len(added_tokens) + 1))
        nonprinting = bool(added_tokens) and all(
            token in ("{", "}", "\\", "&", r"\par", r"\centering", r"\subsection", r"\subsubsection", r"\midrule", r"\toprule", r"\bottomrule")
            for token in added_tokens)
        classification = ("deletion" if k == l else "move" if moved else
                          "formatting" if nonprinting else "visible_text")
        edit = {"kind": tag, "classification": classification,
                "before": old_text, "after": new_text,
                "marked_or_adjacent_to_marked_replacement": marked}
        edits.append(edit)
        if not marked and classification == "visible_text":
            failures.append(edit)
    return {"edits": edits, "unmarked_changes": failures}


def apply_figure_policy(item: dict, name: str, normal_colour: bool) -> dict:
    """Record the explicit figure exemption without weakening prose checks."""
    exempt = normal_colour and name.startswith("figures/")
    item["revision_colour_required"] = not exempt
    item["normal_colour_figure_changes"] = item["unmarked_changes"] if exempt else []
    if exempt:
        item["unmarked_changes"] = []
    return item


def audit(root: Path, baseline: Path, *, figures_normal_colour: bool = False) -> dict:
    # Include old-only sources so retirement is not silently omitted from the
    # deletion/movement record. Archived contents are identified separately.
    paths = {Path("paper_zh.tex")}
    paths.update(p.relative_to(tree) for tree in (root, baseline)
                 for folder in ("sections", "figures")
                 for p in (tree / folder).glob("*.tex"))
    files = []
    for relative in sorted(paths):
        old_path, new_path = baseline / relative, root / relative
        if not old_path.is_file():
            raise ValueError(f"baseline missing source: {relative}")
        retired = not new_path.is_file()
        archived = artifact_path(root, relative) if retired else None
        archived = archived if archived and archived != new_path and archived.is_file() else None
        old, new = old_path.read_text(), new_path.read_text() if not retired else ""
        if old == new:
            continue
        item = compare(visible_scope(old, relative.as_posix()),
                       visible_scope(new, relative.as_posix()))
        item = apply_figure_policy(item, relative.as_posix(), figures_normal_colour)
        files.append({"file": relative.as_posix(),
                      "retired_from_manuscript": retired,
                      "archived_path": str(archived) if archived else None,
                      "archived_sha256": hashlib.sha256(archived.read_bytes()).hexdigest() if archived else None,
                      "before_sha256": hashlib.sha256(old.encode()).hexdigest(),
                      "after_sha256": hashlib.sha256(new.encode()).hexdigest(),
                      "preamble_only": not item["edits"] and not relative.as_posix().startswith("figures/"),
                      "drawing_structure_review_required": relative.as_posix().startswith("figures/"),
                      **item})
    return {"protocol": "chinese-paper-redline-audit-v3",
            "figure_colour_policy": "normal_by_user_request" if figures_normal_colour else "review",
            "status": "pass" if all(not f["unmarked_changes"] for f in files) else "fail",
            "baseline": str(baseline), "paper_root": str(root), "files": files}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--figures-normal-color", action="store_true",
                        help="author requests normal-colour figure text; prose still requires redline")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1] / "IEEE_conference_template"
    output = args.output.resolve()
    if not output.is_relative_to(root / "build"):
        raise SystemExit("Review reports belong under IEEE_conference_template/build/")
    report = audit(root, args.baseline.resolve(), figures_normal_colour=args.figures_normal_color)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "changed_files": len(report["files"]),
                      "unmarked_changes": sum(len(f["unmarked_changes"]) for f in report["files"]),
                      "report": str(output)}, ensure_ascii=False))
    raise SystemExit(0 if report["status"] == "pass" else 1)


if __name__ == "__main__":
    main()
