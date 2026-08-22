"""End-to-end and fail-closed tests for the formal Large M2 runner."""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evaluation import CanonicalTraceEvent, normalize_na, score_trace  # noqa: E402
from experiments_v2.na_physicalizer import (  # noqa: E402
    physicalize_na_streaming as physicalize_na_streaming_actual,
)
from streaming.formal_large_qmap_compiler import (  # noqa: E402
    FormalLargeQmapAttemptError,
    QMAP_32_FROZEN_STREAM_CONFIG,
    _verify_qmap_provenance,
    audit_qmap_schedule,
    compile_formal_large_qmap,
)
from streaming.qasm_sqlite import build_layer_store  # noqa: E402
from streaming.qmap_schedule_sqlite import build_qmap_schedule_store  # noqa: E402


ARCHITECTURE = {
    "name": "formal_m2_toy",
    "operation_duration": {
        "rydberg": 0.36,
        "1qGate": 52.0,
        "atom_transfer": 15.0,
    },
    "operation_fidelity": {
        "two_qubit_gate": 0.995,
        "single_qubit_gate": 0.9997,
        "atom_transfer": 0.999,
    },
    "storage_zones": [{
        "zone_id": 0,
        "offset": [0, 0],
        "dimenstion": [8, 4],
        "slms": [{
            "id": 0,
            "location": [0, 0],
            "site_seperation": [2, 2],
            "r": 2,
            "c": 4,
        }],
    }],
    "entanglement_zones": [{
        "zone_id": 0,
        "offset": [10, 0],
        "dimenstion": [4, 2],
        "slms": [
            {
                "id": 1,
                "location": [10, 0],
                "site_seperation": [4, 4],
                "r": 1,
                "c": 1,
            },
            {
                "id": 2,
                "location": [12, 0],
                "site_seperation": [4, 4],
                "r": 1,
                "c": 1,
            },
        ],
    }],
    "aods": [{
        "id": 0,
        "site_seperation": 2,
        "r": 2,
        "c": 4,
    }],
    "rydberg_range": [[[9, -1], [13, 1]]],
}


TOY_NA = """\
atom (0, 0) atom0
atom (2, 0) atom1
@+ u 0.1 0.2 0.3 atom0
@+ load [
atom0
atom1
]
@+ move [
(10, 0) atom0
(12, 0) atom1
]
@+ store [
atom0
atom1
]
@+ cz zone_cz0
@+ rz 0.4 atom1
"""


MOCK_PROVENANCE = {
    "ok": True,
    "bundle": {
        "base_commit": "745d56b260c06708e1211772f0ed174b7a6c3437",
    },
    "patched_source": {"ok": True},
    "cli_relative_path": "build/mqt-qmap-na-zoned-stream",
    "cli_sha256": "f" * 64,
    "cli_size_bytes": 1234,
}


def _placement(slm0: int, slm1: int) -> list[dict[str, int]]:
    return [
        {"qubit": 0, "slm": slm0, "row": 0, "column": 0},
        {
            "qubit": 1,
            "slm": slm1,
            "row": 0,
            "column": 1 if slm0 == slm1 else 0,
        },
    ]


def _success_cli_source() -> str:
    initial = {
        "schema": "qmap-stream-metadata-v1",
        "kind": "initial",
        "native_operations": 3,
        "schedule": {
            "format": "qmap-schedule-store-v1",
            "qubits": 2,
            "events": 3,
            "gates_1q": 2,
            "gates_2q": 1,
            "two_qubit_layers": 1,
            "scheduled_items": 2,
        },
        "placement": _placement(0, 0),
    }
    transition = {
        "schema": "qmap-stream-metadata-v1",
        "kind": "transition",
        "layer": 0,
        "native_operations": 7,
        "previous_reuse": [],
        "next_reuse": [0, 1],
        "previous_placement": _placement(0, 0),
        "gate_placement": _placement(1, 2),
        "storage_placement": _placement(1, 2),
        "execution_routing": [],
        "storage_routing": [],
    }
    return textwrap.dedent(
        f"""\
        #!/usr/bin/env python3
        import json
        from pathlib import Path
        import sys

        Path(sys.argv[4]).write_text({TOY_NA!r}, encoding="utf-8")
        records = {json.dumps([initial, transition], sort_keys=True)!r}
        values = json.loads(records)
        Path(sys.argv[5]).write_text(
            "\\n".join(json.dumps(value, sort_keys=True) for value in values) + "\\n",
            encoding="utf-8",
        )
        print(json.dumps({{
            "schema": "qmap-stream-cli-result-v1",
            "events": 3,
            "two_qubit_layers": 1,
            "chunks": 2,
            "peak_native_operations_per_chunk": 7,
            "statistics": {{
                "schedulingTime": 1,
                "reuseAnalysisTime": 2,
                "placementTime": 3,
                "routingTime": 4,
                "codeGenerationTime": 5,
                "totalTime": 15,
            }},
        }}, sort_keys=True))
        """
    )


