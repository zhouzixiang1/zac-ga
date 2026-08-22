#!/usr/bin/env python3
"""Verify the frozen QMAP 3.2 patch and its applied-source identity."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Mapping


HERE = Path(__file__).resolve().parent


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(*args: str, cwd: Path, check: bool = True) -> str:
    completed = subprocess.run(
        args,
        cwd=cwd,
        check=check,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.stdout.strip()


def verify_frozen_bundle() -> dict[str, Any]:
    """Verify the tracked manifest, patch, config, and path ledgers."""

    manifest_path = HERE / "freeze_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("schema") != "qmap32-streaming-freeze-v2":
        raise ValueError("unsupported QMAP streaming freeze manifest schema")
    patch = HERE / manifest["patch_file"]
    config = HERE / manifest["frozen_config_file"]
    actual_patch_hash = _sha256(patch)
    actual_config_hash = _sha256(config)
    if actual_patch_hash != manifest["patch_sha256"]:
        raise ValueError("frozen patch SHA256 mismatch")
    if actual_config_hash != manifest["frozen_config_sha256"]:
        raise ValueError("frozen config SHA256 mismatch")

    patch_text = patch.read_text(encoding="utf-8")
    sections = re.split(r"(?=^diff --git )", patch_text, flags=re.MULTILINE)
    sections = [section for section in sections if section.startswith("diff --git ")]
    paths: list[str] = []
    new_paths: list[str] = []
    for section in sections:
        match = re.match(r"diff --git a/(.+) b/(.+)\n", section)
        if match is None or match.group(1) != match.group(2):
            raise ValueError("malformed or rename diff header in frozen patch")
        path = match.group(1)
        paths.append(path)
        if "\nnew file mode " in section:
            new_paths.append(path)
    if paths != manifest["paths"]:
        raise ValueError("frozen patch path ledger mismatch")
    if new_paths != manifest["new_paths"]:
        raise ValueError("frozen patch new-file ledger mismatch")
    if len(paths) != int(manifest["patch_files"]):
        raise ValueError("frozen patch file count mismatch")
    result_hashes = manifest.get("patched_file_sha256")
    if not isinstance(result_hashes, Mapping) or list(result_hashes) != paths:
        raise ValueError("frozen patched-file SHA256 ledger mismatch")
    if any(
        not isinstance(value, str)
        or len(value) != 64
        or not re.fullmatch(r"[0-9a-f]{64}", value)
        for value in result_hashes.values()
    ):
        raise ValueError("invalid frozen patched-file SHA256 value")
    freeze_revision = manifest.get("freeze_revision")
    if (
        isinstance(freeze_revision, bool)
        or not isinstance(freeze_revision, int)
        or freeze_revision <= 0
    ):
        raise ValueError("invalid QMAP binary freeze revision")
    formal_cli = manifest.get("formal_cli")
    if not isinstance(formal_cli, Mapping):
        raise ValueError("frozen formal CLI identity is missing")
    relative_cli = formal_cli.get("relative_path")
    cli_sha256 = formal_cli.get("sha256")
    cli_size = formal_cli.get("size_bytes")
    if (
        not isinstance(relative_cli, str)
        or not relative_cli
        or Path(relative_cli).is_absolute()
        or ".." in Path(relative_cli).parts
        or Path(relative_cli).name != "mqt-qmap-na-zoned-stream"
    ):
        raise ValueError("invalid frozen formal CLI relative path")
    if not isinstance(cli_sha256, str) or not re.fullmatch(
        r"[0-9a-f]{64}", cli_sha256
    ):
        raise ValueError("invalid frozen formal CLI SHA256")
    if isinstance(cli_size, bool) or not isinstance(cli_size, int) or cli_size <= 0:
        raise ValueError("invalid frozen formal CLI size")
    frozen_cli = {
        "relative_path": relative_cli,
        "sha256": cli_sha256,
        "size_bytes": cli_size,
    }
    return {
        "ok": True,
        "schema": manifest["schema"],
        "freeze_revision": freeze_revision,
        "manifest_path": str(manifest_path),
        "manifest_sha256": _sha256(manifest_path),
        "base_commit": manifest["base_commit"],
        "patch_path": str(patch),
        "patch_sha256": actual_patch_hash,
        "config_path": str(config),
        "config_sha256": actual_config_hash,
        "paths": paths,
        "new_paths": new_paths,
        "patched_file_sha256": dict(result_hashes),
        "formal_cli": frozen_cli,
    }


def verify_clean_base_tree(
    source_tree: str | Path, bundle: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Verify an untouched base tree and patch applicability."""

    frozen = dict(bundle or verify_frozen_bundle())
    source = Path(source_tree).resolve()
    head = _run("git", "rev-parse", "HEAD", cwd=source)
    if head != frozen["base_commit"]:
        raise ValueError(f"wrong QMAP base commit: {head}")
    if _run("git", "status", "--porcelain", cwd=source):
        raise ValueError("QMAP verification source tree is not clean")
    _run(
        "git", "apply", "--unidiff-zero", "--check",
        frozen["patch_path"], cwd=source,
    )
    return {
        "ok": True,
        "mode": "clean_base_apply_check",
        "source_tree": str(source),
        "head": head,
    }


