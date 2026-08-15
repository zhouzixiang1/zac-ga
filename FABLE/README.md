# FABLE —— 把 Fable 笔记的"2q 放置优化"复现到 ZAC 底盘上

与 `GA/` 同级的独立实验文件夹：**自包含**（本地 `zac/` 副本与 `ZAC/zac`
逐字节一致）、**零污染**（不 import、不修改 GA/ 与 ZAC/ 的任何东西）。

## 实验问题

Fable（用户自建编译器，Windows 机器 `Fable5_coded_compiler`）用
"2q 放置优化"做到了 geomean **0.938×**（比 ZAC 快）、平均保真度 0.4831。
这套想法装到 ZAC 底盘上（控制变量：调度/复用/路由全用 ZAC 原版），
能不能做到相同？

## Fable 想法 → ZAC 零件的对应表

笔记《中性原子编译》（3）2q放置优化 的原文只有两句话：
"采用了'交换位置''邻居位置'作为邻居解点；评价函数改成了 最大链 + 冲突边 加权"。

| Fable 概念 | 本文件夹的实现 | 出处 |
|---|---|---|
| 冲突图/冲突边 | `fcost.compatible_2d`（两腿 x/y 保序则兼容，否则一条冲突边） | 逐行移植 `ZAC/zac/router/router.py:232` |
| 最大链 | `fcost.stage_decompose`：按距离降序、贪心 MIS 分轮，链耗 = Σ 每轮 √(轮内最长腿) | 照搬 `route_qubit_mis` 的批次循环（router.py:77-90）+ maximalis_solve（:182） |
| 交换位置（邻域算子①） | `fplacer.neighbor_solution`：两件活互换工位（翻译回基因语言，解码器保证合法） | 笔记 |
| 邻居位置（邻域算子②） | 同上：基因 ±1 = 挪到按路程排序的隔壁工位 | 笔记 |
| 参数 | population=6, iterations=8, neighbors=2, sample=24 | 笔记原文记录的参数 |

评估函数 = 最大链 + 冲突边×w_conf(默认1.0) + 复用前瞻 Σ√dis3（开关可关）。
GA v1a（`GA/` 文件夹）= 同底盘同预算 + ICCAD 分组评估 + 朴素变异——
两组实验的差 = 评估函数 + 算子，这就是"复现 Fable 想法"的对照设计。

## 文件结构

```
FABLE/
  fable/
    fcost.py       打分仪表：最大链 + 冲突边（≈80 行）
    fplacer.py     发动机：FablePlacer(VertexMatchingPlacer)，只覆写 place_gate
    zac_fable.py   转接头：ZAC_FABLE(ZAC)，覆写 parse_setting + 中间布局入口
  zac/             ZAC 编译器源码逐字节副本（引擎底盘，零修改）
  benchmark/       与 GA 相同的电路（toy + hpca 18 电路）
  hardware_spec/   与 GA 相同（含去掉 1qGate 键的 zac_arch_repro.json）
  tests/test_fcost.py   单测（含与 ZAC 原版 router 的 2 万对随机差分对比）
  exp_setting/     fable_toy.json / fable_repro.json
  paper_truth/     fable_notes.csv（笔记 compiler_search 列 18 电路转录）
  compare.py       四方对比表：本实验 / ZAC 真值 / GA v1a / Fable 笔记原生
  run.py           入口（与 GA/run.py 同构）
```

## 跑法

```bash
ZAC/.venv/bin/python FABLE/run.py FABLE/exp_setting/fable_toy.json    # 冒烟
ZAC/.venv/bin/python FABLE/run.py FABLE/exp_setting/fable_repro.json  # 18 电路全量
ZAC/.venv/bin/python FABLE/compare.py                                 # 对比表
```
