from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from experiments_v2.contracts import RunManifest  # noqa: E402
from experiments_v2.method_driver import (  # noqa: E402
    QMAP_TIMING_PATCH_SHA256,
    _instrument_m1_transition_timing,
    _load_qmap_timing_freeze,
    _observed_zac_transition_layer_ledger,
    _selected_horizon_counts,
    _transition_decisions,
    _zac_stage_timing,
)


class NativeManifestContractTests(unittest.TestCase):
    def _manifest(self, **updates):
        values = {
            "run_id": "native-contract",
            "dataset": "toy",
            "circuit": "toy",
            "method": "M3",
            "backend": "native",
            "native_abi_version": 7,
            "native_wheel_sha256": "a" * 64,
            "compiler_and_flags": {
                "cxx_standard": 17,
                "openmp": False,
                "fast_math": False,
            },
            "rng_version": "python-random-mt19937-v1",
        }
        values.update(updates)
        return RunManifest(**values)

    def test_native_provenance_is_fail_closed(self):
        self._manifest().validate()
        with self.assertRaisesRegex(ValueError, "wheel"):
            self._manifest(native_wheel_sha256="").validate()
        with self.assertRaisesRegex(ValueError, "compiler"):
            self._manifest(compiler_and_flags={}).validate()
        with self.assertRaisesRegex(ValueError, "flags"):
            self._manifest(compiler_and_flags={
                "cxx_standard": 17,
                "openmp": True,
                "fast_math": False,
            }).validate()

    def test_stage_timing_keeps_primary_transition_separate(self):
        compiler = SimpleNamespace(
            runtime_analysis={
                "initial placement": 0.2,
                "intermediate placement": 0.7,
                "routing": 0.3,
            },
            zzx_stage_timing_ns={
                "initial_placement_ns": 101,
                "transition_decision_ns": 202,
            },
            zzx_decision_log=[
                {"horizon_selection_ns": 2, "selected_horizon": 0},
                {"horizon_selection_ns": 7, "selected_horizon": 2},
            ],
            zzx_backend_timing_log=[
                {"search_kernel_ns": 11, "marshal_ns": 3},
                {"search_kernel_ns": 13, "marshal_ns": 5},
            ],
        )
        timing = _zac_stage_timing(compiler, "M4", 1_500_000_000)
        self.assertEqual(timing["transition_decision_ns"], 202)
        self.assertEqual(timing["initial_placement_ns"], 101)
        self.assertEqual(timing["routing_ns"], 300_000_000)
        self.assertEqual(timing["search_kernel_ns"], 24)
        self.assertEqual(timing["marshal_ns"], 8)
        self.assertEqual(timing["horizon_selection_ns"], 9)
        self.assertEqual(timing["full_compile_ns"], 1_500_000_000)
        self.assertEqual(
            _selected_horizon_counts(compiler.zzx_decision_log),
            {"0": 1, "2": 1},
        )

    @mock.patch(
        "experiments_v2.method_driver.validate_qmap_timing_freeze")
    def test_qmap_timing_freeze_uses_full_binary_validator(self, validate):
        validate.return_value = {
            "protocol": "qmap-3.2-timing-instrumentation-v1",
            "patch_sha256": QMAP_TIMING_PATCH_SHA256,
            "parity_report_sha256": "a" * 64,
            "wheel_sha256": "b" * 64,
            "environment_lock_sha256": "c" * 64,
        }
        self.assertEqual(_load_qmap_timing_freeze(), validate.return_value)
        validate.assert_called_once_with()

    def test_m1_transition_wrapper_is_semantically_transparent(self):
        class FakeCompiler:
            def __init__(self):
                self.trace = []

            def place_qubit_intermedeiate(self):
                self.trace.append({"mapping": [0, 1], "kind": "place"})
                return ("unchanged-return", len(self.trace))

        compiler = FakeCompiler()
        expected = compiler.place_qubit_intermedeiate()
        expected_trace = list(compiler.trace)
        compiler.trace.clear()
        _instrument_m1_transition_timing(compiler)
        observed = compiler.place_qubit_intermedeiate()
        self.assertEqual(observed, expected)
        self.assertEqual(compiler.trace, expected_trace)
        self.assertGreaterEqual(
            compiler.zzx_stage_timing_ns["transition_decision_ns"], 0)

    def test_terminal_boundary_is_not_counted_as_a_transition(self):
        compiler = SimpleNamespace(zzx_decision_log=[
            {"selected_horizon": 2, "horizon_reason": "second_layer"},
            {"selected_horizon": 0, "horizon_reason": "terminal_boundary"},
        ])
        rows = _transition_decisions(compiler)
        self.assertEqual(len(rows), 1)
        self.assertEqual(_selected_horizon_counts(rows),
                         {"2": 1})

    def test_observed_zac_ledger_uses_realised_gate_scheduling(self):
        compiler = SimpleNamespace(
            n_q=4,
            n_g=3,
            gate_scheduling=[[[0, 1]], [[0, 2], [1, 3]]],
        )
        ledger = _observed_zac_transition_layer_ledger(compiler)
        self.assertEqual(
            ledger["layers"], [[(0, 1)], [(0, 2), (1, 3)]])
        self.assertEqual(ledger["transitions"], 1)
        # This schedule is intentionally not reconstructed from a canonical
        # flattened ASAP stream; its explicit boundary remains observable.
        self.assertEqual(ledger["gates_2q"], 3)


if __name__ == "__main__":
    unittest.main()
