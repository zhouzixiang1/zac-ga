#!/usr/bin/env python3
"""Prepare an Overleaf-compatible checkout without committing or pushing.

Run ``make paper`` first, then this script. The active manuscript remains in
IEEE_conference_template; only build/overleaf-sync is adapted for Overleaf.
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
LOCAL_FIGURE_PATH = b"../build/paper_zh/figures/overall_framework.pdf"
OVERLEAF_FIGURE_PATH = b"figures/overall_framework.pdf"
EXCLUDED_DIRECTORIES = {
    ".git", "build", "tmp", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".venv", "venv", "node_modules",
}
EXCLUDED_NAMES = {
    ".latexmkrc", "latexmkrc", ".DS_Store", "paper_zh.pdf",
    "overall_framework.pdf", "overall_framework_standalone.pdf",
    "final_paper_qa.json", "page_overview.png",
}
GENERATED_SUFFIXES = (
    ".aux", ".bbl", ".blg", ".fdb_latexmk", ".fls", ".log", ".out",
    ".xdv", ".synctex.gz", ".synctex", ".toc", ".lof", ".lot",
    ".nav", ".snm", ".vrb", ".bcf", ".run.xml", ".pyc", ".pyo",
)
SCIENTIFIC_SOURCE_SUFFIXES = {".tex", ".bib", ".cls", ".sty", ".dat", ".bst"}


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
            or relative.name.endswith(GENERATED_SUFFIXES))


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
        if not excluded(relative):
            paths.add(relative)
    if PurePosixPath("paper_zh.tex") not in paths:
        raise PreparationError("The active paper is missing or ignored by the ZAC repository.")
    return sorted(paths)


def read_sources(root: Path, files: Sequence[PurePosixPath]) -> dict[PurePosixPath, bytes]:
    paper = root / "IEEE_conference_template"
    snapshot = {}
    for relative in files:
        source = paper / relative
        if source.is_symlink() or not source.resolve().is_relative_to(paper.resolve()):
            raise PreparationError(f"Refusing an external or symbolic-link source: {relative}")
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
    build = root / "build/paper_zh"
    try:
        qa = json.loads((build / "final_paper_qa.json").read_text(encoding="utf-8"))
        if (qa.get("status") != "pass" or qa.get("page_count") != 9
                or not qa.get("references_on_last_page")):
            raise PreparationError("The paper QA is not a passing nine-page build; run make paper.")
        manifest = json.loads((build / "source_build_manifest.json").read_text(encoding="utf-8"))
        source_hashes = {relative.as_posix(): hashlib.sha256(content).hexdigest()
                         for relative, content in snapshot.items()
                         if relative.suffix.lower() in SCIENTIFIC_SOURCE_SUFFIXES}
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
            expected = qa["artifacts"][f"../build/paper_zh/{name}"]["sha256"]
            if digest != expected or digest != manifest["artifacts"][name]["sha256"]:
                raise PreparationError(f"Build/QA hash mismatch for {name}; run make paper.")
        return captured["figures/overall_framework.pdf"]
    except (OSError, KeyError, TypeError, AttributeError, json.JSONDecodeError) as error:
        raise PreparationError(f"Missing or incomplete verified build; run make paper. {error}") from error


def require_clean_checkout(checkout: Path) -> None:
    if git(["status", "--porcelain=v1", "--untracked-files=all"], cwd=checkout).stdout:
        raise PreparationError("The sync checkout is dirty; review/commit its prepared changes before retrying.")


def prepare_checkout(root: Path, expected_remote: str) -> tuple[Path, str, list[str]]:
    checkout = root / "build/overleaf-sync"
    checkout.parent.mkdir(parents=True, exist_ok=True)
    if checkout.is_symlink():
        raise PreparationError("The sync checkout must not be a symbolic link.")
    if checkout.exists():
        if not (checkout / ".git").is_dir():
            raise PreparationError("build/overleaf-sync exists but is not a dedicated Git checkout.")
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
    existing_rc = checkout / ".latexmkrc"
    if existing_rc.is_file() and "../build" in existing_rc.read_text(encoding="utf-8"):
        raise PreparationError("The remote .latexmkrc contains a local ../build path; resolve it before export.")
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


def export_payload(snapshot: dict[PurePosixPath, bytes],
                   figure: bytes) -> dict[PurePosixPath, bytes]:
    method = PurePosixPath("sections/03_method.tex")
    if snapshot.get(method, b"").count(LOCAL_FIGURE_PATH) != 1:
        raise PreparationError("Expected exactly one local build path in sections/03_method.tex.")
    export = dict(snapshot)
    export[method] = snapshot[method].replace(LOCAL_FIGURE_PATH, OVERLEAF_FIGURE_PATH)
    export[PurePosixPath("figures/overall_framework.pdf")] = figure
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
        if relative in snapshot:
            shutil.copymode(root / "IEEE_conference_template" / relative, destination)
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
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", args.expected_remote):
        parser.error("--expected-remote must be a Git commit hash")
    root = Path(__file__).resolve().parents[1]
    try:
        files = local_files(root)
        snapshot = read_sources(root, files)
        figure = check_build(root, snapshot)
        export = export_payload(snapshot, figure)
        checkout, remote_tip, remote_changes = prepare_checkout(root, args.expected_remote)
        if not args.check_only:
            copy_export(root, checkout, files, snapshot, export)
        print(json.dumps({
            "status": "checked" if args.check_only else "prepared",
            "checkout": str(checkout), "remote_tip": remote_tip,
            "expected_remote": args.expected_remote, "remote_non_scientific_changes": remote_changes,
            "exported_file_count": 0 if args.check_only else len(export),
            "only_scientific_source_adaptation": "sections/03_method.tex: local PDF path to figures/overall_framework.pdf",
            "remote_extra_files_deleted": False, "committed": False, "pushed": False,
        }, ensure_ascii=False, indent=2))
        return 0
    except (OSError, UnicodeError, PreparationError) as error:
        print(safe_message(str(error)), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
