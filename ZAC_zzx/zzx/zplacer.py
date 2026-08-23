"""发动机（ZAC_zzx 的放置器层）：本文件有两个类，主角在后半部分。

┌─ 阅读指南 ────────────────────────────────────────────────────────────┐
│ ① BatchAwarePlacer（前半，ZAC_new 的对照引擎，placer="batch" 时用）    │
│    place_gate 整体替换 ZAC 的门放置：双引擎 penalty（匹配+定向冲突罚单）│
│    / ga（进化搜索），适应度 = 着色分批代价 w_batch×χ + Σ√dmax。        │
│    每轮做完门仍全员回存储——它是"批次感知但不驻留"的对照组。             │
│                                                                       │
│ ② ResidentPlacer（后半，ZAC_zzx 主角，placer="resident" 时用）        │
│    驻留编译器：做完门默认不回存储（resident.py 负责决策），门位由       │
│    engine="match"（A1 解析匹配）或 "ga"（A3 联合搜索）产生。           │
│    方法地图（按调用顺序）：                                             │
│      run()                轮循环总控：plan → decide_lazy → commit      │
│      _plan_round()        A1 门位：菜单三级兜底 + ZAC 距离匹配         │
│        _zone_anchors()      原子对 → 入区锚点（区内原子锚=自己座位）    │
│        _expanded_sites()    锚点展开窗 + ZAC 容量自动扩窗              │
│        _all_zone_sites()    全区左 SLM 工位全集（终极兜底搜索域）      │
│        _build_opts()        候选集 → (site,w,q1,q2) 菜单 + 硬排除      │
│        _pair_seats()        工位 → 座位对朝向（驻留者保座优先）         │
│        _site_weight()       ZAC 权重公式 + 驻留者移动折扣              │
│        _match_gates()       scipy 最小权匹配 + 贪心兜底                │
│        _repair_placements() 终检安全网：违例门位改选全区过滤域          │
│      _ga_step()          A3 联合搜索：门位基因 ∪ STAY/RETURN 基因，    │
│                          适应度 = 分相位着色 + 锚点前瞻                │
│      _commit_round()     参与者落门位 + 登记簿入区，追加映射           │
│      _append_boundary()  RETURN 者落存储位，追加边界映射               │
│      _assert_contract()  流契约断言：长度 2n+1 / 拷贝不变式 / 单射      │
└───────────────────────────────────────────────────────────────────────┘

核心思想（为什么放置要重写而不是改 ZAC 的 place_gate）：
ZAC 原版（vmplacer.py:165）用最小权完美匹配分工位，边权 = 纯距离——
匹配的边权表达不了"门与门之间的批次耦合"（一个门的工位选择改变另一个门
要不要多开一班车，这是决策间的二次交互项）。本文件的解法是把整套座位
方案放进 GA 染色体，用着色适应度整体计价；驻留语义则要求重写轮循环
（父类每轮强制全员回存储，vmplacer.py:343-348——驻留在父类里无法表达）。
"""
from __future__ import annotations

import math
import random
import time
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
from itertools import product
from math import sqrt

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import min_weight_full_bipartite_matching

# 引用本包的打分仪表；zzx/__init__.py 已把 ZAC_zzx 根目录挂上 sys.path，
# 所以下面的 zac.* 解析到 ZAC_zzx/zac/（本地副本）。
from zac.placer.vmplacer import VertexMatchingPlacer

from zzx.boundary_problem import (
    ArchitectureSnapshot as BoundaryArchitectureSnapshot,
    BoundaryConfig,
    BoundaryProblem,
    CandidatePlan,
    Ghost as BoundaryGhost,
    Leg as BoundaryLeg,
    MovementPhase as BoundaryMovementPhase,
    Point as BoundaryPoint,
    RichForecastTerm,
    RichGateOption,
    RichH0Problem,
    RichReturnOption,
    RichSearchConfig,
)
from zzx.algorithm_v2 import (AdaptiveHorizonDecision, CacheStats,
                              ForecastOracle, PhysicalCostBreakdown,
                              PhysicalIncrementalCost,
                              build_seed_population, is_adaptive_lookahead,
                              is_decay_lookahead,
                              maximum_lookahead_horizon,
                              resident_decision_candidates)
from zzx.native_backend import select_backend
from zzx.zcost import batch_cost, compatible_2d, conflict_graph
from zzx.ghost import (ghost_hits, hit_count, leg_hits, new_conflicts,
                       pair_edges)
from zzx.resident import (NextUse, ResidentRegistry, boundary_legs,
                          decide_lazy, match_return_sites,
                          _all_storage_sites, _box_sites)


@dataclass(frozen=True)
class _BackendMovementPhase:
    """One phase represented in both legacy and native-exact forms.

    ``PhysicalIncrementalCost`` deliberately consumes objects structurally, so
    these proxy properties keep all pre-migration callers byte-for-byte stable
    while retaining the exact legs/ghosts/owners needed by ``BoundaryProblem``.
    """

    physical: object
    boundary: BoundaryMovementPhase

    @property
    def batches(self):
        return self.physical.batches

    @property
    def move_time_us(self):
        return self.physical.move_time_us

    @property
    def total_distance_um(self):
        return self.physical.total_distance_um

    @property
    def movers(self):
        return self.physical.movers


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

        # ---- 第 4 步：适应度（着色分批版 = ZAC_zzx 的灵魂差异）------------
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


