# 实验演进与后续开发起点

本页记录截至 2026-10-01 的方法演进、有效证据和未采用的尝试，供继续开发使用。数字来自保留的协议、汇总与逐项结果，不依赖本轮清理的旧原始轨迹。所有保真度均为统一物理模型下的估计值，不是硬件测量。

## 1. 先辨认当前版本

| 对象 | 当前入口与含义 |
|---|---|
| 论文主配置 | `physical_prefix_ga` 初始化 + 动态 GA-LK H8；[冻结协议](../ZAC_zzx/results/physical_ga_main_v1/protocol.json)、[结果入口](../ZAC_zzx/results/physical_ga_main_v1/README.md) |
| 源码默认配置 | 仍是 `physical_prefix`：SA 布局加最多四个候选的物理前缀评价；[默认配置](../ZAC_zzx/exp_setting/ga_lk_default.json)。论文采用 GA 初始化没有自动改变此默认值 |
| 当前主结果 | 模型保真度几何均值最高提高 **26.42%**，平均 MOVE 批次最多减少 **25.86%**；[宏与统计来源](../ZAC_zzx/results/physical_ga_main_v1/paper_exports/ga_main_values.json) |
| 历史接受包 | [paper_zh_v2](../ZAC_zzx/results/paper_zh_v2/final_manifest.json)，保留旧主比较及动态搜索、前瞻、预算研究；不能用它的旧主表替换当前 GA 初始化主表 |
| 开发代码 | Python 编译入口与初始布局在 [zzx](../ZAC_zzx/zzx/)，动态物理搜索在 [native](../ZAC_zzx/native/README.md)，协议驱动在 [experiments_v2](../ZAC_zzx/experiments_v2/) |

两个基线分别是 Lin、Tan、Cong 的 **ZAC**（HPCA 2025，*Reuse-Aware Compilation for Zoned Quantum Architectures Based on Neutral Atoms*），以及 Stade、Lin、Cong、Wille 的 **routing-aware placement**（ICCAD 2025，*Routing-Aware Placement for Zoned Neutral Atom-Based Quantum Computing*）。后者的历史方法键为 `ICCAD`，实现通过 MQT QMAP 使用；`QMAP154` 又是基准集的内部键，两者不能混为同一个概念。正式引文见 [references.bib](../IEEE_conference_template/references.bib)。

本项目的工作主线是：**联合选择门位、驻留和回迁位置，并从候选执行后的真实配置预测后续物理损失**；初始化后来也使用同一物理评价。已有门调度、匹配、路由及 ZAIR 输出承担支撑作用。Python/C++ 实现、物理约束修复和协议验证使方法可执行、可比较，不应分别包装成额外的方法贡献。

## 2. 方法是怎样发展过来的

| 阶段 | 当时要解决的问题与实现路线 | 留下来的判断 |
|---|---|---|
| M3 / M4 驻留搜索 | 将驻留与门位、回迁关联；M3 是历史无前瞻配置，M4 加入多层预测。早期还经历过冲突罚项、启发式评分和参数调优 | `M3` 不是当前 M4 只关闭 H 的严格消融：配置也有差异，不能用 M4/M3 比值直接解释前瞻收益 |
| native 物理搜索 | 将解码、匹配、路由评价、前瞻和搜索放进 C++；修复非目标交点、驻留曝光顺序、RETURN 后再次入区与中转等物理语义 | 先让比较对象物理可执行，再讨论搜索优劣。代码提交记录证明改过什么，不独立证明某次修复带来了多少增益 |
| 物理损失前瞻 | 从候选执行后的位置、占位和时间出发，预测未来执行；以转移、空闲受激与退相干损失统一评分，并限制预测范围与评价预算 | 保留为核心方法。受控 H8/H0 证据支持前瞻，但不保证每条电路都获益 |
| SA + 四候选初始化 | 在 SA 布局附近形成候选，用早期层实际执行损失选择起点，而不是只优化距离 | 成为源码默认 `physical_prefix`；保存独立主矩阵 `default_initial_v1`，没有覆盖更早证据 |
| 物理 GA 初始化 | 直接搜索原子到存储位的排列；结构化种子、交叉与变异负责产生候选，早期物理执行损失负责评价，不调用 SA | 先完成四组先导，再开展完整主矩阵；成为论文显式配置，旧默认仍可用 |

