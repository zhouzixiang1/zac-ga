"""zga 包：GA 放置器（自包含版，不再依赖外层 ZAC/ 目录）。

包结构（共 4 个模块）：
    zga.zac_ga   —— 转接头：把 GAPlacer 接进 ZAC 流水线（40 行）
    zga.gaplacer —— 发动机：遗传搜索放置器（约 230 行）
    zga.racost   —— 打分仪表：路由感知代价（约 80 行）

本包依赖同目录下的 zac/ 文件夹（从 ZAC 原版逐字节复制的编译器源码，
见 zac/MODULE_GUIDE.md），以及 GA/hardware_spec、GA/benchmark 下的数据。
"""
import sys
from pathlib import Path

# 把 GA 文件夹自身挂到模块搜索路径最前面。
# 这样 `import zac` 会命中 GA/zac/（本地副本），而不是外层 ZAC/zac/——
# 整个 GA 文件夹因此可以单独拷走、单独运行。
_GA_ROOT = str(Path(__file__).resolve().parents[1])
if _GA_ROOT not in sys.path:
    sys.path.insert(0, _GA_ROOT)
