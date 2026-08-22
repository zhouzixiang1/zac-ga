"""打分仪表：图着色版"要几批"评估（ZAC_new 文件夹的灵魂部件）。

要解决的两件事（对照 ZAC 原版的行为）：
    ①"怎样算一批"      ← ZAC router.py:232 compatible_2D 的逐行移植
        （与 FABLE/fcost.py 同源，那边已做过 2 万随机对差分验证）
    ②"要几批"          ← 图着色，而不是贪心 MIS 逐轮剥离：
        ZAC 的 route_qubit_mis 每轮取一个"极大"独立集发一班车，
        剥到运完为止——总批数是贪心的副产品，没人对它负责。
        把"每条搬运腿"当节点、冲突连边，则【最少批数 = 冲突图的
        色数 χ】，每个颜色类 = 一批（独立集 = 两两兼容 = 一班车）。
        这是 ZAC / ZAP / ICCAD'25 三家都没占的位置。

两个用途（一个模块，两种精度）：
    * 放置适应度（zplacer.py）：每次打分都调用 → 只用启发式 DSATUR
      （饱和度优先 + 距离降序 tie-break），毫秒级；
    * 路由分批（zac_new.py routing_strategy="coloring"）：每层一次
      → 小图（≤ exact_threshold 节点）用精确分支限界求真正最少批数。

腿（leg）的格式与 fcost.py 一致：(dist, 起x, 起y, 终x, 终y)，
只放真正要动的腿（dist>0；原地不动的原子不占车）。
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


def conflict_graph(legs: list[tuple]) -> list[list[int]]:
    """建冲突图：O(n²) 两两判定，返回邻接表（= collect_violation 的图形态）。

    legs 每项 = (dist, 起x, 起y, 终x, 终y)。注意向量顺序换算——
    compatible_2d 要 (起x, 终x, 起y, 终y)，腿存的是 (起x, 起y, 终x, 终y)
    顺序（fcost.py 踩过一次的坑，这里换算写死在构造式里）。
    """
    vecs = [(leg[1], leg[3], leg[2], leg[4]) for leg in legs]
    n = len(legs)
    adj = [[] for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            if not compatible_2d(vecs[i], vecs[j]):
                adj[i].append(j)
                adj[j].append(i)
    return adj


def _dsatur_heuristic(n: int, adj: list[list[int]], dist: list[float]) -> list[int]:
    """启发式 DSATUR：反复给"邻居颜色种类最多"的节点染最小可行色。

    tie-break 链：饱和度 ↓ → 度数 ↓ → 距离 ↓（远腿优先定色，
    与 maximalis_sort"远的先上车"的取向一致）。
    返回颜色数组（0..χ-1）。纯 O(n²) 级，n≤几百毫无压力。
    """
    colors = [-1] * n
    neighbor_colors: list[set] = [set() for _ in range(n)]
    for _ in range(n):
        # 选下一个染色的节点：饱和度 → 度数 → 距离
        best_v, best_key = -1, (-1, -1, -1.0)
        for v in range(n):
            if colors[v] != -1:
                continue
            key = (len(neighbor_colors[v]), len(adj[v]), dist[v])
            if key > best_key:
                best_key, best_v = key, v
        v = best_v
        c = 0
        while c in neighbor_colors[v]:
            c += 1
        colors[v] = c
        for u in adj[v]:
            neighbor_colors[u].add(c)
    return colors


def _dsatur_exact(n: int, adj: list[list[int]], ub: int,
                  node_budget: int = 200_000) -> tuple[int, list[int] | None]:
    """精确 DSATUR 分支限界：求真正的色数 χ。

    上界 ub 来自启发式解；剪枝：已用颜色数 ≥ 当前最优即回溯。
    节点预算兜底（Python 速度保险丝），耗尽时提前返回当前最优
    （best_colors 为 None 表示"预算内没找到更优/没搜完"，调用方
    保留启发式解即可，结果仍是合法着色）。
    对称性剪枝：新颜色只允许比已用最大色号大 1（颜色重排等价）。
    """
    best_colors: list[int] | None = None
    best = ub + 1                             # 记录解时才与 ub 比较（严格更小才收）
    colors = [-1] * n
    # 邻居颜色用 {色号: 计数} 而非 set：一条路径上两个不相邻顶点可以同色
    # 且共享邻居，回溯时必须按计数增删（set 会把别人的颜色误删掉）
    neighbor_colors: list[dict] = [dict() for _ in range(n)]
    budget = [node_budget]

    def backtrack(n_used: int):
        nonlocal best, best_colors
        if n_used >= best:
            return
        budget[0] -= 1
        if budget[0] <= 0:
            raise TimeoutError
        # DSATUR 规则选下一个节点
        best_v, best_key = -1, (-1, -1)
        for v in range(n):
            if colors[v] != -1:
                continue
            key = (len(neighbor_colors[v]), len(adj[v]))
            if key > best_key:
                best_key, best_v = key, v
        if best_v == -1:                      # 全部染色完毕 → 找到更优解
            best, best_colors = n_used, colors[:]
            return
        v = best_v
        # 对称性剪枝：色号 0..n_used（新色只开在 n_used 号）
        for c in range(min(n_used, best - 1) + 1):
            if c in neighbor_colors[v]:
                continue
            colors[v] = c
            for u in adj[v]:
                neighbor_colors[u][c] = neighbor_colors[u].get(c, 0) + 1
            backtrack(max(n_used, c + 1))
            colors[v] = -1
            for u in adj[v]:
                neighbor_colors[u][c] -= 1
                if neighbor_colors[u][c] == 0:
                    del neighbor_colors[u][c]

    try:
        backtrack(0)
    except TimeoutError:
        pass
    # best==ub+1 且无解：要么整个空间被剪掉（ub 就是 χ），要么预算耗尽
    return best, best_colors


def color_batches(legs: list[tuple], exact_threshold: int = 24,
                  node_budget: int = 200_000) -> tuple[int, list[list[int]], str]:
    """主入口：把搬运腿分批，返回 (批数, 每批腿下标列表, 求解方式)。

    * n ≤ exact_threshold 时先跑启发式拿上界，再试精确 B&B 求 χ；
      预算耗尽自动回退启发式解（求解方式标注 "exact"/"heuristic"）。
    * 批次顺序：按批内最长腿降序（远批先发，与 maximalis_sort 取向一致）；
      批内腿序：按距离降序（process_movement_layer 拿到的是集合，
      这里给个确定的顺序便于复现与对账）。
    """
    n = len(legs)
    if n == 0:
        return 0, [], "empty"
    dist = [leg[0] for leg in legs]
    adj = conflict_graph(legs)
    colors = _dsatur_heuristic(n, adj, dist)
    n_used = (max(colors) + 1) if colors else 0
    method = "heuristic"
    if n <= exact_threshold and n_used > 1:
        # 孤立点（度为 0 的腿）不约束任何人，先剥掉只对核心图精确求解
        core = [v for v in range(n) if adj[v]]
        if core:
            core_pos = {v: k for k, v in enumerate(core)}
            core_adj = [[core_pos[u] for u in adj[v]] for v in core]
            ub_core = len({colors[v] for v in core})   # 启发式在核心上实际用的颜色数
            _, colors_core = _dsatur_exact(len(core), core_adj, ub_core)
            if colors_core is not None:                # 找到严格更优的着色才采纳
                remap = {old: new for new, old in enumerate(
                    sorted({colors_core[k] for k in range(len(core))}))}
                for k, v in enumerate(core):
                    colors[v] = remap[colors_core[k]]
                core_set = set(core)
                for v in range(n):                     # 孤立腿并入 0 号批（兼容一切）
                    if v not in core_set:
                        colors[v] = 0
                n_used = len(remap)
                method = "exact"
    # 色类 → 批次（按批内最长腿降序排列批次）
    classes: dict[int, list[int]] = {}
    for v, c in enumerate(colors):
        classes.setdefault(c, []).append(v)
    batches = sorted(
        (sorted(members, key=lambda v: dist[v], reverse=True) for members in classes.values()),
        key=lambda members: max(dist[v] for v in members),
        reverse=True,
    )
    return n_used, batches, method


def batch_cost(legs: list[tuple], w_batch: float = 1.0,
               exact_threshold: int = 0) -> tuple[float, int, int]:
    """放置适应度用的代价：w_batch×批数 + Σ批 √(批内最长腿)。

    与 FABLE 的 fable_cost 同量纲（√μm），差异只在"分批器"：
    那边贪心 MIS 分轮，这边着色分批（前者是后者的上界近似）。
    适应度内只用启发式（exact_threshold=0 关掉精确档，速度优先）。
    返回 (cost, n_batches, n_conflicts)——后两项供消融与对账。
    """
    if not legs:
        return 0.0, 0, 0
    adj = conflict_graph(legs)
    n_conflicts = sum(len(a) for a in adj) // 2
    n_batches, batches, _ = color_batches(legs, exact_threshold=exact_threshold)
    time_cost = sum(sqrt(max(legs[v][0] for v in members)) for members in batches)
    return w_batch * n_batches + time_cost, n_batches, n_conflicts
