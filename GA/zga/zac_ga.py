"""转接头：ZAC_GA = ZAC 流水线 + GA 发动机（40 行，杠杆效应最大的文件）。

背景：ZAC 编译流水线共有九道工序（详见 zac/MODULE_GUIDE.md）：
    电路解析 → ①ASAP分批 → ②复用点名 → ④SA初始床位
    → ⑤进入中间布局（本文件在这里换零件）
        → ⑥分床位（原版）→ ⑦给下一批的活分工位 ★→ ⑧终审择优（原版）
    → ⑨MIS排车 → 输出指令流(ZAIR) + 合法性校验

本文件的全部作用：把第⑤道工序里创建的放置器对象
    VertexMatchingPlacer（ZAC 原版，一步求解式）
换成
    GAPlacer（本包的遗传搜索版）
其余工序一行不改。ZAC_GA 类继承 ZAC，只覆写两个方法：
    parse_setting            —— 多认识一个 "placer" 开关和一堆 GA 旋钮
    place_qubit_intermedeiate —— 唯一的行为改动点（换放置器）

专业考量（为什么这样做）：
  * 控制变量：GA 与 ZAC 的任何差异都能归因到"放置搜索"这一处，
    这是实验可比性的根基；
  * 零侵入：ZAC 源码不用改一行——继承 + 覆写就够了。
"""
import time
from copy import deepcopy

# 注意：这里 import 的是 GA 文件夹内的本地副本（zga/__init__.py 已把
# GA 根目录挂到 sys.path 最前），与外层 ZAC/zac 逐字节一致（可用
# `diff -r ../ZAC/zac zac` 验证）。
from zac.zac import ZAC


class ZAC_GA(ZAC):
    """ZAC 的子类：流水线完全相同，仅"中间布局"环节可切换为 GA。"""

    # parse_setting 能识别的 GA 旋钮（全部有默认值，见 gaplacer.py）
    GA_KEYS = ("population_size", "iterations", "neighbors_per_solution",
               "neighbor_sample_size", "use_sd", "seed")

    def __init__(self):
        super().__init__()
        self.placer_kind = "zac"       # 默认 "zac"：行为与原版完全一致（对照组）
        self.ga_params: dict = {}      # GA 旋钮集合

    def parse_setting(self, setting: dict):
        # 先让 ZAC 原版解析所有它认识的字段（dir/reuse/window/...）
        super().parse_setting(setting)
        # 再多收两个新东西：
        self.placer_kind = setting.get("placer", "zac")                # 总开关
        self.ga_params = {k: setting[k] for k in self.GA_KEYS if k in setting}

    def place_qubit_intermedeiate(self):
        """第⑤道工序的入口（注意 ZAC 源码里就是这个拼写）。

        原版内容：创建 VertexMatchingPlacer 并驱动它逐层放置。
        覆写内容：一模一样的流程，只把放置器换成 GAPlacer。
        """
        if self.placer_kind != "ga":
            # 开关关着 → 完全走原版逻辑（跑 ZAC 基线就靠这条路径）
            return super().place_qubit_intermedeiate()

        from zga.gaplacer import GAPlacer  # 延迟导入，避免无谓的加载顺序依赖

        t_p = time.time()
        # 与原版 placer.py 的三行完全对应，仅类名不同：
        placer = GAPlacer(deepcopy(self.qubit_mapping[0]), **self.ga_params)
        placer.run(self.architecture, self.qubit_mapping, self.gate_scheduling,
                   self.dynamic_placement, self.reuse_qubit)
        self.qubit_mapping = placer.mapping   # 放置结果交回流水线（第⑨道排车用）

        # 计时口径与原版对齐，并额外单列 GA 搜索本身的耗时（报告用）
        self.runtime_analysis["intermediate placement"] = time.time() - t_p
        self.runtime_analysis["ga gate placement"] = placer.ga_time
