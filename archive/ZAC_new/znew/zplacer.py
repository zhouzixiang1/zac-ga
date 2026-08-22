"""发动机：BatchAwarePlacer —— 双引擎"批次感知"门放置（ZAC_new 的核心改动）。

要解决的问题：ZAC 原版 place_gate（vmplacer.py:165）用最小权完美匹配
给每个门分工位，边权 = √距离 + √距离 + √前瞻——纯距离，且匹配这种
求解器天然表达不了"门与门之间的批次耦合"（一个门的工位选择改变
另一个门会不会跟它挡路，这是决策间的二次交互项，边权写不进去）。

两个引擎（config 里 engine 切换，消融对照）：
    * "penalty"（主推）—— 保留 ZAC 的匹配机器，外面套"冲突罚单"循环：
        第 1 轮 = 原版纯距离匹配；对派工单建冲突图着色，按"冲突条数"
        给涉事 (门, 当前工位) 的边加罚，重解匹配；罚额逐轮衰减，
        全程记录历史最优。每轮只花一次匹配 + 一次着色，运算量最小。
    * "ga"（对照）—— FABLE v1a 骨架的进化搜索（种群 6 × 迭代 8 ×
        邻居采样 24），适应度换成 zcost 的"着色分批"代价。

适应度（两引擎共用，与 FABLE 的差异 100% 集中在"分批器"）：
    FABLE:     Σ√dmax(贪心 MIS 轮) + w_conf × 冲突边数 + 前瞻
    ZAC_new:   w_batch × χ(着色批数) + Σ√dmax(色类)      + 前瞻
    着色的色类数 χ ≤ 贪心轮数（贪心剥离是着色的一种粗糙实现），
    且 χ 直接惩罚批数本身（每批有固定激活/关断开销）。

继承关系（与 GA/FABLE 相同，底盘不动）：
    BatchAwarePlacer(VertexMatchingPlacer)
      ├─ 覆写：place_gate()          ← 唯一改动的行为
      └─ 原样继承：run() / place_qubit() / filter_mapping()
"""
from __future__ import annotations

import math
import random
import time
from copy import deepcopy
from math import sqrt

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import min_weight_full_bipartite_matching

# 引用本包的打分仪表；znew/__init__.py 已把 ZAC_new 根目录挂上 sys.path，
# 所以下面的 zac.* 解析到 ZAC_new/zac/（本地副本）。
from zac.placer.vmplacer import VertexMatchingPlacer

from znew.zcost import batch_cost, compatible_2d, conflict_graph


