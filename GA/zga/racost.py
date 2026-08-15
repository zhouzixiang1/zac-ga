"""Routing-aware cost model, ported from MQT-QMAP's HeuristicPlacer.cpp (verified lines):
  - checkCompatibilityWithGroup  (cpp:1296)  -> compatible_with_group
  - checkCompatibilityAndAddPlacement (cpp:1338) -> add_to_groups
  - getCost (cpp:1084) -> groups_cost  (sum of sqrt(dmax) per compatible group)
  - sumStdDeviationForGroups (cpp:1100) -> groups_sd  (with key scaling, for ablation)

Group semantics: a group holds one row map and one column map (discrete indices of
the moved atoms' source and target rows/columns). A new movement (key -> value) is
compatible with a group iff
  (1) the key already exists and maps to the same value (preservation), or
  (2) the key is new and the neighbouring values satisfy lower < value < upper
      (order preservation / no crossing / no merging).
"""
from __future__ import annotations

import bisect
from math import sqrt


def compatible_with_group(key: int, value: int, group: dict[int, int]) -> bool:
    keys = sorted(group)
    idx = bisect.bisect_left(keys, key)
    if idx < len(keys) and keys[idx] == key:
        return group[key] == value
    upper = group[keys[idx]] if idx < len(keys) else None
    lower = group[keys[idx - 1]] if idx > 0 else None
    if upper is not None and lower is not None:
        return lower < value < upper
    if upper is not None:
        return value < upper
    if lower is not None:
        return lower < value
    return True


def add_to_groups(
    h_key: int, h_value: int, v_key: int, v_value: int, distance: float,
    groups: list[tuple[dict[int, int], dict[int, int]]],
    max_distances: list[float],
) -> bool:
    for i, (h_group, v_group) in enumerate(groups):
        if compatible_with_group(h_key, h_value, h_group) and \
           compatible_with_group(v_key, v_value, v_group):
            h_group[h_key] = h_value
            v_group[v_key] = v_value
            max_distances[i] = max(max_distances[i], distance)
            return True
    groups.append(({h_key: h_value}, {v_key: v_value}))
    max_distances.append(distance)
    return False


def groups_cost(max_distances: list[float]) -> float:
    """goal-node cost from HeuristicPlacer.getCost: sum of sqrt(group dmax)."""
    return sum(sqrt(d) for d in max_distances)


def groups_sd(
    groups: list[tuple[dict[int, int], dict[int, int]]],
    scale: tuple[float, float] = (1.0, 1.0),
) -> float:
    """sum of standard deviations of (value - scale*key) over both orientations."""
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
    """map exact coordinates to discrete indices (rank among the moved atoms)."""
    order = {v: i for i, v in enumerate(sorted(set(values)))}
    return [order[v] for v in values]