可沿 Git 检索这些实现节点：`e8c7053a`（M0–M4 驻留编译与验证）、`4fe3f689`（驻留移动与 Rydberg 曝光顺序）、`4c308321`（物理前瞻）、`08669bcf`（RETURN 再入区）、`51583a50`（Python 参考物理预测）、`c5d5df23`（有界衰减前缀）、`f7c75449`（当前 GA 初始化证据与双语论文整理）。例如 `git show 08669bcf` 可查看实现差异。这些是演进定位点，不是可互换的实验版本；原运行身份以各协议的源码快照、native wheel 和输入哈希为准。

### 动态方法已经支持哪些结论

历史接受包的 M4 相对**逐电路较强基线**，在 ZAC 的 18 条电路和 QMAP 的 120 个 canonical 输入上，保真度比值分别为 **1.0795**、**1.2478**。这是当时配置和集合下的结果，与当前分别比较两条基线的口径不同。[历史汇总](../ZAC_zzx/results/paper_zh_v2/main_summary.json)与[宏](../ZAC_zzx/results/paper_zh_v2/paper_values.json)保留了这些数值。

更适合解释设计作用的是同配置受控比较：

| 比较 | 有效电路数 | 保真度几何均值增幅 | 胜 / 平 / 负 | 能回答的问题 |
|---|---:|---:|---:|---|
| 动态 H8 对 H0 | 39 | 23.08% | 20 / 8 / 11 | 在其余设置固定时，加入这套多层物理预测是否有帮助 |
| GA 对逐坐标贪心 | 38 | 0.99% | 14 / 17 / 7 | 在同一个联合决策空间内，遗传搜索与该贪心策略有什么差异 |

来源：[ablation.csv](../ZAC_zzx/results/paper_zh_v2/ablation.csv)、[paper_values.json](../ZAC_zzx/results/paper_zh_v2/paper_values.json)。两个比较来自不同样本，增益不能相加；GA/贪心比较也没有验证“联合决策优于顺序决策”。当前初始化更换后，这些仍是原固定起始布局下的动态研究，不能重命名为新 GA 初始化的配对消融。

## 3. 初始化为什么从四候选走到 GA

### 先把评价依据统一，再改变候选搜索

`physical_prefix` 的第一步是保留 SA 产生的布局及少量交换扰动，用首层和后两层的物理执行损失选取起点。它解决了“距离小不一定实际输运损失小”的问题，但候选只覆盖原布局附近。

该阶段独立主矩阵对 ZAC 基线的保真度增幅为 ZAC **8.90%**、QMAP **26.24%**，批次减少 **19.19%**、**25.85%**；对应 16 / 120 个 canonical 输入。来源为 [default_initial_values.json](../ZAC_zzx/results/default_initial_v1/paper_exports/default_initial_values.json)。它是已保存的旧方案结果，不是现稿的主数值，也不是证明初始化单独贡献的对照实验。

GA 路径将染色体定义为原子到存储位的排列，三个结构化种子及其扰动构成初始种群。种子可以读取全电路交互，适应度仅评价规定早期前缀。外层最多评价 **32** 个唯一映射，种群 **8**、精英 **2**、OX 交叉概率 **0.25**；交换、插入和反转变异，最多 **6** 代、连续 **3** 代无改进或 **320** 次提案停止。内层编码预算另为 **32**，内层前瞻关闭；H2 指当前首层加后两层，衰减为 **0.7**。外层映射预算与内层编码预算不能合并计数。实现与边界见 [physical_initial_ga.py](../ZAC_zzx/zzx/physical_initial_ga.py)，冻结设置见[主协议](../ZAC_zzx/results/physical_ga_main_v1/protocol.json)。

### 四组先导支持采用什么、不支持什么