class FormalM2Fixture:
    def __init__(self, directory: str):
        self.root = Path(directory)
        self.qasm = self.root / "toy.qasm"
        self.layers = self.root / "layers.sqlite"
        self.schedule = self.root / "qmap.sqlite"
        self.architecture = self.root / "architecture.json"
        self.config = self.root / "config.json"
        self.cli = self.root / "qmap-stream"
        self.qasm.write_text(
            "OPENQASM 2.0;\n"
            "include \"qelib1.inc\";\n"
            "qreg q[2];\n"
            "u1(0.1) q[0];\n"
            "cz q[0],q[1];\n"
            "u2(0.2,0.3) q[1];\n",
            encoding="utf-8",
        )
        build_layer_store(self.qasm, self.layers)
        build_qmap_schedule_store(
            self.layers,
            self.schedule,
            max_two_qubit_gates_per_layer=1,
            commit_interval=1,
            count_cache_size=1,
        )
        self.architecture.write_text(
            json.dumps(ARCHITECTURE, sort_keys=True), encoding="utf-8"
        )
        self.config.write_text(
            json.dumps(QMAP_32_FROZEN_STREAM_CONFIG, sort_keys=True),
            encoding="utf-8",
        )
        self.write_cli(_success_cli_source())

    def write_cli(self, source: str) -> None:
        self.cli.write_text(source, encoding="utf-8")
        self.cli.chmod(0o755)


def _trace_dicts(path: Path) -> list[dict]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle]


