"""zcost 单测：着色正确性 + 与 ZAC 原版 router 的差分对比 + 精确档对拍。

跑法（ZAC/.venv，无第三方依赖，差分测试要 import ZAC_new/zac 副本）：
    cd ZAC_new && ../ZAC/.venv/bin/python -m pytest tests/ -q
"""
import random
import sys
import time
from itertools import combinations
from pathlib import Path
from math import sqrt

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from znew.zcost import (compatible_2d, conflict_graph, color_batches,
                        batch_cost, _dsatur_heuristic, _dsatur_exact)


# ---------------------------------------------------------------- 规则移植
def test_compatible_rules():
    """四条铁律逐条验证（同起分叉/同终合流/交叉 × 两个维度）。"""
    assert not compatible_2d((0, 0, 0, 0), (0, 5, 1, 1))     # 同起x→必同终x
    assert not compatible_2d((0, 5, 0, 0), (3, 5, 1, 1))     # 同终x→必同起x
    assert not compatible_2d((0, 9, 0, 0), (9, 0, 1, 1))     # x 交叉
    assert not compatible_2d((0, 0, 0, 9), (1, 1, 9, 0))     # y 交叉
    assert compatible_2d((0, 1, 0, 1), (5, 9, 3, 7))         # 双双保序 → 兼容
    assert compatible_2d((0, 4, 0, 0), (0, 4, 1, 1))         # 同行齐平走 → 兼容


def test_differential_vs_zac_router():
    """2 万随机向量对，与 ZAC 原版 router.compatible_2D 逐一对答案。"""
    from zac.router.router import Router_mixin
    rng = random.Random(42)
    for _ in range(20_000):
        a = tuple(rng.uniform(-50, 50) for _ in range(4))
        b = tuple(rng.uniform(-50, 50) for _ in range(4))
        # 精确相等会随机触发同线规则，留 1/4 概率把坐标对齐成整数再测
        if rng.random() < 0.25:
            a = (float(int(a[0])), float(int(a[1])), float(int(a[2])), float(int(a[3])))
            b = (float(int(b[0]) + rng.randint(0, 1)),
                 float(int(b[1]) + rng.randint(0, 1)),
                 float(int(b[2]) + rng.randint(0, 1)),
                 float(int(b[3]) + rng.randint(0, 1)))
        assert compatible_2d(a, b) == Router_mixin.compatible_2D(None, a, b), (a, b)


# ---------------------------------------------------------------- 着色正确性
def _color_graph(n, edges):
    """对抽象图直接调启发式 DSATUR（绕开腿几何，专测图算法本身）。"""
    dist = [1.0] * n
    adj = [[] for _ in range(n)]
    for i, j in edges:
        adj[i].append(j)
        adj[j].append(i)
    colors = _dsatur_heuristic(n, adj, dist)
    n_used = (max(colors) + 1) if n else 0
    return colors, n_used, adj


def test_known_graphs():
    """教科书图：χ 无边=1、偶环/二部=2、三角=3、C5=3、K33=2。"""
    def chi(n, edges):
        colors, n_used, adj = _color_graph(n, edges)
        # 校验着色合法（每条边两端异色）
        for i, j in edges:
            assert colors[i] != colors[j]
        return n_used

    assert chi(3, []) == 1                                   # 无边 → 1 批
    assert chi(4, [(0, 1), (1, 2), (2, 3), (3, 0)]) == 2     # C4 二部
    assert chi(3, [(0, 1), (1, 2), (0, 2)]) == 3             # 三角形
    assert chi(5, [(0, 1), (1, 2), (2, 3), (3, 4), (4, 0)]) == 3   # C5
    assert chi(6, [(0, 1), (0, 3), (0, 5), (2, 1), (2, 3), (2, 5),
                   (4, 1), (4, 3), (4, 5)]) == 2             # K33 二部