def verify_patched_source_tree(
    source_tree: str | Path, bundle: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Verify the exact 24-file result of applying the frozen patch.

    Build directories may be untracked. They are ignored here; provenance is
    closed by the base HEAD, complete tracked-diff paths, explicit recognition
    of the five new paths, and SHA256 of every patched result file.
    """

    frozen = dict(bundle or verify_frozen_bundle())
    source = Path(source_tree).resolve()
    top_level = Path(
        _run("git", "rev-parse", "--show-toplevel", cwd=source)
    ).resolve()
    if top_level != source:
        raise ValueError(f"QMAP patched source must be its Git top-level: {top_level}")
    head = _run("git", "rev-parse", "HEAD", cwd=source)
    if head != frozen["base_commit"]:
        raise ValueError(f"wrong QMAP patched-source base commit: {head}")

    expected_paths = list(frozen["paths"])
    expected_set = set(expected_paths)
    new_set = set(frozen["new_paths"])
    baseline_set = expected_set - new_set
    raw_diff = _run("git", "diff", "--name-only", "HEAD", "--", cwd=source)
    tracked_diff = [line for line in raw_diff.splitlines() if line]
    tracked_set = set(tracked_diff)
    if len(tracked_set) != len(tracked_diff):
        raise ValueError("QMAP tracked diff contains duplicate paths")
    if tracked_set - expected_set:
        raise ValueError(
            "QMAP patched source has unrelated tracked changes: "
            f"{sorted(tracked_set - expected_set)}"
        )
    if not baseline_set.issubset(tracked_set):
        raise ValueError(
            "QMAP patched source is missing tracked patch paths: "
            f"{sorted(baseline_set - tracked_set)}"
        )

    untracked_patch_paths: list[str] = []
    for path in frozen["new_paths"]:
        if path in tracked_set:
            continue
        raw_untracked = _run(
            "git", "ls-files", "--others", "--exclude-standard", "--", path,
            cwd=source,
        )
        if raw_untracked.splitlines() != [path]:
            raise ValueError(f"QMAP patched source is missing new patch path: {path}")
        untracked_patch_paths.append(path)
    if tracked_set | set(untracked_patch_paths) != expected_set:
        raise ValueError("QMAP patched source path identity mismatch")

    actual_hashes: dict[str, str] = {}
    for path in expected_paths:
        candidate = source / path
        if not candidate.is_file():
            raise ValueError(f"QMAP patched result file is missing: {path}")
        actual_hashes[path] = _sha256(candidate)
    if actual_hashes != frozen["patched_file_sha256"]:
        mismatches = [
            path for path in expected_paths
            if actual_hashes[path] != frozen["patched_file_sha256"][path]
        ]
        raise ValueError(f"QMAP patched result SHA256 mismatch: {mismatches}")
    frozen_cli = dict(frozen["formal_cli"])
    binary = source / frozen_cli["relative_path"]
    if not binary.is_file():
        raise ValueError(f"frozen formal QMAP CLI is missing: {binary}")
    if binary.stat().st_size != frozen_cli["size_bytes"]:
        raise ValueError("frozen formal QMAP CLI size mismatch")
    if _sha256(binary) != frozen_cli["sha256"]:
        raise ValueError("frozen formal QMAP CLI SHA256 mismatch")
    if not binary.stat().st_mode & 0o111:
        raise ValueError("frozen formal QMAP CLI is not executable")
    return {
        "ok": True,
        "mode": "patched_source_identity",
        "source_tree": str(source),
        "head": head,
        "tracked_diff_paths": tracked_diff,
        "untracked_patch_paths": untracked_patch_paths,
        "patched_file_sha256": actual_hashes,
        "formal_cli": {
            **frozen_cli,
            "absolute_path": str(binary),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--source-tree",
        type=Path,
        help=(
            "optional clean v3.2.0 tree on which to run "
            "git apply --unidiff-zero --check"
        ),
    )
    group.add_argument(
        "--patched-source-tree",
        type=Path,
        help="optional v3.2.0 tree with the frozen patch already applied",
    )
    args = parser.parse_args()

    try:
        bundle = verify_frozen_bundle()
        source: Mapping[str, Any] | str = "not_requested"
        if args.source_tree is not None:
            source = verify_clean_base_tree(args.source_tree, bundle)
        elif args.patched_source_tree is not None:
            source = verify_patched_source_tree(args.patched_source_tree, bundle)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error)) from error

    print(json.dumps({**bundle, "source_tree_verification": source}, sort_keys=True))


if __name__ == "__main__":
    main()
