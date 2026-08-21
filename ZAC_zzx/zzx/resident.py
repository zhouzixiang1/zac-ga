"""驻留核心（ZAC_zzx 的灵魂部件）：登记簿 + 惰性策略 + RETURN 三方案箱匹配。

对照三个世界的行为（为什么需要这个模块）：
    ZAC 原版   ：每轮做完门，激发区全员强制回存储（vmplacer.py:347 一刀切），
                 只有 collect_reuse_qubit 点名的"下一层复用者"能多留一轮。
    原生编译器 ：不调回（做完门就坐在门位上），只在挡路/测量时逐出，
                 以此在串行电路上大胜（qft 0.59-0.74）。
    ZAC_zzx    ：激发区原子默认驻留；每个边界跑一遍决策：
                 挡路者（座位被下一轮门位需要）与容量超压者强制 RETURN，
                 其余 STAY。RETURN 的落位不是回原位那么简单——按用户笔记
                 :123-131 的三方案（原位/就近/伙伴位）箱式化后最小权匹配。

模块内容：
    NextUse            —— 下次使用表：r(q)=下次参与 2q 门的轮次（调度已知，一次扫描）
    ResidentRegistry   —— 驻留登记簿：谁在激发区哪个座、存储位占用、容量、锚点、拥挤度
    match_return_sites —— RETURN 落位三方案箱匹配（每步一次，结果作常量供缓存）
    decide_lazy        —— A1 惰性策略：默认全留 + E2 挡路逐出 + 容量阀逐出
    boundary_legs      —— 决策 → 回相腿清单（zcost 的 (dist,起x,起y,终x,终y) 格式）

距离单位：全部用 architecture.exact_SLM_location_tuple 的 μm 坐标做欧氏距离，
代价取 √d——与 zcost.batch_cost / ZAC 放置代价同一本账（√μm）。
"""
from __future__ import annotations

import math
from math import sqrt

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import min_weight_full_bipartite_matching


# ---------------------------------------------------------------------- 下次使用表
class NextUse:
    """每个原子的"下次上场时刻"：r(q) 与搭档。ASAP 调度静态可知，零运行时开销。

    例：调度 [[[0,1],[2,3]], [[1,2]], [[0,3]]] 时
        rounds   = {0:[0,2], 1:[0,1], 2:[0,1], 3:[0,2]}
        partner  = {(0,0):1, (1,0):0, (2,0):3, ..., (1,1):2, (2,1):1, ...}
        next_round(1, after=0) = 1   （原子 1 在第 1 轮还有门）
        next_round(2, after=1) = None（第 1 轮之后原子 2 再不上场 → 死驻留者）
    用途：①锚点前瞻（搭档在哪，下次会合成本多少）②容量阀逐出顺序
    （下次使用越远越先走）③决策基因资格（有后续使用者才有 STAY/RETURN 基因）。
    """

    def __init__(self, gate_scheduling: list):
        # rounds[q] = 该原子参与过的轮次（升序）；partner[(q, round)] = 搭档
        self.rounds: dict[int, list[int]] = {}
        self.partner: dict[tuple[int, int], int] = {}
        for layer, gates in enumerate(gate_scheduling):
            for q0, q1 in gates:
                self.rounds.setdefault(q0, []).append(layer)
                self.rounds.setdefault(q1, []).append(layer)
                self.partner[(q0, layer)] = q1
                self.partner[(q1, layer)] = q0
        for q in self.rounds:
            self.rounds[q].sort()

    def next_round(self, q: int, after: int):
        """r(q)：严格晚于 after 的首个参与轮次；无则 None（死驻留者）。
        轮次列表已升序，顺序扫描第一个 > after 的即可（列表很短，无需二分）。"""
        for r in self.rounds.get(q, ()):
            if r > after:
                return r
        return None

    def partner_at(self, q: int, round_: int):
        return self.partner.get((q, round_))

    def has_future_use(self, q: int, after: int) -> bool:
        return self.next_round(q, after) is not None


