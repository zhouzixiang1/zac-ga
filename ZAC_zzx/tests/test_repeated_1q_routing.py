"""Regression for repeated authored 1Q gates packed into one ZAC block."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from streaming.zac_route_transition import ZACRouteTransitionDriver  # noqa: E402
from zac.ds.architecture import Architecture  # noqa: E402


class TestRepeatedOneQubitRouting(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = json.loads(
            (ROOT / "hardware_spec/full_architecture.json").read_text())
        cls.architecture = Architecture(spec)
        cls.architecture.preprocessing()

    def test_leading_and_parent_blocks_never_depend_on_themselves(self):
        homes = [(0, 99, q) for q in range(4)]
        gate = list(homes)
        gate[0], gate[1] = (1, 0, 0), (2, 0, 0)
        driver = ZACRouteTransitionDriver(
            self.architecture,
            homes,
            initial_one_qubit_gates=(("u1", 0), ("u2", 0), ("u3", 2)),
            placer_kind="resident",
        )
        leading = next(
            instruction for instruction in driver.initial_instructions
            if instruction["type"] == "1qGate")
        self.assertNotIn(leading["id"], leading["dependency"]["qubit"])
        self.assertEqual(leading["end_time"] - leading["begin_time"], 3 * 52)

        result = driver.route_layer(
            0, homes, gate, gate, [[0, 1]], [0],
            (("u1", 0), ("u2", 0), ("u3", 0)),
        )
        parent = next(
            instruction for instruction in result.instructions
            if instruction["type"] == "1qGate")
        self.assertNotIn(parent["id"], parent["dependency"]["qubit"])
        self.assertEqual(parent["end_time"] - parent["begin_time"], 3 * 52)


if __name__ == "__main__":
    unittest.main()
