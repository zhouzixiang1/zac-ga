"""Author the two-sheet Chinese-paper workbook with ``artifact-tool`` only."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence

from .workbook_renderer import _normalize_xlsx, _resolve_runtime


EXPECTED_SHEETS = ("ZAC18", "QMAP154")
EXPECTED_ROWS = {"ZAC18": 18, "QMAP154": 154}


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
    temporary.unlink(missing_ok=True)
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
        raise RuntimeError(
            f"paper XLSX rendering exceeded {timeout_seconds:g} seconds") from error
    if result.returncode != 0:
        temporary.unlink(missing_ok=True)
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(
            f"paper XLSX renderer failed with exit code {result.returncode}: {detail}")
    if not temporary.is_file() or temporary.stat().st_size == 0:
        raise RuntimeError("paper XLSX renderer returned no workbook")
    os.replace(temporary, destination)
    _normalize_xlsx(destination)
    qa_path = qa / "workbook_qa.json"
    if not qa_path.is_file():
        raise RuntimeError("paper XLSX renderer omitted workbook_qa.json")
    payload = json.loads(qa_path.read_text(encoding="utf-8"))
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
        "preview_paths": [str(path) for path in previews],
        "sheet_names": list(names),
        "row_counts": row_counts,
        "column_count": payload.get("column_count"),
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