# ---------------------------------------------------------------------- 驻留登记簿
class ResidentRegistry:
    """跨轮线程化的"谁在哪"账本：激发区座位 + 存储位占用 + 容量 + 锚点。"""

    def __init__(self, architecture, initial_mapping: list, theta_capacity: float = 0.9):
        self.arch = architecture
        self.homes: list = list(initial_mapping)          # mapping[0]：SA/平凡初始布局
        self.zone_seat: dict[int, tuple] = {}             # q -> (slm, r, c) 当前激发区座位
        self.storage_site: dict[int, tuple] = {}          # q -> 当前存储位（初始=原位）
        for q, loc in enumerate(initial_mapping):
            if self._is_zone(loc):
                # SA 初始布局理论上全存储；防御性兜底：罕见异常输入直接按区处理
                self.zone_seat[q] = tuple(loc)
            else:
                self.storage_site[q] = tuple(loc)
        self.theta = theta_capacity
        # 容量（座位数）：纠缠区两片镜像 SLM 的全部座位（一对门位占 2 座）
        self.zone_sites = sum(slm.n_r * slm.n_c
                              for slm in architecture.dict_SLM.values()
                              if slm.entanglement_id != -1)

    # ---- 基本查询 ----
    def _is_zone(self, loc) -> bool:
        return self.arch.dict_SLM[loc[0]].entanglement_id != -1

    def is_resident(self, q: int) -> bool:
        return q in self.zone_seat

    def current_pos(self, q: int) -> tuple:
        """原子当前所在（激发区座位或存储位）。"""
        return self.zone_seat.get(q) or self.storage_site[q]

    def occupancy(self) -> int:
        """激发区占用座位数。"""
        return len(self.zone_seat)

    def occupied_storage(self) -> set:
        return set(self.storage_site.values())

    def congestion(self, demand_seats: int) -> float:
        """第 3 层前瞻·拥挤梯度：占用 + 需求 - θ·容量（正值即超压程度）。"""
        return max(0.0, len(self.zone_seat) + demand_seats - self.theta * self.zone_sites)

    def anchor(self, q: int, after: int, next_use: NextUse):
        """下次使用锚点：搭档当前座位在存储区的投影（搭档在激发区 → 其最近存储位）。

        返回 (anchor_loc 或 None, 轮次距离 dr)；无下次使用 → (None, None)。
        例：原子 q 第 3 轮与 p 配对、p 现坐激发区 (1,4,6) → 锚点 =
        nearest_storage_site(1,4,6)（p 下次大概率从存储侧来会合的落点），
        dr = 3 - after。锚点供 GA 适应度的前瞻层使用（γ0^dr 折现）。"""
        r = next_use.next_round(q, after)
        if r is None:
            return None, None
        p = next_use.partner_at(q, r)
        ploc = self.current_pos(p)
        if self._is_zone(ploc):
            return self.arch.nearest_storage_site(ploc[0], ploc[1], ploc[2]), r - after
        return ploc, r - after

    # ---- 状态更新（由放置器在提交映射时调用）----
    def enter_zone(self, q: int, seat: tuple):
        """原子进入激发区（参与门后驻留在门位）。"""
        self.storage_site.pop(q, None)
        self.zone_seat[q] = tuple(seat)

    def return_to_storage(self, q: int, site: tuple):
        self.zone_seat.pop(q, None)
        self.storage_site[q] = tuple(site)

    def reseat(self, q: int, new_seat: tuple):
        self.zone_seat[q] = tuple(new_seat)


# ---------------------------------------------------------------------- 三方案箱匹配
def _box_sites(arch, center: tuple, ratio: int, free: set) -> list:
    """以 storage 位 center 为中心的 (2ratio+1)^2 自由位箱（裁剪到 SLM 边界）。"""
    slm = arch.dict_SLM[center[0]]
    out = []
    for r in range(max(0, center[1] - ratio), min(slm.n_r, center[1] + ratio + 1)):
        for c in range(max(0, center[2] - ratio), min(slm.n_c, center[2] + ratio + 1)):
            site = (center[0], r, c)
            if site in free:
                out.append(site)
    return out


