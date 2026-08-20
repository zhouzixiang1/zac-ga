"""zzx 包：ZAC_zzx 实验——驻留编译器（不放回）+ GA 分相位着色适应度。

包结构（5 个模块，建议按此顺序阅读）：
    zzx.zac_zzx  —— 转接头：ZAC_zzx(ZAC) 子类。三种放置模式分发
                    （zac=原版对照 / batch=ZAC_new 对照 / resident=本作主角），
                    驻留路由（remain_graph 扩展 + 依赖账本补丁），配置白名单
                    与消费断言（任务单里未认识的键直接报错，防静默空实验）。
    zzx.resident —— 驻留决策的大脑（M1）：下次使用表 NextUse、驻留登记簿
                    ResidentRegistry、惰性决策 decide_lazy（默认全留 +
                    E2 挡路逐出 + 容量阀）、RETURN 落位三方案箱式匹配
                    match_return_sites（原位/就近/伙伴，笔记 :123-131）。
    zzx.zplacer  —— 发动机（M2/M3）：两个放置器。
                    ResidentPlacer = 主角（轮循环/菜单三级兜底/K2 钉扎/
                    GA 联合搜索 _ga_step/终检修复 _repair_placements）；
                    BatchAwarePlacer = ZAC_new 的对照引擎（penalty/ga），
                    仅在 placer="batch" 模式下使用。
    zzx.zcost    —— 打分仪表：compatible_2d（同车判定，ZAC router.py:232
                    移植 + 2 万对差分验证）、冲突图、DSATUR 着色（启发式 +
                    精确分支限界）、batch_cost = w_batch×χ + Σ√dmax。
    （verify_batches.py / run.py 等在 ZAC_zzx 根目录，不在本包内。）

本包依赖同目录下的 zac/ 文件夹（从 ZAC 原版逐字节复制的编译器源码，
绝不可修改——diff -r 与 ZAC/zac 校验字节一致）、ZAC_zzx/hardware_spec
与 ZAC_zzx/benchmark 的数据。与 GA/、FABLE/、ZAC_new/ 完全平行的
自包含结构——各实验互不引用、互不污染。
"""
import sys
from pathlib import Path

# 把 ZAC_zzx 文件夹自身挂到模块搜索路径最前面。
# 这样 `import zac` 会命中 ZAC_zzx/zac/（本地副本），而不是外层 ZAC/zac/——
# 整个 ZAC_zzx 文件夹因此可以单独拷走、单独运行。
_ZZX_ROOT = str(Path(__file__).resolve().parents[1])
if _ZZX_ROOT not in sys.path:
    sys.path.insert(0, _ZZX_ROOT)