class TestFormalLargeQmapCompiler(unittest.TestCase):
    def compile_fixture(
        self, fixture: FormalM2Fixture, output: Path, **kwargs,
    ):
        with patch(
            "streaming.formal_large_qmap_compiler._verify_qmap_provenance",
            return_value=MOCK_PROVENANCE,
        ):
            return compile_formal_large_qmap(
                schedule_path=fixture.schedule,
                architecture_path=fixture.architecture,
                config_path=fixture.config,
                cli_path=fixture.cli,
                qmap_source_tree=fixture.root,
                output_directory=output,
                **kwargs,
            )

    def test_toy_is_exact_to_batch_adapter_and_scorer(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = FormalM2Fixture(directory)
            output = fixture.root / "formal-m2"
            result = self.compile_fixture(
                fixture, output,
                development_prefix=True,
            )

            self.assertEqual(result.status, "success")
            self.assertFalse(result.full_circuit)
            self.assertTrue(result.provenance_ok)
            self.assertFalse(result.support_claim_eligible)
            self.assertTrue(output.is_dir())
            self.assertFalse(list(fixture.root.glob(".formal-m2.*.inprogress")))

            gate_pairs = [((0, 1),)]
            expected_events = list(normalize_na(
                output / "native.na",
                architecture=output / "qmap_architecture.json",
                gate_pairs=gate_pairs,
            ))
            actual_dicts = _trace_dicts(output / "trace.jsonl.gz")
            self.assertEqual(
                actual_dicts,
                [event.to_dict() for event in expected_events],
            )
            self.assertEqual(result.canonical_events, len(expected_events))
            self.assertEqual(
                dict(result.fidelity),
                score_trace(expected_events, n_qubits=2).to_dict(),
            )
            self.assertEqual(result.validation["one_qubit_gates"], 2)
            self.assertEqual(result.validation["two_qubit_gates"], 1)
            self.assertEqual(result.validation["ghost_hits"], 0)

            manifest = json.loads((output / "manifest.json").read_text())
            self.assertEqual(manifest["status"], "success")
            self.assertTrue(manifest["provenance_ok"])
            self.assertEqual(manifest["event_order"], "chronological")
            self.assertFalse(manifest["resume_supported"])
            self.assertEqual(
                manifest["qmap"]["base_commit"],
                MOCK_PROVENANCE["bundle"]["base_commit"],
            )
            self.assertEqual(result.qmap_core_ns, 15_000)
            self.assertEqual(
                result.compiler_core_ns,
                result.qmap_core_ns + result.physicalization_wall_ns,
            )
            self.assertGreaterEqual(result.physicalization_wall_ns, 0)
            self.assertEqual(
                manifest["result"]["compiler_core_ns"],
                manifest["result"]["qmap_core_ns"]
                + manifest["result"]["physicalization_wall_ns"],
            )
            self.assertIn(
                "compiler_core_ns=qmap_core_ns+physicalization_wall_ns",
                manifest["runtime_definition"],
            )
            self.assertGreater(result.compiler_process_wall_ns, 0)
            self.assertEqual(manifest["metadata_audit"]["chunks"], 2)
            self.assertTrue((output / "native.raw.na").is_file())
            self.assertTrue((output / "native.na").is_file())
            self.assertFalse((output / "metadata.jsonl").exists())
            self.assertTrue((output / "metadata.jsonl.gz").is_file())
            with gzip.open(
                output / "metadata.jsonl.gz", "rb"
            ) as metadata_handle:
                raw_metadata = metadata_handle.read()
            self.assertEqual(
                hashlib.sha256(raw_metadata).hexdigest(),
                result.metadata_sha256,
            )
            self.assertEqual(result.physicalization, {
                "raw_move_batches": 1,
                "repaired_move_batches": 1,
                "ghost_splits": 0,
                "waypoint_atoms": 0,
            })
            self.assertTrue(result.placement_semantics_ok)
            self.assertEqual(
                result.raw_placement_semantics,
                result.repaired_placement_semantics,
            )
            self.assertEqual(
                result.raw_placement_semantics["schema"],
                "na-placement-boundary-xor-v1",
            )
            self.assertEqual(
                manifest["placement_semantics"]["raw"],
                manifest["placement_semantics"]["repaired"],
            )
            self.assertEqual(
                result.raw_native_sha256,
                hashlib.sha256((output / "native.raw.na").read_bytes()).hexdigest(),
            )
            self.assertEqual(
                manifest["metadata_archive"]["archive_path"],
                "metadata.jsonl.gz",
            )
            self.assertEqual(
                manifest["orchestrator_git"],
                manifest["orchestrator_git_end"],
            )

    def test_changed_repaired_endpoint_is_verifier_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = FormalM2Fixture(directory)
            output = fixture.root / "endpoint-tampered"

            def physicalize_then_tamper(source, destination, architecture):
                stats = physicalize_na_streaming_actual(
                    source, destination, architecture,
                )
                destination_path = Path(destination)
                repaired = destination_path.read_text(encoding="utf-8")
                tampered, replacements = re.subn(
                    r"\(10\.000000, 0\.000000\) atom0",
                    "(11.000000, 0.000000) atom0",
                    repaired,
                    count=1,
                )
                self.assertEqual(replacements, 1)
                destination_path.write_text(tampered, encoding="utf-8")
                return stats

            with patch(
                "streaming.formal_large_qmap_compiler."
                "physicalize_na_streaming",
                side_effect=physicalize_then_tamper,
            ):
                with self.assertRaises(FormalLargeQmapAttemptError) as caught:
                    self.compile_fixture(
                        fixture, output, development_prefix=True,
                    )

            self.assertEqual(caught.exception.status, "verifier_fail")
            self.assertFalse(output.exists())
            manifest = json.loads(
                (caught.exception.attempt_directory / "manifest.json").read_text()
            )
            result = manifest["result"]
            self.assertIn(
                "endpoint placement semantics mismatch", result["error"],
            )
            self.assertFalse(result["placement_semantics_ok"])
            raw = result["raw_placement_semantics"]
            repaired = result["repaired_placement_semantics"]
            self.assertEqual(raw["schema"], "na-placement-boundary-xor-v1")
            self.assertEqual(raw["boundary_count"], repaired["boundary_count"])
            self.assertNotEqual(raw["sha256"], repaired["sha256"])
            self.assertNotEqual(
                raw["final_placement_sha256"],
                repaired["final_placement_sha256"],
            )

    def test_timeout_kills_attempt_and_never_publishes_final(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = FormalM2Fixture(directory)
            fixture.write_cli(textwrap.dedent("""\
                #!/usr/bin/env python3
                import time
                time.sleep(30)
            """))
            output = fixture.root / "timed-out"
            with self.assertRaises(FormalLargeQmapAttemptError) as caught:
                self.compile_fixture(
                    fixture, output,
                    timeout_seconds=0.05,
                    development_prefix=True,
                )
            self.assertEqual(caught.exception.status, "timeout")
            self.assertFalse(output.exists())
            attempt = caught.exception.attempt_directory
            self.assertTrue(attempt.is_dir())
            manifest = json.loads((attempt / "manifest.json").read_text())
            self.assertEqual(manifest["status"], "timeout")
            self.assertFalse(manifest["result"]["support_claim_eligible"])

    def test_nonzero_cli_never_publishes_final(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = FormalM2Fixture(directory)
            fixture.write_cli(textwrap.dedent("""\
                #!/usr/bin/env python3
                import sys
                print("deliberate failure", file=sys.stderr)
                raise SystemExit(7)
            """))
            output = fixture.root / "compiler-error"
            with self.assertRaises(FormalLargeQmapAttemptError) as caught:
                self.compile_fixture(
                    fixture, output,
                    development_prefix=True,
                )
            self.assertEqual(caught.exception.status, "compiler_error")
            self.assertFalse(output.exists())
            manifest = json.loads(
                (caught.exception.attempt_directory / "manifest.json").read_text()
            )
            self.assertEqual(manifest["result"]["exit_code"], 7)

    def test_schedule_tamper_fails_before_cli_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = FormalM2Fixture(directory)
            connection = sqlite3.connect(fixture.schedule)
            connection.execute("UPDATE qmap_operations SET q0=99 WHERE seq=0")
            connection.commit()
            connection.close()
            with self.assertRaises(ValueError):
                audit_qmap_schedule(fixture.schedule)

    def test_frozen_config_and_wrong_source_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = FormalM2Fixture(directory)
            bad = json.loads(fixture.config.read_text())
            bad["placerConfig"]["lookaheadFactor"] = 0.3
            fixture.config.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "frozen QMAP"):
                self.compile_fixture(
                    fixture, fixture.root / "bad-config",
                    development_prefix=True,
                )

            fixture.config.write_text(
                json.dumps(QMAP_32_FROZEN_STREAM_CONFIG), encoding="utf-8"
            )
            with self.assertRaises((ValueError, RuntimeError)):
                compile_formal_large_qmap(
                    schedule_path=fixture.schedule,
                    architecture_path=fixture.architecture,
                    config_path=fixture.config,
                    cli_path=fixture.cli,
                    qmap_source_tree=fixture.root,
                    output_directory=fixture.root / "bad-source",
                    development_prefix=True,
                )

    def test_git_state_change_during_attempt_is_verifier_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = FormalM2Fixture(directory)
            output = fixture.root / "git-changed"
            start = {"commit": "1" * 40, "dirty": False}
            end = {"commit": "2" * 40, "dirty": False}
            with patch(
                "streaming.formal_large_qmap_compiler._git_state",
                side_effect=[start, end],
            ):
                with self.assertRaises(FormalLargeQmapAttemptError) as caught:
                    self.compile_fixture(
                        fixture, output,
                        development_prefix=True,
                    )
            self.assertEqual(caught.exception.status, "verifier_fail")
            self.assertFalse(output.exists())
            self.assertTrue(
                (caught.exception.attempt_directory / "metadata.jsonl").is_file()
            )
            self.assertFalse(
                (caught.exception.attempt_directory / "metadata.jsonl.gz").exists()
            )

    def test_full_run_refuses_dirty_feature_head(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = FormalM2Fixture(directory)
            with patch(
                "streaming.formal_large_qmap_compiler._git_state",
                return_value={"commit": "1" * 40, "dirty": True},
            ):
                with self.assertRaisesRegex(RuntimeError, "requires.*clean"):
                    self.compile_fixture(
                        fixture, fixture.root / "dirty-full",
                        development_prefix=False,
                    )

    def test_fake_cli_cannot_become_eligible_by_mocking_clean_git(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = FormalM2Fixture(directory)
            output = fixture.root / "fake-full"
            with patch(
                "streaming.formal_large_qmap_compiler._git_state",
                return_value={"commit": "1" * 40, "dirty": False},
            ):
                with self.assertRaises((ValueError, RuntimeError)):
                    compile_formal_large_qmap(
                        schedule_path=fixture.schedule,
                        architecture_path=fixture.architecture,
                        config_path=fixture.config,
                        cli_path=fixture.cli,
                        qmap_source_tree=fixture.root,
                        output_directory=output,
                        development_prefix=False,
                    )
            self.assertFalse(output.exists())

    def test_exec_format_error_writes_compiler_error_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = FormalM2Fixture(directory)
            fixture.write_cli("not an executable format\n")
            output = fixture.root / "launch-error"
            with self.assertRaises(FormalLargeQmapAttemptError) as caught:
                self.compile_fixture(
                    fixture, output, development_prefix=True,
                )
            self.assertEqual(caught.exception.status, "compiler_error")
            manifest = json.loads(
                (caught.exception.attempt_directory / "manifest.json").read_text()
            )
            self.assertEqual(manifest["status"], "compiler_error")
            self.assertIsNotNone(manifest["result"]["compiler_process_wall_ns"])
            self.assertFalse(output.exists())

    def test_rss_limit_exceedance_is_oom_and_not_published(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = FormalM2Fixture(directory)
            fixture.write_cli(textwrap.dedent("""\
                #!/usr/bin/env python3
                import time
                time.sleep(30)
            """))
            output = fixture.root / "oom"
            limit = 64 * 1024 * 1024
            with patch(
                "streaming.formal_large_qmap_compiler._process_group_rss_bytes",
                return_value=limit + 4096,
            ):
                with self.assertRaises(FormalLargeQmapAttemptError) as caught:
                    self.compile_fixture(
                        fixture, output,
                        rss_limit_bytes=limit,
                        development_prefix=True,
                    )
            self.assertEqual(caught.exception.status, "oom")
            manifest = json.loads(
                (caught.exception.attempt_directory / "manifest.json").read_text()
            )
            self.assertTrue(manifest["result"]["rss_limit_exceeded"])
            self.assertGreater(manifest["result"]["peak_rss_bytes"], limit)
            self.assertFalse(output.exists())

    def test_sigkill_exit_is_classified_as_oom(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = FormalM2Fixture(directory)
            fixture.write_cli(textwrap.dedent("""\
                #!/usr/bin/env python3
                import os
                import signal
                os.kill(os.getpid(), signal.SIGKILL)
            """))
            output = fixture.root / "signal-oom"
            with self.assertRaises(FormalLargeQmapAttemptError) as caught:
                self.compile_fixture(
                    fixture, output, development_prefix=True,
                )
            self.assertEqual(caught.exception.status, "oom")
            manifest = json.loads(
                (caught.exception.attempt_directory / "manifest.json").read_text()
            )
            self.assertEqual(manifest["status"], "oom")
            self.assertTrue(manifest["result"]["rss_limit_exceeded"])
            self.assertFalse(output.exists())

    def test_fake_cli_inside_valid_source_is_rejected(self):
        source = ROOT.parent.parent / "qmap-v3.2-stream-src"
        frozen_cli = (
            source / "build-transition-tests" / "src" / "na" / "zoned"
            / "mqt-qmap-na-zoned-stream"
        )
        if not source.is_dir() or not frozen_cli.is_file():
            self.skipTest("verified QMAP development source/build is unavailable")
        config = (
            ROOT / "third_party" / "qmap32_streaming"
            / "routing_aware_paper.json"
        )
        with tempfile.TemporaryDirectory(prefix="fake-build-", dir=source) as directory:
            fake_cli = Path(directory) / "mqt-qmap-na-zoned-stream"
            fake_cli.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake_cli.chmod(0o755)
            with self.assertRaisesRegex(ValueError, "relative path.*frozen"):
                _verify_qmap_provenance(source, fake_cli, config)

    def test_verified_development_source_identity_passes(self):
        source = ROOT.parent.parent / "qmap-v3.2-stream-src"
        cli = (
            source / "build-transition-tests" / "src" / "na" / "zoned"
            / "mqt-qmap-na-zoned-stream"
        )
        if not source.is_dir() or not cli.is_file():
            self.skipTest("verified QMAP development source/build is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            fixture = FormalM2Fixture(directory)
            result = compile_formal_large_qmap(
                schedule_path=fixture.schedule,
                architecture_path=ROOT / "hardware_spec" / "full_architecture.json",
                config_path=(
                    ROOT / "third_party" / "qmap32_streaming"
                    / "routing_aware_paper.json"
                ),
                cli_path=cli,
                qmap_source_tree=source,
                output_directory=fixture.root / "verified-source",
                timeout_seconds=60,
                development_prefix=True,
            )
            self.assertEqual(result.status, "success")
            self.assertTrue(result.provenance_ok)
            self.assertFalse(result.support_claim_eligible)


if __name__ == "__main__":
    unittest.main()