先导固定九类电路、三个种子，比较 SA4-H2、GA-H2、GA-H0、Random-H2，共 **108** 项。纠正驱动后完成 **101** 项，**7** 项超时均来自 `ising_n42`；四组全部三种子有效的统计集合为 **8** 条电路。每个字段先取种子中位数，再做配对几何平均。[先导说明](../ZAC_zzx/results/physical_ga_initial_v1/corrected_driver_v1/README.md)、[完整结果](../ZAC_zzx/results/physical_ga_initial_v1/corrected_driver_v1/exports/final/result.json)。

| GA-H2 的参照 | 保真度增幅 | 胜 / 平 / 负 | 解释边界 |
|---|---:|---:|---|
| SA4-H2 | 1.1496% | 3 / 0 / 5 | 位置范围与搜索都改变，是初始化方案整体比较 |
| GA-H0 | 0.0955% | 4 / 0 / 4 | 支持这组样本的早期多层评价有小幅平均收益，不是普遍提升 |
| Random-H2 | 1.2167% | 4 / 1 / 3 | 同位置范围、初始种群和评价上限，提供遗传搜索的比较证据 |

随机组后续独立采样，GA 根据已有分数生成后代；H0/H2 后续候选池不要求相同。因此不要把目标函数值跨 H 直接比较，也不要将这三个增幅相加。三个种子是质量统计，不是计时重复；实际执行混合了串行和并行调度，不能据此宣称 GA 编译更快。

初版驱动曾将 tuple/list 序列化差异误判为一致性失败；修复后按新身份重跑整个预定矩阵，而非只重试有利样本。初版失败记录仍保留。当前论文的三种新策略视图没有抹去原四组证据；先导的 `formal_paper_result=false` 身份也不因其支持后续主实验而改写。

## 4. 当前主结果及其边界

物理 GA 主矩阵包含 **169** 个 canonical 输入、**172** 个文件标签，三个种子共 **507** 项；完整复用先导同配置的 27 项，其余 480 项新运行。最终 **480 成功、18 超时、6 程序错误、3 内存限制**；成功中 **64** 项处于保真度模型适用域之外，另表保留。来源：[README](../ZAC_zzx/results/physical_ga_main_v1/README.md)、[全部记录](../ZAC_zzx/results/physical_ga_main_v1/paper_exports/canonical_runs.csv)、[失败](../ZAC_zzx/results/physical_ga_main_v1/paper_exports/failures.csv)、[模型 OOD](../ZAC_zzx/results/physical_ga_main_v1/paper_exports/completed_ood.csv)。

主统计先逐文件逐字段取三个种子的中位数，再合并相同 canonical 输入的别名。保真度跨电路用几何均值；批次与物理时延用算术均值。三方法完成且模型有效的同一集合才进入该行比较。

| 基准集 / canonical 数 | 参照方法 | 保真度增幅 | 平均批次减少 | 保真度胜 / 平 / 负 |
|---|---|---:|---:|---:|
| ZAC / 16 | ZAC | 5.95% | 15.69% | 12 / 0 / 4 |
| ZAC / 16 | Stade et al. | 8.43% | 5.99% | 13 / 0 / 3 |
| QMAP / 119 | ZAC | 26.42% | 25.86% | 48 / 0 / 71 |
| QMAP / 119 | Stade et al. | 26.28% | 25.22% | 46 / 0 / 73 |

来源：[ga_main_values.json](../ZAC_zzx/results/physical_ga_main_v1/paper_exports/ga_main_values.json)、[逐电路分析单位](../ZAC_zzx/results/physical_ga_main_v1/paper_exports/analysis_units.csv)。QMAP 的几何均值提高与多数电路未提高同时成立，后续优化应看分布而不只看均值。新集合因 `ising_model_13` 的一个种子超时，比旧四候选主结果少一个 canonical 输入；**26.42% − 26.24% 不是 GA 初始化的单因素收益**。两方案在 ZAC 的均值也没有呈现相同方向的变化。