def match_return_sites(registry: ResidentRegistry, returners: list,
                       next_use: NextUse, after: int,
                       box_ratio: int = 3, alpha_lookahead: float = 0.1,
                       forecast=None, candidate_mode: str = "legacy") -> dict:
    """给一批回返者定存储落位：三方案箱候选 ∪ 自由位 → 最小权完美匹配。

    三方案（笔记 :123-131，ZAC place_qubit 的箱式化沿用 vmplacer.py:443-450 ratio=3）：
        C1 原位族 —— mapping[0] 原位±箱（保证可行：原位被占时箱内还有位）
        C2 就近族 —— 当前激发区座位最近存储位±箱（省本次搬运）
        C3 伙伴族 —— 下次搭档锚点±箱（省未来搬运，vmplacer.py:420 的投影规则）
    代价 = √d(激发区座位→候选位) + alpha·√d(候选位→锚点)   （镜像 vmplacer.py:491）
    匹配一次定终身：调用方把结果当常量缓存，染色体选 RETURN 即用其指派位。

    为什么必须"箱式化"而不能直接用三个点位：ZAC 的 nearest_storage_site
    按（半行,列）塌缩——激发区同一列两个座位会映射到同一个存储位，两个
    回返者的 C2/C3 候选撞车，匹配无解；扩成 (2·box_ratio+1)² 的自由位箱
    后候选池恒够（存储 10000 位 vs ≤98 回返者）。
    """
    if candidate_mode not in ("legacy", "nearest", "forecast"):
        raise ValueError(f"未知 RETURN 候选模式: {candidate_mode!r}")
    arch = registry.arch
    # 全存储位清单 + 自由位集合（未被任何在储原子占用——occupied 含未来
    # 参与者仍在存储的家，所以回返者永远不会落到别人头上）
    all_storage = [
        (sid, r, c) for sid in arch.storage_zone
        for r in range(arch.dict_SLM[sid].n_r)
        for c in range(arch.dict_SLM[sid].n_c)]
    free = set(all_storage) - registry.occupied_storage()    # 未被占用的都可用

    # 二部图：行 = 候选存储位（三族箱并集），列 = 回返者
    site_index: dict = {}
    rows_list: list = []
    rows, cols, data = [], [], []

    def _add(site):
        """给候选位编号（建行索引），重复出现的位共用一行。"""
        if site not in site_index:
            site_index[site] = len(rows_list)
            rows_list.append(site)
        return site_index[site]

    for i, q in enumerate(returners):
        zone_loc = registry.zone_seat[q]                  # 调用保证 q 当前在激发区
        zx, zy = arch.exact_SLM_location_tuple(zone_loc)
        # Schema 2 只能通过 ForecastOracle 读取可见未来。H=0 时 next_use()
        # 必为 None，RETURN 落位因此只看当前位置与最近存储区，不会泄漏搭档。
        anchor_loc = None
        if candidate_mode == "legacy":
            anchor_loc, _ = registry.anchor(q, after, next_use)
            if anchor_loc is None:
                anchor_loc = registry.homes[q]
        elif candidate_mode == "forecast" and forecast is not None:
            visible = forecast.next_use(q, after)
            if visible is not None:
                _, partner = visible
                partner_loc = registry.current_pos(partner)
                anchor_loc = (arch.nearest_storage_site(*partner_loc)
                              if registry._is_zone(partner_loc) else partner_loc)
        if anchor_loc is None:
            anchor_loc = arch.nearest_storage_site(*zone_loc)
        ax, ay = arch.exact_SLM_location_tuple(anchor_loc)

        # C1 原位 / C2 就近 / C3 伙伴 —— 三族候选箱（各以中心±box_ratio 展开）
        near_current = arch.nearest_storage_site(zone_loc[0], zone_loc[1], zone_loc[2])
        if candidate_mode == "nearest":
            families = [near_current]
        elif candidate_mode == "forecast":
            families = [near_current, anchor_loc]
        else:
            families = [registry.homes[q], near_current, anchor_loc]
        candidates = set()
        for center in families:
            if center[0] in arch.storage_zone:
                candidates.update(_box_sites(arch, center, box_ratio, free))
        if candidate_mode == "legacy" and registry.homes[q] in free:
            candidates.add(registry.homes[q])              # 原位自由时永远给一次机会

        # 每个候选位的代价：省本次（离激发区近）+ 省未来（离锚点近）
        for site in candidates:
            sx, sy = arch.exact_SLM_location_tuple(site)
            lookahead_weight = (alpha_lookahead
                                if candidate_mode != "nearest" else 0.0)
            cost = sqrt(math.dist((zx, zy), (sx, sy))) + lookahead_weight * sqrt(
                math.dist((sx, sy), (ax, ay)))
            rows.append(_add(site))
            cols.append(i)
            data.append(cost)

    if not returners:
        return {}
    matrix = coo_matrix((np.array(data), (np.array(rows), np.array(cols))),
                        shape=(len(rows_list), len(returners)))
    try:
        # 最小权完美匹配：所有回返者各得一个互异自由位，总代价最小
        row_ind, col_ind = min_weight_full_bipartite_matching(matrix)
        assignment = {returners[c]: rows_list[r] for r, c in zip(row_ind, col_ind)}
    except ValueError:
        # 保险丝：候选太稀疏导致无完美匹配 → 贪心兜底（按代价升序逐个拿未占位）
        assignment = {}
        taken = set()
        order = sorted(zip(data, rows, cols))
        for w, r, c in order:
            q = returners[c]
            if q in assignment or rows_list[r] in taken:
                continue
            assignment[q] = rows_list[r]
            taken.add(rows_list[r])
        for q in returners:                                # 仍漏的：扫全存储自由位
            if q not in assignment:
                rest = next(s for s in sorted(free) if s not in taken)
                assignment[q] = rest
                taken.add(rest)
    return assignment


