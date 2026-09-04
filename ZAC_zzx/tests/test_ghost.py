"""ghost.py 单测：手构物理用例 + 真实产物回归计数。

回归基准（修正对称判据，2026-08 实测）：
    ZAC 真值  ising=3  qft=117  swap=2  ghz=0
    zzx main  ising=58 qft=632  swap=40 ghz=4
（第一版审计脚本漏掉"动列×静止行"命中，zzx 数字曾被低估——本回归
 锁定的是修正后的口径。）
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from zzx.ghost import ghost_hits, hit_count, leg_hits, beams, sweep_bbox


class TestPhysics(unittest.TestCase):
    """手构用例：每类命中/安全各一。"""

    def test_diagonal_pass_through(self):
        # 腿 (0,0)→(10,10) 的对角路径正好穿过 (5,5)（s=0.5）
        leg = (14.14, 0, 0, 10, 10)
        self.assertEqual(hit_count([leg], [("g", 5, 5)]), 1)
        # 平移一格就安全
        self.assertEqual(hit_count([leg], [("g", 5, 6)]), 0)

    def test_endpoints_activate_deactivate(self):
        # s=0（activate 定点）与 s=1（deactivate 定点）都算命中
        leg = (14.14, 0, 0, 10, 10)
        self.assertEqual(hit_count([leg], [("g", 0, 0)]), 1)
        self.assertEqual(hit_count([leg], [("g", 10, 10)]), 1)

    def test_stationary_row_moving_col(self):
        # 旧判据漏掉的形态：行光束全程停在 y=7，列光束 0→10 平移，
        # 交叉点沿该行滑动——行上 (5,7) 处的鬼被扫
        leg = (10.0, 0, 7, 10, 7)
        self.assertEqual(hit_count([leg], [("g", 5, 7)]), 1)

    def test_empty_crossing_pin(self):
        # 纯空交叉点：鬼不在任何一条腿自己的路径上，但 A 的静止行(y=7)
        # 与 B 的静止列(x=15)全程钉出交叉点 (15,7)
        A = (10.0, 0, 7, 10, 7)      # 行停 y=7，列扫 0→10
        B = (10.0, 15, 20, 15, 30)   # 列停 x=15，行扫 20→30
        self.assertEqual(hit_count([A, B], [("g", 15, 7)]), 1)
        # 任何一条腿单独存在都不命中（各自路径与光束都够不到鬼）
        self.assertEqual(hit_count([A], [("g", 15, 7)]), 0)
        self.assertEqual(hit_count([B], [("g", 15, 7)]), 0)

    def test_synchronized_sweep(self):
        # 两条腿同时刻扫过鬼的 x 与 y（动×动，s 相等）——非对角空交叉
        A = (10.0, 0, 0, 10, 0)     # 列 0→10 扫 x=5 于 s=.5（行停 y=0）
        B = (10.0, 0, 0, 0, 10)     # 行 0→10 扫 y=5 于 s=.5（列停 x=0）
        # 鬼 (5,5)：A 列 s=.5 × B 行 s=.5 → 命中；鬼 (5,6) 时刻错开 → 安全
        self.assertEqual(hit_count([A, B], [("g1", 5, 5), ("g2", 5, 6)]), 1)

    def test_bbox_pruning(self):
        leg = (14.14, 0, 0, 10, 10)
        far = [("g%d" % i, 50 + i, 50) for i in range(100)]
        self.assertEqual(hit_count([leg], far), 0)

    def test_beams_and_bbox(self):
        cols, rows = beams([(1, 0, 5, 10, 5), (1, 3, 0, 3, 8)])
        self.assertIn((0, 10), cols) and self.assertIn((5, 5), rows)
        box = sweep_bbox(cols, rows)
        self.assertEqual(box, (0, 10, 0, 8))

    def test_leg_hits_single(self):
        leg = (10.0, 0, 7, 10, 7)
        self.assertEqual(len(leg_hits(leg, [("g", 5, 7)])), 1)


class TestRegression(unittest.TestCase):
    """真实产物回归：修正判据的命中计数锁定。"""

    def test_known_counts(self):
        import json
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from verify_batches import load_arch

        def scan(path):
            with open(path, encoding="utf-8") as handle:
                code = json.load(handle)
            arch = load_arch(code, Path(path))
            ex3 = lambda t: arch.exact_SLM_location_tuple(tuple(t))
            where = {}
            for loc in code["instructions"][0]["init_locs"]:
                where[loc[0]] = tuple(loc[1:])
            total = 0
            for inst in code["instructions"]:
                if inst["type"] != "rearrangeJob":
                    continue
                legs = []
                movers = set()
                for q, b, e in zip(inst["aod_qubits"],
                                   inst["begin_locs"], inst["end_locs"]):
                    p0, p1 = ex3(tuple(b[1:])), ex3(tuple(e[1:]))
                    if abs(p0[0] - p1[0]) + abs(p0[1] - p1[1]) > 1e-9:
                        movers.add(q)
                        from math import dist
                        legs.append((dist(p0, p1), p0[0], p0[1], p1[0], p1[1]))
                if legs:
                    ghosts = [(q, *ex3(s)) for q, s in where.items()
                              if q not in movers]
                    total += hit_count(legs, ghosts)
                for q, e in zip(inst["aod_qubits"], inst["end_locs"]):
                    where[q] = tuple(e[1:])
            return total

        root = Path(__file__).resolve().parent.parent
        cases = [
            (root / "results/main/code/ising_n42_code.json", 58),
            (root / "results/main/code/qft_n29_transpiled_code.json", 632),
            (root / "results/main/code/swap_test_n25_transpiled_code.json", 40),
            (root / "results/main/code/ghz_n23_code.json", 4),
        ]
        for path, want in cases:
            if not path.exists():
                continue        # 产物被清理时跳过，不阻塞单测
            self.assertEqual(scan(path), want, msg=str(path))


if __name__ == "__main__":
    unittest.main()