`qft_10`、`qft_16` 的程序错误来自空门序列快速路径后缺少初始化报告字段；`urf1_149` 是已选出初始化映射后的内存限制。这些不能统称为“GA 搜索失败”。修复它们应形成新运行与新结果版本，不能修改原失败状态。

## 5. 前瞻范围、预算与未采用的尝试

### 动态前瞻不是越深越好，也不能只看名义 H

[horizon_extension_v2](../ZAC_zzx/results/horizon_extension_v2/combined_quality_summary.json) 在固定起始布局下比较 H0/1/2/4/8：12 条计划电路、三个种子，合并 180 条执行记录；10 条进入全设置有效集合，另外两条的 30 条记录为模型 OOD。该研究没有启动独立计时。下表是相对 H8 的配对几何均值，**这里的批次比口径不同于主表的算术均值降幅**。

| 动态 H | 保真度比 | 批次比 |
|---:|---:|---:|
| 0 | 0.78245 | 1.20445 |
| 1 | 0.96577 | 1.01648 |
| 2 | 0.97939 | 1.00336 |
| 4 | 0.99997 | 0.99858 |
| 8 | 1.00000 | 1.00000 |

这组样本的平均收益主要出现在引入短程前瞻之后；H4/H8 接近还受实际剩余层数和规模截断影响，不能据此断言任意电路都只需 H4。历史回执中的 runner 状态与重新判定后的物理执行/模型状态分别保存，不能只读一个 `success` 字段决定样本是否有效。

### 更小预算是候选方向，更大预算没有自动带来收益

历史单因素预算研究在 10 条保真度有效电路上，将动态唯一编码上限从 576 改为 192，保真度比为 **1.0012**；将 RETURN 候选位置/分配数从 6/4 改为 4/2，比为 **1.0011**。[历史分数](../ZAC_zzx/results/paper_zh_v2/sensitivity.csv)、[汇总宏](../ZAC_zzx/results/paper_zh_v2/paper_values.json)。

对应 12 条电路，扣除完整初始化后的剩余编译时间比为 **0.7572 / 0.7517**；这是历史四路并行执行内的配对观察，不是新 GA 初始化端到端计时，也不是纯动态内核时间。[当前导出中的独立计时来源](../ZAC_zzx/results/physical_ga_main_v1/paper_exports/ga_main_values.json)保留了计算口径与审计哈希。它支持“值得继续验证较小预算”，尚不足以把新主配置直接改为更小预算。

另一次 RETURN 扩展开发实验明确没有通过预设验收：

| 变体（相对 6 个位置 / 4 个分配） | 有效电路数 | 保真度变化 | CPU 时间变化 | 决定 |
|---|---:|---:|---:|---|
| A：位置扩为 8 | 11 | −0.1302% | +0.8907% | 未通过收益、分数据集不退化及单电路退化约束 |
| B：分配扩为 6 | 11 | +0.0437% | +19.7737% | 未达到预设收益要求 |

来源：[refinement 协议](../ZAC_zzx/results/refinement_v1/return-coverage-v1-smoke-20260907/protocol.json)、[development/decision.json](../ZAC_zzx/results/refinement_v1/return-coverage-v1-smoke-20260907/development/decision.json)。`selected_variant=null`、`keep_original_method=true`、`formal_paper_result=false`。烟雾测试通过只说明可运行，不等于优化验收通过；该研究没有产生新的正式主结果。

## 6. 后续从哪里继续

优先延续已实现的联合物理评价与明确的状态边界，不要退回只以几何距离或 MOVE 数代替保真度损失。可检验的下一步有三类：

1. **降低评价成本。** 对前缀仿真、候选缓存和动态预算做有针对性的性能分析；在同一电路、配置、种子与调度条件下验证是否保持质量。较小预算的历史观察是起点，不是新 GA 配置的现成结论。
2. **补齐覆盖与鲁棒性。** 分别解决空电路报告路径、超时和内存峰值；对 QMAP 的逐电路退化分布分析具体门结构和物理损失分量。失败恢复、模型适用域扩大与优化收益要分开统计。
3. **检验新的搜索假设。** 若继续改 RETURN 域、初始化种子或自适应 H，先固定问题、控制变量、输入哈希和预算，再开独立结果目录。保留旧失败与原比较集合，不能通过重试、替换样本或更改旧宏获得增幅。

