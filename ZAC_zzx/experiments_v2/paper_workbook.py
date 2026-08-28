"""Author the two-sheet Chinese-paper workbook with ``artifact-tool`` only."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import zipfile
from pathlib import Path
from typing import Any, Mapping, Sequence
from xml.etree import ElementTree

from .workbook_renderer import _normalize_xlsx, _resolve_runtime


EXPECTED_SHEETS = ("ZAC18", "QMAP154")
EXPECTED_ROWS = {"ZAC18": 18, "QMAP154": 154}
FREEZE_ROWS = 14
FREEZE_COLUMNS = 4
FREEZE_TOP_LEFT_CELL = "E15"

_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_OFFICE_REL_NS = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships")
_PACKAGE_REL_NS = (
    "http://schemas.openxmlformats.org/package/2006/relationships")
_SELF_CLOSING_SHEET_VIEW = re.compile(rb"(<x:sheetView\b[^>]*)\s*/>")


def _worksheet_targets(path: Path) -> Mapping[str, str]:
    with zipfile.ZipFile(path, "r") as archive:
        workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        relationships = ElementTree.fromstring(
            archive.read("xl/_rels/workbook.xml.rels"))
    targets = {
        item.attrib["Id"]: item.attrib["Target"]
        for item in relationships.findall(
            f"{{{_PACKAGE_REL_NS}}}Relationship")
    }
    worksheets: dict[str, str] = {}
    for sheet in workbook.findall(f".//{{{_MAIN_NS}}}sheet"):
        relationship_id = sheet.attrib[f"{{{_OFFICE_REL_NS}}}id"]
        target = targets[relationship_id].lstrip("/")
        worksheets[sheet.attrib["name"]] = (
            target if target.startswith("xl/") else f"xl/{target}")
    return worksheets


def _repair_and_verify_freeze_panes(path: Path) -> Mapping[str, Any]:
    """Repair the current artifact-tool freeze serialization gap.

    The workbook is still authored and rendered exclusively by artifact-tool.
    Version 2.8.52 accepts ``freezeRows``/``freezeColumns`` but omits ``pane``
    from OOXML, so this deterministic post-export repair only restores that
    requested worksheet-view metadata; it does not touch values or styles.
    """
    targets = _worksheet_targets(path)
    if tuple(targets) != EXPECTED_SHEETS:
        raise RuntimeError(f"paper workbook sheet drift before freeze repair: {targets}")
    pane = (
        f'<x:pane xSplit="{FREEZE_COLUMNS}" ySplit="{FREEZE_ROWS}" '
        f'topLeftCell="{FREEZE_TOP_LEFT_CELL}" activePane="bottomRight" '
        'state="frozen" />').encode("ascii")
    entries: list[tuple[zipfile.ZipInfo, bytes]] = []
    with zipfile.ZipFile(path, "r") as archive:
        for info in sorted(archive.infolist(), key=lambda item: item.filename):
            data = archive.read(info.filename)
            if info.filename in targets.values():
                worksheet = ElementTree.fromstring(data)
                existing = worksheet.find(f".//{{{_MAIN_NS}}}pane")
                if existing is not None:
                    if (existing.attrib.get("xSplit") != str(FREEZE_COLUMNS) or
                            existing.attrib.get("ySplit") != str(FREEZE_ROWS) or
                            existing.attrib.get("topLeftCell") !=
                            FREEZE_TOP_LEFT_CELL or
                            existing.attrib.get("state") != "frozen"):
                        raise RuntimeError(
                            "artifact-tool emitted unexpected freeze panes: "
                            f"{info.filename}")
                else:
                    data, replacements = _SELF_CLOSING_SHEET_VIEW.subn(
                        rb"\1>" + pane + rb"</x:sheetView>", data, count=1)
                    if replacements != 1:
                        raise RuntimeError(
                            "artifact-tool worksheet view cannot be repaired "
                            f"safely: {info.filename}")
            entries.append((info, data))
    temporary = path.with_name(path.stem + ".freeze" + path.suffix)
    temporary.unlink(missing_ok=True)
    try:
        with zipfile.ZipFile(
                temporary, "w", compression=zipfile.ZIP_DEFLATED,
                compresslevel=9) as archive:
            for original, data in entries:
                info = zipfile.ZipInfo(
                    filename=original.filename,
                    date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = original.external_attr
                info.flag_bits = original.flag_bits
                archive.writestr(
                    info, data, compress_type=zipfile.ZIP_DEFLATED,
                    compresslevel=9)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    _normalize_xlsx(path)

    verified: dict[str, str] = {}
    with zipfile.ZipFile(path, "r") as archive:
        for sheet_name, target in targets.items():
            worksheet = ElementTree.fromstring(archive.read(target))
            node = worksheet.find(f".//{{{_MAIN_NS}}}pane")
            if (node is None or node.attrib.get("xSplit") != str(FREEZE_COLUMNS) or
                    node.attrib.get("ySplit") != str(FREEZE_ROWS) or
                    node.attrib.get("topLeftCell") != FREEZE_TOP_LEFT_CELL or
                    node.attrib.get("state") != "frozen"):
                raise RuntimeError(
                    f"paper workbook freeze-pane verification failed: {sheet_name}")
            verified[sheet_name] = target
    return {
        "rows": FREEZE_ROWS,
        "columns": FREEZE_COLUMNS,
        "top_left_cell": FREEZE_TOP_LEFT_CELL,
        "verified": True,
        "worksheets": verified,
    }


def export_paper_workbook(
        aggregate_dir: str | Path,
        output_path: str | Path,
        *,
        qa_directory: str | Path | None = None,
        node: str | Path | None = None,
        node_modules: str | Path | None = None,
        timeout_seconds: float = 180.0) -> Mapping[str, Any]:
    """Render and QA the final ZAC18/QMAP154 workbook.

    The aggregate directory is read-only.  The renderer accepts only
    ``zac18.csv``, ``qmap154.csv`` and ``main_summary.json`` and is therefore
    unable to add QASMBench, Large, runtime, sensitivity or hidden sheets.
    """
    source = Path(aggregate_dir).expanduser().resolve()
    destination = Path(output_path).expanduser().resolve()
    required = (source / "zac18.csv", source / "qmap154.csv",
                source / "main_summary.json")
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"paper workbook inputs are missing: {missing}")
    if timeout_seconds <= 0:
        raise ValueError("workbook render timeout must be positive")
    renderer = Path(__file__).with_name("render_paper_workbook.mjs")
    if not renderer.is_file():
        raise RuntimeError(f"paper workbook renderer is missing: {renderer}")
    node_path, modules_path = _resolve_runtime(node, node_modules)
    qa = (Path(qa_directory).expanduser().resolve()
          if qa_directory is not None else destination.parent / "paper_workbook_qa")
    destination.parent.mkdir(parents=True, exist_ok=True)
    qa.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.stem + ".tmp" + destination.suffix)
    sidecar = Path(str(temporary) + ".inspect.ndjson")
    qa_sidecar = qa / "workbook_export.inspect.ndjson"
    temporary.unlink(missing_ok=True)
    sidecar.unlink(missing_ok=True)
    qa_sidecar.unlink(missing_ok=True)
    command = [
        str(node_path), str(renderer), str(source), str(temporary), str(qa),
        str(modules_path),
    ]
    try:
        result = subprocess.run(
            command, check=False, capture_output=True, text=True,
            timeout=timeout_seconds)
    except subprocess.TimeoutExpired as error:
        temporary.unlink(missing_ok=True)
        sidecar.unlink(missing_ok=True)
        raise RuntimeError(
            f"paper XLSX rendering exceeded {timeout_seconds:g} seconds") from error
    if result.returncode != 0:
        temporary.unlink(missing_ok=True)
        sidecar.unlink(missing_ok=True)
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(
            f"paper XLSX renderer failed with exit code {result.returncode}: {detail}")
    if not temporary.is_file() or temporary.stat().st_size == 0:
        raise RuntimeError("paper XLSX renderer returned no workbook")
    os.replace(temporary, destination)
    _normalize_xlsx(destination)
    freeze_panes = _repair_and_verify_freeze_panes(destination)
    if sidecar.is_file():
        os.replace(sidecar, qa_sidecar)
    qa_path = qa / "workbook_qa.json"
    if not qa_path.is_file():
        raise RuntimeError("paper XLSX renderer omitted workbook_qa.json")
    payload = json.loads(qa_path.read_text(encoding="utf-8"))
    payload["output_xlsx"] = str(destination)
    payload["freeze_panes"] = freeze_panes
    qa_temporary = qa_path.with_name(f".{qa_path.name}.tmp")
    qa_temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    qa_temporary.replace(qa_path)
    names = tuple(item.get("name") for item in payload.get("sheets", ()))
    if names != EXPECTED_SHEETS:
        raise RuntimeError(f"paper workbook sheet drift: {names}")
    row_counts = {item["name"]: item.get("rows")
                  for item in payload.get("sheets", ())}
    if row_counts != EXPECTED_ROWS:
        raise RuntimeError(f"paper workbook row-count drift: {row_counts}")
    if payload.get("formula_errors"):
        raise RuntimeError(
            f"paper workbook formula errors: {payload['formula_errors']}")
    previews = [qa / name for name in payload.get("previews", ())]
    if len(previews) != 4 or any(not path.is_file() for path in previews):
        raise RuntimeError("paper workbook must render two previews per sheet")
    return {
        "xlsx_path": str(destination),
        "qa_path": str(qa_path),
        "qa_artifact_paths": [
            str(path) for path in (qa_path, qa_sidecar) if path.is_file()
        ],
        "preview_paths": [str(path) for path in previews],
        "sheet_names": list(names),
        "row_counts": row_counts,
        "column_count": payload.get("column_count"),
        "freeze_panes": freeze_panes,
        "artifact_tool_version": payload.get("artifact_tool_version"),
        "node": str(node_path),
        "node_modules": str(modules_path),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render the final two-sheet Chinese-paper workbook")
    parser.add_argument("aggregate_dir", type=Path)
    parser.add_argument("output_xlsx", type=Path)
    parser.add_argument("--qa-directory", type=Path)
    parser.add_argument("--node", type=Path)
    parser.add_argument("--node-modules", type=Path)
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    result = export_paper_workbook(
        args.aggregate_dir, args.output_xlsx,
        qa_directory=args.qa_directory, node=args.node,
        node_modules=args.node_modules, timeout_seconds=args.timeout_seconds)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()


__all__ = ["EXPECTED_ROWS", "EXPECTED_SHEETS", "export_paper_workbook", "main"]