# ════════════════════════════════════════════════════════════ 驻留放置器（ZAC_zzx 主放置器）
class ResidentPlacer(VertexMatchingPlacer):
    """驻留放置器：门位匹配（ZAC 距离公式）+ 边界惰性决策。

    为什么整个覆写父类 run()（审计 MAJOR-3）：
        父类循环每轮调 place_qubit 强制激发区全员回存储（vmplacer.py:343-348），
        复用两世界 filter_mapping 只看一个边界——驻留语义在父类里无法表达。
    本类轮循环（顺序与原生编译器同构：先定门位，闲人让路）：
        plan(L+1)   下一轮门位（菜单按登记簿真实位置生成，ZAC 容量自动扩窗）
        decide_lazy 边界决策（E2 挡路/容量阀强制 RETURN，其余 STAY——M1 模块）
        commit(L+1) 登记簿入区 + 映射流追加
    流契约（断言强制，审计 MAJOR-4）：长度恰 2n+1；[0]=初始布局；[2L+1]=第 L 轮
    门位（非参与者逐位拷贝 [2L]）；[2L+2]=边界态；每张映射单射。
    """

    def __init__(self, mapping: list, l2: bool = False, seed: int = 0, **params):
        super().__init__(mapping, l2)
        self.rng = random.Random(seed)              # RNG 隔离（审计 MAJOR-6：模块级共享会毁 SA 确定性）
        # Independent current-transition stream retained for legacy adaptive
        # H=0/1/2 regression.  Formal decay uses the ABI3 one-call solver.
        self.safety_rng = random.Random(seed)
        self.theta_capacity: float = params.get("theta_capacity", 0.9)
        self.box_ratio: int = params.get("box_ratio", 3)
        self.alpha_lookahead: float = params.get("alpha_lookahead", 0.1)
        self.final_return_home: bool = params.get("final_return_home", False)
        # 驻留者移动折扣（解析版钉扎）：区内原子的移动在匹配权重里打折，
        # 让门位倾向"驻留者不动、搭档来会合"（原生编译器 K2/K3 的近似）——
        # 否则门位落在两人折中点，链式电路逐轮漂移，驻留者反复长走
        self.w_resident: float = params.get("w_resident", 0.3)
        # K2 钉扎半径：有驻留者参与的门，菜单塌缩到其座位对 ± pin_radius 列
        # （ZAC 复用塌缩 vmplacer.py:208-215 的同款结构）——驻留者一步不走、
        # 新来者跑全程。实测：无钉扎时门位落在两人折中点，链式电路每轮
        # 仍要付"驻留者区内长走 + 新人入区"两条腿，驻留的收益被漂移吃光
        self.pin_radius: int = params.get("pin_radius", 2)
        # 钉扎列偏移罚：钉扎菜单里离驻留者每远一列的罚额。消融实测（4 电路）：
        #   不钉扎     ghz 1.118 / bv 1.137 / ising 0.921 / qft 1.341 → geo 1.115
        #   钉扎 w=0   ghz 1.533 / bv 1.372 / ising 0.815 / qft 0.763 → geo 1.095 ★默认
        #   钉扎 w=2   ghz 1.533 / bv 1.372 / ising 0.815 / qft 1.198 → geo 1.197
        # 链式电路钉死反而亏：座位锚在原地、新原子家门逐轮走远，入区腿线性上涨
        # （ghz 实测 157→250μs/轮）——"钉不钉"本质是逐门决策，解析规则顾此
        # 失彼，默认取 geomean 最优档，真正解法在 M3 搜索层（适应度定夺）
        self.w_pin: float = params.get("w_pin", 0.0)
        # ---- M3：GA 决策层旋钮 ----
        # engine="match" 即 A1（距离匹配 + 惰性决策）；"ga" 为 A3（联合搜索）
        self.engine: str = params.get("engine", "match")
        # 每批 2×15μs 固定开销的 √μm 当量（审计标定：1.0 低估 36%）
        self.w_batch: float = params.get("w_batch", 1.57)
        # ---- 前瞻 v2（用户思路一/二）：软层罚项 ----
        # 鬼点罚权重：每对"同批组合会撞静止原子"的腿 ≈ 一次被迫分批（批当量）
        self.w_ghost: float = params.get("w_ghost", 1.0)
        # 顺序罚权重：未来层存储搭档的进场行碎片化，每多一行 ≈ 多一批
        self.w_ord: float = params.get("w_ord", 1.0)
        # 逐批次折现（γ^批距）：越远的窗权重越低；1.0=不折现
        self.gamma_batch: float = params.get("gamma_batch", 0.5)
        # fitness="phase" 分相位着色（正确）；"lumped" 混合（A3' 消融用）
        self.fitness_mode: str = params.get("fitness_mode", "phase")
        # GA 预算（ZAC_new 引擎 A 同款默认）
        self.population_size: int = params.get("population_size", 6)
        self.iterations: int = params.get("iterations", 8)
        self.neighbors_per_solution: int = params.get("neighbors_per_solution", 2)
        self.neighbor_sample_size: int = params.get("neighbor_sample_size", 24)
        # Shared Python/native search-budget contract.  An omitted unique-
        # evaluation budget is deterministically derived from the historical GA
        # knobs so M3 and M4 cannot receive different effective work by accident.
        self.elite_count: int = int(params.get("elite_count", 1))
        self.early_stop_patience: int = int(
            params.get("early_stop_patience", 0))
        configured_unique_budget = params.get("max_unique_evaluations")
        self.max_unique_evaluations: int = int(
            configured_unique_budget
            if configured_unique_budget is not None
            else (self.population_size * self.iterations
                  * self.neighbor_sample_size))
        self.operator_profile: str = params.get("operator_profile", "exact")
        if not 1 <= self.elite_count <= self.population_size:
            raise ValueError("elite_count must be in [1, population_size]")
        if self.early_stop_patience < 0:
            raise ValueError("early_stop_patience must be non-negative")
        if self.max_unique_evaluations <= 0:
            raise ValueError("max_unique_evaluations must be positive")
        if self.operator_profile not in {"exact", "tuned"}:
            raise ValueError("operator_profile must be 'exact' or 'tuned'")
        self.experiment_schema: int = params.get("experiment_schema", 1)
        self.method_id: str = params.get("method_id", "legacy")
        self.objective: str = params.get("objective", "legacy")
        # Explicit migration switch.  Existing research fixtures intentionally
        # default to the Python oracle; formal runners set backend="native" and
        # formal_native=True, which makes extension load/ABI failures fatal.
        self.resident_backend_requested: str = params.get(
            "backend", params.get("resident_backend", "reference"))
        # Public Schema-2 configs use ``native_fail_closed``.  The older
        # ``formal_native`` spelling remains a compatibility alias for golden
        # fixtures; an explicit public value takes precedence.
        self.formal_native: bool = bool(params.get(
            "native_fail_closed", params.get("formal_native", False)))
        self.native_wheel_sha256: str = str(
            params.get("native_wheel_sha256", ""))
        if self.resident_backend_requested not in {"reference", "native"}:
            raise ValueError(
                "resident backend must be explicitly 'reference' or 'native'")
        if self.formal_native and self.resident_backend_requested != "native":
            raise ValueError("formal resident runs require backend='native'")
        self.lookahead_horizon_config = deepcopy(
            params.get("lookahead_horizon", 0))
        # ``lookahead_horizon`` is the maximum readable window.  Formal M3/M4
        # share one geometric-decay spec and differ only in max_horizon=0/8;
        # the old adaptive 0/1/2 form remains a legacy regression path.
        self.lookahead_horizon: int = maximum_lookahead_horizon(
            self.lookahead_horizon_config)
        self.adaptive_lookahead: bool = is_adaptive_lookahead(
            self.lookahead_horizon_config)
        self.decay_lookahead: bool = is_decay_lookahead(
            self.lookahead_horizon_config)
        self.fitness_cache: bool = params.get("fitness_cache", True)
        # Formal ablation controls are injected by method_driver only after the
        # frozen main Schema-2 config has passed its strict validation.  They are
        # intentionally absent from the M3/M4 config contract.
        self.ablation_policy: str = params.get("ablation_policy", "optimize")
        self.ablation_fitness_mode: str = params.get(
            "ablation_fitness_mode", "phase")
        if self.ablation_policy not in {
                "optimize", "always_stay", "always_return", "adjacent_only"}:
            raise ValueError(f"unknown ablation decision policy: {self.ablation_policy!r}")
        if self.ablation_fitness_mode not in {"phase", "lumped_greedy"}:
            raise ValueError(
                f"unknown ablation fitness mode: {self.ablation_fitness_mode!r}")
        self.search_time = 0.0
        self.decision_log: list = []                # 每层决策统计（供账本/实验）
        self.cache_stats = CacheStats()
        self.transition_cache = OrderedDict()
        self.transition_cache_limit = 256
        self.phase_cost_cache = OrderedDict()
        self.phase_cost_cache_limit = 65_536
        self.return_candidate_cache = OrderedDict()
        self.return_cache_limit = 8_192
        self.rollout_pair_cache = OrderedDict()
        self.rollout_site_cache = OrderedDict()
        self.rollout_geometry_cache_limit = 65_536
        self.registry = None
        self.nu = None
        self.forecast = None
        self.boundary_backend = None
        self.boundary_architecture_snapshot = None
        self.boundary_site_locations: tuple[tuple[int, int, int], ...] = ()
        self.boundary_site_id: dict[tuple[int, int, int], int] = {}
        self.boundary_storage_locations: tuple[tuple[int, int, int], ...] = ()
        self.boundary_storage_site_id: dict[tuple[int, int, int], int] = {}
        self.boundary_backend_metrics = {
            "marshal_ns": 0,
            "search_kernel_ns": 0,
            "fitness_ns": 0,
            "selection_ns": 0,
            "native_parse_ns": 0,
            "native_serialize_ns": 0,
            "calls": 0,
            "candidates": 0,
        }
        self.backend_timing_log: list[dict] = []
        # q -> (visible use layer, exact zone seat).  A lookahead STAY is a
        # physical residency commitment, not merely a promise that a later
        # greedy placement may immediately undo with a zone-to-zone transfer.
        self.residency_commitments: dict[int, tuple[int, tuple]] = {}

    # ------------------------------------------------------------------ 轮循环
    def _prepare_boundary_architecture(self):
        """Preload immutable storage geometry once for the native solver.

        Rich H=0 RETURN matching occasionally needs the global free-site
        fallback used by :func:`match_return_sites`.  Sending all storage sites
        at every layer would dominate marshalling, so site ids and coordinates
        live in the persistent ``ArchitectureSnapshot``.  Per-boundary payloads
        contain only the small local matching columns and occupied ids.
        """
        storage_locations = tuple(sorted(_all_storage_sites(self.architecture)))
        # One immutable index covers storage, entanglement, and every current
        # mapping position.  Rich boundary DTOs can therefore send compact ids
        # instead of repeating coordinates, legs, owners, and ghost rows for
        # every gate option at every layer.
        all_locations = set(storage_locations)
        for array, slm in sorted(self.architecture.dict_SLM.items()):
            all_locations.update(
                (int(array), row, column)
                for row in range(slm.n_r)
                for column in range(slm.n_c)
            )
        site_locations = tuple(sorted(all_locations))
        site_id = {
            tuple(location): index
            for index, location in enumerate(site_locations)
        }
        storage_site_id = {
            tuple(location): site_id[tuple(location)]
            for location in storage_locations
        }
        coordinates = tuple(
            BoundaryPoint(*self.architecture.exact_SLM_location_tuple(location))
            for location in site_locations
        )
        self.boundary_site_locations = site_locations
        self.boundary_site_id = site_id
        # Backwards-compatible name: site ids are global ABI3 ids, so native
        # RETURN assignments are decoded through the complete location table.
        self.boundary_storage_locations = site_locations
        self.boundary_storage_site_id = storage_site_id
        self.boundary_architecture_snapshot = BoundaryArchitectureSnapshot(
            len(self.mapping[0]), coordinates,
            tuple(storage_site_id[location] for location in storage_locations))
        return self.boundary_architecture_snapshot

    def _initialize_run_state(self, architecture, qubit_mapping,
                              gate_scheduling, *, forecast_source=None):
        """Initialize the state shared by batch and bounded-memory execution.

        ``gate_scheduling`` is the placement view used by the current physical
        stage.  Batch execution passes its in-memory list; Large execution passes
        a sequence facade backed by SQLite.  ``forecast_source`` may therefore be
        a bounded :class:`ForecastLayerProvider` while preserving the exact
        :class:`ForecastOracle` gate on future information.
        """
        self.architecture = architecture
        self.gate_scheduling = gate_scheduling
        n = len(gate_scheduling)
        # Neutralise the legacy adjacent-layer reuse mechanism.  The resident
        # registry is the sole source of cross-layer reuse for M3/M4.
        self.list_reuse_qubit = [set() for _ in range(n)]
        self.mapping = [list(qubit_mapping[0])]
        self.registry = ResidentRegistry(architecture, qubit_mapping[0],
                                         self.theta_capacity)
        if self.experiment_schema == 2 and self.engine == "ga":
            snapshot = self._prepare_boundary_architecture()
            self.boundary_backend = select_backend(
                snapshot,
                backend=self.resident_backend_requested,
                formal=self.formal_native,
                require_registered_wheel=self.formal_native,
                expected_wheel_sha256=(
                    self.native_wheel_sha256 if self.formal_native else None),
            )
            self.boundary_backend_metrics = {
                "marshal_ns": 0,
                "search_kernel_ns": 0,
                "fitness_ns": 0,
                "selection_ns": 0,
                "native_parse_ns": 0,
                "native_serialize_ns": 0,
                "calls": 0,
                "candidates": 0,
            }
            self.backend_timing_log = []
        # Schema 2 makes ForecastOracle the sole future-information channel.
        # Keep a deliberately empty legacy helper so accidental next-use reads
        # cannot leak L+2+ even though older function signatures still accept it.
        self.nu = NextUse([] if self.experiment_schema == 2 else gate_scheduling)
        self.forecast = ForecastOracle(
            gate_scheduling if forecast_source is None else forecast_source,
            self.lookahead_horizon_config,
            alpha_lookahead=self.alpha_lookahead)
        self.residency_commitments = {}
        return n

    def _finish_terminal_boundary(self, layer: int):
        """Commit the exact terminal boundary shared by batch and Large paths."""
        terminal_ablation_return = self.ablation_policy == "always_return"
        if terminal_ablation_return:
            # Keep the registry at its true pre-move positions until ghost
            # repair has replayed every RETURN leg.  decide_lazy's legacy
            # final-return branch mutates eagerly and is therefore not used by
            # the strict Schema-2 ablation path.
            returners = sorted(self.registry.zone_seat)
            sites = match_return_sites(
                self.registry, returners, self.nu, layer,
                self.box_ratio, self.alpha_lookahead,
                forecast=self.forecast, candidate_mode="forecast")
            decisions = {q: ("RETURN", sites[q]) for q in returners}
            stats = {"stay": 0, "forced_e2": 0, "capacity": 0,
                     "return": len(returners), "participants": 0}
        else:
            decisions, stats = decide_lazy(
                self.registry, self.nu, layer, [], [],
                final_return_home=self.final_return_home)
        self._repair_ghosts([], decisions)
        if terminal_ablation_return:
            for q, (kind, loc) in decisions.items():
                if kind == "RETURN":
                    self.registry.return_to_storage(q, loc)
                elif kind == "RESEAT":
                    self.registry.reseat(q, loc)
        self._append_boundary(decisions)
        terminal_horizon = AdaptiveHorizonDecision.fixed(
            0, reason="terminal_boundary")
        self.backend_timing_log.append({
            "layer": layer,
            "backend": getattr(
                self.boundary_backend, "name", self.resident_backend_requested),
            "marshal_ns": 0,
            "search_kernel_ns": 0,
            "fitness_ns": 0,
            "selection_ns": 0,
            "native_parse_ns": 0,
            "native_serialize_ns": 0,
            "calls": 0,
            "candidates": 0,
        })
        self.decision_log.append({
            "layer": layer,
            **stats,
            **terminal_horizon.as_log(),
            "configured_lookahead_horizon": self.lookahead_horizon_config,
            "horizon_selection_ns": 0,
            "search_kernel_ns": 0,
            "ghost_fix": getattr(self, "ghost_fixes", 0),
        })

    def run(self, architecture, qubit_mapping, gate_scheduling,
            dynamic_placement, reuse_qubit):
        """轮循环总控：产出与 ZAC 同构的映射流（长度 2n+1）。

        映射流的下标约定（下游 route_qubit_mis 按 2L/2L+1 取用）：
            mapping[0]      = SA 初始布局（床位）
            mapping[2L+1]   = 第 L 轮门位映射（参与者落座，非参与者逐位不动）
            mapping[2L+2]   = 第 L 轮边界映射（RETURN 者已落到存储位）
        每轮三拍（顺序与原生编译器同构——门赢，闲人让路）：
            ① plan(L+1)     先定下一轮门位（要用到下一轮座位才能判 E2 挡路）
            ② decide_lazy   边界决策：默认全 STAY，E2/容量强制 RETURN
            ③ commit(L+1)   参与者登记入区，两张映射依序入流
        engine="ga" 时 ①② 合并成一步联合搜索（_ga_step）。
        """
        n = self._initialize_run_state(
            architecture, qubit_mapping, gate_scheduling)

        # Canonical optimisation can legitimately remove every two-qubit gate
        # (for example, ground_state_estimation_10).  Such a circuit has no
        # resident boundary to optimise; preserving the initial mapping is the
        # complete 2n+1 contract and must not probe layer zero.
        if n == 0:
            self.decision_log = []
            self._assert_contract()
            return

        placement = self._plan_round(0)
        self._repair_ghosts(placement, {})       # 第 0 轮入场也过鬼点防线
        self._commit_round(0, placement)
        for layer in range(n):
            if layer + 1 < n:
                if self.engine == "ga":
                    # A3：边界决策 + 下一轮门位联合搜索（一步 GA 两张映射一起提交）
                    if self.experiment_schema == 2:
                        self._ga_step_v2(layer)
                    else:
                        self._ga_step(layer)
                    continue
                placement = self._plan_round(layer + 1)   # 门赢：先定门位，闲人让路
                next_gates = self.gate_scheduling[layer + 1]
                next_seats = [p["seats"] for p in placement]
            else:
                # 末边界：没有下一轮门位 → 全员 STAY（native 同款），
                # 或正式消融/显式开关要求时回存储。
                self._finish_terminal_boundary(layer)
                continue
            decisions, stats = decide_lazy(
                self.registry, self.nu, layer, next_gates, next_seats)
            self._repair_ghosts(placement, decisions)   # 防线③（match 引擎同享）
            self._append_boundary(decisions)
            self.decision_log.append({"layer": layer, **stats,
                                      "ghost_fix": getattr(self, "ghost_fixes", 0)})
            self._commit_round(layer + 1, placement)
        self._assert_contract()

    # ------------------------------------------------------------------ 门位规划
    def _norm_left(self, site: tuple) -> tuple:
        """锚点归一化到该纠缠区的左 SLM（座位对约定 (s, s+1) 的前提）。"""
        eid = self.architecture.dict_SLM[site[0]].entanglement_id
        return (self.architecture.entanglement_zone[eid][0], site[1], site[2])

    def _zone_anchors(self, p1: tuple, p2: tuple) -> list:
        """一对原子的入区锚点。ZAC 的预计算表只认存储位（KeyError 教训）——
        区内原子的锚点 = 自己的座位（行粘性，原生编译器 K3 同款）。"""
        arch = self.architecture
        in_zone1 = arch.dict_SLM[p1[0]].entanglement_id != -1
        in_zone2 = arch.dict_SLM[p2[0]].entanglement_id != -1
        if not in_zone1 and not in_zone2:
            return [self._norm_left(s) for s in
                    arch.nearest_entanglement_site(p1[0], p1[1], p1[2],
                                                   p2[0], p2[1], p2[2])]
        anchors = []
        for p, in_zone in ((p1, in_zone1), (p2, in_zone2)):
            if in_zone:
                anchors.append(p)                      # 已在区内：锚=当前座位
            else:
                # 单原子版定义被 6 参版覆盖（Python 同名只留最后一个），
                # 用 6 参版传同一个位代替：same site1==site2 → 返回单锚点
                anchors.append(arch.nearest_entanglement_site(
                    p[0], p[1], p[2], p[0], p[1], p[2])[0])
        return [self._norm_left(a) for a in anchors]

    def _plan_round(self, layer: int) -> list:
        """第 layer 轮门位：菜单（按登记簿真实位置）+ ZAC 距离公式 + 最小权匹配。

        与 ZAC place_gate 的差别：无复用钉扎分支（驻留者可自由换座，其入区腿
        就是区内短移）；候选锚点带 +1 配对防御过滤。
        """
        t0 = time.time()
        list_gate = self.gate_scheduling[layer]
        reg = self.registry
        expand_factor = max(1, math.ceil(math.sqrt(len(list_gate)) / 2))
        # 硬排除（审计 FATAL-2 的两级扩展）：
        #   ① 闲住驻留者（非参与者）的座位不让门——先撞再逐会触发 E2 风暴
        #   ② 本轮其他参与者的当前座位也不让门——ZAC 的 site 账本只记"离开"
        #      不记"到达"，同相位内"B 到达 A 的座位"无法排序（A 的离开作业
        #      发射在后），ising/qft 宽层实测产生真双占；从构造上禁绝同相位
        #      座位交接（门位 = 本门原子自己的当前座位不算，那是钉扎不动）
        participants = {q for gate in list_gate for q in gate}
        candidates: list[list] = []     # 每门 [(site, w, q1, q2)]，w 为 ZAC 权重公式

        for gate in list_gate:
            q1, q2 = gate
            resident_seats = [reg.zone_seat[q] for q in (q1, q2) if reg.is_resident(q)]
            if resident_seats:
                # K2 钉扎：菜单塌缩到驻留者座位对 ± pin_radius 列——
                # 驻留者一步不走（或挪一列），新来者跑全程
                set_sites = set()
                for seat in resident_seats:
                    base = self._norm_left(seat)
                    slm = self.architecture.dict_SLM[base[0]]
                    for dc in range(-self.pin_radius, self.pin_radius + 1):
                        c = base[2] + dc
                        if 0 <= c < slm.n_c:
                            set_sites.add((base[0], base[1], c))
            else:
                set_sites = self._expanded_sites(q1, q2, list_gate)
            pin_base = self._norm_left(resident_seats[0]) if resident_seats else None
            # 本门视角的硬排除：区内所有座位，除了本门两个原子自己的
            blocked_g = {seat for q, seat in reg.zone_seat.items()
                         if q != q1 and q != q2}
            # 三级菜单：常驻域过滤 → 全区过滤（保持排除！宽敞层恒非空：
            # 280 座 − ≤98 驻留者 ≥ 42 个完整空对）→ 全区不过滤（理论兜底）
            opts = self._build_opts(set_sites, q1, q2, blocked_g, pin_base)
            if not opts:
                opts = self._build_opts(set(self._all_zone_sites()),
                                        q1, q2, blocked_g, None)
            if not opts:
                opts = self._build_opts(set(self._all_zone_sites()),
                                        q1, q2, None, None)
            candidates.append(opts)

        # 防线①：菜单预过滤（match 引擎无鸽笼约束，保底 1 个选项即可）
        ghosts0 = self._static_ghosts({q for gate in list_gate for q in gate})
        candidates = [self._filter_menu_ghosts(opts, g[0], g[1], ghosts0, 1)
                      for opts, g in zip(candidates, list_gate)]
        # 全局二部图匹配（行=工位，列=门；与 ZAC place_gate 同构）+ 安全网修复
        placement = self._repair_placements(self._match_gates(candidates, list_gate),
                                            list_gate)
        self.search_time += time.time() - t0
        return placement

    # ------------------------------------------------------------------ 菜单助手
    def _all_zone_sites(self) -> list:
        """全区左 SLM 工位全集（菜单的终极兜底搜索域）。"""
        out = []
        for zone in self.architecture.entanglement_zone:
            left = self.architecture.dict_SLM[zone[0]]
            for r in range(left.n_r):
                for c in range(left.n_c):
                    out.append((zone[0], r, c))
        return out

    def _pair_seats(self, q1: int, q2: int, site: tuple) -> tuple:
        """工位 → 座位对朝向。驻留者优先保座：配对恰含其当前座位时它不动、
        搭档去另一座（否则列序朝向会把搭档安排到驻留者旧座上，形成同门
        同相位的座位交接——site 账本表达不了，ising 实测双占）。"""
        a, b = site, (site[0] + 1, site[1], site[2])
        reg = self.registry
        for q, other, mine_first in ((q1, q2, True), (q2, q1, False)):
            if reg.is_resident(q) and reg.zone_seat[q] in (a, b):
                mine = reg.zone_seat[q]
                other_seat = b if mine == a else a
                return (mine, other_seat) if mine_first else (other_seat, mine)
        p1, p2 = reg.current_pos(q1), reg.current_pos(q2)
        if p1[2] < p2[2]:
            return a, b
        return b, a

    def _site_weight(self, q1: int, q2: int, site: tuple) -> float:
        """ZAC 权重公式（vmplacer.py:267-270）+ 驻留者折扣（解析版钉扎）。"""
        reg = self.registry
        p1, p2 = reg.current_pos(q1), reg.current_pos(q2)
        s1, s2 = self._pair_seats(q1, q2, site)
        d1 = self.architecture.distance(p1[0], p1[1], p1[2], s1[0], s1[1], s1[2])
        d2 = self.architecture.distance(p2[0], p2[1], p2[2], s2[0], s2[1], s2[2])
        r1 = self.w_resident if reg.is_resident(q1) else 1.0
        r2 = self.w_resident if reg.is_resident(q2) else 1.0
        if p1[0] == p2[0] and p1[1] == p2[1]:     # 同 SLM 同行并排走只记最远腿
            return max(math.sqrt(d1) * r1, math.sqrt(d2) * r2)
        return math.sqrt(d1) * r1 + math.sqrt(d2) * r2

    def _build_opts(self, set_sites: set, q1: int, q2: int, blocked,
                    pin_base=None) -> list:
        """把候选工位集做成 (site, w, q1, q2) 菜单；blocked 非空时硬排除；
        pin_base 非空时（钉扎菜单）加列偏移罚——座位基本钉在驻留者列上。"""
        reg = self.registry
        p1, p2 = reg.current_pos(q1), reg.current_pos(q2)
        opts = []
        for site in set_sites:
            if p1[2] < p2[2]:
                s1, s2 = site, (site[0] + 1, site[1], site[2])
            else:
                s1, s2 = (site[0] + 1, site[1], site[2]), site
            if blocked and (s1 in blocked or s2 in blocked):
                continue           # 非参与者驻留者的座位：硬排除
            w = self._site_weight(q1, q2, site)
            if pin_base is not None:
                w += self.w_pin * abs(site[2] - pin_base[2])
            opts.append((site, w, q1, q2))
        opts.sort(key=lambda o: o[1])
        return opts

    def _expanded_sites(self, q1: int, q2: int, list_gate: list) -> set:
        """常规锚点展开（含 ZAC 容量自动扩窗）——非钉扎门的菜单全集。"""
        reg = self.registry
        p1, p2 = reg.current_pos(q1), reg.current_pos(q2)
        expand_factor = max(1, math.ceil(math.sqrt(len(list_gate)) / 2))
        set_sites = set()
        for near in self._zone_anchors(p1, p2):
            slm = self.architecture.dict_SLM[near[0]]
            # +1 配对防御：锚点 SLM 必须有同区搭档（实测两套架构恒满足）
            partner = self.architecture.dict_SLM.get(near[0] + 1)
            if partner is None or partner.entanglement_id != slm.entanglement_id:
                continue
            set_sites.add(near)
            low_r = max(0, near[1] - expand_factor)
            high_r = min(slm.n_r, near[1] + expand_factor + 1)
            low_c = max(0, near[2] - expand_factor)
            high_c = min(slm.n_c, near[2] + expand_factor + 1)
            # ZAC 容量自动扩窗（vmplacer.py:235-242 原样移植，缺它会 no full matching）
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
        return set_sites

    def _match_gates(self, candidates: list, list_gate: list) -> list:
        """最小权完美匹配选门位；病态层退化为逐门贪心顺延（不崩整个编译）。"""
        site_to_row: dict = {}
        rows_list: list = []
        rows, cols, data = [], [], []
        for col, opts in enumerate(candidates):
            for site, w, q1, q2 in opts:
                if site not in site_to_row:
                    site_to_row[site] = len(rows_list)
                    rows_list.append(site)
                rows.append(site_to_row[site])
                cols.append(col)
                # scipy represents the cost matrix sparsely and discards
                # explicit zero entries.  A genuinely zero-distance gate/site
                # edge is still a legal (and usually best) matching edge, so
                # retain it with the smallest positive float instead of
                # silently deleting it from the bipartite graph.
                data.append(max(float(w), np.nextafter(0.0, 1.0)))
        n_rows, n_cols = len(rows_list), len(candidates)

        def greedy():
            """兜底：逐门按权重顺延取未占位；菜单耗尽时全量展开再取。
            （曾在此翻车：耗尽时硬拿 opts[0] 会座位双订——流契约断言逮住过）"""
            used = set()
            chosen = {}
            for col, opts in enumerate(candidates):
                site = next((o[0] for o in opts if o[0] not in used), None)
                if site is None:
                    q1, q2 = opts[0][2], opts[0][3]
                    big = sorted(self._expanded_sites(q1, q2, list_gate))
                    site = next(s for s in big if s not in used)
                used.add(site)
                chosen[col] = site
            return chosen

        try:
            matrix = coo_matrix((np.array(data), (np.array(rows), np.array(cols))),
                                shape=(n_rows, n_cols))
            row_ind, col_ind = min_weight_full_bipartite_matching(matrix)
            chosen = {c: rows_list[r] for r, c in zip(row_ind, col_ind)}
            # scipy 的"full"只保证小侧全覆盖：钉扎菜单太窄时 sites < gates，
            # 可能有门未被匹配——验证全覆盖，否则落入贪心兜底
            if len(chosen) == n_cols:
                return [self._mk_placement(opts[0][2], opts[0][3], chosen[col])
                        for col, opts in enumerate(candidates)]
        except ValueError:
            pass
        chosen = greedy()
        return [self._mk_placement(candidates[col][0][2], candidates[col][0][3],
                                   chosen[col])
                for col in range(len(candidates))]

    def _mk_placement(self, q1: int, q2: int, site: tuple) -> dict:
        """由门 + 选定工位落座位对（驻留者保座 + 列序朝向，与权重/缓存同一规则）。"""
        s1, s2 = self._pair_seats(q1, q2, site)
        return {"gate": (q1, q2), "site": site, "seats": (s1, s2)}

    def _repair_placements(self, placements: list, list_gate: list,
                           vacated: set[int] | None = None) -> list:
        """最终安全网：门位不得与其他原子当前座位或其他门已选工位重叠。

        菜单三级兜底在极端拥挤轮次仍可能放过个别工位（qft 宽层实证：
        原子 6 落到参与者原子 5 的座位上，E2 时序救不回来）。这里在选择
        完成后按不变式逐门复查、违例即改选全区过滤域最优工位——正确性
        不再依赖任何菜单层级的表现。
        """
        reg = self.registry
        vacated = set() if vacated is None else set(vacated)
        participants = {q for gate in list_gate for q in gate}
        used = {p["site"] for p in placements}
        out = []
        for p in placements:
            q1, q2 = p["gate"]
            own = {reg.zone_seat.get(q) for q in (q1, q2)}    # 自己的座位可保留
            others = {seat for q, seat in reg.zone_seat.items()
                      if q != q1 and q != q2 and q not in vacated}
            s1, s2 = p["seats"]
            if s1 not in others and s2 not in others and p["site"] not in (used - {p["site"]}):
                out.append(p)
                continue
            # 违例：全区过滤域里挑最优未用工位（others 已排除本门原子自己的座位）
            best, best_w = None, float("inf")
            for site in self._all_zone_sites():
                if site in used:
                    continue
                a, b = site, (site[0] + 1, site[1], site[2])
                if a in others or b in others:
                    continue
                w = self._site_weight(q1, q2, site)
                if w < best_w:
                    best, best_w = site, w
            if best is not None:
                used.discard(p["site"])
                used.add(best)
                out.append(self._mk_placement(q1, q2, best))
            else:
                out.append(p)      # 理论不可达（容量余量恒足）
        return out

    # ============================================================== 鬼点硬保证层
    # 三道防线（与 engine/fitness 完全无关——无前瞻的 GA 也零鬼点）：
    #   ① 菜单预过滤 _filter_menu_ghosts：入区腿自查不撞当前静态原子
    #   ② 解码增量检查（_ga_step.decode 内）：新座位与已落座者组合零新增
    #   ③ _repair_ghosts：提交前确定性修补——本层是最终保证
    # 联合口径定理（ghost.py 模块头）：按相位全腿同批检查零鬼点 ⇒ 路由
    # 着色任意分批后仍零鬼点。修补只会改写 decisions（RESEAT 让座 / 换
    # RETURN 落位）或个别门位，改写后照常走 _append_boundary/_commit_round，
    # 路由端按映射增量自动带上，无特殊处理。

    def _static_ghosts(self, participants):
        """本轮静态原子 [(q, x, y)]：非参与者在当前位置。

        保守口径：预过滤/解码时还不知道 RETURN 决策，驻留者可能本步
        回撤（届时它不再是鬼）——按当前位置滤只会多滤不会漏。精确口径
        在 _repair_ghosts 里按相位分别构建。
        """
        arch = self.architecture
        return [(q, *arch.exact_SLM_location_tuple(self.registry.current_pos(q)))
                for q in range(len(self.mapping[0])) if q not in participants]

    def _filter_menu_ghosts(self, opts, q1, q2, ghosts, keep):
        """防线①：剔除"自己的入区腿就撞鬼"的选项。

        keep = 菜单必须保住的最少选项数（GA 食堂顺延的鸽笼不变式）；
        干净选项不足时按命中数升序回填脏选项——绝不空菜单、绝不少于 keep。
        """
        if not ghosts or not opts:
            return opts
        reg, arch = self.registry, self.architecture
        scored = []
        for o in opts:
            s1, s2 = self._pair_seats(q1, q2, o[0])
            legs = []
            for q, s in ((q1, s1), (q2, s2)):
                p0 = arch.exact_SLM_location_tuple(reg.current_pos(q))
                p1 = arch.exact_SLM_location_tuple(s)
                d = math.dist(p0, p1)
                if d > 1e-9:
                    legs.append((d, *p0, *p1))
            scored.append((hit_count(legs, ghosts), o))
        clean = [o for g, o in scored if g == 0]
        if len(clean) >= keep:
            return clean
        dirty = sorted((g, o) for g, o in scored if g > 0)
        return clean + [o for _, o in dirty[:max(0, keep - len(clean))]]

    def _free_zone_seats(self, exclude):
        """空闲激发区单座全集（RESEAT 让座目标域；含左右两种 SLM）。"""
        reg = self.registry
        taken = set(reg.zone_seat.values()) | set(exclude)
        seats = []
        for s in self._all_zone_sites():
            for cand in (s, (s[0] + 1, s[1], s[2])):
                if cand not in taken:
                    seats.append(cand)
        return seats

    def _free_storage_sites(self, exclude):
        """空闲存储位全集（RETURN 落位重选域；SLM 0 全枚举）。"""
        slm = self.architecture.dict_SLM[0]
        taken = set(self.registry.storage_site.values()) | set(exclude)
        return [(0, r, c) for r in range(slm.n_r) for c in range(slm.n_c)
                if (0, r, c) not in taken]

    def _repair_ghosts(self, placements, decisions, pinned_seats=None):
        """防线③：每条腿【单独】不得撞任何静止原子（批化解不了的部分）。

        M2 架构分工：单腿自撞（腿自己的列×行交叉扫到别人）任何分批都
        化解不了——本层在放置期根除；两腿组合撞鬼由路由层的鬼点边强制
        分批（zac_zzx._coloring_batches → ghost.pair_edges）。

        时间线：T0(现在) --back--> T1 --out--> T2。
          back 腿的鬼 = 其余原子在 {T0, T1} 位置（它是别的批的搬运者时
                        飞前坐 T0、飞完坐 T1，两个位置都可能被扫）；
          out  腿的鬼 = 其余原子在 {T1, T2} 位置。
        修补只改写 decisions（RESEAT 让座 / 换 RETURN 落位）或个别门位；
        帽 60 轮，修不净即 raise——硬保证语义：宁可大声失败不静默放走。
        """
        reg, arch = self.registry, self.architecture
        pinned_seats = {
            int(q): tuple(seat) for q, seat in (pinned_seats or {}).items()}
        n_q = len(self.mapping[0])
        ex = lambda loc: arch.exact_SLM_location_tuple(tuple(loc))
        participants = {q for p in placements for q in p["gate"]}

        def t1(q):
            return decisions[q][1] if q in decisions else reg.current_pos(q)

        def t2(q):
            if q in participants:
                for p in placements:
                    if q in p["gate"]:
                        return p["seats"][p["gate"].index(q)]
            return t1(q)

        def both(q, ta, tb):
            """原子在相位前/后两个可能位置（相同则只列一次）。"""
            a, b = ex(ta(q)), ex(tb(q))
            out = [(q, *a)]
            if b != a:
                out.append((q, *b))
            return out

        def gate_of(q):
            for i, p in enumerate(placements):
                if q in p["gate"]:
                    return i
            return None

        def leg_issues():
            """全部单腿自撞问题 [(相位, 腿主q, leg, 受害者id, x, y)]。"""
            issues = []
            for q, v in decisions.items():
                if v[0] == "STAY":
                    continue
                p0, p1 = ex(reg.current_pos(q)), ex(v[1])
                d = math.dist(p0, p1)
                if d < 1e-9:
                    continue
                ghosts = [g for a in range(n_q) if a != q
                          for g in both(a, reg.current_pos, t1)]
                for gid, gx, gy in leg_hits((d, *p0, *p1), ghosts):
                    issues.append(("back", q, (d, *p0, *p1), gid, gx, gy))
            for p in placements:
                for q, s in zip(p["gate"], p["seats"]):
                    p0, p1 = ex(t1(q)), ex(s)
                    d = math.dist(p0, p1)
                    if d < 1e-9:
                        continue
                    ghosts = [g for a in range(n_q) if a != q
                              for g in both(a, t1, t2)]
                    for gid, gx, gy in leg_hits((d, *p0, *p1), ghosts):
                        issues.append(("out", q, (d, *p0, *p1), gid, gx, gy))
            return issues

        fixed_total = 0
        self.ghost_fixes = 0
        banned = {}          # 禁回表：("gate",i)/("dec",q)/("seat",q) → 用过的座位
        for _round in range(12):            # 禁回 ⇒ 状态不重复 ⇒ 无振荡，必收敛
            issues = leg_issues()
            if not issues:
                self.ghost_fixes = fixed_total
                return
            progress = False
            for issue in list(issues):      # 一轮修完所有问题（修法内部
                if self._fix_leg_ghost(     # 按实时状态校验，过期问题天然
                        issue, placements, decisions,   # 无害——条件不满足即跳过）
                        participants, t1, t2, both, gate_of, n_q, banned,
                        pinned_seats):
                    fixed_total += 1
                    progress = True
            if not progress:
                break
        residual = leg_issues()
        raise RuntimeError(
            f"鬼点硬保证层修补失败：{len(residual)} 处单腿自撞残留 "
            f"(首批: {residual[:3]})——请检查修补策略覆盖度")

    def _repair_ghosts_with_commitments(
            self, placements, decisions, pinned_seats):
        """Repair ghosts transactionally, treating residency pins as preferred.

        A pin records that an atom physically stayed in the zone until this
        reuse layer.  Hard ghost safety has higher priority than keeping that
        atom on the exact same gate seat: when no pin-preserving straight-leg
        schedule exists, retry from the untouched pre-repair state without the
        seat restriction.  The caller re-scores the actual repaired schedule,
        so the resulting zone-to-zone move and transfers are never hidden.
        """
        pins = {int(q): tuple(seat) for q, seat in pinned_seats.items()}

        def broken_pins():
            observed = {}
            for placement in placements:
                for q, seat in zip(placement["gate"], placement["seats"]):
                    if q in pins and tuple(seat) != pins[q]:
                        observed[q] = tuple(seat)
            return observed

        pristine_placements = deepcopy(placements)
        pristine_decisions = deepcopy(decisions)
        if not broken_pins():
            try:
                self._repair_ghosts(
                    placements, decisions, pinned_seats=pins)
                return {}, False
            except RuntimeError:
                placements[:] = deepcopy(pristine_placements)
                decisions.clear()
                decisions.update(deepcopy(pristine_decisions))

        # Either the occupancy repair had already displaced a pin or the
        # pin-preserving ghost repair proved infeasible.  Retry once from the
        # pristine state with the common hard-safety repair.  Any failure here
        # still propagates and the attempt remains fail-closed.
        self._repair_ghosts(placements, decisions)
        return broken_pins(), True

    def _fix_leg_ghost(self, issue, placements, decisions, participants,
                       t1, t2, both, gate_of, n_q, banned, pinned_seats=None):
        """修一个单腿自撞问题，返回是否动手。按代价从小到大：

        ① 受害者是驻留非参与者(STAY) → 让座 RESEAT（恒可解兜底）
        ② 腿主是决策(RETURN/RESEAT) → 换落位
        ③ 腿主是参与者 → 改门位
        """
        phase, owner, leg, gid, gx, gy = issue
        reg, arch = self.registry, self.architecture
        pinned_seats = pinned_seats or {}
        ex = lambda loc: arch.exact_SLM_location_tuple(tuple(loc))

        # ① 受害者让座：新座不得被任何腿扫到，让座腿自身也要单腿干净
        if gid not in participants and reg.is_resident(gid) and \
                decisions.get(gid, ("STAY",))[0] == "STAY":
            old = reg.zone_seat[gid]
            exclude = {v[1] for v in decisions.values() if v[0] == "RESEAT"}
            exclude |= {s for p in placements for s in p["seats"]}
            all_legs = [l for q, v in decisions.items() if v[0] != "STAY"
                        for l in [ (lambda a, b: (math.dist(a, b), *a, *b)
                                    )(ex(reg.current_pos(q)), ex(v[1])) ]
                        if l[0] > 1e-9]
            for p in placements:
                for q, s in zip(p["gate"], p["seats"]):
                    a, b = ex(t1(q)), ex(s)
                    if math.dist(a, b) > 1e-9:
                        all_legs.append((math.dist(a, b), *a, *b))
            others = [g for a in range(n_q) if a != gid
                      for g in both(a, reg.current_pos, t1)]
            ban = banned.setdefault(("seat", gid), set())
            for site in sorted(self._free_zone_seats(exclude),
                               key=lambda s: arch.distance(
                                   old[0], old[1], old[2], *s)):
                if site in ban:
                    continue
                t0 = ex(site)
                d = math.dist(ex(old), t0)
                if d < 1e-9 or ghost_hits(all_legs, [(gid, *t0)]):
                    continue
                if leg_hits((d, *ex(old), *t0), others):
                    continue
                decisions[gid] = ("RESEAT", site)
                ban.add(old)
                return True

        # ② 腿主是决策 → 换落位（RETURN 换存储位，RESEAT 换区座位）
        if owner in decisions and decisions[owner][0] in ("RETURN", "RESEAT"):
            kind = decisions[owner][0]
            sx, sy = ex(reg.current_pos(owner))
            taken = {v[1] for v in decisions.values() if v[0] == kind}
            taken |= {s for p in placements for s in p["seats"]}
            pool = (self._free_storage_sites(taken) if kind == "RETURN"
                    else self._free_zone_seats(taken))
            ghosts = [g for a in range(n_q) if a != owner
                      for g in both(a,
                                    (reg.current_pos if phase == "back" else t1),
                                    (t1 if phase == "back" else t2))]
            out_legs = []
            for p in placements:
                for q, s in zip(p["gate"], p["seats"]):
                    a, b = ex(t1(q)), ex(s)
                    if math.dist(a, b) > 1e-9:
                        out_legs.append((math.dist(a, b), *a, *b))
            ban = banned.setdefault(("dec", owner), set())
            for site in sorted(pool, key=lambda candidate: math.dist(
                    (sx, sy), ex(candidate))):
                if site in ban:
                    continue
                t0 = ex(site)
                d = math.dist((sx, sy), t0)
                if d < 1e-9:
                    continue
                if leg_hits((d, sx, sy, t0[0], t0[1]), ghosts):
                    continue
                if phase == "back" and ghost_hits(out_legs, [(owner, *t0)]):
                    continue      # 新落位在 out 相也不得被扫
                decisions[owner] = (kind, site)
                ban.add(site)
                return True

        # ③ 腿主是参与者 → 改门位（全区按权重搜索替位）；
        # ④ 受害者是【坐定参与者】（零腿坐在门位）→ 改它的门位
        #    （③的同一套搜索，只是目标门换成受害者的门）
        gi = gate_of(owner)
        if gi is None and gid in participants:
            # 受害者坐定 = 其入区腿为零：改它的门位等于把它挪开
            for p in placements:
                if gid in p["gate"]:
                    others = [q for q, s in zip(p["gate"], p["seats"])
                              if math.dist(ex(t1(q)), ex(s)) < 1e-9]
                    if others:
                        gi = gate_of(gid)
                        owner = gid
                    break
        if gi is not None:
            p = placements[gi]
            q1, q2 = p["gate"]
            vacated = {q for q, value in decisions.items()
                       if value[0] in ("RETURN", "RESEAT")}
            others_seats = {seat for qq, seat in reg.zone_seat.items()
                            if qq != q1 and qq != q2 and qq not in vacated}
            used = {pp["site"] for pp in placements}
            ghosts = [g for a in range(n_q) if a not in (q1, q2)
                      for g in both(a, t1, t2)]
            ban = banned.setdefault(("gate", gi), set())
            cand_sites = [s for s in sorted(self._all_zone_sites(),
                               key=lambda s: self._site_weight(q1, q2, s))
                          if s not in used and s not in ban
                          and s not in others_seats
                          and (s[0] + 1, s[1], s[2]) not in others_seats]
            for site in cand_sites:
                new_p = self._mk_placement(q1, q2, site)
                if any(
                        q in pinned_seats
                        and tuple(seat) != tuple(pinned_seats[q])
                        for q, seat in zip(new_p["gate"], new_p["seats"])):
                    continue
                new_legs = []
                clean = True
                for q, s in zip(new_p["gate"], new_p["seats"]):
                    pa, pb = ex(t1(q)), ex(s)
                    d = math.dist(pa, pb)
                    if d > 1e-9:
                        nl = (d, *pa, *pb)
                        if leg_hits(nl, ghosts):
                            clean = False
                            break
                        new_legs.append(nl)
                if not clean:
                    continue
                placements[gi] = new_p
                ban.add(p["site"])
                return True
            import os
            if os.environ.get("GHOST_DEBUG"):
                print(f"[fixdbg] ③失败 owner=q{owner} victim=q{gid} "
                      f"尝试站点数={len(cand_sites)} 全排除/全脏")
        import os
        if os.environ.get("GHOST_DEBUG"):
            print(f"[fixdbg] 无修法适用 phase={phase} owner=q{owner} victim=q{gid} "
                  f"victim是参与者={gid in participants} "
                  f"victim是驻留={self.registry.is_resident(gid)} "
                  f"owner决策={owner in decisions}")
        return False


    def _commit_round(self, layer: int, placement: list):
        """追加第 layer 轮门位映射（= 上一张映射 + 参与者落座）；登记簿入区。"""
        m = list(self.mapping[-1])
        for p in placement:
            q1, q2 = p["gate"]
            s1, s2 = p["seats"]
            m[q1], m[q2] = s1, s2
            self.registry.enter_zone(q1, s1)
            self.registry.enter_zone(q2, s2)
        self.mapping.append(m)

    def _append_boundary(self, decisions: dict):
        """追加边界映射（= 门位映射 + RETURN 者落存储位 / RESEAT 者落新区座）。

        路由端按映射增量取 back 相搬运者（zac_zzx._route_resident 扫全部
        原子的 gate→final 差分），RESEAT 腿因此自动上车，无需特判。
        """
        m = list(self.mapping[-1])
        for q, (kind, loc) in decisions.items():
            if kind in ("RETURN", "RESEAT"):
                m[q] = loc
        self.mapping.append(m)

    # ------------------------------------------------------------------ 流契约
    def _assert_contract(self):
        """审计 MAJOR-4：长度/锚定/拷贝不变式/单射，违例即刻爆炸（不静默）。"""
        n = len(self.gate_scheduling)
        assert len(self.mapping) == 2 * n + 1, \
            f"映射流长度 {len(self.mapping)} ≠ 2×{n}+1"
        participants = [set(q for gate in gates for q in gate)
                        for gates in self.gate_scheduling]
        for L in range(n):
            for q in range(len(self.mapping[0])):
                if q not in participants[L]:
                    assert self.mapping[2 * L + 1][q] == self.mapping[2 * L][q], \
                        f"拷贝不变式破坏：layer {L} 非参与者 {q} 被移动"
        for i, m in enumerate(self.mapping):
            seats = [tuple(s) for s in m]
            assert len(seats) == len(set(seats)), f"映射 {i} 非单射（座位双订）"

    # ============================================================== GA 决策层（A3）
    def _ga_step(self, layer: int):
        """一步联合搜索：边界 L 决策（STAY/RETURN）∪ 轮 L+1 门位。

        染色体 = [每门菜单下标] ++ [有后续使用的非参与者 0=STAY/1=RETURN]；
        适应度 = 分相位着色（back=RETURN 腿 / out=入区腿，各自 DSATUR）
                 + 下次使用锚点前瞻（γ0 按轮次折现）。
        A1 的消融数据（钉 vs 融合各赢一半电路）说明门位取舍必须逐门搜索——
        这里菜单同时含钉扎窗与全展开集，GA 用同一本批数+距离账定夺。
        菜单/锚点/RETURN 落位每步只算一次（常量缓存），fitness 只剩查表
        + 两次小图 DSATUR。
        """
        t0 = time.time()
        next_layer = layer + 1
        list_gate = self.gate_scheduling[next_layer]
        reg, nu, arch = self.registry, self.nu, self.architecture
        participants = {q for gate in list_gate for q in gate}
        static_ghosts = self._static_ghosts(participants)   # 防线①②共用的静态鬼

        # ---- 菜单：钉扎窗 ∪ 全展开；硬排除=区内所有座位除本门原子自己的
        #     （同相位座位交接从构造上禁绝——site 账本只记离开不记到达）----
        candidates: list[list] = []
        gate_cache: list[dict] = []      # (col, site) → {legs, anchor}
        for q1, q2 in list_gate:
            resident_seats = [reg.zone_seat[q] for q in (q1, q2)
                              if reg.is_resident(q)]
            set_sites = self._expanded_sites(q1, q2, list_gate)
            if resident_seats:
                for seat in resident_seats:
                    base = self._norm_left(seat)
                    slm = arch.dict_SLM[base[0]]
                    for dc in range(-self.pin_radius, self.pin_radius + 1):
                        c = base[2] + dc
                        if 0 <= c < slm.n_c:
                            set_sites.add((base[0], base[1], c))
            blocked_g = {seat for q, seat in reg.zone_seat.items()
                         if q != q1 and q != q2}
            opts = self._build_opts(set_sites, q1, q2, blocked_g)
            if not opts:
                opts = self._build_opts(set(self._all_zone_sites()),
                                        q1, q2, blocked_g)
            assert opts, f"layer {next_layer} 门 ({q1},{q2}) 菜单为空"
            # 预扩容：菜单至少"门数"个选项——食堂顺延在被占满的菜单上会
            # 原地打转（ising 宽层钉扎窗挤满时两门同 site 的实证）；
            # 扩容源同样保持硬排除（全区过滤域）
            if len(opts) < len(list_gate):
                seen = {o[0] for o in opts}
                extra = sorted((set(self._all_zone_sites())
                                | self._expanded_sites(q1, q2, list_gate)) - seen,
                               key=lambda s: self._site_weight(q1, q2, s))
                for site in extra:
                    if any(seat in blocked_g for seat in
                           (site, (site[0] + 1, site[1], site[2]))):
                        continue
                    opts.append((site, self._site_weight(q1, q2, site), q1, q2))
                    if len(opts) >= len(list_gate):
                        break
                opts.sort(key=lambda o: o[1])
            # 防线①：入区腿自查撞鬼的选项剔除（保住鸽笼下限 len(list_gate)）
            opts = self._filter_menu_ghosts(opts, q1, q2, static_ghosts,
                                            len(list_gate))
            candidates.append(opts)
            # 每工位缓存：两原子的入区腿 + 坐定者（前瞻 v2：锚点项已删——
            # nolook 对照实证其亏 1%，且"投影到存储位"是回撤世界的遗产假设）
            cache = {}
            for site, _, _, _ in opts:
                s1, s2 = self._pair_seats(q1, q2, site)
                legs = []
                seated = []        # 已坐定参与者（零腿）——解码时成为后续门的鬼
                for q, s in ((q1, s1), (q2, s2)):
                    sx, sy = arch.exact_SLM_location_tuple(reg.current_pos(q))
                    tx, ty = arch.exact_SLM_location_tuple(s)
                    d = math.dist((sx, sy), (tx, ty))
                    if d > 1e-9:
                        legs.append((d, sx, sy, tx, ty))
                    else:
                        seated.append((q, tx, ty))
                cache[site] = {"legs": legs, "seated": seated}
            gate_cache.append(cache)

        # ---- 决策基因与 RETURN 落位常量 ----
        eligible = sorted(q for q in reg.zone_seat
                          if q not in participants and nu.has_future_use(q, layer))
        return_sites = (match_return_sites(reg, eligible, nu, layer,
                                           self.box_ratio, self.alpha_lookahead)
                        if eligible else {})
        dec_cache: dict = {}             # q → {0: STAY(无腿), 1: RETURN 腿}
        for q in eligible:
            seat = reg.zone_seat[q]
            sx, sy = arch.exact_SLM_location_tuple(seat)
            site = return_sites.get(q)
            tx, ty = arch.exact_SLM_location_tuple(site) if site else (None, None)
            ret_leg = (math.dist((sx, sy), (tx, ty)), sx, sy, tx, ty) if site else None
            dec_cache[q] = {0: [], 1: [ret_leg] if ret_leg else []}

        n_gates = len(candidates)
        n_genes = n_gates + len(eligible)

        # ---- 解码：食堂顺延（被占/撞鬼就沿菜单找下一个空位）----
        # 防线②：新座位的入区腿与已落座者的腿做增量鬼点检查，冲突则顺延；
        # 菜单耗尽时退回纯占位检查（第三道防线 _repair_ghosts 兜底）。
        def decode(chrom):
            used = set()
            placed = []
            acc_legs = []
            acc_ghosts = list(static_ghosts)
            for col, opts in enumerate(candidates):
                idx = chrom[col] % len(opts)
                cand = None
                for step in range(len(opts)):
                    c = opts[(idx + step) % len(opts)]
                    if c[0] in used:
                        continue
                    if new_conflicts(acc_legs, gate_cache[col][c[0]]["legs"],
                                     acc_ghosts):
                        continue
                    cand = c
                    break
                if cand is None:
                    for step in range(len(opts)):
                        c = opts[(idx + step) % len(opts)]
                        if c[0] not in used:
                            cand = c
                            break
                used.add(cand[0])
                placed.append(cand)
                e = gate_cache[col][cand[0]]
                acc_legs.extend(e["legs"])
                acc_ghosts.extend(e["seated"])   # 坐定即成鬼
            return placed

        # ---- 适应度：分相位着色 + 锚点前瞻（查表 + 两次 DSATUR）----
        # ---- 软层（前瞻 v2）：鬼点罚（思路二）+ 顺序罚（思路一），缓存 ----
        # 鬼点代理 = 本窗两相"同批组合会撞静止原子"的腿对数——每对在路由
        # 重放审计里≈一次被迫分批，与主项同单位（批）；T0 静止位近似，
        # 精确性由硬保证层兜底。顺序罚 = 未来层存储搭档的进场行碎片化
        # （同起点行必同终点行：同批进场者必须落同一行，行数=批数下界）。
        soft_cache: dict = {}

        def order_penalty(placed):
            """未来 1-2 层还在宿舍的搭档对数 vs 各行自由对位容量的碎片化。

            罚 = Σ_未来层 γ^批距 × (装下该层宿舍搭档所需行数 − 1)。
            当前方案的座位占用决定每行剩多少完整空对——占得碎，行数就多。
            """
            occupied = set(reg.zone_seat.values())
            for c in placed:
                s1, s2 = self._pair_seats(c[2], c[3], c[0])
                occupied.update((s1, s2))
            total = 0.0
            for dl in (1, 2):
                Lf = next_layer + dl
                if Lf >= len(self.gate_scheduling):
                    break
                m = sum(1 for g in self.gate_scheduling[Lf]
                        if not reg.is_resident(g[0]) and not reg.is_resident(g[1]))
                if m == 0:
                    continue
                cap = {}
                for s in self._all_zone_sites():
                    if s not in occupied and \
                            (s[0] + 1, s[1], s[2]) not in occupied:
                        cap[s[1]] = cap.get(s[1], 0) + 1
                used, need = 0, m
                for c in sorted(cap.values(), reverse=True):
                    need -= c
                    used += 1
                    if need <= 0:
                        break
                if need > 0:
                    used = m                 # 容量不足极端档：按每对一行计
                total += (self.gamma_batch ** dl) * max(0, used - 1)
            return total

        def soft_penalty(placed, chrom):
            key = (tuple(c[0] for c in placed),
                   tuple(chrom[n_gates + i] for i in range(len(eligible))))
            if key in soft_cache:
                return soft_cache[key]
            ghosts_t0 = list(static_ghosts)
            for col, cand in enumerate(placed):
                ghosts_t0.extend(gate_cache[col][cand[0]]["seated"])
            legs_out = []
            for col, cand in enumerate(placed):
                legs_out.extend(gate_cache[col][cand[0]]["legs"])
            # owners 与腿一一对齐（gate_cache 腿按 (q1,q2) 顺序跳过零腿）
            owners_o = []
            for col, cand in enumerate(placed):
                e = gate_cache[col][cand[0]]
                q1, q2 = cand[2], cand[3]
                s1, s2 = self._pair_seats(q1, q2, cand[0])
                for q, s in ((q1, s1), (q2, s2)):
                    if math.dist(arch.exact_SLM_location_tuple(reg.current_pos(q)),
                                 arch.exact_SLM_location_tuple(s)) > 1e-9:
                        owners_o.append(q)
            legs_back, owners_b = [], []
            for i, q in enumerate(eligible):
                legs_back.extend(dec_cache[q][chrom[n_gates + i]])
                owners_b.extend([q] * len(dec_cache[q][chrom[n_gates + i]]))
            g = 0
            if legs_out:
                g += len(pair_edges(legs_out, ghosts_t0, owners=owners_o))
            if legs_back:
                g += len(pair_edges(legs_back, ghosts_t0, owners=owners_b))
            r = order_penalty(placed)
            val = self.w_ghost * g + self.w_ord * r
            soft_cache[key] = val
            return val

        def fitness(chrom):
            placed = decode(chrom)
            legs_out = []
            for col, cand in enumerate(placed):
                legs_out.extend(gate_cache[col][cand[0]]["legs"])
            legs_back = []
            for i, q in enumerate(eligible):
                legs_back.extend(dec_cache[q][chrom[n_gates + i]])
            if self.fitness_mode == "lumped":      # A3' 消融档
                cost = batch_cost(legs_back + legs_out, w_batch=self.w_batch)[0] \
                    if legs_back or legs_out else 0.0
                return cost + soft_penalty(placed, chrom)
            cost_b = batch_cost(legs_back, w_batch=self.w_batch)[0] if legs_back else 0.0
            cost_o = batch_cost(legs_out, w_batch=self.w_batch)[0] if legs_out else 0.0
            return cost_b + cost_o + soft_penalty(placed, chrom)

        # ---- 类型感知邻域：门位基因 ±1/重抽；决策基因翻转 ----
        # 异质染色体不能共用一套算子（审计 F3：原 swap 算子跨类型几乎全空转，
        # 浪费一半邻域预算）——按基因类型各用各的：
        #   门位基因（菜单下标）：±1 走相邻菜（微调）或随机重抽（跳出局部）
        #   决策基因（0/1）：直接翻转 STAY↔RETURN
        def neighbor(chrom):
            m = list(chrom)
            if n_gates and (not eligible or self.rng.random() < 0.5):
                gi = self.rng.randrange(n_gates)
                m[gi] = self.rng.choice([m[gi] + 1, m[gi] - 1,
                                         self.rng.randrange(len(candidates[gi]))])
            else:
                di = self.rng.randrange(len(eligible))
                m[n_gates + di] ^= 1
            return m

        # ---- 进化主循环（ZAC_new 引擎 A 骨架：精英保留 + 邻域采样）----
        # zeros 热启动 = 每门选菜单第一项（权重最优的解析解）+ 全体 STAY
        # ——GA 从不比解析差，这是 A1→A3 只升不降的原因。
        # 每轮每条父本采 neighbor_sample_size 个邻居，取前 neighbors_per_solution
        # 入子代，合并精英截断到 population_size。默认预算 6×8×24 ≈ 1158 次评估。
        zeros = [0] * n_genes
        population = [zeros]
        for _ in range(self.population_size - 1):
            population.append([self.rng.randrange(max(1, len(candidates[i]) if i < n_gates else 2))
                               for i in range(n_genes)])
        scored = sorted((fitness(c), c) for c in population)
        for _ in range(self.iterations):
            offspring = []
            for _, c in scored:
                pool = sorted((fitness(m), m)
                              for m in (neighbor(c)
                                        for _ in range(self.neighbor_sample_size)))
                offspring += pool[: self.neighbors_per_solution]
            scored = sorted(scored + offspring)[: self.population_size]

        # ---- 应用最优解：座位对、边界决策、容量阀兜底、提交 ----
        best_c = scored[0][1]
        placed = decode(best_c)
        # 终检安全网照跑：GA 的菜单虽已硬排除，但 decode 顺延在极端拥挤层
        # 仍可能撞座（与 A1 同一不变式，宁可多一道）
        placements = self._repair_placements(
            [self._mk_placement(c[2], c[3], c[0]) for c in placed], list_gate)

        # 按决策基因生成本边界的决策表（GA 模式下菜单已排除他人座位，
        # 所以理论上不会出现 E2 挡路——forced_e2 恒记 0）
        decisions = {}
        for i, q in enumerate(eligible):
            if best_c[n_gates + i]:
                decisions[q] = ("RETURN", return_sites[q])   # 落位用每步一次匹配的常量
            else:
                decisions[q] = ("STAY", reg.zone_seat[q])

        # 容量阀：保留座位 + 2×门数超压时按下次使用降序强制 RETURN（死驻留者最先）
        # （与 decide_lazy 同一规则——GA 只优化"要不要回"，硬约束仍由阀兜底）
        retained = sum(1 for v in decisions.values() if v[0] == "STAY") \
            + sum(1 for q, seat in reg.zone_seat.items()
                  if q not in participants and q not in decisions)
        demand = 2 * len(list_gate)
        cap_evict = []
        by_next_use = sorted(
            (q for q, v in decisions.items() if v[0] == "STAY"),
            key=lambda q: (nu.next_round(q, layer) is None,
                           nu.next_round(q, layer) or 0), reverse=True)
        for q in by_next_use:
            if retained + demand <= self.theta_capacity * reg.zone_sites:
                break
            cap_evict.append(q)
            retained -= 1
        forced_sites = (match_return_sites(reg, cap_evict, nu, layer,
                                           self.box_ratio, self.alpha_lookahead)
                        if cap_evict else {})
        for q, site in forced_sites.items():
            decisions[q] = ("RETURN", site)

        # 防线③：鬼点硬保证——确定性修补（改 decisions/门位），修不净即 raise
        self._repair_ghosts(placements, decisions)
        # 登记簿同步 + 映射流提交（边界 → 门位，顺序与流契约一致）
        for q, (kind, loc) in decisions.items():
            if kind == "RETURN":
                reg.return_to_storage(q, loc)
            elif kind == "RESEAT":
                reg.reseat(q, loc)
        self._append_boundary(decisions)
        self._commit_round(next_layer, placements)
        self.decision_log.append({
            "layer": layer, "engine": "ga",
            "stay": sum(1 for v in decisions.values() if v[0] == "STAY"),
            "return": sum(1 for v in decisions.values() if v[0] == "RETURN"),
            "reseat": sum(1 for v in decisions.values() if v[0] == "RESEAT"),
            "forced_e2": 0, "capacity": len(cap_evict),
            "ghost_fix": getattr(self, "ghost_fixes", 0),
            "participants": len(participants),
            "score": round(scored[0][0], 3)})
        self.search_time += time.time() - t0

    # ========================================================= Schema 2 GA
    def _ga_step_v2(self, layer: int):
        """Formal M3/M4 transition with a physical, horizon-bounded objective.

        M3 and M4 execute this exact function.  M3 owns a hard H=0 oracle; M4
        consumes a bounded geometric-decay window.  The current boundary is
        always exact and undiscounted; only predicted future terms are weighted.
        Ghost safety and current-phase scoring are shared invariants.
        """
        t0 = time.time()
        next_layer = layer + 1
        list_gate = self.forecast.target_layer(layer)
        reg, arch = self.registry, self.architecture
        participants = {q for gate in list_gate for q in gate}
        horizon_selection_started_ns = time.perf_counter_ns()
        if self.adaptive_lookahead:
            horizon_decision = AdaptiveHorizonDecision.select(
                self.forecast,
                layer,
                residents=reg.zone_seat,
                target_participants=participants,
                zone_sites=reg.zone_sites,
                theta_capacity=self.theta_capacity,
                active_commitments=self.residency_commitments,
            )
        elif self.decay_lookahead:
            horizon_decision = AdaptiveHorizonDecision.fixed(
                self.lookahead_horizon,
                reason=("strict_zero_no_future_access"
                        if self.lookahead_horizon == 0
                        else "fixed_bounded_decay"),
            )
        else:
            # Crucially, fixed H=0 chooses without calling visible_future().
            # Its backing provider therefore sees only the target-layer read.
            horizon_decision = AdaptiveHorizonDecision.fixed(
                self.lookahead_horizon)
        active_horizon = horizon_decision.selected_horizon
        forecast = (self.forecast if self.decay_lookahead
                    else self.forecast.bounded(active_horizon))
        # Materialise the bounded oracle exactly once.  H=0 executes an empty
        # loop and therefore performs no provider read beyond target_layer().
        visible_forecast = tuple(forecast.weighted_future(layer))
        visible_window = tuple(
            (absolute_layer, gates)
            for absolute_layer, gates, _offset, _weight in visible_forecast)
        next_visible_use = {}
        for absolute_layer, gates, _offset, _weight in visible_forecast:
            for q0, q1 in gates:
                next_visible_use.setdefault(int(q0), (absolute_layer, int(q1)))
                next_visible_use.setdefault(int(q1), (absolute_layer, int(q0)))

        def visible_use(q):
            return next_visible_use.get(int(q))

        # Both formal methods use the same generic rich solve.  The strict H=0
        # DTO contains no forecast term; M4 carries the bounded term table.
        use_rich_boundary = (
            self.resident_backend_requested == "native"
            and self.decay_lookahead
            and self.ablation_fitness_mode == "phase"
        )
        horizon_selection_ns = (
            time.perf_counter_ns() - horizon_selection_started_ns)
        # The boundary is physically split into ``back`` then ``out``.  A
        # non-participating resident may therefore RETURN before the target
        # gates enter and its old zone seat is a legitimate target.  Earlier
        # versions built the gate menu against every T0 resident and silently
        # made that reuse impossible, even when the chromosome selected
        # RETURN.  Only atoms that cannot be moved by this boundary are
        # immutable while the menu is decoded.
        potential_returners = (
            set(reg.zone_seat)
            if self.ablation_policy == "always_return"
            else set(resident_decision_candidates(reg.zone_seat, participants)))
        # Target participants are normally moved only in the out phase.  A
        # lookahead commitment can nevertheless become physically impossible
        # when every straight leg to its pinned seat intersects a stationary
        # atom.  Such a participant must be released in the preceding back
        # phase (RETURN -> re-entry), so keep a separate forced-cycle set rather
        # than weakening ghost safety or falling back to an unrelated router.
        forced_cycle_candidates: set[int] = set()
        movable_before_out = set(potential_returners)

        def static_ghost_rows():
            return [
                (q, *arch.exact_SLM_location_tuple(reg.current_pos(q)))
                for q in range(len(self.mapping[0]))
                if q not in participants and q not in movable_before_out]

        static_ghosts = static_ghost_rows()
        step_cache = CacheStats()
        physical = PhysicalIncrementalCost(len(self.mapping[0]))
        phase_cache = {}
        return_candidate_cache = self.return_candidate_cache

        def movement_phase(legs, ghosts=None, owners=None,
                           exact_threshold=0, batching="phase"):
            """Memoise immutable physical phase costs within one boundary."""
            leg_key = tuple(legs)
            ghost_key = tuple(ghosts) if ghosts else ()
            owner_key = tuple(owners) if owners else ()
            key = (batching, int(exact_threshold), leg_key,
                   ghost_key, owner_key)
            if self.fitness_cache and key in phase_cache:
                return phase_cache[key]
            if (self.fitness_cache and not ghost_key
                    and key in self.phase_cost_cache):
                self.phase_cost_cache.move_to_end(key)
                value = self.phase_cost_cache[key]
                phase_cache[key] = value
                return value
            physical_value = physical.movement_phase(
                leg_key, ghosts=ghosts, owners=owners,
                exact_threshold=exact_threshold, batching=batching)
            boundary_value = BoundaryMovementPhase(
                legs=tuple(
                    BoundaryLeg(
                        float(leg[0]),
                        BoundaryPoint(float(leg[1]), float(leg[2])),
                        BoundaryPoint(float(leg[3]), float(leg[4])),
                    )
                    for leg in leg_key),
                ghosts=tuple(
                    BoundaryGhost(
                        int(ghost[0]),
                        BoundaryPoint(float(ghost[1]), float(ghost[2])),
                    )
                    for ghost in ghost_key),
                owners=owner_key,
                batching=batching,
            )
            value = _BackendMovementPhase(physical_value, boundary_value)
            if self.fitness_cache:
                phase_cache[key] = value
                if not ghost_key:
                    self.phase_cost_cache[key] = value
                    self.phase_cost_cache.move_to_end(key)
                    while len(self.phase_cost_cache) > self.phase_cost_cache_limit:
                        self.phase_cost_cache.popitem(last=False)
            return value

        # ---- Gate menus and immutable leg cache ---------------------------------
        candidates, gate_cache = [], []
        target_pins = {
            q: seat for q, (use_layer, seat) in self.residency_commitments.items()
            if use_layer == next_layer and q in participants
        }
        for q1, q2 in list_gate:
            resident_seats = [reg.zone_seat[q] for q in (q1, q2)
                              if reg.is_resident(q)]
            committed_sites = {
                self._norm_left(target_pins[q]) for q in (q1, q2)
                if q in target_pins
            }
            if len(committed_sites) > 1:
                released = {q for q in (q1, q2) if q in target_pins}
                if not self.decay_lookahead:
                    forced_cycle_candidates.update(released)
                movable_before_out.update(released)
                for q in released:
                    target_pins.pop(q, None)
                committed_sites = set()
                static_ghosts = static_ghost_rows()
            if committed_sites:
                sites = set(committed_sites)
            else:
                sites = self._expanded_sites(q1, q2, list_gate)
                for seat in resident_seats:
                    base = self._norm_left(seat)
                    slm = arch.dict_SLM[base[0]]
                    for dc in range(-self.pin_radius, self.pin_radius + 1):
                        col = base[2] + dc
                        if 0 <= col < slm.n_c:
                            sites.add((base[0], base[1], col))
            # Other target-layer participants cannot vacate before the out
            # phase.  Potential RETURN atoms can, and are checked against the
            # actual decision bits in ``score_plan`` below.
            blocked = {seat for q, seat in reg.zone_seat.items()
                       if q not in (q1, q2) and q not in movable_before_out}
            opts = self._build_opts(sites, q1, q2, blocked)
            if not opts and not committed_sites:
                opts = self._build_opts(set(self._all_zone_sites()),
                                        q1, q2, blocked)
            if not committed_sites and len(opts) < len(list_gate):
                seen = {o[0] for o in opts}
                for site in sorted(set(self._all_zone_sites()) - seen,
                                   key=lambda s: self._site_weight(q1, q2, s)):
                    pair = (site, (site[0] + 1, site[1], site[2]))
                    if pair[0] in blocked or pair[1] in blocked:
                        continue
                    opts.append((site, self._site_weight(q1, q2, site), q1, q2))
                    if len(opts) >= len(list_gate):
                        break
                opts.sort(key=lambda o: (o[1], o[0]))
            opts = self._filter_menu_ghosts(
                opts, q1, q2, static_ghosts, max(1, len(list_gate)))
            if not opts and committed_sites:
                # The earlier STAY remains honoured until this target boundary,
                # but its pin cannot be executed ghost-safely.  Release the
                # pinned endpoint(s) through a mandatory physical cycle, then
                # rebuild the menu over the ordinary complete domain.
                released = {q for q in (q1, q2) if q in target_pins}
                if not self.decay_lookahead:
                    forced_cycle_candidates.update(released)
                movable_before_out.update(released)
                for q in released:
                    target_pins.pop(q, None)
                static_ghosts = static_ghost_rows()
                blocked = {seat for q, seat in reg.zone_seat.items()
                           if q not in (q1, q2)
                           and q not in movable_before_out}
                opts = self._build_opts(
                    set(self._all_zone_sites()), q1, q2, blocked)
                opts = self._filter_menu_ghosts(
                    opts, q1, q2, static_ghosts,
                    max(1, len(list_gate)))
            elif not opts:
                # A local expansion is only a speed path.  Preserve the hard
                # feasibility contract by retrying the complete gate domain
                # before declaring the physical layer impossible.
                opts = self._build_opts(
                    set(self._all_zone_sites()), q1, q2, blocked)
                opts = self._filter_menu_ghosts(
                    opts, q1, q2, static_ghosts,
                    max(1, len(list_gate)))
            if not opts:
                raise RuntimeError(f"layer {next_layer} 门 ({q1},{q2}) 无合法门位")
            candidates.append(opts)
            cached_sites = {}
            for site, _, _, _ in opts:
                s1, s2 = self._pair_seats(q1, q2, site)
                legs, owners, seated = [], [], []
                for q, target in ((q1, s1), (q2, s2)):
                    source_xy = arch.exact_SLM_location_tuple(reg.current_pos(q))
                    target_xy = arch.exact_SLM_location_tuple(target)
                    distance = math.dist(source_xy, target_xy)
                    if distance > 1e-9:
                        legs.append((distance, *source_xy, *target_xy))
                        owners.append(q)
                    else:
                        seated.append((q, *target_xy))
                cached_sites[site] = {
                    "legs": tuple(legs), "owners": tuple(owners),
                    "seated": tuple(seated),
                }
            gate_cache.append(cached_sites)

        # Every non-participating resident is a real decision, including dead ones.
        # Immediate target participants are searched separately below as bounded
        # RETURN -> re-entry relocation moves, so the registered residency GA and
        # its RNG stream remain byte-identical when no cycle is selected.
        eligible = sorted(potential_returners)
        adjacent_resident_participants = sorted(
            set(reg.zone_seat) & participants)
        # The extra RETURN -> re-entry refinement is exact only for a serial
        # chain boundary: one target gate with one reused resident endpoint.
        # On a parallel target front, independently cycling one endpoint can
        # displace the joint gate matching beyond the finite rollout (QFT is a
        # concrete example).  The ordinary resident GA already evaluates that
        # coupled placement, so keep the local extension fail-closed there.
        optional_cycle_candidates = (
            adjacent_resident_participants
            if (not self.decay_lookahead
                and self.ablation_policy == "optimize"
                and len(list_gate) == 1
                and len(adjacent_resident_participants) == 1)
            else [])
        cycle_candidates = sorted(
            set(optional_cycle_candidates) | forced_cycle_candidates)
        n_gates = len(candidates)
        gate_domains = [len(opts) for opts in candidates]
        demand = 2 * len(list_gate)
        max_stays = math.floor(self.theta_capacity * reg.zone_sites - demand + 1e-12)
        if max_stays < 0:
            raise RuntimeError(
                f"layer {next_layer} 当前门需求 {demand} 已超过驻留容量阈值")
        min_returns = max(0, len(eligible) - max_stays)

        # Capacity is normalized before fitness, never patched onto the winner.
        def eviction_key(q):
            visible = visible_use(q)
            return (visible is None, visible[0] if visible else -1, q)

        eviction_order = sorted(eligible, key=eviction_key, reverse=True)
        # Receding-horizon guard: LK may retain an atom only when its next use
        # is actually visible inside L+2/L+3.  Otherwise every boundary can
        # postpone the same terminal RETURN by two layers and the atom remains
        # illuminated indefinitely (the classic finite-horizon procrastination
        # failure observed on multiply).  This rule consumes no information
        # beyond ForecastOracle and is intentionally absent for H=0, whose
        # contract is the exact current transition.
        horizon_forced_returns = {
            q for q in eligible
            if (not self.decay_lookahead
                and self.ablation_policy == "optimize"
                and active_horizon > 0
                and visible_use(q) is None)
        }

        # A visible future use is necessary but not sufficient for residency.
        # Compare every proposed cross-layer wait with a deterministic
        # all-RETURN reference using only the current state and ForecastOracle.
        # The guard credits transfer fidelity and the moving atom's coherence,
        # but not stationary-atom coherence that may disappear when moves share
        # a batch.  This guard belongs only to the legacy discrete-horizon path;
        # formal decay expresses future residency explicitly in forecast_terms.
        all_return_sites = (match_return_sites(
            reg, eligible, self.nu, layer,
            self.box_ratio, self.alpha_lookahead,
            forecast=forecast,
            candidate_mode=(
                "forecast" if active_horizon and not self.decay_lookahead
                else "nearest"),
            candidate_cache=return_candidate_cache)
            if eligible and not use_rich_boundary else {})
        physical_guard_details = []
        physical_forced_returns = set()
        if (not self.decay_lookahead
                and self.ablation_policy == "optimize"
                and active_horizon > 0):
            for q in eligible:
                visible = visible_use(q)
                if visible is None:
                    continue
                idle_pulses = visible[0] - next_layer
                source = arch.exact_SLM_location_tuple(reg.zone_seat[q])
                storage = arch.exact_SLM_location_tuple(all_return_sites[q])
                distance = math.dist(source, storage)
                leg_out = (distance, *source, *storage)
                leg_back = (distance, *storage, *source)
                phases = (
                    movement_phase([leg_out], owners=[q]),
                    movement_phase([leg_back], owners=[q]),
                )
                admit, idle_nll, avoided_move_nll = \
                    physical.residency_break_even(phases, idle_pulses)
                if not admit:
                    physical_forced_returns.add(q)
                physical_guard_details.append({
                    "q": q,
                    "next_use_layer": visible[0],
                    "idle_exposures": idle_pulses,
                    "idle_nll": idle_nll,
                    "avoidable_return_nll": avoided_move_nll,
                    "margin_nll": avoided_move_nll - idle_nll,
                    "forced_return": not admit,
                })

        def resolve_commitment_conflicts(bits):
            """At most one incompatible resident may pin a visible future gate."""
            index = {q: i for i, q in enumerate(eligible)}
            grouped = {}
            for q, bit in zip(eligible, bits):
                if bit:
                    continue
                visible = visible_use(q)
                if visible is None:
                    continue
                use_layer, partner = visible
                grouped.setdefault(
                    (use_layer, tuple(sorted((q, partner)))), []).append(q)
            for _key, atoms in grouped.items():
                if len(atoms) < 2:
                    continue
                sites = {self._norm_left(reg.zone_seat[q]) for q in atoms}
                if len(sites) <= 1:
                    continue
                # Deterministic single-pin repair.  Lower id keeps residency;
                # the other atom returns and remains an ordinary future mover.
                for q in sorted(atoms)[1:]:
                    bits[index[q]] = 1
            return bits

        normalize_cache = {}

        def normalize_chrom(chrom):
            source = tuple(chrom)
            if self.fitness_cache and source in normalize_cache:
                return list(normalize_cache[source])
            raw = list(source)
            if len(raw) != n_gates + len(eligible):
                raise ValueError("Schema 2 染色体长度错误")
            normalized = [raw[i] % gate_domains[i] for i in range(n_gates)]
            bits = [1 if raw[n_gates + i] else 0 for i in range(len(eligible))]
            if self.ablation_policy == "always_stay":
                bits = [0] * len(eligible)
            elif self.ablation_policy in {"always_return", "adjacent_only"}:
                bits = [1] * len(eligible)
            elif horizon_forced_returns:
                for i, q in enumerate(eligible):
                    if q in horizon_forced_returns:
                        bits[i] = 1
            if physical_forced_returns and self.ablation_policy == "optimize":
                for i, q in enumerate(eligible):
                    if q in physical_forced_returns:
                        bits[i] = 1
            if active_horizon > 0 and self.ablation_policy == "optimize":
                bits = resolve_commitment_conflicts(bits)
            selected = sum(bits)
            if selected < min_returns:
                index = {q: i for i, q in enumerate(eligible)}
                for q in eviction_order:
                    i = index[q]
                    if bits[i] == 0:
                        bits[i] = 1
                        selected += 1
                        if selected == min_returns:
                            break
            value = tuple(normalized + bits)
            if self.fitness_cache:
                normalize_cache[source] = value
            return list(value)

        # ---- Decode cache --------------------------------------------------------
        decode_cache = {}

        def decode(chrom):
            gate_key = tuple(chrom[:n_gates])
            if self.fitness_cache and gate_key in decode_cache:
                step_cache.decode_hits += 1
                return decode_cache[gate_key]
            used, placed, accumulated_legs = set(), [], []
            accumulated_ghosts = list(static_ghosts)
            for col, opts in enumerate(candidates):
                start = gate_key[col]
                chosen = None
                for offset in range(len(opts)):
                    candidate = opts[(start + offset) % len(opts)]
                    if candidate[0] in used:
                        continue
                    entry = gate_cache[col][candidate[0]]
                    if new_conflicts(accumulated_legs, entry["legs"],
                                     accumulated_ghosts):
                        continue
                    chosen = candidate
                    break
                if chosen is None:
                    chosen = next((opts[(start + offset) % len(opts)]
                                   for offset in range(len(opts))
                                   if opts[(start + offset) % len(opts)][0] not in used),
                                  None)
                if chosen is None:
                    raise RuntimeError(f"layer {next_layer} 门位菜单无法形成单射")
                used.add(chosen[0])
                placed.append(chosen)
                entry = gate_cache[col][chosen[0]]
                accumulated_legs.extend(entry["legs"])
                accumulated_ghosts.extend(entry["seated"])
            value = tuple(placed)
            if self.fitness_cache:
                decode_cache[gate_key] = value
            return value

        # RETURN matching is performed for the actual chromosome subset.
        return_cache = ({tuple(eligible): all_return_sites}
                        if eligible else {})

        def return_sites_for_atoms(returners):
            returners = tuple(sorted(returners))
            if self.fitness_cache and returners in return_cache:
                step_cache.return_match_hits += 1
                return return_cache[returners]
            # Formal decay exposes future information only through the audited
            # forecast term table.  RETURN matching itself stays current-state
            # exact and therefore uses the same nearest-site domain as M3.
            mode = ("forecast" if active_horizon and not self.decay_lookahead
                    else "nearest")
            value = (match_return_sites(
                reg, list(returners), self.nu, layer,
                self.box_ratio, self.alpha_lookahead,
                forecast=forecast, candidate_mode=mode,
                candidate_cache=return_candidate_cache)
                if returners else {})
            if self.fitness_cache:
                return_cache[returners] = value
            return value

        def back_legs(returners, sites):
            legs, owners = [], []
            for q in returners:
                p0 = arch.exact_SLM_location_tuple(reg.zone_seat[q])
                p1 = arch.exact_SLM_location_tuple(sites[q])
                distance = math.dist(p0, p1)
                if distance > 1e-9:
                    legs.append((distance, *p0, *p1))
                    owners.append(q)
            return legs, owners

        terminal_return_cache = {}

        def terminal_return_sites(returners, sim_locations):
            # The terminal forecast is evaluated after hypothetical current and
            # future moves.  Build a shadow registry from that simulated state;
            # using the live registry would treat already-returned forecast
            # atoms as free storage sites and could assign two atoms the same
            # endpoint, underpricing the terminal phase.
            state = tuple(tuple(sim_locations[q])
                          for q in range(len(self.mapping[0])))
            key = (tuple(sorted(returners)), state)
            if key not in terminal_return_cache:
                # Do not deepcopy the registry: it owns the full preprocessed
                # Architecture (large nested distance tables), while terminal
                # matching only needs the immutable architecture/home metadata
                # plus simulated occupancy.  Reconstructing this lightweight
                # state is semantically identical and removes the dominant
                # legacy discrete-rollout profiling cost.
                shadow = ResidentRegistry(
                    reg.arch, reg.homes, reg.theta)
                shadow.zone_seat = {}
                shadow.storage_site = {}
                for q, location in enumerate(state):
                    if shadow._is_zone(location):
                        shadow.zone_seat[q] = tuple(location)
                    else:
                        shadow.storage_site[q] = tuple(location)
                terminal_return_cache[key] = (
                    match_return_sites(
                        shadow, list(key[0]), self.nu, layer,
                        self.box_ratio, self.alpha_lookahead,
                        forecast=forecast, candidate_mode="forecast",
                        candidate_cache=return_candidate_cache,
                        assignment_mode="greedy")
                    if key[0] else {})
            return terminal_return_cache[key]

        # Pure rollout geometry survives boundary changes.  Random/large
        # circuits revisit the same small set of storage/zone coordinate pairs
        # millions of times even when the full registry state is not an LRU
        # transition hit.
        rollout_pair_cache = self.rollout_pair_cache
        rollout_site_cache = self.rollout_site_cache
        rollout_phase_score_cache = {}

        def forecast_phases(returners, sites, target_placements):
            """Deterministic multi-layer placement rollout without a nested GA.

            The old rollout followed only the current boundary's eligible atoms.
            Consequently a gate-site chromosome could not see that its target
            participant becomes the shared atom of L+2/L+3 (the dominant GHZ
            pattern).  This proxy now advances positions for every visible gate
            using a deterministic local physical minimum.  It still reads future
            gates exclusively through ``ForecastOracle``.
            """
            phases, exposures, terms = [], 0, []
            batching = ("greedy" if self.ablation_fitness_mode == "lumped_greedy"
                        else "phase")
            sim_locations = {
                q: tuple(sites[q]) if q in returners
                else tuple(reg.current_pos(q))
                for q in range(len(self.mapping[0]))}

            def placement_rows(values):
                rows = []
                for value in values:
                    if isinstance(value, dict):
                        rows.append((tuple(value["gate"]), tuple(value["seats"])))
                    else:
                        q1, q2 = value[2], value[3]
                        rows.append(((q1, q2), self._pair_seats(q1, q2, value[0])))
                return rows

            for gate, seats_pair in placement_rows(target_placements):
                for q, seat in zip(gate, seats_pair):
                    sim_locations[q] = tuple(seat)

            def local_pair(q1, q2, site):
                key = (tuple(site), tuple(sim_locations[q1]),
                       tuple(sim_locations[q2]))
                if self.fitness_cache and key in rollout_pair_cache:
                    rollout_pair_cache.move_to_end(key)
                    return rollout_pair_cache[key]
                a, b = site, (site[0] + 1, site[1], site[2])
                p1, p2 = sim_locations[q1], sim_locations[q2]
                if p1 in (a, b):
                    result = (p1, b if p1 == a else a)
                elif p2 in (a, b):
                    other = b if p2 == a else a
                    result = (other, p2)
                else:
                    p1_xy = arch.exact_SLM_location_tuple(p1)
                    p2_xy = arch.exact_SLM_location_tuple(p2)
                    a_xy = arch.exact_SLM_location_tuple(a)
                    b_xy = arch.exact_SLM_location_tuple(b)
                    distances_ab = (
                        math.dist(p1_xy, a_xy), math.dist(p2_xy, b_xy))
                    distances_ba = (
                        math.dist(p1_xy, b_xy), math.dist(p2_xy, a_xy))

                    def orientation_cost(pair, distances):
                        return (
                            sum(distance > 1e-9 for distance in distances),
                            max(distances), sum(distances), pair)

                    result = min(
                        (orientation_cost((a, b), distances_ab),
                         orientation_cost((b, a), distances_ba)))[-1]
                if self.fitness_cache:
                    rollout_pair_cache[key] = result
                    rollout_pair_cache.move_to_end(key)
                return result

            def local_sites(q1, q2, gate_count):
                key = (int(gate_count), tuple(sim_locations[q1]),
                       tuple(sim_locations[q2]))
                if self.fitness_cache and key in rollout_site_cache:
                    rollout_site_cache.move_to_end(key)
                    return rollout_site_cache[key]
                radius = max(
                    self.pin_radius,
                    max(1, math.ceil(math.sqrt(gate_count) / 2)))
                anchors = set()
                for q in (q1, q2):
                    location = sim_locations[q]
                    if reg._is_zone(location):
                        anchors.add(self._norm_left(location))
                    else:
                        nearest = arch.nearest_entanglement_site(
                            *location, *location)[0]
                        anchors.add(self._norm_left(nearest))
                result = set()
                for base in anchors:
                    slm = arch.dict_SLM[base[0]]
                    for row in range(max(0, base[1] - radius),
                                     min(slm.n_r, base[1] + radius + 1)):
                        for column in range(max(0, base[2] - radius),
                                            min(slm.n_c, base[2] + radius + 1)):
                            result.add((base[0], row, column))
                # A forecast is a deterministic rollout proxy, not a nested
                # placement search.  Evaluating every point in two 5x5 anchor
                # windows made each legacy H=2 chromosome perform 40--50 full
                # physical phase simulations.  Keep a fixed bounded support
                # that always contains both participant anchors and their
                # midpoint, then fill it with the nearest joint neighbours.
                # The selected future phase is still scored by the exact shared
                # physical cost below.  This is the same bounded-rollout rule
                # at every layer and consumes no information beyond the oracle.
                rollout_site_budget = 4
                ordered_anchors = tuple(sorted(anchors))
                priority = [site for site in ordered_anchors if site in result]
                if (len(ordered_anchors) == 2
                        and ordered_anchors[0][0] == ordered_anchors[1][0]):
                    left, right = ordered_anchors
                    midpoint = (
                        left[0],
                        round((left[1] + right[1]) / 2),
                        round((left[2] + right[2]) / 2),
                    )
                    if midpoint in result and midpoint not in priority:
                        priority.append(midpoint)

                def joint_neighbour_key(site):
                    distances = [
                        abs(site[1] - anchor[1])
                        + abs(site[2] - anchor[2])
                        + (0 if site[0] == anchor[0] else 10_000)
                        for anchor in ordered_anchors
                    ]
                    return (min(distances), max(distances),
                            sum(distances), site)

                for site in sorted(result, key=joint_neighbour_key):
                    if site not in priority:
                        priority.append(site)
                    if len(priority) >= rollout_site_budget:
                        break
                value = tuple(priority[:rollout_site_budget])
                if self.fitness_cache:
                    rollout_site_cache[key] = value
                    rollout_site_cache.move_to_end(key)
                return value

            last_visible_offset = 0
            for _absolute_layer, gates, offset, weight in visible_forecast:
                last_visible_offset = offset
                term_phase_start = len(phases)
                term_exposures_start = exposures
                future_participants = {q for gate in gates for q in gate}
                blocked = {
                    location for q, location in sim_locations.items()
                    if q not in future_participants and reg._is_zone(location)}
                chosen_rows, used_sites = [], set()
                for q1, q2 in gates:
                    options = []
                    for site in local_sites(q1, q2, len(gates)):
                        if site in used_sites:
                            continue
                        pair = local_pair(q1, q2, site)
                        if pair[0] in blocked or pair[1] in blocked:
                            continue
                        legs = []
                        for q, target in zip((q1, q2), pair):
                            p0 = arch.exact_SLM_location_tuple(sim_locations[q])
                            p1 = arch.exact_SLM_location_tuple(target)
                            distance = math.dist(p0, p1)
                            if distance > 1e-9:
                                legs.append((distance, *p0, *p1))
                        phase = movement_phase(legs, batching=batching)
                        score_key = (phase, q1, q2)
                        if self.fitness_cache and \
                                score_key in rollout_phase_score_cache:
                            objective = rollout_phase_score_cache[score_key]
                        else:
                            objective, _ = physical.score(
                                [phase], 0, (q1, q2))
                            if self.fitness_cache:
                                rollout_phase_score_cache[score_key] = objective
                        options.append((objective, site, pair))
                    if not options:
                        # The local window is a speed path, not a semantic
                        # restriction.  Fall back to the complete zone domain.
                        for site in self._all_zone_sites():
                            if site in used_sites:
                                continue
                            pair = local_pair(q1, q2, site)
                            if pair[0] in blocked or pair[1] in blocked:
                                continue
                            options.append(((0.0, 0, 0.0, 0.0, ()), site, pair))
                    _, site, pair = min(options)
                    used_sites.add(site)
                    chosen_rows.append(((q1, q2), pair))

                before = dict(sim_locations)
                legs, owners = [], []
                for gate, pair in chosen_rows:
                    for q, target in zip(gate, pair):
                        p0 = arch.exact_SLM_location_tuple(before[q])
                        p1 = arch.exact_SLM_location_tuple(target)
                        distance = math.dist(p0, p1)
                        if distance > 1e-9:
                            legs.append((distance, *p0, *p1))
                            owners.append(q)
                        sim_locations[q] = tuple(target)
                if legs:
                    ghosts = [(q, *arch.exact_SLM_location_tuple(location))
                              for q, location in before.items()]
                    phases.append(movement_phase(
                        legs, ghosts=ghosts, owners=owners,
                        batching=batching))
                # Every atom sitting in an illuminated entanglement zone but
                # absent from this future gate layer is physically exposed.
                # Restricting this to the current boundary's decision genes
                # misses atoms that entered in the target or first forecast
                # layer and then became idle inside the legacy rollout window.
                exposures += sum(
                    1 for q in range(len(self.mapping[0]))
                    if reg._is_zone(sim_locations[q])
                    and q not in future_participants)
                terms.append({
                    "kind": "future_layer",
                    "offset": offset,
                    "weight": weight,
                    "phases": tuple(phases[term_phase_start:]),
                    "idle_exposures": exposures - term_exposures_start,
                })
            # Receding-horizon optimisation otherwise has a procrastination
            # failure: an atom with no visible use can choose STAY because one
            # RETURN phase is slightly dearer than H+1 excitation pulses, make
            # the same choice at the next boundary, and remain illuminated for
            # the rest of the circuit.  M4 closes its two-layer window with a
            # deterministic terminal RETURN cost for every simulated resident.
            # This terminal value reads no layer outside ForecastOracle and is
            # intentionally absent for H=0, whose registered contract is the
            # exact current transition only.
            if active_horizon and last_visible_offset:
                # Close every candidate on the same physical terminal state.
                # Limiting this to the boundary's original decision genes
                # omitted target/future-layer partners that entered the zone
                # during rollout.  Their eventual exit then vanished from the
                # objective, systematically favouring placements that dragged
                # a fresh partner to a resident's remote seat.  All simulated
                # residents must therefore receive a terminal RETURN value.
                terminal = tuple(
                    q for q in range(len(self.mapping[0]))
                    if reg._is_zone(sim_locations[q]))
                terminal_sites = terminal_return_sites(terminal, sim_locations)
                terminal_legs, terminal_owners = [], []
                for q in terminal:
                    p0 = arch.exact_SLM_location_tuple(sim_locations[q])
                    p1 = arch.exact_SLM_location_tuple(terminal_sites[q])
                    distance = math.dist(p0, p1)
                    if distance > 1e-9:
                        terminal_legs.append((distance, *p0, *p1))
                        terminal_owners.append(q)
                if terminal_legs:
                    terminal_phase = movement_phase(
                        terminal_legs, owners=terminal_owners,
                        batching=batching)
                    phases.append(terminal_phase)
                    terminal_phases = (terminal_phase,)
                else:
                    terminal_phases = ()
                # Terminal closure is valued at the last actually visible
                # depth.  It never reads or discounts once more at H+1, and no
                # terminal term exists when the circuit has no visible future.
                terminal_offset = last_visible_offset
                terminal_weight = forecast.future_weight(terminal_offset)
                terms.append({
                    "kind": "terminal_return",
                    "offset": terminal_offset,
                    "weight": terminal_weight,
                    "phases": terminal_phases,
                    "idle_exposures": 0,
                })
            return phases, exposures, tuple(terms)

        def score_decay_objective(current_phases, current_exposures,
                                   chromosome, gate_option_indices,
                                   return_assignments):
            """Exact current physics plus a weighted, non-executable forecast.

            Only the primary search NLL receives future heuristic terms.  Move
            batches/time/distance and the returned physical breakdown describe
            the executable current boundary exactly and are never discounted or
            inflated by a predicted layer.
            """
            current_objective, current_breakdown = physical.score(
                current_phases, current_exposures, chromosome)
            if native_rich_problem is None or rich_config is None:
                raise RuntimeError("decay forecast term table is unavailable")
            # This independent Python evaluator is also the differential oracle
            # for the C++ generic solver.  Both consume the same immutable term
            # table; current physical fidelity is scored separately above.
            from zzx.reference_backend import evaluate_decay_forecast
            (weighted_nll, by_depth, category_breakdown,
             terms_applied, terms_skipped) = evaluate_decay_forecast(
                native_rich_problem, rich_config, chromosome,
                gate_option_indices, return_assignments)
            search_objective = (
                current_breakdown.negative_log_fidelity + weighted_nll,
                current_objective[1], current_objective[2],
                current_objective[3], current_objective[4],
            )
            return search_objective, current_breakdown, {
                "configured_depth": self.lookahead_horizon,
                "effective_depth": forecast.effective_horizon,
                "rho": float(self.lookahead_horizon_config["rho"]),
                "epsilon": float(self.lookahead_horizon_config["epsilon"]),
                "alpha_lookahead": self.alpha_lookahead,
                "forecast_by_depth": list(by_depth),
                "forecast_breakdown": dict(category_breakdown),
                "forecast_terms_applied": terms_applied,
                "forecast_terms_skipped_cutoff": terms_skipped,
                "weighted_negative_log_fidelity": weighted_nll,
            }

        boundary_architecture = self.boundary_architecture_snapshot
        if boundary_architecture is None:
            # Focused transition tests may invoke _ga_step_v2 directly instead
            # of entering through run(); keep that diagnostic path exact while
            # constructing the same persistent snapshot only once.
            boundary_architecture = self._prepare_boundary_architecture()
        boundary_config = BoundaryConfig(
            exact_coloring_threshold=0,
            horizon_policy=("dynamic" if self.adaptive_lookahead else "fixed"),
            max_horizon=active_horizon,
            # Preserve the frozen Python search semantics.  Executable ghost
            # safety is still enforced by _repair_ghosts_with_commitments and
            # the independent replay after winner selection.
            enforce_single_leg_ghost=False,
        )
        rich_config = None
        if self.decay_lookahead:
            lookahead_spec = self.lookahead_horizon_config
            rich_config = RichSearchConfig(
                operator_profile=self.operator_profile,
                forecast_mode="decay",
                forecast_policy=str(lookahead_spec["policy"]),
                decay_kind=str(lookahead_spec["decay"]),
                max_horizon=active_horizon,
                alpha_lookahead=self.alpha_lookahead,
                decay_rho=float(lookahead_spec["rho"]),
                decay_epsilon=float(lookahead_spec["epsilon"]),
                population_size=self.population_size,
                iterations=self.iterations,
                neighbors_per_solution=self.neighbors_per_solution,
                neighbor_sample_size=self.neighbor_sample_size,
                elite_count=self.elite_count,
                early_stop_patience=self.early_stop_patience,
                max_unique_evaluations=self.max_unique_evaluations,
                exact_coloring_threshold=0,
                enforce_single_leg_ghost=False,
                fitness_cache=self.fitness_cache,
            )
        step_backend = {
            "marshal_ns": 0,
            "search_kernel_ns": 0,
            "fitness_ns": 0,
            "selection_ns": 0,
            "native_parse_ns": 0,
            "native_serialize_ns": 0,
            "calls": 0,
            "candidates": 0,
        }

        def score_from_backend(value):
            breakdown = PhysicalCostBreakdown(
                negative_log_fidelity=value.negative_log_fidelity,
                transfer_nll=value.transfer_nll,
                idle_excitation_nll=value.idle_excitation_nll,
                coherence_nll=value.coherence_nll,
                move_batches=value.move_batches,
                move_time_us=value.move_time_us,
                total_distance_um=value.total_distance_um,
                idle_exposures=value.idle_exposures,
                transfers=value.transfers,
            )
            return value.objective, breakdown

        def evaluate_prepared(prepared):
            """Evaluate decoded plans through one coarse backend call."""
            if not prepared:
                return []
            # A few focused transition tests construct the registry/oracle
            # directly and invoke _ga_step_v2 without run().  Lazily create the
            # exact same persistent backend for that supported diagnostic path.
            if self.boundary_backend is None:
                self.boundary_backend = select_backend(
                    boundary_architecture,
                    backend=self.resident_backend_requested,
                    formal=self.formal_native,
                    require_registered_wheel=self.formal_native,
                    expected_wheel_sha256=(
                        self.native_wheel_sha256
                        if self.formal_native else None),
                )
            problem = BoundaryProblem(
                boundary_architecture,
                tuple(item[0] for item in prepared),
                boundary_id=f"{self.method_id}:L{layer}",
                effective_horizon=active_horizon,
            )
            started_ns = time.perf_counter_ns()
            values = self.boundary_backend.evaluate_many(
                problem, config=boundary_config)
            elapsed_ns = time.perf_counter_ns() - started_ns
            native_timing = getattr(
                self.boundary_backend, "last_evaluate_timing", {}) or {}
            marshal_ns = int(native_timing.get("marshal_ns", 0))
            fitness_ns = int(native_timing.get("fitness_ns", elapsed_ns))
            selection_ns = 0
            kernel_ns = fitness_ns + selection_ns
            delta = {
                "marshal_ns": marshal_ns,
                "search_kernel_ns": kernel_ns,
                "fitness_ns": fitness_ns,
                "selection_ns": selection_ns,
                "native_parse_ns": int(
                    native_timing.get("native_parse_ns", 0)),
                "native_serialize_ns": int(
                    native_timing.get("native_serialize_ns", 0)),
                "calls": 1,
                "candidates": len(values),
            }
            for key, value in delta.items():
                step_backend[key] += value
                self.boundary_backend_metrics[key] += value
            if len(values) != len(prepared):
                raise RuntimeError("resident backend candidate count mismatch")
            return [score_from_backend(value) for value in values]

        forecast_audit_cache = {}

        def build_plan(chrom, include_forecast=True, cycle_returners=()):
            chrom = normalize_chrom(chrom)
            cycle_returners = tuple(sorted(
                (set(cycle_returners) | forced_cycle_candidates)
                & set(cycle_candidates)))
            scored_chrom = tuple(chrom) + tuple(
                1 if q in cycle_returners else 0 for q in cycle_candidates)
            try:
                placed = decode(chrom)
            except RuntimeError:
                # Some gene vectors induce a greedy menu order that violates
                # Hall's condition even though the layer has other legal
                # placements.  Such a chromosome is infeasible, not a compiler
                # failure; the seeded full matching remains a finite incumbent.
                return (None, (), 0,
                        ((float("inf"), float("inf"), float("inf"),
                          float("inf"), scored_chrom),
                         PhysicalIncrementalCost(len(self.mapping[0])).score(
                             (), 0, scored_chrom)[1]))
            bits = chrom[n_gates:]
            returners = tuple(sorted(
                {q for q, bit in zip(eligible, bits) if bit}
                | set(cycle_returners)))
            sites = return_sites_for_atoms(returners)
            legs_back, owners_back = back_legs(returners, sites)
            positions_t0 = [
                (q, *arch.exact_SLM_location_tuple(reg.current_pos(q)))
                for q in range(len(self.mapping[0]))]
            positions_t1 = []
            returned = set(returners)
            for q in range(len(self.mapping[0])):
                loc = sites[q] if q in returned else reg.current_pos(q)
                positions_t1.append((q, *arch.exact_SLM_location_tuple(loc)))
            # Exact T1 occupancy gate: a target pair may reuse a RETURNed old
            # seat, but never a seat whose owner selected STAY.  This makes
            # RETURN and gate placement genuinely joint genes instead of a
            # menu-time approximation.
            occupied_t1 = {
                (x, y): q for q, x, y in positions_t1 if q not in participants}
            for chosen in placed:
                s1, s2 = self._pair_seats(chosen[2], chosen[3], chosen[0])
                for target in (s1, s2):
                    target_xy = arch.exact_SLM_location_tuple(target)
                    if target_xy in occupied_t1:
                        return (None, (), 0,
                                ((float("inf"), float("inf"), float("inf"),
                                  float("inf"), scored_chrom),
                                 PhysicalIncrementalCost(
                                     len(self.mapping[0])).score(
                                         (), 0, scored_chrom)[1]))
            # Compute out legs from the actual post-decision positions.  Main
            # NL/LK results are unchanged, while always-RETURN can faithfully
            # model a next-layer participant returning and re-entering.
            position_t1_by_q = {row[0]: (row[1], row[2]) for row in positions_t1}
            legs_out, owners_out = [], []
            for chosen in placed:
                s1, s2 = self._pair_seats(chosen[2], chosen[3], chosen[0])
                for q, target in ((chosen[2], s1), (chosen[3], s2)):
                    p0 = position_t1_by_q[q]
                    p1 = arch.exact_SLM_location_tuple(target)
                    distance = math.dist(p0, p1)
                    if distance > 1e-9:
                        legs_out.append((distance, *p0, *p1))
                        owners_out.append(q)
            if self.ablation_fitness_mode == "lumped_greedy":
                # Deliberately reproduce the old proxy: back/out legs are put in
                # one greedy pool even though the executable router must retain
                # the physical gate boundary.  Ghost correctness remains a hard
                # post-selection repair and final replay invariant.
                phases = [movement_phase(
                    legs_back + legs_out,
                    owners=owners_back + owners_out,
                    batching="greedy")]
            else:
                phases = [
                    movement_phase(
                        legs_back, ghosts=positions_t0, owners=owners_back),
                    movement_phase(
                        legs_out, ghosts=positions_t1, owners=owners_out),
                ]
            current_phases = tuple(phases)
            current_exposures = sum(
                1 for q in eligible
                if q not in returned and q not in participants)
            if include_forecast and self.decay_lookahead:
                gate_option_indices = tuple(
                    candidates[column].index(chosen)
                    for column, chosen in enumerate(placed))
                return_assignments = tuple(
                    (q, self.boundary_storage_site_id[tuple(sites[q])])
                    for q in returners)
                objective, current_breakdown, audit = score_decay_objective(
                    current_phases, current_exposures, scored_chrom,
                    gate_option_indices, return_assignments)
                forecast_audit_cache[scored_chrom] = audit
                return None, (), 0, (objective, current_breakdown)
            if include_forecast:
                future_phases, future_exposures, forecast_terms = \
                    forecast_phases(returned, sites, placed)
            else:
                future_phases, future_exposures, forecast_terms = (), 0, ()
            phases.extend(future_phases)
            idle_exposures = current_exposures + future_exposures
            candidate = CandidatePlan(
                chromosome=scored_chrom,
                phases=tuple(phase.boundary for phase in phases),
                idle_exposures=idle_exposures,
            )
            return candidate, tuple(phases), idle_exposures, None

        fitness_cache = {}

        def fitness_many(chromosomes, include_forecast=True,
                         cycle_returners=()):
            chromosomes = list(chromosomes)
            cycles = tuple(sorted(
                (set(cycle_returners) | forced_cycle_candidates)
                & set(cycle_candidates)))
            values = [None] * len(chromosomes)
            pending = []
            for index, chrom in enumerate(chromosomes):
                key = tuple(normalize_chrom(chrom))
                cache_key = (bool(include_forecast), key, cycles)
                step_cache.evaluations += 1
                if self.fitness_cache and cache_key in fitness_cache:
                    step_cache.fitness_hits += 1
                    values[index] = fitness_cache[cache_key]
                    continue
                step_cache.unique_evaluations += 1
                candidate, phases, idle_exposures, ready = build_plan(
                    key, include_forecast=include_forecast,
                    cycle_returners=cycles)
                if ready is not None:
                    value = ready
                elif any(phase.boundary.batching == "greedy"
                         for phase in phases):
                    value = physical.score(
                        phases, idle_exposures, candidate.chromosome)
                else:
                    pending.append((index, cache_key, candidate, phases,
                                    idle_exposures))
                    continue
                values[index] = value
                if self.fitness_cache:
                    fitness_cache[cache_key] = value
            if pending:
                evaluated = evaluate_prepared([
                    (candidate, phases, idle_exposures)
                    for _, _, candidate, phases, idle_exposures in pending])
                for (index, cache_key, _candidate, _phases,
                     _idle_exposures), value in zip(pending, evaluated):
                    values[index] = value
                    if self.fitness_cache:
                        fitness_cache[cache_key] = value
            return values

        def fitness(chrom, include_forecast=True, cycle_returners=()):
            return fitness_many(
                [chrom], include_forecast=include_forecast,
                cycle_returners=cycle_returners)[0]

        # The direct path below exhaustively scores every normalized chromosome.
        # Any stochastic/greedy seed refinement before that enumeration cannot
        # change its winner; it only repeats physical rollouts.  Decide this
        # once so small-space layers retain exact search while skipping redundant
        # seed work (the dominant case in long random circuits).
        search_kernel_started_ns = time.perf_counter_ns()
        direct_search_space = 1
        for domain in gate_domains:
            direct_search_space *= domain
            if direct_search_space > 64:
                break
        if direct_search_space <= 64 and self.ablation_policy == "optimize":
            direct_search_space *= 2 ** len(eligible)
        will_enumerate = direct_search_space <= 64

        # The LRU key is constructed before either backend starts searching so
        # the same previous-boundary elite is supplied to Python and C++.
        # H=0's bounded oracle returns an empty window without reading a future
        # layer from its provider.
        state_key = (
            active_horizon, self.ablation_policy,
            self.ablation_fitness_mode,
            tuple(tuple(reg.current_pos(q)) for q in range(len(self.mapping[0]))),
            tuple(list_gate), visible_window, tuple(gate_domains), tuple(eligible),
            min_returns, tuple(sorted(physical_forced_returns)),
            tuple(sorted((q, use_layer, tuple(seat))
                         for q, (use_layer, seat)
                         in self.residency_commitments.items())),
        )
        cached_winner = self.transition_cache.get(state_key)

        # Physical greedy seed: first solve the ZAC-style global gate/site
        # matching, then start with every non-participant RETURNed so every
        # matched site is genuinely vacant at T1.  This is a strong incumbent
        # inside the same joint chromosome space, not an external baseline
        # fallback.  Subsequent coordinate sweeps restore STAY whenever the
        # complete registered physical objective prefers residency.
        matched = self._match_gates(candidates, list_gate) if n_gates else []
        matched_genes = []
        for col, placement in enumerate(matched):
            site = tuple(placement["site"])
            matched_genes.append(next(
                (index for index, option in enumerate(candidates[col])
                 if tuple(option[0]) == site), 0))

        native_rich_result = None
        native_rich_problem = None
        native_return_sites = None
        native_reference_forecast = None
        rich_search_stats = {}
        rich_forecast_terms = ()
        if self.decay_lookahead:
            if forced_cycle_candidates or cycle_candidates:
                raise RuntimeError(
                    "formal decay boundary cannot contain legacy cycle search")
            if rich_config is None:
                raise RuntimeError("formal decay search config was not resolved")

            rich_gate_domains = []
            for column, domain in enumerate(candidates):
                rich_domain = []
                for site, _weight, q1, q2 in domain:
                    site = tuple(site)
                    target1, target2 = self._pair_seats(q1, q2, site)
                    rich_domain.append(RichGateOption(
                        site_id=self.boundary_site_id[site],
                        q1=int(q1),
                        q2=int(q2),
                        target1=None,
                        target2=None,
                        target1_site_id=self.boundary_site_id[tuple(target1)],
                        target2_site_id=self.boundary_site_id[tuple(target2)],
                    ))
                rich_gate_domains.append(tuple(rich_domain))

            occupied_storage = reg.occupied_storage()
            rich_return_domains = []
            for q in eligible:
                zone_location = tuple(reg.zone_seat[q])
                nearest = tuple(arch.nearest_storage_site(*zone_location))
                cache_key = (
                    "nearest", zone_location, nearest,
                    tuple(reg.homes[q]), int(self.box_ratio), 0.0,
                )
                options = return_candidate_cache.get(cache_key)
                if options is None:
                    source_xy = arch.exact_SLM_location_tuple(zone_location)
                    options = tuple(sorted(
                        (sqrt(math.dist(
                            source_xy,
                            arch.exact_SLM_location_tuple(site))), tuple(site))
                        for site in set(_box_sites(
                            arch, nearest, self.box_ratio, set()))
                    ))
                    return_candidate_cache[cache_key] = options
                rich_return_domains.append(tuple(
                    RichReturnOption(
                        site_id=self.boundary_storage_site_id[tuple(site)],
                        point=None,
                        cost=float(cost),
                        site_location=tuple(site),
                    )
                    for cost, site in options
                    if tuple(site) not in occupied_storage
                ))

            eligible_index = {q: index for index, q in enumerate(eligible)}
            future_atoms = {
                offset: {q for gate in gates for q in gate}
                for _absolute, gates, offset, _weight in visible_forecast
            }
            last_visible_offset = max(future_atoms, default=0)
            transfer_move_nll = -2.0 * math.log(physical.F_TRANSFER)
            idle_pulse_nll = -(
                math.log(physical.F_EXC)
                + math.log1p(-physical.T_RYDBERG_US / physical.T2_US))

            def mover_coherence_nll(source_location, target_location):
                source = arch.exact_SLM_location_tuple(source_location)
                target = arch.exact_SLM_location_tuple(target_location)
                distance = math.dist(source, target)
                if distance <= 1e-12:
                    return 0.0
                mover_idle = math.sqrt(distance / physical.ACCEL_UM_PER_US2)
                if mover_idle >= physical.T2_US:
                    raise RuntimeError("forecast mover coherence is outside T2")
                return -math.log1p(-mover_idle / physical.T2_US)

            terms = []
            for q in eligible:
                index = eligible_index[q]
                use_offsets = [
                    offset for offset in sorted(future_atoms)
                    if q in future_atoms[offset]
                ]
                first_use = use_offsets[0] if use_offsets else None
                for offset in sorted(future_atoms):
                    if first_use is not None and offset >= first_use:
                        break
                    terms.append(RichForecastTerm(
                        depth=offset,
                        kind="stay",
                        category="residency",
                        index=index,
                        nll=idle_pulse_nll,
                    ))
                if first_use is not None:
                    # RETURN now implies one later storage->zone re-entry.  Its
                    # transfer part is site-independent; atom-local movement
                    # coherence is tied to the actual matched RETURN site.
                    terms.append(RichForecastTerm(
                        depth=first_use,
                        kind="return",
                        category="reentry",
                        index=index,
                        nll=transfer_move_nll,
                    ))
                    target = tuple(reg.zone_seat[q])
                    for option in rich_return_domains[index]:
                        terms.append(RichForecastTerm(
                            depth=first_use,
                            kind="return_site",
                            category="reentry",
                            index=index,
                            selector=option.site_id,
                            nll=mover_coherence_nll(
                                option.site_location, target),
                        ))
                elif last_visible_offset:
                    # No visible reuse leaves a STAYing atom in the zone at
                    # window close.  Charge its deterministic terminal exit at
                    # the last actual visible offset, never at H+1.
                    source = tuple(reg.zone_seat[q])
                    target = tuple(arch.nearest_storage_site(*source))
                    terms.append(RichForecastTerm(
                        depth=last_visible_offset,
                        kind="stay",
                        category="terminal",
                        index=index,
                        nll=(transfer_move_nll
                             + mover_coherence_nll(source, target)),
                    ))

            if last_visible_offset:
                # Current target participants are resident after this boundary.
                # Their terminal exit depends on the decoded gate-site gene, so
                # preserve that physical distinction with gate-option terms.
                for column, domain in enumerate(candidates):
                    for selector, (site, _weight, q1, q2) in enumerate(domain):
                        target1, target2 = self._pair_seats(q1, q2, tuple(site))
                        terminal_nll = 0.0
                        for target in (tuple(target1), tuple(target2)):
                            storage = tuple(arch.nearest_storage_site(*target))
                            terminal_nll += (
                                transfer_move_nll
                                + mover_coherence_nll(target, storage))
                        terms.append(RichForecastTerm(
                            depth=last_visible_offset,
                            kind="gate_option",
                            category="terminal",
                            index=column,
                            selector=selector,
                            nll=terminal_nll,
                        ))
            rich_forecast_terms = tuple(terms)
            native_rich_problem = RichH0Problem(
                architecture=boundary_architecture,
                current_points=(),
                current_site_ids=tuple(
                    self.boundary_site_id[tuple(reg.current_pos(q))]
                    for q in range(len(self.mapping[0]))),
                participants=tuple(sorted(participants)),
                gate_domains=tuple(rich_gate_domains),
                static_ghosts=tuple(BoundaryGhost(
                    int(row[0]),
                    BoundaryPoint(float(row[1]), float(row[2])))
                    for row in static_ghosts),
                eligible=tuple(eligible),
                min_returns=min_returns,
                eviction_order_indices=tuple(
                    eligible_index[q] for q in eviction_order),
                forced_return_mask=tuple(
                    q in horizon_forced_returns
                    or q in physical_forced_returns
                    for q in eligible),
                return_domains=tuple(rich_return_domains),
                matched_gate_genes=tuple(matched_genes),
                decision_policy=self.ablation_policy,
                occupied_storage_site_ids=tuple(sorted(
                    self.boundary_storage_site_id[tuple(site)]
                    for site in occupied_storage)),
                forecast_terms=rich_forecast_terms,
                boundary_id=f"{self.method_id}:L{layer}",
                selected_horizon=active_horizon,
            )

        if use_rich_boundary:
            if self.boundary_backend is None:
                self.boundary_backend = select_backend(
                    boundary_architecture,
                    backend=self.resident_backend_requested,
                    formal=self.formal_native,
                    require_registered_wheel=self.formal_native,
                    expected_wheel_sha256=(
                        self.native_wheel_sha256
                        if self.formal_native else None),
                )
            native_rich_result = self.boundary_backend.solve_rich_boundary(
                native_rich_problem,
                rich_config,
                self.rng.getstate(),
                cached_winner=cached_winner,
            )
            if native_rich_result.operator_profile != self.operator_profile:
                raise RuntimeError(
                    "native rich operator profile differs from resolved config")
            self.rng.setstate(native_rich_result.rng_state)
            native_return_sites = {
                int(q): self.boundary_storage_locations[int(site_id)]
                for q, site_id in native_rich_result.return_assignments
            }
            if set(native_return_sites) != {
                    q for q, bit in zip(
                        eligible,
                        native_rich_result.winner.chromosome[n_gates:])
                    if bit}:
                raise RuntimeError(
                    "native rich RETURN assignments disagree with winner bits")
            from zzx.reference_backend import evaluate_decay_forecast
            reference_forecast = evaluate_decay_forecast(
                native_rich_problem, rich_config,
                native_rich_result.winner.chromosome,
                native_rich_result.gate_option_indices,
                native_rich_result.return_assignments)
            native_reference_forecast = reference_forecast
            if not math.isclose(
                    reference_forecast[0], native_rich_result.forecast_nll,
                    rel_tol=0.0, abs_tol=1e-12):
                raise RuntimeError(
                    "native decay forecast differs from Python oracle")
            expected_search_nll = (
                native_rich_result.winner.negative_log_fidelity
                + native_rich_result.forecast_nll)
            if not math.isclose(
                    expected_search_nll,
                    native_rich_result.search_negative_log_fidelity,
                    rel_tol=0.0, abs_tol=1e-12):
                raise RuntimeError(
                    "native search NLL does not equal current plus forecast")
            step_cache.evaluations = native_rich_result.evaluations
            step_cache.unique_evaluations = \
                native_rich_result.unique_evaluations
            step_cache.fitness_hits = native_rich_result.fitness_hits
            step_cache.decode_hits = native_rich_result.decode_hits
            step_cache.return_match_hits = native_rich_result.return_match_hits
            native_timing = dict(native_rich_result.timing)
            rich_delta = {
                "marshal_ns": int(native_timing.get("marshal_ns", 0)),
                "search_kernel_ns": int(
                    native_timing.get("search_kernel_ns", 0)),
                "fitness_ns": int(native_timing.get("fitness_ns", 0)),
                "selection_ns": int(native_timing.get("selection_ns", 0)),
                "native_parse_ns": int(
                    native_timing.get("native_parse_ns", 0)),
                "native_serialize_ns": int(
                    native_timing.get("native_serialize_ns", 0)),
                "calls": 1,
                "candidates": int(native_rich_result.evaluations),
            }
            for key, value in rich_delta.items():
                step_backend[key] += value
                self.boundary_backend_metrics[key] += value
            rich_search_stats = {
                "deterministic_unique_evaluations": int(
                    native_rich_result.deterministic_unique_evaluations),
                "stochastic_unique_evaluations": int(
                    native_rich_result.stochastic_unique_evaluations),
                "generations": int(native_rich_result.generations),
                "early_stopped": bool(native_rich_result.early_stopped),
                "early_stop_reason": str(
                    native_rich_result.early_stop_reason),
                "stochastic_budget": int(
                    native_rich_result.stochastic_budget),
                "operator_profile": self.operator_profile,
                "operator_stats": dict(
                    getattr(native_rich_result, "operator_stats", {}) or {}),
                "forecast_terms": len(rich_forecast_terms),
                "forecast_terms_applied": int(
                    native_rich_result.forecast_terms_applied),
                "forecast_terms_skipped_cutoff": int(
                    native_rich_result.forecast_terms_skipped_cutoff),
                "forecast_nll": float(native_rich_result.forecast_nll),
                "search_negative_log_fidelity": float(
                    native_rich_result.search_negative_log_fidelity),
            }
        seed_chromosomes = [
            list(native_rich_result.winner.chromosome)
            if native_rich_result is not None
            else normalize_chrom(matched_genes + [1] * len(eligible))]

        # Legacy adaptive M4 carries an explicit H=0 safety incumbent on the *same current
        # registry state*.  It is obtained once per boundary, outside fitness,
        # so this is not a nested GA and cannot read L+2 through a side channel.
        # Gate genes are first improved for the exact current transition; legacy H=2
        # may then toggle residency genes while those gate sites stay fixed.
        # The unrestricted legacy H=2 GA can replace this candidate only when its
        # current physical objective is non-inferior (see winner guard below).
        safe_chrom = list(seed_chromosomes[0])
        myopic_chrom = list(safe_chrom)
        if (not self.decay_lookahead
                and active_horizon > 0
                and self.ablation_policy == "optimize"):
            myopic_score = fitness(myopic_chrom, include_forecast=False)[0]
            for i in range(len(eligible)):
                trial = list(myopic_chrom)
                trial[n_gates + i] ^= 1
                trial = normalize_chrom(trial)
                trial_score = fitness(trial, include_forecast=False)[0]
                if trial_score < myopic_score:
                    myopic_chrom, myopic_score = trial, trial_score
            if n_gates:
                myopic_trials = max(
                    2, self.neighbor_sample_size // max(1, n_gates))
                for i, domain in enumerate(gate_domains):
                    if domain <= myopic_trials:
                        values = list(range(domain))
                    else:
                        values = sorted({
                            round(j * (domain - 1) / (myopic_trials - 1))
                            for j in range(myopic_trials)
                        } | {myopic_chrom[i]})
                    for value in values:
                        if value == myopic_chrom[i]:
                            continue
                        trial = list(myopic_chrom)
                        trial[i] = value
                        trial = normalize_chrom(trial)
                        trial_score = fitness(
                            trial, include_forecast=False)[0]
                        if trial_score < myopic_score:
                            myopic_chrom, myopic_score = trial, trial_score
                for i in range(len(eligible)):
                    trial = list(myopic_chrom)
                    trial[n_gates + i] ^= 1
                    trial = normalize_chrom(trial)
                    trial_score = fitness(
                        trial, include_forecast=False)[0]
                    if trial_score < myopic_score:
                        myopic_chrom, myopic_score = trial, trial_score
            safe_chrom = list(myopic_chrom)
            if not will_enumerate:
                safe_score = fitness(safe_chrom)[0]
                for i in range(len(eligible)):
                    trial = list(safe_chrom)
                    trial[n_gates + i] ^= 1
                    trial = normalize_chrom(trial)
                    trial_score = fitness(trial)[0]
                    if trial_score < safe_score:
                        safe_chrom, safe_score = trial, trial_score
                seed_chromosomes.append(list(safe_chrom))
        if native_rich_result is not None:
            greedy_chrom = list(native_rich_result.winner.chromosome)
            greedy_score = native_rich_result.search_objective
        else:
            greedy_score, greedy_chrom = min(
                (fitness(chromosome)[0], chromosome)
                for chromosome in seed_chromosomes)
        if native_rich_result is None and not will_enumerate:
            for i in range(len(eligible)):
                trial = list(greedy_chrom)
                trial[n_gates + i] ^= 1
                trial = normalize_chrom(trial)
                trial_score = fitness(trial)[0]
                if trial_score < greedy_score:
                    greedy_chrom, greedy_score = trial, trial_score

        # Joint physical-greedy gate seed.  Menu index zero is ordered by the
        # legacy sqrt-distance proxy and can be poor once transfer fidelity,
        # phase makespan, stationary coherence, and legacy terminal value are
        # evaluated together.  Spend a deterministic *per-layer* budget across
        # gate genes: narrow layers see most/all of each menu, while wide Ising
        # layers remain bounded instead of multiplying runtime by menu size.
        if native_rich_result is None and n_gates and not will_enumerate:
            trials_per_gate = max(
                2, self.neighbor_sample_size // max(1, n_gates))
            for i, domain in enumerate(gate_domains):
                if domain <= trials_per_gate:
                    values = list(range(domain))
                else:
                    values = sorted({
                        round(j * (domain - 1) / (trials_per_gate - 1))
                        for j in range(trials_per_gate)
                    } | {greedy_chrom[i]})
                best_value = greedy_chrom[i]
                best_score = greedy_score
                for value in values:
                    if value == greedy_chrom[i]:
                        continue
                    trial = list(greedy_chrom)
                    trial[i] = value
                    trial = normalize_chrom(trial)
                    trial_score = fitness(trial)[0]
                    if trial_score < best_score:
                        best_value, best_score = trial[i], trial_score
                greedy_chrom[i] = best_value
                greedy_score = best_score

            # Gate placement can change the true price of a RETURN subset.  One
            # deterministic toggle sweep closes that coupling without nesting
            # another GA or reading beyond ForecastOracle.
            for i in range(len(eligible)):
                trial = list(greedy_chrom)
                trial[n_gates + i] ^= 1
                trial = normalize_chrom(trial)
                trial_score = fitness(trial)[0]
                if trial_score < greedy_score:
                    greedy_chrom, greedy_score = trial, trial_score
        greedy = greedy_chrom[n_gates:]

        def score_unique(chromosomes, include_forecast=True):
            unique = {}
            for chromosome in chromosomes:
                key = tuple(normalize_chrom(chromosome))
                unique.setdefault(key, list(key))
            chromosomes = list(unique.values())
            objectives = fitness_many(
                chromosomes, include_forecast=include_forecast)
            scored = [(value[0], chromosome)
                      for value, chromosome in zip(objectives, chromosomes)]
            return sorted(scored, key=lambda item: (item[0], tuple(item[1])))

        def neighbor_with_rng(chrom, rng):
            result = list(chrom)
            if n_gates and (not eligible or rng.random() < 0.5):
                i = rng.randrange(n_gates)
                result[i] = rng.choice(
                    [result[i] + 1, result[i] - 1,
                     rng.randrange(gate_domains[i])])
            elif eligible:
                i = rng.randrange(len(eligible))
                result[n_gates + i] ^= 1
            return normalize_chrom(result)

        def neighbor(chrom):
            return neighbor_with_rng(chrom, self.rng)

        # ``myopic_chrom`` above is a deterministic current-transition safety
        # candidate.  Earlier legacy versions ran a second full H=0 GA here
        # before the registered H=2 GA.  That doubled the search budget and obscured the
        # horizon-only comparison with M3, and dominated large-circuit runtime.
        # The Pareto guard below still compares the horizon winner against the
        # deterministic H=0 incumbent, but M3 and M4 now each execute exactly
        # one stochastic GA per boundary.

        # Deterministic fast paths are shared by NL/LK.  ``state_key`` and its
        # optional cached elite were frozen before backend-specific search.
        search_mode = "ga"
        if native_rich_result is not None:
            scored = [(native_rich_result.search_objective,
                       list(native_rich_result.winner.chromosome))]
            search_mode = native_rich_result.search_mode
        elif cached_winner is not None:
            self.transition_cache.move_to_end(state_key)
            scored = score_unique([cached_winner])
            search_mode = "lru"
        else:
            search_space = direct_search_space
            if search_space <= 64:
                domains = [range(domain) for domain in gate_domains]
                decision_domain = (range(2) if self.ablation_policy == "optimize"
                                   else range(1))
                domains.extend((decision_domain for _ in eligible))
                chromosomes = product(*domains) if domains else [()]
                scored = score_unique(chromosomes)
                search_mode = "direct" if search_space <= 1 else "enumerate"
            else:
                population = build_seed_population(
                    gate_domains, len(eligible), greedy, self.population_size,
                    self.rng, normalize=normalize_chrom,
                    greedy_gate_genes=greedy_chrom[:n_gates])
                if (not self.decay_lookahead
                        and active_horizon > 0
                        and self.ablation_policy == "optimize"):
                    population.extend((list(safe_chrom), list(greedy_chrom)))
                scored = score_unique(population)[:self.population_size]
                for _ in range(self.iterations):
                    offspring = []
                    for _, chromosome in scored:
                        pool = score_unique(
                            neighbor(chromosome)
                            for _ in range(self.neighbor_sample_size))
                        offspring.extend(chromosome for _, chromosome
                                         in pool[:self.neighbors_per_solution])
                    scored = score_unique(
                        [chromosome for _, chromosome in scored] + offspring
                    )[:self.population_size]

        best_chrom = normalize_chrom(scored[0][1])
        lookahead_selection = "horizon"
        safe_current_objective = None
        horizon_current_objective = None
        if (not self.decay_lookahead
                and active_horizon > 0
                and self.ablation_policy == "optimize"):
            # Ensure the safety candidate participates even in an LRU or
            # truncated-population path, then apply a registered current-step
            # Pareto guard.  No completed-circuit score is observed here.
            # Preserve the horizon-selected residency pattern while projecting
            # only its gate genes onto the H=0 physical incumbent.  This keeps
            # the very mechanism being tested (cross-layer STAY) eligible for
            # the safety path instead of comparing it against all-RETURN.  The
            # decision bits remain byte-for-byte identical: this guard audits
            # forecast-induced gate displacement, not the lookahead decision.
            safe_chrom = normalize_chrom(
                list(myopic_chrom[:n_gates]) + list(best_chrom[n_gates:]))
            horizon_current_objective = fitness(
                best_chrom, include_forecast=False)[0]
            safe_current_objective = fitness(
                safe_chrom, include_forecast=False)[0]
            if (lookahead_selection == "horizon" and
                    horizon_current_objective[:4] > safe_current_objective[:4]):
                best_chrom = normalize_chrom(safe_chrom)
                lookahead_selection = "myopic_safety"
        cache_chrom = tuple(best_chrom)

        # Adjacent reuse/cycle relocation is a bounded lookahead-only extension
        # around the already selected resident-GA incumbent.  Keeping these bits
        # outside the stochastic chromosome preserves the exact H=0 search and
        # prevents a larger gene vector from degrading a no-cycle winner merely
        # by consuming a different RNG stream.  For each currently resident
        # target participant, test RETURN -> re-entry jointly with the gate-site
        # gene of its target gate.  This is the neutral-atom analogue of deciding
        # when a chain should advance the interaction site instead of pinning it.
        selected_cycles: set[int] = set(forced_cycle_candidates)
        cycle_objective = (
            native_rich_result.search_objective
            if native_rich_result is not None and not selected_cycles
            else fitness(best_chrom, cycle_returners=selected_cycles)[0])
        cycle_search_log = []
        # A rollout improvement smaller than one extra load+store fidelity pair
        # is not robust enough to justify changing the executable current state.
        # This physical hysteresis suppresses receding-horizon chattering without
        # introducing a tunable proxy weight.
        min_cycle_gain = -2.0 * math.log(physical.F_TRANSFER)
        if (not self.decay_lookahead
                and active_horizon > 0
                and self.ablation_policy == "optimize"):
            gate_of = {
                q: gate_index for gate_index, gate in enumerate(list_gate)
                for q in gate}
            for q in cycle_candidates:
                if q in forced_cycle_candidates:
                    cycle_search_log.append({
                        "q": q, "accepted": True, "forced": True,
                        "negative_log_fidelity_gain": None,
                        "gate_index": gate_of[q],
                        "gate_gene": best_chrom[gate_of[q]],
                    })
                    continue
                gate_index = gate_of[q]
                domain = gate_domains[gate_index]
                trial_budget = max(
                    2, self.neighbor_sample_size // max(1, len(list_gate)))
                if domain <= trial_budget:
                    values = list(range(domain))
                else:
                    values = sorted({
                        round(j * (domain - 1) / (trial_budget - 1))
                        for j in range(trial_budget)
                    } | {best_chrom[gate_index]})
                trial_cycles = set(selected_cycles)
                trial_cycles.add(q)
                local_score = cycle_objective
                local_chrom = list(best_chrom)
                for value in values:
                    trial = list(best_chrom)
                    trial[gate_index] = value
                    trial = normalize_chrom(trial)
                    score = fitness(
                        trial, cycle_returners=trial_cycles)[0]
                    if score < local_score:
                        local_score, local_chrom = score, trial
                candidate_gain = cycle_objective[0] - local_score[0]
                if candidate_gain > min_cycle_gain:
                    prior_objective = cycle_objective
                    best_chrom = normalize_chrom(local_chrom)
                    selected_cycles = trial_cycles
                    cycle_objective = local_score
                    lookahead_selection = "horizon_cycle"
                    cycle_search_log.append({
                        "q": q, "accepted": True,
                        "negative_log_fidelity_gain": (
                            prior_objective[0] - local_score[0]),
                        "gate_index": gate_index,
                        "gate_gene": best_chrom[gate_index],
                    })
                else:
                    cycle_search_log.append({
                        "q": q, "accepted": False,
                        "negative_log_fidelity_gain": max(0.0, candidate_gain),
                        "gate_index": gate_index,
                        "gate_gene": best_chrom[gate_index],
                    })

        search_kernel_ns = time.perf_counter_ns() - search_kernel_started_ns

        self.transition_cache[state_key] = cache_chrom
        self.transition_cache.move_to_end(state_key)
        while len(self.transition_cache) > self.transition_cache_limit:
            self.transition_cache.popitem(last=False)
        placed = decode(best_chrom)
        if native_rich_result is not None:
            replayed_option_indices = tuple(
                candidates[column].index(chosen)
                for column, chosen in enumerate(placed))
            if replayed_option_indices != tuple(
                    native_rich_result.gate_option_indices):
                raise RuntimeError(
                    "native rich gate decode disagrees with Python replay")
        bits = best_chrom[n_gates:]
        returners = ({q for q, bit in zip(eligible, bits) if bit}
                     | selected_cycles)
        sites = (dict(native_return_sites)
                 if native_rich_result is not None
                 else return_sites_for_atoms(returners))
        if set(sites) != returners:
            raise RuntimeError(
                "native rich RETURN site set disagrees with selected returners")
        decisions = {
            q: (("RETURN", sites[q]) if bit else ("STAY", reg.zone_seat[q]))
            for q, bit in zip(eligible, bits)
        }
        decisions.update({q: ("RETURN", sites[q]) for q in selected_cycles})
        vacated = {q for q, value in decisions.items()
                   if value[0] in ("RETURN", "RESEAT")}
        decoded_placements = [
            self._mk_placement(c[2], c[3], c[0]) for c in placed]
        placements = self._repair_placements(
            deepcopy(decoded_placements), list_gate, vacated=vacated)
        placement_repaired = placements != decoded_placements
        if sum(1 for value in decisions.values() if value[0] == "STAY") + demand \
                > self.theta_capacity * reg.zone_sites + 1e-12:
            raise AssertionError("Schema 2 容量约束未在 fitness 前满足")

        # Hard repair is method-independent.  The repaired schedule is scored again
        # below, so the ledger never reports the stale pre-repair objective.
        pre_ghost_placements = deepcopy(placements)
        pre_ghost_decisions = dict(decisions)
        commitment_repairs, commitment_ghost_fallback = \
            self._repair_ghosts_with_commitments(
                placements, decisions, target_pins)
        physical_repair_applied = (
            placement_repaired
            or placements != pre_ghost_placements
            or decisions != pre_ghost_decisions
            or bool(commitment_repairs)
            or commitment_ghost_fallback)

        selected_forecast_audit = {}

        def score_repaired():
            nonlocal selected_forecast_audit
            positions_t0 = {
                q: reg.current_pos(q) for q in range(len(self.mapping[0]))}
            positions_t1 = dict(positions_t0)
            back, back_owners = [], []
            for q, (kind, loc) in decisions.items():
                if kind == "STAY":
                    continue
                p0 = arch.exact_SLM_location_tuple(positions_t0[q])
                p1 = arch.exact_SLM_location_tuple(loc)
                distance = math.dist(p0, p1)
                if distance > 1e-9:
                    back.append((distance, *p0, *p1))
                    back_owners.append(q)
                positions_t1[q] = loc
            out, out_owners = [], []
            for placement in placements:
                for q, seat in zip(placement["gate"], placement["seats"]):
                    p0 = arch.exact_SLM_location_tuple(positions_t1[q])
                    p1 = arch.exact_SLM_location_tuple(seat)
                    distance = math.dist(p0, p1)
                    if distance > 1e-9:
                        out.append((distance, *p0, *p1))
                        out_owners.append(q)
            ghosts_t0 = [(q, *arch.exact_SLM_location_tuple(loc))
                          for q, loc in positions_t0.items()]
            ghosts_t1 = [(q, *arch.exact_SLM_location_tuple(loc))
                          for q, loc in positions_t1.items()]
            if self.ablation_fitness_mode == "lumped_greedy":
                phases = [movement_phase(
                    back + out, owners=back_owners + out_owners,
                    batching="greedy")]
            else:
                phases = [
                    movement_phase(back, ghosts_t0, back_owners),
                    movement_phase(out, ghosts_t1, out_owners),
                ]
            returners = {q for q, value in decisions.items()
                         if value[0] == "RETURN"}
            repaired_sites = {q: decisions[q][1] for q in returners}
            current_phases = tuple(phases)
            current_exposures = sum(
                1 for q in eligible
                if q not in participants and decisions[q][0] != "RETURN")
            if self.decay_lookahead:
                repaired_gate_indices = []
                for column, placement in enumerate(placements):
                    site = tuple(placement["site"])
                    repaired_gate_indices.append(next(
                        (index for index, option in enumerate(candidates[column])
                         if tuple(option[0]) == site), -1))
                repaired_bits = tuple(
                    1 if decisions[q][0] == "RETURN" else 0
                    for q in eligible)
                repaired_chromosome = (
                    tuple(repaired_gate_indices) + repaired_bits)
                return_assignments = tuple(
                    (q, self.boundary_storage_site_id[
                        tuple(decisions[q][1])])
                    for q in sorted(returners))
                objective, current_breakdown, selected_forecast_audit = \
                    score_decay_objective(
                        current_phases, current_exposures,
                        repaired_chromosome, repaired_gate_indices,
                        return_assignments)
                return objective, current_breakdown
            future, future_exposures, forecast_terms = forecast_phases(
                returners, repaired_sites, placements)
            phases.extend(future)
            return physical.score(
                phases, current_exposures + future_exposures,
                tuple(best_chrom) + tuple(
                    1 if q in selected_cycles else 0
                    for q in cycle_candidates))

        if physical_repair_applied:
            # A changed executable state must be replayed and rescored; no
            # stale chromosome fitness may enter the evidence ledger.
            repaired_score, breakdown = score_repaired()
        elif native_rich_result is not None:
            repaired_score, breakdown = score_from_backend(
                native_rich_result.winner)
        else:
            # The chromosome score already used the exact same back/out phases,
            # rollout and terminal state.  Re-evaluating it once per boundary
            # duplicated millions of legacy rollouts on long circuits.  The
            # reference cache returns the objective and physical breakdown.
            repaired_score, breakdown = fitness(
                best_chrom, cycle_returners=selected_cycles)
        if self.decay_lookahead and not selected_forecast_audit:
            if native_rich_result is not None:
                selected_forecast_audit = {
                    "configured_depth": self.lookahead_horizon,
                    "effective_depth": forecast.effective_horizon,
                    "rho": float(self.lookahead_horizon_config["rho"]),
                    "epsilon": float(self.lookahead_horizon_config["epsilon"]),
                    "alpha_lookahead": self.alpha_lookahead,
                    "forecast_by_depth": list(
                        native_rich_result.forecast_by_depth),
                    "forecast_breakdown": dict(
                        native_rich_result.forecast_breakdown),
                    "forecast_terms_applied": int(
                        native_reference_forecast[3]),
                    "forecast_terms_skipped_cutoff": int(
                        native_reference_forecast[4]),
                    "weighted_negative_log_fidelity": float(
                        native_rich_result.forecast_nll),
                    "search_negative_log_fidelity": float(
                        native_rich_result.search_negative_log_fidelity),
                }
            else:
                forecast_key = tuple(best_chrom) + tuple(
                    1 if q in selected_cycles else 0
                    for q in cycle_candidates)
                selected_forecast_audit = deepcopy(
                    forecast_audit_cache.get(forecast_key, {}))
        if self.decay_lookahead:
            # ``score`` is executable current-boundary physics only.  The
            # non-executable forecast contribution is audited separately and
            # never leaks into the final fidelity decomposition.
            repaired_score = breakdown.objective(tuple(best_chrom))
        for field in step_cache.as_dict():
            setattr(self.cache_stats, field,
                    getattr(self.cache_stats, field) + getattr(step_cache, field))

        for q, (kind, loc) in decisions.items():
            if kind == "RETURN":
                self.residency_commitments.pop(q, None)
                reg.return_to_storage(q, loc)
            elif kind == "RESEAT":
                self.residency_commitments.pop(q, None)
                reg.reseat(q, loc)
            elif kind == "STAY" and active_horizon > 0:
                visible = visible_use(q)
                if visible is None:
                    self.residency_commitments.pop(q, None)
                else:
                    self.residency_commitments[q] = (
                        visible[0], tuple(reg.zone_seat[q]))
            elif kind == "STAY":
                # A boundary that selected H=0 must not keep a commitment made
                # by an earlier deeper window as a hidden future-information
                # channel.  The atom may still physically stay, but uncommitted.
                self.residency_commitments.pop(q, None)
        self._append_boundary(decisions)
        self._commit_round(next_layer, placements)
        for q in participants:
            commitment = self.residency_commitments.get(q)
            if commitment is not None and commitment[0] <= next_layer:
                self.residency_commitments.pop(q, None)
        if self.decay_lookahead:
            selected_forecast_audit = {
                "configured_depth": self.lookahead_horizon,
                "effective_depth": forecast.effective_horizon,
                "visible_depth": max(
                    (int(offset) for offset in future_atoms), default=0),
                "rho": float(self.lookahead_horizon_config["rho"]),
                "epsilon": float(self.lookahead_horizon_config["epsilon"]),
                "alpha_lookahead": self.alpha_lookahead,
                "offset_weights": [
                    {"offset": offset,
                     "decay_factor": forecast.future_decay_factor(offset),
                     "weight": forecast.future_weight(offset)}
                    for offset in range(1, forecast.effective_horizon + 1)
                ],
                "forecast_by_depth": selected_forecast_audit.get(
                    "forecast_by_depth", []),
                "forecast_breakdown": selected_forecast_audit.get(
                    "forecast_breakdown", {}),
                "forecast_terms_applied": selected_forecast_audit.get(
                    "forecast_terms_applied", 0),
                "forecast_terms_skipped_cutoff": selected_forecast_audit.get(
                    "forecast_terms_skipped_cutoff", 0),
                "weighted_negative_log_fidelity": selected_forecast_audit.get(
                    "weighted_negative_log_fidelity", 0.0),
                "search_negative_log_fidelity": selected_forecast_audit.get(
                    "search_negative_log_fidelity",
                    breakdown.negative_log_fidelity
                    + selected_forecast_audit.get(
                        "weighted_negative_log_fidelity", 0.0)),
            }
        self.backend_timing_log.append({
            "layer": layer,
            "backend": getattr(
                self.boundary_backend, "name", self.resident_backend_requested),
            **step_backend,
        })
        self.decision_log.append({
            "layer": layer,
            "engine": "ga-v2",
            "method_id": self.method_id,
            "lookahead_horizon": active_horizon,
            "configured_lookahead_horizon": self.lookahead_horizon_config,
            **horizon_decision.as_log(),
            "horizon_selection_ns": horizon_selection_ns,
            "search_kernel_ns": search_kernel_ns,
            "backend": getattr(
                self.boundary_backend, "name", self.resident_backend_requested),
            "backend_calls": step_backend["calls"],
            "backend_candidates": step_backend["candidates"],
            "ablation_policy": self.ablation_policy,
            "fitness_phase_mode": self.ablation_fitness_mode,
            "search_mode": search_mode,
            "rich_search": rich_search_stats,
            "lookahead_selection": lookahead_selection,
            "forecast_objective": selected_forecast_audit,
            "safe_current_objective": (
                list(safe_current_objective[:4])
                if safe_current_objective is not None else None),
            "horizon_current_objective": (
                list(horizon_current_objective[:4])
                if horizon_current_objective is not None else None),
            "stay": sum(1 for v in decisions.values() if v[0] == "STAY"),
            "return": sum(1 for v in decisions.values() if v[0] == "RETURN"),
            "reseat": sum(1 for v in decisions.values() if v[0] == "RESEAT"),
            "eligible_decisions": len(eligible),
            "adjacent_cycle_candidates": len(cycle_candidates),
            "adjacent_cycle_returns": len(selected_cycles),
            "forced_commitment_cycles": len(forced_cycle_candidates),
            "adjacent_cycle_search": cycle_search_log,
            "no_visible_use": sum(
                1 for q in eligible
                if visible_use(q) is None),
            "forced_e2": 0,
            "capacity": min_returns,
            "horizon_guard_returns": len(horizon_forced_returns),
            "physical_guard_returns": len(physical_forced_returns),
            "physical_guard": physical_guard_details,
            "committed_target_atoms": len(target_pins),
            "commitment_ghost_fallback": commitment_ghost_fallback,
            "commitment_repairs": [
                {"q": q, "pinned_seat": list(target_pins[q]),
                 "repaired_seat": list(commitment_repairs[q])}
                for q in sorted(commitment_repairs)],
            "active_commitments": len(self.residency_commitments),
            "ghost_fix": getattr(self, "ghost_fixes", 0),
            "participants": len(participants),
            "score": [repaired_score[0], repaired_score[1],
                      repaired_score[2], repaired_score[3]],
            "physical": {
                "negative_log_fidelity": breakdown.negative_log_fidelity,
                "idle_exposures": breakdown.idle_exposures,
                "transfers": breakdown.transfers,
                "move_batches": breakdown.move_batches,
                "move_time_us": breakdown.move_time_us,
                "total_distance_um": breakdown.total_distance_um,
            },
            "cache": step_cache.as_dict(),
        })
        while len(self.return_candidate_cache) > self.return_cache_limit:
            self.return_candidate_cache.popitem(last=False)
        while len(self.rollout_pair_cache) > self.rollout_geometry_cache_limit:
            self.rollout_pair_cache.popitem(last=False)
        while len(self.rollout_site_cache) > self.rollout_geometry_cache_limit:
            self.rollout_site_cache.popitem(last=False)
        self.search_time += time.time() - t0
