"""发动机：GAPlacer —— 用遗传搜索替换 ZAC 的门放置（v1a 版）。

继承关系（本文件的核心设计）：
    GAPlacer(VertexMatchingPlacer)
      ├─ 覆写：place_gate()          ← 唯一改动的行为：给下一批的活分工位
      └─ 原样继承：
           run()               每层的编舞：排"都回家"和"按名单留人"两版方案
           place_qubit()       分床位（回家的工人住哪，加权匹配）
           filter_mapping()    终审：两版方案算总账择优，输家名单作废
    这样"换发动机、底盘不动"：GA 与 ZAC 的差异 100% 归因于放置搜索本身。

place_gate 内部流程（对应工厂故事）：
    1. 查复用名单 → 记下"谁的下批搭档是谁"（前瞻对象）
    2. 生成"点菜单"：每件活的候选工位列表（复用门只 1 道菜；普通门
       按"锚点+窗口+路程排序"生成一列，第 0 个永远最近）
    3. 解码器：染色体(一串菜单序号) → 合法座位表（撞工位就顺延，
       像食堂打饭：想要的菜没了就点下一道）
    4. 打分：调 racost.py 的"快速排车模拟器"算总耗时 + 搭档前瞻
    5. 进化：6 张方案 × 8 轮 × 每张采 24 个变异邻居留 2 个，精英保留
    6. 落盘：左右座位分配（与 ZAC 原版逐字节兼容的输出格式）

染色体/变异通俗版见本文件注释；物理背景见 racost.py 文件头。
"""
from __future__ import annotations

import math
import random
import sys
import time
from copy import deepcopy
from math import sqrt

# 引用本包的打分仪表；zga/__init__.py 已把 GA 根目录挂上 sys.path，
# 所以下面的 zac.* 全部解析到 GA/zac/（本地副本）。
from zac.placer.vmplacer import VertexMatchingPlacer

from zga.racost import add_to_groups, discretize, groups_cost, groups_sd


