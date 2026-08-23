"""Fail-closed contracts for the formal native M3/M4 configurations."""
from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from zzx.algorithm_v2 import (  # noqa: E402
    FORMAL_NATIVE_ABI_VERSION,
    FORMAL_NATIVE_RNG_VERSION,
    FORMAL_LK_LOOKAHEAD_V1,
    FORMAL_NL_LOOKAHEAD_V1,
    SCHEMA2_NATIVE_REQUIRED_KEYS,
    resolved_max_unique_evaluations,
    validate_schema2_pair,
    validate_schema2_setting,
)
from zzx.zac_zzx import ZAC_zzx  # noqa: E402


CONFIG_ROOT = ROOT / "exp_setting" / "native_ga_v1"


def load_setting(name: str) -> dict:
    payload = json.loads((CONFIG_ROOT / name).read_text(encoding="utf-8"))
    return payload["zac_setting"][0]


class NativeConfigContractTests(unittest.TestCase):
    def setUp(self):
        self.nl = load_setting("ours_nl_shared.json")
        self.lk = load_setting("ours_lk_shared.json")

    def test_committed_shared_pair_is_formal_and_only_differs_by_method(self):
        validate_schema2_pair(self.nl, self.lk)
        self.assertEqual(
            self.nl["lookahead_horizon"], FORMAL_NL_LOOKAHEAD_V1)
        self.assertEqual(
            self.lk["lookahead_horizon"], FORMAL_LK_LOOKAHEAD_V1)
        for setting in (self.nl, self.lk):
            self.assertEqual(setting["backend"], "native")
            self.assertIs(setting["native_fail_closed"], True)
            self.assertIs(setting["formal_native"], True)
            self.assertEqual(
                setting["native_abi_version"], FORMAL_NATIVE_ABI_VERSION)
            self.assertEqual(setting["rng_version"], FORMAL_NATIVE_RNG_VERSION)
            self.assertEqual(setting["operator_profile"], "tuned")
            self.assertEqual(len(setting["native_wheel_sha256"]), 64)

    def test_parse_setting_consumes_every_native_and_search_control(self):
        compiler = ZAC_zzx()
        compiler.parse_setting({**self.nl, "name": "native-contract"})
        expected = SCHEMA2_NATIVE_REQUIRED_KEYS | {
            "max_unique_evaluations",
        }
        # max_unique_evaluations is an optional resolved control; all committed
        # native provenance/search controls must survive into the placer DTO.
        expected.discard("max_unique_evaluations")
        self.assertTrue(expected <= set(compiler.zzx_params))
        for key in ("elite_count", "early_stop_patience", "backend",
                    "native_fail_closed", "formal_native",
                    "algorithm_revision", "tuning_protocol_id",
                    "native_abi_version", "native_wheel_sha256",
                    "rng_version", "operator_profile"):
            self.assertEqual(compiler.zzx_params[key], self.nl[key])

    def test_unknown_tuning_key_is_not_silently_ignored(self):
        typo = {**self.nl, "early_stop_patient": 3}
        with self.assertRaisesRegex(ValueError, "未知设置键"):
            ZAC_zzx().parse_setting(typo)

    def test_partial_native_request_is_rejected(self):
        legacy = json.loads(
            (ROOT / "exp_setting" / "ours_nl_v2.json").read_text(
                encoding="utf-8"))["zac_setting"][0]
        validate_schema2_setting(legacy)
        partial = {**legacy, "backend": "native", "native_fail_closed": True}
        with self.assertRaisesRegex(ValueError, "正式 native.*缺少"):
            validate_schema2_setting(partial)

    def test_native_never_accepts_false_fail_closed_flags(self):
        for key in ("native_fail_closed", "formal_native"):
            broken = {**self.nl, key: False}
            with self.subTest(key=key):
                with self.assertRaisesRegex(ValueError, key):
                    validate_schema2_setting(broken)

    def test_native_provenance_is_bound_to_registered_abi_rng_and_wheel(self):
        invalid = (
            ("native_abi_version", FORMAL_NATIVE_ABI_VERSION + 1,
             "native_abi_version"),
            ("native_wheel_sha256", "", "native_wheel_sha256"),
            ("native_wheel_sha256", "z" * 64, "native_wheel_sha256"),
            ("rng_version", "different-rng", "rng_version"),
            ("algorithm_revision", "", "algorithm_revision"),
            ("tuning_protocol_id", "", "tuning_protocol_id"),
            ("operator_profile", "exact", "operator_profile"),
        )
        for key, value, message in invalid:
            broken = {**self.nl, key: value}
            with self.subTest(key=key, value=value):
                with self.assertRaisesRegex(ValueError, message):
                    validate_schema2_setting(broken)

    def test_search_controls_are_typed_and_pair_locked(self):
        invalid = (
            ("elite_count", 0),
            ("elite_count", self.nl["population_size"] + 1),
            ("early_stop_patience", -1),
            ("max_unique_evaluations", 0),
            ("max_unique_evaluations", self.nl["population_size"] - 1),
        )
        for key, value in invalid:
            broken = {**self.nl, key: value}
            with self.subTest(key=key, value=value):
                with self.assertRaisesRegex(ValueError, key):
                    validate_schema2_setting(broken)

        explicit = {**self.nl, "max_unique_evaluations": 777}
        validate_schema2_setting(explicit)
        self.assertEqual(resolved_max_unique_evaluations(explicit), 777)
        self.assertEqual(
            resolved_max_unique_evaluations(self.nl),
            self.nl["population_size"] * self.nl["iterations"]
            * self.nl["neighbor_sample_size"],
        )

        changed = copy.deepcopy(self.lk)
        changed["elite_count"] += 1
        with self.assertRaisesRegex(ValueError, "配置差异"):
            validate_schema2_pair(self.nl, changed)


if __name__ == "__main__":
    unittest.main()
