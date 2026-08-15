# ZAC_new —— 把"搬运批次"（图着色）放进 ZAC 的决策回路

从 ZAC（HPCA'25）派生的独立实验文件夹，与 `GA/`、`FABLE/` 同级、互不引用。
自包含：本地 `zac/` 是 ZAC 源码的字节级副本（`diff -r` 校验过），整个文件夹
可单独拷走运行，只需一个装了 qiskit/scipy/rustworkx 的 Python 3.10。

## 解决的两个问题

1. **放置看不见批次**。ZAC 的 `place_gate` 用最小权完美匹配分工位，边权是
   纯距离（√d1+√d2+√前瞻）；"两个门各选某工位会冲突、要多开一辆车"是决策
   间的二次交互项，匹配的边权表达不了。批数于是成了路由阶段贪心剥离的
   副产品——没有任何阶段对它负责。
2. **贪心剥离 ≠ 最少批数**。`maximalis_solve` 逐轮取"极大"独立集发车，
   总批数可能多于色数下限 χ。

## 三个改动（一一对应）

| 改动 | 位置 | 机制 |
|---|---|---|
| "怎样算一批" | `znew/zcost.py` | `compatible_2d`（router.py:232 逐行移植，2 万对差分验证）→ 冲突图 |
| "要几批" | `znew/zcost.py` | DSATUR：启发式（适应度内循环）+ 精确分支限界（≤24 节点，路由每层一次）。**最少批数 = 冲突图色数 χ，每色 = 一批** |
| 放置双引擎 | `znew/zplacer.py` | **B "penalty"**（主推）：保留 ZAC 匹配机器，按冲突条数给涉事边开罚单迭代重解——便宜（每层 8 次匹配+着色）、确定、三篇论文都没做过的形态；**A "ga"**（对照）：FABLE v1a 进化骨架，适应度换成着色分批（与 FABLE 的差异 100% = 分批器）|
| 路由着色分批 | `znew/zac_new.py` | `routing_strategy="coloring"`：一次着色出全部批次（每色=独立集=一批），`process_movement_layer`/停车/AOD 分配零改动复用。默认仍 `maximalis_sort`，可关 |

适应度：`w_batch×χ + Σ批 √(批内最长腿) + 复用前瞻`（量纲 √μm，与 FABLE 同构）。

## 正确性证据链（三层）

1. **构造层**：色类=独立集（批内两两兼容），着色覆盖全部节点（都运达），
   下游机制零改动。
2. **单测**：`tests/test_zcost.py` 7/7——铁律逐条、2 万随机对差分、
   教科书图 χ、精确档 vs 暴力对拍 200 张、K₃,₂ 上"启发式 3 批→精确 2 批"。
3. **独立回放**：`verify_batches.py` 读落盘 ZAIR 指令流逐批检查
   （批内兼容/位置连续/时序依赖/门邻接）：4 toy + **ZAC 原版 18 电路全过**
   （证明校验器可信）+ ZAC_new 36 个输出全过 + 篡改样本 5 类违例全抓到。

**回归保障**：`placer="zac"` + `maximalis_sort` 的输出与 ZAC 原版
**逐字节一致**（toy 上 diff 验证）；同放置下 coloring 路由与原版路由等价
（玩具上批数/runtime 完全相同——贪心已达 χ 时着色不多不少）。

## 用法

```bash
ZAC/.venv/bin/python ZAC_new/run.py ZAC_new/exp_setting/zac_new_toy.json    # 冒烟
ZAC/.venv/bin/python ZAC_new/run.py ZAC_new/exp_setting/zac_new_repro.json  # 18 电路 penalty
ZAC/.venv/bin/python ZAC_new/run.py ZAC_new/exp_setting/zac_new_repro_ga.json # 18 电路 ga
ZAC/.venv/bin/python ZAC_new/verify_batches.py results/repro_penalty/code/*.json
ZAC/.venv/bin/python ZAC_new/compare.py        # 五方对比表（ZAC/GA/FABLE/B/A）
```

旋钮全在配置里（`engine/w_batch/lambda_penalty/penalty_decay/max_penalty_iter/
population_size/.../coloring_exact_threshold`），白名单见 `zac_new.py` 的
`ZAC_NEW_KEYS`（FABLE 的教训：不在白名单的键会被静默丢弃）。

## 结果速览（18 电路全量，`results/comparison_table.md`）

| | ZAC 真值 | GA v1a | FABLE | **ZAC_new-B** | **ZAC_new-A** |
|---|---|---|---|---|---|
| 时长 geomean（对 ZAC） | 1.000 | 0.948 | 0.942 | **0.959** | **0.943** |
| 保真度 geomean | 0.3236 | 0.3277 | 0.3287 | 0.3238 | 0.3274 |
| 总批数 | 1702 | 1724 | 1711 | 1707 | 1749 |
| 放置耗时（18 电路总和） | 1.26s | — | 25.15s | **4.82s** | 40.83s |
| χ 预演=实际批数 | — | — | — | **689/689 层** | **689/689 层** |

- **B（匹配+定向罚单，确定性）**：ising_n42 0.668、**ising_n98 0.661（五方最佳**，
  23→16 批、平均 18.3 原子/批）、swap_test 0.965；串行电路大多保持 1.000
  （best-ever 兜底）。放置耗时仅为 A 引擎的 1/8、FABLE 的 1/5。
- **A（ga+着色适应度）**：geomean 0.943 追平 FABLE（0.942），但电路分布不同
  ——高并行更强（ising_n98 0.861、knn 0.984），qft_n29 因回程盲区 0.872。
- **消融三则**（详见 results/abl_*）：
  ① w_batch ∈ {0,0.25,0.5,1,2} 扫描完全平坦（0.871±0.001）——起作用的
  是**着色分解器**本身（对比 FABLE 贪心轮），不是批数权重大小；
  ② 无向罚单（只罚所选边）对 λ∈{0.25,1,3}×衰减{0.7,1.0} 完全免疫
  （八配置输出全同）——均匀涨价不改变匹配 argmin，罚单必须**定向**
  （每门×每工位算"坐这儿会冲突几条"）；
  ③ 路由隔离测试：同放置下 coloring 路由与原版 maximalis 完全等价
  （玩具批数/runtime 逐项相同）——路由换着色无副作用，收益全在放置。

已知局限（v1.1 方向）：适应度只预演去程，回程（place_qubit 归位）不可见，
qft_n29 上定向罚单 1.077 回退；根治 = 适应度加"座位→家"回程代理项。


## 与三家论文的坐标

ZAC 距离匹配（2024）→ ZAP 公式 17 加冲突项+单遍快编（2024.11）→
ICCAD'25 A* 全局搜索（-17% 步数，放置时间 ×4-40）。
**"直接对着色数 χ 优化"这个位置三家都没占**：ZAC 的 `mis` 策略（kamis）
仍是逐轮最大集 + 外部二进制 + 3600s 限时；本文件夹用纯 Python DSATUR
一次求全局最少批数，并把它同时放进放置目标（B/A 引擎）与路由分批器。
