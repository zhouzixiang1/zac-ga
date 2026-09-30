#!/usr/bin/env python3
"""Prepare an Overleaf-compatible checkout without committing or pushing.

Run ``make paper`` first, then this script. The active manuscript remains in
IEEE_conference_template; only its build/overleaf-sync checkout is adapted for Overleaf.
An existing sync checkout must be clean. The remote tip must exactly match
the explicitly reviewed commit before any manuscript file is overwritten.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
from typing import Sequence


REMOTE_URL = "https://git@git.overleaf.com/6a866ce86ea64496e2ae01a5"
BRANCH = "main"
KEYCHAIN_HELPER = "/Applications/Xcode.app/Contents/Developer/usr/libexec/git-core/git-credential-osxkeychain"
LOCAL_FIGURE_PATH = b"build/paper_zh/figures/overall_framework.pdf"
LOCAL_EN_FIGURE_PATH = b"build/paper_en/figures/overall_framework.pdf"
OVERLEAF_FIGURE_PATH = b"figures/overall_framework.pdf"
LOCAL_FIGURE_REFERENCE = re.compile(rb"(?<![\w./-])" + rb"(?:" + re.escape(LOCAL_FIGURE_PATH) + rb"|" + re.escape(LOCAL_EN_FIGURE_PATH) + rb")(?![\w./-])")
LOCAL_LATEXMK_PATH = re.compile(
    r"\.\./build\b|(?<![\w-])build/(?:paper_zh|paper_en|overleaf-sync)(?![\w-])")
EXCLUDED_DIRECTORIES = {
    ".git", "build", "tmp", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".venv", "venv", "node_modules",
}
EXCLUDED_NAMES = {
    ".latexmkrc", "latexmkrc", ".DS_Store", "paper_zh.pdf", "paper_en.pdf",
    "overall_framework.pdf", "overall_framework_standalone.pdf",
    "template-A4.tex", "template-A4.pdf", "fig1.png", "IEEEtran_HOWTO.pdf",
    "final_paper_qa.json", "page_overview.png",
}
GENERATED_SUFFIXES = (
    ".aux", ".bbl", ".blg", ".fdb_latexmk", ".fls", ".log", ".out",
    ".xdv", ".synctex.gz", ".synctex", ".toc", ".lof", ".lot",
    ".nav", ".snm", ".vrb", ".bcf", ".run.xml", ".pyc", ".pyo",
)
SCIENTIFIC_SOURCE_SUFFIXES = {".tex", ".bib", ".cls", ".sty", ".dat", ".bst"}
DEFAULT_PUBLICATION_DIRECTORY = PurePosixPath("../ZAC_zzx/results/default_initial_v1/paper_exports")
DEFAULT_PUBLICATION_DESTINATION = PurePosixPath("paper_exports/default_initial_v1")
DEFAULT_PUBLICATION_FILES = (
    "default_initial_values.json", "default_initial_values.tex", "main_rows.csv",
    "analysis_units.csv", "mechanism.csv", "representative_cases.tex",
)
DEFAULT_PUBLICATION_SOURCES = frozenset(
    DEFAULT_PUBLICATION_DIRECTORY / name for name in DEFAULT_PUBLICATION_FILES)
GA_PUBLICATION_DIRECTORY = PurePosixPath("../ZAC_zzx/results/physical_ga_main_v1/paper_exports")
GA_PUBLICATION_DESTINATION = PurePosixPath("paper_exports/physical_ga_main_v1")
GA_PUBLICATION_FILES = (
    "ga_main_values.json", "ga_main_values.tex", "main_rows.csv",
    "analysis_units.csv", "mechanism.csv", "representative_cases.tex",
)
GA_PUBLICATION_SOURCES = frozenset(GA_PUBLICATION_DIRECTORY / name for name in GA_PUBLICATION_FILES)
PUBLICATION_SOURCES = DEFAULT_PUBLICATION_SOURCES | GA_PUBLICATION_SOURCES
# Audit the complete evidence bundle locally; Overleaf needs only its TeX inputs.
PUBLICATION_TEX_SOURCES = frozenset(path for path in PUBLICATION_SOURCES if path.suffix == ".tex")
ENGLISH_ONLY_FILES = frozenset(map(PurePosixPath, (
    "paper_en.tex", "README_en.md", "verify_paper_en.py", "writing/test_verify_paper_en.py",
)))


class PreparationError(RuntimeError):
    pass


def safe_message(message: str) -> str:
    """Never echo URL passwords even if an external Git diagnostic has one."""
    return re.sub(r"https://[^\s/@:]+:[^\s/@]+@", "https://[redacted]@", message)


def git(arguments: Sequence[str], *, cwd: Path, network: bool = False,
        allowed_codes: tuple[int, ...] = (0,), binary: bool = False
        ) -> subprocess.CompletedProcess:
    command = ["git"]
    environment = os.environ.copy()
    if network:
        # Reset inherited helpers before selecting the user's macOS Keychain.
        # Disable prompts and tracing: this script never retrieves or prints
        # a credential, and never falls back to an interactive password input.
        command.extend([
            "-c", "credential.helper=", "-c", f"credential.helper={KEYCHAIN_HELPER}",
            "-c", "credential.interactive=false",
        ])
        environment = {key: value for key, value in environment.items()
                       if not key.startswith("GIT_TRACE") and key != "GIT_CURL_VERBOSE"}
        environment.update({"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/usr/bin/false"})
    result = subprocess.run(
        [*command, *arguments], cwd=cwd, env=environment,
        text=not binary, encoding=None if binary else "utf-8",
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        check=False)
    if result.returncode not in allowed_codes:
        detail = (result.stderr or result.stdout).strip()[-3000:]
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", errors="replace")
        detail = safe_message(detail)
        raise PreparationError(f"Git {arguments[0]} failed: {detail}")
    return result


def excluded(relative: PurePosixPath) -> bool:
    return (any(part in EXCLUDED_DIRECTORIES for part in relative.parts)
            or relative.name in EXCLUDED_NAMES
            or relative.name.endswith(GENERATED_SUFFIXES)
            or relative.suffix.lower() in {".py", ".json"})


def uses_default_publication(source: bytes) -> bool:
    # Match the build verifier's dependency discovery, without TeX comments.
    text = re.sub(rb"(?<!\\)%[^\n]*", b"", source)
    return b"default_initial_values" in text or bool(re.search(
        rb"\\Default(?:ZAC|QMAP|Max|Mechanism|Representative)", text))


def uses_ga_publication(source: bytes) -> bool:
    text = re.sub(rb"(?<!\\)%[^\n]*", b"", source)
    return b"ga_main_values" in text or bool(re.search(rb"\\GAMain[A-Za-z]+", text))


def source_path(root: Path, relative: PurePosixPath) -> Path:
    paper = root / "IEEE_conference_template"
    if relative in PUBLICATION_SOURCES:
        boundary = root
        source = root.joinpath(*relative.parts[1:])
    else:
        boundary = paper
        if relative.is_absolute() or ".." in relative.parts:
            raise PreparationError(f"Refusing an unregistered external source: {relative}")
        source = paper / relative
    for path in (source, *source.parents):
        if path == boundary:
            break
        if path.is_symlink():
            raise PreparationError(f"Refusing an external or symbolic-link source: {relative}")
    if not source.resolve().is_relative_to(boundary.resolve()):
        raise PreparationError(f"Source escapes its registered directory: {relative}")
    return source


def export_path(relative: PurePosixPath) -> PurePosixPath:
    if relative in GA_PUBLICATION_SOURCES:
        return GA_PUBLICATION_DESTINATION / relative.name
    return (DEFAULT_PUBLICATION_DESTINATION / relative.name
            if relative in DEFAULT_PUBLICATION_SOURCES else relative)


def english_only(relative: PurePosixPath) -> bool:
    return relative in ENGLISH_ONLY_FILES or relative.parts[0] == "sections_en"


def local_files(root: Path) -> list[PurePosixPath]:
    listing = git([
        "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--",
        "IEEE_conference_template/"], cwd=root).stdout
    prefix = PurePosixPath("IEEE_conference_template")
    paths = set()
    for item in listing.split("\0"):
        if not item:
            continue
        relative = PurePosixPath(item).relative_to(prefix)
        local = root / "IEEE_conference_template" / relative
        # Git still lists relocated tracked files until the author stages them.
        # Only present sources participate in the current manuscript closure.
        if not excluded(relative) and (local.exists() or local.is_symlink()):
            paths.add(relative)
    if PurePosixPath("paper_zh.tex") not in paths:
        raise PreparationError("The active paper is missing or ignored by the ZAC repository.")
    main = source_path(root, PurePosixPath("paper_zh.tex"))
    if main.is_file() and uses_default_publication(main.read_bytes()):
        paths.update(DEFAULT_PUBLICATION_SOURCES)
    if main.is_file() and uses_ga_publication(main.read_bytes()):
        paths.update(GA_PUBLICATION_SOURCES)
    return sorted(paths)


def read_sources(root: Path, files: Sequence[PurePosixPath]) -> dict[PurePosixPath, bytes]:
    snapshot = {}
    for relative in files:
        source = source_path(root, relative)
        # A tracked deletion is not an exportable current file. Remote extras
        # remain intact; the caller resolves deletions separately if needed.
        if not source.is_file():
            raise PreparationError(f"Listed manuscript file is missing: {relative}")
        before = source.stat()
        snapshot[relative] = source.read_bytes()
        after = source.stat()
        if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
            raise PreparationError(f"Source changed while reading: {relative}; retry after review.")
    return snapshot


def check_build(root: Path, snapshot: dict[PurePosixPath, bytes]) -> bytes:
    build = root / "IEEE_conference_template/build/paper_zh"
    try:
        qa = json.loads((build / "final_paper_qa.json").read_text(encoding="utf-8"))
        if (qa.get("status") != "pass" or not isinstance(qa.get("page_count"), int) or qa.get("page_count", 0) < 2
                or not qa.get("references_on_last_page")):
            raise PreparationError("The paper QA is not a passing manuscript build; run make paper.")
        manifest = json.loads((build / "source_build_manifest.json").read_text(encoding="utf-8"))
        source_hashes = {relative.as_posix(): hashlib.sha256(content).hexdigest()
                         for relative, content in snapshot.items()
                         if (relative.suffix.lower() in SCIENTIFIC_SOURCE_SUFFIXES
                             or relative in PUBLICATION_SOURCES)}
        if (manifest.get("protocol") != "paper-source-build-v1"
                or manifest.get("source_sha256") != source_hashes):
            raise PreparationError("Source content differs from the verified build manifest; run make paper.")
        captured = {}
        for name in ("paper_zh.pdf", "figures/overall_framework.pdf"):
            path = build / name
            # Capture once: the exported figure is exactly the byte sequence
            # whose digest is checked, even if another process replaces it.
            captured[name] = path.read_bytes()
            digest = hashlib.sha256(captured[name]).hexdigest()
            expected = qa["artifacts"][f"build/paper_zh/{name}"]["sha256"]
            if digest != expected or digest != manifest["artifacts"][name]["sha256"]:
                raise PreparationError(f"Build/QA hash mismatch for {name}; run make paper.")
        return captured["figures/overall_framework.pdf"]
    except (OSError, KeyError, TypeError, AttributeError, json.JSONDecodeError) as error:
        raise PreparationError(f"Missing or incomplete verified build; run make paper. {error}") from error


def require_clean_checkout(checkout: Path) -> None:
    if git(["status", "--porcelain=v1", "--untracked-files=all"], cwd=checkout).stdout:
        raise PreparationError("The sync checkout is dirty; review/commit its prepared changes before retrying.")


def prepare_checkout(root: Path, expected_remote: str) -> tuple[Path, str, list[str]]:
    checkout = root / "IEEE_conference_template/build/overleaf-sync"
    checkout.parent.mkdir(parents=True, exist_ok=True)
    if checkout.is_symlink():
        raise PreparationError("The sync checkout must not be a symbolic link.")
    if checkout.exists():
        if not (checkout / ".git").is_dir():
            raise PreparationError("IEEE_conference_template/build/overleaf-sync exists but is not a dedicated Git checkout.")
        require_clean_checkout(checkout)
        origin = git(["remote", "get-url", "origin"], cwd=checkout).stdout.strip()
        if origin != REMOTE_URL:
            raise PreparationError("The sync checkout origin differs from the designated Overleaf project.")
        branch = git(["symbolic-ref", "--quiet", "--short", "HEAD"], cwd=checkout).stdout.strip()
        if branch != BRANCH:
            raise PreparationError("The sync checkout must be on main before synchronization.")
        git(["fetch", "--prune", "origin"], cwd=checkout, network=True)
    else:
        git(["clone", "--branch", BRANCH, "--single-branch", REMOTE_URL, str(checkout)],
            cwd=root, network=True)

    require_clean_checkout(checkout)
    remote_tip = git(["rev-parse", "refs/remotes/origin/main"], cwd=checkout).stdout.strip()
    expected = git(["rev-parse", "--verify", f"{expected_remote}^{{commit}}"], cwd=checkout).stdout.strip()
    if remote_tip != expected:
        changed = [name for name in git([
            "diff", "--no-renames", "--name-only", "-z", expected, remote_tip,
        ], cwd=checkout).stdout.split("\0") if name]
        raise PreparationError(
            "The Overleaf remote no longer matches the reviewed commit. Changed files: "
            + (", ".join(changed) or "no file difference; commit history changed")
            + f". Current remote: {remote_tip}. After reviewing them, supply --expected-remote explicitly.")
    git(["merge", "--ff-only", "refs/remotes/origin/main"], cwd=checkout)
    head = git(["rev-parse", "HEAD"], cwd=checkout).stdout.strip()
    if head != remote_tip:
        raise PreparationError("The sync checkout contains local commits ahead of origin/main; review them before another export.")
    require_clean_checkout(checkout)
    for name in (".latexmkrc", "latexmkrc"):
        existing_rc = checkout / name
        if existing_rc.is_file() and LOCAL_LATEXMK_PATH.search(existing_rc.read_text(encoding="utf-8")):
            raise PreparationError(f"The remote {name} contains a local build path; resolve it before export.")
    return checkout, remote_tip, []


def safe_destination(checkout: Path, relative: PurePosixPath) -> Path:
    destination = checkout / relative
    for path in (destination, *destination.parents):
        if path == checkout:
            break
        if path.is_symlink():
            raise PreparationError(f"Refusing to overwrite a symbolic-link destination: {relative}")
    if not destination.resolve().is_relative_to(checkout.resolve()):
        raise PreparationError(f"Export destination escapes checkout: {relative}")
    return destination


def overleaf_readme() -> bytes:
    return ("# GA-LK manuscript\n\n"
            "Choose **XeLaTeX** as the compiler and **TeX Live 2025**.\n\n"
            "- 中文主文件 / Chinese main document: `paper_zh.tex`\n"
            "- English main document: `paper_en.tex`\n"
            "- Chapters: `sections/` and `sections_en/`\n"
            "- Figures: `figures/`; Figure 2 uses `overall_framework.pdf`.\n"
            "- Algorithm 1: `sections/03_algorithm.tex` and `sections_en/03_algorithm.tex`; "
            "shared formatting: `algorithm_style.tex` (`algpseudocodex`).\n"
            "- Shared references: `references.bib`\n\n"
            "The framework PDF is supplied from an independent build. If editing its source, "
            "compile `figures/overall_framework_standalone.tex` and replace "
            "`figures/overall_framework.pdf` with the resulting PDF.\n\n"
            "Both versions use clean text and share the same figures and numerical inputs. "
            "Only manuscript files and compilation dependencies are kept here.\n").encode("utf-8")


def export_payload(snapshot: dict[PurePosixPath, bytes],
                   figure: bytes, *, chinese_only: bool = False
                   ) -> dict[PurePosixPath, bytes]:
    methods = [PurePosixPath("sections/03_method.tex")]
    if not chinese_only and (PurePosixPath("paper_en.tex") in snapshot
            or any(relative.parts[0] == "sections_en" for relative in snapshot)):
        methods.append(PurePosixPath("sections_en/03_method.tex"))
    # Validate both counterparts before constructing the export. A partial
    # English source tree must not silently retain a local-only figure path.
    for method in methods:
        source = snapshot.get(method, b"")
        expected_path = LOCAL_EN_FIGURE_PATH if method.parts[0] == "sections_en" else LOCAL_FIGURE_PATH
        if source.count(expected_path) != 1 or len(LOCAL_FIGURE_REFERENCE.findall(source)) != 1:
            raise PreparationError(f"Expected exactly one local build path in {method}.")
    export = {}
    for relative, content in snapshot.items():
        if chinese_only and english_only(relative):
            continue
        if relative in PUBLICATION_SOURCES and relative not in PUBLICATION_TEX_SOURCES:
            continue
        destination = export_path(relative)
        if destination in export:
            raise PreparationError(f"Two sources map to the same export destination: {destination}")
        export[destination] = overleaf_readme() if relative == PurePosixPath("README.md") else content
    for method in methods:
        export[method] = LOCAL_FIGURE_REFERENCE.sub(OVERLEAF_FIGURE_PATH, snapshot[method])
    export[PurePosixPath("figures/overall_framework.pdf")] = figure
    main = PurePosixPath("paper_zh.tex")
    package_sources = set(snapshot) & DEFAULT_PUBLICATION_SOURCES
    if uses_default_publication(snapshot.get(main, b"")) or package_sources:
        if package_sources != DEFAULT_PUBLICATION_SOURCES:
            raise PreparationError("The current paper requires the complete six-file default-initial package.")
        for name in ("default_initial_values.tex", "representative_cases.tex"):
            original = (DEFAULT_PUBLICATION_DIRECTORY / name).as_posix().encode()
            replacement = (DEFAULT_PUBLICATION_DESTINATION / name).as_posix().encode()
            reference = re.compile(rb"(\\input\s*\{\s*)" + re.escape(original) + rb"(\s*\})")
            if export[main].count(original) != 1 or len(reference.findall(export[main])) != 1:
                raise PreparationError(f"Expected exactly one registered external input in paper_zh.tex: {name}")
            export[main] = reference.sub(lambda match: match[1] + replacement + match[2], export[main])
    ga_sources = set(snapshot) & GA_PUBLICATION_SOURCES
    if uses_ga_publication(snapshot.get(main, b"")) or ga_sources:
        if ga_sources != GA_PUBLICATION_SOURCES:
            raise PreparationError("The current paper requires the complete six-file physical-GA package.")
        # Main text and standalone figure wrappers share registered inputs.
        # Adapt only the input path; optional .tex suffix and all prose survive.
        for name in ("ga_main_values", "representative_cases"):
            original = (GA_PUBLICATION_DIRECTORY / name).as_posix().encode()
            replacement = (GA_PUBLICATION_DESTINATION / name).as_posix().encode()
            reference = re.compile(rb"(\\input\s*\{\s*)" + re.escape(original)
                                   + rb"(\.tex)?(\s*\})")
            if len(reference.findall(export[main])) != 1:
                raise PreparationError(f"Expected exactly one registered GA input in paper_zh.tex: {name}")
            for relative, content in list(export.items()):
                if relative.suffix == ".tex":
                    export[relative] = reference.sub(
                        lambda match: match[1] + replacement + (match[2] or b"") + match[3], content)
    return export


def copy_export(root: Path, checkout: Path, files: Sequence[PurePosixPath],
                snapshot: dict[PurePosixPath, bytes],
                export: dict[PurePosixPath, bytes]) -> None:
    # Resolve every destination first; no partial export precedes a detected
    # destination symlink or path escape. Existing remote extras are untouched.
    require_clean_checkout(checkout)
    head = git(["rev-parse", "HEAD"], cwd=checkout).stdout.strip()
    tracked = set(git(["ls-tree", "-r", "--name-only", "-z", head], cwd=checkout).stdout.split("\0"))
    expected = {
        relative: git(["show", f"{head}:{relative.as_posix()}"], cwd=checkout, binary=True).stdout
        if relative.as_posix() in tracked else None
        for relative in export
    }
    source_by_destination = {export_path(relative): relative for relative in snapshot}

    def check_target(relative: PurePosixPath) -> Path:
        destination = safe_destination(checkout, relative)
        baseline = expected[relative]
        if baseline is None:
            if destination.exists():
                raise PreparationError(f"Untracked or ignored export target already exists: {relative}")
        elif not destination.is_file() or destination.read_bytes() != baseline:
            raise PreparationError(f"Export target differs from HEAD; preserve the manual edit: {relative}")
        return destination

    for relative in export:
        check_target(relative)
    for relative, content in export.items():
        if git(["rev-parse", "HEAD"], cwd=checkout).stdout.strip() != head:
            raise PreparationError("The sync checkout HEAD changed during export; review before continuing.")
        destination = check_target(relative)
        if expected[relative] == content:
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Check again immediately before writing, after any directory creation.
        destination = check_target(relative)
        destination.write_bytes(content)
        if relative in source_by_destination:
            shutil.copymode(source_path(root, source_by_destination[relative]), destination)
    if local_files(root) != list(files) or read_sources(root, files) != snapshot:
        raise PreparationError("Local paper changed during export. Prepared checkout is uncommitted; review it before retrying.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--expected-remote", required=True,
        help="Explicitly reviewed remote commit; any remote-tip change stops export")
    parser.add_argument(
        "--check-only", action="store_true",
        help="Fetch and check both versions, but do not copy manuscript files")
    parser.add_argument(
        "--chinese-only", action="store_true",
        help="Verify the full source snapshot, but preserve remote English-only files")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", args.expected_remote):
        parser.error("--expected-remote must be a Git commit hash")
    root = Path(__file__).resolve().parents[1]
    try:
        files = local_files(root)
        snapshot = read_sources(root, files)
        figure = check_build(root, snapshot)
        export = export_payload(snapshot, figure, chinese_only=args.chinese_only)
        checkout, remote_tip, remote_changes = prepare_checkout(root, args.expected_remote)
        if not args.check_only:
            copy_export(root, checkout, files, snapshot, export)
        print(json.dumps({
            "status": "checked" if args.check_only else "prepared",
            "checkout": str(checkout), "remote_tip": remote_tip,
            "expected_remote": args.expected_remote, "remote_non_scientific_changes": remote_changes,
            "exported_file_count": 0 if args.check_only else len(export),
            "chinese_only": args.chinese_only,
            "english_only_files_preserved": [relative.as_posix() for relative in sorted(snapshot)
                                             if args.chinese_only and english_only(relative)],
            "only_scientific_source_adaptation": [
                (f"{relative}: local PDF path to figures/overall_framework.pdf"
                 if LOCAL_FIGURE_REFERENCE.search(snapshot[relative]) else
                 f"{relative}: registered publication inputs to paper_exports/")
                for relative in sorted(snapshot)
                if relative.suffix.lower() in SCIENTIFIC_SOURCE_SUFFIXES
                and export_path(relative) in export
                and export[export_path(relative)] != snapshot[relative]
            ],
            "external_source_files": [
                {"source": relative.as_posix(), "destination": export_path(relative).as_posix(),
                 "exported": export_path(relative) in export,
                 "sha256": hashlib.sha256(snapshot[relative]).hexdigest()}
                for relative in sorted(set(snapshot) & PUBLICATION_SOURCES)
            ],
            "remote_extra_files_deleted": False, "committed": False, "pushed": False,
        }, ensure_ascii=False, indent=2))
        return 0
    except (OSError, UnicodeError, PreparationError) as error:
        print(safe_message(str(error)), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
