"""Render the normalized Schema-2 workbook contract to a verified XLSX."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Any, Mapping, Optional


_BUNDLED_RUNTIME = (
    Path.home() / ".cache" / "codex-runtimes" / "codex-primary-runtime" /
    "dependencies" / "node"
)
_RELATIONSHIP_ID = re.compile(rb"(?<![A-Za-z0-9])R[0-9a-fA-F]{16}(?![A-Za-z0-9])")


def _normalize_xlsx(path: Path) -> None:
    """Remove random relationship IDs and ZIP timestamps from artifact output."""
    entries: list[tuple[zipfile.ZipInfo, bytes]] = []
    with zipfile.ZipFile(path, "r") as archive:
        for info in sorted(archive.infolist(), key=lambda item: item.filename):
            entries.append((info, archive.read(info.filename)))
    identifiers: dict[bytes, bytes] = {}

    def replacement(match: re.Match[bytes]) -> bytes:
        original = match.group(0)
        if original not in identifiers:
            identifiers[original] = f"R{len(identifiers) + 1:016x}".encode("ascii")
        return identifiers[original]

    normalized: list[tuple[zipfile.ZipInfo, bytes]] = []
    for info, data in entries:
        if info.filename.endswith((".xml", ".rels")):
            data = _RELATIONSHIP_ID.sub(replacement, data)
        normalized.append((info, data))
    temporary = path.with_name(path.stem + ".normalized" + path.suffix)
    temporary.unlink(missing_ok=True)
    try:
        with zipfile.ZipFile(
                temporary, "w", compression=zipfile.ZIP_DEFLATED,
                compresslevel=9) as archive:
            for original, data in normalized:
                info = zipfile.ZipInfo(
                    filename=original.filename,
                    date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = original.external_attr
                info.flag_bits = original.flag_bits
                archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED,
                                 compresslevel=9)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _resolve_runtime(
        node: Optional[str | Path] = None,
        node_modules: Optional[str | Path] = None) -> tuple[Path, Path]:
    node_candidates = [
        Path(node).expanduser() if node is not None else None,
        Path(os.environ["ZAC_ARTIFACT_NODE"]).expanduser()
        if os.environ.get("ZAC_ARTIFACT_NODE") else None,
        _BUNDLED_RUNTIME / "bin" / "node",
        Path(shutil.which("node")) if shutil.which("node") else None,
    ]
    modules_candidates = [
        Path(node_modules).expanduser() if node_modules is not None else None,
        Path(os.environ["ZAC_ARTIFACT_NODE_MODULES"]).expanduser()
        if os.environ.get("ZAC_ARTIFACT_NODE_MODULES") else None,
        _BUNDLED_RUNTIME / "node_modules",
    ]
    node_path = next(
        (candidate.resolve() for candidate in node_candidates
         if candidate is not None and candidate.is_file()), None)
    modules_path = next(
        (candidate.resolve() for candidate in modules_candidates
         if candidate is not None and
         (candidate / "@oai" / "artifact-tool").exists()), None)
    if node_path is None:
        raise RuntimeError(
            "XLSX rendering requires Node.js; set ZAC_ARTIFACT_NODE")
    if modules_path is None:
        raise RuntimeError(
            "XLSX rendering requires @oai/artifact-tool; set "
            "ZAC_ARTIFACT_NODE_MODULES")
    return node_path, modules_path


def render_workbook(
        contract_path: str | Path, output_path: str | Path, *,
        qa_directory: Optional[str | Path] = None,
        node: Optional[str | Path] = None,
        node_modules: Optional[str | Path] = None,
        timeout_seconds: float = 180.0) -> Mapping[str, Any]:
    """Render and inspect one workbook using the approved artifact runtime."""
    contract = Path(contract_path).expanduser().resolve()
    destination = Path(output_path).expanduser().resolve()
    if not contract.is_file():
        raise ValueError(f"workbook contract does not exist: {contract}")
    if timeout_seconds <= 0:
        raise ValueError("workbook render timeout must be positive")
    renderer = Path(__file__).with_name("render_workbook.mjs")
    if not renderer.is_file():
        raise RuntimeError(f"workbook renderer is missing: {renderer}")
    node_path, modules_path = _resolve_runtime(node, node_modules)
    qa = (Path(qa_directory).expanduser().resolve()
          if qa_directory is not None else destination.parent / "workbook_qa")
    destination.parent.mkdir(parents=True, exist_ok=True)
    qa.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.stem + ".tmp" + destination.suffix)
    temporary.unlink(missing_ok=True)
    command = [
        str(node_path), str(renderer), str(contract), str(temporary),
        str(qa), str(modules_path),
    ]
    try:
        result = subprocess.run(
            command, check=False, capture_output=True, text=True,
            timeout=timeout_seconds)
    except subprocess.TimeoutExpired as error:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(
            f"XLSX rendering exceeded {timeout_seconds:g} seconds") from error
    if result.returncode != 0:
        temporary.unlink(missing_ok=True)
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(
            f"XLSX renderer failed with exit code {result.returncode}: {detail}")
    if not temporary.is_file() or temporary.stat().st_size == 0:
        raise RuntimeError("XLSX renderer returned success without a workbook")
    os.replace(temporary, destination)
    _normalize_xlsx(destination)
    sidecar = Path(str(temporary) + ".inspect.ndjson")
    qa_sidecar = qa / "workbook_export.inspect.ndjson"
    if sidecar.is_file():
        os.replace(sidecar, qa_sidecar)
    qa_path = qa / "workbook_qa.json"
    if not qa_path.is_file():
        raise RuntimeError("XLSX renderer did not emit its QA record")
    payload = json.loads(qa_path.read_text(encoding="utf-8"))
    if payload.get("formula_errors"):
        raise RuntimeError(
            f"XLSX QA found formula errors: {payload['formula_errors']}")
    preview_paths = [qa / name for name in payload.get("previews", [])]
    if any(not path.is_file() for path in preview_paths):
        raise RuntimeError("XLSX renderer omitted one or more sheet previews")
    return {
        "xlsx_path": str(destination),
        "qa_path": str(qa_path),
        "qa_artifact_paths": [
            str(path) for path in (qa_path, qa_sidecar) if path.is_file()
        ],
        "preview_paths": [str(path) for path in preview_paths],
        "artifact_tool_version": payload.get("artifact_tool_version"),
        "node": str(node_path),
        "node_modules": str(modules_path),
    }


__all__ = ["render_workbook"]