# ---------------------------------------------------------------------- A1 惰性策略
def decide_lazy(registry: ResidentRegistry, next_use: NextUse, layer: int,
                next_gates: list, next_gate_seats: list,
                final_return_home: bool = False) -> tuple[dict, dict]:
    """惰性驻留决策（A1）：默认全留；两类强制 RETURN。

        E2 挡路逐出 —— 非参与者驻留者的座位 ∈ 下一轮门位需要的座位集
        容量阀逐出 —— 保留座位 + 2×门数 > θ·区内容量时，按"下次使用轮次降序"
                      （死驻留者=∞ 最先）逐出非参与者直到放下
    参与者本人不动（他的换座就是入区腿，由放置器/路由处理）。

    参数：
        next_gates      —— 下一轮门列表 [[q0,q1],...]（空 = 末边界）
        next_gate_seats —— 每个门选定的座位对 [(s1, s2), ...]（(slm,r,c) 元组）
    返回：
        decisions —— {q: ("STAY", zone_seat) | ("RETURN", storage_site)}
        stats     —— 计数 {stay, forced_e2, capacity, return, participants}
    """
    participants = {q for gate in next_gates for q in gate}
    needed = {seat for pair in next_gate_seats for seat in pair}

    # ---- 末边界：无下一轮 ----
    if not next_gates:
        if final_return_home:
            returners = sorted(registry.zone_seat)
            sites = match_return_sites(registry, returners, next_use, layer)
            decisions = {q: ("RETURN", sites[q]) for q in returners}
            for q in returners:
                registry.return_to_storage(q, sites[q])
            return decisions, {"stay": 0, "forced_e2": 0, "capacity": 0,
                               "return": len(returners), "participants": 0}
        decisions = {q: ("STAY", seat) for q, seat in registry.zone_seat.items()}
        return decisions, {"stay": len(decisions), "forced_e2": 0, "capacity": 0,
                           "return": 0, "participants": 0}

    # ---- E2 挡路逐出（只针对非参与者）----
    # 非参与者 = 本轮不做门的闲住驻留者；他们的座位若被下一轮门位需要，
    # 必须回存储（否则门放不进 / 依赖账本无法排序同相位交接）。
    # 参与者本人的座位被需要不算挡路——他要挪去自己的门位，入区腿自理。
    forced = [q for q, seat in registry.zone_seat.items()
              if q not in participants and seat in needed]
    forced_set = set(forced)

    # ---- 容量阀逐出（下次使用越远越先走；死驻留者最先）----
    # 排序键解读：(是否死驻留者, 下次使用轮次) 都降序——死驻留者（r=None）
    # 排最前先走，活着的按下次使用从远到近逐出，直到 保留+需求 ≤ θ·容量。
    evict_order = sorted(
        (q for q in registry.zone_seat if q not in participants and q not in forced_set),
        key=lambda q: (next_use.next_round(q, layer) is None,
                       next_use.next_round(q, layer) or 0),
        reverse=True)
    retained = len(registry.zone_seat) - len(forced)
    demand = 2 * len(next_gates)                     # 每个门要占一对座位
    capacity_evict = []
    for q in evict_order:
        if retained + demand <= registry.theta * registry.zone_sites:
            break
        capacity_evict.append(q)
        retained -= 1

    returners = sorted(forced + capacity_evict)
    sites = match_return_sites(registry, returners, next_use, layer) if returners else {}

    decisions = {}
    for q, seat in list(registry.zone_seat.items()):     # 快照：循环内要更新登记簿
        if q in sites:
            decisions[q] = ("RETURN", sites[q])
            registry.return_to_storage(q, sites[q])
        else:
            decisions[q] = ("STAY", seat)
    stats = {"stay": sum(1 for v in decisions.values() if v[0] == "STAY"),
             "forced_e2": len(forced), "capacity": len(capacity_evict),
             "return": len(returners), "participants": len(participants)}
    return decisions, stats


def boundary_legs(registry_before: dict, decisions: dict, arch) -> list:
    """决策 → 回相腿清单（zcost 格式 (dist, 起x, 起y, 终x, 终y)，dist=0 不上车）。

    registry_before：决策应用前各搬运决策者的激发区座位 {q: seat}
    （决策后登记簿已变）。RESEAT 是鬼点修补引入的第三种决策——
    驻留者区内让座（激发区 → 激发区），与 RETURN 一样发生在边界相位，
    路由端按映射增量自动带上，无需特殊处理。
    """
    legs = []
    for q, decision in decisions.items():
        if decision[0] not in ("RETURN", "RESEAT"):
            continue
        seat = registry_before[q]
        sx, sy = arch.exact_SLM_location_tuple(seat)
        tx, ty = arch.exact_SLM_location_tuple(decision[1])
        d = math.dist((sx, sy), (tx, ty))
        if d > 1e-9:
            legs.append((d, sx, sy, tx, ty))
    return legs
