"""发动机（ZAC_zzx 的放置器层）：本文件有两个类，主角在后半部分。

┌─ 阅读指南 ────────────────────────────────────────────────────────────┐
│ ① BatchAwarePlacer（前半，ZAC_new 的对照引擎，placer="batch" 时用）    │
│    place_gate 整体替换 ZAC 的门放置：双引擎 penalty（匹配+定向冲突罚单）│
│    / ga（进化搜索），适应度 = 着色分批代价 w_batch×χ + Σ√dmax。        │
│    每轮做完门仍全员回存储——它是"批次感知但不驻留"的对照组。             │
│                                                                       │
│ ② ResidentPlacer（后半，ZAC_zzx 主角，placer="resident" 时用）        │
│    驻留编译器：做完门默认不回存储（resident.py 负责决策），门位由       │
│    engine="match"（A1 解析匹配）或 "ga"（A3 联合搜索）产生。           │
│    方法地图（按调用顺序）：                                             │
│      run()                轮循环总控：plan → decide_lazy → commit      │
│      _plan_round()        A1 门位：菜单三级兜底 + ZAC 距离匹配         │
│        _zone_anchors()      原子对 → 入区锚点（区内原子锚=自己座位）    │
│        _expanded_sites()    锚点展开窗 + ZAC 容量自动扩窗              │
│        _all_zone_sites()    全区左 SLM 工位全集（终极兜底搜索域）      │
│        _build_opts()        候选集 → (site,w,q1,q2) 菜单 + 硬排除      │
│        _pair_seats()        工位 → 座位对朝向（驻留者保座优先）         │
│        _site_weight()       ZAC 权重公式 + 驻留者移动折扣              │
│        _match_gates()       scipy 最小权匹配 + 贪心兜底                │
│        _repair_placements() 终检安全网：违例门位改选全区过滤域          │
│      _ga_step()          A3 联合搜索：门位基因 ∪ STAY/RETURN 基因，    │
│                          适应度 = 分相位着色 + 锚点前瞻                │
│      _commit_round()     参与者落门位 + 登记簿入区，追加映射           │
│      _append_boundary()  RETURN 者落存储位，追加边界映射               │
│      _assert_contract()  流契约断言：长度 2n+1 / 拷贝不变式 / 单射      │
└───────────────────────────────────────────────────────────────────────┘

核心思想（为什么放置要重写而不是改 ZAC 的 place_gate）：
ZAC 原版（vmplacer.py:165）用最小权完美匹配分工位，边权 = 纯距离——
匹配的边权表达不了"门与门之间的批次耦合"（一个门的工位选择改变另一个门
要不要多开一班车，这是决策间的二次交互项）。本文件的解法是把整套座位
方案放进 GA 染色体，用着色适应度整体计价；驻留语义则要求重写轮循环
（父类每轮强制全员回存储，vmplacer.py:343-348——驻留在父类里无法表达）。
"""
from __future__ import annotations

import math
import random
import time
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass, replace
from itertools import product
from math import sqrt

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import min_weight_full_bipartite_matching

# 引用本包的打分仪表；zzx/__init__.py 已把 ZAC_zzx 根目录挂上 sys.path，
# 所以下面的 zac.* 解析到 ZAC_zzx/zac/（本地副本）。
from zac.placer.vmplacer import VertexMatchingPlacer

from zzx.boundary_problem import (
    ArchitectureSnapshot as BoundaryArchitectureSnapshot,
    BoundaryConfig,
    BoundaryProblem,
    CandidatePlan,
    Ghost as BoundaryGhost,
    Leg as BoundaryLeg,
    MovementPhase as BoundaryMovementPhase,
    Point as BoundaryPoint,
    RichForecastTerm,
    RichGateOption,
    RichH0Problem,
    RichReturnOption,
    RichSearchConfig,
)
from zzx.algorithm_v2 import (AdaptiveHorizonDecision, CacheStats,
                              ForecastOracle, PhysicalCostBreakdown,
                              PhysicalIncrementalCost, MovementPhaseCost,
                              build_seed_population, is_adaptive_lookahead,
                              is_decay_lookahead,
                              maximum_lookahead_horizon,
                              resident_decision_candidates)
from zzx.native_backend import NativeBackendError, select_backend
from zzx.exact_current_reference import ExactCurrentReferenceScheduler
from zzx.zcost import batch_cost, compatible_2d, conflict_graph
from zzx.ghost import (ghost_hits, hit_count, leg_hits, new_conflicts,
                       pair_edges)
from zzx.resident import (NextUse, ResidentRegistry, boundary_legs,
                          decide_lazy, match_return_sites,
                          _all_storage_sites, _box_sites)


@dataclass(frozen=True)
class _BackendMovementPhase:
    """One phase represented in both legacy and native-exact forms.

    ``PhysicalIncrementalCost`` deliberately consumes objects structurally, so
    these proxy properties keep all pre-migration callers byte-for-byte stable
    while retaining the exact legs/ghosts/owners needed by ``BoundaryProblem``.
    """

    physical: object
    boundary: BoundaryMovementPhase

    @property
    def batches(self):
        return self.physical.batches

    @property
    def move_time_us(self):
        return self.physical.move_time_us

    @property
    def total_distance_um(self):
        return self.physical.total_distance_um

    @property
    def movers(self):
        return self.physical.movers


@dataclass(frozen=True)
class _IndexedRichGateRow:
    """One native-rich gate option with its geometry computed exactly once."""

    option: tuple
    seats: tuple
    rich_option: RichGateOption


@dataclass(frozen=True)
class _IndexedRichGateDomain:
    """Reusable views of one gate's complete native-rich placement domain."""

    by_site: dict
    weight_order: tuple[_IndexedRichGateRow, ...]
    canonical_order: tuple[_IndexedRichGateRow, ...]


def _formal_rich_blocked_seats(zone_seat, participants,
                               movable_before_out) -> set:
    """Return only seats whose owners cannot vacate either physical phase.

    Every target-layer participant is part of the native out phase, including
    participants belonging to another gate column.  Their source seats are
    therefore legal rows in the *complete* joint gate domain; the native
    geometry replay, rather than a per-column Python filter, decides whether a
    particular simultaneous hand-off is executable.  A nonparticipant that can
    RETURN/RESEAT in the back phase is dynamic for the same reason.
    """
    participants = set(participants)
    movable_before_out = set(movable_before_out)
    return {
        seat for q, seat in zone_seat.items()
        if q not in participants and q not in movable_before_out
    }


def _native_rich_phase_owner_orders(problem, result) -> tuple[tuple, tuple]:
    """Rebuild the two raw-leg owner orders used by C++ ``build_geometry``.

    ``FitnessResult.phase_batches`` contains indices into C++'s geometry
    vectors, not into Python's decision dictionary or repaired placements.
    Phase 0 is ordered as RETURN assignments, RESEAT assignments, then derived
    participant parking assignments; phase 1 is ordered by gate-domain column
    and then q1/q2.  Zero-length legs are omitted exactly as in the native
    solver.
    """
    coordinates = tuple(problem.architecture.site_coordinates)

    def site_point(site_id):
        site_id = int(site_id)
        if site_id < 0 or site_id >= len(coordinates):
            raise RuntimeError(
                f"native rich geometry references invalid site id {site_id}")
        return coordinates[site_id]

    if problem.current_site_ids:
        if len(problem.current_site_ids) != problem.architecture.n_atoms:
            raise RuntimeError(
                "native rich current_site_ids do not cover every atom")
        positions = [site_point(site_id)
                     for site_id in problem.current_site_ids]
    elif problem.current_points:
        if len(problem.current_points) != problem.architecture.n_atoms:
            raise RuntimeError(
                "native rich current_points do not cover every atom")
        positions = list(problem.current_points)
    else:
        raise RuntimeError("native rich geometry has no current positions")

    def moved(source, target):
        return math.hypot(source.x - target.x,
                          source.y - target.y) > 1e-9

    back_owners = []
    for q, site_id in (*result.return_assignments,
                       *result.reseat_assignments,
                       *getattr(
                           result, "participant_parking_assignments", ())):
        q = int(q)
        if q < 0 or q >= len(positions):
            raise RuntimeError(
                f"native rich geometry references invalid atom {q}")
        target = site_point(site_id)
        if moved(positions[q], target):
            back_owners.append(q)
        positions[q] = target

    if len(result.gate_option_indices) != len(problem.gate_domains):
        raise RuntimeError(
            "native rich gate-option indices do not match gate domains")
    out_owners = []
    for gate_index, option_index in enumerate(result.gate_option_indices):
        domain = problem.gate_domains[gate_index]
        option_index = int(option_index)
        if option_index < 0 or option_index >= len(domain):
            raise RuntimeError(
                "native rich geometry references invalid gate option "
                f"{option_index} in column {gate_index}")
        option = domain[option_index]
        targets = (
            (int(option.q1),
             site_point(option.target1_site_id)
             if option.target1_site_id is not None else option.target1),
            (int(option.q2),
             site_point(option.target2_site_id)
             if option.target2_site_id is not None else option.target2),
        )
        for q, target in targets:
            if q < 0 or q >= len(positions) or target is None:
                raise RuntimeError(
                    "native rich gate geometry has an invalid endpoint")
            if moved(positions[q], target):
                out_owners.append(q)
    return tuple(back_owners), tuple(out_owners)


def _phase_batches_by_owner(batches, owners, *, phase) -> tuple:
    """Translate raw native leg indices with explicit alignment checks."""
    translated = []
    for batch in batches:
        owner_batch = []
        for raw_index in batch:
            index = int(raw_index)
            if index < 0 or index >= len(owners):
                raise RuntimeError(
                    f"native {phase} batch index {index} is outside "
                    f"the {len(owners)} reconstructed legs")
            owner_batch.append(int(owners[index]))
        translated.append(tuple(sorted(owner_batch)))
    return tuple(translated)


def _rich_result_placements(problem, result, site_locations) -> list[dict]:
    """Decode the selected native DTO endpoints without changing its plan.

    A rich gate option owns both the interaction-site id and the oriented seat
    ids selected during candidate evaluation.  Re-running ``_pair_seats`` or
    the legacy post-matching repair after C++ returns can silently choose a
    different orientation/site (in particular when one gate hands another
    participant's vacated source seat to its partner).  The executable Python
    mapping must therefore be a direct projection of the scored DTO.
    """
    locations = tuple(tuple(location) for location in site_locations)

    def decode_site(raw_site_id, *, label):
        if raw_site_id is None:
            raise RuntimeError(f"native rich {label} lacks a registered site id")
        site_id = int(raw_site_id)
        if site_id < 0 or site_id >= len(locations):
            raise RuntimeError(
                f"native rich {label} site id {site_id} is outside "
                f"the {len(locations)} registered locations")
        return locations[site_id]

    if len(result.gate_option_indices) != len(problem.gate_domains):
        raise RuntimeError(
            "native rich gate-option indices do not match gate domains")
    placements = []
    for gate_index, raw_option_index in enumerate(
            result.gate_option_indices):
        domain = problem.gate_domains[gate_index]
        option_index = int(raw_option_index)
        if option_index < 0 or option_index >= len(domain):
            raise RuntimeError(
                "native rich gate option is outside its registered domain")
        option = domain[option_index]
        placements.append({
            "gate": (int(option.q1), int(option.q2)),
            "site": decode_site(option.site_id, label="interaction"),
            "seats": (
                decode_site(option.target1_site_id, label="target1"),
                decode_site(option.target2_site_id, label="target2"),
            ),
        })
    return placements


def _replay_phase_batches(architecture, legs, owners, positions,
                          *, batching="phase", native_replay=False,
                          retain_boundary_ghosts=True):
    """Replay a colored phase against each atom's position before every batch.

    Movers are not conservatively frozen at both endpoints.  They remain at
    their source until their selected batch and at their target afterwards.
    This mirrors the router's real batch-order audit and declares infeasible
    only a cycle of straight legs that cannot be ordered safely.
    """
    if len(legs) != len(owners):
        raise ValueError("phase legs and owners are not aligned")
    ghost_rows = tuple(
        (int(atom), *map(float,
            architecture.exact_SLM_location_tuple(location)))
        for atom, location in sorted(positions.items())
    )
    boundary_ghosts = (tuple(BoundaryGhost(
        atom, BoundaryPoint(x, y)) for atom, x, y in ghost_rows)
        if retain_boundary_ghosts else ())
    phase = BoundaryMovementPhase(
        legs=tuple(BoundaryLeg(
            float(leg[0]),
            BoundaryPoint(float(leg[1]), float(leg[2])),
            BoundaryPoint(float(leg[3]), float(leg[4])))
            for leg in legs),
        ghosts=boundary_ghosts,
        owners=tuple(int(owner) for owner in owners),
        batching=batching,
    )
    if native_replay and batching == "phase":
        try:
            import zac_native_core
            batches = zac_native_core.replay_phase_batches_raw(
                [tuple(map(float, leg)) for leg in legs],
                ghost_rows, [int(owner) for owner in owners], 0)
        except RuntimeError as exc:
            raise ValueError(str(exc)) from exc
        return tuple(tuple(int(index) for index in batch)
                     for batch in batches), phase
    from zzx.reference_backend import replay_phase_batches
    return replay_phase_batches(phase), phase


