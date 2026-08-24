from __future__ import annotations

import copy
import sys
import unittest
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from native.tests.python.benchmark_real_boundary import (  # noqa: E402
    ABI7_TRANSITION_CACHE_FIELDS,
    _transition_rows,
)


class ABI7RealBoundaryAuditTests(unittest.TestCase):
    SCHEDULE = (((0, 1),), ((0, 1),))

    @staticmethod
    def _fixture():
        source = [
            (1, 0, 0), (2, 0, 0), (1, 1, 0), (1, 2, 0), (1, 3, 0)]
        boundary = list(source)
        boundary[3] = (0, 9, 0)  # RETURN
        boundary[4] = (1, 4, 0)  # derived RESEAT
        target = list(boundary)
        target[0] = (1, 5, 0)
        target[1] = (2, 5, 0)
        mapping = [list(source), source, boundary, target, list(target)]

        row = {
            "layer": 0,
            "selected_horizon": 8,
            "ablation_policy": "optimize",
            "fitness_phase_mode": "phase",
            "eligible_decisions": 3,
            "capacity": 1,
            "physical_guard_returns": 1,
            "rent_guard_returns": 0,
            "stay": 1,
            "return": 1,
            "reseat": 1,
            "return_assignments": [{
                "atom": 3,
                "location": [0, 9, 0],
            }],
            "physical": {
                "negative_log_fidelity": 0.2,
                "move_batches": 2,
                "move_time_us": 3.0,
                "transfers": 2,
            },
            "forecast_objective": {
                "weighted_negative_log_fidelity": 0.1,
                "search_negative_log_fidelity": 0.3,
                "forecast_by_depth": [0.1],
                "forecast_breakdown": {"transfer": 0.1},
            },
            "search_mode": "enumerate",
            "cache": {"evaluations": 4, "unique_evaluations": 4},
        }
        state = (
            8, "optimize", "phase", tuple(source),
            0.0, (0.0,) * 5, (), (), 0.0, (), (), (), (), (),
            ((0, 1),), (), (3,), (2, 3, 4), 1, (3,), (), (),
        )
        assert len(state) == len(ABI7_TRANSITION_CACHE_FIELDS)
        placer = SimpleNamespace(
            decision_log=[row, {"layer": 1}],
            transition_cache=OrderedDict([(state, (1, 0, 1, 0))]),
            mapping=mapping,
        )
        return placer

    def test_reads_abi7_eligible_atoms_and_audits_reseat_from_mapping(self):
        rows = _transition_rows(self._fixture(), self.SCHEDULE)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["gate_option_genes"], [1])
        self.assertEqual(rows[0]["eligible_atoms"], [2, 3, 4])
        self.assertEqual(rows[0]["residency_bits"], [0, 1, 0])
        self.assertEqual(rows[0]["stay_atoms"], [2])
        self.assertEqual(rows[0]["return_atoms"], [3])
        self.assertEqual(rows[0]["reseat_atoms"], [4])

    def test_rejects_old_positional_eligible_lookup_shape(self):
        placer = self._fixture()
        state, chromosome = next(iter(placer.transition_cache.items()))
        drifted = list(state)
        drifted[7] = (2, 3, 4)
        drifted[17] = ()
        placer.transition_cache = OrderedDict([(tuple(drifted), chromosome)])
        with self.assertRaisesRegex(
                AssertionError, "eligible atom count differs"):
            _transition_rows(placer, self.SCHEDULE)

    def test_rejects_return_assignment_semantic_drift(self):
        placer = self._fixture()
        placer.decision_log[0] = copy.deepcopy(placer.decision_log[0])
        placer.decision_log[0]["return_assignments"][0]["atom"] = 2
        with self.assertRaisesRegex(
                AssertionError, "RETURN assignment differs"):
            _transition_rows(placer, self.SCHEDULE)


if __name__ == "__main__":
    unittest.main()
