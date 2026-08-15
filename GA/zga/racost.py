"""打分仪表：路由感知代价模型（约 80 行，整套 GA 架构的灵魂）。

来源：从 MQT-QMAP 的 HeuristicPlacer.cpp 逐行移植（当年我们逐行验证过的版本），
对应关系：
    compatible_with_group        ← cpp 的 checkCompatibilityWithGroup   (cpp:1296)
    add_to_groups                ← cpp 的 checkCompatibilityAndAddPlacement (cpp:1338)
    groups_cost                  ← cpp 的 getCost: Σ 每组√最长腿         (cpp:1084)
    groups_sd                    ← cpp 的 sumStdDeviationForGroups       (cpp:1100)

它回答的唯一问题："这版座位表，车实际要跑几趟、每趟多久？"

背景物理（为什么这么设计）：
  * 摆渡车(AOD)一次能带走一批原子，但车上的原子必须保持相对顺序、
    路线不能交叉——所以不是所有搬移都能并成一趟；
  * 一趟车的耗时由车上"住得最远的原子"决定（木桶原理）；
  * 车是匀加速运动：距离/时间² = a = 2750 m/s²，所以耗时 ∝ √距离；
  * 于是：把候选搬移按"能否并车"分组，总代价 = Σ 每组 √(组内最长腿)。
    组数 ≈ 趟数（串行部分），√最长腿 ≈ 每趟时长。

对比：ZAC 原版打分只算距离之和（不问车能不能并）；本仪表模拟了排车，
这就是 GA 能赢的方向盘。
"""
from __future__ import annotations

import bisect
from math import sqrt


def compatible_with_group(key: int, value: int, group: dict[int, int]) -> bool:
    """一条新搬移 (key→value) 能否并进已有的组？

    约定：group 是 {源行/源列: 目标行/目标列} 的有序映射（一个组存两张
    这样的表：行映射一张、列映射一张，对应车上原子的横向/纵向队形）。

    两条判规（= AOD 物理铁律的数学化）：
      (1) key 已存在且值相同 → 兼容
          【保序】同一源行上的原子必须去同一目标行（一根行不能分裂）；
      (2) key 是新的 → 必须满足 lower < value < upper
          （upper/lower 是按 key 排序后紧邻的上下邻居的值）
          【非交叉+不并线】队形的上下顺序不能颠倒、不能挤到同一行。
    """
    keys = sorted(group)
    idx = bisect.bisect_left(keys, key)      # 二分定位 key 应插入的位置
    if idx < len(keys) and keys[idx] == key:
        # 情形(1)：同源。只有目标也相同才兼容。
        return group[key] == value
    upper = group[keys[idx]] if idx < len(keys) else None   # 上邻居的值
    lower = group[keys[idx - 1]] if idx > 0 else None       # 下邻居的值
    if upper is not None and lower is not None:
        return lower < value < upper        # 夹在中间：顺序保持
    if upper is not None:                   # key 比所有已有键都小
        return value < upper
    if lower is not None:                   # key 比所有已有键都大
        return lower < value
    return True                              # 空组，随便进


def add_to_groups(
    h_key: int, h_value: int, v_key: int, v_value: int, distance: float,
    groups: list[tuple[dict[int, int], dict[int, int]]],
    max_distances: list[float],
) -> bool:
    """把一条搬移塞进第一个能兼容它的组；谁都不兼容就开新组（= 多一趟车）。

    一个原子同时有横向和纵向两个方向的队形要守，所以行映射、列映射
    必须同时兼容才能进组（对应 cpp 里 hGroup/vGroup 两次检查）。

    参数：h_* 列(横向)映射，v_* 行(纵向)映射，distance 这条腿的距离。
    返回：True=并进了现有组（好，不加趟数），False=开了新组（多一趟）。
    """
    for i, (h_group, v_group) in enumerate(groups):
        if compatible_with_group(h_key, h_value, h_group) and \
           compatible_with_group(v_key, v_value, v_group):
            h_group[h_key] = h_value        # 记入队形表
            v_group[v_key] = v_value
            # 这趟车的耗时看最远的原子 → 组内最长腿取 max
            max_distances[i] = max(max_distances[i], distance)
            return True
    groups.append(({h_key: h_value}, {v_key: v_value}))   # 开新趟
    max_distances.append(distance)
    return False


def groups_cost(max_distances: list[float]) -> float:
    """总分的主项：Σ 每组 √(最长腿)。

    开根号 = 匀加速运动模型（d/t²=a）；组数已经隐含在求和次数里——
    多一个组就多一项，天然惩罚"多拆一趟"。
    """
    return sum(sqrt(d) for d in max_distances)


def groups_sd(
    groups: list[tuple[dict[int, int], dict[int, int]]],
    scale: tuple[float, float] = (1.0, 1.0),
) -> float:
    """可选加分项（默认关闭，消融实验用）：队形"歪扭度"之和。

    对每个组算 (目标值 − scale×源值) 的标准差：间距越规整 SD 越小，
    队形像仪仗队平移（将来再塞人也不容易撞车）；歪七扭八则 SD 大。
    这是 ICCAD 论文 A* 启发函数的"加速段"思想，对完整解属于锦上添花，
    所以做成开关（use_sd）留作消融对照。
    """
    total = 0.0
    for h_group, v_group in groups:
        for g, s in ((h_group, scale[0]), (v_group, scale[1])):
            if not g:
                continue
            diffs = [v - s * k for k, v in g.items()]
            mean = sum(diffs) / len(diffs)
            var = sum((d - mean) ** 2 for d in diffs) / len(diffs)
            total += sqrt(var)
    return total


def discretize(values: list[float]) -> list[int]:
    """坐标 → 次序（秩）。例如 [10.0, 30.0, 10.0, 20.0] → [0, 2, 0, 1]。

    为什么扔掉绝对坐标：AOD 的规矩只关心原子之间的【相对顺序】
    （谁在谁上面/左边），绝对位置是冗余信息。离散化后 key/value 都是
    小整数，兼容判定和标准差计算都更稳定。
    """
    order = {v: i for i, v in enumerate(sorted(set(values)))}
    return [order[v] for v in values]
