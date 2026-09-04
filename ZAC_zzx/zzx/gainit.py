"""GA 初始放置器：接管 ZAC 的 SA（saplacer.py）做的"谁住宿舍哪个铺"。

背景（README·已知边界 有完整记录）：ZAC 的 SA 占编译时间 97-98%，但邻域
生成器有两处缺陷——行移动是死代码（randrange(0,1,1) 恒 0），new_c 误用
old_r（saplacer.py:291，目标列恒为 99±5 的窗口）；18 电路里 9 个的初始
布局就是平凡顺序。本模块用 GA 重做**同一目标**的搜索：

    cost(p) = 0.5 · Σ_ij  W[i,j] · D[p[i], p[j]]

  W = 搭档亲和矩阵（SA preprocessing 同款层权重：第 l 层 1-0.1·l，
      ≥5 层取 0.6，逐门累加，对称）
  D = 座位对会合距离矩阵（arch.nearest_entanglement_site_dis 再开方，
      SA 的 distance() 同款语义：搭档俩走到共同最近工位的路程）
  p = 排列染色体：p[i] = 原子 i 坐的座位下标

座位枚举与 SA 的 init_sa_solution 逐行一致（从离激发区最近的存储行起
逐列填），保证解空间与原版相同、只有搜索引擎不同。三个结构化解热启动
注入初始种群（平凡序 / 首用轮次序 / 权重贪心相邻序），之后靠
swap / insert / 段反转三类邻域精英进化。全程 numpy 向量化 + 隔离 RNG。
"""
from __future__ import annotations

import random
import time

import numpy as np