当前开发同步节点是 `f7c75449`，但原主实验源码身份以 [protocol.json](../ZAC_zzx/results/physical_ga_main_v1/protocol.json) 的 `frozen_source`、`source_snapshot_sha256` 和 native 身份为准。主协议 SHA-256 为 `9bb0ae97d2f3f1536a5c496a65fe2e21e2192ce20830979dd75b30b0a3148fc7`；对应 native ABI 9、wheel SHA-256 为 `1d6c66cc852ec6b1136bbe50d2b0977c144730dbf93a1e6f233fac3602d12b04`。不能只 checkout 一个 Git 提交就宣称重现了原运行环境。

配置新实验时，直接采用冻结协议中的完整设置，或按 [REPRODUCIBILITY.md](REPRODUCIBILITY.md) 从默认配置显式切换：设 `init_strategy=physical_prefix_ga`、`init_engine=ga`，移除 SA 专用的 `initial_lookahead`、`init_pop`、`init_gens`，并使用 `initial_ga` 控制项。不要静默改默认配置或继续写入冻结输出目录。

已有主结果可从仓库根进行只读复核；这里重算保存分数、集合与统计并检查绑定证据，不重跑编译：

```bash
PYTHONPATH=ZAC_zzx ZAC/.venv/bin/python -B \
  scripts/paper/verify_ga_publication.py \
  --protocol ZAC_zzx/results/physical_ga_main_v1/protocol.json \
  --output-root ZAC_zzx/results/physical_ga_main_v1/paper_exports --check
```

双语严格构建使用 `make paper-en PYTHON=ZAC/.venv/bin/python`，相关检查用 `make paper-test PYTHON=ZAC/.venv/bin/python`。源码轻量导出与仅凭已捆绑宏渲染是另一条路径，不能代替证据复核。完整输入、原冻结环境和部分保留原始证据仍仅在本地，公共 Git 克隆不等于完整实验环境；获取与运行说明见 [DATA_AND_CODE.md](DATA_AND_CODE.md) 和 [REPRODUCIBILITY.md](REPRODUCIBILITY.md)。

## 7. 2026-10-01 清理后的保留边界

本轮已按授权清单清理归档中的旧实验轨迹、详细搜索日志及旧方法重复输出，以及活动历史包中已逐文件核对的旧 M3/M4/GA-NL 轨迹和大体积统计记录；未参与当前复现的本地 QASMBench 副本也已移除。不是删除所有失败实验，也不是把整个 `fidelity-lookahead-v2/` 当作缓存处理。协议、配置、源码版本、输入身份、分数、汇总和失败回执仍用于追溯上面的路线与决定。逐文件执行和保留校验回执位于本地 [archive/_manifests/prune-20261001](../archive/_manifests/prune-20261001/)，不随 Git 分发。

保留当前主实验和先导所需原始证据、两套基准完整 canonical 输入、344 项基线对应的真实轨迹/分数、冻结源码与 native 运行环境，以及当前受控、计时、前瞻范围和补充研究的必要证据。新主结果仍按原严格检查使用这些文件；没有为省空间放宽检查或改写冻结哈希。

**历史全量封存记录不再保证逐文件可回放。** 保留下来的旧 manifest、seal 或 provenance 仍可能指向已按清单删除的历史 raw；它们说明原来有什么，不表示这些文件现在仍在。旧小表可以复查已保存指标，但不能据此重新验证已删除轨迹的物理执行，也不能重演每次搜索过程。新的实验应从保留的源码、canonical 输入和明确配置重新运行，并使用新结果身份。目录与恢复规则见 [ARCHIVE_POLICY.md](ARCHIVE_POLICY.md)、[REPOSITORY_MAP.md](REPOSITORY_MAP.md)。
