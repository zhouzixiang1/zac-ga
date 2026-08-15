"""ZAC_GA: ZAC with the gate-placement search replaced by GA. ZAC source untouched."""
from __future__ import annotations

import sys
import time
from copy import deepcopy
from pathlib import Path

ZAC_ROOT = Path(__file__).resolve().parents[2] / "ZAC"
if str(ZAC_ROOT) not in sys.path:
    sys.path.insert(0, str(ZAC_ROOT))

from zac.zac import ZAC  # noqa: E402


class ZAC_GA(ZAC):
    GA_KEYS = ("population_size", "iterations", "neighbors_per_solution",
               "neighbor_sample_size", "use_sd", "seed")

    def __init__(self):
        super().__init__()
        self.placer_kind = "zac"
        self.ga_params: dict = {}

    def parse_setting(self, setting: dict):
        super().parse_setting(setting)
        self.placer_kind = setting.get("placer", "zac")
        self.ga_params = {k: setting[k] for k in self.GA_KEYS if k in setting}

    def place_qubit_intermedeiate(self):
        if self.placer_kind != "ga":
            return super().place_qubit_intermedeiate()
        from zga.gaplacer import GAPlacer
        t_p = time.time()
        placer = GAPlacer(deepcopy(self.qubit_mapping[0]), **self.ga_params)
        placer.run(self.architecture, self.qubit_mapping, self.gate_scheduling,
                   self.dynamic_placement, self.reuse_qubit)
        self.qubit_mapping = placer.mapping
        self.runtime_analysis["intermediate placement"] = time.time() - t_p
        self.runtime_analysis["ga gate placement"] = placer.ga_time
