"""转接头：ZAC_zzx = ZAC 流水线 + 驻留放置（不放回）+ 图着色分批路由。

三种放置模式（placer 键，回归与消融的对照由此保证）：
    "zac"      —— 原版放置（对照组：等价 ZAC 原行为）
    "batch"    —— ZAC_new 的 BatchAwarePlacer（对照：批次感知但每轮回存储）
    "resident" —— 本文件夹的主角 ResidentPlacer：驻留（不放回）+ 边界决策

改动集中在三处：
    parse_setting             —— 三模式开关 + 驻留旋钮 + 消费断言（防静默丢参的空实验）
    place_qubit_intermedeiate —— 注入 BatchAwarePlacer / ResidentPlacer
    route_qubit_mis           —— coloring 着色分批；resident 模式额外：
                                 ① remain_graph 扩为"有映射增量者"（闲住驻留者的回撤腿）
                                 ② 断言放宽为位置合法性（区内换座 zone→zone 合法）
                                 ③ 依赖账本补丁：回撤腿的非参与者依赖压到本轮门指令之后
                                    （否则陈旧依赖会让搬运与 rydberg/1q 并行——审计 FATAL）
其余工序（ASAP 调度、SA 初始床位、停车让位、AOD 分配、校验）一行不改。
"""
import time
from copy import deepcopy

# import 的是 ZAC_zzx 文件夹内的本地副本（zzx/__init__.py 已把根目录
# 挂到 sys.path 最前），与外层 ZAC/zac 逐字节一致。
from zac.zac import ZAC


from zzx.algorithm_v2 import validate_schema2_setting
from zzx.ghost import ghost_hits
from zzx.zcost import compatible_2d, greedy_phase_batches, phase_batches
from math import hypot


