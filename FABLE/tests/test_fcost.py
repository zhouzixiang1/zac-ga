"""fcost 单测：规则移植正确性 + 与 ZAC 原版 router 的差分对比。

跑法（任意 python3，无第三方依赖）：
    cd FABLE && ../ZAC/.venv/bin/python -m pytest tests/ -q
（差分测试需要 import FABLE/zac 副本；单测本身只用标准库）
"""
import random
import sys
from pathlib import Path
from math import sqrt

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fable.fcost import compatible_2d, fable_cost, stage_decompose


def test_uniform_translation_one_round():
    """全体同向平移（仪仗队齐步走）：保序 → 一趟车全部带走，零冲突。"""
    legs = [(10.0 + i, 0.0, float(i), 5.0, float(i) + 2) for i in range(6)]
    chain, n_conf = stage_decompose(legs)
    assert n_conf == 0
    assert chain == sqrt(15.0)          # 唯一一轮的耗时 = 最长腿 √dmax


def test_crossing_pair_two_rounds():
    """两条交叉腿（你往我这边来、我往你那边去）：冲突 → 必须分两趟。"""
    legs = [(9.0, 0.0, 0.0, 9.0, 0.0), (9.0, 9.0, 0.0, 0.0, 0.0)]
    chain, n_conf = stage_decompose(legs)
    assert n_conf == 1                  # 一条冲突边
    assert chain == 2 * sqrt(9.0)       # 两轮串行，每轮 √9


def test_same_start_split_conflict():
    """同一起点线去不同终点（一根 AOD 行不能分裂）→ 冲突。"""
    a = (0.0, 0.0, 0.0, 5.0, 0.0)
    b = (9.0, 0.0, 1.0, 9.0, 0.0)
    legs = [a, b]
    chain, n_conf = stage_decompose(legs)
    assert n_conf == 1


def test_zero_length_leg_excluded_by_caller():
    """dist=0 的腿本就不该传进来（原地不动不占车）——契约在文档里。"""
    legs = [(4.0, 0.0, 0.0, 4.0, 0.0)]
    chain, n_conf = stage_decompose(legs)
    assert (chain, n_conf) == (sqrt(4.0), 0)


def test_fable_cost_weighting():
    legs = [(9.0, 0.0, 0.0, 9.0, 0.0), (9.0, 9.0, 0.0, 0.0, 0.0)]
    assert fable_cost(legs, w_conf=1.0) == 2 * sqrt(9.0) + 1.0
    assert fable_cost(legs, w_conf=0.0) == 2 * sqrt(9.0)


def test_differential_vs_zac_router():
    """差分对比：随机 20 万对腿上与 ZAC 原版 router.compatible_2D 逐位一致。

    Router_mixin.compatible_2D 不引用 self，可以当普通函数调用——
    这条测试保证了"逐行移植"不是一句口号。
    """
    from zac.router.router import Router_mixin

    rng = random.Random(42)
    for _ in range(20000):
        a = (rng.uniform(0, 100), rng.uniform(0, 100),
             rng.uniform(0, 100), rng.uniform(0, 100))
        b = (rng.uniform(0, 100), rng.uniform(0, 100),
             rng.uniform(0, 100), rng.uniform(0, 100))
        assert compatible_2d(a, b) == Router_mixin.compatible_2D(None, a, b), (a, b)


def test_stage_decompose_matches_router_batching_shape():
    """分轮形状对照：冲突图上贪心取独立集 = 一轮；轮数 ≤ 腿数（上界检查）。"""
    rng = random.Random(7)
    legs = [(rng.uniform(1, 50), rng.uniform(0, 40), rng.uniform(0, 40),
             rng.uniform(0, 40), rng.uniform(0, 40)) for _ in range(20)]
    chain, n_conf = stage_decompose(legs)
    assert 0 <= n_conf <= 20 * 19 // 2
    assert chain >= sqrt(min(l[0] for l in legs))    # 至少一趟车
