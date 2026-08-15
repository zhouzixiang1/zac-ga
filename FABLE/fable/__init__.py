"""fable 包：Fable 想法复现实验（自包含，不依赖外层 ZAC/ 或 GA/ 目录）。

包结构（共 3 个模块）：
    fable.zac_fable —— 转接头：把 FablePlacer 接进 ZAC 流水线
    fable.fplacer   —— 发动机：Fable 式局部搜索放置器（交换/邻居算子）
    fable.fcost     —— 打分仪表："最大链 + 冲突边"评估（ZAC 排车器预演）

本包依赖同目录下的 zac/ 文件夹（从 ZAC 原版逐字节复制的编译器源码），
以及 FABLE/hardware_spec、FABLE/benchmark 下的数据。与 GA/ 文件夹
完全平行的自包含结构——两个实验互不引用、互不污染。
"""
import sys
from pathlib import Path

# 把 FABLE 文件夹自身挂到模块搜索路径最前面。
# 这样 `import zac` 会命中 FABLE/zac/（本地副本），而不是外层 ZAC/zac/——
# 整个 FABLE 文件夹因此可以单独拷走、单独运行。
_FABLE_ROOT = str(Path(__file__).resolve().parents[1])
if _FABLE_ROOT not in sys.path:
    sys.path.insert(0, _FABLE_ROOT)
