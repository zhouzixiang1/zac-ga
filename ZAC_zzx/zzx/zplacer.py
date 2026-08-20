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
from copy import deepcopy
from math import sqrt

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import min_weight_full_bipartite_matching

# 引用本包的打分仪表；zzx/__init__.py 已把 ZAC_zzx 根目录挂上 sys.path，
# 所以下面的 zac.* 解析到 ZAC_zzx/zac/（本地副本）。
from zac.placer.vmplacer import VertexMatchingPlacer

from zzx.zcost import batch_cost, compatible_2d, conflict_graph
from zzx.ghost import (ghost_hits, hit_count, leg_hits, new_conflicts,
                       pair_edges)
from zzx.resident import (NextUse, ResidentRegistry, boundary_legs,
                          decide_lazy, match_return_sites)


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
        self.search_time = 0.0
        self.decision_log: list = []                # 每层决策统计（供账本/实验）
        self.registry = None
        self.nu = None

    # ------------------------------------------------------------------ 轮循环
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
        self.architecture = architecture
        self.gate_scheduling = gate_scheduling
        # 中和复用机制（双保险：ZAC_zzx 层已置空 self.reuse_qubit）。
        # 不动 dynamic_placement/reuse 旗标——路由回程分支判定还用它们。
        self.list_reuse_qubit = [set() for _ in gate_scheduling]
        self.mapping = [list(qubit_mapping[0])]
        self.registry = ResidentRegistry(architecture, qubit_mapping[0],
                                         self.theta_capacity)
        self.nu = NextUse(gate_scheduling)
        n = len(gate_scheduling)

        placement = self._plan_round(0)
        self._repair_ghosts(placement, {})       # 第 0 轮入场也过鬼点防线
        self._commit_round(0, placement)
        for layer in range(n):
            if layer + 1 < n:
                if self.engine == "ga":
                    # A3：边界决策 + 下一轮门位联合搜索（一步 GA 两张映射一起提交）
                    self._ga_step(layer)
                    continue
                placement = self._plan_round(layer + 1)   # 门赢：先定门位，闲人让路
                next_gates = self.gate_scheduling[layer + 1]
                next_seats = [p["seats"] for p in placement]
            else:
                # 末边界：没有下一轮门位 → 全员 STAY（native 同款），
                # 或 final_return_home=True 时全队回存储（实验开关）
                placement, next_gates, next_seats = None, [], []
            decisions, stats = decide_lazy(
                self.registry, self.nu, layer, next_gates, next_seats,
                final_return_home=self.final_return_home and layer == n - 1)
            self._repair_ghosts(placement or [], decisions)   # 防线③（match 引擎同享）
            self._append_boundary(decisions)
            self.decision_log.append({"layer": layer, **stats,
                                      "ghost_fix": getattr(self, "ghost_fixes", 0)})
            if placement is not None:
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
                data.append(w)
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

    def _repair_placements(self, placements: list, list_gate: list) -> list:
        """最终安全网：门位不得与其他原子当前座位或其他门已选工位重叠。

        菜单三级兜底在极端拥挤轮次仍可能放过个别工位（qft 宽层实证：
        原子 6 落到参与者原子 5 的座位上，E2 时序救不回来）。这里在选择
        完成后按不变式逐门复查、违例即改选全区过滤域最优工位——正确性
        不再依赖任何菜单层级的表现。
        """
        reg = self.registry
        participants = {q for gate in list_gate for q in gate}
        used = {p["site"] for p in placements}
        out = []
        for p in placements:
            q1, q2 = p["gate"]
            own = {reg.zone_seat.get(q) for q in (q1, q2)}    # 自己的座位可保留
            others = {seat for q, seat in reg.zone_seat.items()
                      if q != q1 and q != q2}
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

    def _repair_ghosts(self, placements, decisions):
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
                        participants, t1, t2, both, gate_of, n_q, banned):
                    fixed_total += 1
                    progress = True
            if not progress:
                break
        residual = leg_issues()
        raise RuntimeError(
            f"鬼点硬保证层修补失败：{len(residual)} 处单腿自撞残留 "
            f"(首批: {residual[:3]})——请检查修补策略覆盖度")

    def _fix_leg_ghost(self, issue, placements, decisions, participants,
                       t1, t2, both, gate_of, n_q, banned):
        """修一个单腿自撞问题，返回是否动手。按代价从小到大：

        ① 受害者是驻留非参与者(STAY) → 让座 RESEAT（恒可解兜底）
        ② 腿主是决策(RETURN/RESEAT) → 换落位
        ③ 腿主是参与者 → 改门位
        """
        phase, owner, leg, gid, gx, gy = issue
        reg, arch = self.registry, self.architecture
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
            for site in sorted(pool, key=lambda s: math.dist(
                    (sx, sy), ex(site))):
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
                ban.add(loc)
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
            others_seats = {seat for qq, seat in reg.zone_seat.items()
                            if qq != q1 and qq != q2}
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
