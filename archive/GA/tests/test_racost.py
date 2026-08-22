"""Unit tests for the routing-aware cost port (run with any python3, no deps)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zga.racost import (add_to_groups, compatible_with_group, discretize,
                        groups_cost, groups_sd)


def test_same_key_same_value_compatible():
    g = {0: 5}
    assert compatible_with_group(0, 5, g)          # preservation: same source row -> same target
    assert not compatible_with_group(0, 6, g)      # same source row must not split


def test_order_preservation():
    g = {0: 0, 2: 4}
    assert compatible_with_group(1, 2, g)          # fits between: 0 < 2 < 4
    assert not compatible_with_group(1, 5, g)      # crossing: would exceed upper
    assert not compatible_with_group(1, 4, g)      # merging: equals upper value
    assert not compatible_with_group(1, 0, g)      # merging: equals lower value


def test_boundaries():
    g = {3: 7}
    assert compatible_with_group(1, 5, g)          # below: only needs value < upper
    assert not compatible_with_group(1, 8, g)
    assert compatible_with_group(5, 9, g)          # above: only needs value > lower
    assert not compatible_with_group(5, 6, g)


def test_add_to_groups_and_cost():
    groups, maxd = [], []
    assert add_to_groups(0, 0, 0, 0, 4.0, groups, maxd) is False   # new group
    assert add_to_groups(1, 1, 1, 2, 9.0, groups, maxd) is True    # joins (parallel shift)
    assert add_to_groups(0, 2, 0, 0, 1.0, groups, maxd) is False   # splits -> new group
    assert len(groups) == 2
    assert maxd == [9.0, 1.0]
    assert groups_cost(maxd) == 3.0 + 1.0                          # sqrt(9)+sqrt(1)
    # parallel shift has zero SD
    assert groups_sd([({0: 0, 1: 1}, {0: 0, 1: 1})]) == 0.0


def test_discretize():
    assert discretize([10.0, 20.0, 10.0, 30.0]) == [0, 1, 0, 2]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name}: PASS")
    print("all racost tests passed")
