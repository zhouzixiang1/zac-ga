"""发动机：FablePlacer —— 把 Fable 笔记里的"2q放置优化"装上 ZAC 底盘。

与 GA/zga/gaplacer.py（v1a）的关系：底盘完全相同（继承关系、候选生成、
解码器、进化循环、落盘格式全部一致），只换两样东西——恰好就是笔记
（3）2q放置优化 里说的两样：

    ① 评价函数（打分仪表换成 fcost.py）
        v1a:  Σ√dmax（ICCAD 分组模型，"车能并就并"的近似）
        本版: 最大链 + 冲突边×权重（逐行预演 ZAC 排车器：
              compatible_2D 冲突边 + 贪心 MIS 分轮的串行链）
    ② 邻域算子（变异换成结构化算子）
        v1a:  基因 ±1 / 随机跳（在"距离排序菜单"上瞎挪）
        本版: "交换位置"（两件活互换工位）+ "邻居位置"（挪到隔壁工位）
              —— 直接在【座位表】层面构造邻居解，改动一次生效两件活

继承关系（与 v1a 相同，底盘不动）：
    FablePlacer(VertexMatchingPlacer)
      ├─ 覆写：place_gate()          ← 唯一改动的行为：给下一批的活分工位
      └─ 原样继承：run()（每层编舞）、place_qubit()（分床位）、
                   filter_mapping()（复用终审两版择优）

place_gate 内部流程（对应工厂故事）：
    1. 查复用名单 → 记下"谁的下批搭档是谁"（前瞻对象）
    2. 生成"点菜单"：每件活的候选工位列表（与 v1a 逐字节相同，
       保证两组实验的搜索空间一致，差异 100% 归因于①②）
    3. 解码器：染色体(一串菜单序号) → 合法座位表（撞工位就顺延）
    4. 打分：fcost 的"排车预演"算最大链+冲突边，另加复用前瞻
    5. 进化：6 张方案 × 8 轮 × 每张采 24 个邻居留 2 个，精英保留
    6. 落盘：左右座位分配（与 ZAC 原版逐字节兼容的输出格式）
"""
from __future__ import annotations

import math
import random
import time
from copy import deepcopy
from math import sqrt

# 引用本包的打分仪表；fable/__init__.py 已把 FABLE 根目录挂上 sys.path，
# 所以下面的 zac.* 全部解析到 FABLE/zac/（本地副本）。
from zac.placer.vmplacer import VertexMatchingPlacer

from fable.fcost import stage_decompose