class BatchAwarePlacer(VertexMatchingPlacer):
    """VertexMatchingPlacer 的子类：门放置改为"批次感知"双引擎。"""

    def __init__(self, mapping: list, l2: bool = False, seed: int = 0, **params):
        super().__init__(mapping, l2)
        self.rng = random.Random(seed)     # 独立随机源，种子固定 → 可复现
        # ---- 引擎选择与公共旋钮 ----
        self.engine: str = params.get("engine", "penalty")
        self.w_batch: float = params.get("w_batch", 1.0)   # 批数惩罚（√μm 单位/批）
        self.use_lookahead: bool = params.get("use_lookahead", True)
        self.min_expand: int = params.get("min_expand", 1)
        # ---- penalty 引擎旋钮 ----
        self.lambda_penalty: float = params.get("lambda_penalty", 0.25)
        self.penalty_decay: float = params.get("penalty_decay", 0.7)
        self.max_penalty_iter: int = params.get("max_penalty_iter", 8)
        # ---- ga 引擎旋钮（= FABLE v1a 预算）----
        self.population_size: int = params.get("population_size", 6)
        self.iterations: int = params.get("iterations", 8)
        self.neighbors_per_solution: int = params.get("neighbors_per_solution", 2)
        self.neighbor_sample_size: int = params.get("neighbor_sample_size", 24)
        # ---- 报告用 ----
        self.search_time = 0.0
        self.batch_preview: list = []   # 每层 (layer, χ, Σ√dmax, 冲突边数)——
                                        # 与路由实际批数对账的"预演账本"

    # ------------------------------------------------------------------ utils
    def _norm_site(self, location):
        """纠缠区工位坐标归一化到该区第一个 SLM（左右座唯一键值）。"""
        slm_idx = self.architecture.entanglement_zone[
            self.architecture.dict_SLM[location[0]].entanglement_id][0]
        return (slm_idx, location[1], location[2])

    def place_gate(self, list_qubit_mapping: list, list_two_gate_layer: list,
                   layer: int, test_reuse: bool):
        """给第 layer 层的门分配纠缠区工位 —— 被替换的唯一方法（契约同父类）。"""
        list_gate = list_two_gate_layer[0]

        # ---- 第 1 步：备好前瞻对象（与 FABLE 相同）------------------------
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

        # ---- 第 2 步：生成"点菜单"（与 GA v1a / FABLE 逐字节相同）----------
        expand_factor = max(self.min_expand, math.ceil(math.sqrt(len(list_gate)) / 2))
        candidates: list[list] = []   # 每门一个候选表；表项=(工位, d1, d2, 前瞻, q1, q2)
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
                # ZAC 原版的容量自动扩窗（vmplacer.py:235-242 原样移植）：
                # 窗口装不下本层全部门时按 门数/边长 扩边，保证匹配有解
                # （ising_n42 这类 21 门并行层没有它就会 no full matching）
                if high_c - low_c < 2 * expand_factor:
                    height_gap = math.ceil(len(list_gate) // (high_c - low_c)) - expand_factor
                    low_r = max(0, low_r - height_gap // 2)
                    high_r = min(slm.n_r, low_r + height_gap + expand_factor)
                if high_r - low_r < 2 * expand_factor:
                    width_gap = math.ceil(len(list_gate) / (high_r - low_r)) - expand_factor
                    low_c = max(0, low_c - width_gap // 2)
                    high_c = min(slm.n_c, low_c + width_gap + expand_factor)
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

        site_index = [{o[0]: k for k, o in enumerate(opts)} for opts in candidates]
        movable = [gi for gi, opts in enumerate(candidates) if len(opts) > 1]

        # ---- 第 3 步：解码器（染色体 → 合法座位表；与 FABLE 相同）----------
        def decode(chrom):
            """食堂打饭规则：按基因选菜，被占就沿菜单顺延到下一道。"""
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

        # ---- 第 4 步：适应度（着色分批版 = ZAC_new 的灵魂差异）------------
        def seats(site, q1, q2):
            """两个比特各自的真实座位（打分与落盘同一套规则）。"""
            right = (site[0] + 1, site[1], site[2])
            reuse1 = test_reuse and layer > 0 and q1 in self.list_reuse_qubit[layer - 1]
            reuse2 = test_reuse and layer > 0 and q2 in self.list_reuse_qubit[layer - 1]
            if reuse1:
                pin = gate_mapping[q1]
                return pin, (right if pin == site else site)
            if reuse2:
                pin = gate_mapping[q2]
                return (right if pin == site else site), pin
            if qubit_mapping[q1][2] < qubit_mapping[q2][2]:
                return site, right
            return right, site

        def legs_of(placed):
            """按真实座位收集搬运腿 (dist, 起x, 起y, 终x, 终y)；dist=0 不占车。"""
            legs = []
            owner = []   # 每条腿属于哪个门（罚单要按门记账）
            for gi, (site, d1, d2, dis3, q1, q2) in enumerate(placed):
                s1, s2 = seats(site, q1, q2)
                for q, tgt in ((q1, s1), (q2, s2)):
                    sx, sy = self.architecture.exact_SLM_location_tuple(qubit_mapping[q])
                    tx, ty = self.architecture.exact_SLM_location_tuple(tgt)
                    d = math.dist((sx, sy), (tx, ty))
                    if d > 1e-9:
                        legs.append((d, sx, sy, tx, ty))
                        owner.append(gi)
            return legs, owner

        def fitness(placed):
            legs, _ = legs_of(placed)
            cost, _, _ = batch_cost(legs, w_batch=self.w_batch)   # χ×w + Σ√dmax
            if self.use_lookahead:
                cost += sum(sqrt(o[3]) for o in placed)
            return cost

        # ---- 第 5 步：两个引擎，择一驱动 -----------------------------------
        t0 = time.time()
        if self.engine == "ga":
            best = self._engine_ga(candidates, site_index, movable, decode, fitness)
        else:
            best = self._engine_penalty(candidates, site_index, decode,
                                        legs_of, fitness, qubit_mapping, seats)
        self.search_time += time.time() - t0

        # ---- 预演账本：χ / Σ√dmax / 冲突边数（与路由实际批数对账用）-------
        legs, _ = legs_of(best)
        _, n_batches, n_conflicts = batch_cost(legs, w_batch=self.w_batch)
        self.batch_preview.append(
            (layer, n_batches, sum(sqrt(l[0]) for l in legs), n_conflicts))

        # ---- 第 6 步：落盘（与 ZAC/FABLE 输出逐字节兼容）-------------------
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

    # ================================================================== engines
    def _engine_ga(self, candidates, site_index, movable, decode, fitness):
        """引擎 A：FABLE v1a 进化骨架（算子、预算、精英保留全部同款）。"""
        def neighbor_solution(chrom):
            """结构化算子：交换两门工位 / 邻居挪动（与 FABLE 相同）。"""
            if movable and len(movable) >= 2 and self.rng.random() < 0.5:
                gi, gj = self.rng.sample(movable, 2)
                si, sj = decode(chrom)[gi][0], decode(chrom)[gj][0]
                if sj in site_index[gi] and si in site_index[gj]:
                    c = list(chrom)
                    c[gi] = site_index[gi][sj]
                    c[gj] = site_index[gj][si]
                    return c
            c = list(chrom)
            if movable:
                gi = self.rng.choice(movable)
                c[gi] = self.rng.choice([c[gi] + 1, c[gi] - 1,
                                         self.rng.randrange(64)])
            return c

        zeros = [0] * len(candidates)          # 贪心热启动：全 0 = 每门选最近
        population = [zeros]
        for _ in range(self.population_size - 1):
            population.append([self.rng.randrange(8) for _ in candidates])
        scored = [(fitness(decode(c)), c) for c in population]
        scored.sort(key=lambda x: x[0])
        for _ in range(self.iterations):
            offspring = []
            for _, c in scored:
                pool = [neighbor_solution(c) for _ in range(self.neighbor_sample_size)]
                pool_scored = sorted((fitness(decode(m)), m) for m in pool)
                offspring += pool_scored[: self.neighbors_per_solution]
            scored = sorted(scored + offspring, key=lambda x: x[0])[: self.population_size]
        return decode(scored[0][1])

    def _engine_penalty(self, candidates, site_index, decode, legs_of, fitness,
                        qubit_mapping, seats):
        """引擎 B：ZAC 匹配机器 + 冲突罚单迭代。

        每轮三拍：①按当前边权解最小权匹配（第 1 轮边权 = ZAC 原版距离
        公式，行为等价于原版 place_gate）②对每个门 × 每个候选工位算
        "定向冲突数"：这门若坐这个工位，会与当前其他门的腿冲突几条
        （罚额随位置变化，匹配才有梯度——均匀罚单不改变 argmin，
        λ∈{0.25,1,3} 扫描已证明只罚所选边时引擎完全不动）③罚额
        衰减后重解。历史最优全程保留，震荡也不会丢好解。
        定向罚单 = 把 ZAP 公式 17（λ·冲突数 + 距离）嫁接到匹配边权上。
        """
        # ---- 建全局二部图（行=工位，列=门；边只来自各门自己的菜单）------
        site_to_row: dict = {}
        list_rows: list = []
        edges: list = []          # (row, col, base_weight)
        for col, opts in enumerate(candidates):
            for opt in opts:
                site = opt[0]
                if site not in site_to_row:
                    site_to_row[site] = len(list_rows)
                    list_rows.append(site)
                row = site_to_row[site]
                _, d1, d2, dis3, q1, q2 = opt
                # ZAC 原版权重公式（vmplacer.py:267-270）：两比特同 SLM 同行
                # → 并排走只记最远的那条腿；否则两条腿都记
                if (qubit_mapping[q1][0] == qubit_mapping[q2][0]
                        and qubit_mapping[q1][1] == qubit_mapping[q2][1]):
                    base = math.sqrt(max(d1, d2))
                else:
                    base = math.sqrt(d1) + math.sqrt(d2)
                edges.append((row, col, base + math.sqrt(dis3)))

        n_rows, n_cols = len(list_rows), len(candidates)

        # 每门每工位的搬运腿向量缓存（菜单跨轮不变，只算一次）：
        # vec = (起x, 终x, 起y, 终y)，dist=0 的腿不占车、不参与兼容判定
        seat_vecs: dict = {}
        for col, opts in enumerate(candidates):
            for opt in opts:
                site = opt[0]
                vecs = []
                s1, s2 = seats(site, opt[4], opt[5])
                for q, tgt in ((opt[4], s1), (opt[5], s2)):
                    sx, sy = self.architecture.exact_SLM_location_tuple(qubit_mapping[q])
                    tx, ty = self.architecture.exact_SLM_location_tuple(tgt)
                    if abs(tx - sx) + abs(ty - sy) > 1e-9:
                        vecs.append((sx, tx, sy, ty))
                seat_vecs[(col, site)] = vecs

        def solve_matching(pen: dict) -> list:
            """带罚单解匹配，返回染色体（每门所选菜单下标）。"""
            rows, cols, data = [], [], []
            for (row, col, w) in edges:
                w_eff = w + pen.get((col, list_rows[row]), 0.0)
                rows.append(row)
                cols.append(col)
                data.append(w_eff)
            matrix = coo_matrix((np.array(data), (np.array(rows), np.array(cols))),
                                shape=(n_rows, n_cols))
            try:
                row_ind, col_ind = min_weight_full_bipartite_matching(matrix)
            except ValueError:
                # 保险丝：理论上 ZAC 扩窗后不会再走到这里；万一遇到病态层，
                # 退化为贪心顺延（decode 保证合法性），不崩整个编译
                return [0] * n_cols
            chrom = [0] * n_cols
            for r, c in zip(row_ind, col_ind):
                chrom[c] = site_index[c][list_rows[r]]
            return chrom

        lam = self.lambda_penalty
        pen: dict = {}
        best_placed, best_score = None, float("inf")
        for _ in range(self.max_penalty_iter):
            chrom = solve_matching(pen)
            placed = decode(chrom)
            score = fitness(placed)
            if score < best_score:
                best_score, best_placed = score, placed
            # 当前局面的冲突图；无冲突 = 一车装下 → 已经最优，提前收工
            legs, owner = legs_of(placed)
            adj = conflict_graph(legs)
            if not any(adj):
                break
            # ---- 定向罚单：对涉冲突的每个门 × 每个候选工位，算"坐这儿
            # 会与当前其他门冲突几条"，罚额 = lam × 冲突数。罚额随位置
            # 变化，匹配才有梯度（只罚所选边时 λ∈{0.25,1,3} 全部不动，
            # 扫描已证）。每轮按新局面重算，不跨轮累积。
            now_vecs = [(l[1], l[3], l[2], l[4]) for l in legs]
            conflict_gates = {owner[i] for i, nbrs in enumerate(adj) if nbrs}
            pen = {}
            for gi in conflict_gates:
                for opt in candidates[gi]:
                    n_conf = 0
                    for vec in seat_vecs[(gi, opt[0])]:
                        for k, ov in enumerate(owner):
                            if ov == gi:
                                continue
                            if not compatible_2d(vec, now_vecs[k]):
                                n_conf += 1
                    if n_conf:
                        pen[(gi, opt[0])] = lam * n_conf
            lam *= self.penalty_decay
        return best_placed