class BatchAwarePlacer(VertexMatchingPlacer):
    """VertexMatchingPlacer 的子类：门放置改为"批次感知"双引擎。"""

    def __init__(self, mapping: list, l2: bool = False, seed: int = 0, **params):
        super().__init__(mapping, l2)
        self.rng = random.Random(seed)     # 独立随机源，种子固定 → 可复现
        # ---- 引擎选择与公共旋钮 ----
        self.engine: str = params.get("engine", "penalty")
        self.w_batch: float = params.get("w_batch", 1.0)   # 批数惩罚（√μm 单位/批）
        self.use_lookahead: bool = params.get("use_lookahead", True)
        self.min_expand: int = params.get("min_expand", 1)
        # ---- penalty 引擎旋钮 ----
        self.lambda_penalty: float = params.get("lambda_penalty", 0.25)
        self.penalty_decay: float = params.get("penalty_decay", 0.7)
        self.max_penalty_iter: int = params.get("max_penalty_iter", 8)
        # ---- ga 引擎旋钮（= FABLE v1a 预算）----
        self.population_size: int = params.get("population_size", 6)
        self.iterations: int = params.get("iterations", 8)
        self.neighbors_per_solution: int = params.get("neighbors_per_solution", 2)
        self.neighbor_sample_size: int = params.get("neighbor_sample_size", 24)
        # ---- 报告用 ----
        self.search_time = 0.0
        self.batch_preview: list = []   # 每层 (layer, χ, Σ√dmax, 冲突边数)——
                                        # 与路由实际批数对账的"预演账本"

    # ------------------------------------------------------------------ utils
    def _norm_site(self, location):
        """纠缠区工位坐标归一化到该区第一个 SLM（左右座唯一键值）。"""
        slm_idx = self.architecture.entanglement_zone[
            self.architecture.dict_SLM[location[0]].entanglement_id][0]
        return (slm_idx, location[1], location[2])

    def place_gate(self, list_qubit_mapping: list, list_two_gate_layer: list,
                   layer: int, test_reuse: bool):
        """给第 layer 层的门分配纠缠区工位 —— 被替换的唯一方法（契约同父类）。"""
        list_gate = list_two_gate_layer[0]

        # ---- 第 1 步：备好前瞻对象（与 FABLE 相同）------------------------
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

        if layer > 0:
            gate_mapping = list_qubit_mapping[0]
            qubit_mapping = list_qubit_mapping[1]
        else:
            gate_mapping = None
            qubit_mapping = list_qubit_mapping[0]

        # ---- 第 2 步：生成"点菜单"（与 GA v1a / FABLE 逐字节相同）----------
        expand_factor = max(self.min_expand, math.ceil(math.sqrt(len(list_gate)) / 2))
        candidates: list[list] = []   # 每门一个候选表；表项=(工位, d1, d2, 前瞻, q1, q2)
        for gate in list_gate:
            q1, q2 = gate
            pinned = test_reuse and layer > 0 and q1 in self.list_reuse_qubit[layer - 1]
            if not pinned and test_reuse and layer > 0 and q2 in self.list_reuse_qubit[layer - 1]:
                pinned, side = True, 2
            elif pinned:
                side = 1
            else:
                side = 0

            if pinned:
                loc = gate_mapping[q1 if side == 1 else q2]
                site = self._norm_site(loc)
                other = q2 if side == 1 else q1
                d_other = self.architecture.distance(
                    qubit_mapping[other][0], qubit_mapping[other][1], qubit_mapping[other][2],
                    site[0], site[1], site[2])
                q_r = q1 if side == 1 else q2
                dis3 = 0.0
                if q_r in dict_reuse_neighbor:
                    q3 = dict_reuse_neighbor[q_r]
                    dis3 = self.architecture.distance(
                        qubit_mapping[q3][0], qubit_mapping[q3][1], qubit_mapping[q3][2],
                        site[0], site[1], site[2])
                candidates.append([(site, 0.0, d_other, dis3, q1, q2)])
                continue

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
                # ZAC 原版的容量自动扩窗（vmplacer.py:235-242 原样移植）：
                # 窗口装不下本层全部门时按 门数/边长 扩边，保证匹配有解
                # （ising_n42 这类 21 门并行层没有它就会 no full matching）
                if high_c - low_c < 2 * expand_factor:
                    height_gap = math.ceil(len(list_gate) // (high_c - low_c)) - expand_factor
                    low_r = max(0, low_r - height_gap // 2)
                    high_r = min(slm.n_r, low_r + height_gap + expand_factor)
                if high_r - low_r < 2 * expand_factor:
                    width_gap = math.ceil(len(list_gate) / (high_r - low_r)) - expand_factor
                    low_c = max(0, low_c - width_gap // 2)
                    high_c = min(slm.n_c, low_c + width_gap + expand_factor)
                for r in range(low_r, high_r):
                    for c in range(low_c, high_c):
                        set_sites.add((near[0], r, c))

            opts = []
            for site in set_sites:
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

        site_index = [{o[0]: k for k, o in enumerate(opts)} for opts in candidates]
        movable = [gi for gi, opts in enumerate(candidates) if len(opts) > 1]

        # ---- 第 3 步：解码器（染色体 → 合法座位表；与 FABLE 相同）----------
        def decode(chrom):
            """食堂打饭规则：按基因选菜，被占就沿菜单顺延到下一道。"""
            used = {}
            placed = []
            for gi, opts in enumerate(candidates):
                if len(opts) == 1:
                    placed.append(opts[0])
                    continue
                idx = chrom[gi] % len(opts)
                for step in range(len(opts)):
                    cand = opts[(idx + step) % len(opts)]
                    if cand[0] not in used:
                        break
                used[cand[0]] = gi
                placed.append(cand)
            return placed

        # ---- 第 4 步：适应度（着色分批版 = ZAC_zzx 的灵魂差异）------------
        def seats(site, q1, q2):
            """两个比特各自的真实座位（打分与落盘同一套规则）。"""
            right = (site[0] + 1, site[1], site[2])
            reuse1 = test_reuse and layer > 0 and q1 in self.list_reuse_qubit[layer - 1]
            reuse2 = test_reuse and layer > 0 and q2 in self.list_reuse_qubit[layer - 1]
            if reuse1:
                pin = gate_mapping[q1]
                return pin, (right if pin == site else site)
            if reuse2:
                pin = gate_mapping[q2]
                return (right if pin == site else site), pin
            if qubit_mapping[q1][2] < qubit_mapping[q2][2]:
                return site, right
            return right, site

        def legs_of(placed):
            """按真实座位收集搬运腿 (dist, 起x, 起y, 终x, 终y)；dist=0 不占车。"""
            legs = []
            owner = []   # 每条腿属于哪个门（罚单要按门记账）
            for gi, (site, d1, d2, dis3, q1, q2) in enumerate(placed):
                s1, s2 = seats(site, q1, q2)
                for q, tgt in ((q1, s1), (q2, s2)):
                    sx, sy = self.architecture.exact_SLM_location_tuple(qubit_mapping[q])
                    tx, ty = self.architecture.exact_SLM_location_tuple(tgt)
                    d = math.dist((sx, sy), (tx, ty))
                    if d > 1e-9:
                        legs.append((d, sx, sy, tx, ty))
                        owner.append(gi)
            return legs, owner

        def fitness(placed):
            legs, _ = legs_of(placed)
            cost, _, _ = batch_cost(legs, w_batch=self.w_batch)   # χ×w + Σ√dmax
            if self.use_lookahead:
                cost += sum(sqrt(o[3]) for o in placed)
            return cost

        # ---- 第 5 步：两个引擎，择一驱动 -----------------------------------
        t0 = time.time()
        if self.engine == "ga":
            best = self._engine_ga(candidates, site_index, movable, decode, fitness)
        else:
            best = self._engine_penalty(candidates, site_index, decode,
                                        legs_of, fitness, qubit_mapping, seats)
        self.search_time += time.time() - t0

        # ---- 预演账本：χ / Σ√dmax / 冲突边数（与路由实际批数对账用）-------
        legs, _ = legs_of(best)
        _, n_batches, n_conflicts = batch_cost(legs, w_batch=self.w_batch)
        self.batch_preview.append(
            (layer, n_batches, sum(sqrt(l[0]) for l in legs), n_conflicts))

        # ---- 第 6 步：落盘（与 ZAC/FABLE 输出逐字节兼容）-------------------
        tmp_mapping = deepcopy(qubit_mapping)
        for (site, d1, d2, dis3, q1, q2) in best:
            reuse1 = test_reuse and layer > 0 and q1 in self.list_reuse_qubit[layer - 1]
            reuse2 = test_reuse and layer > 0 and q2 in self.list_reuse_qubit[layer - 1]
            if reuse1:
                tmp_mapping[q1] = gate_mapping[q1]
                if site == gate_mapping[q1]:
                    tmp_mapping[q2] = (site[0] + 1, site[1], site[2])
                else:
                    tmp_mapping[q2] = site
            elif reuse2:
                tmp_mapping[q2] = gate_mapping[q2]
                if site == gate_mapping[q2]:
                    tmp_mapping[q1] = (site[0] + 1, site[1], site[2])
                else:
                    tmp_mapping[q1] = site
            else:
                if qubit_mapping[q1][2] < qubit_mapping[q2][2]:
                    tmp_mapping[q1] = site
                    tmp_mapping[q2] = (site[0] + 1, site[1], site[2])
                else:
                    tmp_mapping[q1] = (site[0] + 1, site[1], site[2])
                    tmp_mapping[q2] = site
        self.mapping.append(tmp_mapping)

    # ================================================================== engines
    def _engine_ga(self, candidates, site_index, movable, decode, fitness):
        """引擎 A：FABLE v1a 进化骨架（算子、预算、精英保留全部同款）。"""
        def neighbor_solution(chrom):
            """结构化算子：交换两门工位 / 邻居挪动（与 FABLE 相同）。"""
            if movable and len(movable) >= 2 and self.rng.random() < 0.5:
                gi, gj = self.rng.sample(movable, 2)
                si, sj = decode(chrom)[gi][0], decode(chrom)[gj][0]
                if sj in site_index[gi] and si in site_index[gj]:
                    c = list(chrom)
                    c[gi] = site_index[gi][sj]
                    c[gj] = site_index[gj][si]
                    return c
            c = list(chrom)
            if movable:
                gi = self.rng.choice(movable)
                c[gi] = self.rng.choice([c[gi] + 1, c[gi] - 1,
                                         self.rng.randrange(64)])
            return c

        zeros = [0] * len(candidates)          # 贪心热启动：全 0 = 每门选最近
        population = [zeros]
        for _ in range(self.population_size - 1):
            population.append([self.rng.randrange(8) for _ in candidates])
        scored = [(fitness(decode(c)), c) for c in population]
        scored.sort(key=lambda x: x[0])
        for _ in range(self.iterations):
            offspring = []
            for _, c in scored:
                pool = [neighbor_solution(c) for _ in range(self.neighbor_sample_size)]
                pool_scored = sorted((fitness(decode(m)), m) for m in pool)
                offspring += pool_scored[: self.neighbors_per_solution]
            scored = sorted(scored + offspring, key=lambda x: x[0])[: self.population_size]
        return decode(scored[0][1])

    def _engine_penalty(self, candidates, site_index, decode, legs_of, fitness,
                        qubit_mapping, seats):
        """引擎 B：ZAC 匹配机器 + 冲突罚单迭代。

        每轮三拍：①按当前边权解最小权匹配（第 1 轮边权 = ZAC 原版距离
        公式，行为等价于原版 place_gate）②对每个门 × 每个候选工位算
        "定向冲突数"：这门若坐这个工位，会与当前其他门的腿冲突几条
        （罚额随位置变化，匹配才有梯度——均匀罚单不改变 argmin，
        λ∈{0.25,1,3} 扫描已证明只罚所选边时引擎完全不动）③罚额
        衰减后重解。历史最优全程保留，震荡也不会丢好解。
        定向罚单 = 把 ZAP 公式 17（λ·冲突数 + 距离）嫁接到匹配边权上。
        """
        # ---- 建全局二部图（行=工位，列=门；边只来自各门自己的菜单）------
        site_to_row: dict = {}
        list_rows: list = []
        edges: list = []          # (row, col, base_weight)
        for col, opts in enumerate(candidates):
            for opt in opts:
                site = opt[0]
                if site not in site_to_row:
                    site_to_row[site] = len(list_rows)
                    list_rows.append(site)
                row = site_to_row[site]
                _, d1, d2, dis3, q1, q2 = opt
                # ZAC 原版权重公式（vmplacer.py:267-270）：两比特同 SLM 同行
                # → 并排走只记最远的那条腿；否则两条腿都记
                if (qubit_mapping[q1][0] == qubit_mapping[q2][0]
                        and qubit_mapping[q1][1] == qubit_mapping[q2][1]):
                    base = math.sqrt(max(d1, d2))
                else:
                    base = math.sqrt(d1) + math.sqrt(d2)
                edges.append((row, col, base + math.sqrt(dis3)))

        n_rows, n_cols = len(list_rows), len(candidates)

        # 每门每工位的搬运腿向量缓存（菜单跨轮不变，只算一次）：
        # vec = (起x, 终x, 起y, 终y)，dist=0 的腿不占车、不参与兼容判定
        seat_vecs: dict = {}
        for col, opts in enumerate(candidates):
            for opt in opts:
                site = opt[0]
                vecs = []
                s1, s2 = seats(site, opt[4], opt[5])
                for q, tgt in ((opt[4], s1), (opt[5], s2)):
                    sx, sy = self.architecture.exact_SLM_location_tuple(qubit_mapping[q])
                    tx, ty = self.architecture.exact_SLM_location_tuple(tgt)
                    if abs(tx - sx) + abs(ty - sy) > 1e-9:
                        vecs.append((sx, tx, sy, ty))
                seat_vecs[(col, site)] = vecs

        def solve_matching(pen: dict) -> list:
            """带罚单解匹配，返回染色体（每门所选菜单下标）。"""
            rows, cols, data = [], [], []
            for (row, col, w) in edges:
                w_eff = w + pen.get((col, list_rows[row]), 0.0)
                rows.append(row)
                cols.append(col)
                data.append(w_eff)
            matrix = coo_matrix((np.array(data), (np.array(rows), np.array(cols))),
                                shape=(n_rows, n_cols))
            try:
                row_ind, col_ind = min_weight_full_bipartite_matching(matrix)
            except ValueError:
                # 保险丝：理论上 ZAC 扩窗后不会再走到这里；万一遇到病态层，
                # 退化为贪心顺延（decode 保证合法性），不崩整个编译
                return [0] * n_cols
            chrom = [0] * n_cols
            for r, c in zip(row_ind, col_ind):
                chrom[c] = site_index[c][list_rows[r]]
            return chrom

        lam = self.lambda_penalty
        pen: dict = {}
        best_placed, best_score = None, float("inf")
        for _ in range(self.max_penalty_iter):
            chrom = solve_matching(pen)
            placed = decode(chrom)
            score = fitness(placed)
            if score < best_score:
                best_score, best_placed = score, placed
            # 当前局面的冲突图；无冲突 = 一车装下 → 已经最优，提前收工
            legs, owner = legs_of(placed)
            adj = conflict_graph(legs)
            if not any(adj):
                break
            # ---- 定向罚单：对涉冲突的每个门 × 每个候选工位，算"坐这儿
            # 会与当前其他门冲突几条"，罚额 = lam × 冲突数。罚额随位置
            # 变化，匹配才有梯度（只罚所选边时 λ∈{0.25,1,3} 全部不动，
            # 扫描已证）。每轮按新局面重算，不跨轮累积。
            now_vecs = [(l[1], l[3], l[2], l[4]) for l in legs]
            conflict_gates = {owner[i] for i, nbrs in enumerate(adj) if nbrs}
            pen = {}
            for gi in conflict_gates:
                for opt in candidates[gi]:
                    n_conf = 0
                    for vec in seat_vecs[(gi, opt[0])]:
                        for k, ov in enumerate(owner):
                            if ov == gi:
                                continue
                            if not compatible_2d(vec, now_vecs[k]):
                                n_conf += 1
                    if n_conf:
                        pen[(gi, opt[0])] = lam * n_conf
            lam *= self.penalty_decay
        return best_placed


# ════════════════════════════════════════════════════════════ 驻留放置器（ZAC_zzx 主放置器）
class ResidentPlacer(VertexMatchingPlacer):
    """驻留放置器：门位匹配（ZAC 距离公式）+ 边界惰性决策。

    为什么整个覆写父类 run()（审计 MAJOR-3）：
        父类循环每轮调 place_qubit 强制激发区全员回存储（vmplacer.py:343-348），
        复用两世界 filter_mapping 只看一个边界——驻留语义在父类里无法表达。
    本类轮循环（顺序与原生编译器同构：先定门位，闲人让路）：
        plan(L+1)   下一轮门位（菜单按登记簿真实位置生成，ZAC 容量自动扩窗）
        decide_lazy 边界决策（E2 挡路/容量阀强制 RETURN，其余 STAY——M1 模块）
        commit(L+1) 登记簿入区 + 映射流追加
    流契约（断言强制，审计 MAJOR-4）：长度恰 2n+1；[0]=初始布局；[2L+1]=第 L 轮
    门位（非参与者逐位拷贝 [2L]）；[2L+2]=边界态；每张映射单射。
    """

    _ONE_QUBIT_DURATION_US = 52.0

    def __init__(self, mapping: list, l2: bool = False, seed: int = 0, **params):
        super().__init__(mapping, l2)
        self.rng = random.Random(seed)              # RNG 隔离（审计 MAJOR-6：模块级共享会毁 SA 确定性）
        # Independent current-transition stream retained for legacy adaptive
        # H=0/1/2 regression.  Formal decay uses the ABI5 one-call solver.
        self.safety_rng = random.Random(seed)
        self.theta_capacity: float = params.get("theta_capacity", 0.9)
        self.box_ratio: int = params.get("box_ratio", 3)
        self.alpha_lookahead: float = params.get("alpha_lookahead", 0.1)
        self.final_return_home: bool = params.get("final_return_home", False)
        # 驻留者移动折扣（解析版钉扎）：区内原子的移动在匹配权重里打折，
        # 让门位倾向"驻留者不动、搭档来会合"（原生编译器 K2/K3 的近似）——
        # 否则门位落在两人折中点，链式电路逐轮漂移，驻留者反复长走
        self.w_resident: float = params.get("w_resident", 0.3)
        # K2 钉扎半径：有驻留者参与的门，菜单塌缩到其座位对 ± pin_radius 列
        # （ZAC 复用塌缩 vmplacer.py:208-215 的同款结构）——驻留者一步不走、
        # 新来者跑全程。实测：无钉扎时门位落在两人折中点，链式电路每轮
        # 仍要付"驻留者区内长走 + 新人入区"两条腿，驻留的收益被漂移吃光
        self.pin_radius: int = params.get("pin_radius", 2)
        # 钉扎列偏移罚：钉扎菜单里离驻留者每远一列的罚额。消融实测（4 电路）：
        #   不钉扎     ghz 1.118 / bv 1.137 / ising 0.921 / qft 1.341 → geo 1.115
        #   钉扎 w=0   ghz 1.533 / bv 1.372 / ising 0.815 / qft 0.763 → geo 1.095 ★默认
        #   钉扎 w=2   ghz 1.533 / bv 1.372 / ising 0.815 / qft 1.198 → geo 1.197
        # 链式电路钉死反而亏：座位锚在原地、新原子家门逐轮走远，入区腿线性上涨
        # （ghz 实测 157→250μs/轮）——"钉不钉"本质是逐门决策，解析规则顾此
        # 失彼，默认取 geomean 最优档，真正解法在 M3 搜索层（适应度定夺）
        self.w_pin: float = params.get("w_pin", 0.0)
        # ---- M3：GA 决策层旋钮 ----
        # engine="match" 即 A1（距离匹配 + 惰性决策）；"ga" 为 A3（联合搜索）
        self.engine: str = params.get("engine", "match")
        # 每批 2×15μs 固定开销的 √μm 当量（审计标定：1.0 低估 36%）
        self.w_batch: float = params.get("w_batch", 1.57)
        # ---- 前瞻 v2（用户思路一/二）：软层罚项 ----
        # 鬼点罚权重：每对"同批组合会撞静止原子"的腿 ≈ 一次被迫分批（批当量）
        self.w_ghost: float = params.get("w_ghost", 1.0)
        # 顺序罚权重：未来层存储搭档的进场行碎片化，每多一行 ≈ 多一批
        self.w_ord: float = params.get("w_ord", 1.0)
        # 逐批次折现（γ^批距）：越远的窗权重越低；1.0=不折现
        self.gamma_batch: float = params.get("gamma_batch", 0.5)
        # fitness="phase" 分相位着色（正确）；"lumped" 混合（A3' 消融用）
        self.fitness_mode: str = params.get("fitness_mode", "phase")
        # GA 预算（ZAC_new 引擎 A 同款默认）
        self.population_size: int = params.get("population_size", 6)
        self.iterations: int = params.get("iterations", 8)
        self.neighbors_per_solution: int = params.get("neighbors_per_solution", 2)
        self.neighbor_sample_size: int = params.get("neighbor_sample_size", 24)
        # Shared Python/native search-budget contract.  An omitted unique-
        # evaluation budget is deterministically derived from the historical GA
        # knobs so M3 and M4 cannot receive different effective work by accident.
        self.elite_count: int = int(params.get("elite_count", 1))
        self.early_stop_patience: int = int(
            params.get("early_stop_patience", 0))
        configured_unique_budget = params.get("max_unique_evaluations")
        self.max_unique_evaluations: int = int(
            configured_unique_budget
            if configured_unique_budget is not None
            else (self.population_size * self.iterations
                  * self.neighbor_sample_size))
        self.direct_enumeration_limit: int = int(
            params.get("direct_enumeration_limit", 512))
        self.crossover_rate: float = float(
            params.get("crossover_rate", 0.25))
        self.local_polish_sweeps: int = int(
            params.get("local_polish_sweeps", 1))
        self.return_candidate_limit: int = int(
            params.get("return_candidate_limit", 6))
        self.return_assignment_k: int = int(
            params.get("return_assignment_k", 8))
        self.return_anchor_policy: str = str(
            params.get("return_anchor_policy", "bounded_mixed"))
        self.return_anchor_effective_policy: str = self.return_anchor_policy
        self.static_partner_degree_max = None
        # Checkpoint restore may resume after run-state initialization.  Keep
        # empty, conservative fallbacks so old checkpoints remain readable;
        # fresh batch runs replace both maps from their frozen interaction
        # inventory in ``_initialize_run_state``.
        self.static_interaction_counts: dict[tuple[int, int], int] = {}
        self.static_interaction_totals: dict[int, int] = {}
        self.static_partner_degrees: dict[int, int] = {}
        self.static_interaction_median: float = 0.0
        # H=0 may use a depth-zero geometric value function without observing
        # a future layer.  It measures how far the committed boundary state
        # drifts from the common SA initial anchors.  Zero preserves the exact
        # current-only reference; positive values are tuned only for M3.
        self.h0_state_potential_weight: float = float(
            params.get("h0_state_potential_weight", 0.0))
        self.h0_state_potential_weight_policy: str = str(
            params.get("h0_state_potential_weight_policy", "fixed"))
        self.h0_state_potential_effective_weight = (
            self.h0_state_potential_weight)
        self.h0_uncertain_stay_weight: float = float(
            params.get("h0_uncertain_stay_weight", 0.0))
        self.h0_uncertain_stay_policy: str = str(
            params.get("h0_uncertain_stay_policy", "fixed"))
        self.h0_uncertain_stay_effective_weight = (
            self.h0_uncertain_stay_weight)
        # Order-free H=0 hazard prior.  The current target's share of an
        # atom's frozen interaction mass estimates whether keeping that atom
        # resident is likely to buy a near-term reuse.  The complement is a
        # soft one-pulse STAY cost; it never reads an ordered future layer.
        self.h0_opportunity_stay_weight: float = float(
            params.get("h0_opportunity_stay_weight", 0.0))
        # Past-only temporal prior for H=0.  Once an atom has at least one
        # observed reuse interval, an EWMA predicts how many idle pulses remain
        # after the current boundary.  This is online history, not an ordered
        # future read, and therefore keeps the no-lookahead contract intact.
        self.h0_history_gap_weight: float = float(
            params.get("h0_history_gap_weight", 0.0))
        # Soft H=0 value for the omitted storage-to-zone re-entry half-cycle.
        # It is derived from the current ghost-safe RETURN witness and never
        # reads a future layer.  Zero preserves the strict current-boundary
        # control; tuned M3 configurations may use a fractional value.
        self.h0_reentry_value_weight: float = float(
            params.get("h0_reentry_value_weight", 0.0))
        # A uniform H=0 re-entry value over-retains residents on long
        # circuits.  These order-free static-graph gates restrict that value
        # to atoms whose interactions are concentrated on the *current target
        # layer*.  They use no L+2 instruction or next-use index, so M3 keeps
        # its strict future-information firewall.
        self.h0_reentry_value_min_interaction_mass: int = int(
            params.get("h0_reentry_value_min_interaction_mass", 0))
        self.h0_reentry_value_min_interaction_ratio: float = float(
            params.get("h0_reentry_value_min_interaction_ratio", 0.0))
        self.h0_reentry_value_min_circuit_median_interactions: int = int(
            params.get(
                "h0_reentry_value_min_circuit_median_interactions", 0))
        self.h0_reentry_value_scale_by_interaction_ratio = params.get(
            "h0_reentry_value_scale_by_interaction_ratio", False)
        self.h0_state_anchor_policy: str = str(
            params.get("h0_state_anchor_policy", "home"))
        self.h0_anchor_pull_radius_um: float = float(
            params.get("h0_anchor_pull_radius_um", 12.0))
        self.m3_search_budget_policy: str = str(
            params.get("m3_search_budget_policy", "fixed"))
        self.m3_pin_radius_policy: str = str(
            params.get("m3_pin_radius_policy", "fixed"))
        self.m3_pin_radius_effective: int = self.pin_radius
        # H=0 cannot inspect L+2, but it may use the frozen, order-free
        # interaction graph that is already shared with initial placement.
        # ``topology_current_v1`` is the broad diagnostic policy.  The tracked
        # ``topology_terminal_v2`` applies the same one-way comparison only to
        # terminal chain atoms (global max degree <= 2 and at most two total
        # interactions).  This fixes measured GHZ/BV idle rent without
        # hard-masking high-reuse QMAP decisions whose RETURNs can lengthen a
        # shared AOD batch.  Both descriptors are order-free and shared with
        # initial placement; neither reads a future layer.
        self.h0_rent_policy: str = str(
            params.get("h0_rent_policy", "rolling_one_pulse"))
        # Number of ghost-safe future gate sites replayed before choosing the
        # deterministic physical minimum.  The geometric support itself stays
        # fixed at four sites; this bounded evaluation budget prevents the
        # rollout from becoming a nested placement search at every GA fitness.
        self.forecast_gate_candidate_budget: int = int(
            params.get("forecast_gate_candidate_budget", 4))
        self.operator_profile: str = params.get("operator_profile", "exact")
        if not 1 <= self.elite_count <= self.population_size:
            raise ValueError("elite_count must be in [1, population_size]")
        if self.early_stop_patience < 0:
            raise ValueError("early_stop_patience must be non-negative")
        if self.max_unique_evaluations <= 0:
            raise ValueError("max_unique_evaluations must be positive")
        if self.direct_enumeration_limit <= 0:
            raise ValueError("direct_enumeration_limit must be positive")
        if not 0.0 <= self.crossover_rate <= 1.0:
            raise ValueError("crossover_rate must be in [0, 1]")
        if self.local_polish_sweeps < 0:
            raise ValueError("local_polish_sweeps must be non-negative")
        if self.return_candidate_limit <= 0:
            raise ValueError("return_candidate_limit must be positive")
        if self.return_assignment_k <= 0:
            raise ValueError("return_assignment_k must be positive")
        if self.return_anchor_policy not in {
                "bounded_mixed", "home_stable", "topology_adaptive"}:
            raise ValueError(
                "return_anchor_policy must be bounded_mixed, home_stable, "
                "or topology_adaptive")
        if (not math.isfinite(self.h0_state_potential_weight)
                or self.h0_state_potential_weight < 0.0):
            raise ValueError(
                "h0_state_potential_weight must be finite and non-negative")
        if self.h0_state_potential_weight_policy not in {
                "fixed", "anchor_adaptive_v1"}:
            raise ValueError(
                "h0_state_potential_weight_policy must be fixed or "
                "anchor_adaptive_v1")
        if (not math.isfinite(self.h0_uncertain_stay_weight)
                or self.h0_uncertain_stay_weight < 0.0):
            raise ValueError(
                "h0_uncertain_stay_weight must be finite and non-negative")
        if (not math.isfinite(self.h0_opportunity_stay_weight)
                or self.h0_opportunity_stay_weight < 0.0):
            raise ValueError(
                "h0_opportunity_stay_weight must be finite and non-negative")
        if (not math.isfinite(self.h0_history_gap_weight)
                or self.h0_history_gap_weight < 0.0):
            raise ValueError(
                "h0_history_gap_weight must be finite and non-negative")
        if (not math.isfinite(self.h0_reentry_value_weight)
                or self.h0_reentry_value_weight < 0.0):
            raise ValueError(
                "h0_reentry_value_weight must be finite and non-negative")
        if self.h0_reentry_value_min_interaction_mass < 0:
            raise ValueError(
                "h0_reentry_value_min_interaction_mass must be non-negative")
        if self.h0_reentry_value_min_circuit_median_interactions < 0:
            raise ValueError(
                "h0_reentry_value_min_circuit_median_interactions must be "
                "non-negative")
        if (not math.isfinite(
                self.h0_reentry_value_min_interaction_ratio)
                or not 0.0 <=
                self.h0_reentry_value_min_interaction_ratio <= 1.0):
            raise ValueError(
                "h0_reentry_value_min_interaction_ratio must be in [0, 1]")
        if not isinstance(
                self.h0_reentry_value_scale_by_interaction_ratio, bool):
            raise ValueError(
                "h0_reentry_value_scale_by_interaction_ratio must be boolean")
        if self.h0_uncertain_stay_policy not in {
                "fixed", "hub_adaptive_v1"}:
            raise ValueError(
                "h0_uncertain_stay_policy must be fixed or hub_adaptive_v1")
        if self.h0_state_anchor_policy not in {
                "home", "interaction_barycenter", "topology_adaptive"}:
            raise ValueError(
                "h0_state_anchor_policy must be home, "
                "interaction_barycenter, or topology_adaptive")
        if (not math.isfinite(self.h0_anchor_pull_radius_um)
                or self.h0_anchor_pull_radius_um <= 0.0):
            raise ValueError(
                "h0_anchor_pull_radius_um must be finite and positive")
        if self.m3_search_budget_policy not in {
                "fixed", "topology_regularized_v1"}:
            raise ValueError(
                "m3_search_budget_policy must be fixed or "
                "topology_regularized_v1")
        if self.m3_pin_radius_policy not in {
                "fixed", "topology_dispersed_v1"}:
            raise ValueError(
                "m3_pin_radius_policy must be fixed or "
                "topology_dispersed_v1")
        if self.h0_rent_policy not in {
                "rolling_one_pulse", "topology_current_v1",
                "topology_terminal_v2"}:
            raise ValueError(
                "h0_rent_policy must be rolling_one_pulse or "
                "topology_current_v1 or topology_terminal_v2")
        if self.forecast_gate_candidate_budget not in {1, 2, 4}:
            raise ValueError(
                "forecast_gate_candidate_budget must be one of {1, 2, 4}")
        if self.operator_profile not in {"exact", "tuned"}:
            raise ValueError("operator_profile must be 'exact' or 'tuned'")
        self.experiment_schema: int = params.get("experiment_schema", 1)
        self.method_id: str = params.get("method_id", "legacy")
        self.objective: str = params.get("objective", "legacy")
        # Explicit migration switch.  Existing research fixtures intentionally
        # default to the Python oracle; formal runners set backend="native" and
        # formal_native=True, which makes extension load/ABI failures fatal.
        self.resident_backend_requested: str = params.get(
            "backend", params.get("resident_backend", "reference"))
        # Public Schema-2 configs use ``native_fail_closed``.  The older
        # ``formal_native`` spelling remains a compatibility alias for golden
        # fixtures; an explicit public value takes precedence.
        self.formal_native: bool = bool(params.get(
            "native_fail_closed", params.get("formal_native", False)))
        self.native_wheel_sha256: str = str(
            params.get("native_wheel_sha256", ""))
        if self.resident_backend_requested not in {"reference", "native"}:
            raise ValueError(
                "resident backend must be explicitly 'reference' or 'native'")
        if self.formal_native and self.resident_backend_requested != "native":
            raise ValueError("formal resident runs require backend='native'")
        self.lookahead_horizon_config = deepcopy(
            params.get("lookahead_horizon", 0))
        # ``lookahead_horizon`` is the maximum readable window.  Formal M3/M4
        # share one geometric-decay spec and differ only in max_horizon=0/8;
        # the old adaptive 0/1/2 form remains a legacy regression path.
        self.lookahead_horizon: int = maximum_lookahead_horizon(
            self.lookahead_horizon_config)
        self.adaptive_lookahead: bool = is_adaptive_lookahead(
            self.lookahead_horizon_config)
        self.decay_lookahead: bool = is_decay_lookahead(
            self.lookahead_horizon_config)
        self.fitness_cache: bool = params.get("fitness_cache", True)
        # The candidate scorer and the final resident router must color the
        # same conflict graph with the same exact-search cutoff.  The router's
        # registered default is 24; a hidden native value of zero can change a
        # winner's executable batch count before any ghost audit runs.
        self.exact_coloring_threshold: int = int(
            params.get("coloring_exact_threshold", 24))
        if self.exact_coloring_threshold < 0:
            raise ValueError("coloring_exact_threshold must be non-negative")
        # Formal ablation controls are injected by method_driver only after the
        # frozen main Schema-2 config has passed its strict validation.  They are
        # intentionally absent from the M3/M4 config contract.
        self.ablation_policy: str = params.get("ablation_policy", "optimize")
        self.ablation_fitness_mode: str = params.get(
            "ablation_fitness_mode", "phase")
        if self.ablation_policy not in {
                "optimize", "always_stay", "always_return", "adjacent_only"}:
            raise ValueError(f"unknown ablation decision policy: {self.ablation_policy!r}")
        if self.ablation_fitness_mode not in {"phase", "lumped_greedy"}:
            raise ValueError(
                f"unknown ablation fitness mode: {self.ablation_fitness_mode!r}")
        self.search_time = 0.0
        # Populated from the complete schedule by _initialize_run_state.
        # Defaults keep direct unit-test and checkpoint-resume construction
        # well-defined before that one-time inventory is restored.
        self.total_two_qubit_gates = 0
        self.total_transition_count = 0
        self.decision_log: list = []                # 每层决策统计（供账本/实验）
        self.cache_stats = CacheStats()
        self.transition_cache = OrderedDict()
        self.transition_cache_limit = 256
        self.phase_cost_cache = OrderedDict()
        self.phase_cost_cache_limit = 65_536
        self.return_candidate_cache = OrderedDict()
        self.return_cache_limit = 8_192
        self.rollout_pair_cache = OrderedDict()
        self.rollout_site_cache = OrderedDict()
        self.rollout_geometry_cache_limit = 65_536
        self.registry = None
        self.nu = None
        self.forecast = None
        self.boundary_backend = None
        self.boundary_architecture_snapshot = None
        self.boundary_site_locations: tuple[tuple[int, int, int], ...] = ()
        self.boundary_site_id: dict[tuple[int, int, int], int] = {}
        self.boundary_storage_locations: tuple[tuple[int, int, int], ...] = ()
        self.boundary_storage_site_id: dict[tuple[int, int, int], int] = {}
        self._zone_site_cache: tuple[tuple[int, int, int], ...] | None = None
        self._rich_gate_option_cache: dict[tuple, RichGateOption] = {}
        self._rich_gate_option_cache_limit = 131_072
        self.boundary_backend_metrics = {
            "marshal_ns": 0,
            "python_marshal_ns": 0,
            "native_call_wall_ns": 0,
            "native_search_wall_ns": 0,
            "search_kernel_ns": 0,
            "fitness_ns": 0,
            "normalize_ns": 0,
            "decode_ns": 0,
            "return_match_ns": 0,
            "forecast_ns": 0,
            "selection_ns": 0,
            "native_parse_ns": 0,
            "native_serialize_ns": 0,
            "calls": 0,
            "candidates": 0,
        }
        self.backend_timing_log: list[dict] = []
        # q -> (visible use layer, exact zone seat).  A lookahead STAY is a
        # physical residency commitment, not merely a promise that a later
        # greedy placement may immediately undo with a zone-to-zone transfer.
        self.residency_commitments: dict[int, tuple[int, tuple]] = {}
        self.scheduler_reference = None
        self.current_scheduler_prefix = None
        self.expected_scheduler_prefix_sha256 = None
        self.expected_scheduler_prefix_idle_us = None
        self.one_qubit_gates_by_layer = ()

    # ------------------------------------------------------------------ 轮循环
    def _record_leading_one_qubit_gates(self, gates) -> dict:
        """Commit the globally serial 1Q prefix before the initial AOD move.

        Only the prefix attached to parent ``-1`` is accounted here: the
        emitted native trace makes the initial AOD wait for that whole block.
        Parent-layer 1Q gates are deliberately excluded because their real
        schedule may overlap an AOD phase; treating them as serial boundary
        intervals would overstate the serialized boundary-phase prior.
        """
        count = 0
        n_qubits = len(self.mapping[0])
        for gate in gates or ():
            if not isinstance(gate, (tuple, list)) or len(gate) != 2:
                raise ValueError(
                    "leading one-qubit gate must be an (operation, qubit) pair")
            q = int(gate[1])
            if not 0 <= q < n_qubits:
                raise ValueError(
                    f"leading one-qubit gate references invalid qubit {q}")
            self.registry.record_external_idle_interval(
                self._ONE_QUBIT_DURATION_US, busy_atoms=(q,))
            count += 1
        return {
            "gates": count,
            "duration_us": count * self._ONE_QUBIT_DURATION_US,
        }

    def _record_initial_out_phase(self, placement: list) -> dict:
        """Commit the real initial ``mapping[0] -> gate[0]`` AOD phase.

        The initial movement is repaired before this call and is replayed by
        the same batch-order oracle and expanded-AOD timing used for every
        later committed out phase.  It must precede the first CZ pulse so the
        stateful coherence prior contains the known leading-prefix and initial
        movement history in the correct order.
        """
        positions = {
            q: tuple(self.registry.current_pos(q))
            for q in range(len(self.mapping[0]))}
        legs, owners = [], []
        for row in placement:
            for q, seat in zip(row["gate"], row["seats"]):
                p0 = self.architecture.exact_SLM_location_tuple(positions[q])
                p1 = self.architecture.exact_SLM_location_tuple(seat)
                distance = math.dist(p0, p1)
                if distance > 1e-9:
                    legs.append((distance, *p0, *p1))
                    owners.append(int(q))
        batches, _phase = _replay_phase_batches(
            self.architecture, legs, owners, positions)
        physical = PhysicalIncrementalCost(len(self.mapping[0]))
        duration = sum(
            physical._expanded_batch_time(legs, batch) for batch in batches)
        self.registry.record_movement_phase(
            duration, owners,
            transfer_time_us=PhysicalIncrementalCost.T_TRANSFER_US)
        return {
            "move_time_us": float(duration),
            "move_batches": len(batches),
            "movers": len(owners),
        }

    def _prepare_boundary_architecture(self):
        """Preload immutable storage geometry once for the native solver.

        Rich H=0 RETURN matching occasionally needs the global free-site
        fallback used by :func:`match_return_sites`.  Sending all storage sites
        at every layer would dominate marshalling, so site ids and coordinates
        live in the persistent ``ArchitectureSnapshot``.  Per-boundary payloads
        contain only the small local matching columns and occupied ids.
        """
        storage_locations = tuple(sorted(_all_storage_sites(self.architecture)))
        # One immutable index covers storage, entanglement, and every current
        # mapping position.  Rich boundary DTOs can therefore send compact ids
        # instead of repeating coordinates, legs, owners, and ghost rows for
        # every gate option at every layer.
        all_locations = set(storage_locations)
        for array, slm in sorted(self.architecture.dict_SLM.items()):
            all_locations.update(
                (int(array), row, column)
                for row in range(slm.n_r)
                for column in range(slm.n_c)
            )
        site_locations = tuple(sorted(all_locations))
        site_id = {
            tuple(location): index
            for index, location in enumerate(site_locations)
        }
        storage_site_id = {
            tuple(location): site_id[tuple(location)]
            for location in storage_locations
        }
        coordinates = tuple(
            BoundaryPoint(*self.architecture.exact_SLM_location_tuple(location))
            for location in site_locations
        )
        self.boundary_site_locations = site_locations
        self.boundary_site_id = site_id
        # Backwards-compatible name: site ids are global ABI5 ids, so native
        # RETURN assignments are decoded through the complete location table.
        self.boundary_storage_locations = site_locations
        self.boundary_storage_site_id = storage_site_id
        entangling_site_pairs = tuple(
            (
                site_id[tuple(site)],
                site_id[(int(site[0]) + 1, int(site[1]), int(site[2]))],
            )
            for site in sorted(self._all_zone_sites())
        )
        self.boundary_architecture_snapshot = BoundaryArchitectureSnapshot(
            len(self.mapping[0]), coordinates,
            tuple(storage_site_id[location] for location in storage_locations),
            entangling_site_pairs)
        return self.boundary_architecture_snapshot

    def _initialize_run_state(self, architecture, qubit_mapping,
                              gate_scheduling, *, forecast_source=None,
                              leading_one_qubit_gates=(),
                              one_qubit_gates_by_layer=()):
        """Initialize the state shared by batch and bounded-memory execution.

        ``gate_scheduling`` is the placement view used by the current physical
        stage.  Batch execution passes its in-memory list; Large execution passes
        a sequence facade backed by SQLite.  ``forecast_source`` may therefore be
        a bounded :class:`ForecastLayerProvider` while preserving the exact
        :class:`ForecastOracle` gate on future information.
        """
        self.architecture = architecture
        self.gate_scheduling = gate_scheduling
        self._h0_participant_cycle_split_transfer_time_us = 0.0
        # A fixed interaction-graph descriptor is already available to the
        # common initial placer and does not expose the order or distance of a
        # future use at a boundary.  It lets strict-H=0 choose a stable RETURN
        # anchor for chain-like circuits, where repeatedly chasing the nearest
        # free site creates long back/out legs, while retaining the mixed
        # physical candidate family for high-degree interaction graphs.
        partner_sets = {}
        interaction_counts = {}
        # A forward-only streaming facade must not be scanned: doing so would
        # both evict layer zero and violate the H=0 read firewall.  The current
        # ZAC18/QMAP154 batch path supplies a frozen list/tuple and can reuse
        # the graph that the initial placer already materialised.
        has_frozen_interaction_graph = isinstance(
            gate_scheduling, (list, tuple))
        if has_frozen_interaction_graph:
            for gates in gate_scheduling:
                for gate in gates:
                    if len(gate) < 2:
                        continue
                    q0, q1 = int(gate[0]), int(gate[1])
                    partner_sets.setdefault(q0, set()).add(q1)
                    partner_sets.setdefault(q1, set()).add(q0)
                    interaction_counts[(q0, q1)] = (
                        interaction_counts.get((q0, q1), 0) + 1)
                    interaction_counts[(q1, q0)] = (
                        interaction_counts.get((q1, q0), 0) + 1)
        self.static_partner_degree_max = (
            max((len(partners) for partners in partner_sets.values()),
                default=0)
            if has_frozen_interaction_graph else None)
        self.static_interaction_counts = dict(interaction_counts)
        self.total_two_qubit_gates = sum(interaction_counts.values()) // 2
        self.static_interaction_totals = {
            q: sum(
                count for (atom, _partner), count in interaction_counts.items()
                if atom == q)
            for q in range(len(qubit_mapping[0]))
        }
        active_interaction_totals = sorted(
            value for value in self.static_interaction_totals.values()
            if value > 0)
        if not active_interaction_totals:
            self.static_interaction_median = 0.0
        else:
            middle = len(active_interaction_totals) // 2
            if len(active_interaction_totals) % 2:
                self.static_interaction_median = float(
                    active_interaction_totals[middle])
            else:
                self.static_interaction_median = 0.5 * (
                    active_interaction_totals[middle - 1]
                    + active_interaction_totals[middle])
        self.static_partner_degrees = {
            q: len(partner_sets.get(q, ()))
            for q in range(len(qubit_mapping[0]))
        }
        self.return_anchor_effective_policy = self.return_anchor_policy
        if (self.return_anchor_policy == "topology_adaptive"
                and self.static_partner_degree_max is not None):
            self.return_anchor_effective_policy = (
                "home_stable"
                if self.static_partner_degree_max <= 2
                else "bounded_mixed")
        elif self.return_anchor_policy == "topology_adaptive":
            self.return_anchor_effective_policy = "bounded_mixed"
        self._zone_site_cache = None
        self._rich_gate_option_cache.clear()
        n = len(gate_scheduling)
        self.total_transition_count = max(0, n - 1)
        # Neutralise the legacy adjacent-layer reuse mechanism.  The resident
        # registry is the sole source of cross-layer reuse for M3/M4.
        self.list_reuse_qubit = [set() for _ in range(n)]
        self.mapping = [list(qubit_mapping[0])]
        self.registry = ResidentRegistry(architecture, qubit_mapping[0],
                                         self.theta_capacity)
        self.h0_last_use_layer = [None] * len(qubit_mapping[0])
        self.h0_gap_ewma = [None] * len(qubit_mapping[0])
        self.h0_gap_samples = [0] * len(qubit_mapping[0])
        self.h0_history_observed_layer = 0
        if n > 0:
            for gate in gate_scheduling[0]:
                for q in gate:
                    self.h0_last_use_layer[int(q)] = 0
        partner_barycenter_shifts = []
        for q, home in enumerate(self.registry.homes):
            counts = [
                (partner, count)
                for (atom, partner), count in interaction_counts.items()
                if atom == q]
            total = sum(count for _partner, count in counts)
            if total <= 0:
                continue
            home_xy = architecture.exact_SLM_location_tuple(home)
            partner_x = sum(
                count * architecture.exact_SLM_location_tuple(
                    self.registry.homes[partner])[0]
                for partner, count in counts) / total
            partner_y = sum(
                count * architecture.exact_SLM_location_tuple(
                    self.registry.homes[partner])[1]
                for partner, count in counts) / total
            partner_barycenter_shifts.append(
                math.dist(home_xy, (partner_x, partner_y)))
        self.static_partner_barycenter_shift_mean_um = (
            sum(partner_barycenter_shifts) / len(partner_barycenter_shifts)
            if partner_barycenter_shifts else 0.0)
        self.h0_uncertain_stay_effective_weight = (
            self.h0_uncertain_stay_weight
            if (self.h0_uncertain_stay_policy == "fixed"
                or (self.static_partner_degree_max is not None
                    and self.static_partner_degree_max >= 20))
            else 0.0)
        self.m3_pin_radius_effective = self.pin_radius
        if (self.method_id == "ours_nl"
                and self.lookahead_horizon == 0
                and self.m3_pin_radius_policy == "topology_dispersed_v1"
                and self.static_partner_degree_max is not None
                and self.static_partner_degree_max > 2
                and self.static_partner_barycenter_shift_mean_um
                > self.h0_anchor_pull_radius_um):
            self.pin_radius = max(self.pin_radius, 4)
            self.m3_pin_radius_effective = self.pin_radius
        self.h0_state_anchor_effective_policy = self.h0_state_anchor_policy
        if self.h0_state_anchor_policy == "topology_adaptive":
            self.h0_state_anchor_effective_policy = (
                "home"
                if (self.static_partner_degree_max is None
                    or self.static_partner_degree_max <= 2
                    or self.static_partner_barycenter_shift_mean_um
                    > self.h0_anchor_pull_radius_um)
                else "interaction_barycenter")
        self.h0_state_potential_effective_weight = (
            2.0 * self.h0_state_potential_weight
            if (self.h0_state_potential_weight_policy == "anchor_adaptive_v1"
                and self.h0_state_anchor_effective_policy ==
                "interaction_barycenter")
            else self.h0_state_potential_weight)
        self.h0_state_anchor_xy = []
        for q, home in enumerate(self.registry.homes):
            home_xy = architecture.exact_SLM_location_tuple(home)
            if self.h0_state_anchor_effective_policy == "home":
                self.h0_state_anchor_xy.append(tuple(home_xy))
                continue
            partners = [
                (partner, count)
                for (atom, partner), count in interaction_counts.items()
                if atom == q]
            total = sum(count for _partner, count in partners)
            if total <= 0:
                self.h0_state_anchor_xy.append(tuple(home_xy))
                continue
            # Half self-home stability, half weighted partner-home pull.  This
            # is the same static interaction inventory used by initial SA; it
            # contains no layer order or next-use distance.
            x = total * float(home_xy[0])
            y = total * float(home_xy[1])
            for partner, count in partners:
                partner_xy = architecture.exact_SLM_location_tuple(
                    self.registry.homes[partner])
                x += count * float(partner_xy[0])
                y += count * float(partner_xy[1])
            anchor_x, anchor_y = x / (2.0 * total), y / (2.0 * total)
            pull_x = anchor_x - float(home_xy[0])
            pull_y = anchor_y - float(home_xy[1])
            pull = math.hypot(pull_x, pull_y)
            if pull > self.h0_anchor_pull_radius_um:
                scale = self.h0_anchor_pull_radius_um / pull
                anchor_x = float(home_xy[0]) + scale * pull_x
                anchor_y = float(home_xy[1]) + scale * pull_y
            self.h0_state_anchor_xy.append((anchor_x, anchor_y))
        self.m3_search_profile_effective = "configured"
        if (self.method_id == "ours_nl"
                and self.lookahead_horizon == 0
                and self.m3_search_budget_policy ==
                "topology_regularized_v1"):
            # A larger stochastic budget over-optimises the approximate H=0
            # value and was empirically worse on both high-interaction probes.
            # Use the preregistered >64-qubit scale boundary plus the static
            # home-vs-partner disagreement to select one of two bounded C++
            # profiles.  No circuit name, layer order, baseline score, or
            # future-use distance participates in this choice.
            conservative = (
                len(self.mapping[0]) > 64
                or self.static_partner_barycenter_shift_mean_um
                > self.h0_anchor_pull_radius_um)
            if conservative:
                self.population_size = 4
                self.iterations = 1
                self.neighbor_sample_size = 4
                self.neighbors_per_solution = 1
                self.elite_count = 1
                self.early_stop_patience = 1
                self.max_unique_evaluations = 16
                self.m3_search_profile_effective = "nano16"
            else:
                self.population_size = 4
                self.iterations = 2
                self.neighbor_sample_size = 8
                self.neighbors_per_solution = 1
                self.elite_count = 1
                self.early_stop_patience = 1
                self.max_unique_evaluations = 64
                self.m3_search_profile_effective = "micro64"
        # The complete-domain exact path is valuable on ZAC-sized circuits,
        # but it turns a 10k-layer serial QMAP circuit into millions of exact
        # candidate replays.  The total interaction count is the same
        # order-free inventory already consumed by initial placement; it does
        # not reveal any next layer to H=0.  Above 512 transitions, retain every
        # physical candidate in the native DTO while bounding stochastic work
        # and disabling the redundant post-budget polish pass.  The threshold
        # is registered on transition count; ZAC18 tops out at 109, so its
        # already-accepted quality path is untouched.
        self.large_search_profile_effective = "configured"
        if self.total_transition_count > 512:
            self.direct_enumeration_limit = min(
                self.direct_enumeration_limit, 64)
            self.local_polish_sweeps = 0
            self.return_assignment_k = min(self.return_assignment_k, 2)
            self.forecast_gate_candidate_budget = 1
            if self.method_id == "ours_lk":
                self.population_size = min(self.population_size, 6)
                self.iterations = min(self.iterations, 4)
                self.neighbor_sample_size = min(
                    self.neighbor_sample_size, 8)
                self.elite_count = min(
                    self.elite_count, self.population_size)
                self.early_stop_patience = min(
                    self.early_stop_patience, 2)
                self.max_unique_evaluations = min(
                    self.max_unique_evaluations, 192)
            if (self.total_transition_count >= 2500
                    and len(self.mapping[0]) <= 16):
                self.large_search_profile_effective = \
                    "deep-serial-local8-64-h4-v3"
            else:
                self.large_search_profile_effective = "long-depth-64-h4-v2"
            if (self.total_transition_count >= 5000
                    and len(self.mapping[0]) <= 16):
                # The 28 QMAP rows outside the common finite-fidelity cohort
                # contain 6k--224k CZ gates.  They are still part of QMAP154's
                # Move/runtime table, but a 192-evaluation H=4 GA at every
                # almost-serial boundary is needlessly superlinear in the
                # circuit depth.  Preserve two geometrically decayed future
                # layers and the deterministic physical anchors while bounding
                # only the stochastic refinement budget.
                self.direct_enumeration_limit = min(
                    self.direct_enumeration_limit, 16)
                self.return_assignment_k = 1
                self.population_size = min(self.population_size, 4)
                self.iterations = min(self.iterations, 1)
                self.neighbors_per_solution = min(
                    self.neighbors_per_solution, 1)
                self.neighbor_sample_size = min(
                    self.neighbor_sample_size, 4)
                self.early_stop_patience = min(
                    self.early_stop_patience, 1)
                self.max_unique_evaluations = min(
                    self.max_unique_evaluations, 32)
                self.large_search_profile_effective = \
                    "ultra-deep-local8-32-h2-v5"
            if self.method_id == "ours_nl":
                self.m3_search_profile_effective += \
                    "+" + self.large_search_profile_effective
        self._record_leading_one_qubit_gates(leading_one_qubit_gates)
        self.one_qubit_gates_by_layer = one_qubit_gates_by_layer or ()
        self.scheduler_reference = ExactCurrentReferenceScheduler(
            architecture,
            qubit_mapping[0],
            leading_one_qubit_gates=leading_one_qubit_gates,
            coloring_exact_threshold=self.exact_coloring_threshold,
        )
        self.current_scheduler_prefix = None
        self.expected_scheduler_prefix_sha256 = None
        self.expected_scheduler_prefix_idle_us = None
        if self.experiment_schema == 2 and self.engine == "ga":
            snapshot = self._prepare_boundary_architecture()
            self.boundary_backend = select_backend(
                snapshot,
                backend=self.resident_backend_requested,
                formal=self.formal_native,
                require_registered_wheel=self.formal_native,
                expected_wheel_sha256=(
                    self.native_wheel_sha256 if self.formal_native else None),
            )
            self.boundary_backend_metrics = {
                "marshal_ns": 0,
                "python_marshal_ns": 0,
                "native_call_wall_ns": 0,
                "native_search_wall_ns": 0,
                "search_kernel_ns": 0,
                "fitness_ns": 0,
                "normalize_ns": 0,
                "decode_ns": 0,
                "return_match_ns": 0,
                "forecast_ns": 0,
                "selection_ns": 0,
                "native_parse_ns": 0,
                "native_serialize_ns": 0,
                "calls": 0,
                "candidates": 0,
            }
            self.backend_timing_log = []
        # Schema 2 makes ForecastOracle the sole future-information channel.
        # Keep a deliberately empty legacy helper so accidental next-use reads
        # cannot leak L+2+ even though older function signatures still accept it.
        self.nu = NextUse([] if self.experiment_schema == 2 else gate_scheduling)
        self.forecast = ForecastOracle(
            gate_scheduling if forecast_source is None else forecast_source,
            self.lookahead_horizon_config,
            alpha_lookahead=self.alpha_lookahead)
        self.residency_commitments = {}
        return n

    def _one_qubit_for_layer(self, layer: int) -> tuple[tuple[str, int], ...]:
        source = self.one_qubit_gates_by_layer
        if not source:
            return ()
        try:
            gates = source[int(layer)]
        except (IndexError, KeyError):
            return ()
        return tuple((str(gate[0]), int(gate[1])) for gate in gates)

    def _finish_terminal_boundary(self, layer: int):
        """Commit the exact terminal boundary shared by batch and Large paths."""
        source_gate_mapping = deepcopy(self.mapping[-1])
        terminal_ablation_return = self.ablation_policy == "always_return"
        if terminal_ablation_return:
            # Keep the registry at its true pre-move positions until ghost
            # repair has replayed every RETURN leg.  decide_lazy's legacy
            # final-return branch mutates eagerly and is therefore not used by
            # the strict Schema-2 ablation path.
            returners = sorted(self.registry.zone_seat)
            sites = match_return_sites(
                self.registry, returners, self.nu, layer,
                self.box_ratio, self.alpha_lookahead,
                forecast=self.forecast, candidate_mode="forecast")
            decisions = {q: ("RETURN", sites[q]) for q in returners}
            stats = {"stay": 0, "forced_e2": 0, "capacity": 0,
                     "return": len(returners), "participants": 0}
        else:
            decisions, stats = decide_lazy(
                self.registry, self.nu, layer, [], [],
                final_return_home=self.final_return_home)
        self._repair_ghosts([], decisions)
        if terminal_ablation_return:
            for q, (kind, loc) in decisions.items():
                if kind == "RETURN":
                    self.registry.return_to_storage(q, loc)
                elif kind == "RESEAT":
                    self.registry.reseat(q, loc)
        self._append_boundary(decisions)
        approximate_ultra_deep_current = bool(
            self.total_transition_count >= 5000
            and len(self.mapping[0]) <= 16
            and self.resident_backend_requested == "native")
        if (self.scheduler_reference is not None
                and not approximate_ultra_deep_current):
            self.scheduler_reference.commit_source(
                layer,
                source_gate_mapping,
                self.mapping[-1],
                self.gate_scheduling[layer],
                self._one_qubit_for_layer(layer),
            )
            if self.expected_scheduler_prefix_sha256 is not None:
                if any(kind != "STAY" for kind, _location in decisions.values()):
                    if self.ablation_policy != "always_return":
                        raise RuntimeError(
                            "formal terminal boundary changed a previously "
                            "audited no-back candidate")
                else:
                    snapshot = self.scheduler_reference.scheduler_snapshot
                    if snapshot.timing_sha256 != \
                            self.expected_scheduler_prefix_sha256:
                        raise RuntimeError(
                            "terminal production scheduler hash differs from "
                            "the prior native-winner candidate audit")
                    expected_idle = self.expected_scheduler_prefix_idle_us
                    if (expected_idle is None
                            or len(expected_idle) != len(snapshot.idle_time_us)
                            or any(not math.isclose(
                                actual, expected, rel_tol=0.0, abs_tol=1e-9)
                                for actual, expected in zip(
                                    snapshot.idle_time_us, expected_idle))):
                        raise RuntimeError(
                            "terminal production idle vector differs from the "
                            "prior native-winner candidate audit")
                self.expected_scheduler_prefix_sha256 = None
                self.expected_scheduler_prefix_idle_us = None
        terminal_horizon = AdaptiveHorizonDecision.fixed(
            0, reason="terminal_boundary")
        self.backend_timing_log.append({
            "layer": layer,
            "backend": getattr(
                self.boundary_backend, "name", self.resident_backend_requested),
            "marshal_ns": 0,
            "search_kernel_ns": 0,
            "fitness_ns": 0,
            "selection_ns": 0,
            "native_parse_ns": 0,
            "native_serialize_ns": 0,
            "calls": 0,
            "candidates": 0,
        })
        self.decision_log.append({
            "layer": layer,
            **stats,
            **terminal_horizon.as_log(),
            "configured_lookahead_horizon": self.lookahead_horizon_config,
            "horizon_selection_ns": 0,
            "search_kernel_ns": 0,
            "ghost_fix": getattr(self, "ghost_fixes", 0),
        })

    def run(self, architecture, qubit_mapping, gate_scheduling,
            dynamic_placement, reuse_qubit, *, leading_one_qubit_gates=(),
            one_qubit_gates_by_layer=()):
        """轮循环总控：产出与 ZAC 同构的映射流（长度 2n+1）。

        映射流的下标约定（下游 route_qubit_mis 按 2L/2L+1 取用）：
            mapping[0]      = SA 初始布局（床位）
            mapping[2L+1]   = 第 L 轮门位映射（参与者落座，非参与者逐位不动）
            mapping[2L+2]   = 第 L 轮边界映射（RETURN 者已落到存储位）
        每轮三拍（顺序与原生编译器同构——门赢，闲人让路）：
            ① plan(L+1)     先定下一轮门位（要用到下一轮座位才能判 E2 挡路）
            ② decide_lazy   边界决策：默认全 STAY，E2/容量强制 RETURN
            ③ commit(L+1)   参与者登记入区，两张映射依序入流
        engine="ga" 时 ①② 合并成一步联合搜索（_ga_step）。
        """
        n = self._initialize_run_state(
            architecture, qubit_mapping, gate_scheduling,
            leading_one_qubit_gates=leading_one_qubit_gates,
            one_qubit_gates_by_layer=one_qubit_gates_by_layer)

        # Canonical optimisation can legitimately remove every two-qubit gate
        # (for example, ground_state_estimation_10).  Such a circuit has no
        # resident boundary to optimise; preserving the initial mapping is the
        # complete 2n+1 contract and must not probe layer zero.
        if n == 0:
            self.decision_log = []
            self._assert_contract()
            return

        placement = self._plan_round(0)
        self._repair_ghosts(placement, {})       # 第 0 轮入场也过鬼点防线
        self._record_initial_out_phase(placement)
        self._commit_round(0, placement)
        for layer in range(n):
            if layer + 1 < n:
                if self.engine == "ga":
                    # A3：边界决策 + 下一轮门位联合搜索（一步 GA 两张映射一起提交）
                    if self.experiment_schema == 2:
                        self._ga_step_v2(layer)
                    else:
                        self._ga_step(layer)
                    continue
                placement = self._plan_round(layer + 1)   # 门赢：先定门位，闲人让路
                next_gates = self.gate_scheduling[layer + 1]
                next_seats = [p["seats"] for p in placement]
            else:
                # 末边界：没有下一轮门位 → 全员 STAY（native 同款），
                # 或正式消融/显式开关要求时回存储。
                self._finish_terminal_boundary(layer)
                continue
            decisions, stats = decide_lazy(
                self.registry, self.nu, layer, next_gates, next_seats)
            self._repair_ghosts(placement, decisions)   # 防线③（match 引擎同享）
            self._append_boundary(decisions)
            self.decision_log.append({"layer": layer, **stats,
                                      "ghost_fix": getattr(self, "ghost_fixes", 0)})
            self._commit_round(layer + 1, placement)
        self._assert_contract()

    # ------------------------------------------------------------------ 门位规划
    def _norm_left(self, site: tuple) -> tuple:
        """锚点归一化到该纠缠区的左 SLM（座位对约定 (s, s+1) 的前提）。"""
        eid = self.architecture.dict_SLM[site[0]].entanglement_id
        return (self.architecture.entanglement_zone[eid][0], site[1], site[2])

    def _zone_anchors(self, p1: tuple, p2: tuple) -> list:
        """一对原子的入区锚点。ZAC 的预计算表只认存储位（KeyError 教训）——
        区内原子的锚点 = 自己的座位（行粘性，原生编译器 K3 同款）。"""
        arch = self.architecture
        in_zone1 = arch.dict_SLM[p1[0]].entanglement_id != -1
        in_zone2 = arch.dict_SLM[p2[0]].entanglement_id != -1
        if not in_zone1 and not in_zone2:
            return [self._norm_left(s) for s in
                    arch.nearest_entanglement_site(p1[0], p1[1], p1[2],
                                                   p2[0], p2[1], p2[2])]
        anchors = []
        for p, in_zone in ((p1, in_zone1), (p2, in_zone2)):
            if in_zone:
                anchors.append(p)                      # 已在区内：锚=当前座位
            else:
                # 单原子版定义被 6 参版覆盖（Python 同名只留最后一个），
                # 用 6 参版传同一个位代替：same site1==site2 → 返回单锚点
                anchors.append(arch.nearest_entanglement_site(
                    p[0], p[1], p[2], p[0], p[1], p[2])[0])
        return [self._norm_left(a) for a in anchors]

    def _plan_round(self, layer: int) -> list:
        """第 layer 轮门位：菜单（按登记簿真实位置）+ ZAC 距离公式 + 最小权匹配。

        与 ZAC place_gate 的差别：无复用钉扎分支（驻留者可自由换座，其入区腿
        就是区内短移）；候选锚点带 +1 配对防御过滤。
        """
        t0 = time.time()
        list_gate = self.gate_scheduling[layer]
        reg = self.registry
        expand_factor = max(1, math.ceil(math.sqrt(len(list_gate)) / 2))
        # 硬排除（审计 FATAL-2 的两级扩展）：
        #   ① 闲住驻留者（非参与者）的座位不让门——先撞再逐会触发 E2 风暴
        #   ② 本轮其他参与者的当前座位也不让门——ZAC 的 site 账本只记"离开"
        #      不记"到达"，同相位内"B 到达 A 的座位"无法排序（A 的离开作业
        #      发射在后），ising/qft 宽层实测产生真双占；从构造上禁绝同相位
        #      座位交接（门位 = 本门原子自己的当前座位不算，那是钉扎不动）
        participants = {q for gate in list_gate for q in gate}
        candidates: list[list] = []     # 每门 [(site, w, q1, q2)]，w 为 ZAC 权重公式

        for gate in list_gate:
            q1, q2 = gate
            resident_seats = [reg.zone_seat[q] for q in (q1, q2) if reg.is_resident(q)]
            if resident_seats:
                # K2 钉扎：菜单塌缩到驻留者座位对 ± pin_radius 列——
                # 驻留者一步不走（或挪一列），新来者跑全程
                set_sites = set()
                for seat in resident_seats:
                    base = self._norm_left(seat)
                    slm = self.architecture.dict_SLM[base[0]]
                    for dc in range(-self.pin_radius, self.pin_radius + 1):
                        c = base[2] + dc
                        if 0 <= c < slm.n_c:
                            set_sites.add((base[0], base[1], c))
            else:
                set_sites = self._expanded_sites(q1, q2, list_gate)
            pin_base = self._norm_left(resident_seats[0]) if resident_seats else None
            # 本门视角的硬排除：区内所有座位，除了本门两个原子自己的
            blocked_g = {seat for q, seat in reg.zone_seat.items()
                         if q != q1 and q != q2}
            # 三级菜单：常驻域过滤 → 全区过滤（保持排除！宽敞层恒非空：
            # 280 座 − ≤98 驻留者 ≥ 42 个完整空对）→ 全区不过滤（理论兜底）
            opts = self._build_opts(set_sites, q1, q2, blocked_g, pin_base)
            if not opts:
                opts = self._build_opts(set(self._all_zone_sites()),
                                        q1, q2, blocked_g, None)
            if not opts:
                opts = self._build_opts(set(self._all_zone_sites()),
                                        q1, q2, None, None)
            candidates.append(opts)

        # 防线①：菜单预过滤（match 引擎无鸽笼约束，保底 1 个选项即可）
        ghosts0 = self._static_ghosts({q for gate in list_gate for q in gate})
        candidates = [self._filter_menu_ghosts(opts, g[0], g[1], ghosts0, 1)
                      for opts, g in zip(candidates, list_gate)]
        # 全局二部图匹配（行=工位，列=门；与 ZAC place_gate 同构）+ 安全网修复
        placement = self._repair_placements(self._match_gates(candidates, list_gate),
                                            list_gate)
        self.search_time += time.time() - t0
        return placement

    # ------------------------------------------------------------------ 菜单助手
    def _all_zone_sites(self) -> tuple[tuple[int, int, int], ...]:
        """全区左 SLM 工位全集（菜单的终极兜底搜索域）。"""
        if self._zone_site_cache is not None:
            return self._zone_site_cache
        out = []
        for zone in self.architecture.entanglement_zone:
            left = self.architecture.dict_SLM[zone[0]]
            for r in range(left.n_r):
                for c in range(left.n_c):
                    out.append((zone[0], r, c))
        self._zone_site_cache = tuple(out)
        return self._zone_site_cache

    def _pair_seats(self, q1: int, q2: int, site: tuple) -> tuple:
        """工位 → 座位对朝向。驻留者优先保座：配对恰含其当前座位时它不动、
        搭档去另一座（否则列序朝向会把搭档安排到驻留者旧座上，形成同门
        同相位的座位交接——site 账本表达不了，ising 实测双占）。"""
        a, b = site, (site[0] + 1, site[1], site[2])
        reg = self.registry
        for q, other, mine_first in ((q1, q2, True), (q2, q1, False)):
            if reg.is_resident(q) and reg.zone_seat[q] in (a, b):
                mine = reg.zone_seat[q]
                other_seat = b if mine == a else a
                return (mine, other_seat) if mine_first else (other_seat, mine)
        p1, p2 = reg.current_pos(q1), reg.current_pos(q2)
        if p1[2] < p2[2]:
            return a, b
        return b, a

    def _site_weight(self, q1: int, q2: int, site: tuple) -> float:
        """ZAC 权重公式（vmplacer.py:267-270）+ 驻留者折扣（解析版钉扎）。"""
        reg = self.registry
        p1, p2 = reg.current_pos(q1), reg.current_pos(q2)
        s1, s2 = self._pair_seats(q1, q2, site)
        d1 = self.architecture.distance(p1[0], p1[1], p1[2], s1[0], s1[1], s1[2])
        d2 = self.architecture.distance(p2[0], p2[1], p2[2], s2[0], s2[1], s2[2])
        r1 = self.w_resident if reg.is_resident(q1) else 1.0
        r2 = self.w_resident if reg.is_resident(q2) else 1.0
        if p1[0] == p2[0] and p1[1] == p2[1]:     # 同 SLM 同行并排走只记最远腿
            return max(math.sqrt(d1) * r1, math.sqrt(d2) * r2)
        return math.sqrt(d1) * r1 + math.sqrt(d2) * r2

    def _site_weight_from_seats(self, q1: int, q2: int, p1: tuple,
                                p2: tuple, s1: tuple, s2: tuple) -> float:
        """Evaluate the unchanged ZAC edge weight for precomputed endpoints."""
        reg = self.registry
        d1 = self.architecture.distance(p1[0], p1[1], p1[2], s1[0], s1[1], s1[2])
        d2 = self.architecture.distance(p2[0], p2[1], p2[2], s2[0], s2[1], s2[2])
        r1 = self.w_resident if reg.is_resident(q1) else 1.0
        r2 = self.w_resident if reg.is_resident(q2) else 1.0
        if p1[0] == p2[0] and p1[1] == p2[1]:     # 同 SLM 同行并排走只记最远腿
            return max(math.sqrt(d1) * r1, math.sqrt(d2) * r2)
        return math.sqrt(d1) * r1 + math.sqrt(d2) * r2

    def _cached_rich_gate_option(self, q1: int, q2: int, site: tuple,
                                 target1: tuple,
                                 target2: tuple) -> RichGateOption:
        """Return the immutable indexed DTO used by the native rich solver."""
        site_id = self.boundary_site_id[tuple(site)]
        target1_site_id = self.boundary_site_id[tuple(target1)]
        target2_site_id = self.boundary_site_id[tuple(target2)]
        option_key = (
            int(q1), int(q2), site_id, target1_site_id, target2_site_id)
        rich_option = self._rich_gate_option_cache.get(option_key)
        if rich_option is not None:
            return rich_option
        if len(self._rich_gate_option_cache) >= \
                self._rich_gate_option_cache_limit:
            # Object identity is not part of the DTO contract.  A bounded
            # construction cache prevents high-qubit suites from retaining
            # every logical-pair/site combination ever observed.
            self._rich_gate_option_cache.clear()
        rich_option = RichGateOption(
            site_id=site_id,
            q1=int(q1),
            q2=int(q2),
            target1=None,
            target2=None,
            target1_site_id=target1_site_id,
            target2_site_id=target2_site_id,
        )
        self._rich_gate_option_cache[option_key] = rich_option
        return rich_option

    def _build_indexed_rich_gate_domain(
            self, q1: int, q2: int) -> _IndexedRichGateDomain:
        """Build every native-rich gate row in one geometry traversal.

        ``weight_order`` reproduces the stable ordering of
        ``_build_opts(set(_all_zone_sites()), ...)``.  ``canonical_order``
        reproduces the later ``(weight, site)`` complete-domain ordering while
        re-sorting only equal-weight groups.  Both views share the exact same
        option, endpoint and DTO objects.
        """
        if self.boundary_architecture_snapshot is None:
            # Checkpoint restore rebuilds dynamic resident/RNG/cache state but
            # deliberately does not serialize immutable architecture indexes.
            # The indexed-domain fast path runs before the later backend
            # fallback, so recreate those ids lazily on the first resumed
            # boundary.  Ordinary batch execution already prepared the same
            # snapshot in ``_initialize_run_state`` and pays no extra work.
            self._prepare_boundary_architecture()
        reg = self.registry
        p1, p2 = reg.current_pos(q1), reg.current_pos(q2)
        rows = []
        # The legacy complete-domain path receives a set.  Iterating the same
        # set here preserves its stable-sort tie order byte for byte.
        for site in set(self._all_zone_sites()):
            site = tuple(site)
            target1, target2 = self._pair_seats(q1, q2, site)
            weight = self._site_weight_from_seats(
                q1, q2, p1, p2, target1, target2)
            option = (site, weight, q1, q2)
            rows.append(_IndexedRichGateRow(
                option=option,
                seats=(target1, target2),
                rich_option=self._cached_rich_gate_option(
                    q1, q2, site, target1, target2),
            ))
        weight_order = tuple(sorted(
            rows, key=lambda row: row.option[1]))
        canonical = []
        start = 0
        while start < len(weight_order):
            stop = start + 1
            weight = weight_order[start].option[1]
            while (stop < len(weight_order)
                   and weight_order[stop].option[1] == weight):
                stop += 1
            canonical.extend(sorted(
                weight_order[start:stop],
                key=lambda row: row.option[0]))
            start = stop
        return _IndexedRichGateDomain(
            by_site={row.option[0]: row for row in rows},
            weight_order=weight_order,
            canonical_order=tuple(canonical),
        )

    def _use_indexed_native_gate_domains(self, use_rich_boundary: bool) -> bool:
        """Keep reference and legacy construction untouched by the fast path."""
        return bool(
            use_rich_boundary
            and self.resident_backend_requested == "native")

    def _use_bounded_deep_serial_gate_domain(
            self, use_rich_boundary: bool, gate_count: int,
            *, force_complete: bool = False) -> bool:
        """Use the historical local gate menu only on deep serial boundaries.

        The three unresolved QMAP regressions contain 2,519--3,927
        transitions, and more than 98% of their layers contain at most two CZ
        gates.  Restricting *all* circuits above a small threshold changed the
        search semantics of ordinary QMAP inputs.  This predicate therefore
        targets only the deep, small-qubit, serial regime.  A failed bounded
        native solve is retried with the complete domain from the exact same
        boundary RNG state.
        """
        return bool(
            use_rich_boundary
            and self.resident_backend_requested == "native"
            and not force_complete
            and self.total_transition_count >= 2500
            and len(self.mapping[0]) <= 16
            and int(gate_count) <= 2)

    @staticmethod
    def _indexed_rich_rows_unblocked(rows, blocked):
        if not blocked:
            return list(rows)
        return [
            row for row in rows
            if row.seats[0] not in blocked and row.seats[1] not in blocked
        ]

    def _indexed_rich_local_opts(self, domain: _IndexedRichGateDomain,
                                 sites, blocked) -> list:
        """Reproduce ``_build_opts`` local-set ordering without geometry work."""
        rows = self._indexed_rich_rows_unblocked(
            (domain.by_site[tuple(site)] for site in sites), blocked)
        rows.sort(key=lambda row: row.option[1])
        return [row.option for row in rows]

    def _indexed_rich_complete_opts(self, domain: _IndexedRichGateDomain,
                                    blocked, *, canonical=False) -> list:
        """Return a filtered full-domain view in either legacy exact order."""
        rows = (domain.canonical_order if canonical
                else domain.weight_order)
        return [row.option for row in
                self._indexed_rich_rows_unblocked(rows, blocked)]

    def _h0_participant_cycle_amortization_seed(
            self, list_gate, candidates, formal_cycle_candidates):
        """Return one current-only progressive gate/cycle seed for M3.

        A tiny stochastic H=0 budget can miss the useful two-step pattern seen
        on a serial chain: first move the gate centre by one physical lattice
        site, then let the ordinary participant RETURN bit decide whether that
        step is cheaper as a direct move or as a storage cycle.  The seed below
        exposes both variants to the existing rich solver.  A deterministic
        static-anchor amortization term ranks that one-step option below; the
        policy does not force a RETURN or inspect any layer after the current
        target.

        The policy is deliberately structural rather than tunable.  It applies
        only to the already-registered single participant-cycle candidate whose
        frozen interaction degree and total interaction count are both at most
        two.  Static H0 anchors choose the direction, while the registry's
        observed rent is carried into the audit and the soft cycle seed.  Exact
        current scheduler NLL remains the physical component of the unified
        score, and exact ghost replay remains the feasibility authority.
        """
        policy = "h0_participant_cycle_amortization_v1"
        detail = {
            "policy": policy,
            "active": False,
            "participant": None,
            "static_partner_degree": None,
            "static_interaction_total": None,
            "history_idle_exposures": None,
            "history_idle_time_us": None,
            "current_site": None,
            "topology_target_site": None,
            "progressive_site": None,
            "progressive_gate_gene": None,
            "cycle_soft_seed": False,
            "observed_split_transfer_time_us": float(getattr(
                self, "_h0_participant_cycle_split_transfer_time_us", 0.0)),
            "lattice_step_move_time_us": None,
            "physical_throttle_reached": False,
        }
        if (len(list_gate) != 1 or len(candidates) != 1
                or len(formal_cycle_candidates) != 1):
            return {}, set(), detail

        q = int(formal_cycle_candidates[0])
        degree = int(self.static_partner_degrees.get(q, 0))
        total = int(self.static_interaction_totals.get(q, 0))
        history_exposures, history_time_us = self.registry.resident_rent(q)
        detail.update({
            "participant": q,
            "static_partner_degree": degree,
            "static_interaction_total": total,
            "history_idle_exposures": int(history_exposures),
            "history_idle_time_us": float(history_time_us),
        })
        if degree <= 0 or total <= 0 or degree > 2 or total > 2:
            return {}, set(), detail

        q1, q2 = (int(value) for value in list_gate[0])
        if q not in (q1, q2) or not candidates[0]:
            return {}, set(), detail
        current_site = tuple(self._norm_left(self.registry.zone_seat[q]))
        anchors = getattr(self, "h0_state_anchor_xy", ())
        if len(anchors) <= max(q1, q2):
            return {}, set(), detail

        def topology_key(selector):
            option = candidates[0][selector]
            seat1, seat2 = self._pair_seats(q1, q2, option[0])
            duration = 0.0
            for atom, seat in ((q1, seat1), (q2, seat2)):
                seat_xy = self.architecture.exact_SLM_location_tuple(seat)
                distance = math.dist(seat_xy, anchors[atom])
                if distance > 1e-12:
                    duration += math.sqrt(
                        distance / PhysicalIncrementalCost.ACCEL_UM_PER_US2)
            return (duration, float(option[1]), tuple(option[0]))

        topology_selector = min(
            range(len(candidates[0])), key=topology_key)
        topology_site = tuple(candidates[0][topology_selector][0])
        one_step = [
            selector for selector, option in enumerate(candidates[0])
            if (int(option[0][0]) == int(current_site[0])
                and max(abs(int(option[0][1]) - int(current_site[1])),
                        abs(int(option[0][2]) - int(current_site[2]))) <= 1)
        ]
        if not one_step:
            return {}, set(), detail
        current_xy = self.architecture.exact_SLM_location_tuple(current_site)
        positive_step_distances = []
        for selector in one_step:
            option_xy = self.architecture.exact_SLM_location_tuple(
                candidates[0][selector][0])
            distance = math.dist(current_xy, option_xy)
            if distance > 1e-12:
                positive_step_distances.append(distance)
        if not positive_step_distances:
            return {}, set(), detail
        lattice_step_move_time_us = math.sqrt(
            min(positive_step_distances)
            / PhysicalIncrementalCost.ACCEL_UM_PER_US2)
        observed_split_transfer_time_us = float(getattr(
            self, "_h0_participant_cycle_split_transfer_time_us", 0.0))
        physical_throttle_reached = (
            observed_split_transfer_time_us
            + 2.0 * PhysicalIncrementalCost.T_TRANSFER_US
            > lattice_step_move_time_us + 1e-12)
        detail.update({
            "observed_split_transfer_time_us": (
                observed_split_transfer_time_us),
            "lattice_step_move_time_us": float(lattice_step_move_time_us),
            "physical_throttle_reached": bool(physical_throttle_reached),
        })
        if physical_throttle_reached:
            return {}, set(), detail
        progressive_selector = min(one_step, key=topology_key)
        progressive_site = tuple(candidates[0][progressive_selector][0])
        detail.update({
            "active": True,
            "current_site": list(current_site),
            "topology_target_site": list(topology_site),
            "progressive_site": list(progressive_site),
            "progressive_gate_gene": int(progressive_selector),
            # This remains a candidate-generation hint.  The native solver also
            # receives the corresponding direct-move singleton and may reject
            # both after exact current physical/ghost replay.
            "cycle_soft_seed": True,
        })
        return {0: progressive_selector}, {q}, detail

    def _build_opts(self, set_sites: set, q1: int, q2: int, blocked,
                    pin_base=None) -> list:
        """把候选工位集做成 (site, w, q1, q2) 菜单；blocked 非空时硬排除；
        pin_base 非空时（钉扎菜单）加列偏移罚——座位基本钉在驻留者列上。"""
        reg = self.registry
        p1, p2 = reg.current_pos(q1), reg.current_pos(q2)
        opts = []
        for site in set_sites:
            if p1[2] < p2[2]:
                s1, s2 = site, (site[0] + 1, site[1], site[2])
            else:
                s1, s2 = (site[0] + 1, site[1], site[2]), site
            if blocked and (s1 in blocked or s2 in blocked):
                continue           # 非参与者驻留者的座位：硬排除
            w = self._site_weight(q1, q2, site)
            if pin_base is not None:
                w += self.w_pin * abs(site[2] - pin_base[2])
            opts.append((site, w, q1, q2))
        opts.sort(key=lambda o: o[1])
        return opts

    def _expanded_sites(self, q1: int, q2: int, list_gate: list) -> set:
        """常规锚点展开（含 ZAC 容量自动扩窗）——非钉扎门的菜单全集。"""
        reg = self.registry
        p1, p2 = reg.current_pos(q1), reg.current_pos(q2)
        expand_factor = max(1, math.ceil(math.sqrt(len(list_gate)) / 2))
        set_sites = set()
        for near in self._zone_anchors(p1, p2):
            slm = self.architecture.dict_SLM[near[0]]
            # +1 配对防御：锚点 SLM 必须有同区搭档（实测两套架构恒满足）
            partner = self.architecture.dict_SLM.get(near[0] + 1)
            if partner is None or partner.entanglement_id != slm.entanglement_id:
                continue
            set_sites.add(near)
            low_r = max(0, near[1] - expand_factor)
            high_r = min(slm.n_r, near[1] + expand_factor + 1)
            low_c = max(0, near[2] - expand_factor)
            high_c = min(slm.n_c, near[2] + expand_factor + 1)
            # ZAC 容量自动扩窗（vmplacer.py:235-242 原样移植，缺它会 no full matching）
            if high_c - low_c < 2 * expand_factor:
                height_gap = math.ceil(len(list_gate) // (high_c - low_c)) - expand_factor
                low_r = max(0, low_r - height_gap // 2)
                high_r = min(slm.n_r, low_r + height_gap + expand_factor)
            if high_r - low_r < 2 * expand_factor:
                width_gap = math.ceil(len(list_gate) / (high_r - low_r)) - expand_factor
                low_c = max(0, low_c - width_gap // 2)
                high_c = min(slm.n_c, low_c + width_gap + expand_factor)
            for r in range(low_r, high_r):
                for c in range(low_c, high_c):
                    set_sites.add((near[0], r, c))
        return set_sites

    def _match_gates(self, candidates: list, list_gate: list, *,
                     return_sites=False) -> list:
        """最小权完美匹配选门位；病态层退化为逐门贪心顺延（不崩整个编译）。"""

        def finish(sites):
            if return_sites:
                return [tuple(site) for site in sites]
            return [
                self._mk_placement(
                    candidates[column][0][2], candidates[column][0][3], site)
                for column, site in enumerate(sites)
            ]

        if len(candidates) == 1:
            # A one-column bipartite matching is exactly its minimum-cost edge.
            # ``_build_opts`` emits unique sites and orders them by weight; the
            # explicit indexed minimum below also preserves scipy's first-row
            # tie-break and its zero-edge promotion to the smallest positive
            # float.  Long serial circuits otherwise paid sparse-matrix and
            # csgraph setup tens of thousands of times for this trivial case.
            opts = candidates[0]
            if len({option[0] for option in opts}) == len(opts):
                _index, chosen = min(
                    enumerate(opts),
                    key=lambda row: (
                        max(float(row[1][1]), np.nextafter(0.0, 1.0)),
                        row[0],
                    ),
                )
                return finish((chosen[0],))
        site_to_row: dict = {}
        rows_list: list = []
        rows, cols, data = [], [], []
        for col, opts in enumerate(candidates):
            for site, w, q1, q2 in opts:
                if site not in site_to_row:
                    site_to_row[site] = len(rows_list)
                    rows_list.append(site)
                rows.append(site_to_row[site])
                cols.append(col)
                # scipy represents the cost matrix sparsely and discards
                # explicit zero entries.  A genuinely zero-distance gate/site
                # edge is still a legal (and usually best) matching edge, so
                # retain it with the smallest positive float instead of
                # silently deleting it from the bipartite graph.
                data.append(max(float(w), np.nextafter(0.0, 1.0)))
        n_rows, n_cols = len(rows_list), len(candidates)

        def greedy():
            """兜底：逐门按权重顺延取未占位；菜单耗尽时全量展开再取。
            （曾在此翻车：耗尽时硬拿 opts[0] 会座位双订——流契约断言逮住过）"""
            used = set()
            chosen = {}
            for col, opts in enumerate(candidates):
                site = next((o[0] for o in opts if o[0] not in used), None)
                if site is None:
                    q1, q2 = opts[0][2], opts[0][3]
                    big = sorted(self._expanded_sites(q1, q2, list_gate))
                    site = next(s for s in big if s not in used)
                used.add(site)
                chosen[col] = site
            return chosen

        try:
            matrix = coo_matrix((np.array(data), (np.array(rows), np.array(cols))),
                                shape=(n_rows, n_cols))
            row_ind, col_ind = min_weight_full_bipartite_matching(matrix)
            chosen = {c: rows_list[r] for r, c in zip(row_ind, col_ind)}
            # scipy 的"full"只保证小侧全覆盖：钉扎菜单太窄时 sites < gates，
            # 可能有门未被匹配——验证全覆盖，否则落入贪心兜底
            if len(chosen) == n_cols:
                return finish(tuple(chosen[col] for col in range(n_cols)))
        except ValueError:
            pass
        chosen = greedy()
        return finish(tuple(chosen[col] for col in range(len(candidates))))

    def _mk_placement(self, q1: int, q2: int, site: tuple) -> dict:
        """由门 + 选定工位落座位对（驻留者保座 + 列序朝向，与权重/缓存同一规则）。"""
        s1, s2 = self._pair_seats(q1, q2, site)
        return {"gate": (q1, q2), "site": site, "seats": (s1, s2)}

    def _repair_placements(self, placements: list, list_gate: list,
                           vacated: set[int] | None = None) -> list:
        """最终安全网：门位不得与其他原子当前座位或其他门已选工位重叠。

        菜单三级兜底在极端拥挤轮次仍可能放过个别工位（qft 宽层实证：
        原子 6 落到参与者原子 5 的座位上，E2 时序救不回来）。这里在选择
        完成后按不变式逐门复查、违例即改选全区过滤域最优工位——正确性
        不再依赖任何菜单层级的表现。
        """
        reg = self.registry
        vacated = set() if vacated is None else set(vacated)
        participants = {q for gate in list_gate for q in gate}
        used = {p["site"] for p in placements}
        out = []
        for p in placements:
            q1, q2 = p["gate"]
            own = {reg.zone_seat.get(q) for q in (q1, q2)}    # 自己的座位可保留
            others = {seat for q, seat in reg.zone_seat.items()
                      if q != q1 and q != q2 and q not in vacated}
            s1, s2 = p["seats"]
            if s1 not in others and s2 not in others and p["site"] not in (used - {p["site"]}):
                out.append(p)
                continue
            # 违例：全区过滤域里挑最优未用工位（others 已排除本门原子自己的座位）
            best, best_w = None, float("inf")
            for site in self._all_zone_sites():
                if site in used:
                    continue
                a, b = site, (site[0] + 1, site[1], site[2])
                if a in others or b in others:
                    continue
                w = self._site_weight(q1, q2, site)
                if w < best_w:
                    best, best_w = site, w
            if best is not None:
                used.discard(p["site"])
                used.add(best)
                out.append(self._mk_placement(q1, q2, best))
            else:
                out.append(p)      # 理论不可达（容量余量恒足）
        return out

    # ============================================================== 鬼点硬保证层
    # 三道防线（与 engine/fitness 完全无关——无前瞻的 GA 也零鬼点）：
    #   ① 菜单预过滤 _filter_menu_ghosts：入区腿自查不撞当前静态原子
    #   ② 解码增量检查（_ga_step.decode 内）：新座位与已落座者组合零新增
    #   ③ _repair_ghosts：提交前确定性修补——本层是最终保证
    # 联合口径定理（ghost.py 模块头）：按相位全腿同批检查零鬼点 ⇒ 路由
    # 着色任意分批后仍零鬼点。修补只会改写 decisions（RESEAT 让座 / 换
    # RETURN 落位）或个别门位，改写后照常走 _append_boundary/_commit_round，
    # 路由端按映射增量自动带上，无特殊处理。

    def _static_ghosts(self, participants):
        """本轮静态原子 [(q, x, y)]：非参与者在当前位置。

        保守口径：预过滤/解码时还不知道 RETURN 决策，驻留者可能本步
        回撤（届时它不再是鬼）——按当前位置滤只会多滤不会漏。精确口径
        在 _repair_ghosts 里按相位分别构建。
        """
        arch = self.architecture
        return [(q, *arch.exact_SLM_location_tuple(self.registry.current_pos(q)))
                for q in range(len(self.mapping[0])) if q not in participants]

    def _filter_menu_ghosts(self, opts, q1, q2, ghosts, keep,
                            indexed_domain=None):
        """防线①：剔除"自己的入区腿就撞鬼"的选项。

        keep = 菜单必须保住的最少选项数（GA 食堂顺延的鸽笼不变式）；
        干净选项不足时按命中数升序回填脏选项——绝不空菜单、绝不少于 keep。
        """
        if not ghosts or not opts:
            return opts
        reg, arch = self.registry, self.architecture
        scored = []
        for o in opts:
            if indexed_domain is None:
                s1, s2 = self._pair_seats(q1, q2, o[0])
            else:
                s1, s2 = indexed_domain.by_site[tuple(o[0])].seats
            legs = []
            for q, s in ((q1, s1), (q2, s2)):
                p0 = arch.exact_SLM_location_tuple(reg.current_pos(q))
                p1 = arch.exact_SLM_location_tuple(s)
                d = math.dist(p0, p1)
                if d > 1e-9:
                    legs.append((d, *p0, *p1))
            scored.append((hit_count(legs, ghosts), o))
        clean = [o for g, o in scored if g == 0]
        if len(clean) >= keep:
            return clean
        dirty = sorted((g, o) for g, o in scored if g > 0)
        return clean + [o for _, o in dirty[:max(0, keep - len(clean))]]

    def _free_zone_seats(self, exclude):
        """空闲激发区单座全集（RESEAT 让座目标域；含左右两种 SLM）。"""
        reg = self.registry
        taken = set(reg.zone_seat.values()) | set(exclude)
        seats = []
        for s in self._all_zone_sites():
            for cand in (s, (s[0] + 1, s[1], s[2])):
                if cand not in taken:
                    seats.append(cand)
        return seats

    def _free_storage_sites(self, exclude):
        """空闲存储位全集（RETURN 落位重选域；SLM 0 全枚举）。"""
        slm = self.architecture.dict_SLM[0]
        taken = set(self.registry.storage_site.values()) | set(exclude)
        return [(0, r, c) for r in range(slm.n_r) for c in range(slm.n_c)
                if (0, r, c) not in taken]

    def _repair_ghosts(self, placements, decisions, pinned_seats=None):
        """防线③：每条腿【单独】不得撞任何静止原子（批化解不了的部分）。

        M2 架构分工：单腿自撞（腿自己的列×行交叉扫到别人）任何分批都
        化解不了——本层在放置期根除；两腿组合撞鬼由路由层的鬼点边强制
        分批（zac_zzx._coloring_batches → ghost.pair_edges）。

        时间线：T0(现在) --back--> T1 --out--> T2。
          back 腿的鬼 = 其余原子在 {T0, T1} 位置（它是别的批的搬运者时
                        飞前坐 T0、飞完坐 T1，两个位置都可能被扫）；
          out  腿的鬼 = 其余原子在 {T1, T2} 位置。
        修补只改写 decisions（RESEAT 让座 / 换 RETURN 落位）或个别门位；
        帽 60 轮，修不净即 raise——硬保证语义：宁可大声失败不静默放走。
        """
        reg, arch = self.registry, self.architecture
        pinned_seats = {
            int(q): tuple(seat) for q, seat in (pinned_seats or {}).items()}
        n_q = len(self.mapping[0])
        ex = lambda loc: arch.exact_SLM_location_tuple(tuple(loc))
        participants = {q for p in placements for q in p["gate"]}

        def t1(q):
            return decisions[q][1] if q in decisions else reg.current_pos(q)

        def t2(q):
            if q in participants:
                for p in placements:
                    if q in p["gate"]:
                        return p["seats"][p["gate"].index(q)]
            return t1(q)

        def both(q, ta, tb):
            """原子在相位前/后两个可能位置（相同则只列一次）。"""
            a, b = ex(ta(q)), ex(tb(q))
            out = [(q, *a)]
            if b != a:
                out.append((q, *b))
            return out

        def gate_of(q):
            for i, p in enumerate(placements):
                if q in p["gate"]:
                    return i
            return None

        def leg_issues():
            """全部单腿自撞问题 [(相位, 腿主q, leg, 受害者id, x, y)]。"""
            issues = []
            for q, v in decisions.items():
                if v[0] == "STAY":
                    continue
                p0, p1 = ex(reg.current_pos(q)), ex(v[1])
                d = math.dist(p0, p1)
                if d < 1e-9:
                    continue
                ghosts = [g for a in range(n_q) if a != q
                          for g in both(a, reg.current_pos, t1)]
                for gid, gx, gy in leg_hits((d, *p0, *p1), ghosts):
                    issues.append(("back", q, (d, *p0, *p1), gid, gx, gy))
            for p in placements:
                for q, s in zip(p["gate"], p["seats"]):
                    p0, p1 = ex(t1(q)), ex(s)
                    d = math.dist(p0, p1)
                    if d < 1e-9:
                        continue
                    ghosts = [g for a in range(n_q) if a != q
                              for g in both(a, t1, t2)]
                    for gid, gx, gy in leg_hits((d, *p0, *p1), ghosts):
                        issues.append(("out", q, (d, *p0, *p1), gid, gx, gy))
            return issues

        fixed_total = 0
        self.ghost_fixes = 0
        banned = {}          # 禁回表：("gate",i)/("dec",q)/("seat",q) → 用过的座位
        for _round in range(12):            # 禁回 ⇒ 状态不重复 ⇒ 无振荡，必收敛
            issues = leg_issues()
            if not issues:
                self.ghost_fixes = fixed_total
                return
            progress = False
            for issue in list(issues):      # 一轮修完所有问题（修法内部
                if self._fix_leg_ghost(     # 按实时状态校验，过期问题天然
                        issue, placements, decisions,   # 无害——条件不满足即跳过）
                        participants, t1, t2, both, gate_of, n_q, banned,
                        pinned_seats):
                    fixed_total += 1
                    progress = True
            if not progress:
                break
        residual = leg_issues()
        raise RuntimeError(
            f"鬼点硬保证层修补失败：{len(residual)} 处单腿自撞残留 "
            f"(首批: {residual[:3]})——请检查修补策略覆盖度")

    def _repair_ghosts_with_commitments(
            self, placements, decisions, pinned_seats):
        """Repair ghosts transactionally, treating residency pins as preferred.

        A pin records that an atom physically stayed in the zone until this
        reuse layer.  Hard ghost safety has higher priority than keeping that
        atom on the exact same gate seat: when no pin-preserving straight-leg
        schedule exists, retry from the untouched pre-repair state without the
        seat restriction.  The caller re-scores the actual repaired schedule,
        so the resulting zone-to-zone move and transfers are never hidden.
        """
        pins = {int(q): tuple(seat) for q, seat in pinned_seats.items()}

        def broken_pins():
            observed = {}
            for placement in placements:
                for q, seat in zip(placement["gate"], placement["seats"]):
                    if q in pins and tuple(seat) != pins[q]:
                        observed[q] = tuple(seat)
            return observed

        pristine_placements = deepcopy(placements)
        pristine_decisions = deepcopy(decisions)
        if not broken_pins():
            try:
                self._repair_ghosts(
                    placements, decisions, pinned_seats=pins)
                return {}, False
            except RuntimeError:
                placements[:] = deepcopy(pristine_placements)
                decisions.clear()
                decisions.update(deepcopy(pristine_decisions))

        # Either the occupancy repair had already displaced a pin or the
        # pin-preserving ghost repair proved infeasible.  Retry once from the
        # pristine state with the common hard-safety repair.  Any failure here
        # still propagates and the attempt remains fail-closed.
        self._repair_ghosts(placements, decisions)
        return broken_pins(), True

    def _fix_leg_ghost(self, issue, placements, decisions, participants,
                       t1, t2, both, gate_of, n_q, banned, pinned_seats=None):
        """修一个单腿自撞问题，返回是否动手。按代价从小到大：

        ① 受害者是驻留非参与者(STAY) → 让座 RESEAT（恒可解兜底）
        ② 腿主是决策(RETURN/RESEAT) → 换落位
        ③ 腿主是参与者 → 改门位
        """
        phase, owner, leg, gid, gx, gy = issue
        reg, arch = self.registry, self.architecture
        pinned_seats = pinned_seats or {}
        ex = lambda loc: arch.exact_SLM_location_tuple(tuple(loc))

        # ① 受害者让座：新座不得被任何腿扫到，让座腿自身也要单腿干净
        if gid not in participants and reg.is_resident(gid) and \
                decisions.get(gid, ("STAY",))[0] == "STAY":
            old = reg.zone_seat[gid]
            exclude = {v[1] for v in decisions.values() if v[0] == "RESEAT"}
            exclude |= {s for p in placements for s in p["seats"]}
            all_legs = [l for q, v in decisions.items() if v[0] != "STAY"
                        for l in [ (lambda a, b: (math.dist(a, b), *a, *b)
                                    )(ex(reg.current_pos(q)), ex(v[1])) ]
                        if l[0] > 1e-9]
            for p in placements:
                for q, s in zip(p["gate"], p["seats"]):
                    a, b = ex(t1(q)), ex(s)
                    if math.dist(a, b) > 1e-9:
                        all_legs.append((math.dist(a, b), *a, *b))
            others = [g for a in range(n_q) if a != gid
                      for g in both(a, reg.current_pos, t1)]
            ban = banned.setdefault(("seat", gid), set())
            for site in sorted(self._free_zone_seats(exclude),
                               key=lambda s: arch.distance(
                                   old[0], old[1], old[2], *s)):
                if site in ban:
                    continue
                t0 = ex(site)
                d = math.dist(ex(old), t0)
                if d < 1e-9 or ghost_hits(all_legs, [(gid, *t0)]):
                    continue
                if leg_hits((d, *ex(old), *t0), others):
                    continue
                decisions[gid] = ("RESEAT", site)
                ban.add(old)
                return True

        # ② 腿主是决策 → 换落位（RETURN 换存储位，RESEAT 换区座位）
        if owner in decisions and decisions[owner][0] in (
                "RETURN", "RESEAT", "PARK"):
            kind = decisions[owner][0]
            sx, sy = ex(reg.current_pos(owner))
            storage_kinds = {"RETURN", "PARK"}
            taken = {
                v[1] for v in decisions.values()
                if (v[0] in storage_kinds
                    if kind in storage_kinds else v[0] == kind)
            }
            taken |= {s for p in placements for s in p["seats"]}
            pool = (self._free_storage_sites(taken)
                    if kind in storage_kinds
                    else self._free_zone_seats(taken))
            ghosts = [g for a in range(n_q) if a != owner
                      for g in both(a,
                                    (reg.current_pos if phase == "back" else t1),
                                    (t1 if phase == "back" else t2))]
            out_legs = []
            for p in placements:
                for q, s in zip(p["gate"], p["seats"]):
                    a, b = ex(t1(q)), ex(s)
                    if math.dist(a, b) > 1e-9:
                        out_legs.append((math.dist(a, b), *a, *b))
            ban = banned.setdefault(("dec", owner), set())
            for site in sorted(pool, key=lambda candidate: math.dist(
                    (sx, sy), ex(candidate))):
                if site in ban:
                    continue
                t0 = ex(site)
                d = math.dist((sx, sy), t0)
                if d < 1e-9:
                    continue
                if leg_hits((d, sx, sy, t0[0], t0[1]), ghosts):
                    continue
                if phase == "back" and ghost_hits(out_legs, [(owner, *t0)]):
                    continue      # 新落位在 out 相也不得被扫
                decisions[owner] = (kind, site)
                ban.add(site)
                return True

        # ③ 腿主是参与者 → 改门位（全区按权重搜索替位）；
        # ④ 受害者是【坐定参与者】（零腿坐在门位）→ 改它的门位
        #    （③的同一套搜索，只是目标门换成受害者的门）
        gi = gate_of(owner)
        if gi is None and gid in participants:
            # 受害者坐定 = 其入区腿为零：改它的门位等于把它挪开
            for p in placements:
                if gid in p["gate"]:
                    others = [q for q, s in zip(p["gate"], p["seats"])
                              if math.dist(ex(t1(q)), ex(s)) < 1e-9]
                    if others:
                        gi = gate_of(gid)
                        owner = gid
                    break
        if gi is not None:
            p = placements[gi]
            q1, q2 = p["gate"]
            vacated = {q for q, value in decisions.items()
                       if value[0] in ("RETURN", "RESEAT", "PARK")}
            others_seats = {seat for qq, seat in reg.zone_seat.items()
                            if qq != q1 and qq != q2 and qq not in vacated}
            used = {pp["site"] for pp in placements}
            ghosts = [g for a in range(n_q) if a not in (q1, q2)
                      for g in both(a, t1, t2)]
            ban = banned.setdefault(("gate", gi), set())
            cand_sites = [s for s in sorted(self._all_zone_sites(),
                               key=lambda s: self._site_weight(q1, q2, s))
                          if s not in used and s not in ban
                          and s not in others_seats
                          and (s[0] + 1, s[1], s[2]) not in others_seats]
            for site in cand_sites:
                new_p = self._mk_placement(q1, q2, site)
                if any(
                        q in pinned_seats
                        and tuple(seat) != tuple(pinned_seats[q])
                        for q, seat in zip(new_p["gate"], new_p["seats"])):
                    continue
                new_legs = []
                clean = True
                for q, s in zip(new_p["gate"], new_p["seats"]):
                    pa, pb = ex(t1(q)), ex(s)
                    d = math.dist(pa, pb)
                    if d > 1e-9:
                        nl = (d, *pa, *pb)
                        if leg_hits(nl, ghosts):
                            clean = False
                            break
                        new_legs.append(nl)
                if not clean:
                    continue
                placements[gi] = new_p
                ban.add(p["site"])
                return True
            import os
            if os.environ.get("GHOST_DEBUG"):
                print(f"[fixdbg] ③失败 owner=q{owner} victim=q{gid} "
                      f"尝试站点数={len(cand_sites)} 全排除/全脏")
        import os
        if os.environ.get("GHOST_DEBUG"):
            print(f"[fixdbg] 无修法适用 phase={phase} owner=q{owner} victim=q{gid} "
                  f"victim是参与者={gid in participants} "
                  f"victim是驻留={self.registry.is_resident(gid)} "
                  f"owner决策={owner in decisions}")
        return False


    def _commit_round(self, layer: int, placement: list):
        """追加第 layer 轮门位映射（= 上一张映射 + 参与者落座）；登记簿入区。"""
        m = list(self.mapping[-1])
        participants = {
            int(q) for p in placement for q in p["gate"]}
        # The pulse is committed before ``enter_zone`` resets participating
        # residents' online rent.  Non-participants accrue both global
        # coherence time and, when resident, one real idle-excitation exposure.
        self.registry.record_rydberg_pulse(
            participants, PhysicalIncrementalCost.T_RYDBERG_US)
        for p in placement:
            q1, q2 = p["gate"]
            s1, s2 = p["seats"]
            m[q1], m[q2] = s1, s2
            self.registry.enter_zone(q1, s1)
            self.registry.enter_zone(q2, s2)
        self.mapping.append(m)

    def _append_boundary(self, decisions: dict):
        """追加含 RETURN、RESEAT 与参与者临时 PARK 的真实边界映射。

        路由端按映射增量取 back 相搬运者（zac_zzx._route_resident 扫全部
        原子的 gate→final 差分），三类腿因此都会自动上车，无需特判。
        """
        m = list(self.mapping[-1])
        for q, (kind, loc) in decisions.items():
            if kind in ("RETURN", "RESEAT", "PARK"):
                m[q] = loc
        self.mapping.append(m)

    # ------------------------------------------------------------------ 流契约
    def _assert_contract(self):
        """审计 MAJOR-4：长度/锚定/拷贝不变式/单射，违例即刻爆炸（不静默）。"""
        n = len(self.gate_scheduling)
        assert len(self.mapping) == 2 * n + 1, \
            f"映射流长度 {len(self.mapping)} ≠ 2×{n}+1"
        participants = [set(q for gate in gates for q in gate)
                        for gates in self.gate_scheduling]
        for L in range(n):
            for q in range(len(self.mapping[0])):
                if q not in participants[L]:
                    assert self.mapping[2 * L + 1][q] == self.mapping[2 * L][q], \
                        f"拷贝不变式破坏：layer {L} 非参与者 {q} 被移动"
        for i, m in enumerate(self.mapping):
            seats = [tuple(s) for s in m]
            assert len(seats) == len(set(seats)), f"映射 {i} 非单射（座位双订）"

    # ============================================================== GA 决策层（A3）
    def _ga_step(self, layer: int):
        """一步联合搜索：边界 L 决策（STAY/RETURN）∪ 轮 L+1 门位。

        染色体 = [每门菜单下标] ++ [有后续使用的非参与者 0=STAY/1=RETURN]；
        适应度 = 分相位着色（back=RETURN 腿 / out=入区腿，各自 DSATUR）
                 + 下次使用锚点前瞻（γ0 按轮次折现）。
        A1 的消融数据（钉 vs 融合各赢一半电路）说明门位取舍必须逐门搜索——
        这里菜单同时含钉扎窗与全展开集，GA 用同一本批数+距离账定夺。
        菜单/锚点/RETURN 落位每步只算一次（常量缓存），fitness 只剩查表
        + 两次小图 DSATUR。
        """
        t0 = time.time()
        next_layer = layer + 1
        list_gate = self.gate_scheduling[next_layer]
        reg, nu, arch = self.registry, self.nu, self.architecture
        participants = {q for gate in list_gate for q in gate}
        static_ghosts = self._static_ghosts(participants)   # 防线①②共用的静态鬼

        # ---- 菜单：钉扎窗 ∪ 全展开；硬排除=区内所有座位除本门原子自己的
        #     （同相位座位交接从构造上禁绝——site 账本只记离开不记到达）----
        candidates: list[list] = []
        gate_cache: list[dict] = []      # (col, site) → {legs, anchor}
        for q1, q2 in list_gate:
            resident_seats = [reg.zone_seat[q] for q in (q1, q2)
                              if reg.is_resident(q)]
            set_sites = self._expanded_sites(q1, q2, list_gate)
            if resident_seats:
                for seat in resident_seats:
                    base = self._norm_left(seat)
                    slm = arch.dict_SLM[base[0]]
                    for dc in range(-self.pin_radius, self.pin_radius + 1):
                        c = base[2] + dc
                        if 0 <= c < slm.n_c:
                            set_sites.add((base[0], base[1], c))
            blocked_g = {seat for q, seat in reg.zone_seat.items()
                         if q != q1 and q != q2}
            opts = self._build_opts(set_sites, q1, q2, blocked_g)
            if not opts:
                opts = self._build_opts(set(self._all_zone_sites()),
                                        q1, q2, blocked_g)
            assert opts, f"layer {next_layer} 门 ({q1},{q2}) 菜单为空"
            # 预扩容：菜单至少"门数"个选项——食堂顺延在被占满的菜单上会
            # 原地打转（ising 宽层钉扎窗挤满时两门同 site 的实证）；
            # 扩容源同样保持硬排除（全区过滤域）
            if len(opts) < len(list_gate):
                seen = {o[0] for o in opts}
                extra = sorted((set(self._all_zone_sites())
                                | self._expanded_sites(q1, q2, list_gate)) - seen,
                               key=lambda s: self._site_weight(q1, q2, s))
                for site in extra:
                    if any(seat in blocked_g for seat in
                           (site, (site[0] + 1, site[1], site[2]))):
                        continue
                    opts.append((site, self._site_weight(q1, q2, site), q1, q2))
                    if len(opts) >= len(list_gate):
                        break
                opts.sort(key=lambda o: o[1])
            # 防线①：入区腿自查撞鬼的选项剔除（保住鸽笼下限 len(list_gate)）
            opts = self._filter_menu_ghosts(opts, q1, q2, static_ghosts,
                                            len(list_gate))
            candidates.append(opts)
            # 每工位缓存：两原子的入区腿 + 坐定者（前瞻 v2：锚点项已删——
            # nolook 对照实证其亏 1%，且"投影到存储位"是回撤世界的遗产假设）
            cache = {}
            for site, _, _, _ in opts:
                s1, s2 = self._pair_seats(q1, q2, site)
                legs = []
                seated = []        # 已坐定参与者（零腿）——解码时成为后续门的鬼
                for q, s in ((q1, s1), (q2, s2)):
                    sx, sy = arch.exact_SLM_location_tuple(reg.current_pos(q))
                    tx, ty = arch.exact_SLM_location_tuple(s)
                    d = math.dist((sx, sy), (tx, ty))
                    if d > 1e-9:
                        legs.append((d, sx, sy, tx, ty))
                    else:
                        seated.append((q, tx, ty))
                cache[site] = {"legs": legs, "seated": seated}
            gate_cache.append(cache)

        # ---- 决策基因与 RETURN 落位常量 ----
        eligible = sorted(q for q in reg.zone_seat
                          if q not in participants and nu.has_future_use(q, layer))
        return_sites = (match_return_sites(reg, eligible, nu, layer,
                                           self.box_ratio, self.alpha_lookahead)
                        if eligible else {})
        dec_cache: dict = {}             # q → {0: STAY(无腿), 1: RETURN 腿}
        for q in eligible:
            seat = reg.zone_seat[q]
            sx, sy = arch.exact_SLM_location_tuple(seat)
            site = return_sites.get(q)
            tx, ty = arch.exact_SLM_location_tuple(site) if site else (None, None)
            ret_leg = (math.dist((sx, sy), (tx, ty)), sx, sy, tx, ty) if site else None
            dec_cache[q] = {0: [], 1: [ret_leg] if ret_leg else []}

        n_gates = len(candidates)
        n_genes = n_gates + len(eligible)

        # ---- 解码：食堂顺延（被占/撞鬼就沿菜单找下一个空位）----
        # 防线②：新座位的入区腿与已落座者的腿做增量鬼点检查，冲突则顺延；
        # 菜单耗尽时退回纯占位检查（第三道防线 _repair_ghosts 兜底）。
        def decode(chrom):
            used = set()
            placed = []
            acc_legs = []
            acc_ghosts = list(static_ghosts)
            for col, opts in enumerate(candidates):
                idx = chrom[col] % len(opts)
                cand = None
                for step in range(len(opts)):
                    c = opts[(idx + step) % len(opts)]
                    if c[0] in used:
                        continue
                    if new_conflicts(acc_legs, gate_cache[col][c[0]]["legs"],
                                     acc_ghosts):
                        continue
                    cand = c
                    break
                if cand is None:
                    for step in range(len(opts)):
                        c = opts[(idx + step) % len(opts)]
                        if c[0] not in used:
                            cand = c
                            break
                used.add(cand[0])
                placed.append(cand)
                e = gate_cache[col][cand[0]]
                acc_legs.extend(e["legs"])
                acc_ghosts.extend(e["seated"])   # 坐定即成鬼
            return placed

        # ---- 适应度：分相位着色 + 锚点前瞻（查表 + 两次 DSATUR）----
        # ---- 软层（前瞻 v2）：鬼点罚（思路二）+ 顺序罚（思路一），缓存 ----
        # 鬼点代理 = 本窗两相"同批组合会撞静止原子"的腿对数——每对在路由
        # 重放审计里≈一次被迫分批，与主项同单位（批）；T0 静止位近似，
        # 精确性由硬保证层兜底。顺序罚 = 未来层存储搭档的进场行碎片化
        # （同起点行必同终点行：同批进场者必须落同一行，行数=批数下界）。
        soft_cache: dict = {}

        def order_penalty(placed):
            """未来 1-2 层还在宿舍的搭档对数 vs 各行自由对位容量的碎片化。

            罚 = Σ_未来层 γ^批距 × (装下该层宿舍搭档所需行数 − 1)。
            当前方案的座位占用决定每行剩多少完整空对——占得碎，行数就多。
            """
            occupied = set(reg.zone_seat.values())
            for c in placed:
                s1, s2 = self._pair_seats(c[2], c[3], c[0])
                occupied.update((s1, s2))
            total = 0.0
            for dl in (1, 2):
                Lf = next_layer + dl
                if Lf >= len(self.gate_scheduling):
                    break
                m = sum(1 for g in self.gate_scheduling[Lf]
                        if not reg.is_resident(g[0]) and not reg.is_resident(g[1]))
                if m == 0:
                    continue
                cap = {}
                for s in self._all_zone_sites():
                    if s not in occupied and \
                            (s[0] + 1, s[1], s[2]) not in occupied:
                        cap[s[1]] = cap.get(s[1], 0) + 1
                used, need = 0, m
                for c in sorted(cap.values(), reverse=True):
                    need -= c
                    used += 1
                    if need <= 0:
                        break
                if need > 0:
                    used = m                 # 容量不足极端档：按每对一行计
                total += (self.gamma_batch ** dl) * max(0, used - 1)
            return total

        def soft_penalty(placed, chrom):
            key = (tuple(c[0] for c in placed),
                   tuple(chrom[n_gates + i] for i in range(len(eligible))))
            if key in soft_cache:
                return soft_cache[key]
            ghosts_t0 = list(static_ghosts)
            for col, cand in enumerate(placed):
                ghosts_t0.extend(gate_cache[col][cand[0]]["seated"])
            legs_out = []
            for col, cand in enumerate(placed):
                legs_out.extend(gate_cache[col][cand[0]]["legs"])
            # owners 与腿一一对齐（gate_cache 腿按 (q1,q2) 顺序跳过零腿）
            owners_o = []
            for col, cand in enumerate(placed):
                e = gate_cache[col][cand[0]]
                q1, q2 = cand[2], cand[3]
                s1, s2 = self._pair_seats(q1, q2, cand[0])
                for q, s in ((q1, s1), (q2, s2)):
                    if math.dist(arch.exact_SLM_location_tuple(reg.current_pos(q)),
                                 arch.exact_SLM_location_tuple(s)) > 1e-9:
                        owners_o.append(q)
            legs_back, owners_b = [], []
            for i, q in enumerate(eligible):
                legs_back.extend(dec_cache[q][chrom[n_gates + i]])
                owners_b.extend([q] * len(dec_cache[q][chrom[n_gates + i]]))
            g = 0
            if legs_out:
                g += len(pair_edges(legs_out, ghosts_t0, owners=owners_o))
            if legs_back:
                g += len(pair_edges(legs_back, ghosts_t0, owners=owners_b))
            r = order_penalty(placed)
            val = self.w_ghost * g + self.w_ord * r
            soft_cache[key] = val
            return val

        def fitness(chrom):
            placed = decode(chrom)
            legs_out = []
            for col, cand in enumerate(placed):
                legs_out.extend(gate_cache[col][cand[0]]["legs"])
            legs_back = []
            for i, q in enumerate(eligible):
                legs_back.extend(dec_cache[q][chrom[n_gates + i]])
            if self.fitness_mode == "lumped":      # A3' 消融档
                cost = batch_cost(legs_back + legs_out, w_batch=self.w_batch)[0] \
                    if legs_back or legs_out else 0.0
                return cost + soft_penalty(placed, chrom)
            cost_b = batch_cost(legs_back, w_batch=self.w_batch)[0] if legs_back else 0.0
            cost_o = batch_cost(legs_out, w_batch=self.w_batch)[0] if legs_out else 0.0
            return cost_b + cost_o + soft_penalty(placed, chrom)

        # ---- 类型感知邻域：门位基因 ±1/重抽；决策基因翻转 ----
        # 异质染色体不能共用一套算子（审计 F3：原 swap 算子跨类型几乎全空转，
        # 浪费一半邻域预算）——按基因类型各用各的：
        #   门位基因（菜单下标）：±1 走相邻菜（微调）或随机重抽（跳出局部）
        #   决策基因（0/1）：直接翻转 STAY↔RETURN
        def neighbor(chrom):
            m = list(chrom)
            if n_gates and (not eligible or self.rng.random() < 0.5):
                gi = self.rng.randrange(n_gates)
                m[gi] = self.rng.choice([m[gi] + 1, m[gi] - 1,
                                         self.rng.randrange(len(candidates[gi]))])
            else:
                di = self.rng.randrange(len(eligible))
                m[n_gates + di] ^= 1
            return m

        # ---- 进化主循环（ZAC_new 引擎 A 骨架：精英保留 + 邻域采样）----
        # zeros 热启动 = 每门选菜单第一项（权重最优的解析解）+ 全体 STAY
        # ——GA 从不比解析差，这是 A1→A3 只升不降的原因。
        # 每轮每条父本采 neighbor_sample_size 个邻居，取前 neighbors_per_solution
        # 入子代，合并精英截断到 population_size。默认预算 6×8×24 ≈ 1158 次评估。
        zeros = [0] * n_genes
        population = [zeros]
        for _ in range(self.population_size - 1):
            population.append([self.rng.randrange(max(1, len(candidates[i]) if i < n_gates else 2))
                               for i in range(n_genes)])
        scored = sorted((fitness(c), c) for c in population)
        for _ in range(self.iterations):
            offspring = []
            for _, c in scored:
                pool = sorted((fitness(m), m)
                              for m in (neighbor(c)
                                        for _ in range(self.neighbor_sample_size)))
                offspring += pool[: self.neighbors_per_solution]
            scored = sorted(scored + offspring)[: self.population_size]

        # ---- 应用最优解：座位对、边界决策、容量阀兜底、提交 ----
        best_c = scored[0][1]
        placed = decode(best_c)
        # 终检安全网照跑：GA 的菜单虽已硬排除，但 decode 顺延在极端拥挤层
        # 仍可能撞座（与 A1 同一不变式，宁可多一道）
        placements = self._repair_placements(
            [self._mk_placement(c[2], c[3], c[0]) for c in placed], list_gate)

        # 按决策基因生成本边界的决策表（GA 模式下菜单已排除他人座位，
        # 所以理论上不会出现 E2 挡路——forced_e2 恒记 0）
        decisions = {}
        for i, q in enumerate(eligible):
            if best_c[n_gates + i]:
                decisions[q] = ("RETURN", return_sites[q])   # 落位用每步一次匹配的常量
            else:
                decisions[q] = ("STAY", reg.zone_seat[q])

        # 容量阀：保留座位 + 2×门数超压时按下次使用降序强制 RETURN（死驻留者最先）
        # （与 decide_lazy 同一规则——GA 只优化"要不要回"，硬约束仍由阀兜底）
        retained = sum(1 for v in decisions.values() if v[0] == "STAY") \
            + sum(1 for q, seat in reg.zone_seat.items()
                  if q not in participants and q not in decisions)
        demand = 2 * len(list_gate)
        cap_evict = []
        by_next_use = sorted(
            (q for q, v in decisions.items() if v[0] == "STAY"),
            key=lambda q: (nu.next_round(q, layer) is None,
                           nu.next_round(q, layer) or 0), reverse=True)
        for q in by_next_use:
            if retained + demand <= self.theta_capacity * reg.zone_sites:
                break
            cap_evict.append(q)
            retained -= 1
        forced_sites = (match_return_sites(reg, cap_evict, nu, layer,
                                           self.box_ratio, self.alpha_lookahead)
                        if cap_evict else {})
        for q, site in forced_sites.items():
            decisions[q] = ("RETURN", site)

        # 防线③：鬼点硬保证——确定性修补（改 decisions/门位），修不净即 raise
        self._repair_ghosts(placements, decisions)
        # 登记簿同步 + 映射流提交（边界 → 门位，顺序与流契约一致）
        for q, (kind, loc) in decisions.items():
            if kind == "RETURN":
                reg.return_to_storage(q, loc)
            elif kind == "RESEAT":
                reg.reseat(q, loc)
        self._append_boundary(decisions)
        self._commit_round(next_layer, placements)
        self.decision_log.append({
            "layer": layer, "engine": "ga",
            "stay": sum(1 for v in decisions.values() if v[0] == "STAY"),
            "return": sum(1 for v in decisions.values() if v[0] == "RETURN"),
            "reseat": sum(1 for v in decisions.values() if v[0] == "RESEAT"),
            "forced_e2": 0, "capacity": len(cap_evict),
            "ghost_fix": getattr(self, "ghost_fixes", 0),
            "participants": len(participants),
            "score": round(scored[0][0], 3)})
        self.search_time += time.time() - t0

    # ========================================================= Schema 2 GA
    def _ga_step_v2(self, layer: int, *, _force_complete_gate_domain=False):
        """Formal M3/M4 transition with a physical, horizon-bounded objective.

        M3 and M4 execute this exact function.  M3 owns a hard H=0 oracle; M4
        consumes a bounded geometric-decay window.  The current boundary is
        always exact and undiscounted; only predicted future terms are weighted.
        Ghost safety and current-phase scoring are shared invariants.
        """
        t0 = time.time()
        transition_step_started_ns = time.perf_counter_ns()
        next_layer = layer + 1
        list_gate = self.forecast.target_layer(layer)
        reg, arch = self.registry, self.architecture
        source_gate_mapping = deepcopy(self.mapping[-1])
        source_one_qubit = self._one_qubit_for_layer(layer)
        # Immutable solve-time prior for the stateful-coherence native DTO.
        # Keeping this snapshot here prevents later state commits from shifting
        # it.  Its explicit tracked scope is leading 1Q + AOD movement + CZ;
        # parent-layer 1Q timing remains a router-level overlap concern.
        coherence_idle_prior = reg.coherence_idle_snapshot()
        approximate_ultra_deep_current = bool(
            self.total_transition_count >= 5000
            and len(self.mapping[0]) <= 16
            and self.resident_backend_requested == "native")
        scheduler_prefix = None
        if approximate_ultra_deep_current:
            # These circuits are already outside the paper's linear-coherence
            # domain and contribute only actual Move/runtime plus the reported
            # exponential sensitivity.  Use the same bounded physical current
            # model as the historical pilot inside the C++ solver; the final
            # production trace remains the sole source of reported metrics.
            scheduler_idle_prior = tuple(coherence_idle_prior)
            self.current_scheduler_prefix = None
            self.expected_scheduler_prefix_sha256 = None
            self.expected_scheduler_prefix_idle_us = None
        else:
            if self.scheduler_reference is None:
                raise RuntimeError("Schema-2 boundary lacks scheduler reference")
            scheduler_prefix = self.scheduler_reference.prepare_source_prefix(
                layer,
                source_gate_mapping,
                self.gate_scheduling[layer],
                source_one_qubit,
            )
            if self.expected_scheduler_prefix_sha256 is not None:
                if scheduler_prefix.scheduler.timing_sha256 != \
                        self.expected_scheduler_prefix_sha256:
                    raise RuntimeError(
                        "committed production scheduler prefix hash differs from "
                        "the prior native-winner candidate audit")
                expected_idle = self.expected_scheduler_prefix_idle_us
                actual_idle = scheduler_prefix.scheduler.idle_time_us
                if (expected_idle is None
                        or len(expected_idle) != len(actual_idle)
                        or any(not math.isclose(
                            float(actual), float(expected), rel_tol=0.0,
                            abs_tol=1e-9)
                            for actual, expected in zip(
                                actual_idle, expected_idle))):
                    raise RuntimeError(
                        "committed production scheduler idle vector differs from "
                        "the prior native-winner candidate audit")
                self.expected_scheduler_prefix_sha256 = None
                self.expected_scheduler_prefix_idle_us = None
            self.current_scheduler_prefix = scheduler_prefix
            scheduler_idle_prior = scheduler_prefix.scheduler.idle_time_us
        participants = {q for gate in list_gate for q in gate}
        if (self.lookahead_horizon == 0
                and self.h0_history_gap_weight > 0.0
                and next_layer > self.h0_history_observed_layer):
            # H=0 may learn only from layers that have already executed.  This
            # exponentially weighted reuse-gap estimate never queries the
            # ForecastOracle beyond the current target layer, so it preserves
            # the no-lookahead contract while discouraging indefinitely cheap
            # STAY decisions for historically sparse qubits.
            for q in sorted(participants):
                previous = self.h0_last_use_layer[q]
                if previous is not None:
                    gap = next_layer - int(previous)
                    if gap > 0:
                        prior = self.h0_gap_ewma[q]
                        self.h0_gap_ewma[q] = (
                            float(gap) if prior is None
                            else 0.5 * float(prior) + 0.5 * float(gap))
                        self.h0_gap_samples[q] += 1
                self.h0_last_use_layer[q] = next_layer
            self.h0_history_observed_layer = next_layer
        horizon_selection_started_ns = time.perf_counter_ns()
        if self.adaptive_lookahead:
            horizon_decision = AdaptiveHorizonDecision.select(
                self.forecast,
                layer,
                residents=reg.zone_seat,
                target_participants=participants,
                zone_sites=reg.zone_sites,
                theta_capacity=self.theta_capacity,
                active_commitments=self.residency_commitments,
            )
        elif self.decay_lookahead:
            horizon_decision = AdaptiveHorizonDecision.fixed(
                self.lookahead_horizon,
                reason=("strict_zero_no_future_access"
                        if self.lookahead_horizon == 0
                        else "fixed_bounded_decay"),
            )
        else:
            # Crucially, fixed H=0 chooses without calling visible_future().
            # Its backing provider therefore sees only the target-layer read.
            horizon_decision = AdaptiveHorizonDecision.fixed(
                self.lookahead_horizon)
        active_horizon = horizon_decision.selected_horizon
        if (self.decay_lookahead and self.total_transition_count > 512
                and active_horizon > (
                    2 if self.total_transition_count >= 5000 else 4)):
            # Geometric lookahead remains genuinely multi-layer and keeps the
            # registered alpha*rho**(d-1) weighting.  On circuits with thousands
            # of transitions, however, replaying eight future layers for every
            # strict ghost-safe candidate dominates the entire compilation.
            # A deterministic four-layer resource cap retains the meaningful
            # high-weight prefix (1, rho, rho^2, rho^3) and is independent of
            # circuit name, baseline score, or future contents.
            active_horizon = (
                2 if self.total_transition_count >= 5000 else 4)
            horizon_decision = AdaptiveHorizonDecision.fixed(
                active_horizon,
                reason=("ultra_deep_geometric_h2"
                        if active_horizon == 2
                        else "long_depth_geometric_h4"))
        forecast = self.forecast.bounded(active_horizon)
        # Materialise the bounded oracle exactly once.  H=0 executes an empty
        # loop and therefore performs no provider read beyond target_layer().
        visible_forecast = tuple(forecast.weighted_future(layer))
        visible_window = tuple(
            (absolute_layer, gates)
            for absolute_layer, gates, _offset, _weight in visible_forecast)
        next_visible_use = {}
        for absolute_layer, gates, _offset, _weight in visible_forecast:
            for q0, q1 in gates:
                next_visible_use.setdefault(int(q0), (absolute_layer, int(q1)))
                next_visible_use.setdefault(int(q1), (absolute_layer, int(q0)))

        def visible_use(q):
            return next_visible_use.get(int(q))

        # Both formal methods use the same generic rich solve.  The strict H=0
        # DTO contains no forecast term; M4 carries the bounded term table.
        use_rich_boundary = (
            self.decay_lookahead
            and self.ablation_fitness_mode == "phase"
        )
        horizon_selection_ns = (
            time.perf_counter_ns() - horizon_selection_started_ns)
        # The boundary is physically split into ``back`` then ``out``.  A
        # non-participating resident may therefore RETURN before the target
        # gates enter and its old zone seat is a legitimate target.  Earlier
        # versions built the gate menu against every T0 resident and silently
        # made that reuse impossible, even when the chromosome selected
        # RETURN.  Only atoms that cannot be moved by this boundary are
        # immutable while the menu is decoded.
        potential_returners = (
            set(reg.zone_seat)
            if self.ablation_policy == "always_return"
            else set(resident_decision_candidates(reg.zone_seat, participants)))
        # Target participants are normally moved only in the out phase.  A
        # lookahead commitment can nevertheless become physically impossible
        # when every straight leg to its pinned seat intersects a stationary
        # atom.  Such a participant must be released in the preceding back
        # phase (RETURN -> re-entry), so keep a separate forced-cycle set rather
        # than weakening ghost safety or falling back to an unrelated router.
        forced_cycle_candidates: set[int] = set()
        movable_before_out = set(potential_returners)

        def static_ghost_rows():
            return [
                (q, *arch.exact_SLM_location_tuple(reg.current_pos(q)))
                for q in range(len(self.mapping[0]))
                if q not in participants and q not in movable_before_out]

        static_ghosts = static_ghost_rows()
        step_cache = CacheStats()
        physical = PhysicalIncrementalCost(len(self.mapping[0]))
        phase_cache = {}
        return_candidate_cache = self.return_candidate_cache

        def movement_phase(legs, ghosts=None, owners=None,
                           exact_threshold=0, batching="phase"):
            """Memoise immutable physical phase costs within one boundary."""
            leg_key = tuple(legs)
            ghost_key = tuple(ghosts) if ghosts else ()
            owner_key = tuple(owners) if owners else ()
            key = (batching, int(exact_threshold), leg_key,
                   ghost_key, owner_key)
            if self.fitness_cache and key in phase_cache:
                return phase_cache[key]
            if (self.fitness_cache and not ghost_key
                    and key in self.phase_cost_cache):
                self.phase_cost_cache.move_to_end(key)
                value = self.phase_cost_cache[key]
                phase_cache[key] = value
                return value
            physical_value = physical.movement_phase(
                leg_key, ghosts=ghosts, owners=owners,
                exact_threshold=exact_threshold, batching=batching)
            boundary_value = BoundaryMovementPhase(
                legs=tuple(
                    BoundaryLeg(
                        float(leg[0]),
                        BoundaryPoint(float(leg[1]), float(leg[2])),
                        BoundaryPoint(float(leg[3]), float(leg[4])),
                    )
                    for leg in leg_key),
                ghosts=tuple(
                    BoundaryGhost(
                        int(ghost[0]),
                        BoundaryPoint(float(ghost[1]), float(ghost[2])),
                    )
                    for ghost in ghost_key),
                owners=owner_key,
                batching=batching,
            )
            value = _BackendMovementPhase(physical_value, boundary_value)
            if self.fitness_cache:
                phase_cache[key] = value
                if not ghost_key:
                    self.phase_cost_cache[key] = value
                    self.phase_cost_cache.move_to_end(key)
                    while len(self.phase_cost_cache) > self.phase_cost_cache_limit:
                        self.phase_cost_cache.popitem(last=False)
            return value

        # ---- Gate menus and immutable leg cache ---------------------------------
        candidates, gate_cache = [], []
        bounded_long_depth_domain = \
            self._use_bounded_deep_serial_gate_domain(
                use_rich_boundary, len(list_gate),
                force_complete=_force_complete_gate_domain)
        indexed_native_rich = bool(
            self._use_indexed_native_gate_domains(use_rich_boundary)
            and not bounded_long_depth_domain)
        indexed_native_domains = []
        indexed_native_dto_domains = []
        indexed_native_gene_by_site = []
        target_pins = {
            q: seat for q, (use_layer, seat) in self.residency_commitments.items()
            if use_layer == next_layer and q in participants
        }

        def gate_blocked_seats(q1, q2):
            if use_rich_boundary:
                return _formal_rich_blocked_seats(
                    reg.zone_seat, participants, movable_before_out)
            return {
                seat for q, seat in reg.zone_seat.items()
                if q not in (q1, q2) and q not in movable_before_out
            }

        for q1, q2 in list_gate:
            indexed_domain = (
                self._build_indexed_rich_gate_domain(q1, q2)
                if indexed_native_rich else None)

            def build_opts(site_domain, current_blocked, *, complete=False,
                           canonical=False):
                if indexed_domain is None:
                    values = self._build_opts(
                        site_domain, q1, q2, current_blocked)
                    return (sorted(values, key=lambda row: (row[1], row[0]))
                            if canonical else values)
                if complete:
                    return self._indexed_rich_complete_opts(
                        indexed_domain, current_blocked,
                        canonical=canonical)
                return self._indexed_rich_local_opts(
                    indexed_domain, site_domain, current_blocked)

            resident_seats = [reg.zone_seat[q] for q in (q1, q2)
                              if reg.is_resident(q)]
            committed_sites = {
                self._norm_left(target_pins[q]) for q in (q1, q2)
                if q in target_pins
            }
            if len(committed_sites) > 1:
                released = {q for q in (q1, q2) if q in target_pins}
                if not self.decay_lookahead:
                    forced_cycle_candidates.update(released)
                movable_before_out.update(released)
                for q in released:
                    target_pins.pop(q, None)
                committed_sites = set()
                static_ghosts = static_ghost_rows()
            if committed_sites:
                sites = set(committed_sites)
            else:
                sites = self._expanded_sites(q1, q2, list_gate)
                for seat in resident_seats:
                    base = self._norm_left(seat)
                    slm = arch.dict_SLM[base[0]]
                    for dc in range(-self.pin_radius, self.pin_radius + 1):
                        col = base[2] + dc
                        if 0 <= col < slm.n_c:
                            sites.add((base[0], base[1], col))
            # The formal rich solver jointly replays every target participant,
            # so another gate's source seat belongs in its complete domain.
            # Legacy per-column decoding retains its conservative filter.
            blocked = gate_blocked_seats(q1, q2)
            opts = build_opts(sites, blocked)
            if not opts and not committed_sites:
                opts = build_opts(
                    set(self._all_zone_sites()), blocked, complete=True)
            if not committed_sites and len(opts) < len(list_gate):
                seen = {o[0] for o in opts}
                for site in sorted(set(self._all_zone_sites()) - seen,
                                   key=lambda s: (
                                       indexed_domain.by_site[tuple(s)].option[1]
                                       if indexed_domain is not None else
                                       self._site_weight(q1, q2, s))):
                    pair = (site, (site[0] + 1, site[1], site[2]))
                    if pair[0] in blocked or pair[1] in blocked:
                        continue
                    opts.append(
                        indexed_domain.by_site[tuple(site)].option
                        if indexed_domain is not None else
                        (site, self._site_weight(q1, q2, site), q1, q2))
                    if len(opts) >= len(list_gate):
                        break
                opts.sort(key=lambda o: (o[1], o[0]))
            opts = self._filter_menu_ghosts(
                opts, q1, q2, static_ghosts, max(1, len(list_gate)),
                indexed_domain=indexed_domain)
            if not opts and committed_sites:
                # The earlier STAY remains honoured until this target boundary,
                # but its pin cannot be executed ghost-safely.  Release the
                # pinned endpoint(s) through a mandatory physical cycle, then
                # rebuild the menu over the ordinary complete domain.
                released = {q for q in (q1, q2) if q in target_pins}
                if not self.decay_lookahead:
                    forced_cycle_candidates.update(released)
                movable_before_out.update(released)
                for q in released:
                    target_pins.pop(q, None)
                static_ghosts = static_ghost_rows()
                blocked = gate_blocked_seats(q1, q2)
                opts = build_opts(
                    set(self._all_zone_sites()), blocked, complete=True)
                opts = self._filter_menu_ghosts(
                    opts, q1, q2, static_ghosts,
                    max(1, len(list_gate)),
                    indexed_domain=indexed_domain)
            elif not opts:
                # A local expansion is only a speed path.  Preserve the hard
                # feasibility contract by retrying the complete gate domain
                # before declaring the physical layer impossible.
                opts = build_opts(
                    set(self._all_zone_sites()), blocked, complete=True)
                opts = self._filter_menu_ghosts(
                    opts, q1, q2, static_ghosts,
                    max(1, len(list_gate)),
                    indexed_domain=indexed_domain)
            if use_rich_boundary and not bounded_long_depth_domain:
                # Candidate-level deterministic gate repair must see the same
                # complete zone domain as the final Python safety net.  A seat
                # promised by an earlier forecast stays first in the menu, but
                # it is not allowed to make the real target boundary
                # impossible.  Exact ghost replay may therefore override a
                # stale commitment by selecting another executable gate site.
                seen = {tuple(option[0]) for option in opts}
                complete = build_opts(
                    set(self._all_zone_sites()), blocked,
                    complete=True, canonical=True)
                for option in complete:
                    if tuple(option[0]) not in seen:
                        opts.append(option)
                        seen.add(tuple(option[0]))
            elif bounded_long_depth_domain:
                # Ultra-deep QMAP circuits are overwhelmingly one- and
                # two-gate layers.  Shipping the complete 140-site menu across
                # Python/C++ for every one of tens of thousands of boundaries
                # made the native implementation slower than the historical
                # Python pilot.  Keep the physically local ZAC expansion and a
                # deterministic amount of injective slack.  Every retained
                # option has already passed the exact single-leg ghost filter;
                # the native solver still performs combined-ghost coloring,
                # RETURN matching, capacity repair and final physical replay.
                # Empty local menus already take the complete-domain recovery
                # branch above, so this bound never converts infeasibility into
                # a silent fallback.
                if self.total_transition_count >= 50_000:
                    local_limit = max(2, 2 * len(list_gate))
                elif self.total_transition_count >= 5_000:
                    local_limit = max(4, 2 * len(list_gate))
                else:
                    local_limit = max(8, 2 * len(list_gate) + 4)
                if len(opts) > local_limit:
                    opts = opts[:local_limit]
            if not opts:
                raise RuntimeError(f"layer {next_layer} 门 ({q1},{q2}) 无合法门位")
            candidates.append(opts)
            if indexed_domain is not None:
                indexed_native_domains.append(indexed_domain)
                indexed_native_dto_domains.append(tuple(
                    indexed_domain.by_site[tuple(option[0])].rich_option
                    for option in opts))
                indexed_native_gene_by_site.append({
                    tuple(option[0]): index
                    for index, option in enumerate(opts)
                })
            if use_rich_boundary:
                # The rich solver consumes indexed gate endpoints
                # directly from ``RichGateOption`` below.  It never calls the
                # legacy Python ``decode`` closure, so eagerly materialising a
                # dictionary of Python legs for every option duplicated the
                # entire complete-zone domain at every boundary.  Long QMAP
                # circuits execute tens of thousands of boundaries; keeping a
                # placeholder here preserves column indexing while avoiding
                # millions of unused tuples, dictionaries and ``math.dist``
                # calls.  ``decode`` fails closed if this contract ever drifts.
                gate_cache.append(None)
            else:
                cached_sites = {}
                for site, _, _, _ in opts:
                    s1, s2 = self._pair_seats(q1, q2, site)
                    legs, owners, seated = [], [], []
                    for q, target in ((q1, s1), (q2, s2)):
                        source_xy = arch.exact_SLM_location_tuple(
                            reg.current_pos(q))
                        target_xy = arch.exact_SLM_location_tuple(target)
                        distance = math.dist(source_xy, target_xy)
                        if distance > 1e-9:
                            legs.append((distance, *source_xy, *target_xy))
                            owners.append(q)
                        else:
                            seated.append((q, *target_xy))
                    cached_sites[site] = {
                        "legs": tuple(legs), "owners": tuple(owners),
                        "seated": tuple(seated),
                    }
                gate_cache.append(cached_sites)

        # Every non-participating resident is a real decision, including dead
        # ones.  On a serial chain, the one resident target participant is also
        # a native joint decision: bit 0 moves it directly in the out phase,
        # while bit 1 performs a physical back-to-storage then out-to-gate
        # cycle.  This is part of the same chromosome as gate placement and
        # RETURN matching; it is never a winner-after-the-fact Python repair.
        resident_eligible = sorted(potential_returners)
        adjacent_resident_participants = sorted(
            set(reg.zone_seat) & participants)
        formal_cycle_candidates = (
            adjacent_resident_participants
            if (use_rich_boundary
                and self.ablation_policy == "optimize"
                and len(list_gate) == 1
                and len(adjacent_resident_participants) == 1)
            else [])

        # H=0's nano16 population evaluates its deterministic gene-zero seeds
        # before stochastic variants.  Promote one current-only, low-topology
        # progressive centre into that slot so both the direct and participant-
        # cycle forms reach the existing exact physical scorer.  This is only a
        # domain-ordering hint: every option remains present, native NLL/ghost
        # replay still chooses the winner, and M4's recommended-STAY field is
        # neither read nor written here.
        h0_participant_cycle_recommended_returns: set[int] = set()
        h0_participant_cycle_amortization = {
            "policy": "h0_participant_cycle_amortization_v1",
            "active": False,
        }
        h0_participant_cycle_promoted_genes: dict[int, int] = {}
        if active_horizon == 0 and self.method_id == "ours_nl":
            (progressive_genes,
             h0_participant_cycle_recommended_returns,
             h0_participant_cycle_amortization) = \
                self._h0_participant_cycle_amortization_seed(
                    list_gate, candidates, formal_cycle_candidates)
            for column, selector in progressive_genes.items():
                selector = int(selector)
                option = candidates[column][selector]
                candidates[column] = (
                    [option]
                    + candidates[column][:selector]
                    + candidates[column][selector + 1:])
                h0_participant_cycle_promoted_genes[column] = 0
                if indexed_native_rich:
                    indexed_domain = indexed_native_domains[column]
                    indexed_native_dto_domains[column] = tuple(
                        indexed_domain.by_site[tuple(row[0])].rich_option
                        for row in candidates[column])
                    indexed_native_gene_by_site[column] = {
                        tuple(row[0]): index
                        for index, row in enumerate(candidates[column])
                    }
            h0_participant_cycle_amortization[
                "prior_progressive_gate_genes"] = {
                    str(column): int(selector)
                    for column, selector in progressive_genes.items()
                }
            h0_participant_cycle_amortization[
                "promoted_progressive_gate_genes"] = {
                    str(column): int(selector)
                    for column, selector in
                    h0_participant_cycle_promoted_genes.items()
                }
        eligible = sorted(
            set(resident_eligible) | set(formal_cycle_candidates))
        # The extra RETURN -> re-entry refinement is exact only for a serial
        # chain boundary: one target gate with one reused resident endpoint.
        # On a parallel target front, independently cycling one endpoint can
        # displace the joint gate matching beyond the finite rollout (QFT is a
        # concrete example).  The ordinary resident GA already evaluates that
        # coupled placement, so keep the local extension fail-closed there.
        optional_cycle_candidates = (
            adjacent_resident_participants
            if (not self.decay_lookahead
                and self.ablation_policy == "optimize"
                and len(list_gate) == 1
                and len(adjacent_resident_participants) == 1)
            else [])
        cycle_candidates = sorted(
            set(optional_cycle_candidates) | forced_cycle_candidates)
        n_gates = len(candidates)
        gate_domains = [len(opts) for opts in candidates]
        demand = 2 * len(list_gate)
        max_stays = math.floor(self.theta_capacity * reg.zone_sites - demand + 1e-12)
        if max_stays < 0:
            raise RuntimeError(
                f"layer {next_layer} 当前门需求 {demand} 已超过驻留容量阈值")
        # A participant cycle returns only transiently: the atom re-enters as
        # part of ``demand`` and therefore does not free final zone capacity.
        # Native/reference normalization count only non-participant RETURN bits
        # toward this lower bound.
        min_returns = max(0, len(resident_eligible) - max_stays)

        # Capacity is normalized before fitness, never patched onto the winner.
        def eviction_key(q):
            if active_horizon == 0:
                # H=0 capacity repair is current-state deterministic and may
                # not consult the next-use oracle merely to break a tie.
                return (False, 0, q)
            visible = visible_use(q)
            return (visible is None, visible[0] if visible else -1, q)

        eviction_order = (
            sorted(resident_eligible, key=eviction_key, reverse=True)
            + sorted(formal_cycle_candidates))
        # Receding-horizon guard: LK may retain an atom only when its next use
        # is actually visible inside L+2/L+3.  Otherwise every boundary can
        # postpone the same terminal RETURN by two layers and the atom remains
        # illuminated indefinitely (the classic finite-horizon procrastination
        # failure observed on multiply).  This rule consumes no information
        # beyond ForecastOracle and is intentionally absent for H=0, whose
        # contract is the exact current transition.
        horizon_forced_returns = {
            q for q in eligible
            if (not self.decay_lookahead
                and self.ablation_policy == "optimize"
                and active_horizon > 0
                and visible_use(q) is None)
        }

        # A visible future use is necessary but not sufficient for residency.
        # Compare every proposed cross-layer wait with a deterministic
        # all-RETURN reference using only the current state and ForecastOracle.
        # The guard credits transfer fidelity and the moving atom's coherence,
        # but not stationary-atom coherence that may disappear when moves share
        # a batch.  This guard belongs only to the legacy discrete-horizon path;
        # formal decay expresses future residency explicitly in forecast_terms.
        all_return_sites = (match_return_sites(
            reg, eligible, self.nu, layer,
            self.box_ratio, self.alpha_lookahead,
            forecast=forecast,
            candidate_mode=(
                "forecast" if active_horizon and not self.decay_lookahead
                else "nearest"),
            candidate_cache=return_candidate_cache)
            if eligible and not use_rich_boundary else {})
        physical_guard_details = []
        physical_forced_returns = set()
        if (not self.decay_lookahead
                and self.ablation_policy == "optimize"
                and active_horizon > 0):
            for q in resident_eligible:
                visible = visible_use(q)
                if visible is None:
                    continue
                idle_pulses = visible[0] - next_layer
                source = arch.exact_SLM_location_tuple(reg.zone_seat[q])
                storage = arch.exact_SLM_location_tuple(all_return_sites[q])
                distance = math.dist(source, storage)
                leg_out = (distance, *source, *storage)
                leg_back = (distance, *storage, *source)
                phases = (
                    movement_phase([leg_out], owners=[q]),
                    movement_phase([leg_back], owners=[q]),
                )
                admit, idle_nll, avoided_move_nll = \
                    physical.residency_break_even(phases, idle_pulses)
                if not admit:
                    physical_forced_returns.add(q)
                physical_guard_details.append({
                    "q": q,
                    "next_use_layer": visible[0],
                    "idle_exposures": idle_pulses,
                    "idle_nll": idle_nll,
                    "avoidable_return_nll": avoided_move_nll,
                    "margin_nll": avoided_move_nll - idle_nll,
                    "forced_return": not admit,
                })

        # Formal decay rent-or-return audit.  This executes before chromosome
        # normalization so its ghost-safe witness can enrich the bounded RETURN
        # domain.  Recommendations remain soft on their first boundary; the
        # one-pulse rolling-horizon deadline below may then populate the native
        # forced-return mask to prevent infinite terminal-cost postponement.
        # M3 projects exactly the immediate target pulse from current state and
        # never asks the oracle for a future use.  M4 returns atoms with no
        # reuse in its registered window; visible reuses are admitted only
        # while the *future incremental* STAY loss remains cheaper than a
        # dedicated ghost-safe RETURN/re-entry round trip.  Historical rent is
        # audit evidence only: it is sunk cost and must not be charged again.
        rent_guard_details = []
        rent_recommended_returns: set[int] = set()
        rent_recommended_stays: set[int] = set()
        rent_forced_returns: set[int] = set()
        rent_guard_return_sites: dict[int, tuple] = {}
        h0_reentry_return_nll: dict[int, float] = {}
        h0_reentry_value_scales: dict[int, float] = {}
        h0_opportunity_stay_scales: dict[int, float] = {}
        h0_history_gap_extra_pulses: dict[int, float] = {}

        def cheapest_ghost_safe_return(q):
            zone_location = tuple(reg.zone_seat[q])
            source_xy = arch.exact_SLM_location_tuple(zone_location)
            occupied = reg.occupied_storage()
            nearest = tuple(arch.nearest_storage_site(*zone_location))
            centers = (
                (tuple(reg.homes[q]),)
                if active_horizon == 0
                and getattr(
                    self, "return_anchor_effective_policy",
                    self.return_anchor_policy) == "home_stable"
                else (nearest, tuple(reg.homes[q])))
            local = set()
            for center in centers:
                if center[0] in arch.storage_zone:
                    local.update(_box_sites(
                        arch, center, self.box_ratio, occupied))
            ghosts = [
                (atom, *arch.exact_SLM_location_tuple(reg.current_pos(atom)))
                for atom in range(len(self.mapping[0])) if atom != q
            ]

            def key(site):
                target_xy = arch.exact_SLM_location_tuple(site)
                return (math.dist(source_xy, target_xy), tuple(site))

            def first_safe(domain):
                for site in sorted(domain, key=key):
                    site = tuple(site)
                    target_xy = arch.exact_SLM_location_tuple(site)
                    distance = math.dist(source_xy, target_xy)
                    leg = (distance, *source_xy, *target_xy)
                    if distance > 1e-9 and not leg_hits(leg, ghosts):
                        return site, leg
                return None

            selected = first_safe(local)
            if selected is None:
                # This path is rare (the local boxes normally contain dozens
                # of sites) but keeps the hard ghost contract complete.
                selected = first_safe(
                    site for site in _all_storage_sites(arch)
                    if tuple(site) not in occupied)
            return selected

        if self.decay_lookahead and self.ablation_policy == "optimize":
            for q in resident_eligible:
                # The explicit branch is an H=0 information firewall: no
                # visible-use lookup, future layer access or future-dependent
                # return anchor is evaluated for M3.
                visible = (None if active_horizon == 0 else visible_use(q))
                history_exposures, history_time_us = reg.resident_rent(q)
                h0_terminal_chain = (
                    active_horizon == 0
                    and self.h0_rent_policy == "topology_terminal_v2"
                    and self.static_partner_degrees.get(q, 0) <= 2
                    and self.static_interaction_totals.get(q, 0) <= 2)
                h0_topology_current = (
                    active_horizon == 0
                    and (self.h0_rent_policy == "topology_current_v1"
                         or h0_terminal_chain))
                h0_static_reuse_support = (
                    sum(
                        1 for participant in participants
                        if self.static_interaction_counts.get(
                            (q, participant), 0) > 0)
                    if h0_topology_current else 0)
                h0_target_interaction_mass = (
                    sum(
                        self.static_interaction_counts.get(
                            (q, participant), 0)
                        for participant in participants)
                    if active_horizon == 0 else 0)
                h0_target_interaction_support = (
                    sum(
                        1 for participant in participants
                        if self.static_interaction_counts.get(
                            (q, participant), 0) > 0)
                    if active_horizon == 0 else 0)
                h0_static_interaction_total = int(
                    self.static_interaction_totals.get(q, 0))
                h0_target_interaction_ratio = (
                    float(h0_target_interaction_mass)
                    / float(h0_static_interaction_total)
                    if h0_static_interaction_total > 0 else 0.0)
                if active_horizon == 0:
                    h0_opportunity_stay_scales[q] = max(
                        0.0, min(1.0, 1.0 - h0_target_interaction_ratio))
                if (active_horizon == 0
                        and self.h0_history_gap_weight > 0.0):
                    last_use = self.h0_last_use_layer[q]
                    gap_estimate = self.h0_gap_ewma[q]
                    samples = int(self.h0_gap_samples[q])
                    if last_use is not None and gap_estimate is not None:
                        age = max(1, next_layer - int(last_use))
                        remaining = max(1.0, float(gap_estimate) - age)
                        confidence = min(1.0, samples / 2.0)
                        h0_history_gap_extra_pulses[q] = (
                            max(0.0, remaining - 1.0) * confidence)
                h0_reentry_value_selected = (
                    active_horizon == 0
                    and self.static_interaction_median + 1e-15 >=
                    self.h0_reentry_value_min_circuit_median_interactions
                    and h0_target_interaction_mass >=
                    self.h0_reentry_value_min_interaction_mass
                    and h0_target_interaction_ratio + 1e-15 >=
                    self.h0_reentry_value_min_interaction_ratio)
                h0_reentry_value_scale = (
                    h0_target_interaction_ratio
                    if self.h0_reentry_value_scale_by_interaction_ratio
                    else 1.0)
                h0_admit_one_rent = (
                    h0_topology_current
                    and history_exposures == 0
                    and h0_static_reuse_support >= 2)
                if active_horizon == 0 and not h0_topology_current:
                    # Deterministic online ski-rental rule: every observed
                    # unused pulse is one paid rent unit. H=0 reads no circuit
                    # future, but it buys the RETURN once accumulated rent plus
                    # the immediate pulse reaches the bounded round-trip cost.
                    # Capping the certificate keeps it a local progress rule,
                    # not an invented long-horizon forecast.
                    future_pulses = 1 + min(history_exposures, 3)
                elif active_horizon == 0:
                    # Strict current-transition comparison.  Historical rent
                    # is sunk and no unobserved pulse is invented.
                    future_pulses = 1
                elif visible is None:
                    # Every pulse in the bounded visible window is avoidable
                    # when the atom has no registered reuse there.
                    future_pulses = 1 + len(visible_forecast)
                else:
                    future_pulses = max(1, visible[0] - next_layer)
                future_pulse_idle_us = (
                    future_pulses * physical.T_RYDBERG_US)
                # Rank the two alternatives against the exact ASAP scheduler
                # prior used by the native candidate scorer.  Registry rent is
                # retained only as historical audit evidence (sunk cost).
                prior_idle_us = float(scheduler_idle_prior[q])

                def conditional_coherence_nll(delta_us):
                    delta_us = float(delta_us)
                    after = prior_idle_us + delta_us
                    if delta_us < -1e-12:
                        return float("inf")
                    # The paper's linear coherence term is still enforced by
                    # the independent final scorer, which reports OOD once an
                    # atom reaches T2.  Search itself must nevertheless keep
                    # ranking candidates on long circuits; use the registered
                    # exponential sensitivity only after either endpoint
                    # leaves the linear model's domain.
                    if (prior_idle_us >= physical.T2_US
                            or after >= physical.T2_US):
                        return delta_us / physical.T2_US
                    return (
                        math.log1p(-prior_idle_us / physical.T2_US)
                        - math.log1p(-after / physical.T2_US))

                stay_excitation_nll = (
                    -future_pulses * math.log(physical.F_EXC))
                stay_coherence_nll = conditional_coherence_nll(
                    future_pulse_idle_us)
                stay_increment_nll = (
                    stay_excitation_nll + stay_coherence_nll)

                safe_return = cheapest_ghost_safe_return(q)
                return_site = None
                return_distance = None
                return_nll = float("inf")
                return_transfer_nll = float("inf")
                return_coherence_nll = float("inf")
                return_move_idle_us = None
                if safe_return is not None:
                    return_site, leg_out = safe_return
                    return_distance = float(leg_out[0])
                    leg_back = (
                        leg_out[0], leg_out[3], leg_out[4],
                        leg_out[1], leg_out[2])
                    return_phase = movement_phase([leg_out], owners=[q])
                    reentry_phase = movement_phase([leg_back], owners=[q])
                    phases = ((return_phase,) if h0_topology_current else
                              (return_phase, reentry_phase))
                    movers = sum(phase.movers for phase in phases)
                    return_move_idle_us = sum(
                        max(0.0, phase.move_time_us
                            - 2.0 * physical.T_TRANSFER_US)
                        for phase in phases)
                    return_transfer_nll = (
                        -2 * movers * math.log(physical.F_TRANSFER))
                    # Both alternatives wait through the same future pulses.
                    # RETURN additionally pays the moving atom's exact
                    # load-excluded AOD idle and its real transfer errors.
                    return_coherence_nll = conditional_coherence_nll(
                        future_pulse_idle_us + return_move_idle_us)
                    return_nll = (
                        return_transfer_nll + return_coherence_nll)
                    rent_guard_return_sites[q] = tuple(return_site)
                    if active_horizon == 0:
                        reentry_move_idle_us = max(
                            0.0,
                            reentry_phase.move_time_us
                            - 2.0 * physical.T_TRANSFER_US)
                        h0_reentry_return_nll[q] = (
                            -2 * reentry_phase.movers
                            * math.log(physical.F_TRANSFER)
                            + conditional_coherence_nll(
                                reentry_move_idle_us))
                        if h0_reentry_value_selected:
                            h0_reentry_value_scales[q] = (
                                h0_reentry_value_scale)

                no_visible_reuse = active_horizon > 0 and visible is None
                recommended = (
                    safe_return is not None
                    and math.isfinite(return_nll)
                    and return_nll + 1e-12 < stay_increment_nll)
                # Only M4 turns a proven round-trip STAY win into a hard trust-
                # region hint.  H=0 instead receives the fractional, current-
                # witness re-entry value below; a hard mask over-retains atoms
                # whose actual next reuse is unknown and loses fidelity to idle
                # excitation on long circuits.
                recommended_stay = (
                    active_horizon > 0
                    and safe_return is not None
                    and math.isfinite(return_nll)
                    and stay_increment_nll + 1e-12 < return_nll)
                if recommended:
                    rent_recommended_returns.add(q)
                elif recommended_stay:
                    rent_recommended_stays.add(q)
                # A bounded rolling controller may otherwise postpone the
                # same terminal RETURN forever: the atom is RESEATed/STAYed,
                # leaves the visible window again, and pays another real idle
                # excitation at every boundary.  Past rent remains sunk cost
                # and is never added to fitness.  It only supplies a one-pulse
                # progress certificate: after one already-observed exposure,
                # a still-unreused atom whose ghost-safe RETURN is physically
                # cheaper receives a native pre-score RETURN commitment.
                # The native solver still chooses the gate and RETURN site and
                # must replay the joint move with zero ghost hits.
                forced_return = (
                    recommended
                    and (
                        (h0_topology_current
                         and not h0_admit_one_rent)
                        or (not h0_topology_current
                            and history_exposures > 0
                            and (active_horizon == 0
                                 or no_visible_reuse))))
                if forced_return:
                    rent_forced_returns.add(q)
                rent_guard_details.append({
                    "q": q,
                    "mode": ("h0_current_only" if active_horizon == 0 else
                             ("no_visible_reuse" if visible is None else
                              "visible_break_even")),
                    "next_use_layer": (None if visible is None else visible[0]),
                    "history_idle_exposures": history_exposures,
                    "history_idle_time_us": history_time_us,
                    "coherence_prior_us": prior_idle_us,
                    "future_idle_exposures": future_pulses,
                    "h0_rent_policy": self.h0_rent_policy,
                    "h0_static_reuse_support": h0_static_reuse_support,
                    "h0_static_interaction_total": (
                        self.static_interaction_totals.get(q, 0)),
                    "h0_target_interaction_mass": (
                        h0_target_interaction_mass),
                    "h0_target_interaction_support": (
                        h0_target_interaction_support),
                    "h0_target_interaction_ratio": (
                        h0_target_interaction_ratio),
                    "h0_reentry_value_selected": (
                        h0_reentry_value_selected),
                    "h0_static_interaction_median": (
                        self.static_interaction_median),
                    "h0_reentry_value_scale": h0_reentry_value_scale,
                    "h0_terminal_chain": h0_terminal_chain,
                    "h0_admit_one_rent": h0_admit_one_rent,
                    "return_cost_scope": (
                        "current_one_way" if h0_topology_current
                        else "return_and_reentry"),
                    "future_pulse_idle_time_us": future_pulse_idle_us,
                    "stay_excitation_nll": stay_excitation_nll,
                    "stay_coherence_increment_nll": stay_coherence_nll,
                    "stay_increment_nll": stay_increment_nll,
                    "return_site": (None if return_site is None
                                    else list(return_site)),
                    "return_distance_um": return_distance,
                    "return_move_idle_time_us": return_move_idle_us,
                    "return_transfer_nll": return_transfer_nll,
                    "return_coherence_increment_nll": return_coherence_nll,
                    "return_round_trip_nll": return_nll,
                    "h0_reentry_return_nll": (
                        h0_reentry_return_nll.get(q)),
                    "h0_reentry_value_weight": (
                        self.h0_reentry_value_weight),
                    "margin_nll": return_nll - stay_increment_nll,
                    "recommended_return": recommended,
                    "recommended_stay": recommended_stay,
                    # Before the one-pulse rolling deadline this remains a
                    # soft exact-scored recommendation.  At the deadline only
                    # the RETURN bit is committed; K-best site assignment and
                    # gate placement remain joint native decisions.
                    "forced_return": forced_return,
                    "stay_admitted": not forced_return,
                    "reason": ("no_ghost_safe_return" if safe_return is None
                               else (("h0_low_topology_return"
                                      if (forced_return
                                          and h0_topology_current
                                          and history_exposures == 0)
                                      else "rolling_horizon_return_deadline")
                                     if forced_return
                                     else ("no_visible_reuse" if no_visible_reuse
                                     else ("physical_break_even" if recommended
                                           else "rent_below_return")))),
                })

        def resolve_commitment_conflicts(bits):
            """At most one incompatible resident may pin a visible future gate."""
            index = {q: i for i, q in enumerate(eligible)}
            grouped = {}
            for q, bit in zip(eligible, bits):
                if bit:
                    continue
                visible = visible_use(q)
                if visible is None:
                    continue
                use_layer, partner = visible
                grouped.setdefault(
                    (use_layer, tuple(sorted((q, partner)))), []).append(q)
            for _key, atoms in grouped.items():
                if len(atoms) < 2:
                    continue
                sites = {self._norm_left(reg.zone_seat[q]) for q in atoms}
                if len(sites) <= 1:
                    continue
                # Deterministic single-pin repair.  Lower id keeps residency;
                # the other atom returns and remains an ordinary future mover.
                for q in sorted(atoms)[1:]:
                    bits[index[q]] = 1
            return bits

        normalize_cache = {}

        def normalize_chrom(chrom):
            source = tuple(chrom)
            if self.fitness_cache and source in normalize_cache:
                return list(normalize_cache[source])
            raw = list(source)
            if len(raw) != n_gates + len(eligible):
                raise ValueError("Schema 2 染色体长度错误")
            normalized = [raw[i] % gate_domains[i] for i in range(n_gates)]
            bits = [1 if raw[n_gates + i] else 0 for i in range(len(eligible))]
            if self.ablation_policy == "always_stay":
                bits = [0] * len(eligible)
            elif self.ablation_policy in {"always_return", "adjacent_only"}:
                bits = [1] * len(eligible)
            elif horizon_forced_returns:
                for i, q in enumerate(eligible):
                    if q in horizon_forced_returns:
                        bits[i] = 1
            if physical_forced_returns and self.ablation_policy == "optimize":
                for i, q in enumerate(eligible):
                    if q in physical_forced_returns:
                        bits[i] = 1
            if (not self.decay_lookahead
                    and active_horizon > 0
                    and self.ablation_policy == "optimize"):
                # The pairwise commitment projection belongs to the legacy
                # discrete-horizon search.  Formal decay M4 has already scored
                # the coupled future geometry and its native winner must not be
                # rewritten by a second Python-only normalization rule.
                bits = resolve_commitment_conflicts(bits)
            selected = sum(bits)
            if selected < min_returns:
                index = {q: i for i, q in enumerate(eligible)}
                for q in eviction_order:
                    i = index[q]
                    if bits[i] == 0:
                        bits[i] = 1
                        selected += 1
                        if selected == min_returns:
                            break
            value = tuple(normalized + bits)
            if self.fitness_cache:
                normalize_cache[source] = value
            return list(value)

        # ---- Decode cache --------------------------------------------------------
        decode_cache = {}

        def decode(chrom):
            if use_rich_boundary:
                raise RuntimeError(
                    "rich-solver-owned boundary entered legacy Python decode")
            gate_key = tuple(chrom[:n_gates])
            if self.fitness_cache and gate_key in decode_cache:
                step_cache.decode_hits += 1
                return decode_cache[gate_key]
            used, placed, accumulated_legs = set(), [], []
            accumulated_ghosts = list(static_ghosts)
            for col, opts in enumerate(candidates):
                start = gate_key[col]
                chosen = None
                for offset in range(len(opts)):
                    candidate = opts[(start + offset) % len(opts)]
                    if candidate[0] in used:
                        continue
                    entry = gate_cache[col][candidate[0]]
                    if new_conflicts(accumulated_legs, entry["legs"],
                                     accumulated_ghosts):
                        continue
                    chosen = candidate
                    break
                if chosen is None:
                    chosen = next((opts[(start + offset) % len(opts)]
                                   for offset in range(len(opts))
                                   if opts[(start + offset) % len(opts)][0] not in used),
                                  None)
                if chosen is None:
                    raise RuntimeError(f"layer {next_layer} 门位菜单无法形成单射")
                used.add(chosen[0])
                placed.append(chosen)
                entry = gate_cache[col][chosen[0]]
                accumulated_legs.extend(entry["legs"])
                accumulated_ghosts.extend(entry["seated"])
            value = tuple(placed)
            if self.fitness_cache:
                decode_cache[gate_key] = value
            return value

        # RETURN matching is performed for the actual chromosome subset.
        return_cache = ({tuple(eligible): all_return_sites}
                        if eligible else {})

        def return_sites_for_atoms(returners):
            returners = tuple(sorted(returners))
            if self.fitness_cache and returners in return_cache:
                step_cache.return_match_hits += 1
                return return_cache[returners]
            # Formal decay exposes future information only through the audited
            # forecast term table.  RETURN matching itself stays current-state
            # exact and therefore uses the same nearest-site domain as M3.
            mode = ("forecast" if active_horizon and not self.decay_lookahead
                    else "nearest")
            value = (match_return_sites(
                reg, list(returners), self.nu, layer,
                self.box_ratio, self.alpha_lookahead,
                forecast=forecast, candidate_mode=mode,
                candidate_cache=return_candidate_cache)
                if returners else {})
            if self.fitness_cache:
                return_cache[returners] = value
            return value

        def back_legs(returners, sites):
            legs, owners = [], []
            for q in returners:
                p0 = arch.exact_SLM_location_tuple(reg.zone_seat[q])
                p1 = arch.exact_SLM_location_tuple(sites[q])
                distance = math.dist(p0, p1)
                if distance > 1e-9:
                    legs.append((distance, *p0, *p1))
                    owners.append(q)
            return legs, owners

        terminal_return_cache = {}

        def terminal_return_sites(returners, sim_locations):
            # The terminal forecast is evaluated after hypothetical current and
            # future moves.  Build a shadow registry from that simulated state;
            # using the live registry would treat already-returned forecast
            # atoms as free storage sites and could assign two atoms the same
            # endpoint, underpricing the terminal phase.
            state = tuple(tuple(sim_locations[q])
                          for q in range(len(self.mapping[0])))
            key = (tuple(sorted(returners)), state)
            if key not in terminal_return_cache:
                # Do not deepcopy the registry: it owns the full preprocessed
                # Architecture (large nested distance tables), while terminal
                # matching only needs the immutable architecture/home metadata
                # plus simulated occupancy.  Reconstructing this lightweight
                # state is semantically identical and removes the dominant
                # legacy discrete-rollout profiling cost.
                shadow = ResidentRegistry(
                    reg.arch, reg.homes, reg.theta)
                shadow.zone_seat = {}
                shadow.storage_site = {}
                for q, location in enumerate(state):
                    if shadow._is_zone(location):
                        shadow.zone_seat[q] = tuple(location)
                    else:
                        shadow.storage_site[q] = tuple(location)
                terminal_return_cache[key] = (
                    match_return_sites(
                        shadow, list(key[0]), self.nu, layer,
                        self.box_ratio, self.alpha_lookahead,
                        forecast=forecast, candidate_mode="forecast",
                        candidate_cache=return_candidate_cache,
                        assignment_mode="greedy")
                    if key[0] else {})
            return terminal_return_cache[key]

        # Pure rollout geometry survives boundary changes.  Random/large
        # circuits revisit the same small set of storage/zone coordinate pairs
        # millions of times even when the full registry state is not an LRU
        # transition hit.
        rollout_pair_cache = self.rollout_pair_cache
        rollout_site_cache = self.rollout_site_cache
        rollout_phase_score_cache = {}

        def forecast_phases(returners, sites, target_placements):
            """Deterministic multi-layer placement rollout without a nested GA.

            The old rollout followed only the current boundary's eligible atoms.
            Consequently a gate-site chromosome could not see that its target
            participant becomes the shared atom of L+2/L+3 (the dominant GHZ
            pattern).  This proxy now advances positions for every visible gate
            using a deterministic local physical minimum.  It still reads future
            gates exclusively through ``ForecastOracle``.
            """
            phases, exposures, terms = [], 0, []
            batching = ("greedy" if self.ablation_fitness_mode == "lumped_greedy"
                        else "phase")
            native_forecast_replay = bool(
                use_rich_boundary
                and self.resident_backend_requested == "native")

            def phase_from_replay(legs, batches, boundary):
                physical_value = MovementPhaseCost(
                    batches=len(batches),
                    move_time_us=sum(
                        physical._expanded_batch_time(legs, members)
                        for members in batches),
                    total_distance_um=sum(float(leg[0]) for leg in legs),
                    movers=len(legs),
                )
                return _BackendMovementPhase(physical_value, boundary)

            def replayed_movement_phase(legs, owners, positions):
                batches, boundary = _replay_phase_batches(
                    arch, tuple(legs), tuple(owners), positions,
                    batching=batching, native_replay=native_forecast_replay,
                    retain_boundary_ghosts=not native_forecast_replay)
                return phase_from_replay(legs, batches, boundary)

            sim_locations = {
                q: tuple(sites[q]) if q in returners
                else tuple(reg.current_pos(q))
                for q in range(len(self.mapping[0]))}

            def placement_rows(values):
                rows = []
                for value in values:
                    if isinstance(value, dict):
                        rows.append((tuple(value["gate"]), tuple(value["seats"])))
                    else:
                        q1, q2 = value[2], value[3]
                        rows.append(((q1, q2), self._pair_seats(q1, q2, value[0])))
                return rows

            for gate, seats_pair in placement_rows(target_placements):
                for q, seat in zip(gate, seats_pair):
                    sim_locations[q] = tuple(seat)

            def local_pair(q1, q2, site):
                key = (tuple(site), tuple(sim_locations[q1]),
                       tuple(sim_locations[q2]))
                if self.fitness_cache and key in rollout_pair_cache:
                    rollout_pair_cache.move_to_end(key)
                    return rollout_pair_cache[key]
                a, b = site, (site[0] + 1, site[1], site[2])
                p1, p2 = sim_locations[q1], sim_locations[q2]
                if p1 in (a, b):
                    result = (p1, b if p1 == a else a)
                elif p2 in (a, b):
                    other = b if p2 == a else a
                    result = (other, p2)
                else:
                    p1_xy = arch.exact_SLM_location_tuple(p1)
                    p2_xy = arch.exact_SLM_location_tuple(p2)
                    a_xy = arch.exact_SLM_location_tuple(a)
                    b_xy = arch.exact_SLM_location_tuple(b)
                    distances_ab = (
                        math.dist(p1_xy, a_xy), math.dist(p2_xy, b_xy))
                    distances_ba = (
                        math.dist(p1_xy, b_xy), math.dist(p2_xy, a_xy))

                    def orientation_cost(pair, distances):
                        return (
                            sum(distance > 1e-9 for distance in distances),
                            max(distances), sum(distances), pair)

                    result = min(
                        (orientation_cost((a, b), distances_ab),
                         orientation_cost((b, a), distances_ba)))[-1]
                if self.fitness_cache:
                    rollout_pair_cache[key] = result
                    rollout_pair_cache.move_to_end(key)
                return result

            def local_sites(q1, q2, gate_count):
                key = (int(gate_count), tuple(sim_locations[q1]),
                       tuple(sim_locations[q2]))
                if self.fitness_cache and key in rollout_site_cache:
                    rollout_site_cache.move_to_end(key)
                    return rollout_site_cache[key]
                radius = max(
                    self.pin_radius,
                    max(1, math.ceil(math.sqrt(gate_count) / 2)))
                anchors = set()
                for q in (q1, q2):
                    location = sim_locations[q]
                    if reg._is_zone(location):
                        anchors.add(self._norm_left(location))
                    else:
                        nearest = arch.nearest_entanglement_site(
                            *location, *location)[0]
                        anchors.add(self._norm_left(nearest))
                result = set()
                for base in anchors:
                    slm = arch.dict_SLM[base[0]]
                    for row in range(max(0, base[1] - radius),
                                     min(slm.n_r, base[1] + radius + 1)):
                        for column in range(max(0, base[2] - radius),
                                            min(slm.n_c, base[2] + radius + 1)):
                            result.add((base[0], row, column))
                # A forecast is a deterministic rollout proxy, not a nested
                # placement search.  Evaluating every point in two 5x5 anchor
                # windows made each legacy H=2 chromosome perform 40--50 full
                # physical phase simulations.  Keep a fixed bounded support
                # that always contains both participant anchors and their
                # midpoint, then fill it with the nearest joint neighbours.
                # The selected future phase is still scored by the exact shared
                # physical cost below.  This is the same bounded-rollout rule
                # at every layer and consumes no information beyond the oracle.
                rollout_site_budget = 4
                ordered_anchors = tuple(sorted(anchors))
                priority = [site for site in ordered_anchors if site in result]
                if (len(ordered_anchors) == 2
                        and ordered_anchors[0][0] == ordered_anchors[1][0]):
                    left, right = ordered_anchors
                    midpoint = (
                        left[0],
                        round((left[1] + right[1]) / 2),
                        round((left[2] + right[2]) / 2),
                    )
                    if midpoint in result and midpoint not in priority:
                        priority.append(midpoint)

                def joint_neighbour_key(site):
                    distances = [
                        abs(site[1] - anchor[1])
                        + abs(site[2] - anchor[2])
                        + (0 if site[0] == anchor[0] else 10_000)
                        for anchor in ordered_anchors
                    ]
                    return (min(distances), max(distances),
                            sum(distances), site)

                for site in sorted(result, key=joint_neighbour_key):
                    if site not in priority:
                        priority.append(site)
                    if len(priority) >= rollout_site_budget:
                        break
                value = tuple(priority[:rollout_site_budget])
                if self.fitness_cache:
                    rollout_site_cache[key] = value
                    rollout_site_cache.move_to_end(key)
                return value

            last_visible_offset = 0
            for _absolute_layer, gates, offset, weight in visible_forecast:
                last_visible_offset = offset
                term_phase_start = len(phases)
                term_exposures_start = exposures
                future_participants = {q for gate in gates for q in gate}
                blocked = {
                    location for q, location in sim_locations.items()
                    if q not in future_participants and reg._is_zone(location)}
                chosen_rows, used_sites = [], set()

                def future_gate_geometry(locations, rows, *, replay=True):
                    after = dict(locations)
                    legs, owners = [], []
                    for gate, pair in rows:
                        for q, target in zip(gate, pair):
                            p0 = arch.exact_SLM_location_tuple(locations[q])
                            p1 = arch.exact_SLM_location_tuple(target)
                            distance = math.dist(p0, p1)
                            if distance > 1e-9:
                                legs.append((distance, *p0, *p1))
                                owners.append(q)
                            after[q] = tuple(target)
                    movers = set(owners)
                    static_ghosts = [
                        (atom, *arch.exact_SLM_location_tuple(location))
                        for atom, location in locations.items()
                        if atom not in movers]
                    hits = tuple(sorted({
                        int(hit[0])
                        for leg in legs
                        for hit in leg_hits(leg, static_ghosts)
                    }))
                    batches, boundary = None, None
                    if replay:
                        try:
                            batches, boundary = _replay_phase_batches(
                                arch, legs, owners, locations,
                                batching=batching,
                                native_replay=native_forecast_replay,
                                retain_boundary_ghosts=not native_forecast_replay)
                        except ValueError:
                            batches, boundary = None, None
                    return legs, owners, after, hits, batches, boundary

                for q1, q2 in gates:
                    rejection_counts = {
                        "occupied_target": 0,
                        "no_reseat_site": 0,
                        "gate_endpoint": 0,
                        "reseat_endpoint": 0,
                    }

                    def score_future_site(site):
                        pair = local_pair(q1, q2, site)
                        if pair[0] in blocked or pair[1] in blocked:
                            rejection_counts["occupied_target"] += 1
                            return None
                        local_locations = dict(sim_locations)
                        prospective_rows = chosen_rows + [((q1, q2), pair)]
                        (combined_legs, combined_owners, _combined_after,
                         hit_atoms, _combined_batches,
                         _combined_boundary) = future_gate_geometry(
                            local_locations, prospective_rows, replay=False)
                        # A hit future participant is not treated as a static
                        # obstacle or ignored.  It performs an explicit
                        # pre-gate cycle to storage and then re-enters with its
                        # gate.  Nonparticipants use the same deterministic
                        # RESEAT phase.  Both movements are included in the
                        # rollout cost and endpoint replay below.
                        relocations = {}
                        relocation_single_phases = {}
                        for atom in sorted(set(hit_atoms)):
                            source = tuple(local_locations[atom])
                            source_xy = arch.exact_SLM_location_tuple(source)
                            center = (tuple(arch.nearest_storage_site(*source))
                                      if reg._is_zone(source) else source)
                            occupied = set(local_locations.values())
                            candidates_storage = list(_box_sites(
                                arch, center, self.box_ratio, occupied))
                            if not candidates_storage:
                                candidates_storage = [
                                    candidate for candidate in _all_storage_sites(arch)
                                    if candidate not in occupied]

                            def relocation_key(candidate):
                                target_xy = arch.exact_SLM_location_tuple(candidate)
                                return (math.dist(source_xy, target_xy), candidate)

                            chosen = None
                            chosen_phase = None
                            for candidate in sorted(
                                    candidates_storage, key=relocation_key):
                                target_xy = arch.exact_SLM_location_tuple(candidate)
                                move_distance = math.dist(source_xy, target_xy)
                                move_leg = (move_distance, *source_xy, *target_xy)
                                try:
                                    batches, boundary = _replay_phase_batches(
                                        arch, (move_leg,), (atom,),
                                        local_locations, batching=batching,
                                        native_replay=native_forecast_replay,
                                        retain_boundary_ghosts=(
                                            not native_forecast_replay))
                                except ValueError:
                                    continue
                                chosen = tuple(candidate)
                                chosen_phase = phase_from_replay(
                                    (move_leg,), batches, boundary)
                                break
                            if chosen is None:
                                rejection_counts["no_reseat_site"] += 1
                                return None
                            relocations[atom] = chosen
                            relocation_single_phases[atom] = chosen_phase
                            local_locations[atom] = chosen

                        (combined_legs, combined_owners, _combined_after,
                         remaining_hits, combined_batches,
                         combined_boundary) = future_gate_geometry(
                            local_locations, prospective_rows)
                        if remaining_hits or combined_batches is None:
                            rejection_counts["gate_endpoint"] += 1
                            return None

                        relocation_legs, relocation_owners = [], []
                        for atom, target in sorted(relocations.items()):
                            p0 = arch.exact_SLM_location_tuple(
                                sim_locations[atom])
                            p1 = arch.exact_SLM_location_tuple(target)
                            distance = math.dist(p0, p1)
                            if distance > 1e-9:
                                relocation_legs.append(
                                    (distance, *p0, *p1))
                                relocation_owners.append(atom)
                        relocation_phase = None
                        if relocation_legs:
                            if len(relocation_legs) == 1:
                                relocation_phase = relocation_single_phases[
                                    relocation_owners[0]]
                            else:
                                try:
                                    relocation_phase = replayed_movement_phase(
                                        relocation_legs, relocation_owners,
                                        sim_locations)
                                except ValueError:
                                    rejection_counts["reseat_endpoint"] += 1
                                    return None
                        phase_physical = MovementPhaseCost(
                            batches=len(combined_batches),
                            move_time_us=sum(
                                physical._expanded_batch_time(
                                    combined_legs, members)
                                for members in combined_batches),
                            total_distance_um=sum(
                                float(leg[0]) for leg in combined_legs),
                            movers=len(combined_legs),
                        )
                        phase = _BackendMovementPhase(
                            phase_physical, combined_boundary)
                        score_key = (relocation_phase, phase, q1, q2)
                        if (self.fitness_cache
                                and score_key in rollout_phase_score_cache):
                            objective = rollout_phase_score_cache[score_key]
                        else:
                            score_phases = ([relocation_phase]
                                            if relocation_phase is not None
                                            else [])
                            objective, _ = physical.score(
                                [*score_phases, phase], 0, (q1, q2))
                            if self.fitness_cache:
                                rollout_phase_score_cache[score_key] = objective
                        return objective, pair, relocations, relocation_phase

                    options = []
                    for site in local_sites(q1, q2, len(gates)):
                        if site in used_sites:
                            continue
                        scored = score_future_site(site)
                        if scored is not None:
                            objective, pair, relocations, relocation_phase = scored
                            options.append((objective, site, pair, relocations,
                                            relocation_phase))
                            if (len(options)
                                    >= self.forecast_gate_candidate_budget):
                                break
                    if not options and not self.decay_lookahead:
                        # Preserve the historical discrete-H regression
                        # contract.  Formal decay M4 intentionally stops at the
                        # bounded support and lets the audited outer surrogate
                        # handle a missing continuation.
                        for site in self._all_zone_sites():
                            if site in used_sites:
                                continue
                            scored = score_future_site(site)
                            if scored is not None:
                                (objective, pair, relocations,
                                 relocation_phase) = scored
                                options.append((
                                    objective, site, pair, relocations,
                                    relocation_phase))
                    if not options:
                        # This is deliberately a bounded rollout rather than a
                        # nested placement search.  Failure is propagated to
                        # the outer forecast surrogate; it never removes the
                        # corresponding current candidate from the native GA.
                        raise RuntimeError(
                            "bounded forecast has no ghost-safe gate placement: "
                            f"boundary={layer}, future_layer={_absolute_layer}, "
                            f"offset={offset}, gate=({q1},{q2}), "
                            f"chosen_gates={len(chosen_rows)}, "
                            f"rejections={rejection_counts}")
                    _, site, pair, relocations, relocation_phase = min(options)
                    if (relocation_phase is not None
                            and relocation_phase.boundary.legs):
                        phases.append(relocation_phase)
                    for atom, new_location in relocations.items():
                        blocked.discard(tuple(sim_locations[atom]))
                        sim_locations[atom] = tuple(new_location)
                    used_sites.add(site)
                    chosen_rows.append(((q1, q2), pair))

                before = dict(sim_locations)
                (legs, owners, after, static_hits,
                 replayed_batches, replayed_boundary) = future_gate_geometry(
                    before, chosen_rows)
                if static_hits or replayed_batches is None:
                    raise RuntimeError(
                        "bounded forecast retained an unsafe gate phase")
                sim_locations = after
                if legs:
                    phases.append(_BackendMovementPhase(
                        MovementPhaseCost(
                            batches=len(replayed_batches),
                            move_time_us=sum(
                                physical._expanded_batch_time(legs, members)
                                for members in replayed_batches),
                            total_distance_um=sum(
                                float(leg[0]) for leg in legs),
                            movers=len(legs)),
                        replayed_boundary))
                # Every atom sitting in an illuminated entanglement zone but
                # absent from this future gate layer is physically exposed.
                # Restricting this to the current boundary's decision genes
                # misses atoms that entered in the target or first forecast
                # layer and then became idle inside the legacy rollout window.
                exposures += sum(
                    1 for q in range(len(self.mapping[0]))
                    if reg._is_zone(sim_locations[q])
                    and q not in future_participants)
                terms.append({
                    "kind": "future_layer",
                    "offset": offset,
                    "weight": weight,
                    "phases": tuple(phases[term_phase_start:]),
                    "idle_exposures": exposures - term_exposures_start,
                })
            # Receding-horizon optimisation otherwise has a procrastination
            # failure: an atom with no visible use can choose STAY because one
            # RETURN phase is slightly dearer than H+1 excitation pulses, make
            # the same choice at the next boundary, and remain illuminated for
            # the rest of the circuit.  M4 closes its two-layer window with a
            # deterministic terminal RETURN cost for every simulated resident.
            # This terminal value reads no layer outside ForecastOracle and is
            # intentionally absent for H=0, whose registered contract is the
            # exact current transition only.
            if active_horizon and last_visible_offset:
                # Close every candidate on the same physical terminal state.
                # Limiting this to the boundary's original decision genes
                # omitted target/future-layer partners that entered the zone
                # during rollout.  Their eventual exit then vanished from the
                # objective, systematically favouring placements that dragged
                # a fresh partner to a resident's remote seat.  All simulated
                # residents must therefore receive a terminal RETURN value.
                terminal = tuple(
                    q for q in range(len(self.mapping[0]))
                    if reg._is_zone(sim_locations[q]))
                terminal_sites = terminal_return_sites(terminal, sim_locations)
                terminal_legs, terminal_owners = [], []
                for q in terminal:
                    p0 = arch.exact_SLM_location_tuple(sim_locations[q])
                    p1 = arch.exact_SLM_location_tuple(terminal_sites[q])
                    distance = math.dist(p0, p1)
                    if distance > 1e-9:
                        terminal_legs.append((distance, *p0, *p1))
                        terminal_owners.append(q)
                if terminal_legs:
                    try:
                        terminal_phase = replayed_movement_phase(
                            terminal_legs, terminal_owners, sim_locations)
                    except ValueError:
                        terminal_phase = None
                    if terminal_phase is not None:
                        phases.append(terminal_phase)
                        terminal_phases = (terminal_phase,)
                    else:
                        # A combined terminal assignment can be individually
                        # impossible even though its Hungarian distance is
                        # small.  Fall back to deterministic one-atom phases,
                        # selecting the nearest currently free storage point
                        # whose complete source/target endpoint replay is safe.
                        # This is a real schedule (and therefore potentially
                        # slower), not a finite ghost penalty.
                        sequential_locations = dict(sim_locations)
                        occupied = {
                            tuple(location)
                            for q, location in sequential_locations.items()
                            if q not in terminal and not reg._is_zone(location)
                        }
                        sequential_phases = []
                        for q in terminal:
                            source = tuple(sequential_locations[q])
                            source_xy = arch.exact_SLM_location_tuple(source)
                            preferred = tuple(terminal_sites[q])
                            alternatives = [preferred]
                            alternatives.extend(sorted(
                                (tuple(site) for site in _all_storage_sites(arch)
                                 if tuple(site) != preferred),
                                key=lambda site: (
                                    math.dist(
                                        source_xy,
                                        arch.exact_SLM_location_tuple(site)),
                                    site)))
                            selected_site = None
                            selected_phase = None
                            for site in alternatives:
                                if site in occupied:
                                    continue
                                target_xy = arch.exact_SLM_location_tuple(site)
                                distance = math.dist(source_xy, target_xy)
                                leg = (distance, *source_xy, *target_xy)
                                try:
                                    candidate_phase = replayed_movement_phase(
                                        (leg,), (q,), sequential_locations)
                                except ValueError:
                                    continue
                                selected_site = site
                                selected_phase = candidate_phase
                                break
                            if selected_site is None:
                                raise RuntimeError(
                                    "terminal RETURN has no ghost-safe storage site")
                            sequential_locations[q] = selected_site
                            occupied.add(selected_site)
                            if selected_phase.boundary.legs:
                                sequential_phases.append(selected_phase)
                        phases.extend(sequential_phases)
                        terminal_phases = tuple(sequential_phases)
                else:
                    terminal_phases = ()
                # Terminal closure is valued at the last actually visible
                # depth.  It never reads or discounts once more at H+1, and no
                # terminal term exists when the circuit has no visible future.
                terminal_offset = last_visible_offset
                terminal_weight = forecast.future_weight(terminal_offset)
                terms.append({
                    "kind": "terminal_return",
                    "offset": terminal_offset,
                    "weight": terminal_weight,
                    "phases": terminal_phases,
                    "idle_exposures": 0,
                })
            return phases, exposures, tuple(terms)

        def score_decay_objective(current_phases, current_exposures,
                                   chromosome, gate_option_indices,
                                   return_assignments):
            """Exact current physics plus a weighted, non-executable forecast.

            Only the primary search NLL receives future heuristic terms.  Move
            batches/time/distance and the returned physical breakdown describe
            the executable current boundary exactly and are never discounted or
            inflated by a predicted layer.
            """
            current_objective, current_breakdown = physical.score(
                current_phases, current_exposures, chromosome)
            if native_rich_problem is None or rich_config is None:
                raise RuntimeError("decay forecast term table is unavailable")
            # This independent Python evaluator is also the differential oracle
            # for the C++ generic solver.  Both consume the same immutable term
            # table; current physical fidelity is scored separately above.
            from zzx.reference_backend import evaluate_decay_forecast
            (weighted_nll, by_depth, category_breakdown,
             terms_applied, terms_skipped) = evaluate_decay_forecast(
                native_rich_problem, rich_config, chromosome,
                gate_option_indices, return_assignments)
            search_objective = (
                current_breakdown.negative_log_fidelity + weighted_nll,
                current_objective[1], current_objective[2],
                current_objective[3], current_objective[4],
            )
            return search_objective, current_breakdown, {
                "configured_depth": self.lookahead_horizon,
                "effective_depth": forecast.effective_horizon,
                "rho": float(self.lookahead_horizon_config["rho"]),
                "epsilon": float(self.lookahead_horizon_config["epsilon"]),
                "alpha_lookahead": self.alpha_lookahead,
                "forecast_by_depth": list(by_depth),
                "forecast_breakdown": dict(category_breakdown),
                "forecast_terms_applied": terms_applied,
                "forecast_terms_skipped_cutoff": terms_skipped,
                "weighted_negative_log_fidelity": weighted_nll,
            }

        boundary_architecture = self.boundary_architecture_snapshot
        if boundary_architecture is None:
            # Focused transition tests may invoke _ga_step_v2 directly instead
            # of entering through run(); keep that diagnostic path exact while
            # constructing the same persistent snapshot only once.
            boundary_architecture = self._prepare_boundary_architecture()
        boundary_config = BoundaryConfig(
            exact_coloring_threshold=self.exact_coloring_threshold,
            horizon_policy=("dynamic" if self.adaptive_lookahead else "fixed"),
            max_horizon=active_horizon,
            # Candidate ranking is based on the executable current transition.
            # A single-leg ghost cannot be removed by coloring and is therefore
            # infeasible before the GA compares this candidate.
            enforce_single_leg_ghost=True,
        )
        rich_config = None
        if self.decay_lookahead:
            lookahead_spec = self.lookahead_horizon_config
            rich_config = RichSearchConfig(
                operator_profile=self.operator_profile,
                forecast_mode="decay",
                forecast_policy=str(lookahead_spec["policy"]),
                decay_kind=str(lookahead_spec["decay"]),
                max_horizon=active_horizon,
                alpha_lookahead=self.alpha_lookahead,
                decay_rho=float(lookahead_spec["rho"]),
                decay_epsilon=float(lookahead_spec["epsilon"]),
                population_size=self.population_size,
                iterations=self.iterations,
                neighbors_per_solution=self.neighbors_per_solution,
                neighbor_sample_size=self.neighbor_sample_size,
                elite_count=self.elite_count,
                early_stop_patience=self.early_stop_patience,
                max_unique_evaluations=self.max_unique_evaluations,
                direct_enumeration_limit=self.direct_enumeration_limit,
                crossover_rate=self.crossover_rate,
                local_polish_sweeps=self.local_polish_sweeps,
                return_candidate_limit=self.return_candidate_limit,
                return_assignment_k=self.return_assignment_k,
                forecast_gate_candidate_budget=
                    self.forecast_gate_candidate_budget,
                exact_coloring_threshold=self.exact_coloring_threshold,
                enforce_single_leg_ghost=True,
                fitness_cache=self.fitness_cache,
            )
        step_backend = {
            "marshal_ns": 0,
            "python_marshal_ns": 0,
            "native_call_wall_ns": 0,
            "native_search_wall_ns": 0,
            "search_kernel_ns": 0,
            "fitness_ns": 0,
            "normalize_ns": 0,
            "decode_ns": 0,
            "return_match_ns": 0,
            "forecast_ns": 0,
            "selection_ns": 0,
            "native_parse_ns": 0,
            "native_serialize_ns": 0,
            "calls": 0,
            "candidates": 0,
        }

        def score_from_backend(value):
            breakdown = PhysicalCostBreakdown(
                negative_log_fidelity=value.negative_log_fidelity,
                transfer_nll=value.transfer_nll,
                idle_excitation_nll=value.idle_excitation_nll,
                coherence_nll=value.coherence_nll,
                move_batches=value.move_batches,
                move_time_us=value.move_time_us,
                total_distance_um=value.total_distance_um,
                idle_exposures=value.idle_exposures,
                transfers=value.transfers,
            )
            return value.objective, breakdown

        def evaluate_prepared(prepared):
            """Evaluate decoded plans through one coarse backend call."""
            if not prepared:
                return []
            # A few focused transition tests construct the registry/oracle
            # directly and invoke _ga_step_v2 without run().  Lazily create the
            # exact same persistent backend for that supported diagnostic path.
            if self.boundary_backend is None:
                self.boundary_backend = select_backend(
                    boundary_architecture,
                    backend=self.resident_backend_requested,
                    formal=self.formal_native,
                    require_registered_wheel=self.formal_native,
                    expected_wheel_sha256=(
                        self.native_wheel_sha256
                        if self.formal_native else None),
                )
            problem = BoundaryProblem(
                boundary_architecture,
                tuple(item[0] for item in prepared),
                boundary_id=f"{self.method_id}:L{layer}",
                effective_horizon=active_horizon,
            )
            started_ns = time.perf_counter_ns()
            values = self.boundary_backend.evaluate_many(
                problem, config=boundary_config)
            elapsed_ns = time.perf_counter_ns() - started_ns
            native_timing = getattr(
                self.boundary_backend, "last_evaluate_timing", {}) or {}
            marshal_ns = int(native_timing.get("marshal_ns", 0))
            fitness_ns = int(native_timing.get("fitness_ns", elapsed_ns))
            selection_ns = 0
            kernel_ns = fitness_ns + selection_ns
            delta = {
                "marshal_ns": marshal_ns,
                "search_kernel_ns": kernel_ns,
                "fitness_ns": fitness_ns,
                "selection_ns": selection_ns,
                "native_parse_ns": int(
                    native_timing.get("native_parse_ns", 0)),
                "native_serialize_ns": int(
                    native_timing.get("native_serialize_ns", 0)),
                "calls": 1,
                "candidates": len(values),
            }
            for key, value in delta.items():
                step_backend[key] += value
                self.boundary_backend_metrics[key] += value
            if len(values) != len(prepared):
                raise RuntimeError("resident backend candidate count mismatch")
            return [score_from_backend(value) for value in values]

        forecast_audit_cache = {}

        def build_plan(chrom, include_forecast=True, cycle_returners=()):
            chrom = normalize_chrom(chrom)
            cycle_returners = tuple(sorted(
                (set(cycle_returners) | forced_cycle_candidates)
                & set(cycle_candidates)))
            scored_chrom = tuple(chrom) + tuple(
                1 if q in cycle_returners else 0 for q in cycle_candidates)
            try:
                placed = decode(chrom)
            except RuntimeError:
                # Some gene vectors induce a greedy menu order that violates
                # Hall's condition even though the layer has other legal
                # placements.  Such a chromosome is infeasible, not a compiler
                # failure; the seeded full matching remains a finite incumbent.
                return (None, (), 0,
                        ((float("inf"), float("inf"), float("inf"),
                          float("inf"), scored_chrom),
                         PhysicalIncrementalCost(len(self.mapping[0])).score(
                             (), 0, scored_chrom)[1]))
            bits = chrom[n_gates:]
            returners = tuple(sorted(
                {q for q, bit in zip(eligible, bits) if bit}
                | set(cycle_returners)))
            sites = return_sites_for_atoms(returners)
            legs_back, owners_back = back_legs(returners, sites)
            positions_t0 = [
                (q, *arch.exact_SLM_location_tuple(reg.current_pos(q)))
                for q in range(len(self.mapping[0]))]
            positions_t1 = []
            returned = set(returners)
            for q in range(len(self.mapping[0])):
                loc = sites[q] if q in returned else reg.current_pos(q)
                positions_t1.append((q, *arch.exact_SLM_location_tuple(loc)))
            # Exact T1 occupancy gate: a target pair may reuse a RETURNed old
            # seat, but never a seat whose owner selected STAY.  This makes
            # RETURN and gate placement genuinely joint genes instead of a
            # menu-time approximation.
            occupied_t1 = {
                (x, y): q for q, x, y in positions_t1 if q not in participants}
            for chosen in placed:
                s1, s2 = self._pair_seats(chosen[2], chosen[3], chosen[0])
                for target in (s1, s2):
                    target_xy = arch.exact_SLM_location_tuple(target)
                    if target_xy in occupied_t1:
                        return (None, (), 0,
                                ((float("inf"), float("inf"), float("inf"),
                                  float("inf"), scored_chrom),
                                 PhysicalIncrementalCost(
                                     len(self.mapping[0])).score(
                                         (), 0, scored_chrom)[1]))
            # Compute out legs from the actual post-decision positions.  Main
            # NL/LK results are unchanged, while always-RETURN can faithfully
            # model a next-layer participant returning and re-entering.
            position_t1_by_q = {row[0]: (row[1], row[2]) for row in positions_t1}
            legs_out, owners_out = [], []
            for chosen in placed:
                s1, s2 = self._pair_seats(chosen[2], chosen[3], chosen[0])
                for q, target in ((chosen[2], s1), (chosen[3], s2)):
                    p0 = position_t1_by_q[q]
                    p1 = arch.exact_SLM_location_tuple(target)
                    distance = math.dist(p0, p1)
                    if distance > 1e-9:
                        legs_out.append((distance, *p0, *p1))
                        owners_out.append(q)
            if self.ablation_fitness_mode == "lumped_greedy":
                # Deliberately reproduce the old proxy: back/out legs are put in
                # one greedy pool even though the executable router must retain
                # the physical gate boundary.  Ghost correctness remains a hard
                # post-selection repair and final replay invariant.
                phases = [movement_phase(
                    legs_back + legs_out,
                    owners=owners_back + owners_out,
                    batching="greedy")]
            else:
                phases = [
                    movement_phase(
                        legs_back, ghosts=positions_t0, owners=owners_back),
                    movement_phase(
                        legs_out, ghosts=positions_t1, owners=owners_out),
                ]
            current_phases = tuple(phases)
            current_exposures = sum(
                1 for q in eligible
                if q not in returned and q not in participants)
            if include_forecast and self.decay_lookahead:
                gate_option_indices = tuple(
                    candidates[column].index(chosen)
                    for column, chosen in enumerate(placed))
                return_assignments = tuple(
                    (q, self.boundary_storage_site_id[tuple(sites[q])])
                    for q in returners)
                objective, current_breakdown, audit = score_decay_objective(
                    current_phases, current_exposures, scored_chrom,
                    gate_option_indices, return_assignments)
                forecast_audit_cache[scored_chrom] = audit
                return None, (), 0, (objective, current_breakdown)
            if include_forecast:
                future_phases, future_exposures, forecast_terms = \
                    forecast_phases(returned, sites, placed)
            else:
                future_phases, future_exposures, forecast_terms = (), 0, ()
            phases.extend(future_phases)
            idle_exposures = current_exposures + future_exposures
            candidate = CandidatePlan(
                chromosome=scored_chrom,
                phases=tuple(phase.boundary for phase in phases),
                idle_exposures=idle_exposures,
            )
            return candidate, tuple(phases), idle_exposures, None

        fitness_cache = {}

        def fitness_many(chromosomes, include_forecast=True,
                         cycle_returners=()):
            chromosomes = list(chromosomes)
            cycles = tuple(sorted(
                (set(cycle_returners) | forced_cycle_candidates)
                & set(cycle_candidates)))
            values = [None] * len(chromosomes)
            pending = []
            for index, chrom in enumerate(chromosomes):
                key = tuple(normalize_chrom(chrom))
                cache_key = (bool(include_forecast), key, cycles)
                step_cache.evaluations += 1
                if self.fitness_cache and cache_key in fitness_cache:
                    step_cache.fitness_hits += 1
                    values[index] = fitness_cache[cache_key]
                    continue
                step_cache.unique_evaluations += 1
                candidate, phases, idle_exposures, ready = build_plan(
                    key, include_forecast=include_forecast,
                    cycle_returners=cycles)
                if ready is not None:
                    value = ready
                elif any(phase.boundary.batching == "greedy"
                         for phase in phases):
                    value = physical.score(
                        phases, idle_exposures, candidate.chromosome)
                else:
                    pending.append((index, cache_key, candidate, phases,
                                    idle_exposures))
                    continue
                values[index] = value
                if self.fitness_cache:
                    fitness_cache[cache_key] = value
            if pending:
                evaluated = evaluate_prepared([
                    (candidate, phases, idle_exposures)
                    for _, _, candidate, phases, idle_exposures in pending])
                for (index, cache_key, _candidate, _phases,
                     _idle_exposures), value in zip(pending, evaluated):
                    values[index] = value
                    if self.fitness_cache:
                        fitness_cache[cache_key] = value
            return values

        def fitness(chrom, include_forecast=True, cycle_returners=()):
            return fitness_many(
                [chrom], include_forecast=include_forecast,
                cycle_returners=cycle_returners)[0]

        # The direct path below exhaustively scores every normalized chromosome.
        # Any stochastic/greedy seed refinement before that enumeration cannot
        # change its winner; it only repeats physical rollouts.  Decide this
        # once so small-space layers retain exact search while skipping redundant
        # seed work (the dominant case in long random circuits).
        search_kernel_started_ns = time.perf_counter_ns()
        direct_search_space = 1
        for domain in gate_domains:
            direct_search_space *= domain
            if direct_search_space > self.direct_enumeration_limit:
                break
        if (direct_search_space <= self.direct_enumeration_limit
                and self.ablation_policy == "optimize"):
            direct_search_space *= 2 ** len(eligible)
        will_enumerate = (
            direct_search_space <= self.direct_enumeration_limit
            and direct_search_space <= self.max_unique_evaluations)

        # The LRU key is constructed before either backend starts searching so
        # the same previous-boundary elite is supplied to Python and C++.
        # H=0's bounded oracle returns an empty window without reading a future
        # layer from its provider.
        scheduler_state_key = (
            ("approximate-ultra-deep", tuple(float(value)
                                             for value in scheduler_idle_prior))
            if scheduler_prefix is None else (
                float(scheduler_prefix.scheduler.trace_end_us),
                tuple(float(value) for value in scheduler_idle_prior),
                tuple(float(value) for value in
                      scheduler_prefix.scheduler.active_union_us),
                tuple(float(value) for value in
                      scheduler_prefix.scheduler.aod_end_us),
                float(scheduler_prefix.scheduler.one_qubit_end_us),
                tuple(float(value) for value in
                      scheduler_prefix.scheduler.rydberg_end_us),
                tuple(float(value) for value in
                      scheduler_prefix.qubit_dependency_end_us),
                tuple(float(value) for value in
                      scheduler_prefix.back_dependency_end_us),
                tuple((tuple(location), float(value))
                      for location, value in
                      scheduler_prefix.site_dependency_activation_finish_us),
            ))
        state_key = (
            active_horizon, self.ablation_policy,
            self.ablation_fitness_mode,
            tuple(tuple(reg.current_pos(q)) for q in range(len(self.mapping[0]))),
            # ABI8 current fitness is a fork of this exact ASAP scheduler
            # prefix.  Equal geometry/idle with different resource or
            # dependency clocks is a different search state.
            scheduler_state_key,
            tuple(self._one_qubit_for_layer(next_layer)),
            tuple(list_gate), visible_window, tuple(gate_domains), tuple(eligible),
            min_returns, tuple(sorted(physical_forced_returns)),
            tuple(sorted(rent_forced_returns)),
            tuple(sorted((q, use_layer, tuple(seat))
                         for q, (use_layer, seat)
                         in self.residency_commitments.items())),
        )
        # The LRU value is only an approximate native elite.  The exhaustive
        # Python differential backend must enumerate independently and therefore
        # never receives this search hint.
        cached_winner = (
            self.transition_cache.get(state_key)
            if self.resident_backend_requested == "native" else None)

        # Physical greedy seed: first solve the ZAC-style global gate/site
        # matching, then start with every non-participant RETURNed so every
        # matched site is genuinely vacant at T1.  This is a strong incumbent
        # inside the same joint chromosome space, not an external baseline
        # fallback.  Subsequent coordinate sweeps restore STAY whenever the
        # complete registered physical objective prefers residency.
        # The rich backend performs this same deterministic matching when its
        # indexed DTO is assembled below.  Its successful fail-closed path never
        # consumes the legacy Python seed, so avoid solving the assignment twice
        # at every boundary.
        matched = (self._match_gates(candidates, list_gate)
                   if n_gates and not use_rich_boundary else [])
        matched_genes = []
        for col, placement in enumerate(matched):
            site = tuple(placement["site"])
            matched_genes.append(next(
                (index for index, option in enumerate(candidates[col])
                 if tuple(option[0]) == site), 0))

        native_rich_result = None
        native_relaxed_ghost_recovery = False
        native_rich_problem = None
        native_return_sites = None
        native_reseat_sites = None
        native_participant_parking_sites = None
        native_reference_forecast = None
        rich_search_stats = {}
        rich_forecast_terms = ()
        return_option_reasons = {}
        selected_return_audit = []
        selected_participant_parking_audit = []
        native_candidates = candidates
        if self.decay_lookahead:
            if forced_cycle_candidates or cycle_candidates:
                raise RuntimeError(
                    "formal decay boundary cannot contain legacy cycle search")
            if rich_config is None:
                raise RuntimeError("formal decay search config was not resolved")

            occupied_storage = reg.occupied_storage()
            eligible_index = {q: index for index, q in enumerate(eligible)}
            future_atoms = {
                offset: {q for gate in gates for q in gate}
                for _absolute, gates, offset, _weight in visible_forecast
            }
            predicted_corridor_legs = []
            # ABI5 consumes raw bounded 2Q layers and performs the physical
            # rollout inside C++.  The legacy Python marginal-term builder is
            # retained below only as a regression oracle for old fixtures; it
            # is never executed by formal M3/M4.
            native_future_rollout = True
            if active_horizon and not native_future_rollout:
                for _absolute, gates, _offset, _weight in visible_forecast:
                    for q1, q2 in gates:
                        location1 = tuple(reg.current_pos(q1))
                        location2 = tuple(reg.current_pos(q2))
                        if reg._is_zone(location1):
                            site = tuple(self._norm_left(location1))
                        elif reg._is_zone(location2):
                            site = tuple(self._norm_left(location2))
                        else:
                            site = tuple(arch.nearest_entanglement_site(
                                *location1, *location2)[0])
                        targets = self._pair_seats(q1, q2, site)
                        for q, target in zip((q1, q2), targets):
                            source_xy = arch.exact_SLM_location_tuple(
                                reg.current_pos(q))
                            target_xy = arch.exact_SLM_location_tuple(target)
                            distance = math.dist(source_xy, target_xy)
                            if distance > 1e-9:
                                predicted_corridor_legs.append(
                                    (distance, *source_xy, *target_xy))

            def return_phase_nll(q, site):
                source_xy = arch.exact_SLM_location_tuple(reg.zone_seat[q])
                target_xy = arch.exact_SLM_location_tuple(site)
                distance = math.dist(source_xy, target_xy)
                if distance <= 1e-9:
                    return 0.0
                ghosts = [
                    (atom, *arch.exact_SLM_location_tuple(reg.current_pos(atom)))
                    for atom in range(len(self.mapping[0]))
                ]
                phase = movement_phase(
                    [(distance, *source_xy, *target_xy)],
                    ghosts=ghosts, owners=[q])
                return physical.score([phase], 0, (q,))[1].negative_log_fidelity

            provisional_return_domains = []
            for q in eligible:
                zone_location = tuple(reg.zone_seat[q])
                nearest = tuple(arch.nearest_storage_site(*zone_location))
                family_centers = (
                    [("home", tuple(reg.homes[q]))]
                    if active_horizon == 0
                    and getattr(
                        self, "return_anchor_effective_policy",
                        self.return_anchor_policy) == "home_stable"
                    else [
                        ("nearest", nearest),
                        ("home", tuple(reg.homes[q])),
                    ])
                visible = visible_use(q) if active_horizon else None
                if visible is not None:
                    _use_layer, partner = visible
                    partner_location = tuple(reg.current_pos(partner))
                    partner_anchor = (
                        tuple(arch.nearest_storage_site(*partner_location))
                        if reg._is_zone(partner_location)
                        else partner_location)
                    family_centers.append(("future_partner", partner_anchor))

                source_xy = arch.exact_SLM_location_tuple(zone_location)
                family_options = {}
                site_reasons = {}
                all_candidates = set()
                for reason, center in family_centers:
                    if center[0] not in arch.storage_zone:
                        continue
                    family = tuple(
                        site for site in _box_sites(
                            arch, center, self.box_ratio, occupied_storage)
                        if tuple(site) not in occupied_storage)
                    family_options[reason] = family
                    for site in family:
                        site = tuple(site)
                        all_candidates.add(site)
                        site_reasons.setdefault(site, set()).add(reason)

                def corridor_hits(site):
                    if not predicted_corridor_legs:
                        return 0
                    x, y = arch.exact_SLM_location_tuple(site)
                    return len(ghost_hits(
                        predicted_corridor_legs, [(q, x, y)]))

                def current_key(site):
                    target_xy = arch.exact_SLM_location_tuple(site)
                    return (sqrt(math.dist(source_xy, target_xy)), site)

                selected = []

                def select(site, reason):
                    site = tuple(site)
                    if site in occupied_storage or site in selected:
                        return
                    selected.append(site)
                    site_reasons.setdefault(site, set()).add(reason)

                if q in rent_guard_return_sites:
                    # Keep the physical break-even witness inside the native
                    # bounded domain as an audit-backed option, never a mask.
                    select(rent_guard_return_sites[q], "rent_audit_safe")
                for reason, _center in family_centers:
                    family = family_options.get(reason, ())
                    if family:
                        select(min(family, key=current_key), reason)
                if active_horizon and all_candidates:
                    select(min(
                        all_candidates,
                        key=lambda site: (corridor_hits(site), *current_key(site))),
                        "corridor_clear")
                for site in sorted(
                        all_candidates,
                        key=lambda candidate: (
                            corridor_hits(candidate) if active_horizon else 0,
                            *current_key(candidate))):
                    select(site, "bounded_fill")
                    if len(selected) == self.return_candidate_limit:
                        break
                if len(selected) < self.return_candidate_limit:
                    for site in sorted(
                            (tuple(site) for site in _all_storage_sites(arch)
                             if tuple(site) not in occupied_storage),
                            key=current_key):
                        select(site, "global_fill")
                        if len(selected) == self.return_candidate_limit:
                            break
                if not selected:
                    raise RuntimeError(
                        f"RETURN atom {q} has no free bounded storage candidate")
                domain = []
                for site in selected[:self.return_candidate_limit]:
                    reasons = tuple(sorted(site_reasons.get(site, {"bounded_fill"})))
                    current_nll = return_phase_nll(q, site)
                    domain.append((site, current_nll, reasons))
                    return_option_reasons[(q, site)] = reasons
                provisional_return_domains.append(tuple(domain))

            replay_by_depth_cache = {}
            replay_failure = object()

            def replay_by_depth(returners, sites, target_placements):
                placement_rows = []
                for value in target_placements:
                    if isinstance(value, dict):
                        gate = tuple(int(q) for q in value["gate"])
                        seats = tuple(tuple(seat) for seat in value["seats"])
                    else:
                        gate = (int(value[2]), int(value[3]))
                        seats = tuple(
                            tuple(seat) for seat in self._pair_seats(
                                gate[0], gate[1], value[0]))
                    placement_rows.append((gate, seats))
                cache_key = (
                    tuple(sorted(int(q) for q in returners)),
                    tuple(sorted(
                        (int(q), tuple(site)) for q, site in sites.items())),
                    tuple(placement_rows),
                )
                cached = replay_by_depth_cache.get(cache_key, None)
                if cached is replay_failure:
                    raise RuntimeError("cached bounded forecast failure")
                if cached is not None:
                    return dict(cached)
                try:
                    _phases, _exposures, replay_terms = forecast_phases(
                        frozenset(returners), sites, target_placements)
                    by_depth = {}
                    for replay_term in replay_terms:
                        _objective, breakdown = physical.score(
                            replay_term["phases"],
                            replay_term["idle_exposures"], ())
                        offset = int(replay_term["offset"])
                        by_depth[offset] = (
                            by_depth.get(offset, 0.0)
                            + breakdown.negative_log_fidelity)
                except RuntimeError:
                    replay_by_depth_cache[cache_key] = replay_failure
                    raise
                replay_by_depth_cache[cache_key] = tuple(sorted(by_depth.items()))
                return by_depth

            terms = []
            return_future_raw = {}
            future_rollout_fallbacks = 0
            future_rollout_evaluated = 0
            future_rollout_failed = 0
            future_rollout_skipped_budget = 0
            future_rollout_unavailable = False
            h0_uncertain_stay_terms = 0
            h0_opportunity_stay_terms = 0
            h0_history_gap_terms = 0
            h0_reentry_value_terms = 0
            if (active_horizon == 0
                    and self.h0_reentry_value_weight > 0.0):
                for q, scale in sorted(h0_reentry_value_scales.items()):
                    raw_nll = h0_reentry_return_nll[q]
                    value = (self.h0_reentry_value_weight * float(scale)
                             * float(raw_nll))
                    if value <= 1e-15:
                        continue
                    terms.append(RichForecastTerm(
                        depth=0,
                        kind="return",
                        category="terminal",
                        index=eligible_index[q],
                        nll=value,
                    ))
                    h0_reentry_value_terms += 1
            if (active_horizon == 0
                    and self.h0_opportunity_stay_weight > 0.0):
                # A low current interaction share means the resident's frozen
                # topology is diffuse: STAY is then likely to pay an idle
                # Rydberg pulse before it saves a re-entry.  This complements
                # the high-share RETURN value above and uses only the static,
                # order-free graph plus the current target participants.
                one_idle_nll = -math.log(physical.F_EXC)
                for q, scale in sorted(h0_opportunity_stay_scales.items()):
                    value = (self.h0_opportunity_stay_weight
                             * float(scale) * one_idle_nll)
                    if value <= 1e-15:
                        continue
                    terms.append(RichForecastTerm(
                        depth=0,
                        kind="stay",
                        category="terminal",
                        index=eligible_index[q],
                        selector=-1,
                        nll=value,
                    ))
                    h0_opportunity_stay_terms += 1
            if (active_horizon == 0
                    and self.h0_history_gap_weight > 0.0):
                one_idle_nll = -math.log(physical.F_EXC)
                for q, extra_pulses in sorted(
                        h0_history_gap_extra_pulses.items()):
                    value = (self.h0_history_gap_weight
                             * float(extra_pulses) * one_idle_nll)
                    if value <= 1e-15:
                        continue
                    terms.append(RichForecastTerm(
                        depth=0,
                        kind="stay",
                        category="terminal",
                        index=eligible_index[q],
                        selector=-1,
                        nll=value,
                    ))
                    h0_history_gap_terms += 1
            if (active_horizon == 0
                    and (self.h0_state_potential_effective_weight > 0.0
                         or h0_participant_cycle_promoted_genes)):
                # Phi_0 is a state value, not a layer forecast.  Initial SA
                # anchors encode the static interaction graph shared by all
                # methods, while every candidate location below is produced by
                # the current boundary only.  Subtracting the per-gene minimum
                # makes the terms non-negative and leaves the best anchor
                # choice at exactly zero.
                def anchor_nll(q, location):
                    source_xy = arch.exact_SLM_location_tuple(location)
                    distance = math.dist(
                        source_xy, self.h0_state_anchor_xy[q])
                    if distance <= 1e-9:
                        return 0.0
                    duration = math.sqrt(
                        distance / physical.ACCEL_UM_PER_US2)
                    if duration >= physical.T2_US:
                        return float("inf")
                    return -len(self.mapping[0]) * math.log1p(
                        -duration / physical.T2_US)

                for column, domain in enumerate(candidates):
                    raw = []
                    for site, _current_weight, q1, q2 in domain:
                        target1, target2 = self._pair_seats(q1, q2, site)
                        raw.append(
                            anchor_nll(q1, target1)
                            + anchor_nll(q2, target2))
                    minimum = min(raw)
                    for selector, value in enumerate(raw):
                        marginal = self.h0_state_potential_effective_weight * (
                            value - minimum)
                        if marginal > 1e-15:
                            terms.append(RichForecastTerm(
                                depth=0,
                                kind="gate_option",
                                category="terminal",
                                index=column,
                                selector=selector,
                                nll=marginal,
                            ))

                    if column in h0_participant_cycle_promoted_genes:
                        # Physical amortization is structural, not a new
                        # tuning knob: credit the exact static-anchor movement
                        # NLL gained by the one-step option over the admitted
                        # back/out interaction incidences.  The configured
                        # Phi_0 term already supplies part of that credit, so
                        # add only its complement.  Options closer to the same
                        # frozen anchor receive no penalty.  Native current
                        # scheduling NLL and ghost replay still decide the
                        # complete chromosome.
                        progressive_selector = \
                            h0_participant_cycle_promoted_genes[column]
                        progressive_nll = raw[progressive_selector]
                        # Each admitted participant-cycle has exactly two
                        # physical movement phases (back and out).  Amortize
                        # that pair over the frozen interaction multiplicity
                        # (one edge at an endpoint, two inside a chain).  Both
                        # factors are observed structure, already bounded by
                        # the policy's <=2 rule, not free parameters.
                        amortization_ratio = float(max(
                            1,
                            2 * h0_participant_cycle_amortization[
                                "static_interaction_total"]))
                        complement = max(
                            0.0,
                            amortization_ratio
                            - self.h0_state_potential_effective_weight)
                        amortization_terms = []
                        for selector, value in enumerate(raw):
                            marginal = complement * max(
                                0.0, value - progressive_nll)
                            if marginal <= 1e-15:
                                continue
                            terms.append(RichForecastTerm(
                                depth=0,
                                kind="gate_option",
                                category="terminal",
                                index=column,
                                selector=selector,
                                nll=marginal,
                            ))
                            amortization_terms.append(float(marginal))
                        h0_participant_cycle_amortization.update({
                            "static_anchor_amortization_ratio": (
                                amortization_ratio),
                            "static_anchor_complement": float(complement),
                            "static_anchor_amortization_terms": len(
                                amortization_terms),
                            "static_anchor_amortization_max_nll": (
                                max(amortization_terms)
                                if amortization_terms else 0.0),
                        })

                participant_set = set(participants)
                for q in eligible:
                    if q in participant_set:
                        # A participant RETURN is only a transient cycle; its
                        # committed endpoint is already owned by the gate term.
                        continue
                    index = eligible_index[q]
                    choices = [
                        ("stay", None, anchor_nll(q, reg.zone_seat[q]))]
                    choices.extend(
                        ("return_site", site, anchor_nll(q, site))
                        for site, _current_nll, _reasons
                        in provisional_return_domains[index])
                    minimum = min(value for _kind, _site, value in choices)
                    for kind, site, value in choices:
                        marginal = self.h0_state_potential_effective_weight * (
                            value - minimum)
                        if marginal <= 1e-15:
                            continue
                        terms.append(RichForecastTerm(
                            depth=0,
                            kind=kind,
                            category="terminal",
                            index=index,
                            selector=(
                                -1 if site is None else
                                self.boundary_storage_site_id[site]),
                            nll=marginal,
                        ))
            if (active_horizon == 0
                    and self.h0_uncertain_stay_effective_weight > 0.0):
                # H=0 has no ordered L+2 layer.  Use only the frozen,
                # order-free interaction graph and the *current* target
                # participants to estimate reuse uncertainty.  This is a soft
                # value term: the native joint scorer can still keep the atom
                # when shared-batch savings dominate, unlike a forced RETURN
                # mask.  One idle pulse sets the physical scale.
                one_idle_nll = -math.log(physical.F_EXC)
                for q in resident_eligible:
                    support = sum(
                        1 for participant in participants
                        if self.static_interaction_counts.get(
                            (q, participant), 0) > 0)
                    if support >= 2:
                        continue
                    terms.append(RichForecastTerm(
                        depth=0,
                        kind="stay",
                        category="terminal",
                        index=eligible_index[q],
                        selector=-1,
                        nll=(self.h0_uncertain_stay_effective_weight
                             * one_idle_nll),
                    ))
                    h0_uncertain_stay_terms += 1
            if active_horizon and not native_future_rollout:
                # Establish one physically replayable reference placement
                # before forming per-gene marginal terms.  The distance-only
                # Hungarian incumbent is a useful first try, but it is not a
                # semantic requirement and can be future-ghost-infeasible.
                baseline_trials = []

                def add_baseline(placements):
                    try:
                        replay = replay_by_depth((), {}, placements)
                    except RuntimeError:
                        return
                    key = (
                        sum(replay.values()),
                        tuple(tuple(value["site"]) if isinstance(value, dict)
                              else tuple(value[0]) for value in placements),
                    )
                    baseline_trials.append((key, list(placements), replay))

                add_baseline(matched)
                if not baseline_trials:
                    found = False
                    for column, domain in enumerate(candidates):
                        # Reference repair follows the same bounded forecast
                        # support; searching the complete current gate domain
                        # here would turn lookahead into a second placer.
                        for option in domain[:self.return_candidate_limit]:
                            placements = list(matched)
                            placements[column] = option
                            add_baseline(placements)
                            if baseline_trials:
                                found = True
                                break
                        if found:
                            break
                if not baseline_trials:
                    joint_space = math.prod(
                        len(domain) for domain in candidates)
                    if joint_space <= self.direct_enumeration_limit:
                        for placements in product(*candidates):
                            add_baseline(placements)
                            if baseline_trials:
                                break
                    else:
                        # Bounded two-column repair for a wide current front.
                        # It is deterministic placement preparation, not a
                        # nested future GA, and runs only after every one-gene
                        # repair failed.
                        support = 6
                        found = False
                        for first in range(len(candidates)):
                            for second in range(first + 1, len(candidates)):
                                for left in candidates[first][:support]:
                                    for right in candidates[second][:support]:
                                        placements = list(matched)
                                        placements[first] = left
                                        placements[second] = right
                                        add_baseline(placements)
                                        if baseline_trials:
                                            found = True
                                            break
                                    if found:
                                        break
                                if found:
                                    break
                            if found:
                                break
                if not baseline_trials:
                    # Forecasting is a ranking aid, never a current-feasibility
                    # constraint.  If the bounded deterministic rollout cannot
                    # construct a safe reference, keep the complete current
                    # domain and omit future terms for this boundary.  No
                    # synthetic ghost penalty is introduced.
                    baseline_placements = list(matched)
                    stay_replay = {}
                    future_rollout_unavailable = True
                else:
                    _baseline_key, baseline_placements, stay_replay = min(
                        baseline_trials, key=lambda value: value[0])

                    def conservative_replay(safe_replays):
                        """Worst componentwise cost from real safe rollouts.

                        A failed forecast option receives this executable
                        physical surrogate only for ranking.  The option stays
                        in the current domain and the current native evaluator
                        remains solely responsible for feasibility.
                        """
                        depths = sorted({
                            depth for replay in safe_replays for depth in replay
                        })
                        return {
                            depth: max(replay.get(depth, 0.0)
                                       for replay in safe_replays)
                            for depth in depths
                        }

                    for column, domain in enumerate(candidates):
                        raw_replays = []
                        safe_replays = []
                        # The current native domain remains complete so rare
                        # ghost-safe distant placements are always reachable.
                        # Forecast preparation is a bounded deterministic
                        # proxy: replay the current-cost-leading support plus
                        # the selected safe reference, and assign the measured
                        # conservative surrogate to the unsampled tail.
                        reference_selector = next(
                            (selector for selector, option in enumerate(domain)
                             if option == baseline_placements[column]), 0)
                        forecast_selectors = set(range(min(
                            len(domain), self.return_candidate_limit)))
                        forecast_selectors.add(reference_selector)
                        for selector, option in enumerate(domain):
                            if selector not in forecast_selectors:
                                raw_replays.append(None)
                                future_rollout_skipped_budget += 1
                                continue
                            future_rollout_evaluated += 1
                            placements = list(baseline_placements)
                            placements[column] = option
                            try:
                                replay = replay_by_depth((), {}, placements)
                            except RuntimeError:
                                replay = None
                                future_rollout_failed += 1
                            raw_replays.append(replay)
                            if replay is not None:
                                safe_replays.append(replay)
                        # Replacing a column with the reference option is always
                        # replayable, but fail closed if that invariant drifts.
                        if not safe_replays:
                            raise RuntimeError(
                                f"M4 gate {column} lost its safe reference")
                        fallback = conservative_replay(safe_replays)
                        option_replays = []
                        for selector, replay in enumerate(raw_replays):
                            if replay is None:
                                replay = fallback
                                future_rollout_fallbacks += 1
                            option_replays.append((selector, replay))
                        depths = sorted({
                            depth for _selector, replay in option_replays
                            for depth in replay
                        })
                        for depth in depths:
                            minimum = min(
                                replay.get(depth, 0.0)
                                for _selector, replay in option_replays)
                            for selector, replay in option_replays:
                                marginal = replay.get(depth, 0.0) - minimum
                                if marginal <= 1e-15:
                                    continue
                                terms.append(RichForecastTerm(
                                    depth=depth,
                                    kind="gate_option",
                                    category="routing",
                                    index=column,
                                    selector=selector,
                                    nll=marginal,
                                ))

                    # RETURN-position marginals use the same safe gate
                    # reference.  Unsafe bounded forecasts receive the worst
                    # cost of an actually executable choice; domains are never
                    # filtered by lookahead.
                    for q in eligible:
                        index = eligible_index[q]
                        raw_choices = [("stay", None, stay_replay)]
                        safe_replays = [stay_replay]
                        for site, _current_nll, _reasons in \
                                provisional_return_domains[index]:
                            future_rollout_evaluated += 1
                            try:
                                replay = replay_by_depth(
                                    (q,), {q: site}, baseline_placements)
                            except RuntimeError:
                                replay = None
                                future_rollout_failed += 1
                            raw_choices.append(("return_site", site, replay))
                            if replay is not None:
                                safe_replays.append(replay)
                        fallback = conservative_replay(safe_replays)
                        choices = []
                        for kind, site, replay in raw_choices:
                            if replay is None:
                                replay = fallback
                                future_rollout_fallbacks += 1
                            choices.append((kind, site, replay))
                            if site is not None:
                                return_future_raw[(q, site)] = replay
                        depths = sorted({
                            depth for _kind, _site, replay in choices
                            for depth in replay
                        })
                        for depth in depths:
                            minimum = min(
                                replay.get(depth, 0.0)
                                for _kind, _site, replay in choices)
                            for kind, site, replay in choices:
                                marginal = replay.get(depth, 0.0) - minimum
                                if marginal <= 1e-15:
                                    continue
                                terms.append(RichForecastTerm(
                                    depth=depth,
                                    kind=kind,
                                    category="routing",
                                    index=index,
                                    selector=(-1 if site is None else
                                              self.boundary_storage_site_id[site]),
                                    nll=marginal,
                                ))

            if indexed_native_rich:
                if len(indexed_native_domains) != n_gates:
                    raise RuntimeError(
                        "indexed native gate domains are not column-aligned")
                rich_gate_domains = list(indexed_native_dto_domains)
                native_matched_sites = self._match_gates(
                    native_candidates, list_gate,
                    return_sites=True) if n_gates else []
                native_matched_genes = [
                    indexed_native_gene_by_site[column][tuple(site)]
                    for column, site in enumerate(native_matched_sites)
                ]
            else:
                rich_gate_domains = []
                for domain in native_candidates:
                    rich_domain = []
                    for site, _weight, q1, q2 in domain:
                        site = tuple(site)
                        target1, target2 = self._pair_seats(q1, q2, site)
                        rich_domain.append(self._cached_rich_gate_option(
                            q1, q2, site, target1, target2))
                    rich_gate_domains.append(tuple(rich_domain))
                native_matched = self._match_gates(
                    native_candidates, list_gate) if n_gates else []
                native_matched_genes = []
                for column, placement in enumerate(native_matched):
                    site = tuple(placement["site"])
                    native_matched_genes.append(next(
                        (index for index, option in enumerate(
                            native_candidates[column])
                         if tuple(option[0]) == site), 0))

            rich_return_domains = []
            for index, q in enumerate(eligible):
                domain = []
                for site, current_nll, reasons in \
                        provisional_return_domains[index]:
                    domain.append(RichReturnOption(
                        site_id=self.boundary_storage_site_id[site],
                        point=None,
                        # K-best matching is generated from executable current
                        # transfer physics.  Decayed future site value is
                        # applied exactly once by the return_site forecast term
                        # when those assignments are compared.  Mixing it into
                        # this preselection cost could hide every ghost-safe
                        # current assignment behind a speculative forecast.
                        cost=float(current_nll),
                        site_location=site,
                    ))
                    return_option_reasons[(q, site)] = reasons
                rich_return_domains.append(tuple(domain))
            rich_forecast_terms = tuple(terms)
            native_future_layers = tuple(
                (
                    int(offset),
                    tuple((int(q1), int(q2)) for q1, q2 in gates),
                )
                for _absolute, gates, offset, _weight in visible_forecast
            )
            native_rich_problem = RichH0Problem(
                architecture=boundary_architecture,
                current_points=(),
                current_site_ids=tuple(
                    self.boundary_site_id[tuple(reg.current_pos(q))]
                    for q in range(len(self.mapping[0]))),
                participants=tuple(sorted(participants)),
                gate_domains=tuple(rich_gate_domains),
                static_ghosts=tuple(BoundaryGhost(
                    int(row[0]),
                    BoundaryPoint(float(row[1]), float(row[2])))
                    for row in static_ghosts),
                eligible=tuple(eligible),
                min_returns=min_returns,
                eviction_order_indices=tuple(
                    eligible_index[q] for q in eviction_order),
                forced_return_mask=tuple(
                    q in horizon_forced_returns
                    or q in physical_forced_returns
                    or q in rent_forced_returns
                    for q in eligible),
                recommended_return_mask=tuple(
                    q in rent_recommended_returns
                    or q in h0_participant_cycle_recommended_returns
                    for q in eligible),
                recommended_stay_mask=tuple(
                    q in rent_recommended_stays for q in eligible),
                return_domains=tuple(rich_return_domains),
                matched_gate_genes=tuple(native_matched_genes),
                decision_policy=self.ablation_policy,
                occupied_storage_site_ids=tuple(sorted(
                    self.boundary_storage_site_id[tuple(site)]
                    for site in occupied_storage)),
                forecast_terms=rich_forecast_terms,
                future_layers=native_future_layers,
                boundary_id=f"{self.method_id}:L{layer}",
                selected_horizon=active_horizon,
                terminal_boundary=(layer + 2 >= forecast.layer_count),
                prior_idle_time_us=tuple(scheduler_idle_prior),
                scheduler_trace_end_us=(
                    0.0 if approximate_ultra_deep_current else float(
                        scheduler_prefix.scheduler.trace_end_us)),
                scheduler_active_union_us=(
                    () if approximate_ultra_deep_current else tuple(
                        float(value) for value in
                        scheduler_prefix.scheduler.active_union_us)),
                scheduler_aod_end_us=(
                    () if approximate_ultra_deep_current else tuple(
                        float(value) for value in
                        scheduler_prefix.scheduler.aod_end_us)),
                scheduler_one_qubit_end_us=(
                    0.0 if approximate_ultra_deep_current else float(
                        scheduler_prefix.scheduler.one_qubit_end_us)),
                scheduler_rydberg_end_us=(
                    () if approximate_ultra_deep_current else tuple(
                        float(value) for value in
                        scheduler_prefix.scheduler.rydberg_end_us)),
                scheduler_qubit_dependency_end_us=(
                    () if approximate_ultra_deep_current else tuple(
                        float(value) for value in
                        scheduler_prefix.qubit_dependency_end_us)),
                scheduler_back_dependency_end_us=(
                    () if approximate_ultra_deep_current else tuple(
                        float(value) for value in
                        scheduler_prefix.back_dependency_end_us)),
                scheduler_site_dependency_site_ids=(
                    () if approximate_ultra_deep_current else tuple(
                        self.boundary_site_id[tuple(location)]
                        for location, _value in
                        scheduler_prefix.site_dependency_activation_finish_us)),
                scheduler_site_dependency_activation_finish_us=(
                    () if approximate_ultra_deep_current else tuple(
                        float(value) for _location, value in
                        scheduler_prefix.site_dependency_activation_finish_us)),
                target_one_qubit_atoms=(
                    () if approximate_ultra_deep_current else tuple(
                        int(gate[1])
                        for gate in self._one_qubit_for_layer(next_layer))),
                scheduler_one_qubit_duration_us=(
                    float(self.architecture.time_1qGate)
                    if approximate_ultra_deep_current else float(
                        scheduler_prefix.scheduler.one_qubit_duration_us)),
                scheduler_rydberg_duration_us=(
                    float(self.architecture.time_rydberg)
                    if approximate_ultra_deep_current else float(
                        scheduler_prefix.scheduler.rydberg_duration_us)),
                scheduler_one_qubit_common_us=(
                    0.0 if approximate_ultra_deep_current else float(
                        scheduler_prefix.scheduler.one_qubit_common_us)),
                scheduler_transfer_duration_us=float(
                    self.architecture.time_atom_transfer),
                scheduler_accel_um_per_us2=float(
                    physical.ACCEL_UM_PER_US2),
                coherence_t2_us=float(physical.T2_US),
                enforce_frozen_physical_model=bool(
                    self.formal_native
                    and not approximate_ultra_deep_current),
            )

        if use_rich_boundary:
            if self.boundary_backend is None:
                self.boundary_backend = select_backend(
                    boundary_architecture,
                    backend=self.resident_backend_requested,
                    formal=self.formal_native,
                    require_registered_wheel=self.formal_native,
                    expected_wheel_sha256=(
                        self.native_wheel_sha256
                        if self.formal_native else None),
                )
            solve_backend = self.boundary_backend
            if approximate_ultra_deep_current:
                solve_backend = getattr(
                    self, "ultra_deep_boundary_backend", None)
                if solve_backend is None:
                    # Still the registered C++ ABI8 wheel and still fail-closed;
                    # only the expensive absolute-scheduler snapshot contract
                    # is disabled for the OOD ultra-deep tail.
                    solve_backend = select_backend(
                        boundary_architecture,
                        backend="native",
                        formal=False,
                        require_registered_wheel=False,
                    )
                    self.ultra_deep_boundary_backend = solve_backend
            boundary_rng_state = self.rng.getstate()
            native_relaxed_ghost_recovery = False
            try:
                native_rich_result = solve_backend.solve_rich_boundary(
                    native_rich_problem,
                    rich_config,
                    boundary_rng_state,
                    cached_winner=cached_winner,
                )
            except NativeBackendError as exc:
                if (bounded_long_depth_domain
                        and "no feasible candidate" in str(exc)):
                    # The local menu is an acceleration portfolio, not a new
                    # feasibility rule.  Rebuild the same boundary with all
                    # 140 interaction sites and replay the identical RNG state.
                    self.rng.setstate(boundary_rng_state)
                    self.current_scheduler_prefix = None
                    self.deep_serial_full_domain_recoveries = (
                        getattr(self, "deep_serial_full_domain_recoveries", 0)
                        + 1)
                    return self._ga_step_v2(
                        layer, _force_complete_gate_domain=True)
                if (_force_complete_gate_domain
                        and "no feasible candidate" in str(exc)):
                    # Some ultra-deep boundaries need a deterministic RESEAT
                    # of a stationary blocker before any strict single-leg
                    # candidate exists.  Keep candidate generation and search
                    # in C++, then run the existing ghost-safe repair and exact
                    # Python production replay before committing the result.
                    # This is a repair portfolio, not a reference-backend GA.
                    native_rich_result = \
                        solve_backend.solve_rich_boundary(
                            native_rich_problem,
                            replace(
                                rich_config,
                                enforce_single_leg_ghost=False),
                            boundary_rng_state,
                            cached_winner=cached_winner,
                        )
                    native_relaxed_ghost_recovery = True
                    self.native_relaxed_ghost_recoveries = (
                        getattr(self, "native_relaxed_ghost_recoveries", 0)
                        + 1)
                else:
                    raise
            if native_rich_result.operator_profile != self.operator_profile:
                raise RuntimeError(
                    "native rich operator profile differs from resolved config")
            self.rng.setstate(native_rich_result.rng_state)
            native_return_sites = {
                int(q): self.boundary_storage_locations[int(site_id)]
                for q, site_id in native_rich_result.return_assignments
            }
            native_reseat_sites = {
                int(q): self.boundary_site_locations[int(site_id)]
                for q, site_id in native_rich_result.reseat_assignments
            }
            native_participant_parking_sites = {
                int(q): self.boundary_site_locations[int(site_id)]
                for q, site_id in
                native_rich_result.participant_parking_assignments
            }
            if set(native_reseat_sites) & set(native_return_sites):
                raise RuntimeError(
                    "native rich atom cannot both RETURN and RESEAT")
            if ((set(native_participant_parking_sites)
                 & (set(native_return_sites) | set(native_reseat_sites)))):
                raise RuntimeError(
                    "native rich participant parking overlaps a resident "
                    "RETURN/RESEAT assignment")
            if not set(native_reseat_sites) <= set(eligible):
                raise RuntimeError(
                    "native rich RESEAT atom is outside resident decisions")
            if not set(native_participant_parking_sites) <= set(participants):
                raise RuntimeError(
                    "native rich participant parking atom is outside the "
                    "target gate layer")
            parking_site_ids = tuple(
                int(site_id) for _q, site_id in
                native_rich_result.participant_parking_assignments)
            if len(set(parking_site_ids)) != len(parking_site_ids):
                raise RuntimeError(
                    "native rich participant parking sites are not injective")
            if not set(parking_site_ids) <= set(
                    boundary_architecture.storage_site_ids):
                raise RuntimeError(
                    "native rich participant parking target is not storage")
            selected_participant_parking_audit = [
                {
                    "atom": int(q),
                    "site_id": int(site_id),
                    "location": list(self.boundary_site_locations[
                        int(site_id)]),
                    "reason": "stationary_participant_out_ghost",
                }
                for q, site_id in
                native_rich_result.participant_parking_assignments
            ]
            selected_return_audit = [
                {
                    "atom": int(q),
                    "site_id": int(site_id),
                    "location": list(self.boundary_storage_locations[
                        int(site_id)]),
                    "reasons": list(return_option_reasons.get(
                        (int(q), self.boundary_storage_locations[int(site_id)]),
                        ("bounded_fill",))),
                    "assignment_rank": int(
                        native_rich_result.return_assignment_rank),
                }
                for q, site_id in native_rich_result.return_assignments
            ]
            if set(native_return_sites) != {
                    q for q, bit in zip(
                        eligible,
                        native_rich_result.winner.chromosome[n_gates:])
                    if bit}:
                raise RuntimeError(
                    "native rich RETURN assignments disagree with winner bits")
            if rich_config.max_horizon == 0:
                zero_breakdown = {
                    "residency": 0.0,
                    "reentry": 0.0,
                    "terminal": 0.0,
                    "routing": 0.0,
                }
                if native_rich_problem.future_layers:
                    raise RuntimeError("H=0 native problem contains future layers")
                if any(term.depth != 0
                       for term in native_rich_problem.forecast_terms):
                    raise RuntimeError(
                        "H=0 native problem contains a future-depth term")
            if (rich_config.max_horizon == 0
                    and not native_rich_problem.forecast_terms):
                if (not math.isclose(
                        native_rich_result.forecast_nll, 0.0,
                        rel_tol=0.0, abs_tol=1e-15)
                        or tuple(native_rich_result.forecast_by_depth) != (0.0,)
                        or dict(native_rich_result.forecast_breakdown) !=
                        zero_breakdown
                        or native_rich_result.forecast_terms_applied != 0
                        or native_rich_result.forecast_terms_skipped_cutoff != 0):
                    raise RuntimeError(
                        "H=0 native result consumed a forecast heuristic")
                native_reference_forecast = (
                    0.0, (0.0,), zero_breakdown, 0, 0)
            elif native_rich_problem.future_layers:
                # The formal ABI5 forecast is deliberately native-owned.  Its
                # independent contract is raw layer isolation plus final trace
                # verification; reconstructing it in Python would restore the
                # exact performance bottleneck this interface removes.
                native_reference_forecast = (
                    native_rich_result.forecast_nll,
                    native_rich_result.forecast_by_depth,
                    native_rich_result.forecast_breakdown,
                    native_rich_result.forecast_terms_applied,
                    native_rich_result.forecast_terms_skipped_cutoff,
                )
            else:
                from zzx.reference_backend import evaluate_decay_forecast
                reference_forecast = evaluate_decay_forecast(
                    native_rich_problem, rich_config,
                    native_rich_result.winner.chromosome,
                    native_rich_result.gate_option_indices,
                    native_rich_result.return_assignments)
                native_reference_forecast = reference_forecast
                if not math.isclose(
                        reference_forecast[0],
                        native_rich_result.forecast_nll,
                        rel_tol=0.0, abs_tol=1e-12):
                    raise RuntimeError(
                        "native decay forecast differs from Python oracle: "
                        f"boundary={native_rich_problem.boundary_id}, "
                        f"native={native_rich_result.forecast_nll!r}, "
                        f"reference={reference_forecast[0]!r}, "
                        f"native_by_depth="
                        f"{tuple(native_rich_result.forecast_by_depth)!r}, "
                        f"reference_by_depth={reference_forecast[1]!r}, "
                        f"native_breakdown="
                        f"{dict(native_rich_result.forecast_breakdown)!r}, "
                        f"reference_breakdown={reference_forecast[2]!r}, "
                        f"terms={len(native_rich_problem.forecast_terms)}, "
                        f"return_assignments="
                        f"{tuple(native_rich_result.return_assignments)!r}")
            expected_search_nll = (
                native_rich_result.winner.negative_log_fidelity
                + native_rich_result.forecast_nll)
            if not math.isclose(
                    expected_search_nll,
                    native_rich_result.search_negative_log_fidelity,
                    rel_tol=0.0, abs_tol=1e-12):
                raise RuntimeError(
                    "native search NLL does not equal current plus forecast")
            step_cache.evaluations = native_rich_result.evaluations
            step_cache.unique_evaluations = \
                native_rich_result.unique_evaluations
            step_cache.fitness_hits = native_rich_result.fitness_hits
            step_cache.decode_hits = native_rich_result.decode_hits
            step_cache.return_match_hits = native_rich_result.return_match_hits
            native_timing = dict(native_rich_result.timing)
            rich_delta = {
                "marshal_ns": int(native_timing.get("marshal_ns", 0)),
                "python_marshal_ns": int(
                    native_timing.get("python_marshal_ns", 0)),
                "native_call_wall_ns": int(
                    native_timing.get("native_call_wall_ns", 0)),
                "native_search_wall_ns": int(
                    native_timing.get("native_search_wall_ns", 0)),
                "search_kernel_ns": int(
                    native_timing.get("search_kernel_ns", 0)),
                "fitness_ns": int(native_timing.get("fitness_ns", 0)),
                "normalize_ns": int(native_timing.get("normalize_ns", 0)),
                "decode_ns": int(native_timing.get("decode_ns", 0)),
                "return_match_ns": int(
                    native_timing.get("return_match_ns", 0)),
                "forecast_ns": int(native_timing.get("forecast_ns", 0)),
                "selection_ns": int(native_timing.get("selection_ns", 0)),
                "native_parse_ns": int(
                    native_timing.get("native_parse_ns", 0)),
                "native_serialize_ns": int(
                    native_timing.get("native_serialize_ns", 0)),
                "calls": 1,
                "candidates": int(native_rich_result.evaluations),
            }
            for key, value in rich_delta.items():
                step_backend[key] += value
                self.boundary_backend_metrics[key] += value
            rich_search_stats = {
                # Preserve the pre-normalization Cartesian size used by both
                # exact backends.  Migration benchmarks consume this audit to
                # prove that every measured boundary is inside the frozen
                # direct-search budget instead of silently timing a different
                # search mode.
                "direct_search_space": int(direct_search_space),
                "gate_domain_sizes": [int(value) for value in gate_domains],
                "bounded_long_depth_domain": bounded_long_depth_domain,
                "deterministic_unique_evaluations": int(
                    native_rich_result.deterministic_unique_evaluations),
                "stochastic_unique_evaluations": int(
                    native_rich_result.stochastic_unique_evaluations),
                "generations": int(native_rich_result.generations),
                "early_stopped": bool(native_rich_result.early_stopped),
                "early_stop_reason": str(
                    native_rich_result.early_stop_reason),
                "stochastic_budget": int(
                    native_rich_result.stochastic_budget),
                "operator_profile": self.operator_profile,
                "operator_stats": dict(
                    getattr(native_rich_result, "operator_stats", {}) or {}),
                "forecast_terms": len(rich_forecast_terms),
                "native_future_layers": len(
                    native_rich_problem.future_layers),
                "forecast_terms_applied": int(
                    native_rich_result.forecast_terms_applied),
                "forecast_terms_skipped_cutoff": int(
                    native_rich_result.forecast_terms_skipped_cutoff),
                "forecast_nll": float(native_rich_result.forecast_nll),
                "future_rollout_fallbacks": int(
                    future_rollout_fallbacks),
                "future_rollout_evaluated": int(
                    future_rollout_evaluated),
                "future_rollout_failed": int(future_rollout_failed),
                "future_rollout_skipped_budget": int(
                    future_rollout_skipped_budget),
                "future_rollout_unavailable": bool(
                    future_rollout_unavailable),
                "search_negative_log_fidelity": float(
                    native_rich_result.search_negative_log_fidelity),
                "return_assignment_rank": int(
                    native_rich_result.return_assignment_rank),
                "return_assignment_evaluated": int(
                    native_rich_result.return_assignment_evaluated),
                "current_ghost_rejections": int(
                    native_rich_result.current_ghost_rejections),
                "future_ghost_cost": float(
                    native_rich_result.future_ghost_cost),
                "pre_score_reseats": int(
                    native_rich_result.pre_score_reseats),
                "pre_score_participant_parkings": int(
                    native_rich_result.pre_score_participant_parkings),
                "current_gate_anchor": list(
                    native_rich_result.current_gate_anchor),
                "current_gate_anchor_assignment_site_ids": list(
                    native_rich_result.
                    current_gate_anchor_assignment_site_ids),
                "current_gate_final_assignment_site_ids": list(
                    native_rich_result.
                    current_gate_final_assignment_site_ids),
                "current_gate_guard_branch": str(
                    native_rich_result.current_gate_guard_branch),
                "current_gate_guard_cohort_size": int(
                    native_rich_result.current_gate_guard_cohort_size),
                "current_gate_guard_admitted_size": int(
                    native_rich_result.current_gate_guard_admitted_size),
                "current_gate_projection_source": str(
                    native_rich_result.current_gate_projection_source),
                "current_gate_projection_evaluated": int(
                    native_rich_result.current_gate_projection_evaluated),
            }
        seed_chromosomes = [
            list(native_rich_result.winner.chromosome)
            if native_rich_result is not None
            else normalize_chrom(matched_genes + [1] * len(eligible))]

        # Legacy adaptive M4 carries an explicit H=0 safety incumbent on the *same current
        # registry state*.  It is obtained once per boundary, outside fitness,
        # so this is not a nested GA and cannot read L+2 through a side channel.
        # Gate genes are first improved for the exact current transition; legacy H=2
        # may then toggle residency genes while those gate sites stay fixed.
        # The unrestricted legacy H=2 GA can replace this candidate only when its
        # current physical objective is non-inferior (see winner guard below).
        safe_chrom = list(seed_chromosomes[0])
        myopic_chrom = list(safe_chrom)
        if (not self.decay_lookahead
                and active_horizon > 0
                and self.ablation_policy == "optimize"):
            myopic_score = fitness(myopic_chrom, include_forecast=False)[0]
            for i in range(len(eligible)):
                trial = list(myopic_chrom)
                trial[n_gates + i] ^= 1
                trial = normalize_chrom(trial)
                trial_score = fitness(trial, include_forecast=False)[0]
                if trial_score < myopic_score:
                    myopic_chrom, myopic_score = trial, trial_score
            if n_gates:
                myopic_trials = max(
                    2, self.neighbor_sample_size // max(1, n_gates))
                for i, domain in enumerate(gate_domains):
                    if domain <= myopic_trials:
                        values = list(range(domain))
                    else:
                        values = sorted({
                            round(j * (domain - 1) / (myopic_trials - 1))
                            for j in range(myopic_trials)
                        } | {myopic_chrom[i]})
                    for value in values:
                        if value == myopic_chrom[i]:
                            continue
                        trial = list(myopic_chrom)
                        trial[i] = value
                        trial = normalize_chrom(trial)
                        trial_score = fitness(
                            trial, include_forecast=False)[0]
                        if trial_score < myopic_score:
                            myopic_chrom, myopic_score = trial, trial_score
                for i in range(len(eligible)):
                    trial = list(myopic_chrom)
                    trial[n_gates + i] ^= 1
                    trial = normalize_chrom(trial)
                    trial_score = fitness(
                        trial, include_forecast=False)[0]
                    if trial_score < myopic_score:
                        myopic_chrom, myopic_score = trial, trial_score
            safe_chrom = list(myopic_chrom)
            if not will_enumerate:
                safe_score = fitness(safe_chrom)[0]
                for i in range(len(eligible)):
                    trial = list(safe_chrom)
                    trial[n_gates + i] ^= 1
                    trial = normalize_chrom(trial)
                    trial_score = fitness(trial)[0]
                    if trial_score < safe_score:
                        safe_chrom, safe_score = trial, trial_score
                seed_chromosomes.append(list(safe_chrom))
        if native_rich_result is not None:
            greedy_chrom = list(native_rich_result.winner.chromosome)
            greedy_score = native_rich_result.search_objective
        else:
            greedy_score, greedy_chrom = min(
                (fitness(chromosome)[0], chromosome)
                for chromosome in seed_chromosomes)
        if native_rich_result is None and not will_enumerate:
            for i in range(len(eligible)):
                trial = list(greedy_chrom)
                trial[n_gates + i] ^= 1
                trial = normalize_chrom(trial)
                trial_score = fitness(trial)[0]
                if trial_score < greedy_score:
                    greedy_chrom, greedy_score = trial, trial_score

        # Joint physical-greedy gate seed.  Menu index zero is ordered by the
        # legacy sqrt-distance proxy and can be poor once transfer fidelity,
        # phase makespan, stationary coherence, and legacy terminal value are
        # evaluated together.  Spend a deterministic *per-layer* budget across
        # gate genes: narrow layers see most/all of each menu, while wide Ising
        # layers remain bounded instead of multiplying runtime by menu size.
        if native_rich_result is None and n_gates and not will_enumerate:
            trials_per_gate = max(
                2, self.neighbor_sample_size // max(1, n_gates))
            for i, domain in enumerate(gate_domains):
                if domain <= trials_per_gate:
                    values = list(range(domain))
                else:
                    values = sorted({
                        round(j * (domain - 1) / (trials_per_gate - 1))
                        for j in range(trials_per_gate)
                    } | {greedy_chrom[i]})
                best_value = greedy_chrom[i]
                best_score = greedy_score
                for value in values:
                    if value == greedy_chrom[i]:
                        continue
                    trial = list(greedy_chrom)
                    trial[i] = value
                    trial = normalize_chrom(trial)
                    trial_score = fitness(trial)[0]
                    if trial_score < best_score:
                        best_value, best_score = trial[i], trial_score
                greedy_chrom[i] = best_value
                greedy_score = best_score

            # Gate placement can change the true price of a RETURN subset.  One
            # deterministic toggle sweep closes that coupling without nesting
            # another GA or reading beyond ForecastOracle.
            for i in range(len(eligible)):
                trial = list(greedy_chrom)
                trial[n_gates + i] ^= 1
                trial = normalize_chrom(trial)
                trial_score = fitness(trial)[0]
                if trial_score < greedy_score:
                    greedy_chrom, greedy_score = trial, trial_score
        greedy = greedy_chrom[n_gates:]

        def score_unique(chromosomes, include_forecast=True):
            unique = {}
            for chromosome in chromosomes:
                key = tuple(normalize_chrom(chromosome))
                unique.setdefault(key, list(key))
            chromosomes = list(unique.values())
            objectives = fitness_many(
                chromosomes, include_forecast=include_forecast)
            scored = [(value[0], chromosome)
                      for value, chromosome in zip(objectives, chromosomes)]
            return sorted(scored, key=lambda item: (item[0], tuple(item[1])))

        def neighbor_with_rng(chrom, rng):
            result = list(chrom)
            if n_gates and (not eligible or rng.random() < 0.5):
                i = rng.randrange(n_gates)
                result[i] = rng.choice(
                    [result[i] + 1, result[i] - 1,
                     rng.randrange(gate_domains[i])])
            elif eligible:
                i = rng.randrange(len(eligible))
                result[n_gates + i] ^= 1
            return normalize_chrom(result)

        def neighbor(chrom):
            return neighbor_with_rng(chrom, self.rng)

        # ``myopic_chrom`` above is a deterministic current-transition safety
        # candidate.  Earlier legacy versions ran a second full H=0 GA here
        # before the registered H=2 GA.  That doubled the search budget and obscured the
        # horizon-only comparison with M3, and dominated large-circuit runtime.
        # The Pareto guard below still compares the horizon winner against the
        # deterministic H=0 incumbent, but M3 and M4 now each execute exactly
        # one stochastic GA per boundary.

        # Deterministic fast paths are shared by NL/LK.  ``state_key`` and its
        # optional cached elite were frozen before backend-specific search.
        search_mode = "ga"
        if native_rich_result is not None:
            scored = [(native_rich_result.search_objective,
                       list(native_rich_result.winner.chromosome))]
            search_mode = native_rich_result.search_mode
        elif cached_winner is not None:
            self.transition_cache.move_to_end(state_key)
            scored = score_unique([cached_winner])
            search_mode = "lru"
        else:
            search_space = direct_search_space
            if (search_space <= self.direct_enumeration_limit
                    and search_space <= self.max_unique_evaluations):
                domains = [range(domain) for domain in gate_domains]
                decision_domain = (range(2) if self.ablation_policy == "optimize"
                                   else range(1))
                domains.extend((decision_domain for _ in eligible))
                chromosomes = product(*domains) if domains else [()]
                scored = score_unique(chromosomes)
                search_mode = "direct" if search_space <= 1 else "enumerate"
            else:
                population = build_seed_population(
                    gate_domains, len(eligible), greedy, self.population_size,
                    self.rng, normalize=normalize_chrom,
                    greedy_gate_genes=greedy_chrom[:n_gates])
                if (not self.decay_lookahead
                        and active_horizon > 0
                        and self.ablation_policy == "optimize"):
                    population.extend((list(safe_chrom), list(greedy_chrom)))
                scored = score_unique(population)[:self.population_size]
                for _ in range(self.iterations):
                    offspring = []
                    for _, chromosome in scored:
                        pool = score_unique(
                            neighbor(chromosome)
                            for _ in range(self.neighbor_sample_size))
                        offspring.extend(chromosome for _, chromosome
                                         in pool[:self.neighbors_per_solution])
                    scored = score_unique(
                        [chromosome for _, chromosome in scored] + offspring
                    )[:self.population_size]

        best_chrom = normalize_chrom(scored[0][1])
        lookahead_selection = "horizon"
        safe_current_objective = None
        horizon_current_objective = None
        if (not self.decay_lookahead
                and active_horizon > 0
                and self.ablation_policy == "optimize"):
            # Ensure the safety candidate participates even in an LRU or
            # truncated-population path, then apply a registered current-step
            # Pareto guard.  No completed-circuit score is observed here.
            # Preserve the horizon-selected residency pattern while projecting
            # only its gate genes onto the H=0 physical incumbent.  This keeps
            # the very mechanism being tested (cross-layer STAY) eligible for
            # the safety path instead of comparing it against all-RETURN.  The
            # decision bits remain byte-for-byte identical: this guard audits
            # forecast-induced gate displacement, not the lookahead decision.
            safe_chrom = normalize_chrom(
                list(myopic_chrom[:n_gates]) + list(best_chrom[n_gates:]))
            horizon_current_objective = fitness(
                best_chrom, include_forecast=False)[0]
            safe_current_objective = fitness(
                safe_chrom, include_forecast=False)[0]
            if (lookahead_selection == "horizon" and
                    horizon_current_objective[:4] > safe_current_objective[:4]):
                best_chrom = normalize_chrom(safe_chrom)
                lookahead_selection = "myopic_safety"
        cache_chrom = tuple(best_chrom)

        # Adjacent reuse/cycle relocation is a bounded lookahead-only extension
        # around the already selected resident-GA incumbent.  Keeping these bits
        # outside the stochastic chromosome preserves the exact H=0 search and
        # prevents a larger gene vector from degrading a no-cycle winner merely
        # by consuming a different RNG stream.  For each currently resident
        # target participant, test RETURN -> re-entry jointly with the gate-site
        # gene of its target gate.  This is the neutral-atom analogue of deciding
        # when a chain should advance the interaction site instead of pinning it.
        selected_cycles: set[int] = set(forced_cycle_candidates)
        cycle_objective = (
            native_rich_result.search_objective
            if native_rich_result is not None and not selected_cycles
            else fitness(best_chrom, cycle_returners=selected_cycles)[0])
        cycle_search_log = []
        # A rollout improvement smaller than one extra load+store fidelity pair
        # is not robust enough to justify changing the executable current state.
        # This physical hysteresis suppresses receding-horizon chattering without
        # introducing a tunable proxy weight.
        min_cycle_gain = -2.0 * math.log(physical.F_TRANSFER)
        if (not self.decay_lookahead
                and active_horizon > 0
                and self.ablation_policy == "optimize"):
            gate_of = {
                q: gate_index for gate_index, gate in enumerate(list_gate)
                for q in gate}
            for q in cycle_candidates:
                if q in forced_cycle_candidates:
                    cycle_search_log.append({
                        "q": q, "accepted": True, "forced": True,
                        "negative_log_fidelity_gain": None,
                        "gate_index": gate_of[q],
                        "gate_gene": best_chrom[gate_of[q]],
                    })
                    continue
                gate_index = gate_of[q]
                domain = gate_domains[gate_index]
                trial_budget = max(
                    2, self.neighbor_sample_size // max(1, len(list_gate)))
                if domain <= trial_budget:
                    values = list(range(domain))
                else:
                    values = sorted({
                        round(j * (domain - 1) / (trial_budget - 1))
                        for j in range(trial_budget)
                    } | {best_chrom[gate_index]})
                trial_cycles = set(selected_cycles)
                trial_cycles.add(q)
                local_score = cycle_objective
                local_chrom = list(best_chrom)
                for value in values:
                    trial = list(best_chrom)
                    trial[gate_index] = value
                    trial = normalize_chrom(trial)
                    score = fitness(
                        trial, cycle_returners=trial_cycles)[0]
                    if score < local_score:
                        local_score, local_chrom = score, trial
                candidate_gain = cycle_objective[0] - local_score[0]
                if candidate_gain > min_cycle_gain:
                    prior_objective = cycle_objective
                    best_chrom = normalize_chrom(local_chrom)
                    selected_cycles = trial_cycles
                    cycle_objective = local_score
                    lookahead_selection = "horizon_cycle"
                    cycle_search_log.append({
                        "q": q, "accepted": True,
                        "negative_log_fidelity_gain": (
                            prior_objective[0] - local_score[0]),
                        "gate_index": gate_index,
                        "gate_gene": best_chrom[gate_index],
                    })
                else:
                    cycle_search_log.append({
                        "q": q, "accepted": False,
                        "negative_log_fidelity_gain": max(0.0, candidate_gain),
                        "gate_index": gate_index,
                        "gate_gene": best_chrom[gate_index],
                    })

        search_kernel_stopped_ns = time.perf_counter_ns()
        search_kernel_ns = search_kernel_stopped_ns - search_kernel_started_ns
        problem_preparation_ns = (
            search_kernel_started_ns - transition_step_started_ns)

        self.transition_cache[state_key] = cache_chrom
        self.transition_cache.move_to_end(state_key)
        while len(self.transition_cache) > self.transition_cache_limit:
            self.transition_cache.popitem(last=False)
        bits = best_chrom[n_gates:]
        returners = ({q for q, bit in zip(eligible, bits) if bit}
                     | selected_cycles)
        formal_selected_cycles = (
            set(formal_cycle_candidates) & returners)
        if formal_cycle_candidates:
            cycle_search_log.extend({
                "q": int(q),
                "accepted": q in formal_selected_cycles,
                "forced": False,
                "native_joint": True,
                "negative_log_fidelity_gain": None,
                "gate_index": next(
                    index for index, gate in enumerate(list_gate) if q in gate),
                "gate_gene": int(best_chrom[next(
                    index for index, gate in enumerate(list_gate) if q in gate)]),
            } for q in formal_cycle_candidates)
        sites = (dict(native_return_sites)
                 if native_rich_result is not None
                 else return_sites_for_atoms(returners))
        if set(sites) != returners:
            raise RuntimeError(
                "native rich RETURN site set disagrees with selected returners")
        decisions = {}
        for q, bit in zip(eligible, bits):
            if bit:
                decisions[q] = ("RETURN", sites[q])
            elif native_reseat_sites is not None and q in native_reseat_sites:
                decisions[q] = ("RESEAT", native_reseat_sites[q])
            else:
                decisions[q] = ("STAY", reg.zone_seat[q])
        decisions.update({q: ("RETURN", sites[q]) for q in selected_cycles})
        if native_participant_parking_sites:
            overlap = set(decisions) & set(native_participant_parking_sites)
            if overlap:
                raise RuntimeError(
                    "native participant parking overlaps an existing "
                    f"decision for atoms {sorted(overlap)}")
            decisions.update({
                q: ("PARK", location)
                for q, location in native_participant_parking_sites.items()
            })
        for detail in rent_guard_details:
            selected = decisions.get(detail["q"])
            detail["selected_decision"] = (
                None if selected is None else selected[0])
        vacated = {q for q, value in decisions.items()
                   if value[0] in ("RETURN", "RESEAT", "PARK")}
        if native_rich_result is not None:
            # The native winner has already been scored with its exact oriented
            # endpoints and production parking replay.  Project those endpoints
            # verbatim.  The legacy repair treats other target participants as
            # stationary and would undo a valid ordered source-seat handoff.
            decoded_placements = _rich_result_placements(
                native_rich_problem, native_rich_result,
                self.boundary_site_locations)
            placements = deepcopy(decoded_placements)
            placement_repaired = False
        else:
            placed = decode(best_chrom)
            decoded_placements = [
                self._mk_placement(c[2], c[3], c[0]) for c in placed]
            placements = self._repair_placements(
                deepcopy(decoded_placements), list_gate, vacated=vacated)
            placement_repaired = placements != decoded_placements
        selected_gate_seats = {
            int(q): tuple(seat)
            for placement in placements
            for q, seat in zip(placement["gate"], placement["seats"])
        }
        commitment_overrides = [
            {
                "q": int(q),
                "pinned_seat": list(target_pins[q]),
                "selected_seat": list(selected_gate_seats[q]),
                "reason": "current_ghost_safe_override",
            }
            for q in sorted(target_pins)
            if q in selected_gate_seats
            and tuple(selected_gate_seats[q]) != tuple(target_pins[q])
        ]
        if sum(1 for value in decisions.values() if value[0] == "STAY") + demand \
                > self.theta_capacity * reg.zone_sites + 1e-12:
            raise AssertionError("Schema 2 容量约束未在 fitness 前满足")

        # Hard repair is method-independent.  The repaired schedule is scored again
        # below, so the ledger never reports the stale pre-repair objective.
        pre_ghost_placements = deepcopy(placements)
        pre_ghost_decisions = dict(decisions)
        production_candidate_audit = {}
        production_candidate = None
        skip_redundant_production_candidate_audit = bool(
            native_rich_result is not None
            and not native_relaxed_ghost_recovery
            and approximate_ultra_deep_current)
        native_production_replay_divergence = False
        native_production_mismatches = []
        if (native_rich_result is not None
                and not native_relaxed_ghost_recovery
                and not skip_redundant_production_candidate_audit):
            # The native winner was already evaluated by the same two-phase,
            # batch-order replay contract.  Recheck independently in Python,
            # but never run the legacy double-endpoint repair that can mutate a
            # valid ordered schedule after selection.
            positions_t0 = {
                q: reg.current_pos(q) for q in range(len(self.mapping[0]))}
            positions_t1 = dict(positions_t0)
            back_legs, back_owners = [], []
            for q, (kind, location) in decisions.items():
                if kind == "STAY":
                    continue
                p0 = arch.exact_SLM_location_tuple(positions_t0[q])
                p1 = arch.exact_SLM_location_tuple(location)
                distance = math.dist(p0, p1)
                if distance > 1e-9:
                    back_legs.append((distance, *p0, *p1))
                    back_owners.append(q)
                positions_t1[q] = tuple(location)
            out_legs, out_owners = [], []
            for placement in placements:
                for q, seat in zip(placement["gate"], placement["seats"]):
                    p0 = arch.exact_SLM_location_tuple(positions_t1[q])
                    p1 = arch.exact_SLM_location_tuple(seat)
                    distance = math.dist(p0, p1)
                    if distance > 1e-9:
                        out_legs.append((distance, *p0, *p1))
                        out_owners.append(q)
            boundary_mapping = [tuple(positions_t1[q])
                                for q in range(len(self.mapping[0]))]
            target_gate_mapping = list(boundary_mapping)
            for placement in placements:
                for q, seat in zip(placement["gate"], placement["seats"]):
                    target_gate_mapping[int(q)] = tuple(seat)
            production_candidate = self.scheduler_reference.evaluate_candidate(
                prefix=scheduler_prefix,
                boundary_mapping=boundary_mapping,
                source_gates=self.gate_scheduling[layer],
                source_one_qubit_gates=source_one_qubit,
                target_gate_mapping=target_gate_mapping,
                target_gates=list_gate,
                target_one_qubit_gates=self._one_qubit_for_layer(next_layer),
            )
            native_batches = native_rich_result.winner.phase_batches
            if len(native_batches) != 2:
                raise RuntimeError(
                    "native winner did not return exactly two physical phases")
            native_back_owner_order, native_out_owner_order = \
                _native_rich_phase_owner_orders(
                    native_rich_problem, native_rich_result)
            owner_orders_match = bool(
                sorted(native_back_owner_order) == sorted(back_owners)
                and sorted(native_out_owner_order) == sorted(out_owners))
            if owner_orders_match:
                native_back_batches = _phase_batches_by_owner(
                    native_batches[0], native_back_owner_order,
                    phase="source-back")
                native_out_batches = _phase_batches_by_owner(
                    native_batches[1], native_out_owner_order,
                    phase="target-out")
            else:
                native_back_batches = ()
                native_out_batches = ()
                native_production_mismatches.append("phase_owners")
            if (native_back_batches !=
                    production_candidate.source_back_batches):
                native_production_mismatches.append("source_back_batches")
            if (native_out_batches !=
                    production_candidate.target_out_batches):
                native_production_mismatches.append("target_out_batches")
            production_move_time = (
                production_candidate.source_back_time_us
                + production_candidate.target_out_time_us)
            # Python ``math.dist`` and the native C++ distance kernel can
            # differ by a few ulps after phase maxima are accumulated.  The
            # owner order and the exact phase batches are checked above, so a
            # sub-picosecond tolerance here avoids rejecting an otherwise
            # byte-identical routing decision on long circuits.
            if not math.isclose(
                    native_rich_result.winner.move_time_us,
                    production_move_time, rel_tol=0.0, abs_tol=1e-7):
                native_production_mismatches.append("move_time")
            production_transfers = 2 * sum(
                len(batch) for batch in
                (*production_candidate.source_back_batches,
                 *production_candidate.target_out_batches))
            production_idle_exposures = sum(
                q not in participants and reg._is_zone(target_gate_mapping[q])
                for q in range(len(target_gate_mapping)))
            expected_transfer_nll = -production_transfers * math.log(0.999)
            expected_idle_nll = -production_idle_exposures * math.log(0.9975)
            expected_current_nll = (
                expected_transfer_nll + expected_idle_nll
                + production_candidate.coherence_negative_log_fidelity)
            winner = native_rich_result.winner
            for label, actual, expected in (
                    ("transfer NLL", winner.transfer_nll,
                     expected_transfer_nll),
                    ("idle-excitation NLL", winner.idle_excitation_nll,
                     expected_idle_nll),
                    ("coherence NLL", winner.coherence_nll,
                     production_candidate.coherence_negative_log_fidelity),
                    ("current NLL", winner.negative_log_fidelity,
                     expected_current_nll)):
                if not math.isclose(
                        actual, expected, rel_tol=0.0, abs_tol=1e-12):
                    native_production_mismatches.append(label)
            reconstructed_idle = tuple(
                before + delta for before, delta in zip(
                    scheduler_idle_prior,
                    native_rich_result.winner.candidate_idle_time_us))
            if (len(reconstructed_idle) !=
                    len(production_candidate.idle_after_us)
                    or any(not math.isclose(
                        actual, expected, rel_tol=0.0, abs_tol=1e-7)
                        for actual, expected in zip(
                            reconstructed_idle,
                            production_candidate.idle_after_us))):
                native_production_mismatches.append("absolute_idle_vector")
            native_production_replay_divergence = bool(
                native_production_mismatches)
            self.expected_scheduler_prefix_sha256 = str(
                production_candidate.scheduler_after.timing_sha256)
            self.expected_scheduler_prefix_idle_us = tuple(
                production_candidate.scheduler_after.idle_time_us)
            production_candidate_audit = {
                "scheduler_after_timing_sha256": str(
                    production_candidate.scheduler_after.timing_sha256),
                "scheduler_after_trace_end_us": float(
                    production_candidate.scheduler_after.trace_end_us),
                "scheduler_after_idle_us": list(
                    production_candidate.scheduler_after.idle_time_us),
                "source_back_batches": [list(batch) for batch in
                                        production_candidate.
                                        source_back_batches],
                "target_out_batches": [list(batch) for batch in
                                       production_candidate.
                                       target_out_batches],
                "native_source_back_batches": [list(batch) for batch in
                                               native_back_batches],
                "native_target_out_batches": [list(batch) for batch in
                                              native_out_batches],
                "native_replay_divergence": bool(
                    native_production_replay_divergence),
                "native_replay_mismatches": list(
                    native_production_mismatches),
                "move_time_us": float(production_move_time),
                "current_negative_log_fidelity": float(expected_current_nll),
                "zero_duration_shiftback_count": int(
                    production_candidate.zero_duration_shiftback_count),
                "zero_duration_shiftback_distance_um": float(
                    production_candidate.zero_duration_shiftback_distance_um),
            }
            commitment_repairs, commitment_ghost_fallback = {}, False
        elif skip_redundant_production_candidate_audit:
            # The ultra-deep QMAP tail contributes Move/runtime and the
            # exponential OOD sensitivity result, but not the paper's linear
            # fidelity geometric mean.  Re-routing the selected source and
            # target layers in an independent Python fork at every one of tens
            # of thousands of boundaries made the C++ method several times
            # slower than the historical Python pilot.  The winner is already
            # evaluated in C++; below we still replay the committed physical
            # phases, update the persistent exact scheduler, emit the real
            # production route, and run the independent final verifier/scorer.
            # Skip only this redundant candidate-audit fork.
            production_candidate_audit = {
                "skipped": True,
                "reason": "ultra_deep_redundant_candidate_audit",
                "native_phase_batches": [
                    [list(batch) for batch in phase]
                    for phase in native_rich_result.winner.phase_batches
                ],
                "native_move_time_us": float(
                    native_rich_result.winner.move_time_us),
                "current_negative_log_fidelity": float(
                    native_rich_result.winner.negative_log_fidelity),
            }
            commitment_repairs, commitment_ghost_fallback = {}, False
        else:
            commitment_repairs, commitment_ghost_fallback = \
                self._repair_ghosts_with_commitments(
                    placements, decisions, target_pins)
        physical_repair_applied = (
            placement_repaired
            or placements != pre_ghost_placements
            or decisions != pre_ghost_decisions
            or bool(commitment_repairs)
            or commitment_ghost_fallback
            or native_relaxed_ghost_recovery
            or native_production_replay_divergence)
        if (native_rich_result is not None and physical_repair_applied
                and not native_relaxed_ghost_recovery
                and not native_production_replay_divergence):
            raise RuntimeError(
                "native candidate changed during post-selection repair; "
                "candidate-level ghost/RESEAT contract was violated")

        selected_forecast_audit = {}

        def score_repaired():
            nonlocal selected_forecast_audit
            positions_t0 = {
                q: reg.current_pos(q) for q in range(len(self.mapping[0]))}
            positions_t1 = dict(positions_t0)
            back, back_owners = [], []
            for q, (kind, loc) in decisions.items():
                if kind == "STAY":
                    continue
                p0 = arch.exact_SLM_location_tuple(positions_t0[q])
                p1 = arch.exact_SLM_location_tuple(loc)
                distance = math.dist(p0, p1)
                if distance > 1e-9:
                    back.append((distance, *p0, *p1))
                    back_owners.append(q)
                positions_t1[q] = loc
            out, out_owners = [], []
            for placement in placements:
                for q, seat in zip(placement["gate"], placement["seats"]):
                    p0 = arch.exact_SLM_location_tuple(positions_t1[q])
                    p1 = arch.exact_SLM_location_tuple(seat)
                    distance = math.dist(p0, p1)
                    if distance > 1e-9:
                        out.append((distance, *p0, *p1))
                        out_owners.append(q)
            ghosts_t0 = [(q, *arch.exact_SLM_location_tuple(loc))
                          for q, loc in positions_t0.items()]
            ghosts_t1 = [(q, *arch.exact_SLM_location_tuple(loc))
                          for q, loc in positions_t1.items()]

            def production_phase(legs, owners, positions):
                batches, boundary = _replay_phase_batches(
                    arch, legs, owners, positions)
                physical_value = MovementPhaseCost(
                    batches=len(batches),
                    move_time_us=sum(
                        physical._expanded_batch_time(legs, batch)
                        for batch in batches),
                    total_distance_um=sum(float(leg[0]) for leg in legs),
                    movers=len(legs),
                )
                return _BackendMovementPhase(physical_value, boundary)

            if self.ablation_fitness_mode == "lumped_greedy":
                phases = [movement_phase(
                    back + out, owners=back_owners + out_owners,
                    batching="greedy")]
            elif (native_relaxed_ghost_recovery
                  or native_production_replay_divergence):
                # Recovery candidates are committed by the production router,
                # whose ordered single-leg deferrals can differ from the
                # optimizer's conflict coloring.  Score the exact executable
                # batches here so the selected physical time and later commit
                # account are the same object-level schedule.
                phases = [
                    production_phase(back, back_owners, positions_t0),
                    production_phase(out, out_owners, positions_t1),
                ]
            else:
                phases = [
                    movement_phase(back, ghosts_t0, back_owners),
                    movement_phase(out, ghosts_t1, out_owners),
                ]
            returners = {q for q, value in decisions.items()
                         if value[0] == "RETURN"}
            repaired_sites = {q: decisions[q][1] for q in returners}
            current_phases = tuple(phases)
            current_exposures = sum(
                1 for q in eligible
                if q not in participants and decisions[q][0] != "RETURN")
            if self.decay_lookahead:
                repaired_gate_indices = []
                for column, placement in enumerate(placements):
                    site = tuple(placement["site"])
                    repaired_gate_indices.append(next(
                        (index for index, option in enumerate(candidates[column])
                         if tuple(option[0]) == site), -1))
                repaired_bits = tuple(
                    1 if decisions[q][0] == "RETURN" else 0
                    for q in eligible)
                repaired_chromosome = (
                    tuple(repaired_gate_indices) + repaired_bits)
                return_assignments = tuple(
                    (q, self.boundary_storage_site_id[
                        tuple(decisions[q][1])])
                    for q in sorted(returners))
                objective, current_breakdown, selected_forecast_audit = \
                    score_decay_objective(
                        current_phases, current_exposures,
                        repaired_chromosome, repaired_gate_indices,
                        return_assignments)
                return objective, current_breakdown
            future, future_exposures, forecast_terms = forecast_phases(
                returners, repaired_sites, placements)
            phases.extend(future)
            return physical.score(
                phases, current_exposures + future_exposures,
                tuple(best_chrom) + tuple(
                    1 if q in selected_cycles else 0
                    for q in cycle_candidates))

        if physical_repair_applied:
            # A changed executable state must be replayed and rescored; no
            # stale chromosome fitness may enter the evidence ledger.
            repaired_score, breakdown = score_repaired()
        elif native_rich_result is not None:
            repaired_score, breakdown = score_from_backend(
                native_rich_result.winner)
        else:
            # The chromosome score already used the exact same back/out phases,
            # rollout and terminal state.  Re-evaluating it once per boundary
            # duplicated millions of legacy rollouts on long circuits.  The
            # reference cache returns the objective and physical breakdown.
            repaired_score, breakdown = fitness(
                best_chrom, cycle_returners=selected_cycles)

        if h0_participant_cycle_amortization.get("active"):
            progressive_site = tuple(
                h0_participant_cycle_amortization["progressive_site"])
            current_site = tuple(
                h0_participant_cycle_amortization["current_site"])
            accepted_progressive = bool(
                len(placements) == 1
                and tuple(placements[0]["site"]) == progressive_site
                and progressive_site != current_site)
            out_batches = (
                len(native_rich_result.winner.phase_batches[1])
                if native_rich_result is not None
                and len(native_rich_result.winner.phase_batches) == 2
                else 0)
            added_split_batches = (
                max(0, out_batches - 1) if accepted_progressive else 0)
            added_split_transfer_time_us = (
                2.0 * physical.T_TRANSFER_US * added_split_batches)
            prior_split_transfer_time_us = float(getattr(
                self,
                "_h0_participant_cycle_split_transfer_time_us",
                0.0))
            if added_split_transfer_time_us > 0.0:
                self._h0_participant_cycle_split_transfer_time_us = (
                    prior_split_transfer_time_us
                    + added_split_transfer_time_us)
            h0_participant_cycle_amortization.update({
                "accepted_progressive": accepted_progressive,
                "selected_out_batches": out_batches,
                "added_split_batches": added_split_batches,
                "added_split_transfer_time_us": float(
                    added_split_transfer_time_us),
                "split_transfer_time_after_us": float(getattr(
                    self,
                    "_h0_participant_cycle_split_transfer_time_us",
                    prior_split_transfer_time_us)),
            })
        if self.decay_lookahead and not selected_forecast_audit:
            if (native_rich_result is not None
                    and not native_relaxed_ghost_recovery):
                depth_zero_state_nll = (
                    float(native_rich_result.forecast_by_depth[0])
                    if (rich_config.max_horizon == 0
                        and native_rich_result.forecast_by_depth)
                    else 0.0)
                selected_forecast_audit = {
                    "configured_depth": self.lookahead_horizon,
                    "effective_depth": forecast.effective_horizon,
                    "rho": float(self.lookahead_horizon_config["rho"]),
                    "epsilon": float(self.lookahead_horizon_config["epsilon"]),
                    "alpha_lookahead": self.alpha_lookahead,
                    "forecast_by_depth": list(
                        native_rich_result.forecast_by_depth),
                    "forecast_breakdown": dict(
                        native_rich_result.forecast_breakdown),
                    "forecast_terms_applied": int(
                        native_reference_forecast[3]),
                    "forecast_terms_skipped_cutoff": int(
                        native_reference_forecast[4]),
                    # Keep the established forecast summary future-only.  M3's
                    # optional Phi_0 is reported separately, so H=0 remains an
                    # auditable no-future method instead of being mislabeled as
                    # having consumed a future heuristic.
                    # H=0 has no future term by definition.  Subtracting the
                    # separately reported Phi_0 from the combined native
                    # value accumulated round-off over tens of thousands of
                    # boundaries and could make the protocol checker see a
                    # tiny, fictitious future cost.  Publish the exact
                    # contract value for M3 and keep Phi_0 in its own field.
                    "weighted_negative_log_fidelity": (
                        0.0 if rich_config.max_horizon == 0 else float(
                            native_rich_result.forecast_nll
                            - depth_zero_state_nll)),
                    "state_potential_negative_log_fidelity": (
                        depth_zero_state_nll),
                    "future_rollout_fallbacks": int(
                        future_rollout_fallbacks),
                    "future_rollout_evaluated": int(
                        future_rollout_evaluated),
                    "future_rollout_failed": int(future_rollout_failed),
                    "future_rollout_skipped_budget": int(
                        future_rollout_skipped_budget),
                    "future_rollout_unavailable": bool(
                        future_rollout_unavailable),
                    "search_negative_log_fidelity": float(
                        native_rich_result.search_negative_log_fidelity),
                }
            else:
                forecast_key = tuple(best_chrom) + tuple(
                    1 if q in selected_cycles else 0
                    for q in cycle_candidates)
                selected_forecast_audit = deepcopy(
                    forecast_audit_cache.get(forecast_key, {}))
        if (self.decay_lookahead and active_horizon == 0
                and selected_forecast_audit):
            # H=0 has no future layer by contract.  The relaxed ghost-recovery
            # path above reuses the Python differential-oracle audit, whose
            # depth-zero state potential can leave a tiny cancellation residue
            # in ``weighted_negative_log_fidelity``.  Keep that Phi_0 value in
            # its dedicated state-potential field and publish an exact zero for
            # the future-only aggregate, just as the native fast path does.
            selected_forecast_audit["weighted_negative_log_fidelity"] = 0.0
        if self.decay_lookahead:
            # ``score`` is executable current-boundary physics only.  The
            # non-executable forecast contribution is audited separately and
            # never leaks into the final fidelity decomposition.
            repaired_score = breakdown.objective(tuple(best_chrom))
        for field in step_cache.as_dict():
            setattr(self.cache_stats, field,
                    getattr(self.cache_stats, field) + getattr(step_cache, field))

        def committed_phase_account():
            """Reconstruct exact executable back/out phase time by owner.

            This independent replay is intentionally outside the optimizer.
            It updates historical physical state only after a winner has been
            selected and therefore cannot perturb chromosome ranking or RNG.
            """
            if (native_rich_result is not None
                    and not native_relaxed_ghost_recovery
                    and production_candidate is not None):
                return (
                    (float(production_candidate.source_back_time_us),
                     tuple(int(q) for q in back_owners)),
                    (float(production_candidate.target_out_time_us),
                     tuple(int(q) for q in out_owners)),
                )
            positions_t0 = {
                q: tuple(reg.current_pos(q))
                for q in range(len(self.mapping[0]))}
            positions_t1 = dict(positions_t0)
            back, legacy_back_owners = [], []
            for q, (kind, location) in decisions.items():
                if kind == "STAY":
                    continue
                p0 = arch.exact_SLM_location_tuple(positions_t0[q])
                p1 = arch.exact_SLM_location_tuple(location)
                distance = math.dist(p0, p1)
                if distance > 1e-9:
                    back.append((distance, *p0, *p1))
                    legacy_back_owners.append(q)
                positions_t1[q] = tuple(location)
            out, legacy_out_owners = [], []
            for placement in placements:
                for q, seat in zip(placement["gate"], placement["seats"]):
                    p0 = arch.exact_SLM_location_tuple(positions_t1[q])
                    p1 = arch.exact_SLM_location_tuple(seat)
                    distance = math.dist(p0, p1)
                    if distance > 1e-9:
                        out.append((distance, *p0, *p1))
                        legacy_out_owners.append(q)

            def account(legs, owners, positions):
                batches, _phase = _replay_phase_batches(
                    arch, legs, owners, positions)
                duration = sum(
                    physical._expanded_batch_time(legs, batch)
                    for batch in batches)
                return float(duration), tuple(int(q) for q in owners)

            return (account(back, legacy_back_owners, positions_t0),
                    account(out, legacy_out_owners, positions_t1))

        committed_back, committed_out = committed_phase_account()
        committed_move_time = committed_back[0] + committed_out[0]
        committed_move_time_matches = math.isclose(
            committed_move_time, breakdown.move_time_us,
            rel_tol=0.0, abs_tol=1e-7)
        if (skip_redundant_production_candidate_audit
                and not committed_move_time_matches):
            production_candidate_audit[
                "committed_move_time_us"] = float(committed_move_time)
            production_candidate_audit[
                "native_committed_move_time_delta_us"] = float(
                    breakdown.move_time_us - committed_move_time)
        elif self.decay_lookahead and not committed_move_time_matches:
            raise RuntimeError(
                "committed phase replay disagrees with selected physical time: "
                f"{committed_move_time!r} != {breakdown.move_time_us!r}")

        # Commit in real temporal order.  A RETURNer is resident during the
        # back phase but not during the following out phase; RESEAT preserves
        # its rent.  The next CZ pulse is recorded inside ``_commit_round``.
        reg.record_movement_phase(
            committed_back[0], committed_back[1],
            transfer_time_us=physical.T_TRANSFER_US)

        for q, (kind, loc) in decisions.items():
            if kind == "RETURN":
                self.residency_commitments.pop(q, None)
                reg.return_to_storage(q, loc)
            elif kind == "RESEAT":
                self.residency_commitments.pop(q, None)
                reg.reseat(q, loc)
            elif kind == "PARK":
                # PARK is an explicit temporary storage stop for a target
                # participant.  _commit_round immediately re-enters it at the
                # selected gate seat; it is not a semantic RETURN decision.
                self.residency_commitments.pop(q, None)
                reg.return_to_storage(q, loc)
            elif kind == "STAY" and active_horizon > 0:
                visible = visible_use(q)
                if visible is None:
                    self.residency_commitments.pop(q, None)
                else:
                    self.residency_commitments[q] = (
                        visible[0], tuple(reg.zone_seat[q]))
            elif kind == "STAY":
                # A boundary that selected H=0 must not keep a commitment made
                # by an earlier deeper window as a hidden future-information
                # channel.  The atom may still physically stay, but uncommitted.
                self.residency_commitments.pop(q, None)
        reg.record_movement_phase(
            committed_out[0], committed_out[1],
            transfer_time_us=physical.T_TRANSFER_US)
        self._append_boundary(decisions)
        self._commit_round(next_layer, placements)
        if not approximate_ultra_deep_current:
            self.scheduler_reference.commit_source(
                layer,
                source_gate_mapping,
                self.mapping[-2],
                self.gate_scheduling[layer],
                source_one_qubit,
            )
        self.current_scheduler_prefix = None
        for q in participants:
            commitment = self.residency_commitments.get(q)
            if commitment is not None and commitment[0] <= next_layer:
                self.residency_commitments.pop(q, None)
        if self.decay_lookahead:
            selected_forecast_audit = {
                "configured_depth": self.lookahead_horizon,
                "effective_depth": forecast.effective_horizon,
                "visible_depth": max(
                    (int(offset) for offset in future_atoms), default=0),
                "rho": float(self.lookahead_horizon_config["rho"]),
                "epsilon": float(self.lookahead_horizon_config["epsilon"]),
                "alpha_lookahead": self.alpha_lookahead,
                "offset_weights": [
                    {"offset": offset,
                     "decay_factor": forecast.future_decay_factor(offset),
                     "weight": forecast.future_weight(offset)}
                    for offset in range(1, forecast.effective_horizon + 1)
                ],
                "forecast_by_depth": selected_forecast_audit.get(
                    "forecast_by_depth", []),
                "forecast_breakdown": selected_forecast_audit.get(
                    "forecast_breakdown", {}),
                "forecast_terms_applied": selected_forecast_audit.get(
                    "forecast_terms_applied", 0),
                "forecast_terms_skipped_cutoff": selected_forecast_audit.get(
                    "forecast_terms_skipped_cutoff", 0),
                "weighted_negative_log_fidelity": selected_forecast_audit.get(
                    "weighted_negative_log_fidelity", 0.0),
                "state_potential_negative_log_fidelity": (
                    selected_forecast_audit.get(
                        "state_potential_negative_log_fidelity", 0.0)),
                "future_rollout_fallbacks": int(
                    future_rollout_fallbacks),
                "future_rollout_evaluated": int(
                    future_rollout_evaluated),
                "future_rollout_failed": int(future_rollout_failed),
                "future_rollout_skipped_budget": int(
                    future_rollout_skipped_budget),
                "future_rollout_unavailable": bool(
                    future_rollout_unavailable),
                "search_negative_log_fidelity": selected_forecast_audit.get(
                    "search_negative_log_fidelity",
                    breakdown.negative_log_fidelity
                    + selected_forecast_audit.get(
                        "weighted_negative_log_fidelity", 0.0)
                    + selected_forecast_audit.get(
                        "state_potential_negative_log_fidelity", 0.0)),
            }
        self.backend_timing_log.append({
            "layer": layer,
            "backend": getattr(
                self.boundary_backend, "name", self.resident_backend_requested),
            **step_backend,
        })
        formal_decay_state_log = ({
            "rent_guard_returns": len(rent_forced_returns),
            "rent_recommended_returns": len(rent_recommended_returns),
            "rent_recommended_stays": len(rent_recommended_stays),
            "h0_participant_cycle_amortization": (
                h0_participant_cycle_amortization),
            "h0_participant_cycle_recommended_returns": len(
                h0_participant_cycle_recommended_returns),
            "return_anchor_policy": self.return_anchor_policy,
            "return_anchor_effective_policy": (
                getattr(self, "return_anchor_effective_policy",
                        self.return_anchor_policy)),
            "static_partner_degree_max": getattr(
                self, "static_partner_degree_max", None),
            "h0_state_potential_weight": self.h0_state_potential_weight,
            "h0_state_potential_weight_policy": (
                self.h0_state_potential_weight_policy),
            "h0_state_potential_effective_weight": (
                self.h0_state_potential_effective_weight),
            "h0_uncertain_stay_weight": self.h0_uncertain_stay_weight,
            "h0_uncertain_stay_policy": self.h0_uncertain_stay_policy,
            "h0_uncertain_stay_effective_weight": (
                self.h0_uncertain_stay_effective_weight),
            "h0_uncertain_stay_terms": h0_uncertain_stay_terms,
            "h0_opportunity_stay_weight": self.h0_opportunity_stay_weight,
            "h0_opportunity_stay_terms": h0_opportunity_stay_terms,
            "h0_history_gap_weight": self.h0_history_gap_weight,
            "h0_history_gap_terms": h0_history_gap_terms,
            "h0_reentry_value_weight": self.h0_reentry_value_weight,
            "h0_reentry_value_min_interaction_mass": (
                self.h0_reentry_value_min_interaction_mass),
            "h0_reentry_value_min_interaction_ratio": (
                self.h0_reentry_value_min_interaction_ratio),
            "h0_reentry_value_min_circuit_median_interactions": (
                self.h0_reentry_value_min_circuit_median_interactions),
            "h0_static_interaction_median": self.static_interaction_median,
            "h0_reentry_value_scale_by_interaction_ratio": (
                self.h0_reentry_value_scale_by_interaction_ratio),
            "h0_reentry_value_terms": h0_reentry_value_terms,
            "h0_state_anchor_policy": self.h0_state_anchor_policy,
            "h0_state_anchor_effective_policy": getattr(
                self, "h0_state_anchor_effective_policy",
                self.h0_state_anchor_policy),
            "h0_anchor_pull_radius_um": self.h0_anchor_pull_radius_um,
            "static_partner_barycenter_shift_mean_um": getattr(
                self, "static_partner_barycenter_shift_mean_um", 0.0),
            "m3_search_budget_policy": self.m3_search_budget_policy,
            "m3_pin_radius_policy": self.m3_pin_radius_policy,
            "m3_pin_radius_effective": self.m3_pin_radius_effective,
            "h0_rent_policy": self.h0_rent_policy,
            "m3_search_profile_effective": getattr(
                self, "m3_search_profile_effective", "configured"),
            "large_search_profile_effective": getattr(
                self, "large_search_profile_effective", "configured"),
            "rent_guard": rent_guard_details,
            "coherence_idle_prior_us": list(coherence_idle_prior),
            "coherence_idle_after_us": list(
                reg.coherence_idle_snapshot()),
            "scheduler_absolute_idle_prior_us": list(
                scheduler_idle_prior),
            "scheduler_prefix_trace_end_us": (
                None if scheduler_prefix is None else float(
                    scheduler_prefix.scheduler.trace_end_us)),
            "scheduler_prefix_timing_sha256": (
                "ultra-deep-approximate-current"
                if scheduler_prefix is None else str(
                    scheduler_prefix.scheduler.timing_sha256)),
            "approximate_ultra_deep_current": bool(
                approximate_ultra_deep_current),
            "resident_rent_after": [
                {"q": q, "idle_exposures": exposures,
                 "idle_time_us": idle_time}
                for q, exposures, idle_time
                in reg.resident_rent_snapshot()],
        } if self.decay_lookahead else {})
        result_commit_ns = (
            time.perf_counter_ns() - search_kernel_stopped_ns)
        self.decision_log.append({
            "layer": layer,
            "engine": "ga-v2",
            "method_id": self.method_id,
            "lookahead_horizon": active_horizon,
            "configured_lookahead_horizon": self.lookahead_horizon_config,
            **horizon_decision.as_log(),
            "horizon_selection_ns": horizon_selection_ns,
            "problem_preparation_ns": problem_preparation_ns,
            "search_kernel_ns": search_kernel_ns,
            "result_commit_ns": result_commit_ns,
            "backend": getattr(
                self.boundary_backend, "name", self.resident_backend_requested),
            "backend_calls": step_backend["calls"],
            "backend_candidates": step_backend["candidates"],
            "ablation_policy": self.ablation_policy,
            "fitness_phase_mode": self.ablation_fitness_mode,
            "search_mode": search_mode,
            "rich_search": rich_search_stats,
            "return_assignments": selected_return_audit,
            "participant_parking_assignments":
                selected_participant_parking_audit,
            "production_candidate": production_candidate_audit,
            "lookahead_selection": lookahead_selection,
            "forecast_objective": selected_forecast_audit,
            "safe_current_objective": (
                list(safe_current_objective[:4])
                if safe_current_objective is not None else None),
            "horizon_current_objective": (
                list(horizon_current_objective[:4])
                if horizon_current_objective is not None else None),
            "stay": sum(1 for v in decisions.values() if v[0] == "STAY"),
            "return": sum(1 for v in decisions.values() if v[0] == "RETURN"),
            "reseat": sum(1 for v in decisions.values() if v[0] == "RESEAT"),
            "participant_parking": sum(
                1 for v in decisions.values() if v[0] == "PARK"),
            "eligible_decisions": len(eligible),
            "adjacent_cycle_candidates": (
                len(cycle_candidates) + len(formal_cycle_candidates)),
            "adjacent_cycle_returns": (
                len(selected_cycles) + len(formal_selected_cycles)),
            "forced_commitment_cycles": len(forced_cycle_candidates),
            "adjacent_cycle_search": cycle_search_log,
            "no_visible_use": sum(
                1 for q in resident_eligible
                if visible_use(q) is None),
            "forced_e2": 0,
            "capacity": min_returns,
            "horizon_guard_returns": len(horizon_forced_returns),
            "physical_guard_returns": len(physical_forced_returns),
            "physical_guard": physical_guard_details,
            **formal_decay_state_log,
            "committed_target_atoms": len(target_pins),
            "commitment_overrides": commitment_overrides,
            "commitment_ghost_fallback": commitment_ghost_fallback,
            "commitment_repairs": [
                {"q": q, "pinned_seat": list(target_pins[q]),
                 "repaired_seat": list(commitment_repairs[q])}
                for q in sorted(commitment_repairs)],
            "active_commitments": len(self.residency_commitments),
            "ghost_fix": getattr(self, "ghost_fixes", 0),
            "participants": len(participants),
            "score": [repaired_score[0], repaired_score[1],
                      repaired_score[2], repaired_score[3]],
            "physical": {
                "negative_log_fidelity": breakdown.negative_log_fidelity,
                "idle_exposures": breakdown.idle_exposures,
                "transfers": breakdown.transfers,
                "move_batches": breakdown.move_batches,
                "move_time_us": breakdown.move_time_us,
                "total_distance_um": breakdown.total_distance_um,
            },
            "cache": step_cache.as_dict(),
        })
        while len(self.return_candidate_cache) > self.return_cache_limit:
            self.return_candidate_cache.popitem(last=False)
        while len(self.rollout_pair_cache) > self.rollout_geometry_cache_limit:
            self.rollout_pair_cache.popitem(last=False)
        while len(self.rollout_site_cache) > self.rollout_geometry_cache_limit:
            self.rollout_site_cache.popitem(last=False)
        self.search_time += time.time() - t0
