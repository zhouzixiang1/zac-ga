from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace

from experiments_v2.cli import build_parser
from experiments_v2.contracts import RunManifest, RunStatus
from experiments_v2.protocol import (
    ghost_policy_for_method,
    physicalization_policy_for_method,
    trace_protocol_for_method,
)
from experiments_v2.qasmbench_runner import (
    MINIMUM_FREE_BYTES,
    QASMBenchJob,
    QASMBenchSource,
    RSS_LIMIT_BYTES,
    _integral_count,
    _execute_stage,
    _run_job,
    command_run_qasmbench,
    load_qasmbench_sources,
    timeout_seconds_for_two_qubit_gates,
)
from experiments_v2.runner import _apply_metrics, _discard_qasmbench_artifacts


class QASMBenchRunnerTests(unittest.TestCase):
    def test_timeout_tiers_are_closed_at_specified_boundaries(self) -> None:
        self.assertEqual(timeout_seconds_for_two_qubit_gates(0), 600)
        self.assertEqual(timeout_seconds_for_two_qubit_gates(10_000), 600)
        self.assertEqual(timeout_seconds_for_two_qubit_gates(10_001), 1_800)
        self.assertEqual(timeout_seconds_for_two_qubit_gates(250_000), 1_800)
        self.assertEqual(timeout_seconds_for_two_qubit_gates(250_001), 3_600)
        with self.assertRaises(ValueError):
            timeout_seconds_for_two_qubit_gates(-1)

    def test_transfer_metric_is_integral_and_float_safe(self) -> None:
        manifest = RunManifest()
        _apply_metrics(manifest, {"transfers": 12.0})
        self.assertEqual(manifest.transfers, 12)
        self.assertIsInstance(manifest.transfers, int)
        for invalid in (12.5, float("nan"), float("inf"), True, -1):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    _apply_metrics(RunManifest(), {"transfers": invalid})
        self.assertEqual(_integral_count(7.0, "transfers"), 7)
        with self.assertRaises(ValueError):
            _integral_count(7.25, "transfers")

    def test_transient_trace_is_hashed_then_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            content = b'{"kind":"move"}\n'
            with gzip.open(root / "canonical_trace.jsonl.gz", "wb") as handle:
                handle.write(content)
            (root / "compiler_stats.json").write_text("{}\n", encoding="utf-8")
            manifest = RunManifest(status=RunStatus.SUCCESS.value)
            _discard_qasmbench_artifacts(root, manifest)
            self.assertEqual(
                manifest.event_stream_sha256, hashlib.sha256(content).hexdigest())
            self.assertFalse(manifest.trace_retained)
            self.assertFalse((root / "canonical_trace.jsonl.gz").exists())
            self.assertFalse((root / "compiler_stats.json").exists())

    def test_failed_attempt_deletes_partial_trace_without_claiming_event_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with gzip.open(root / "canonical_trace.jsonl.gz", "wb") as handle:
                handle.write(b"partial\n")
            manifest = RunManifest(status=RunStatus.TIMEOUT.value)
            _discard_qasmbench_artifacts(root, manifest)
            self.assertEqual(manifest.event_stream_sha256, "")
            self.assertFalse((root / "canonical_trace.jsonl.gz").exists())

    def test_qasmbench_contract_forbids_proxy(self) -> None:
        method = "M1"
        manifest = RunManifest(
            run_id="run", dataset="qasmbench_small", circuit="adder_n4",
            method=method, run_kind="qasmbench", experiment_id="e" * 64,
            benchmark_scale="small", benchmark_directory="adder_n4",
            upstream_git_blob="a" * 40,
            input_selection_reason="preferred_transpiled",
            canonical_profile="qasmbench_standard_expand_v1",
            rss_limit_bytes=RSS_LIMIT_BYTES,
            minimum_free_bytes=MINIMUM_FREE_BYTES,
            concurrency_limit=4,
            trace_retained=False,
            implementation_status="development_streaming_proxy_v1",
            trace_protocol=trace_protocol_for_method(method),
            ghost_policy=ghost_policy_for_method(method),
            physicalization_policy=physicalization_policy_for_method(method),
        )
        with self.assertRaisesRegex(ValueError, "proxy"):
            manifest.validate()

    def test_large_dispatch_preserves_original_baselines(self) -> None:
        source = QASMBenchSource(
            benchmark_scale="large", benchmark_directory="circuit",
            upstream_path="large/circuit/circuit.qasm",
            upstream_git_blob="a" * 40, source_sha256="b" * 64,
            selection_reason="preferred_named",
            canonical_profile="qasmbench_standard_expand_v1",
            status="success", canonical_path="unused",
            canonical_sha256="c" * 64, qubits=2, gates_1q=0, gates_2q=1,
        )
        with (mock.patch(
                "experiments_v2.qasmbench_runner._standard_attempt",
                return_value="batch") as batch,
              mock.patch(
                "experiments_v2.qasmbench_runner._large_zac_attempt",
                return_value="formal") as formal):
            for method in ("M1", "M2"):
                job = QASMBenchJob(source, method, 0, 0, 600, "e" * 64)
                self.assertEqual(_run_job(None, None, job), "batch")
            for method in ("M3", "M4"):
                job = QASMBenchJob(source, method, 0, 0, 600, "e" * 64)
                self.assertEqual(_run_job(None, None, job), "formal")
            self.assertEqual(batch.call_count, 2)
            self.assertEqual(formal.call_count, 2)

    def test_worker_exception_is_sealed_as_terminal_compiler_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = QASMBenchSource(
                benchmark_scale="small", benchmark_directory="circuit",
                upstream_path="small/circuit/circuit.qasm",
                upstream_git_blob="a" * 40, source_sha256="b" * 64,
                selection_reason="preferred_named",
                canonical_profile="qasmbench_standard_expand_v1",
                status="success", canonical_path="unused",
                canonical_sha256="c" * 64, qubits=2,
                gates_1q=0, gates_2q=1,
            )
            job = QASMBenchJob(source, "M1", 0, 0, 600, "e" * 64)
            with mock.patch(
                    "experiments_v2.qasmbench_runner._run_job",
                    side_effect=ValueError("broken worker")):
                report = _execute_stage(
                    SimpleNamespace(repo_root=root), root, [job],
                    resume=False, dry_run=False, existing={},
                )
            self.assertEqual(report["status_counts"], {"compiler_error": 1})
            manifest_path = Path(report["attempted"][0]["manifest"])
            manifest = RunManifest(**json.loads(
                manifest_path.read_text(encoding="utf-8")))
            manifest.validate()
            self.assertEqual(manifest.status, RunStatus.COMPILER_ERROR.value)
            self.assertIn("ValueError: broken worker", manifest.error or "")
            self.assertFalse(manifest.trace_retained)

    def test_disk_guard_pause_is_not_converted_to_terminal_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = QASMBenchSource(
                benchmark_scale="small", benchmark_directory="circuit",
                upstream_path="small/circuit/circuit.qasm",
                upstream_git_blob="a" * 40, source_sha256="b" * 64,
                selection_reason="preferred_named",
                canonical_profile="qasmbench_standard_expand_v1",
                status="success", canonical_path="unused",
                canonical_sha256="c" * 64, qubits=2,
                gates_1q=0, gates_2q=1,
            )
            job = QASMBenchJob(source, "M1", 0, 0, 600, "e" * 64)
            with mock.patch(
                    "experiments_v2.qasmbench_runner._run_job",
                    side_effect=RuntimeError(
                        "runner paused: only 3.00 GiB free; requires 4.00 GiB")):
                with self.assertRaisesRegex(RuntimeError, "runner paused"):
                    _execute_stage(
                        SimpleNamespace(repo_root=root), root, [job],
                        resume=False, dry_run=False, existing={},
                    )
            manifests = list(root.rglob("manifest.json"))
            self.assertEqual(manifests, [])

    def test_cli_exposes_all_qasmbench_commands(self) -> None:
        parser = build_parser()
        self.assertEqual(parser.parse_args([
            "prepare-qasmbench", "--checkout", ".", "--output-dir", "out",
        ]).command, "prepare-qasmbench")
        self.assertEqual(parser.parse_args([
            "run-qasmbench", "--plan", "plan.json",
            "--source-manifest", "source_manifest.json", "--dry-run",
        ]).command, "run-qasmbench")
        self.assertEqual(parser.parse_args([
            "aggregate-qasmbench", "--source-manifest", "source_manifest.json",
            "--run-root", "runs", "--output-dir", "results",
        ]).command, "aggregate-qasmbench")

    def test_default_output_root_is_exactly_the_plan_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_manifest = root / "source_manifest.json"
            source_manifest.write_text("{}\n", encoding="utf-8")
            plan = SimpleNamespace(output_root=root / "qasmbench-v1")
            with (mock.patch(
                    "experiments_v2.qasmbench_runner.load_qasmbench_sources",
                    return_value=[]),
                  mock.patch(
                    "experiments_v2.qasmbench_runner.qasmbench_experiment_id",
                    return_value="e" * 64),
                  mock.patch(
                    "experiments_v2.qasmbench_runner._load_existing",
                    return_value={}),
                  mock.patch(
                    "experiments_v2.qasmbench_runner._execute_stage",
                    return_value={}) as execute):
                report = command_run_qasmbench(
                    plan, source_manifest, scales=("small",),
                    methods=("M1",), dry_run=True)
            self.assertEqual(
                Path(report["output_root"]), plan.output_root.resolve())
            self.assertEqual(
                execute.call_args.args[1], plan.output_root.resolve())

    def test_real_prepared_status_shape_allows_canonical_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            qasm = root / "canonical.qasm"
            qasm.write_text(
                'OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg q[1];\n',
                encoding="utf-8",
            )
            digest = hashlib.sha256(qasm.read_bytes()).hexdigest()
            rows = []
            no_sources = {
                "QAOA_3SAT_N100_p1000",
                "QV_n1000",
                "quantum_telecloning_N1_M1000_LNN",
                "quantum_telecloning_N1_M1000_all_to_all",
                "quantum_telecloning_N1_M100_LNN",
                "quantum_telecloning_N1_M100_all_to_all",
            }
            directories = {
                "small": [f"small_{index:03d}" for index in range(42)],
                "medium": [f"medium_{index:03d}" for index in range(25)],
                "large": [f"large_{index:03d}" for index in range(64)]
                + sorted(no_sources),
            }
            executable_index = 0
            for scale in ("small", "medium", "large"):
                for directory in sorted(directories[scale]):
                    if directory in no_sources:
                        status = "no_qasm_source"
                    else:
                        status = (
                            "canonical_error" if executable_index < 22
                            else "success")
                        executable_index += 1
                    row = {
                        "benchmark_scale": scale,
                        "benchmark_directory": directory,
                        "upstream_path": f"{scale}/{directory}",
                        "upstream_git_blob": "" if status == "no_qasm_source" else "a" * 40,
                        "source_sha256": "" if status == "no_qasm_source" else "b" * 64,
                        "selection_reason": (
                            "no_qasm_source" if status == "no_qasm_source"
                            else "preferred_named"),
                        "canonical_profile": "qasmbench_standard_expand_v1",
                        "status": status,
                        "error": (
                            "no_qasm_source" if status == "no_qasm_source"
                            else "QASMBenchCanonicalError: unsupported"
                            if status == "canonical_error" else ""),
                    }
                    if status == "success":
                        row.update({
                            "canonical_path": qasm.name,
                            "canonical_sha256": digest,
                            "qubits": 1,
                            "gates_1q": 0,
                            "gates_2q": 0,
                        })
                    rows.append(row)
            manifest = root / "source_manifest.json"
            manifest.write_text(json.dumps({
                "manifest_schema": "qasmbench-source-manifest-v1",
                "upstream_repository": "https://github.com/pnnl/QASMBench.git",
                "upstream_commit": "357b942396d5c2b7cbc1c229c585a6ef5ccaebac",
                "canonical_profile": "qasmbench_standard_expand_v1",
                "trace_retained": False,
                "counts": {
                    "directories": {"small": 42, "medium": 25, "large": 70},
                    "selected_qasm": 131,
                    "no_qasm_source": 6,
                    "success": 109,
                    "canonical_error": 22,
                    "selected": 0,
                },
                "entries": rows,
            }), encoding="utf-8")
            sources = load_qasmbench_sources(manifest)
            self.assertEqual(len(sources), 137)
            self.assertEqual(sum(row.status == "success" for row in sources), 109)
            self.assertEqual(
                sum(row.status == "canonical_error" for row in sources), 22)
            self.assertEqual(
                Path(next(row for row in sources
                          if row.status == "success").canonical_path),
                qasm.resolve())


if __name__ == "__main__":
    unittest.main()
