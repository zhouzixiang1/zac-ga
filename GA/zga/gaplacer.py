"""GA placement: inherits ZAC's VertexMatchingPlacer and replaces ONLY the
gate-placement search (place_gate) with a genetic algorithm whose fitness is the
routing-aware group cost ported from MQT-QMAP's HeuristicPlacer.

Everything else (run orchestration, qubit placement, filter_mapping two-world
comparison, reuse pipeline) is inherited unchanged from ZAC, isolating the search
variable exactly like the ICCAD'25 paper isolates its A* placer.
"""
from __future__ import annotations

import random
import sys
from copy import deepcopy
from math import sqrt

sys.path.insert(0, __import__("pathlib").Path(__file__).resolve().parents[2].joinpath("ZAC").as_posix())

from zac.placer.vmplacer import VertexMatchingPlacer  # noqa: E402

from zga.racost import add_to_groups, discretize, groups_cost, groups_sd  # noqa: E402


class GAPlacer(VertexMatchingPlacer):
    """VertexMatchingPlacer with a GA-based gate placement (v1a: per-layer search)."""

    def __init__(self, mapping: list, l2: bool = False, seed: int = 0, **ga_params):
        super().__init__(mapping, l2)
        self.rng = random.Random(seed)
        self.population_size: int = ga_params.get("population_size", 6)
        self.iterations: int = ga_params.get("iterations", 8)
        self.neighbors_per_solution: int = ga_params.get("neighbors_per_solution", 2)
        self.neighbor_sample_size: int = ga_params.get("neighbor_sample_size", 24)
        self.use_sd: bool = ga_params.get("use_sd", False)  # ablation: add SD term
        self.ga_time = 0.0

    # ------------------------------------------------------------------ utils
    def _norm_site(self, location):
        slm_idx = self.architecture.entanglement_zone[
            self.architecture.dict_SLM[location[0]].entanglement_id][0]
        return (slm_idx, location[1], location[2])

    def place_gate(self, list_qubit_mapping: list, list_two_gate_layer: list,
                   layer: int, test_reuse: bool):
        """GA replacement of VertexMatchingPlacer.place_gate (same contract)."""
        import math
        list_gate = list_two_gate_layer[0]

        # reuse partner lookahead (same as vmplacer.py:175-184)
        dict_reuse_neighbor = {}
        if len(list_two_gate_layer) > 1 and test_reuse:
            for q in self.list_reuse_qubit[layer]:
                for gate in list_two_gate_layer[1]:
                    if q == gate[0]:
                        dict_reuse_neighbor[q] = gate[1]
                        break
                    if q == gate[1]:
                        dict_reuse_neighbor[q] = gate[0]
                        break

        if layer > 0:
            gate_mapping = list_qubit_mapping[0]
            qubit_mapping = list_qubit_mapping[1]
        else:
            gate_mapping = None
            qubit_mapping = list_qubit_mapping[0]

        # ---- build deterministic, distance-ordered candidate list per gate ----
        expand_factor = math.ceil(math.sqrt(len(list_gate)) / 2)
        candidates: list[list] = []  # per gate: list of (site, d1, d2, dis3)
        for gate in list_gate:
            q1, q2 = gate
            pinned = test_reuse and layer > 0 and q1 in self.list_reuse_qubit[layer - 1]
            if not pinned and test_reuse and layer > 0 and q2 in self.list_reuse_qubit[layer - 1]:
                pinned, side = True, 2
            elif pinned:
                side = 1
            else:
                side = 0

            if pinned:
                loc = gate_mapping[q1 if side == 1 else q2]
                site = self._norm_site(loc)
                other = q2 if side == 1 else q1
                d_other = self.architecture.distance(
                    qubit_mapping[other][0], qubit_mapping[other][1], qubit_mapping[other][2],
                    site[0], site[1], site[2])
                # dis3: partner of the reused qubit in the next layer
                q_r = q1 if side == 1 else q2
                dis3 = 0.0
                if q_r in dict_reuse_neighbor:
                    q3 = dict_reuse_neighbor[q_r]
                    dis3 = self.architecture.distance(
                        qubit_mapping[q3][0], qubit_mapping[q3][1], qubit_mapping[q3][2],
                        site[0], site[1], site[2])
                candidates.append([(site, 0.0, d_other, dis3, q1, q2)])
                continue

            set_sites = set()
            for near in self.architecture.nearest_entanglement_site(
                    qubit_mapping[q1][0], qubit_mapping[q1][1], qubit_mapping[q1][2],
                    qubit_mapping[q2][0], qubit_mapping[q2][1], qubit_mapping[q2][2]):
                set_sites.add(near)
                slm = self.architecture.dict_SLM[near[0]]
                low_r = max(0, near[1] - expand_factor)
                high_r = min(slm.n_r, near[1] + expand_factor + 1)
                low_c = max(0, near[2] - expand_factor)
                high_c = min(slm.n_c, near[2] + expand_factor + 1)
                for r in range(low_r, high_r):
                    for c in range(low_c, high_c):
                        set_sites.add((near[0], r, c))
            opts = []
            for site in set_sites:
                if qubit_mapping[q1][2] < qubit_mapping[q2][2]:
                    s1, s2 = site, (site[0] + 1, site[1], site[2])
                else:
                    s1, s2 = (site[0] + 1, site[1], site[2]), site
                d1 = self.architecture.distance(
                    qubit_mapping[q1][0], qubit_mapping[q1][1], qubit_mapping[q1][2],
                    s1[0], s1[1], s1[2])
                d2 = self.architecture.distance(
                    qubit_mapping[q2][0], qubit_mapping[q2][1], qubit_mapping[q2][2],
                    s2[0], s2[1], s2[2])
                dis3 = 0.0
                for q, other in ((q1, q2), (q2, q1)):
                    if q in dict_reuse_neighbor:
                        q3 = dict_reuse_neighbor[q]
                        target = s1 if q == q1 else s2
                        dis3 = self.architecture.distance(
                            qubit_mapping[q3][0], qubit_mapping[q3][1], qubit_mapping[q3][2],
                            target[0], target[1], target[2])
                        break
                opts.append((site, d1, d2, dis3, q1, q2))
            opts.sort(key=lambda o: o[1] + o[2])
            candidates.append(opts)

        # ------------------------- GA machinery -------------------------
        def decode(chrom):
            used = {}
            placed = []
            for gi, opts in enumerate(candidates):
                if len(opts) == 1:
                    placed.append(opts[0])
                    continue
                idx = chrom[gi] % len(opts)
                for step in range(len(opts)):
                    cand = opts[(idx + step) % len(opts)]
                    if cand[0] not in used:
                        break
                used[cand[0]] = gi
                placed.append(cand)
            return placed

        def fitness(placed):
            # routing-aware group cost on discretized rows/cols
            moves = []  # (atom pos, target pos, distance)
            for site, d1, d2, dis3, q1, q2 in placed:
                moves.append(d1)
                moves.append(d2)
            src_r, src_c, tgt_r, tgt_c = [], [], [], []
            for site, d1, d2, dis3, q1, q2 in placed:
                for q, tgt in ((q1, site), (q2, (site[0] + 1, site[1], site[2]))):
                    sx, sy = self.architecture.exact_SLM_location_tuple(qubit_mapping[q])
                    tx, ty = self.architecture.exact_SLM_location_tuple(tgt)
                    src_c.append(sx); src_r.append(sy)
                    tgt_c.append(tx); tgt_r.append(ty)
            dr = discretize(src_r); tr = discretize(tgt_r)
            dc = discretize(src_c); tc = discretize(tgt_c)
            groups = []
            maxd = []
            k = 0
            for site, d1, d2, dis3, q1, q2 in placed:
                add_to_groups(dc[k], tc[k], dr[k], tr[k], d1, groups, maxd)
                k += 1
                add_to_groups(dc[k], tc[k], dr[k], tr[k], d2, groups, maxd)
                k += 1
            cost = groups_cost(maxd) + sum(sqrt(o[3]) for o in placed)
            if self.use_sd:
                cost += groups_sd(groups)
            return cost

        def mutate(chrom):
            c = list(chrom)
            for _ in range(1 + self.rng.randrange(2)):
                gi = self.rng.randrange(len(c))
                c[gi] = self.rng.choice([c[gi] + 1, c[gi] - 1,
                                         self.rng.randrange(64)]) if len(candidates[gi]) > 1 else c[gi]
            return c

        import time
        t0 = time.time()
        zeros = [0] * len(candidates)
        population = [zeros]
        for _ in range(self.population_size - 1):
            population.append([self.rng.randrange(8) for _ in candidates])
        scored = [(fitness(decode(c)), c) for c in population]
        scored.sort(key=lambda x: x[0])
        for _ in range(self.iterations):
            offspring = []
            for _, c in scored:
                pool = [mutate(c) for _ in range(self.neighbor_sample_size)]
                pool_scored = sorted((fitness(decode(m)), m) for m in pool)
                offspring += pool_scored[: self.neighbors_per_solution]
            scored = sorted(scored + offspring, key=lambda x: x[0])[: self.population_size]
        best = decode(scored[0][1])
        self.ga_time += time.time() - t0

        # ---- write result like vmplacer.py:298-322 ----
        tmp_mapping = deepcopy(qubit_mapping)
        for (site, d1, d2, dis3, q1, q2) in best:
            reuse1 = test_reuse and layer > 0 and q1 in self.list_reuse_qubit[layer - 1]
            reuse2 = test_reuse and layer > 0 and q2 in self.list_reuse_qubit[layer - 1]
            if reuse1:
                tmp_mapping[q1] = gate_mapping[q1]
                if site == gate_mapping[q1]:
                    tmp_mapping[q2] = (site[0] + 1, site[1], site[2])
                else:
                    tmp_mapping[q2] = site
            elif reuse2:
                tmp_mapping[q2] = gate_mapping[q2]
                if site == gate_mapping[q2]:
                    tmp_mapping[q1] = (site[0] + 1, site[1], site[2])
                else:
                    tmp_mapping[q1] = site
            else:
                if qubit_mapping[q1][2] < qubit_mapping[q2][2]:
                    tmp_mapping[q1] = site
                    tmp_mapping[q2] = (site[0] + 1, site[1], site[2])
                else:
                    tmp_mapping[q1] = (site[0] + 1, site[1], site[2])
                    tmp_mapping[q2] = site
        self.mapping.append(tmp_mapping)