class FablePlacer(VertexMatchingPlacer):
    """VertexMatchingPlacer 的子类：门放置改为"Fable 式局部搜索"。

    参数默认值 = Fable 笔记里验证过的参数（population=6, iterations=8,
    neighbors=2, sample=24），与 v1a 完全一致——预算不变，只换花法。
    """

    def __init__(self, mapping: list, l2: bool = False, seed: int = 0, **params):
        super().__init__(mapping, l2)
        self.rng = random.Random(seed)   # 独立随机源，种子固定 → 结果可复现
        # ---- 搜索旋钮（默认 = 笔记参数，与 v1a 相同的预算）----
        self.population_size: int = params.get("population_size", 6)
        self.iterations: int = params.get("iterations", 8)
        self.neighbors_per_solution: int = params.get("neighbors_per_solution", 2)
        self.neighbor_sample_size: int = params.get("neighbor_sample_size", 24)
        # ---- Fable 评估函数旋钮 ----
        # w_conf 默认 0.25：qft_n29 消融标定（0→0.88×，0.25→0.79×，1→1.17×，
        # 3→1.18×）——冲突边要给"梯度"但不能压过距离项
        self.w_conf: float = params.get("w_conf", 0.25)
        self.use_lookahead: bool = params.get("use_lookahead", True)  # 复用前瞻项开关
        # 候选窗口下限：每层门少时窗口公式会给 1（太窄，串行链式电路如 qft
        # 的下一层搭档常常在窗口外）——此旋钮把窗口垫高一点
        self.min_expand: int = params.get("min_expand", 1)
        self.search_time = 0.0  # 纯搜索耗时（报告用）

    # ------------------------------------------------------------------ utils
    def _norm_site(self, location):
        """把纠缠区内的陷阱坐标归一化到该区第一个 SLM 的坐标系。

        纠缠区有两个 SLM，同坐标 (slm1,r,c) 与 (slm2,r,c) 是同一个
        Rydberg 工位的左、右两座。统一到 slm1 坐标系后，"同一个工位"
        才有唯一的键值（否则左右座会被当成两个工位）。
        """
        slm_idx = self.architecture.entanglement_zone[
            self.architecture.dict_SLM[location[0]].entanglement_id][0]
        return (slm_idx, location[1], location[2])

    def place_gate(self, list_qubit_mapping: list, list_two_gate_layer: list,
                   layer: int, test_reuse: bool):
        """给第 layer 层的门分配纠缠区工位 —— 被替换的唯一方法（契约同父类）。"""
        list_gate = list_two_gate_layer[0]

        # ---- 第 1 步：备好前瞻对象 --------------------------------------
        # 复用名单 self.list_reuse_qubit[layer] = 本层做完要留在工位上的比特。
        # 它们在下一层的搭档(门友)将来必须搬到这个工位旁边 → 打分时要算这笔账。
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

        # 两张位置表（沿用父类约定）：
        #   gate_mapping  = 上一层门时刻位置（复用比特现在坐在哪个工位）
        #   qubit_mapping = 当前存储态位置（普通比特现在住在哪个床位；
        #                   复用比特的家已经更新为它所在的工位——ZAC 复用
        #                   提交后"家"就搬进了车间，这正是笔记"不调回"的对应物）

        # ---- 第 2 步：生成"点菜单"（与 v1a 逐字节相同）------------------
        expand_factor = max(self.min_expand, math.ceil(math.sqrt(len(list_gate)) / 2))
        candidates: list[list] = []   # 每件门一个候选表；表项=(工位, d1, d2, 前瞻距离, q1, q2)
        for gate in list_gate:
            q1, q2 = gate
            pinned = test_reuse and layer > 0 and q1 in self.list_reuse_qubit[layer - 1]
            if not pinned and test_reuse and layer > 0 and q2 in self.list_reuse_qubit[layer - 1]:
                pinned, side = True, 2      # q2 是留下的那个
            elif pinned:
                side = 1                    # q1 是留下的那个
            else:
                side = 0                    # 普通门，无钉死

            if pinned:
                # 复用门：比特已坐在工位上，这件活必须原地做 → 菜单只有 1 道菜。
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

            # 普通门：锚点(两比特中间位置最近的工位) + 周围 ±expand_factor 的窗口
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

        # 菜单位置索引：工位 → 该菜单里的序号（"交换位置"算子要用它
        # 把"换座位"翻译回"改基因"，让解码器自然保证合法性）
        site_index = [{o[0]: k for k, o in enumerate(opts)} for opts in candidates]
        movable = [gi for gi, opts in enumerate(candidates) if len(opts) > 1]

        # ---- 第 3 步：解码器（染色体 → 合法座位表；与 v1a 相同）----------
        def decode(chrom):
            """食堂打饭规则：每件门按基因选菜；被占就沿菜单顺延到下一道。"""
            used = {}
            placed = []
            for gi, opts in enumerate(candidates):
                if len(opts) == 1:          # 复用门：菜单只有一道菜，基因无效
                    placed.append(opts[0])
                    continue
                idx = chrom[gi] % len(opts)
                for step in range(len(opts)):
                    cand = opts[(idx + step) % len(opts)]
                    if cand[0] not in used:
                        break               # 找到第一个没被占的工位
                used[cand[0]] = gi
                placed.append(cand)
            return placed

        # ---- 第 4 步：适应度（Fable 式：排车预演 = 最大链 + 冲突边）------
        def seats(site, q1, q2):
            """这件门两个比特各自的真实座位（打分与落盘同一套规则）。

            复用门：留下的比特坐原位（可能在左座也可能右座），搭档坐
            旁边空位；普通门：存储列号小的进左陷阱（父类同款规则）。
            """
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

        def fitness(placed):
            # 4a. 按真实座位收集搬运腿 (dist, 起x, 起y, 终x, 终y)；dist=0 不占车
            legs = []
            for (site, d1, d2, dis3, q1, q2) in placed:
                s1, s2 = seats(site, q1, q2)
                for q, tgt in ((q1, s1), (q2, s2)):
                    sx, sy = self.architecture.exact_SLM_location_tuple(qubit_mapping[q])
                    tx, ty = self.architecture.exact_SLM_location_tuple(tgt)
                    d = math.dist((sx, sy), (tx, ty))
                    if d > 1e-9:
                        legs.append((d, sx, sy, tx, ty))
            # 4b. 排车预演：最大链（串行轮次的 Σ√最长腿）+ 冲突边×身价
            chain_cost, n_conflicts = stage_decompose(legs)
            cost = chain_cost + self.w_conf * n_conflicts
            # 4c. 复用前瞻：留下的比特的下批搭档要赶来的路费（足额计入）
            if self.use_lookahead:
                cost += sum(sqrt(o[3]) for o in placed)
            return cost

        # ---- 邻域算子（Fable 结构化算子：交换位置 / 邻居位置）------------
        def neighbor_solution(chrom):
            """生成一个邻居解——笔记里的两种"邻居解点"各一半概率。

            交换位置：两件活互换工位。直接改座位表容易破合法性和局部性，
            这里翻译成基因语言：各自点对方现在那道菜（点得到才换，
            点不到说明对方工位不在自己的候选窗口里 → 退化成邻居挪动）。
            邻居位置：一件活的基因 ±1（挪到按路程排序的隔壁工位）或
            随机跳（保留一点远距离探索，防困死在局部）。
            """
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

        # ---- 第 5 步：进化循环（(μ+λ) 精英保留；与 v1a 相同）--------------
        t0 = time.time()
        zeros = [0] * len(candidates)          # "老实人方案"：全 0 = 每件活选最近
        population = [zeros]
        for _ in range(self.population_size - 1):
            population.append([self.rng.randrange(8) for _ in candidates])
        scored = [(fitness(decode(c)), c) for c in population]
        scored.sort(key=lambda x: x[0])
        for _ in range(self.iterations):
            offspring = []
            for _, c in scored:  # 每个父代独立采邻域 → 各留最好的 2 个子代
                pool = [neighbor_solution(c) for _ in range(self.neighbor_sample_size)]
                pool_scored = sorted((fitness(decode(m)), m) for m in pool)
                offspring += pool_scored[: self.neighbors_per_solution]
            scored = sorted(scored + offspring, key=lambda x: x[0])[: self.population_size]
        best = decode(scored[0][1])
        self.search_time += time.time() - t0

        # ---- 第 6 步：落盘（与 v1a/ZAC 输出逐字节兼容，下游无感知）-------
        tmp_mapping = deepcopy(qubit_mapping)
        for (site, d1, d2, dis3, q1, q2) in best:
            reuse1 = test_reuse and layer > 0 and q1 in self.list_reuse_qubit[layer - 1]
            reuse2 = test_reuse and layer > 0 and q2 in self.list_reuse_qubit[layer - 1]
            if reuse1:
                tmp_mapping[q1] = gate_mapping[q1]
                if site == gate_mapping[q1]:
                    tmp_mapping[q2] = (site[0] + 1, site[1], site[2])   # q1 左座→q2 右座
                else:
                    tmp_mapping[q2] = site                               # q1 右座→q2 左座
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
