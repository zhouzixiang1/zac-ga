"""转接头：ZAC_FABLE = ZAC 流水线 + Fable 式放置搜索。

与 GA/zga/zac_ga.py 同构（对照实验的控制变量由此保证）：
ZAC_FABLE 继承 ZAC，只覆写两个方法——
    parse_setting             —— 多认识一个 "placer": "fable" 开关和搜索旋钮
    place_qubit_intermedeiate —— 唯一的行为改动点（换放置器为 FablePlacer）
其余工序（①ASAP分批 ②复用点名 ④SA初始床位 ⑥分床位 ⑧终审择优 ⑨MIS排车）
一行不改。FABLE 实验与 GA v1a 的差异 100% 归因于放置搜索的
"评价函数 + 邻域算子"两处（见 fplacer.py 文件头）。
"""
import time
from copy import deepcopy

# 注意：这里 import 的是 FABLE 文件夹内的本地副本（fable/__init__.py 已把
# FABLE 根目录挂到 sys.path 最前），与外层 ZAC/zac 逐字节一致。
from zac.zac import ZAC


class ZAC_FABLE(ZAC):
    """ZAC 的子类：流水线完全相同，仅"中间布局"环节换为 Fable 搜索。"""

    # parse_setting 能识别的搜索旋钮（全部有默认值，见 fplacer.py）
    FABLE_KEYS = ("population_size", "iterations", "neighbors_per_solution",
                  "neighbor_sample_size", "w_conf", "use_lookahead", "seed")

    def __init__(self):
        super().__init__()
        self.placer_kind = "zac"         # 默认 "zac"：行为与原版完全一致（对照组）
        self.fable_params: dict = {}     # 搜索旋钮集合

    def parse_setting(self, setting: dict):
        super().parse_setting(setting)   # 先让 ZAC 原版解析它认识的字段
        self.placer_kind = setting.get("placer", "zac")
        self.fable_params = {k: setting[k] for k in self.FABLE_KEYS if k in setting}

    def place_qubit_intermedeiate(self):
        """第⑤道工序的入口（ZAC 源码里就是这个拼写）。"""
        if self.placer_kind != "fable":
            return super().place_qubit_intermedeiate()   # 对照组走原版

        from fable.fplacer import FablePlacer  # 延迟导入，避免加载顺序依赖

        t_p = time.time()
        # 与原版 placer.py 的三行完全对应，仅类名不同：
        placer = FablePlacer(deepcopy(self.qubit_mapping[0]), **self.fable_params)
        placer.run(self.architecture, self.qubit_mapping, self.gate_scheduling,
                   self.dynamic_placement, self.reuse_qubit)
        self.qubit_mapping = placer.mapping   # 放置结果交回流水线（第⑨道排车用）

        # 计时口径与原版对齐，并额外单列搜索本身的耗时（报告用）
        self.runtime_analysis["intermediate placement"] = time.time() - t_p
        self.runtime_analysis["fable gate placement"] = placer.search_time