class GAPlacer(VertexMatchingPlacer):
    """VertexMatchingPlacer 的子类：仅门放置改为遗传搜索（v1a：逐层、保留 ZAC 复用管线）。"""

    def __init__(self, mapping: list, l2: bool = False, seed: int = 0, **ga_params):
        # mapping: SA 初始布局（全体工人的家），父类需要它做兜底候选
        super().__init__(mapping, l2)
        self.rng = random.Random(seed)   # 独立随机源，种子固定 → 结果可复现
        # ---- GA 旋钮（默认值 = Fable 笔记里验证过的参数）----
        self.population_size: int = ga_params.get("population_size", 6)          # 种群规模
        self.iterations: int = ga_params.get("iterations", 8)                    # 进化轮数
        self.neighbors_per_solution: int = ga_params.get("neighbors_per_solution", 2)  # 每父代留几个子代
        self.neighbor_sample_size: int = ga_params.get("neighbor_sample_size", 24)    # 每父代采样几个变异
        self.use_sd: bool = ga_params.get("use_sd", False)  # 消融开关：是否给打分加"队形歪扭度"
        self.ga_time = 0.0  # 纯 GA 搜索的累计耗时（报告用，区别于整道工序的墙钟时间）

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
        """给第 layer 层的门分配纠缠区工位 —— 被 GA 替换的唯一方法。

        参数（与父类契约完全一致，父类的 run() 会按编舞调用它）：
            list_qubit_mapping  [门时刻位置表, 存储位置表]（layer>0 时）
            list_two_gate_layer [本层门列表, 下一层门列表]——后者专供前瞻
            test_reuse          本轮是否按"有复用"放置（双世界各跑一次）
        """
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

        # 两张位置表（沿用父类约定）：
        #   gate_mapping  = 上一层门时刻位置（复用比特现在坐在哪个工位）
        #   qubit_mapping = 当前存储态位置（普通比特现在住在哪个床位）
        if layer > 0:
            gate_mapping = list_qubit_mapping[0]
            qubit_mapping = list_qubit_mapping[1]
        else:
            gate_mapping = None
            qubit_mapping = list_qubit_mapping[0]

        # ---- 第 2 步：生成"点菜单" --------------------------------------
        # 窗口大小随门数增长：门越多需要同时占用越大的车间区域
        expand_factor = math.ceil(math.sqrt(len(list_gate)) / 2)
        candidates: list[list] = []   # 每件门一个候选表；表项=(工位, d1, d2, 前瞻距离, q1, q2)
        for gate in list_gate:
            q1, q2 = gate
            # 判断这件门是否含"被钉死"的复用比特（沿用父类判定：查上一层的名单）
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
                # 搭档从宿舍赶来这个工位的距离（它的搬运腿）
                d_other = self.architecture.distance(
                    qubit_mapping[other][0], qubit_mapping[other][1], qubit_mapping[other][2],
                    site[0], site[1], site[2])
                # 前瞻：如果这个复用比特下一层还要留，其下下层搭档的距离也算
                q_r = q1 if side == 1 else q2
                dis3 = 0.0
                if q_r in dict_reuse_neighbor:
                    q3 = dict_reuse_neighbor[q_r]
                    dis3 = self.architecture.distance(
                        qubit_mapping[q3][0], qubit_mapping[q3][1], qubit_mapping[q3][2],
                        site[0], site[1], site[2])
                candidates.append([(site, 0.0, d_other, dis3, q1, q2)])  # 留下的那条腿距离=0
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

            # 计算窗口内每个工位的两条搬运腿距离 + 前瞻，然后按总路程升序排列。
            # "第 0 个 = 最近" 是染色体局部性的来源：基因 ±1 = 挪到隔壁工位。
            opts = []
            for site in set_sites:
                # 左右座分配：存储位置列号小的进左陷阱（减少路径交叉），沿用父类规则
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
                # 前瞻：这件门若有比特会被继续复用，其下一层搭档到目标座的距离
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

        # ---- 第 3 步：解码器（染色体 → 合法座位表） ----------------------
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

        # ---- 第 4 步：适应度（调 racost 的"快速排车模拟器"） ------------
        def fitness(placed):
            # 4a. 收集每条搬动腿的源/目标坐标（用于离散化和分组）
            src_r, src_c, tgt_r, tgt_c = [], [], [], []
            for site, d1, d2, dis3, q1, q2 in placed:
                for q, tgt in ((q1, site), (q2, (site[0] + 1, site[1], site[2]))):
                    sx, sy = self.architecture.exact_SLM_location_tuple(qubit_mapping[q])
                    tx, ty = self.architecture.exact_SLM_location_tuple(tgt)
                    src_c.append(sx); src_r.append(sy)
                    tgt_c.append(tx); tgt_r.append(ty)
            # 4b. 坐标 → 次序（AOD 只关心相对顺序）
            dr = discretize(src_r); tr = discretize(tgt_r)
            dc = discretize(src_c); tc = discretize(tgt_c)
            # 4c. 逐腿尝试并组：能一起走的进同组，否则开新趟
            groups = []
            maxd = []
            k = 0
            for site, d1, d2, dis3, q1, q2 in placed:
                add_to_groups(dc[k], tc[k], dr[k], tr[k], d1, groups, maxd)
                k += 1
                add_to_groups(dc[k], tc[k], dr[k], tr[k], d2, groups, maxd)
                k += 1
            # 4d. 总分 = Σ每组√最长腿 + Σ复用前瞻（足额计入：搭档真实要付的路费）
            cost = groups_cost(maxd) + sum(sqrt(o[3]) for o in placed)
            if self.use_sd:                 # 消融开关：队形歪扭度（默认关）
                cost += groups_sd(groups)
            return cost

        # ---- 变异：动 1~2 个基因，两种力度 --------------------------------
        def mutate(chrom):
            c = list(chrom)
            for _ in range(1 + self.rng.randrange(2)):
                gi = self.rng.randrange(len(c))
                if len(candidates[gi]) > 1:  # 复用门的哑基因不动
                    c[gi] = self.rng.choice([c[gi] + 1, c[gi] - 1,        # 挪到隔壁工位（精修）
                                             self.rng.randrange(64)])     # 跳到远处（碰运气）
            return c

        # ---- 第 5 步：进化循环（(μ+λ) 精英保留） --------------------------
        t0 = time.time()
        # 初始种群：1 张"老实人方案"（全 0 = 每件活都选最近 → 保底不差于贪心）
        #           + 5 张随机方案（多样性）
        zeros = [0] * len(candidates)
        population = [zeros]
        for _ in range(self.population_size - 1):
            population.append([self.rng.randrange(8) for _ in candidates])
        scored = [(fitness(decode(c)), c) for c in population]
        scored.sort(key=lambda x: x[0])
        for _ in range(self.iterations):
            offspring = []
            for _, c in scored:  # 每个父代独立采邻域 → 各留最好的 2 个子代（防单一血统垄断）
                pool = [mutate(c) for _ in range(self.neighbor_sample_size)]
                pool_scored = sorted((fitness(decode(m)), m) for m in pool)
                offspring += pool_scored[: self.neighbors_per_solution]
            # 父代+子代合并按分截断：历史最优永不丢失（单调不退化）
            scored = sorted(scored + offspring, key=lambda x: x[0])[: self.population_size]
        best = decode(scored[0][1])
        self.ga_time += time.time() - t0

        # ---- 第 6 步：落盘（输出格式与父类逐字节兼容，下游无感知） --------
        tmp_mapping = deepcopy(qubit_mapping)
        for (site, d1, d2, dis3, q1, q2) in best:
            reuse1 = test_reuse and layer > 0 and q1 in self.list_reuse_qubit[layer - 1]
            reuse2 = test_reuse and layer > 0 and q2 in self.list_reuse_qubit[layer - 1]
            if reuse1:
                # q1 留在原地（位置抄自上一层门时刻表）；搭档 q2 坐旁边
                tmp_mapping[q1] = gate_mapping[q1]
                if site == gate_mapping[q1]:
                    tmp_mapping[q2] = (site[0] + 1, site[1], site[2])   # q1 在左座→q2 进右座
                else:
                    tmp_mapping[q2] = site                               # q1 在右座→q2 进左座
            elif reuse2:
                tmp_mapping[q2] = gate_mapping[q2]
                if site == gate_mapping[q2]:
                    tmp_mapping[q1] = (site[0] + 1, site[1], site[2])
                else:
                    tmp_mapping[q1] = site
            else:
                # 普通门：按两比特存储列号分左右座（父类同款规则）
                if qubit_mapping[q1][2] < qubit_mapping[q2][2]:
                    tmp_mapping[q1] = site
                    tmp_mapping[q2] = (site[0] + 1, site[1], site[2])
                else:
                    tmp_mapping[q1] = (site[0] + 1, site[1], site[2])
                    tmp_mapping[q2] = site
        self.mapping.append(tmp_mapping)
