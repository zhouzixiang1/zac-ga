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

TOKEN = re.compile(r"\\\\|\\[A-Za-z@]+|[A-Za-z0-9_]+|[^\s]")


def strip_comments(source: str) -> str:
    return re.sub(r"(?<!\\)%[^\n]*", "", source)


def visible_scope(source: str, name: str) -> str:
    if name == "paper_zh.tex" or name.endswith("_standalone.tex"):
        start = source.find(r"\begin{document}")
        return source[start:] if start >= 0 else source
    if name.startswith("figures/") and r"\begin{tikzpicture}" in source:
        source = source[source.index(r"\begin{tikzpicture}"):]
        # The option's spelling and braces do not print. Audit the label body
        # itself, retaining its revision wrappers (unmarked label bodies fail).
        option = re.compile(r"nodes\s+near\s+coords\s*=\s*\{")
        cursor = 0
        while match := option.search(source, cursor):
            begin, end, depth = match.end(), match.end(), 1
            while end < len(source) and depth:
                if source[end] in "{}" and source[end - 1] != "\\":
                    depth += 1 if source[end] == "{" else -1
                end += 1
            if depth:
                raise ValueError("unbalanced nodes near coords body")
            tail = end + (end < len(source) and source[end] == ",")
            body = source[begin:end - 1]
            source = source[:match.start()] + body + source[tail:]
            cursor = match.start() + len(body)
        return source
    if name.endswith("/05_circuit_table.tex") and r"\ExplSyntaxOn" in source:
        # Display-alias command definitions precede the float. Their invocation
        # remains in the audited table, including its enclosing review color.
        start = source.find(r"\begin{table}")
        if start >= 0:
            return source[start:]
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
        marked = all(all(mask[m.start():m.end()]) for m in b[k:l])
        if k == l:
            # Deleted prose is recorded here; its rewritten surrounding text
            # must be marked, or the deletion needs an explicit human review.
            adjacent = b[max(0, k-1):min(len(b), k+1)]
            marked = any(any(mask[m.start():m.end()]) for m in adjacent)
        edit = {"kind": tag, "before": old_text, "after": new_text,
                "marked_or_adjacent_to_marked_replacement": marked}
        edits.append(edit)
        if not marked:
            failures.append(edit)
    return {"edits": edits, "unmarked_changes": failures}


def audit(root: Path, baseline: Path) -> dict:
    paths = [Path("paper_zh.tex")]
    paths += [p.relative_to(root) for folder in ("sections", "figures")
              for p in sorted((root / folder).glob("*.tex"))]
    files = []
    for relative in paths:
        old_path, new_path = baseline / relative, root / relative
        if not old_path.is_file():
            raise ValueError(f"baseline missing source: {relative}")
        old, new = old_path.read_text(), new_path.read_text()
        if old == new:
            continue
        item = compare(visible_scope(old, relative.as_posix()),
                       visible_scope(new, relative.as_posix()))
        files.append({"file": relative.as_posix(),
                      "before_sha256": hashlib.sha256(old.encode()).hexdigest(),
                      "after_sha256": hashlib.sha256(new.encode()).hexdigest(),
                      "preamble_only": not item["edits"], **item})
    return {"protocol": "chinese-paper-redline-audit-v1",
            "status": "pass" if all(not f["unmarked_changes"] for f in files) else "fail",
            "baseline": str(baseline), "paper_root": str(root), "files": files}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1] / "IEEE_conference_template"
    output = args.output.resolve()
    if not output.is_relative_to(root / "build"):
        raise SystemExit("Review reports belong under IEEE_conference_template/build/")
    report = audit(root, args.baseline.resolve())
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "changed_files": len(report["files"]),
                      "unmarked_changes": sum(len(f["unmarked_changes"]) for f in report["files"]),
                      "report": str(output)}, ensure_ascii=False))
    raise SystemExit(0 if report["status"] == "pass" else 1)


if __name__ == "__main__":
    main()
