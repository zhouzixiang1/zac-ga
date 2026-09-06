# 中文稿参考文献与 baseline 事实审计

审计日期：2026-09-04

## 结论

- 当前 `paper_zh.tex` 及各章节实际引用23个BibTeX key，23个均已定义；XeLaTeX/BibTeX构建无未定义引用。
- ZAC 与 ICCAD/QMAP 的核心算法描述和能力边界均可由本地两篇原文直接支撑。
- 分区、AOD/SLM、Rydberg、transfer、idle excitation 与 ghost 约束均有本地原文支撑。
- 已修正两处证据表达：为 ghost 句补直接引用；把 ZAC 退相干项中的 $t_q$ 从“累计时间”改为原文定义的“累计空闲（退相干）时间”。
- 已用 Crossref 核实并补齐 ZAC、ICCAD、QCE及8项相关工作条目的作者、题名、年份、页码和DOI。

## 事实—证据对应

| 稿件陈述 | 本地一手证据 | 审计判断 |
|---|---|---|
| 存储区屏蔽空闲原子，纠缠区执行 Rydberg/CZ | `documents/md/Reuse-Aware_Compilation_for_Zoned_Quantum_Architectures_Based_on_Neutral_Atoms.md:15-17,55-75`；`documents/md/2025_iccad_routing-aware_placement_zoned_neutral_atom.md:45-48` | 直接支撑 |
| AOD 完成 pickup/load、移动与 drop-off/store；并行搬运受行列次序约束 | ICCAD 原文 `:48-58`；ZAC 原文 `:163-180` | 直接支撑 |
| ghost spot 来自同时激活 AOD 行列，额外交点会影响原子 | ICCAD 原文 `:50-58`，其约束来源指向 QCE 2024 routing model | 直接支撑；正文已补 `stade2024abstractmodel,stade2025routingaware` |
| ZAC 在相邻 Rydberg 层间用最大基数匹配决定立即复用 | ZAC 原文 `:119-131` | 直接支撑 |
| ZAC 用最小权 full matching 决定门位和非复用原子的存储位，并使用下一伙伴信息 | ZAC 原文 `:133-159` | 直接支撑 |
| ZAC Fidelity 包含 gate、idle excitation、atom transfer 和线性退相干项 | ZAC 原文 `:200-215` | 直接支撑；$t_q$ 是 qubit idling time |
| ICCAD/QMAP 用逐层 A* 搜索 gate/intermediate placement | ICCAD 原文 `:131-177` | 直接支撑 |
| ICCAD/QMAP 用兼容移动组及组内最大距离代理 routing time | ICCAD 原文 `:137-145,204-219` | 直接支撑 |
| ICCAD/QMAP look-ahead 使用下一交互伙伴，并可判断连续复用是否有害 | ICCAD 原文 `:149-153,221-229` | 直接支撑 |
| ICCAD/QMAP 未执行本文所述多层 ghost-safe 物理滚动预测 | ICCAD 的代价函数只定义当前 transition proxy 与 next-partner look-ahead，见 `:137-153,221-229` | 属于由公开算法边界得到的审慎推断；稿件没有写成“首次 look-ahead” |

## BibTeX 与编译检查

- 背景、基线与算法引用 key：`preskill2018quantum`、`bluvstein2022coherenttransport`、`evered2023highfidelity`、`bluvstein2024logicalprocessor`、`lin2025zac`、`stade2025routingaware`、`stade2024abstractmodel`、`murty1968assignment`、`brelaz1979coloring`、`holland1975adaptation`、`wille2023qmap`。
- 中性原子编译相关工作 key：`patel2022geyser`、`patel2023graphine`、`tan2022mapping`、`tan2024olsqdpqa`、`tan2025enola`、`wang2024atomique`、`wang2024qpilot`、`schmid2024hybrid`、`ludmir2024parallax`、`kirmemis2025weaver`、`ruan2025powermove`、`jang2025mantra`。
- 未定义引用：0。
- Crossref 核实：
  - ZAC：DOI `10.1109/HPCA61900.2025.00021`，页码 127--142。
  - ICCAD/QMAP：DOI `10.1109/ICCAD66269.2025.11240721`，页码 1--9。
  - QCE routing model：DOI `10.1109/QCE60285.2024.00098`，页码 784--795。
  - Geyser：DOI `10.1145/3470496.3527428`，页码 383--395。
  - GRAPHINE：DOI `10.1145/3581784.3607032`，文章61，页码 1--15。
  - OLSQ-DPQA：DOI `10.22331/q-2024-03-14-1281`，Quantum 8:1281。
  - Enola：DOI `10.1145/3658617.3697778`，页码 921--929。
  - Atomique：DOI `10.1109/ISCA59077.2024.00030`，页码 293--309。
  - Q-Pilot：DOI `10.1145/3649329.3658470`，文章306，6页。
  - PowerMove：DOI `10.1145/3676642.3736128`，页码 163--178。
  - Mantra：DOI `10.1145/3696443.3708937`，页码 459--475。

## 最终写作边界

- “ZAC/ICCAD 没有多层物理滚动预测”应继续表述为算法作用域比较，不应写成作者明示的缺陷。
- ZAC 的初始布局会按整电路 2Q 门加权优化；稿件关于“局部”的比较应限定在动态层边界的复用/RETURN 决策，当前稿件已基本遵守这一边界。
- 当前稿件不宣称首次 routing-aware placement、首次 look-ahead 或全局最优图着色，符合两篇 baseline 的优先权边界。