class ZAC_zzx(ZAC):
    """ZAC 的子类：驻留放置 + 图着色分批路由（附 ZAC_new 对照模式）。"""

    # parse_setting 能识别的新旋钮（FABLE 教训：不在白名单的键会被静默丢弃）
    ZAC_ZZX_KEYS = ("engine", "w_batch", "use_lookahead", "min_expand", "seed",
                    "lambda_penalty", "penalty_decay", "max_penalty_iter",
                    "population_size", "iterations", "neighbors_per_solution",
                    "neighbor_sample_size", "coloring_exact_threshold",
                    "coloring_node_budget",
                    # ---- 驻留旋钮 ----
                    "theta_capacity", "box_ratio", "alpha_lookahead",
                    "final_return_home", "stay_horizon",
                    # ---- M3 前瞻/决策层旋钮（提前入白名单防键漂移）----
                    "w_ghost", "w_ord", "gamma_batch", "fitness_mode",
                    "w_resident", "pin_radius", "w_pin",
                    # ---- 正式实验 Schema 2（NL/LK 只允许 horizon 不同）----
                    "experiment_schema", "method_id", "objective",
                    "lookahead_horizon", "fitness_cache",
                    # ---- 初始布局引擎（"ga" = GAInitialPlacer 换掉 ZAC 的 SA）----
                    "init_engine", "init_pop", "init_gens")
    # ZAC 原版认识的键（消费断言用； Zac.parse_setting 同步维护）
    ZAC_KEYS = ("dependency", "routing_strategy", "scheduling", "trivial_placement",
                "dynamic_placement", "use_window", "window_size", "reuse",
                "use_verifier", "l2", "resyn", "name", "dir", "arch_spec",
                "placer", "qasm_list")

    def __init__(self):
        super().__init__()
        self.placer_kind = "zac"          # 默认 "zac"：放置走原版（对照组/回归）
        self.zzx_params: dict = {}       # 放置搜索旋钮
        self.zzx_exact_threshold = 24    # 路由着色：≤此节点数上精确档
        self.zzx_node_budget = 200_000   # 路由着色：精确档节点预算
        self.zzx_route_log: list = []    # 路由账本：每段搬运的 (层, 相位, 批数, 解法)
        self.zzx_placer_preview: list = []   # 放置预演账本（对账用）
        self.zzx_decision_log: list = []     # 驻留决策账本（每层 STAY/RETURN 计数）
        # The phase replay validates batches in emitted route order.  Preserve
        # that order when the single AOD timeline is assigned; duration-based
        # reordering would move previously audited ghosts before/after a batch.
        self.strict_aod_route_order = True
        self._zzx_waypoint_plan: dict = {}

    def _safe_slm_waypoint(self, q, mapping_from, mapping_to, pos):
        """Find one vacant legal SLM site that makes both single-atom legs safe.

        This is a last-resort physical repair for baseline placements whose
        direct single-atom line crosses a stationary atom.  It preserves the
        baseline's requested endpoint and records the real extra load/move/
        store batch instead of silently perturbing a trajectory.
        """
        arch = self.architecture
        start = arch.exact_SLM_location_tuple(tuple(mapping_from[q]))
        end = arch.exact_SLM_location_tuple(tuple(mapping_to[q]))
        occupied = {tuple(value) for atom, value in pos.items() if atom != q}
        ghosts = [(atom, *value) for atom, value in pos.items() if atom != q]
        candidates = []
        preferred = set(getattr(arch, "storage_zone", []))
        for slm_id in sorted(arch.dict_SLM):
            slm = arch.dict_SLM[slm_id]
            for row in range(slm.n_r):
                for column in range(slm.n_c):
                    location = (slm_id, row, column)
                    point = arch.exact_SLM_location_tuple(location)
                    if point in occupied or point == start or point == end:
                        continue
                    distance = hypot(start[0] - point[0], start[1] - point[1])
                    distance += hypot(point[0] - end[0], point[1] - end[1])
                    candidates.append((0 if slm_id in preferred else 1,
                                       distance, location, point))
        for _zone_rank, _distance, location, point in sorted(candidates):
            first = (hypot(start[0] - point[0], start[1] - point[1]),
                     *start, *point)
            second = (hypot(point[0] - end[0], point[1] - end[1]),
                      *point, *end)
            if not ghost_hits([first], ghosts) and not ghost_hits([second], ghosts):
                return location
        return None

    def _process_ghost_safe_movement(self, set_aod, mapping_from, mapping_to):
        """Emit a repaired route and return its number of physical batches."""
        if len(set_aod) == 1:
            q = next(iter(set_aod))
            key = (q, tuple(mapping_from[q]), tuple(mapping_to[q]))
            waypoint = self._zzx_waypoint_plan.pop(key, None)
            if waypoint is not None:
                intermediate = deepcopy(mapping_from)
                intermediate[q] = list(waypoint)
                self.process_movement_layer({q}, mapping_from, intermediate)
                self.process_movement_layer({q}, intermediate, mapping_to)
                self.zzx_ghost_splits = getattr(self, "zzx_ghost_splits", 0) + 1
                return 2
        self.process_movement_layer(set_aod, mapping_from, mapping_to)
        return 1

    def _bind_resident_rydberg_dependencies(self, gate_mapping, first_instruction):
        """Make every atom exposed by a Rydberg pulse depend on that pulse.

        ZAC's original dependency ledger updates only gate participants.  A
        resident idle atom is nevertheless inside the illuminated zone and is
        counted as an excitation exposure; loading it while the pulse is still
        active is physically impossible.  Preserve a later atom-local 1Q
        dependency by taking the largest instruction id.
        """
        for instruction in self.result_json["instructions"][first_instruction:]:
            if instruction.get("type") != "rydberg":
                continue
            zone_id = int(instruction["zone_id"])
            instruction_id = int(instruction["id"])
            for q, location in enumerate(gate_mapping):
                slm = self.architecture.dict_SLM[int(location[0])]
                if slm.entanglement_id == zone_id:
                    self.qubit_dependency[q] = max(
                        self.qubit_dependency[q], instruction_id)

    def parse_setting(self, setting: dict):
        schema = setting.get("experiment_schema")
        if schema is not None and schema != 2:
            raise ValueError(f"不支持的 experiment_schema: {schema!r}")
        if schema == 2:
            validate_schema2_setting(setting)
        super().parse_setting(setting)    # 先让 ZAC 原版解析它认识的字段
        self.placer_kind = setting.get("placer", "zac")
        self.zzx_params = {k: setting[k] for k in self.ZAC_ZZX_KEYS if k in setting}
        self.zzx_exact_threshold = setting.get("coloring_exact_threshold", 24)
        self.zzx_node_budget = setting.get("coloring_node_budget", 200_000)
        # 审计 MAJOR-6：use_window 无 window_size 会 AttributeError——补默认
        if self.use_window and not hasattr(self, "window_size"):
            self.window_size = 1000
        # 消费断言：出现在任务单里的键必须被认识（防"旋钮拼错→静默跑错模式"的空实验）
        unknown = set(setting) - set(self.ZAC_ZZX_KEYS) - set(self.ZAC_KEYS)
        if unknown:
            raise ValueError(f"未知设置键（防静默丢参）: {sorted(unknown)}")

    # ------------------------------------------------------------ 放置接线
    def place_qubit_initial(self):
        """第①道工序：初始布局。init_engine="ga" 时用 GA 替换 ZAC 的 SA。

        ZAC 的 SAPlacer 占编译时间 97-98% 但邻域生成器有缺陷（README·已知
        边界），GA 版搜索同一目标（层权重搭档会合距离），座位枚举与原版一致。
        给定映射 / 平凡放置两条路径仍走原版，不掺和。
        """
        if (self.zzx_params.get("init_engine", "sa") != "ga"
                or self.given_initial_mapping is not None
                or self.trivial_placement):
            return super().place_qubit_initial()
        import time
        from zzx.gainit import GAInitialPlacer
        t0 = time.time()
        gp = GAInitialPlacer(self.zzx_params)
        gp.run(self.architecture, self.n_q, self.gate_scheduling)
        self.qubit_mapping.append(gp.best_mapping)
        self.runtime_analysis["initial placement"] = time.time() - t0

    def place_qubit_intermedeiate(self):
        """第⑤道工序的入口（ZAC 源码里就是这个拼写）。"""
        if self.placer_kind not in ("batch", "resident"):
            return super().place_qubit_intermedeiate()   # 对照组走原版

        if self.placer_kind == "batch":
            from zzx.zplacer import BatchAwarePlacer     # ZAC_new 对照模式
            placer_cls, preview_attr = BatchAwarePlacer, "batch_preview"
        else:
            from zzx.zplacer import ResidentPlacer       # 驻留主模式
            placer_cls, preview_attr = ResidentPlacer, "decision_log"

        t_p = time.time()
        placer = placer_cls(deepcopy(self.qubit_mapping[0]), **self.zzx_params)
        # 驻留模式中和复用机制（ResidentPlacer 内部还会再置空一次，双保险）：
        # 复用点名 + 两世界 filter_mapping 与驻留决策互斥——同时开会座位双订。
        if self.placer_kind == "resident":
            self.reuse_qubit = [set() for _ in self.gate_scheduling]
        placer.run(self.architecture, self.qubit_mapping, self.gate_scheduling,
                   self.dynamic_placement, self.reuse_qubit)
        self.qubit_mapping = placer.mapping       # 放置结果交回流水线

        self.runtime_analysis["intermediate placement"] = time.time() - t_p
        self.runtime_analysis["zzx gate placement"] = placer.search_time
        self.zzx_placer_preview = getattr(placer, preview_attr)
        if self.placer_kind == "resident":
            # Public Schema-2 evidence channel consumed by method_driver.  Keep
            # the historic preview alias, but never leave the formal decision
            # ledger empty after a resident run.
            self.zzx_decision_log = list(placer.decision_log)

    # ------------------------------------------------------------ 路由接线
    def _expanded_batch_conflicts(self, members, owner, mapping_from,
                                  mapping_to, pos):
        """Replay the exact parking expansion for a proposed physical batch.

        Endpoint-compatible legs are not automatically compatible after ZAC's
        staggered row activation: a parked column can temporarily merge with a
        neighbouring column, and a one-micron parking detour can sweep across a
        stationary atom.  The formal router therefore previews the very same
        ``expand_arrangement`` implementation used for native code generation
        and returns the member indices responsible for the first physical
        conflict.  An empty set means every expanded MOVE phase is AOD ordered
        and ghost safe at the current replay positions.
        """
        arch = self.architecture
        exact = lambda loc: arch.exact_SLM_location_tuple(tuple(loc))
        qubits = sorted(owner[index] for index in members)
        rows = {}
        for q in qubits:
            rows.setdefault(exact(mapping_from[q])[1], []).append(q)
        row_qubits = [rows[y] for y in sorted(rows)]
        begin_locs = [
            [[q, *mapping_from[q]] for q in row]
            for row in row_qubits
        ]
        end_locs = [
            [[q, *mapping_to[q]] for q in row]
            for row in row_qubits
        ]
        details = self.expand_arrangement({
            "begin_locs": begin_locs,
            "end_locs": end_locs,
        })
        member_of = {q: index for index, q in owner.items() if index in members}
        physical = {q: tuple(pos[q]) for q in qubits}
        held = set()

        for detail in details:
            kind = str(detail.get("type", ""))
            if kind == "activate":
                for row_id in detail.get("row_id", []):
                    held.update(row_qubits[int(row_id)])
                continue
            if not kind.startswith("move"):
                continue
            coordinates = {
                int(item["id"]): (float(item["x"]), float(item["y"]))
                for row in detail.get("end_coord", []) for item in row
            }
            phase_legs = []
            phase_owners = []
            phase_positions = dict(pos)
            phase_positions.update(physical)
            for q in sorted(held):
                start = physical[q]
                end = coordinates.get(q, start)
                distance = hypot(start[0] - end[0], start[1] - end[1])
                if distance > 1e-9:
                    phase_legs.append((distance, *start, *end))
                    phase_owners.append(q)

            vectors = [(leg[1], leg[3], leg[2], leg[4]) for leg in phase_legs]
            for left in range(len(vectors)):
                for right in range(left + 1, len(vectors)):
                    if not compatible_2d(vectors[left], vectors[right]):
                        return {
                            member_of[phase_owners[left]],
                            member_of[phase_owners[right]],
                        }

            moving = set(phase_owners)
            ghosts = [(q, *location) for q, location in phase_positions.items()
                      if q not in moving]
            hits = ghost_hits(phase_legs, ghosts, detail=True)
            if hits:
                _gid, _gx, _gy, col_track, row_track, _s = hits[0]
                bad = {
                    member_of[q]
                    for q, leg in zip(phase_owners, phase_legs)
                    if (leg[1], leg[3]) == col_track
                    or (leg[2], leg[4]) == row_track
                }
                return bad or {member_of[phase_owners[0]]}
            for q in held:
                physical[q] = coordinates.get(q, physical[q])
        return set()

    def _coloring_batches(self, remain_graph, mapping_from, mapping_to):
        """把 remain_graph 按注册的 coloring/greedy 策略一次性分批。

        vectors 与 router.graph_construction 同源：(起x, 终x, 起y, 终y)。
        腿格式换算成 zcost 的 (dist, 起x, 起y, 终x, 终y) 后直接复用
        color_batches（含精确档升级与独立集性质）。

        鬼点重放审计（硬保证层路由侧）：着色后按批序"执行"一遍——每批
        的腿对着【当前真实位置】的静止原子查鬼点，命中的批拆出肇事腿
        延后重新分批，直到全部干净。比给冲突图加保守鬼点边精确得多
        （保守边在密集层会把所有腿对都连上，批数爆炸 11→82 的实证）。
        终止性：放置层（zplacer._repair_ghosts）保证每条腿单独不撞
        {批前,批后} 任何位置——重放中的静止位置必属其一，单腿批恒干净。
        """
        vectors = self.graph_construction(remain_graph, mapping_from, mapping_to)
        legs = [(hypot(v[0] - v[1], v[2] - v[3]), v[0], v[2], v[1], v[3])
                for v in vectors]
        arch = self.architecture
        ex = lambda loc: arch.exact_SLM_location_tuple(tuple(loc))
        n_atoms = len(mapping_from)
        owner = {i: q for i, q in enumerate(remain_graph)}

        def audit_pass(batch_ids, pos):
            """按给定批序重放：返回 (干净批列表, 被拆腿下标集)。

            pos: 原子当前位置 {q:(x,y)}，随批次执行推进。
            命中批拆腿策略：detail 给出肇事列/行轨迹，映射回贡献腿移出。
            """
            clean, deferred = [], []
            queue = [list(members) for members in batch_ids]
            while queue:
                members = queue.pop(0)
                pending = list(members)
                waypoint_repaired = False
                while True:
                    ghosts = [(q, *pos[q]) for q in range(n_atoms)
                              if q not in {owner[i] for i in pending}]
                    batch_legs = [legs[i] for i in pending]
                    hits = ghost_hits(batch_legs, ghosts, detail=True)
                    if not hits:
                        break
                    if len(pending) == 1:
                        q = owner[pending[0]]
                        waypoint = self._safe_slm_waypoint(
                            q, mapping_from, mapping_to, pos)
                        if waypoint is None:
                            deferred.append(pending[0])
                            pending = []
                            break
                        key = (q, tuple(mapping_from[q]), tuple(mapping_to[q]))
                        self._zzx_waypoint_plan[key] = waypoint
                        waypoint_repaired = True
                        break
                    _, _, _, ct, rt, _s = hits[0]
                    bad = {i for i in pending
                           if (legs[i][1], legs[i][3]) == ct
                           or (legs[i][2], legs[i][4]) == rt}
                    if not bad:                     # 轨迹映射失败的安全网
                        bad = {pending[0]}
                    deferred += sorted(bad)
                    pending = [i for i in pending if i not in bad]
                    if not pending:
                        break
                if pending and waypoint_repaired:
                    clean.append(pending)
                    i = pending[0]
                    pos[owner[i]] = (legs[i][3], legs[i][4])
                    continue
                if pending:
                    expanded_bad = self._expanded_batch_conflicts(
                        pending, owner, mapping_from, mapping_to, pos)
                    if expanded_bad:
                        if len(pending) == 1:
                            q = owner[pending[0]]
                            waypoint = self._safe_slm_waypoint(
                                q, mapping_from, mapping_to, pos)
                            if waypoint is None:
                                raise ValueError(
                                    f"single-leg expanded route remains unsafe for atom {q}")
                            key = (q, tuple(mapping_from[q]), tuple(mapping_to[q]))
                            self._zzx_waypoint_plan[key] = waypoint
                            clean.append(pending)
                            pos[q] = (legs[pending[0]][3], legs[pending[0]][4])
                            continue
                        # First preserve as much concurrency as possible by
                        # deferring only the phase contributors.  If every leg
                        # contributes (for example two columns merge only after
                        # parking), split deterministically by pickup row.  A
                        # one-row batch has no parking detour; individual legs
                        # are the final hard-safe fallback.
                        bad = [i for i in pending if i in expanded_bad]
                        keep = [i for i in pending if i not in expanded_bad]
                        if keep and bad:
                            queue.insert(0, keep)
                            deferred.extend(bad)
                            continue
                        row_groups = {}
                        for i in pending:
                            y = legs[i][2]
                            row_groups.setdefault(y, []).append(i)
                        groups = [row_groups[y] for y in sorted(row_groups)]
                        if len(groups) == 1:
                            groups = [[i] for i in pending]
                        queue[0:0] = groups
                        continue
                    clean.append(pending)
                    for i in pending:               # 本批落座，推进重放位置
                        pos[owner[i]] = (legs[i][3], legs[i][4])
            return clean, deferred

        pos = {q: ex(mapping_from[q]) for q in range(n_atoms)}
        # 初始着色带"T0 位置鬼点边"（排除两腿主人，稀疏）——引导着色
        # 天然避开绝大多数撞鬼组合；残余由下面的重放审计精确拆批
        ghosts0 = [(q, *pos[q]) for q in range(n_atoms)]
        if self.routing_strategy == "greedy":
            batcher = lambda values, **kwargs: greedy_phase_batches(
                values, ghosts=kwargs.get("ghosts"), owners=kwargs.get("owners"))
        else:
            batcher = lambda values, **kwargs: phase_batches(
                values, ghosts=kwargs.get("ghosts"), owners=kwargs.get("owners"),
                exact_threshold=kwargs.get("exact_threshold", 0),
                node_budget=self.zzx_node_budget)
        chi, batches, method = batcher(
            legs, ghosts=ghosts0, owners=remain_graph,
            exact_threshold=self.zzx_exact_threshold)
        final, deferred = audit_pass(batches, pos)
        for round_i in range(3):                    # 2 轮重批 + 末轮强制单飞
            if not deferred:
                break
            if round_i == 2:
                # 最终单飞仍走同一真实展开审计；绝不在 repair 后绕过重放。
                singles = [[i] for i in sorted(deferred)]
                more, unresolved = audit_pass(singles, pos)
                if unresolved:
                    atoms = [owner[i] for i in unresolved]
                    raise ValueError(
                        f"ghost-safe routing could not place atoms {atoms}")
                final += more
                deferred = []
                break
            sub = sorted(deferred)
            _, sub_batches, _ = batcher(
                [legs[i] for i in sub], exact_threshold=self.zzx_exact_threshold)
            sub_batches = [[sub[k] for k in members] for members in sub_batches]
            more, deferred = audit_pass(sub_batches, pos)
            final += more
        self.zzx_ghost_splits = getattr(self, "zzx_ghost_splits", 0) + \
            (len(final) - len(batches))
        return chi, final, method

    def _phase_batches(self, remain_graph, mapping_from, mapping_to):
        """一个搬运相位的批次生成器：coloring 一次着色；mis/maximalis* 逐轮剥离。
        返回 (set_aod, method) 序列；耗尽即停。"""
        if self.routing_strategy in {"coloring", "greedy"}:
            while remain_graph:
                window = (remain_graph[:self.window_size] if self.use_window
                          else remain_graph)
                chi, batches, method = self._coloring_batches(window, mapping_from,
                                                              mapping_to)
                for members in batches:
                    yield {window[i] for i in members}, method
                moved = {window[i] for members in batches for i in members}
                remain_graph = [q for q in remain_graph if q not in moved]
        else:
            while remain_graph:
                vectors = self.graph_construction(remain_graph, mapping_from,
                                                  mapping_to)
                violations = self.collect_violation(vectors)
                if self.routing_strategy == "mis":
                    moved_idx = self.kamis_solve(len(vectors), violations, 0)
                    method = "mis"
                else:
                    moved_idx = self.maximalis_solve(len(vectors), violations)
                    method = "maximalis"
                set_aod = {remain_graph[i] for i in moved_idx}
                yield set_aod, method
                remain_graph = [q for q in remain_graph if q not in set_aod]

    def _sorted_by_distance(self, remain_graph, mapping_from, mapping_to):
        """maximalis_sort / coloring 的同款预排序：远腿先上车（router.py:65-67）。"""
        import math as _math
        return sorted(remain_graph, key=lambda q: _math.dist(
            self.architecture.exact_SLM_location_tuple(mapping_from[q]),
            self.architecture.exact_SLM_location_tuple(mapping_to[q])), reverse=True)

    def route_qubit_mis(self, layer: int):
        """resident 模式独占路径；其余走 ZAC_new 的着色路由或原版。"""
        if self.placer_kind == "resident":
            return self._route_resident(layer)
        if self.routing_strategy not in {"coloring", "greedy"}:
            return super().route_qubit_mis(layer)

        # ---- 前半程：宿舍 → 车间（与原版 route_qubit_mis 逐行对应）----
        initial_mapping = self.qubit_mapping[2 * layer]
        gate_mapping = self.qubit_mapping[2 * layer + 1]
        if layer + 2 < len(self.qubit_mapping):
            final_mapping = self.qubit_mapping[2 * layer + 2]
        else:
            final_mapping = None

        remain_graph = []
        for gate in self.gate_scheduling[layer]:
            for q in gate:
                if initial_mapping[q] != gate_mapping[q]:
                    assert (initial_mapping[q][0] == 0 or gate_mapping[q][0] == 0)
                    remain_graph.append(q)

        id_layer_start = len(self.result_json["instructions"])
        batch = 0
        method = "empty"
        moved = set()
        while remain_graph:
            # use_window 时 graph_construction 只看前 window_size 条 ——
            # 与原版一致按窗口分批，窗口内一次着色出多批（默认窗口≥n 即单趟）
            window = (remain_graph[:self.window_size] if self.use_window
                      else remain_graph)
            chi, batches, method = self._coloring_batches(window, initial_mapping,
                                                          gate_mapping)
            moved = set(q for members in batches for q in
                        (window[i] for i in members))
            for members in batches:
                set_aod = {window[i] for i in members}
                batch += self._process_ghost_safe_movement(
                    set_aod, initial_mapping, gate_mapping)
            remain_graph = [q for q in remain_graph if q not in moved]
        self.zzx_route_log.append({"layer": layer, "phase": "out",
                                    "batches": batch, "method": method,
                                    "atoms": len(moved) if batch else 0})

        # ---- 门执行层（原版）----
        self.process_gate_layer(layer, gate_mapping)

        # ---- 后半程：车间 → 宿舍（回程，同样着色分批）----
        if final_mapping is not None:
            if self.dynamic_placement or self.reuse:
                remain_graph = []
                for gate in self.gate_scheduling[layer]:
                    for q in gate:
                        if final_mapping[q] != gate_mapping[q]:
                            remain_graph.append(q)
                batch = 0
                method = "empty"
                moved = set()
                while remain_graph:
                    window = (remain_graph[:self.window_size] if self.use_window
                              else remain_graph)
                    chi, batches, method = self._coloring_batches(
                        window, gate_mapping, final_mapping)
                    moved = set(q for members in batches for q in
                                (window[i] for i in members))
                    for members in batches:
                        set_aod = {window[i] for i in members}
                        batch += self._process_ghost_safe_movement(
                            set_aod, gate_mapping, final_mapping)
                    remain_graph = [q for q in remain_graph if q not in moved]
                self.zzx_route_log.append({"layer": layer, "phase": "back",
                                            "batches": batch, "method": method,
                                            "atoms": len(moved)})
            else:
                self.construct_reverse_layer(id_layer_start, gate_mapping, final_mapping)
            self.aod_assignment(id_layer_start)

    # ------------------------------------------------------------ 驻留路由
    def _route_resident(self, layer: int):
        """驻留模式路由：out 相（参与者增量，区内换座合法）→ 门 → back 相
        （全部映射增量者 = 回撤腿，含闲住驻留者）。

        指令流时间顺序（每轮）：out 批次… → rydberg(+1qGate) → back 批次…
        → 下一轮 out…。与 ZAC 原版的三处差别（都有审计实证背书）：
        ① out 相断言从"一端必在存储"放宽为"两端合法 SLM 位"——驻留者的
          入区腿是区内短移（zone→zone），原断言必炸；expand_arrangement/
          get_duration/aod_assignment 全坐标系化，无存储假设（审计验证）。
        ② back 相扫描全部原子而非仅本轮门原子——被 E2/容量阀/GA 决策逐出
          的闲住驻留者不在本轮门里，但其回撤腿必须发车。
        ③ 依赖账本补丁：闲住驻留者的 qubit_dependency 可能停在数轮之前
          （router.py:417-420 只更新本轮参与者），不补的话 aod_assignment
          会让他的回撤车与 rydberg/1q 并行执行——审计 FATAL-1 的洞。
        """
        initial_mapping = self.qubit_mapping[2 * layer]
        gate_mapping = self.qubit_mapping[2 * layer + 1]
        if layer + 2 < len(self.qubit_mapping):
            final_mapping = self.qubit_mapping[2 * layer + 2]
        else:
            final_mapping = None

        # ---- 前半程（out 相）：本轮参与者中"落位≠门位"者（驻留者换座 = 区内短移）----
        # 只扫本轮门原子：非参与者的移动全部发生在 back 相（映射流构造保证），
        # 两相不混——避免"同一座位 A 离开 B 到达"跨相交错的排序难题。
        remain_graph = []
        for gate in self.gate_scheduling[layer]:
            for q in gate:
                if initial_mapping[q] != gate_mapping[q]:
                    # 断言放宽（审计证实 expand_arrangement 全坐标系化，无存储假设）：
                    # 原版"一端必在存储"→"两端均为合法 SLM 位"，zone→zone 合法化
                    assert self.architecture.is_valid_SLM_position(*initial_mapping[q])
                    assert self.architecture.is_valid_SLM_position(*gate_mapping[q])
                    remain_graph.append(q)
        # maximalis_sort / coloring 的同款预排序：远腿先上车（router.py:65-67）
        if self.routing_strategy != "mis" and self.routing_strategy != "maximalis":
            remain_graph = self._sorted_by_distance(remain_graph, initial_mapping,
                                                    gate_mapping)

        id_layer_start = len(self.result_json["instructions"])   # aod_assignment 的起扫点
        batch = 0
        method = "empty"
        moved = set()
        for set_aod, method in self._phase_batches(remain_graph, initial_mapping,
                                                   gate_mapping):
            batch += self._process_ghost_safe_movement(
                set_aod, initial_mapping, gate_mapping)
            moved |= set_aod
        self.zzx_route_log.append({"layer": layer, "phase": "out",
                                    "batches": batch, "method": method,
                                    "atoms": len(moved) if batch else 0})

        # ---- 门执行层（原版）：一次 rydberg 做完本轮全部 2q 门，1q 挂在后面 ----
        first_gate_inst = len(self.result_json["instructions"])
        self.process_gate_layer(layer, gate_mapping)
        self._bind_resident_rydberg_dependencies(gate_mapping, first_gate_inst)
        last_gate_inst = len(self.result_json["instructions"]) - 1

        # ---- 后半程（back 相）：全部映射增量者（映射流保证 = 回撤者），含闲住驻留者 ----
        if final_mapping is not None:
            remain_back = [q for q in range(len(gate_mapping))
                           if gate_mapping[q] != final_mapping[q]]
            # 依赖账本补丁（审计 FATAL-1）：非参与者的 qubit_dependency 可能停在
            # 数轮之前——aod_assignment 只看账本，会让回撤腿与本轮 rydberg/1q 并行。
            # 压到本轮最后一条门指令之后，边界语义（门后→下轮前）即恢复。
            participants = {q for gate in self.gate_scheduling[layer] for q in gate}
            for q in remain_back:
                if q not in participants:
                    self.qubit_dependency[q] = last_gate_inst
            if self.routing_strategy != "mis" and self.routing_strategy != "maximalis":
                remain_back = self._sorted_by_distance(remain_back, gate_mapping,
                                                       final_mapping)
            batch = 0
            method = "empty"
            moved = set()
            for set_aod, method in self._phase_batches(remain_back, gate_mapping,
                                                       final_mapping):
                batch += self._process_ghost_safe_movement(
                    set_aod, gate_mapping, final_mapping)
                moved |= set_aod
            self.zzx_route_log.append({"layer": layer, "phase": "back",
                                        "batches": batch, "method": method,
                                        "atoms": len(moved)})
            # AOD 分配与时间戳：从本轮第一条指令起统一排时（ZAC 原机）
            self.aod_assignment(id_layer_start)
