from __future__ import annotations

import copy
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

from experiments_v2.ablation import validate_ablation_config
from experiments_v2.method_driver import _decision_summary, main
from experiments_v2.paper_protocol import (
    _paper_ablation_wrapper, paper_native_python_identity)
from zzx.algorithm_v2 import (ForecastBoundaryError, ForecastLayerProvider,
                              ForecastOracle, decay_lookahead_spec)
from zzx.boundary_problem import NATIVE_ABI_VERSION, RichSearchConfig
from zzx.zac_zzx import ZAC_zzx


ROOT = Path(__file__).resolve().parents[1]
M3_CONFIG = ROOT / "exp_setting" / "native_ga_v1" / "ours_nl_independent.json"
M4_CONFIG = ROOT / "exp_setting" / "native_ga_v1" / "ours_lk_independent.json"


def _abi9_base(path: Path, *, horizon: int) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    setting = payload["zac_setting"][0]
    setting["native_abi_version"] = 9
    setting["native_wheel_sha256"] = "a" * 64
    setting["lookahead_horizon"]["max_horizon"] = horizon
    return payload


class _RecordingProvider(ForecastLayerProvider):
    def __init__(self) -> None:
        self.reads: list[int] = []

    @property
    def layer_count(self) -> int:
        return 4

    def read_layer(self, layer: int):
        self.reads.append(layer)
        return ((0, 1),)


class Abi9GreedyProtocolTests(unittest.TestCase):
    def test_native_dto_registers_abi9_and_search_policy(self) -> None:
        self.assertEqual(NATIVE_ABI_VERSION, 9)
        config = RichSearchConfig(
            operator_profile="tuned", search_policy="greedy_only")
        self.assertEqual(config.to_wire()["search_policy"], "greedy_only")
        with self.assertRaisesRegex(ValueError, "search_policy"):
            RichSearchConfig(
                operator_profile="tuned", search_policy="random_search")

    def test_protocol2_registers_h0_h8_greedy_and_sensitivity(self) -> None:
        cases = (
            ("paper_h0_ga", "M4", 0, "ga", M4_CONFIG),
            ("paper_h8_ga", "M4", 8, "ga", M4_CONFIG),
            ("paper_h8_greedy_only", "M4", 8, "greedy_only", M4_CONFIG),
            ("paper_sensitivity_horizon_2", "M4", 2, "ga", M4_CONFIG),
        )
        for variant, method, horizon, policy, path in cases:
            with self.subTest(variant=variant):
                wrapper = _paper_ablation_wrapper(
                    _abi9_base(path, horizon=horizon), method=method,
                    variant=variant, horizon=horizon,
                    search_policy=policy)
                base, registered = validate_ablation_config(
                    wrapper, expected_variant=variant,
                    expected_method=method)
                self.assertEqual(registered.search_policy, policy)
                self.assertEqual(
                    base["zac_setting"][0]["lookahead_horizon"]
                    ["max_horizon"], horizon)

    def test_protocol2_rejects_policy_drift_and_unknown_profile(self) -> None:
        wrapper = _paper_ablation_wrapper(
            _abi9_base(M4_CONFIG, horizon=8), method="M4",
            variant="paper_h8_greedy_only", horizon=8,
            search_policy="greedy_only")
        drift = copy.deepcopy(wrapper)
        drift["search_policy"] = "ga"
        with self.assertRaisesRegex(ValueError, "search_policy"):
            validate_ablation_config(drift)
        unknown = copy.deepcopy(wrapper)
        unknown["ablation_variant"] = "paper_sensitivity_unregistered"
        with self.assertRaisesRegex(ValueError, "unknown paper"):
            validate_ablation_config(unknown)

    def test_parser_accepts_abi9_only_through_internal_paper_contract(self) -> None:
        for horizon in (0, 2):
            with self.subTest(horizon=horizon):
                setting = _abi9_base(
                    M4_CONFIG, horizon=horizon)["zac_setting"][0]
                with self.assertRaises(ValueError):
                    ZAC_zzx().parse_setting(setting)
                compiler = ZAC_zzx()
                compiler._paper_ablation_contract = {
                    "native_abi_version": 9, "max_horizon": horizon}
                compiler.parse_setting(setting)
                self.assertEqual(compiler.zzx_params["native_abi_version"], 9)
                self.assertEqual(
                    compiler.zzx_params["lookahead_horizon"]["max_horizon"],
                    horizon)

    def test_h0_oracle_never_reads_future_provider(self) -> None:
        provider = _RecordingProvider()
        oracle = ForecastOracle(
            provider, decay_lookahead_spec(0, rho=0.7),
            alpha_lookahead=0.5)
        self.assertEqual(list(oracle.visible_future(0)), [])
        self.assertEqual(provider.reads, [])
        with self.assertRaises(ForecastBoundaryError):
            oracle.future_layer(0, 1)
        self.assertEqual(provider.reads, [])

    def test_cli_requires_argument_environment_and_ablation_agreement(self) -> None:
        argv = [
            "--method", "M4", "--input", "input.qasm",
            "--config", "config.json", "--architecture", "arch.json",
            "--run-kind", "ablation",
            "--ablation-variant", "paper_h8_greedy_only",
            "--search-policy", "greedy_only",
        ]
        environment = {
            "ZAC_RUN_KIND": "ablation",
            "ZAC_ABLATION_VARIANT": "paper_h8_greedy_only",
            "ZAC_SEARCH_POLICY": "greedy_only",
        }
        with mock.patch.dict(os.environ, environment, clear=False), \
                mock.patch(
                    "experiments_v2.method_driver.compile_zac") as compile_zac:
            main(argv)
        self.assertEqual(
            compile_zac.call_args.kwargs["search_policy"], "greedy_only")

        with mock.patch.dict(
                os.environ, {**environment, "ZAC_SEARCH_POLICY": "ga"},
                clear=False):
            with self.assertRaisesRegex(ValueError, "argument/environment"):
                main(argv)
        with mock.patch.dict(
                os.environ, {
                    "ZAC_RUN_KIND": "main", "ZAC_ABLATION_VARIANT": "",
                    "ZAC_SEARCH_POLICY": "greedy_only"}, clear=False):
            with self.assertRaisesRegex(ValueError, "run_kind=ablation"):
                main([
                    "--method", "M4", "--input", "input.qasm",
                    "--config", "config.json", "--architecture", "arch.json",
                    "--run-kind", "main",
                    "--search-policy", "greedy_only",
                ])

    def test_paper_native_python_probe_rejects_abi8_interpreter(self) -> None:
        reported = json.dumps({
            "abi": 8, "backend": "cpp-native-v8", "version": "0.5.32",
            "extension_path": __file__,
        })
        with mock.patch(
                "experiments_v2.paper_protocol.subprocess.check_output",
                return_value=reported):
            with self.assertRaisesRegex(RuntimeError, "does not load.*ABI9"):
                paper_native_python_identity(sys.executable)

    def test_ga_applicable_boundary_count_includes_both_policies(self) -> None:
        summary = _decision_summary([
            {"search_mode": "direct"},
            {"search_mode": "enumerate"},
            {"search_mode": "ga"},
            {"search_mode": "ga-budget"},
            {"search_mode": "greedy-only-current-recovery"},
        ])
        self.assertEqual(summary["ga_applicable_boundaries"], 3)


if __name__ == "__main__":
    unittest.main()