class GAInitialPlacer:
    """与 SAPlacer 同位替换：run() 之后读 .best_mapping / .best_cost。"""

    def __init__(self, params: dict | None = None):
        p = params or {}
        self.pop_size = int(p.get("init_pop", 40))
        self.generations = int(p.get("init_gens", 150))
        self.rng = random.Random(int(p.get("seed", 0)))
        self.best_mapping: list[tuple] = []
        self.best_cost = float("inf")

    # ---------------- 座位与权重（语义均与 SA 对齐） ----------------

    def _enumerate_seats(self, arch, n: int) -> list[tuple]:
        """复刻 SA init_sa_solution 的填位顺序（无随机部分）。

        从离激发区最近的存储行开始逐列填，列满换行，行满换下一个存储 SLM。
        """
        seats = []
        slm_idx = 0
        zone_s = arch.storage_zone[slm_idx]
        slm = arch.dict_SLM[zone_s]
        n_c, r, c = slm.n_c, 0, 0
        d1 = arch.nearest_entanglement_site_distance(zone_s, 0, 0)
        d2 = arch.nearest_entanglement_site_distance(zone_s, slm.n_r - 1, 0)
        step = 1 if d1 < d2 else -1
        if d1 >= d2:
            r = slm.n_r - 1
        while len(seats) < n:
            seats.append((zone_s, r, c))
            c += 1
            if c % n_c == 0:
                r += step
                c = 0
                if r == slm.n_r:
                    slm_idx += 1
                    zone_s = arch.storage_zone[slm_idx]
                    slm = arch.dict_SLM[zone_s]
                    r = slm.n_r - 1 if step > 0 else 0
                    n_c = slm.n_c
        return seats

    def _weight_matrix(self, n: int, list_gate) -> list[list[float]]:
        W = [[0.0] * n for _ in range(n)]
        weights = [1 - 0.1 * l for l in range(5)]     # [1, .9, .8, .7, .6]
        for l, gates in enumerate(list_gate):
            w = weights[l] if l < 5 else weights[-1]
            for g in gates:
                a, b = g[0], g[1]
                W[a][b] += w
                W[b][a] = W[a][b]
        return W

    def _distance_matrix(self, arch, seats) -> np.ndarray:
        n = len(seats)
        D = np.zeros((n, n))
        for i in range(n):
            s1, r1, c1 = seats[i]
            for j in range(i + 1, n):
                s2, r2, c2 = seats[j]
                d = arch.nearest_entanglement_site_dis(s1, r1, c1, s2, r2, c2) ** 0.5
                D[i, j] = D[j, i] = d
        return D

    # ---------------- 热启动：三个结构化解 ----------------

    @staticmethod
    def _assignment_from_atom_order(atom_at_seat: list[int]) -> list[int]:
        """Invert an ``atom_at_seat`` ordering into the chromosome contract.

        The structured heuristics naturally build a list whose position is a
        seat and whose value is the atom occupying that seat.  GA chromosomes
        use the opposite direction, ``p[atom] = seat_index``.  Treating the
        heuristic ordering directly as ``p`` silently assigns every non-self-
        inverse ordering to the wrong atoms.
        """
        n = len(atom_at_seat)
        if sorted(atom_at_seat) != list(range(n)):
            raise ValueError("warm-start atom order must be a permutation")
        assignment = [-1] * n
        for seat_index, atom in enumerate(atom_at_seat):
            assignment[atom] = seat_index
        return assignment

    def _warm_starts(self, n, W, list_gate) -> list[list[int]]:
        seeds = [list(range(n))]                        # ① 平凡序（SA 的老赢家）

        first_use: dict[int, int] = {}                  # ② 首用轮次序：早用靠前
        for l, gates in enumerate(list_gate):
            for g in gates:
                for q in (g[0], g[1]):
                    first_use.setdefault(q, l)
        first_use_order = sorted(
            range(n), key=lambda q: first_use.get(q, 1 << 30))
        seeds.append(self._assignment_from_atom_order(first_use_order))

        # ③ 权重贪心相邻序：连接强度增量维护，每步挂上与已放集合亲和最大者
        strength = [sum(W[i]) for i in range(n)]
        start = max(range(n), key=lambda i: strength[i])
        placed, in_set = [start], {start}
        conn = [W[i][start] for i in range(n)]         # 各原子与已放集合的总亲和
        while len(placed) < n:
            best, best_w = None, -1.0
            for i in range(n):
                if i not in in_set and conn[i] > best_w:
                    best, best_w = i, conn[i]
            if best is None:                            # 全无连接：退回强度序
                best = max((i for i in range(n) if i not in in_set),
                           key=lambda i: strength[i])
            placed.append(best)
            in_set.add(best)
            for i in range(n):
                conn[i] += W[i][best]
        seeds.append(self._assignment_from_atom_order(placed))
        return seeds

    # ---------------- 邻域与进化 ----------------

    @staticmethod
    def _neighbor(p: list[int], rng) -> list[int]:
        m = list(p)
        n = len(m)
        if n < 2:
            return m
        r = rng.random()
        if r < 0.4:                                     # 交换两原子的座位
            a, b = rng.randrange(n), rng.randrange(n)
            m[a], m[b] = m[b], m[a]
        elif r < 0.8:                                   # 拔出插入（保序挪动）
            a, b = rng.randrange(n), rng.randrange(n)
            m.insert(b, m.pop(a))
        else:                                           # 段反转（2-opt 型）
            a, b = sorted(rng.sample(range(n), 2))
            m[a:b + 1] = reversed(m[a:b + 1])
        return m

    def run(self, arch, n_qubit: int, list_gate):
        t0 = time.time()
        n = n_qubit
        seats = self._enumerate_seats(arch, n)
        W = self._weight_matrix(n, list_gate)
        D = self._distance_matrix(arch, seats)
        Wn = np.array(W)
        # 适应度：0.5 × Σ_ij W·D[p_i,p_j]（W 对称双三角，减半还原逐对求和）
        def cost(p: list[int]) -> float:
            return 0.5 * float((Wn * D[np.ix_(p, p)]).sum())

        seeds = self._warm_starts(n, W, list_gate)
        pop = [list(s) for s in seeds]
        while len(pop) < self.pop_size:                 # 热启动的扰动填充
            base = list(self.rng.choice(seeds))
            for _ in range(self.rng.randrange(1, max(2, n // 4))):
                a, b = self.rng.randrange(n), self.rng.randrange(n)
                base[a], base[b] = base[b], base[a]
            pop.append(base)
        scored = sorted((cost(p), k, p) for k, p in enumerate(pop))
        scored = [(c, p) for c, _, p in scored]

        n_eval = len(scored)
        stall = 0
        prev_best = scored[0][0]
        for _ in range(self.generations):
            offspring = []
            for _, p in scored[: max(2, self.pop_size // 2)]:
                for _ in range(2):
                    m = self._neighbor(p, self.rng)
                    offspring.append((cost(m), m))
            n_eval += len(offspring)
            scored = sorted(scored + offspring)[: self.pop_size]
            if scored[0][0] < prev_best - 1e-9:
                prev_best, stall = scored[0][0], 0
            else:
                stall += 1
                if stall >= 60:                         # 60 代无改进早停
                    break

        self.best_cost = scored[0][0]
        self.best_mapping = [seats[i] for i in scored[0][1]]
        print(f"[INFO] ZAC: GA-based initial placement: cost {self.best_cost:.2f}, "
              f"{n_eval} evals, {time.time() - t0:.2f}s")