def test_exact_matches_bruteforce():
    """精确档 vs 独立暴力判定：随机图上 χ 严格一致（200 张）。"""
    rng = random.Random(7)

    def colorable(n, adj, k):
        """独立的 k-可染色判定（朴素回溯，与 DSATUR 无共享代码）。"""
        colors = [-1] * n

        def bt(v):
            if v == n:
                return True
            used = {colors[u] for u in adj[v] if colors[u] != -1}
            for c in range(k):
                if c not in used:
                    colors[v] = c
                    if bt(v + 1):
                        return True
                    colors[v] = -1
            return False

        return bt(0)

    for _ in range(200):
        n = rng.randint(3, 8)
        p = rng.choice([0.2, 0.5, 0.8])
        edges = [(i, j) for i, j in combinations(range(n), 2) if rng.random() < p]
        adj = [[] for _ in range(n)]
        for i, j in edges:
            adj[i].append(j)
            adj[j].append(i)
        # 暴力 χ：最小可染色 k
        brute = next(k for k in range(1, n + 1) if colorable(n, adj, k))
        # 启发式是上界、精确档应恰好命中
        colors, n_used, _ = _color_graph(n, edges)
        assert n_used >= brute
        exact_chi, exact_colors = _dsatur_exact(n, adj, n_used)
        assert exact_chi == brute, (n, edges, exact_chi, brute)
        if exact_colors is not None:
            assert max(exact_colors) + 1 == exact_chi
            for i, j in edges:
                assert exact_colors[i] != exact_colors[j]


def test_batch_semantics():
    """腿语义：兼容全家 → 1 批；交叉对 → 2 批；批代价公式核对。"""
    # 仪仗队齐步走：保序 → 一车
    legs = [(10.0 + i, 0.0, float(i), 5.0, float(i) + 2) for i in range(6)]
    n_batches, batches, method = color_batches(legs)
    assert n_batches == 1 and len(batches[0]) == 6
    cost, nb, nconf = batch_cost(legs, w_batch=1.0)
    assert nb == 1 and nconf == 0
    assert abs(cost - (1.0 + sqrt(15.0))) < 1e-9      # w×1批 + √15
    # 两条交叉腿 → 两批
    legs = [(9.0, 0.0, 0.0, 9.0, 0.0), (9.0, 9.0, 0.0, 0.0, 0.0)]
    n_batches, batches, _ = color_batches(legs)
    assert n_batches == 2 and all(len(b) == 1 for b in batches)
    # 齐步走的批次比交叉拆批便宜
    good = batch_cost([(10.0 + i, 0.0, float(i), 5.0, float(i) + 2)
                       for i in range(6)])[0]
    bad = batch_cost([(9.0, 0.0, 0.0, 9.0, 0.0), (9.0, 9.0, 0.0, 0.0, 0.0),
                      (4.0, 1.0, 0.0, 5.0, 0.0), (4.0, 6.0, 0.0, 5.0, 0.0)])[0]
    assert good < bad


def test_exact_on_dense_legs():
    """腿场景下精确档真实增益：K₃,₂ 二部冲突图，启发式 3 批 → 精确 2 批。

    腿 i：起点列 = i，终点列 = (i+2)%5 → 冲突图恰为 {A,B,C}×{D,E} 的
    完全二部图 K₃,₂（χ=2）。启发式 DSATUR 在全部距离相等时凭平局
    规则可能染出 3 色；精确档必须压回 2。
    """
    legs = []
    for i in range(5):
        x0, x1 = float(i), float((i + 2) % 5)
        legs.append((abs(x1 - x0) + 1.0, x0, 0.0, x1, 0.0))
    adj = conflict_graph(legs)
    assert all(len(a) >= 1 for a in adj)          # 每条腿都有冲突（非空图）
    n_batches, batches, method = color_batches(legs, exact_threshold=24)
    assert n_batches == 2 and method == "exact"   # K₃,₂ → χ=2，精确档兑现
    # 批内两两兼容（独立集性质——路由正确性的根基）
    vecs = [(l[1], l[3], l[2], l[4]) for l in legs]
    for members in batches:
        for i, j in combinations(members, 2):
            assert compatible_2d(vecs[i], vecs[j])
    # 并集覆盖全部腿且不重不漏
    flat = sorted(v for b in batches for v in b)
    assert flat == list(range(5))


def test_performance_50_legs():
    """n=50 随机腿，启发式一次 < 50ms（适应度内循环的速度门槛）。"""
    rng = random.Random(3)
    legs = [(rng.uniform(1, 30), float(rng.randint(0, 30)), float(rng.randint(0, 10)),
             float(rng.randint(0, 30)), float(rng.randint(0, 10))) for _ in range(50)]
    t0 = time.time()
    for _ in range(10):
        batch_cost(legs)
    assert (time.time() - t0) / 10 < 0.05
