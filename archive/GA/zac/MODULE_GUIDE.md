# GA/zac —— ZAC 编译器源码副本（逐字节未修改）

这个文件夹是从 `../../ZAC/zac/` **原样复制**的编译器源码（约 3470 行，BSD-3 许可证，
作者 Lin/Tan/Cong，HPCA 2025）。**一个字都没改**，可用以下命令随时验证：

```bash
diff -r ../ZAC/zac zac        # 应该没有任何输出
```

为什么原样复制而不改：
- GA 的一切改动都在 `GA/zga/` 里以"继承+覆写"的方式完成；
- 保持逐字节一致 = 保证 GA 与 ZAC 的实验差异 100% 来自放置搜索（控制变量），
  且这份副本的行为与已复现论文数据（16/18 逐位一致）的版本完全相同。

## 每个文件是干什么的（工厂故事版）

| 文件 | 流水线工序 | 作用 |
|---|---|---|
| `zac/zac.py` | 总指挥 | ZAC 主类：解析电路（qiskit 重综合）、驱动九道工序；`collect_reuse_qubit()` = 复用点名（二分图最大匹配备"免回家券"名单） |
| `zac/scheduler/scheduler.py` | ① 分批 | `asap()` 尽早进层；`graph_coloring()` 全可交换电路的边着色；按车间容量切层 |
| `zac/placer/placer.py` | ④⑤ 布局入口 | `place_qubit_initial()` 调 SA 排初始床位；`place_qubit_intermedeiate()` 创建放置器逐层放置——**GA 的挂钩点**（被 `zga/zac_ga.py` 覆写） |
| `zac/placer/saplacer.py` | ④ 初始床位 | 模拟退火：交换/跳床位的小改动迭代千次，代价=√距离(同行取max) |
| `zac/placer/vmplacer.py` | ⑥⑦⑧ 中间布局 | **GAPlacer 的父类**：`run()` 每层编舞（都回家版/留人版两套）；`place_gate()` 分工位（被 GA 覆写的正是它）；`place_qubit()` 分床位；`filter_mapping()` 终审择优 |
| `zac/router/router.py` | ⑨ 排车 | `route_qubit_mis()` 逐层生成搬移批次（贪心独立集）；`compatible_2D()` 判两趟搬移能否并车（非交叉/保序）；`aod_assignment()` 多车负载均衡；`expand_arrangement()` 展开成夹起-移动(含泊车)-放下的机器指令 |
| `zac/simulator/simulator.py` | 考核 | 从 ZAIR 指令流算保真度与电路时长（五项误差连乘），GA/ZAC 共用同一把尺子 |
| `zac/verifier/verifier.py` | 合法性 | 检查调度与座位表合法（每道工序后的质检） |
| `zac/animator/animator.py` | 可视化 | 把指令流画成原子搬运 mp4（需 ffmpeg） |
| `zac/ds/architecture.py` | 硬件模型 | SLM/AOD/区域的解析；陷阱间距离、移动时长 √(d/a)；"最近工位"查询 |

## 数据依赖（同在 GA 文件夹内）

- `hardware_spec/full_architecture.json`：ZAC 原版架构（注意其 `1qGate:52` 会启用
  52μs 单门时长；见下一条）
- `hardware_spec/zac_arch_repro.json`：**复现论文数据用的修正版**（删除 `1qGate` 键
  → 代码回退默认 0.625μs，与 HPCA'25 论文 CSV 的生成行为一致；原因见仓库
  `experiments/` 下的复现报告）
- `benchmark/hpca/`：论文的 18 个 QASMBench 电路
- `benchmark/toy_example/`：14 比特玩具电路（冒烟测试用）
