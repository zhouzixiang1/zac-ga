"""打分仪表：Fable 式"最大链 + 冲突边"评估（本文件夹的灵魂部件）。

来源：用户笔记《中性原子编译》（3）2q放置优化 —— compiler_search 的两个改动：
    邻域     = "交换位置"（两件活互换工位）+ "邻居位置"（挪到隔壁工位）
    评价函数 = 最大链 + 冲突边 加权
笔记里的 Fable 是自建管线；本文件夹把同样的想法装到 ZAC 的零件上，
术语一一对应（这就是"复现 Fable 的想法"的落点）：

    Fable 冲突图/冲突边  ←  ZAC router.py 的 collect_violation：
        两条搬运腿不满足 compatible_2D（router.py:232 的 AOD 保序铁律）
        就有一条冲突边。
    Fable 最大链         ←  ZAC route_qubit_mis 的批次循环（router.py:77-90）：
        每轮在剩余腿的冲突图上贪心取一个 MIS = 一趟并行摆渡车；
        车跑完才能发下一趟 → 轮次串行成链。
        链耗时 = Σ 每轮 √(轮内最长腿)（匀加速运动，木桶原理）。

对比 GA/zga/racost.py（v1a 用的 ICCAD 分组模型）：那边的"组"是 qmap
论文的并车近似（秩比较），与 ZAC 真实排车器并不等价；这边逐条规则
照搬 ZAC 自己的排车器，等于在适应度里"预演"第⑨道工序——预测真实
重排阶段数/时长理应更准。这就是 Fable 评估函数的核心思想：
不问"距离总和"，问"这批活实际要串行跑几趟、每趟多久"。

用法：fplacer.py 把每条搬运腿的 (距离, 起点x, 起点y, 终点x, 终点y)
收进列表，调 stage_decompose 得 (链耗, 冲突边数)，按权重合成适应度。
"""
from __future__ import annotations

from math import sqrt


def compatible_2d(a: tuple, b: tuple) -> bool:
    """两条搬运腿能否同时上车（= router.py:232 compatible_2D 的逐行移植）。

    腿的格式：(起点x, 终点x, 起点y, 终点y)。AOD 物理铁律：一趟车上的
    原子在 x、y 两个方向都必须保持严格次序——
      * 同一起点线 → 必须同一终点（一根 AOD 行/列不能分裂）；
      * 起点在上/左 → 终点必须还在上/左（路线不能交叉）；
      * 同一终点线 → 起点必须同一条（两条腿不能挤进同一行/列）。
    """
    if a[0] == b[0] and a[1] != b[1]:
        return False
    if a[1] == b[1] and a[0] != b[0]:
        return False
    if a[0] < b[0] and a[1] >= b[1]:
        return False
    if a[0] > b[0] and a[1] <= b[1]:
        return False
    if a[2] == b[2] and a[3] != b[3]:
        return False
    if a[3] == b[3] and a[2] != b[2]:
        return False
    if a[2] < b[2] and a[3] >= b[3]:
        return False
    if a[2] > b[2] and a[3] <= b[3]:
        return False
    return True


def stage_decompose(legs: list[tuple]) -> tuple[float, int]:
    """预演 ZAC 排车器：贪心 MIS 分轮，返回 (链耗, 冲突边数)。

    legs 每项 = (dist, 起点x, 起点y, 终点x, 终点y)，只放真正要动的腿
    （dist>0；原地不动的原子不占车，与 router 的 remain_graph 一致）。

    流程 = route_qubit_mis 的逐行照搬：
      1. 按距离降序排（maximalis_sort 的排序规则：远的先上车）；
      2. 每轮从前往后扫，与"本轮已上车"都不冲突的就上 → 一趟车；
      3. 没上车的进入下一轮，直到全部运完。
    链耗 = Σ 每轮 √(轮内最长腿)：轮与轮串行，轮内并行取最慢者。
    冲突边数 = pairwise 不兼容的对数（Fable 评价函数的第二项）：
    分轮已经消化了大部分冲突，剩下的边衡量"这版座位表本质上
    有多不合拍"——边多说明怎么排车都要拆散着跑。
    """
    if not legs:
        return 0.0, 0
    # 与 router 一致：远腿优先上车的扫描顺序（排序一次，逐轮沿用）
    ordered = sorted(legs, key=lambda leg: leg[0], reverse=True)
    # 腿存的是 (dist, 起x, 起y, 终x, 终y)；compatible_2d 要 (起x, 终x, 起y, 终y)
    vecs = [(leg[1], leg[3], leg[2], leg[4]) for leg in ordered]

    # 冲突邻接表（= collect_violation + maximalis_solve 的 node_neighbors）
    n = len(ordered)
    neighbors = [[] for _ in range(n)]
    n_conflicts = 0
    for i in range(n):
        for j in range(i + 1, n):
            if not compatible_2d(vecs[i], vecs[j]):
                neighbors[i].append(j)
                neighbors[j].append(i)
                n_conflicts += 1

    # 贪心分轮：每轮扫一遍剩余腿，无冲突就上这趟车
    chain_cost = 0.0
    remain = list(range(n))
    while remain:
        taken: list[int] = []
        taken_set = set()
        for i in remain:
            if any(j in taken_set for j in neighbors[i]):
                continue
            taken.append(i)
            taken_set.add(i)
        dmax = max(ordered[i][0] for i in taken)
        chain_cost += sqrt(dmax)
        remain = [i for i in remain if i not in taken_set]
    return chain_cost, n_conflicts


def fable_cost(legs: list[tuple], w_conf: float = 1.0) -> float:
    """Fable 评价函数：最大链 + 冲突边×权重。

    两项量纲都是 √μm（链耗本来就是这个单位；w_conf 给冲突边定"身价"，
    默认 1.0 = 一条冲突边 ≈ 1μm 搬运腿的路费，可调）。
    """
    chain_cost, n_conflicts = stage_decompose(legs)
    return chain_cost + w_conf * n_conflicts
