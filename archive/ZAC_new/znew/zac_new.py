"""转接头：ZAC_new = ZAC 流水线 + 批次感知放置 + 图着色分批路由。

与 GA/zga/zac_ga.py、FABLE/fable/zac_fable.py 同构（对照实验的控制
变量由此保证）。ZAC_new 继承 ZAC，改动集中在三处：
    parse_setting             —— 多认识 placer="batch" 开关、搜索旋钮、
                                 routing_strategy="coloring" 的着色档位
    place_qubit_intermedeiate —— 注入 BatchAwarePlacer（双引擎门放置）
    route_qubit_mis           —— 新增 "coloring" 路由策略：一次 DSATUR
                                 着色出全部批次（每色=一批），替代
                                 maximalis_solve 的逐轮贪心剥离
其余工序（ASAP 调度、复用点名、SA 初始床位、回位放置、两世界复用
终审、停车让位、AOD 分配、校验）一行不改。

正确性依据（着色批 vs MIS 批同构）：每个色类都是独立集 = 批内原子
两两 compatible_2D；着色覆盖全部节点 = 所有原子运达；下游
process_movement_layer 的行分组/依赖账本对"批怎么来的"无感知。
"""
import time
from copy import deepcopy

# import 的是 ZAC_new 文件夹内的本地副本（znew/__init__.py 已把根目录
# 挂到 sys.path 最前），与外层 ZAC/zac 逐字节一致。
from zac.zac import ZAC

from znew.zcost import color_batches
from math import hypot


class ZAC_new(ZAC):
    """ZAC 的子类：批次进放置（双引擎）+ 图着色分批路由。"""

    # parse_setting 能识别的新旋钮（FABLE 教训：不在白名单的键会被静默丢弃）
    ZAC_NEW_KEYS = ("engine", "w_batch", "use_lookahead", "min_expand", "seed",
                    "lambda_penalty", "penalty_decay", "max_penalty_iter",
                    "population_size", "iterations", "neighbors_per_solution",
                    "neighbor_sample_size", "coloring_exact_threshold",
                    "coloring_node_budget")

    def __init__(self):
        super().__init__()
        self.placer_kind = "zac"          # 默认 "zac"：放置走原版（对照组/回归）
        self.znew_params: dict = {}       # 放置搜索旋钮
        self.znew_exact_threshold = 24    # 路由着色：≤此节点数上精确档
        self.znew_node_budget = 200_000   # 路由着色：精确档节点预算
        self.znew_route_log: list = []    # 路由账本：每段搬运的 (层, 相位, 批数, 解法)
        self.znew_placer_preview: list = []   # 放置预演账本（对账用）

    def parse_setting(self, setting: dict):
        super().parse_setting(setting)    # 先让 ZAC 原版解析它认识的字段
        self.placer_kind = setting.get("placer", "zac")
        self.znew_params = {k: setting[k] for k in self.ZAC_NEW_KEYS if k in setting}
        self.znew_exact_threshold = setting.get("coloring_exact_threshold", 24)
        self.znew_node_budget = setting.get("coloring_node_budget", 200_000)

    # ------------------------------------------------------------ 放置接线
    def place_qubit_intermedeiate(self):
        """第⑤道工序的入口（ZAC 源码里就是这个拼写）。"""
        if self.placer_kind != "batch":
            return super().place_qubit_intermedeiate()   # 对照组走原版

        from znew.zplacer import BatchAwarePlacer   # 延迟导入，避免加载顺序依赖

        t_p = time.time()
        placer = BatchAwarePlacer(deepcopy(self.qubit_mapping[0]), **self.znew_params)
        placer.run(self.architecture, self.qubit_mapping, self.gate_scheduling,
                   self.dynamic_placement, self.reuse_qubit)
        self.qubit_mapping = placer.mapping       # 放置结果交回流水线

        self.runtime_analysis["intermediate placement"] = time.time() - t_p
        self.runtime_analysis["znew gate placement"] = placer.search_time
        self.znew_placer_preview = placer.batch_preview   # χ 预演账本

    # ------------------------------------------------------------ 路由接线
    def _coloring_batches(self, remain_graph, mapping_from, mapping_to):
        """把 remain_graph 一次性着色分批（每色=一批，批间按最远腿降序）。

        vectors 与 router.graph_construction 同源：(起x, 终x, 起y, 终y)。
        腿格式换算成 zcost 的 (dist, 起x, 起y, 终x, 终y) 后直接复用
        color_batches（含精确档升级与独立集性质）。
        """
        vectors = self.graph_construction(remain_graph, mapping_from, mapping_to)
        legs = [(hypot(v[0] - v[1], v[2] - v[3]), v[0], v[2], v[1], v[3])
                for v in vectors]
        return color_batches(legs, exact_threshold=self.znew_exact_threshold,
                             node_budget=self.znew_node_budget)

    def route_qubit_mis(self, layer: int):
        """routing_strategy="coloring" 时走本类的着色分批；否则原版。"""
        if self.routing_strategy != "coloring":
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
                self.process_movement_layer(set_aod, initial_mapping, gate_mapping)
                batch += 1
            remain_graph = [q for q in remain_graph if q not in moved]
        self.znew_route_log.append({"layer": layer, "phase": "out",
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
                        window, final_mapping, gate_mapping)
                    moved = set(q for members in batches for q in
                                (window[i] for i in members))
                    for members in batches:
                        set_aod = {window[i] for i in members}
                        self.process_movement_layer(set_aod, gate_mapping,
                                                    final_mapping)
                        batch += 1
                    remain_graph = [q for q in remain_graph if q not in moved]
                self.znew_route_log.append({"layer": layer, "phase": "back",
                                            "batches": batch, "method": method,
                                            "atoms": len(moved)})
            else:
                self.construct_reverse_layer(id_layer_start, gate_mapping, final_mapping)
            self.aod_assignment(id_layer_start)
