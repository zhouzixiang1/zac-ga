"""Naviz evaluator reproducing ICCAD'25 Table I metrics.

Verified semantics on seca_n11 (RoutingAgnostic): paper reports 112 steps / 12.2ms;
counting load->move*->store cycles as steps and per-cycle time =
t(max start-to-end atom displacement) + 2 * atom_transfer yields 112 / 12.15ms.

- steps: number of `@+ load ... @+ store` rearrangement cycles
- rearrangement_duration: per cycle, sqrt(d_max/a) with a = 2750 m/s^2 (um/us^2),
  where d_max is the largest start(load-time) -> end(after store) displacement,
  plus 2 * atom_transfer duration. Parking detours within a cycle are ignored,
  matching the paper's cost sum_G sqrt(dmax(G)).
"""
from __future__ import annotations

import re
from math import sqrt
from typing import Mapping

MOVE_LINE = re.compile(r"\((-?\d+\.\d+), (-?\d+\.\d+)\) (\w+)")
ATOM_LINE = re.compile(r"atom\s+\((-?\d+\.\d+),\s*(-?\d+\.\d+)\)\s+(\w+)")


class NavizEvaluator:
    def __init__(self, arch: Mapping, motion_model: str = "sqrt") -> None:
        self.arch = arch
        self.motion_model = motion_model
        self.reset()

    def reset(self) -> None:
        self.rearrangement_duration = 0.0
        self.rearrangement_steps = 0
        self.two_qubit_gate_layer = 0
        self.max_two_qubit_gates = 0
        self.atom_locations: dict[str, tuple[float, float]] = {}
        self._n_transfer_instr = 0

    def _t(self, d: float) -> float:
        if self.motion_model == "sqrt":
            return sqrt(d / 0.00275)  # d/t^2 = a = 2750 m/s^2
        t_d_max, d_max = 200.0, 110.0
        jerk = 32 * d_max / t_d_max**3
        v_max = d_max / t_d_max * 2
        return 2 * (4 * d / jerk) ** (1 / 3) if d <= d_max else t_d_max + (d - d_max) / v_max

    @staticmethod
    def _dist(a, b) -> float:
        return sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2)

    def evaluate(self, code: str) -> dict:
        lines = code.splitlines()
        i = 0
        while i < len(lines):
            m = ATOM_LINE.match(lines[i])
            if not m:
                break
            self.atom_locations[m.group(3)] = (float(m.group(1)), float(m.group(2)))
            i += 1

        transfer = self.arch["operation_duration"]["atom_transfer"]

        def block_or_single(j: int, prefix: str) -> tuple[list[str], int]:
            line = lines[j]
            if line.rstrip().endswith("["):
                toks = []
                j += 1
                while lines[j].strip() != "]":
                    toks.append(lines[j].strip())
                    j += 1
                return toks, j + 1
            return [line[len(prefix):].strip()], j + 1

        y_min = self.arch["entanglement_zones"][0]["slms"][0]["location"][1]

        held: set[str] = set()               # atoms currently picked up
        job_start: dict[str, tuple] = {}     # atom -> position at its load time (current job)
        in_job = False

        def flush_job() -> None:
            nonlocal in_job
            if job_start:
                d_max = max(self._dist(self.atom_locations[a], p) for a, p in job_start.items())
                self.rearrangement_duration += self._t(d_max)
                job_start.clear()
            in_job = False

        while i < len(lines):
            line = lines[i]
            if line.startswith("@+ load"):
                toks, i = block_or_single(i, "@+ load")
                self._n_transfer_instr += 1
                if not in_job and toks:
                    self.rearrangement_steps += 1  # one maximal pickup-transport-dropoff job
                    in_job = True
                for a in toks:
                    if a not in job_start:
                        job_start[a] = self.atom_locations[a]
                    held.add(a)
                continue
            if line.startswith("@+ move"):
                if line.rstrip().endswith("["):
                    i += 1
                    while lines[i].strip() != "]":
                        mm = MOVE_LINE.match(lines[i].strip())
                        if mm:
                            self.atom_locations[mm.group(3)] = (float(mm.group(1)), float(mm.group(2)))
                        i += 1
                    i += 1
                else:
                    mm = re.match(r"@\+ move \((-?\d+\.\d+), (-?\d+\.\d+)\) (\w+)", lines[i])
                    if mm:
                        self.atom_locations[mm.group(3)] = (float(mm.group(1)), float(mm.group(2)))
                    i += 1
                continue
            if line.startswith("@+ store"):
                toks, i = block_or_single(i, "@+ store")
                self._n_transfer_instr += 1
                for a in toks:
                    held.discard(a)
                if not held:
                    flush_job()
                continue
            if line.startswith("@+ cz"):
                flush_job()
                self.two_qubit_gate_layer += 1
                n_atoms = sum(1 for c in self.atom_locations.values() if c[1] >= y_min)
                assert n_atoms % 2 == 0, f"odd atoms in entanglement zone: {n_atoms}"
                self.max_two_qubit_gates = max(self.max_two_qubit_gates, n_atoms // 2)
                i += 1
                continue
            if line.startswith(("@+ u", "@+ rz", "@+ ry", "@+ sh")):
                flush_job()
                if line.rstrip().endswith("["):
                    _, i = block_or_single(i, line.split()[1])
                else:
                    i += 1
                continue
            raise ValueError(f"Unrecognized operation: {line!r}")
        flush_job()
        self.rearrangement_duration += self._n_transfer_instr * transfer
        return {
            "rearrangement_steps": self.rearrangement_steps,
            "rearrangement_duration": self.rearrangement_duration,
            "two_qubit_gate_layer": self.two_qubit_gate_layer,
            "max_two_qubit_gates": self.max_two_qubit_gates,
        }
