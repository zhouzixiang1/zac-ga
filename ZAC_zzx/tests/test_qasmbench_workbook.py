"""Focused validation tests for the artifact-tool QASMBench renderer.

The tests exercise the renderer's read-only ``--validate-only`` mode.  They do
not create an XLSX, so the one-time artifact-authoring marker remains the
responsibility of the final delivery command.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from experiments_v2.qasmbench_aggregate import (  # noqa: E402
    CANONICAL_PROFILE,
    QASMBENCH_COMMIT,
    QASMBENCH_REPOSITORY,
    SOURCE_MANIFEST_SCHEMA,
    aggregate_qasmbench,
)


RENDERER = ROOT / "experiments_v2" / "render_qasmbench_workbook.mjs"


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _entry(scale: str, directory: str, *, no_source: bool = False) -> dict:
    if no_source:
        return {
            "benchmark_scale": scale,
            "benchmark_directory": directory,
            "upstream_path": f"{scale}/{directory}",
            "upstream_git_blob": "",
            "source_sha256": "",
            "selection_reason": "no_qasm_source",
            "canonical_profile": CANONICAL_PROFILE,
            "status": "no_qasm_source",
            "canonical_path": "",
            "canonical_sha256": "",
            "qubits": None,
            "gates_1q": None,
            "gates_2q": None,
            "error": "no_qasm_source",
        }
    return {
        "benchmark_scale": scale,
        "benchmark_directory": directory,
        "upstream_path": f"{scale}/{directory}/{directory}_transpiled.qasm",
        "upstream_git_blob": "a" * 40,
        "source_sha256": _digest(f"source-{scale}-{directory}"),
        "selection_reason": "preferred_transpiled",
        "canonical_profile": CANONICAL_PROFILE,
        "status": "success",
        "canonical_path": f"/canonical/{scale}/{directory}.qasm",
        "canonical_sha256": _digest(f"canonical-{scale}-{directory}"),
        "qubits": 8,
        "gates_1q": 10,
        "gates_2q": 20,
        "error": None,
    }


def _complete_aggregate() -> dict:
    entries: list[dict] = []
    for scale, count in (("small", 42), ("medium", 25), ("large", 70)):
        for index in range(count):
            entries.append(_entry(
                scale,
                f"{scale}_circuit_{index:03d}",
                no_source=scale == "large" and index < 6,
            ))
    source = {
        "manifest_schema": SOURCE_MANIFEST_SCHEMA,
        "upstream_repository": QASMBENCH_REPOSITORY,
        "upstream_commit": QASMBENCH_COMMIT,
        "canonical_profile": CANONICAL_PROFILE,
        "trace_retained": False,
        "counts": {
            "directories": {"small": 42, "medium": 25, "large": 70},
            "selected_qasm": 131,
            "success": 131,
            "canonical_error": 0,
            "no_qasm_source": 6,
        },
        "entries": entries,
    }
    payload = aggregate_qasmbench(source, [])
    payload.pop("source_manifest")
    return payload


def _validate(payload: dict) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "aggregate_summary.json"
        source.write_text(json.dumps(payload), encoding="utf-8")
        return subprocess.run(
            ["node", str(RENDERER), "--validate-only", str(source)],
            check=False,
            capture_output=True,
            text=True,
        )


class TestQasmbenchWorkbookRenderer(unittest.TestCase):
    def test_full_inventory_has_exact_four_sheets_and_137_detail_rows(self):
        result = _validate(_complete_aggregate())
        self.assertEqual(result.returncode, 0, result.stderr)
        validation = json.loads(result.stdout)
        self.assertEqual(
            validation["sheet_names"],
            ["Summary", "Small", "Medium", "Large"],
        )
        self.assertEqual(validation["exact_sheet_count"], 4)
        self.assertEqual(validation["row_counts"], {
            "Summary": 12,
            "Small": 42,
            "Medium": 25,
            "Large": 70,
        })
        self.assertTrue(validation["complete_inventory"])
        self.assertEqual(validation["expected_official_directory_count"], 137)
        self.assertEqual(validation["rendered_detail_column_count"], 52)

    def test_partial_results_remain_renderable(self):
        payload = _complete_aggregate()
        payload["sheets"]["Small"] = payload["sheets"]["Small"][:3]
        payload["sheets"]["Medium"] = []
        payload["sheets"]["Large"] = payload["sheets"]["Large"][:2]
        result = _validate(payload)
        self.assertEqual(result.returncode, 0, result.stderr)
        validation = json.loads(result.stdout)
        self.assertFalse(validation["complete_inventory"])
        self.assertEqual(validation["row_counts"]["Small"], 3)
        self.assertEqual(validation["row_counts"]["Medium"], 0)
        self.assertEqual(validation["row_counts"]["Large"], 2)

    def test_duplicate_official_directory_is_rejected(self):
        payload = _complete_aggregate()
        payload["sheets"]["Small"] = payload["sheets"]["Small"][:2]
        payload["sheets"]["Small"].append(
            dict(payload["sheets"]["Small"][0]))
        result = _validate(payload)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("duplicate directory", result.stderr)

    def test_renderer_contract_contains_required_native_timing_columns(self):
        source = RENDERER.read_text(encoding="utf-8")
        for suffix in (
            "initial_placement_s",
            "problem_preparation_s",
            "native_search_s",
            "search_kernel_s",
            "return_match_s",
            "forecast_s",
            "result_commit_s",
            "routing_s",
        ):
            self.assertIn(suffix, source)
        self.assertIn('@oai/artifact-tool', source)
        self.assertNotIn("openpyxl", source)
        self.assertNotIn("xlsxwriter", source)


if __name__ == "__main__":
    unittest.main()
