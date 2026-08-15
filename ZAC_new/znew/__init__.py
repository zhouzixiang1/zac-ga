"""znew 包：ZAC_new 实验——把"搬运批次"（图着色）放进 ZAC 的决策回路。

包结构（3 个模块）：
    znew.zac_new  —— 转接头：ZAC_new(ZAC) 子类，接线双引擎放置器 + 着色路由
    znew.zplacer  —— 发动机：BatchAwarePlacer（penalty 匹配罚单 / ga 遗传）
    znew.zcost    —— 打分仪表：冲突图 + DSATUR 着色"要几批"（本文件夹灵魂）

本包依赖同目录下的 zac/ 文件夹（从 ZAC 原版逐字节复制的编译器源码）、
ZAC_new/hardware_spec 与 ZAC_new/benchmark 的数据。与 GA/、FABLE/
完全平行的自包含结构——三个实验互不引用、互不污染。
"""
import sys
from pathlib import Path

# 把 ZAC_new 文件夹自身挂到模块搜索路径最前面。
# 这样 `import zac` 会命中 ZAC_new/zac/（本地副本），而不是外层 ZAC/zac/——
# 整个 ZAC_new 文件夹因此可以单独拷走、单独运行。
_ZNEW_ROOT = str(Path(__file__).resolve().parents[1])
if _ZNEW_ROOT not in sys.path:
    sys.path.insert(0, _ZNEW_ROOT)
