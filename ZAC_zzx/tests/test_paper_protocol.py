from __future__ import annotations

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from experiments_v2.contracts import CanonicalCircuitManifest, sha256_file
from experiments_v2.paper_cli import _parser
from experiments_v2.paper_protocol import (
    PAPER_ABLATION_VARIANTS,
    SENSITIVITY_PROFILE_IDS,
    _paper_ablation_wrapper,
    _resolved_main_config,
    build_sensitivity_configs,
    build_shared_lookahead_configs,
    command_run_paper_sensitivity,
    command_run_paper_timing,
    select_paper_ablation,
    select_paper_twelve,
)
from experiments_v2.plan import effective_zac_setting
from experiments_v2.runner import _native_setting_contract


ROOT = Path(__file__).resolve().parents[1]
M4_CONFIG = ROOT / "exp_setting" / "native_ga_v1" / "ours_lk_independent.json"
M3_CONFIG = ROOT / "exp_setting" / "native_ga_v1" / "ours_nl_independent.json"
def _manifest(name: str, qubits: int, gates_2q: int, digest: int
              ) -> CanonicalCircuitManifest:
    value = f"{digest:064x}"
    return CanonicalCircuitManifest(
        experiment_schema=2,
        source_path=f"/source/{name}.qasm",
        canonical_path=f"/canonical/{name}.qasm",
        source_sha256=value,
        canonical_sha256=value,
        qiskit_version="1.2.4",
        basis_gates=["cz", "u1", "u2", "u3"],
        optimization_level=3,
        seed_transpiler=0,
        qubits=qubits,
        gates_1q=0,
        gates_2q=gates_2q,
        depth=gates_2q,
        canonical_profile="main_qiskit_1_2_4_opt3",
    )


def _suites() -> dict[str, list[CanonicalCircuitManifest]]:
    zac = []
    for index in range(12):
        zac.append(_manifest(f"zs{index}", 16, 10, 100 + index))
    for index in range(3):
        zac.append(_manifest(f"zm{index}", 48, 20, 200 + index))
    for index in range(3):
        zac.append(_manifest(f"zl{index}", 80, 30, 300 + index))
    qmap = []
    for index in range(98):
        qmap.append(_manifest(f"qs{index}", 20, 100, 1000 + index))
    for index in range(17):
        qmap.append(_manifest(f"qm{index}", 30, 800, 2000 + index))
    for index in range(39):
        qmap.append(_manifest(f"ql{index}", 40, 2000, 3000 + index))
    return {"zac18": zac, "qmap154": qmap}


class PaperCohortTests(unittest.TestCase):
    def test_twelve_selects_sha_min_two_per_registered_stratum(self) -> None:
        selected, report = select_paper_twelve(_suites())
        self.assertEqual(len(selected), 12)
        self.assertEqual(report["selected"], 12)
        identities = {(dataset, Path(row.canonical_path).stem)
                      for dataset, row in selected}
        self.assertTrue({("zac18", "zs0"), ("zac18", "zs1")} <= identities)
        self.assertTrue({("qmap154", "ql0"), ("qmap154", "ql1")} <= identities)

    def test_ablation_is_all_zac_plus_ten_per_qmap_stratum(self) -> None:
        selected, report = select_paper_ablation(_suites())
        self.assertEqual(len(selected), 48)
        self.assertEqual(report["selected"], 48)
        self.assertEqual(sum(dataset == "zac18" for dataset, _ in selected), 18)
        self.assertEqual(sum(dataset == "qmap154" for dataset, _ in selected), 30)


class PaperConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.m4 = json.loads(M4_CONFIG.read_text(encoding="utf-8"))

    def test_shared_pair_diff_is_only_identity_directory_and_horizon(self) -> None:
        h0, h8, audit = build_shared_lookahead_configs(
            self.m4, native_abi_version=9,
            native_wheel_sha256="a" * 64, seed=2)
        self.assertEqual(
            set(audit["differences"]), {"dir", "lookahead_horizon", "method_id"})
        self.assertEqual(effective_zac_setting(h0)["method_id"], "ours_nl")
        self.assertEqual(
            effective_zac_setting(h0)["lookahead_horizon"]["max_horizon"], 0)
        self.assertEqual(
            effective_zac_setting(h8)["lookahead_horizon"]["max_horizon"], 8)

    def test_sensitivity_design_contains_nine_unique_one_factor_settings(self) -> None:
        configs = build_sensitivity_configs(
            self.m4, native_abi_version=9,
            native_wheel_sha256="a" * 64)
        self.assertEqual(tuple(configs), SENSITIVITY_PROFILE_IDS)
        self.assertEqual(len({json.dumps(value, sort_keys=True)
                              for value in configs.values()}), 9)
        self.assertEqual(
            effective_zac_setting(configs["horizon_2"])
            ["lookahead_horizon"]["max_horizon"], 2)
        self.assertEqual(
            effective_zac_setting(configs["return_10_8"])
            ["return_assignment_k"], 8)

    def test_sensitivity_replaces_stale_raw_configs_and_wrappers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wheel = root / "native.whl"
            wheel.write_bytes(b"new registered wheel")
            requested_sha = sha256_file(wheel)
            raw_root = root / "configs" / "sensitivity"
            wrapper_root = root / "configs" / "sensitivity-wrappers"
            raw_root.mkdir(parents=True)
            wrapper_root.mkdir(parents=True)
            for profile in SENSITIVITY_PROFILE_IDS:
                (raw_root / f"{profile}.json").write_text(
                    json.dumps({"native_wheel_sha256": "0" * 64}),
                    encoding="utf-8")
                (wrapper_root / f"{profile}.json").write_text(
                    json.dumps({"base_config": {
                        "native_wheel_sha256": "0" * 64}}),
                    encoding="utf-8")

            freeze = {
                "freeze_id": "f" * 64,
                "configs": {"M4": {"path": str(M4_CONFIG)}},
            }
            config_calls: list[Path] = []

            def fake_spec(*args, **_kwargs):
                config_calls.append(Path(args[9]))
                return SimpleNamespace()

            def fake_execute(_plan, jobs, *, workers, resume, dry_run):
                self.assertEqual(workers, 4)
                self.assertFalse(resume)
                self.assertTrue(dry_run)
                return {"planned": len(jobs), "commands": [],
                        "attempted": [], "skipped_existing": []}

            native_identity = {
                "paper_native_python_path": "/isolated/bin/python",
                "paper_native_abi_version": "9",
                "paper_native_backend": "cpp-native-v9",
                "paper_native_version": "0.5.34",
            }
            with patch(
                    "experiments_v2.paper_protocol.load_paper_freeze",
                    return_value=freeze), patch(
                    "experiments_v2.paper_protocol._suite_map",
                    return_value=_suites()), patch(
                    "experiments_v2.paper_protocol._historical_heavy",
                    return_value=set()), patch(
                    "experiments_v2.paper_protocol.paper_native_python_identity",
                    return_value=native_identity), patch(
                    "experiments_v2.paper_protocol._paper_spec",
                    side_effect=fake_spec), patch(
                    "experiments_v2.paper_protocol.execute_paper_jobs",
                    side_effect=fake_execute):
                report = command_run_paper_sensitivity(
                    SimpleNamespace(), root / "freeze.json",
                    output_root=root, native_wheel=wheel,
                    native_abi_version=9,
                    native_python=root / "abi9" / "bin" / "python",
                    workers=4, resume=False, dry_run=True)

            self.assertEqual(report["execution"]["planned"], 108)
            self.assertEqual(len(config_calls), 108)
            for profile in SENSITIVITY_PROFILE_IDS:
                raw = json.loads(
                    (raw_root / f"{profile}.json").read_text(encoding="utf-8"))
                wrapper = json.loads(
                    (wrapper_root / f"{profile}.json").read_text(
                        encoding="utf-8"))
                self.assertEqual(
                    effective_zac_setting(raw)["native_wheel_sha256"],
                    requested_sha)
                self.assertEqual(
                    effective_zac_setting(
                        wrapper["base_config"])["native_wheel_sha256"],
                    requested_sha)
            self.assertEqual(
                {path.resolve() for path in config_calls},
                {(wrapper_root / f"{profile}.json").resolve()
                 for profile in SENSITIVITY_PROFILE_IDS})

    def test_failed_warmup_prevents_formal_timing_schedule(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            freeze = {
                "freeze_id": "f" * 64,
                "configs": {
                    "M1": {"path": str(
                        ROOT / "exp_setting" / "zac_m1_v2.json")},
                    "M2": {"path": str(
                        ROOT / "exp_setting" / "iccad_m2_v2.json")},
                    "M3": {"path": str(M3_CONFIG)},
                    "M4": {"path": str(M4_CONFIG)},
                },
            }
            execute_calls: list[int] = []

            def fake_spec(*_args, **_kwargs):
                return SimpleNamespace()

            def fake_execute(_plan, jobs, *, workers, resume, dry_run):
                execute_calls.append(len(jobs))
                self.assertEqual(workers, 1)
                self.assertFalse(resume)
                self.assertFalse(dry_run)
                statuses = ["timeout", *("success" for _ in range(7))]
                return {
                    "planned": len(jobs), "workers": 1,
                    "attempted": [{"status": status}
                                  for status in statuses],
                    "skipped_existing": [], "commands": [],
                    "status_counts": {"success": 7, "timeout": 1},
                }

            with patch(
                    "experiments_v2.paper_protocol.load_paper_freeze",
                    return_value=freeze), patch(
                    "experiments_v2.paper_protocol._suite_map",
                    return_value=_suites()), patch(
                    "experiments_v2.paper_protocol._paper_spec",
                    side_effect=fake_spec), patch(
                    "experiments_v2.paper_protocol.execute_paper_jobs",
                    side_effect=fake_execute):
                with self.assertRaisesRegex(
                        RuntimeError,
                        "formal 144-attempt schedule was not started"):
                    command_run_paper_timing(
                        SimpleNamespace(), root / "freeze.json",
                        output_root=root, resume=False, dry_run=False)

            self.assertEqual(execute_calls, [8])
            report = json.loads(
                (root / "reports" / "run-paper-timing.json").read_text(
                    encoding="utf-8"))
            self.assertFalse(report["formal_execution_started"])
            self.assertFalse(report["warmup_gate"]["passed"])
            self.assertEqual(
                report["warmup_gate"]["status_counts"],
                {"success": 7, "timeout": 1})
            self.assertEqual(
                report["execution"]["not_started_reason"],
                "warmup_gate_failed")
            self.assertEqual(report["execution"]["planned"], 144)

    def test_seed0_resolved_config_is_byte_identical_to_freeze(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "M4.json"
            source.write_bytes(M4_CONFIG.read_bytes())
            freeze = {"configs": {"M4": {"path": str(source)}}}
            seed0 = _resolved_main_config(
                freeze, "M4", 0, root / "resolved-seed0.json")
            seed1 = _resolved_main_config(
                freeze, "M4", 1, root / "resolved-seed1.json")
            self.assertEqual(sha256_file(seed0), sha256_file(source))
            self.assertNotEqual(sha256_file(seed1), sha256_file(source))
            self.assertEqual(
                effective_zac_setting(json.loads(
                    seed1.read_text(encoding="utf-8")))["seed"], 1)

    def test_runner_reads_ablation_depth_from_wrapper_but_main_is_fixed(self) -> None:
        sensitivity = build_sensitivity_configs(
            self.m4, native_abi_version=9,
            native_wheel_sha256="a" * 64)["horizon_2"]
        wrapper = _paper_ablation_wrapper(
            sensitivity, method="M4",
            variant="paper_sensitivity_horizon_2", horizon=2,
            search_policy="ga")
        _config, _controls, depth = _native_setting_contract(
            wrapper, method="M4", run_kind="ablation",
            package_versions={"paper_search_policy": "ga"})
        self.assertEqual(depth, 2)
        with self.assertRaisesRegex(ValueError, "formal main horizon"):
            _native_setting_contract(
                sensitivity, method="M4", run_kind="main",
                package_versions={})
        m3 = json.loads(M3_CONFIG.read_text(encoding="utf-8"))
        _config, _controls, depth = _native_setting_contract(
            m3, method="M3", run_kind="main", package_versions={})
        self.assertEqual(depth, 0)


class PaperCliTests(unittest.TestCase):
    def test_all_six_public_commands_are_registered(self) -> None:
        parser = _parser()
        arguments = {
            "paper-freeze": [],
            "run-paper-main": [],
            "run-paper-ablation": [
                "--abi9-wheel", "wheel.whl",
                "--native-python", "abi9/bin/python"],
            "run-paper-sensitivity": [
                "--native-wheel", "wheel.whl", "--native-abi-version", "9",
                "--native-python", "abi9/bin/python"],
            "run-paper-timing": [],
            "aggregate-paper": [],
        }
        for command, extra in arguments.items():
            with self.subTest(command=command):
                self.assertEqual(
                    parser.parse_args([command, *extra]).command, command)

    def test_greedy_variant_is_distinct_from_h8_ga(self) -> None:
        self.assertNotEqual(
            PAPER_ABLATION_VARIANTS["h8"],
            PAPER_ABLATION_VARIANTS["greedy"])


if __name__ == "__main__":
    unittest.main()
