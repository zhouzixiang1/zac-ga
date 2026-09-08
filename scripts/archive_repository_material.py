#!/usr/bin/env python3
"""Seal, move, and verify explicitly listed repository material without clobbering.

The default invocation is read-only.  A plan is exactly
``{"moves": [{"source": "old/subdir", "destination": "archive/group/subdir"}]}``.
Use --seal with a new --run-dir, then --execute with the same plan and run.
An interrupted --execute may be repeated; receipts are never overwritten.
This is relocation, not deletion, deduplication, or a destructive rollback tool.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import datetime as dt
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import sys
import uuid


SCHEMA = "repository-relocation-v1"
PROTECTED = {".git", ".ssh", ".aws", ".codex", ".config", ".Trash", "venv", "env", "node_modules"}
BROAD_SOURCES = {"archive", "build", "docs"}


class ArchiveError(RuntimeError):
    """A fail-closed validation or execution error."""


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")) + "\n").encode()


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _exists(path: Path) -> bool:
    return os.path.lexists(path)


def _protected(name: str) -> bool:
    return name in PROTECTED or name.startswith(".venv") or name.startswith(".env")


def _relative(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ArchiveError("paths must be nonempty, literal relative strings")
    if any(ord(c) < 32 for c in value) or any(c in value for c in "\\*?[]{}~"):
        raise ArchiveError(f"unsupported path syntax: {value!r}")
    p = PurePosixPath(value)
    if p.is_absolute() or any(c in (".", "..") for c in value.split("/")) or p.as_posix() != value:
        raise ArchiveError(f"noncanonical or escaping path: {value!r}")
    if any(_protected(c) for c in p.parts):
        raise ArchiveError(f"protected path: {value}")
    return value


def _contains(a: str, b: str) -> bool:
    return a == b or b.startswith(a + "/")


def _no_symlink_ancestors(root: Path, relative: str, *, include_leaf: bool = True) -> None:
    parts = PurePosixPath(relative).parts
    current = root
    for part in parts if include_leaf else parts[:-1]:
        current /= part
        if _exists(current):
            if current.is_symlink():
                raise ArchiveError(f"symlink path component is not allowed: {current}")
            if current != root / relative and not current.is_dir():
                raise ArchiveError(f"non-directory path component: {current}")


def _read_regular(path: Path) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ArchiveError(f"not a regular file: {path}")
            data = stream.read()
            after = os.fstat(stream.fileno())
        current = path.lstat()
        keys = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(before, k) != getattr(after, k) or getattr(after, k) != getattr(current, k) for k in keys):
            raise ArchiveError(f"file changed while being read: {path}")
        return data
    except OSError as exc:
        raise ArchiveError(f"cannot safely read {path}: {exc}") from exc


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ArchiveError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _json(data: bytes) -> object:
    try:
        return json.loads(data, object_pairs_hook=_unique_pairs)
    except (ValueError, UnicodeError) as exc:
        raise ArchiveError(f"invalid JSON: {exc}") from exc


def _git(root: Path, *args: str, optional: bool = False) -> str | None:
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    result = subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false", "-C", str(root), *args],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if result.returncode:
        if optional:
            return None
        raise ArchiveError(f"Git inspection failed: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout.decode("utf-8", errors="surrogateescape")


def _root(path: Path) -> Path:
    root = path.resolve(strict=True)
    if not root.is_dir() or root == Path(root.anchor):
        raise ArchiveError("repository root must be a non-filesystem-root directory")
    top = _git(root, "rev-parse", "--show-toplevel")
    if Path(top.strip()).resolve() != root:
        raise ArchiveError("--root must be the actual Git repository root")
    return root


def _git_state(root: Path) -> dict:
    return {
        "head": (_git(root, "rev-parse", "--verify", "HEAD", optional=True) or "").strip() or None,
        "branch": (_git(root, "symbolic-ref", "--quiet", "--short", "HEAD", optional=True) or "").strip() or None,
        "porcelain_v1_z": _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all"),
    }


def _plan(root: Path, plan_path: Path) -> tuple[list[dict], bytes]:
    raw = _read_regular(plan_path)
    plan = _json(raw)
    if not isinstance(plan, dict) or set(plan) != {"moves"} or not isinstance(plan["moves"], list) or not plan["moves"]:
        raise ArchiveError("plan must contain only a nonempty moves list")
    endpoints = []
    moves = []
    script = Path(__file__).resolve()
    for move in plan["moves"]:
        if not isinstance(move, dict) or set(move) != {"source", "destination"}:
            raise ArchiveError("each move must contain exactly source and destination")
        source, destination = (_relative(move[k]) for k in ("source", "destination"))
        if source in BROAD_SOURCES:
            raise ArchiveError(f"broad source root is not allowed: {source}")
        if not destination.startswith("archive/"):
            raise ArchiveError("every destination must be strictly below archive/")
        for endpoint in (source, destination):
            if any(_contains(endpoint, other) or _contains(other, endpoint) for other in endpoints):
                raise ArchiveError(f"overlapping source/destination endpoints: {endpoint}")
            endpoints.append(endpoint)
            _no_symlink_ancestors(root, endpoint)
        for authority in (plan_path.resolve(), script):
            if authority.is_relative_to(root / source) or authority.is_relative_to(root / destination):
                raise ArchiveError("a move may not relocate its plan or executing tool")
        moves.append({"source": source, "destination": destination})
    return moves, raw


def _inventory(root: Path, relative: str, *, relocated_from: str | None = None) -> dict:
    _no_symlink_ancestors(root, relative)
    base = root / relative
    if not _exists(base):
        raise ArchiveError(f"missing inventory root: {relative}")
    entries = []

    def walk(path: Path) -> None:
        info = path.lstat()
        suffix = path.relative_to(base).as_posix()
        item = {"path": suffix, "mode": stat.S_IMODE(info.st_mode)}
        if stat.S_ISLNK(info.st_mode):
            target = os.readlink(path)
            if os.path.isabs(target):
                raise ArchiveError(f"absolute symlinks require a separate migration procedure: {path}")
            try:
                resolved = path.resolve(strict=False)
            except (RuntimeError, OSError) as exc:
                raise ArchiveError(f"unresolvable symlink: {path}") from exc
            if not resolved.is_relative_to(root):
                raise ArchiveError(f"symlink escapes repository: {path}")
            if not resolved.is_relative_to(base):
                raise ArchiveError(f"cross-tree symlinks require a separate migration procedure: {path}")
            # Check the same literal link at its future location before sealing.
            if relocated_from is not None:
                future = root / relocated_from / path.relative_to(base)
                future_target = Path(target) if os.path.isabs(target) else future.parent / target
                if not future_target.resolve(strict=False).is_relative_to(root):
                    raise ArchiveError(f"symlink would escape after relocation: {path}")
            item.update(type="symlink", target=target, bytes=len(os.fsencode(target)))
            entries.append(item)
        elif stat.S_ISDIR(info.st_mode):
            # Whole-directory moves must not accidentally carry an installed environment.
            if path != base and (path.name.startswith(".venv") or path.name in {"venv", "node_modules"}):
                raise ArchiveError(f"embedded environment requires a separate migration procedure: {path}")
            item.update(type="directory")
            entries.append(item)
            for child in sorted(path.iterdir(), key=lambda p: p.name):
                walk(child)
        elif stat.S_ISREG(info.st_mode):
            digest = hashlib.sha256()
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as stream:
                before = os.fstat(stream.fileno())
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
                after = os.fstat(stream.fileno())
            current = path.lstat()
            keys = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_mode")
            if any(getattr(info, k) != getattr(before, k) or getattr(before, k) != getattr(after, k) or getattr(after, k) != getattr(current, k) for k in keys):
                raise ArchiveError(f"file changed during inventory: {path}")
            item.update(type="file", bytes=info.st_size, sha256=digest.hexdigest())
            entries.append(item)
        else:
            raise ArchiveError(f"special filesystem objects cannot be archived: {path}")

    walk(base)
    return {
        "entries": entries,
        "entry_count": len(entries),
        "file_bytes": sum(e.get("bytes", 0) for e in entries if e["type"] == "file"),
        "sha256": _digest(_canonical(entries)),
    }


def preview(root: Path, plan_path: Path) -> dict:
    root = _root(root)
    moves, raw = _plan(root, plan_path)
    records = []
    for move in moves:
        if _exists(root / move["destination"]):
            raise ArchiveError(f"destination already exists: {move['destination']}")
        records.append({**move, "inventory": _inventory(root, move["source"], relocated_from=move["destination"])})
    return {
        "schema": SCHEMA, "root": str(root), "tool_sha256": _digest(_read_regular(Path(__file__).resolve())),
        "plan_sha256": _digest(raw), "moves_sha256": _digest(_canonical(moves)),
        "moves": records, "git_before": _git_state(root),
    }


def _run_path(root: Path, relative: str, moves: list[dict]) -> Path:
    relative = _relative(relative)
    parts = PurePosixPath(relative).parts
    if len(parts) != 3 or parts[0] not in {"build", "docs"} or parts[1] != "repository-reorganization":
        raise ArchiveError("run directory must be build/repository-reorganization/NAME or docs/repository-reorganization/NAME")
    _no_symlink_ancestors(root, relative)
    for move in moves:
        for endpoint in (move["source"], move["destination"]):
            if _contains(endpoint, relative) or _contains(relative, endpoint):
                raise ArchiveError("audit directory overlaps moved material")
    return root / relative


def _mkdir_parents(root: Path, directory: Path) -> None:
    relative = directory.relative_to(root).as_posix()
    _no_symlink_ancestors(root, relative)
    descriptor = _open_directory_nofollow(root)
    try:
        for component in directory.relative_to(root).parts:
            try:
                os.mkdir(component, dir_fd=descriptor)
            except FileExistsError:
                pass
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
    finally:
        os.close(descriptor)


def _open_directory_nofollow(path: Path) -> int:
    """Open every component without following links, and return a pinned handle."""
    if not path.is_absolute():
        raise ArchiveError("internal directory handles require absolute paths")
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in path.parts[1:]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _write_new(path: Path, data: bytes) -> None:
    # Publish only a complete durable document. An interrupted private pending
    # file remains as evidence; it cannot block or replace a later receipt.
    parent = _open_directory_nofollow(path.parent)
    pending_name = f".pending-{uuid.uuid4().hex}.json"
    try:
        descriptor = os.open(pending_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=parent)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fchmod(stream.fileno(), 0o444)
            os.fsync(stream.fileno())
        _exclusive_rename_at(parent, pending_name, parent, path.name)
        os.fsync(parent)
    finally:
        os.close(parent)


def _write_json_new(path: Path, data: object) -> None:
    _write_new(path, _canonical(data))


def seal(root: Path, plan_path: Path, run_relative: str) -> dict:
    before = preview(root, plan_path)
    root = Path(before["root"])
    run = _run_path(root, run_relative, before["moves"])
    if _exists(run):
        raise ArchiveError("run directory already exists; use a new run name")
    _mkdir_parents(root, run.parent)
    parent = _open_directory_nofollow(run.parent)
    try:
        os.mkdir(run.name, dir_fd=parent)  # An incomplete seal is not reusable.
        run_fd = os.open(run.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            for child in ("intents", "receipts", "events"):
                os.mkdir(child, dir_fd=run_fd)
            lock_fd = os.open("execute.lock", os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                              0o600, dir_fd=run_fd)
            os.close(lock_fd)
            os.fsync(run_fd)
        finally:
            os.close(run_fd)
        os.fsync(parent)
    finally:
        os.close(parent)
    raw = _read_regular(plan_path)
    if _digest(raw) != before["plan_sha256"]:
        raise ArchiveError("plan changed while sealing; abandon this incomplete run")
    before.update(run_directory=run_relative, created_utc=_now())
    _write_new(run / "plan.json", raw)
    _write_json_new(run / "seal.json", before)
    _write_json_new(run / "seal.digest.json", {"sha256": _digest(_canonical(before))})
    return {"status": "sealed", "run_directory": str(run), "moves": len(before["moves"]), "seal_sha256": _digest(_canonical(before))}


def _load_seal(root: Path, plan_path: Path, run_relative: str) -> tuple[Path, dict, str]:
    root = _root(root)
    moves, raw = _plan(root, plan_path)
    run = _run_path(root, run_relative, moves)
    for name in ("intents", "receipts", "events"):
        _no_symlink_ancestors(root, f"{run_relative}/{name}")
        if not (run / name).is_dir():
            raise ArchiveError(f"missing audit directory: {name}")
    data = _read_regular(run / "seal.json")
    fingerprint = _digest(data)
    if _json(_read_regular(run / "seal.digest.json")) != {"sha256": fingerprint}:
        raise ArchiveError("seal digest mismatch")
    before = _json(data)
    if not isinstance(before, dict) or before.get("schema") != SCHEMA:
        raise ArchiveError("unknown seal schema")
    if before.get("root") != str(root) or before.get("run_directory") != run_relative:
        raise ArchiveError("root or run directory differs from seal")
    if before.get("tool_sha256") != _digest(_read_regular(Path(__file__).resolve())):
        raise ArchiveError("executing tool changed since seal")
    if before.get("plan_sha256") != _digest(raw) or _read_regular(run / "plan.json") != raw:
        raise ArchiveError("plan changed since seal")
    if before.get("moves_sha256") != _digest(_canonical(moves)):
        raise ArchiveError("move parameters changed since seal")
    if [{k: m[k] for k in ("source", "destination")} for m in before["moves"]] != moves:
        raise ArchiveError("sealed move list differs from plan")
    for record in before["moves"]:
        inv = record["inventory"]
        if inv["sha256"] != _digest(_canonical(inv["entries"])):
            raise ArchiveError("sealed inventory digest mismatch")
    return run, before, fingerprint


@contextlib.contextmanager
def _execution_lock(run: Path):
    import fcntl
    parent = _open_directory_nofollow(run)
    try:
        descriptor = os.open("execute.lock", os.O_RDWR | os.O_NOFOLLOW, dir_fd=parent)
    finally:
        os.close(parent)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ArchiveError("execution lock is not a regular file")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ArchiveError("another execute/verify operation holds the run lock") from exc
        yield
    finally:
        os.close(descriptor)


def _record_base(index: int, move: dict, fingerprint: str) -> dict:
    return {"schema": SCHEMA, "index": index, "source": move["source"], "destination": move["destination"],
            "seal_sha256": fingerprint, "inventory_sha256": move["inventory"]["sha256"]}


def _read_record(path: Path, expected: dict) -> dict | None:
    if not _exists(path):
        return None
    record = _json(_read_regular(path))
    if not isinstance(record, dict) or any(record.get(k) != v for k, v in expected.items()):
        raise ArchiveError(f"audit record mismatch: {path}")
    return record


def _check_inventory(root: Path, relative: str, expected: dict) -> None:
    actual = _inventory(root, relative)
    if actual != expected:
        raise ArchiveError(f"inventory drift: {relative}")


def _states(root: Path, run: Path, before: dict, fingerprint: str) -> list[str]:
    states = []
    for index, move in enumerate(before["moves"]):
        source, destination = root / move["source"], root / move["destination"]
        _no_symlink_ancestors(root, move["source"])
        _no_symlink_ancestors(root, move["destination"])
        expected = _record_base(index, move, fingerprint)
        intent = _read_record(run / "intents" / f"{index:04d}.json", expected)
        receipt = _read_record(run / "receipts" / f"{index:04d}.json", expected)
        if _exists(source) and not _exists(destination):
            if receipt is not None:
                raise ArchiveError(f"completed receipt has an unmoved source: {move['source']}")
            _check_inventory(root, move["source"], move["inventory"])
            states.append("pending")
        elif not _exists(source) and _exists(destination):
            if intent is None:
                raise ArchiveError(f"destination exists without an execution intent: {move['destination']}")
            _check_inventory(root, move["destination"], move["inventory"])
            states.append("completed" if receipt is not None else "recoverable")
        else:
            raise ArchiveError(f"ambiguous move state (both paths exist or neither exists): {move['source']}")
    return states


def _exclusive_rename_at(source_fd: int, source: str, destination_fd: int, destination: str) -> None:
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin" and hasattr(library, "renameatx_np"):
        operation = library.renameatx_np
        exclusive_flag = 4  # RENAME_EXCL
    elif sys.platform.startswith("linux") and hasattr(library, "renameat2"):
        operation = library.renameat2
        exclusive_flag = 1  # RENAME_NOREPLACE
    else:
        raise ArchiveError("atomic exclusive rename is unavailable; refusing an unsafe fallback")
    operation.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    operation.restype = ctypes.c_int
    if operation(source_fd, os.fsencode(source), destination_fd, os.fsencode(destination), exclusive_flag) != 0:
        number = ctypes.get_errno()
        if number == errno.EXDEV:
            raise ArchiveError("cross-filesystem relocation is unsupported; no copy/delete fallback")
        raise OSError(number, os.strerror(number), destination)


def _rename_no_replace(source: Path, destination: Path) -> None:
    """Atomic same-filesystem rename through pinned, no-follow parent handles."""
    source_fd = _open_directory_nofollow(source.parent)
    try:
        destination_fd = _open_directory_nofollow(destination.parent)
        try:
            source_info = os.stat(source.name, dir_fd=source_fd, follow_symlinks=False)
            if not (stat.S_ISREG(source_info.st_mode) or stat.S_ISDIR(source_info.st_mode)):
                raise ArchiveError("source became a symlink or special file before rename")
            _exclusive_rename_at(source_fd, source.name, destination_fd, destination.name)
            os.fsync(source_fd)
            os.fsync(destination_fd)
        finally:
            os.close(destination_fd)
    finally:
        os.close(source_fd)


def _event(run: Path, event: dict) -> None:
    _write_json_new(run / "events" / f"{uuid.uuid4().hex}.json", {"created_utc": _now(), **event})


def execute(root: Path, plan_path: Path, run_relative: str) -> dict:
    run, before, fingerprint = _load_seal(root, plan_path, run_relative)
    root = Path(before["root"])
    with _execution_lock(run):
        try:
            # Validate every source or completed destination before making the first move.
            states = _states(root, run, before, fingerprint)
            for index, (move, state) in enumerate(zip(before["moves"], states)):
                if state == "completed":
                    continue
                record = _record_base(index, move, fingerprint)
                if state == "pending":
                    _check_inventory(root, move["source"], move["inventory"])
                    _no_symlink_ancestors(root, move["source"])
                    _no_symlink_ancestors(root, move["destination"])
                    destination = root / move["destination"]
                    if _exists(destination):
                        raise ArchiveError(f"destination appeared during execution: {destination}")
                    intent_path = run / "intents" / f"{index:04d}.json"
                    if not _exists(intent_path):
                        _write_json_new(intent_path, {**record, "created_utc": _now()})
                    _mkdir_parents(root, destination.parent)
                    _rename_no_replace(root / move["source"], destination)
                # Also handles a previous crash after rename but before receipt creation.
                if _exists(root / move["source"]):
                    raise ArchiveError(f"source still exists after relocation: {move['source']}")
                _check_inventory(root, move["destination"], move["inventory"])
                _write_json_new(run / "receipts" / f"{index:04d}.json",
                                {**record, "created_utc": _now(), "recovered_after_interruption": state == "recoverable"})
            if any(s != "completed" for s in _states(root, run, before, fingerprint)):
                raise ArchiveError("execution did not produce all completion receipts")
            _event(run, {"status": "verified", "seal_sha256": fingerprint, "git_after": _git_state(root)})
            return {"status": "verified", "moves": len(states), "run_directory": str(run)}
        except (ArchiveError, OSError) as exc:
            _event(run, {"status": "stopped", "seal_sha256": fingerprint, "error": str(exc),
                         "instruction": "Do not roll back destructively. Inspect receipts and repeat --execute after resolving the cause."})
            raise


def verify(root: Path, plan_path: Path, run_relative: str) -> dict:
    run, before, fingerprint = _load_seal(root, plan_path, run_relative)
    with _execution_lock(run):
        states = _states(Path(before["root"]), run, before, fingerprint)
        if any(s != "completed" for s in states):
            raise ArchiveError("not all moves have verified completion receipts; use --execute to resume")
    return {"status": "verified", "moves": len(states), "run_directory": str(run)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--run-dir")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--seal", action="store_true")
    actions.add_argument("--execute", action="store_true")
    actions.add_argument("--verify", action="store_true")
    args = parser.parse_args(argv)
    if (args.seal or args.execute or args.verify) and not args.run_dir:
        parser.error("--run-dir is required for --seal, --execute, and --verify")
    if not (args.seal or args.execute or args.verify) and args.run_dir:
        parser.error("default read-only preview does not use --run-dir")
    try:
        if args.seal:
            result = seal(args.root, args.plan, args.run_dir)
        elif args.execute:
            result = execute(args.root, args.plan, args.run_dir)
        elif args.verify:
            result = verify(args.root, args.plan, args.run_dir)
        else:
            result = {"status": "preview_only", **preview(args.root, args.plan)}
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0
    except (ArchiveError, OSError, KeyError, TypeError) as exc:
        print(f"STOPPED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
