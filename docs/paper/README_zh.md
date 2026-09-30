# GA-LK 中文论文

2026-09-20已完成[全部文献核对及匿名修订](notes/20260920_reference_anonymity_audit.md)，移除本人仓库链接，修复参考文献显示和元数据；在线核验版本为 `5cd7151`。摘要、关键词和数据不变，仓库本身保留。

最新[引言衔接与图4留白调整](notes/20260919_intro_story_layout.md)提前引出当前与后续执行的权衡，让算例、相关工作与两项贡献自然衔接，并收紧图4及英文第二页左下的留白。此前[引言与背景完整合并](notes/20260918_intro_bg_restored.md)按作者要求保留原一、二章的信息量，将完整背景留在引言，恢复后续主要图表与算法的位置。[本地论证调整](notes/20260918_local_story_merge.md)中的背景迁入方法和算法后移安排已撤回，结论保留。该版于2026-09-19同步为 `ffdf237`，见[当时同步记录](notes/20260919_overleaf_sync.md)；当前版本已更新至上述匿名修订。此前日期化记录只描述各自当时版本。

活动稿为 [paper_zh.tex](../../IEEE_conference_template/paper_zh.tex)，英文在 [paper_en.tex](../../IEEE_conference_template/paper_en.tex)。两语言保持相同论证、公式、引用与数据，在线稿已包含本轮修改；导出版本通过独立本地编译，未运行浏览器端编译。

## 当前组织

| 位置 | 论证职责 |
|---|---|
| 摘要与关键词 | 原样保留，仅英文摘要的 AOD 全称缩为 AODs |
| I 引言 | 原引言与背景的完整内容：分区架构与SLM/AOD、AOD约束、保真度模型、驻留算例、相关研究、两项贡献 |
| II 方法 | 联合放置建模→跨层物理损失评价→遗传搜索与初始布局→可执行输出 |
| III 实验评估 | 比较设置→总体收益与物理来源→前瞻与搜索的作用→计算预算 |
| IV 结论 | 回答研究问题、概括主要结果并解释其物理与编译意义 |

正文为四张图、一张总体结果表和一个引领算法块。算法在方法总述后引用两语言对应的 `03_algorithm.tex`，以两个阶段、14行概括联合搜索与跨层评价、选择并推进当前层；准入和失败处理规则集中在方法C节。图1为架构与约束；图2保留六阶段框架，输入与调度用同一重基CZ/U3电路，中间用一般门层说明候选后预测及实际配置反馈；图3为完整候选与必要中转；图4为单栏前瞻范围比较。图4保留10个电路、5档 H、三个种子中位数及各电路 H8 归一口径，单栏内左右两幅图分别展示保真度比和批次比，粗线均为电路间几何均值。早期[图片修正记录](notes/20260914_figure_repair.md)保留为历史。

旧图2、5、6、案例表和旧版 `algorithm_precompact.tex` 保存在 [supplementary/](supplementary/)，不再由正文引用；新版引领算法使用独立文件。旧图3及旧配色也保存为历史实例测试依据。框架图的六阶段仍完整呈现，已有路由及 ZAIR 技术明确引用其来源。图号由 LaTeX 连续生成，源码名保留稳定语义。

[本轮改动记录](notes/20260914_compact_revision.md)说明压缩、退役与验收。历史日期记录仅描述其当时版本，之前“第一、二章严格使用云端”及“中文固定九页”的要求由本轮批准的双语压缩方案替代。

## 结果与来源入口

| 内容 | 来源与使用边界 |
|---|---|
| 已完成 GA 主实验与活动稿数据 | [结果入口](../../ZAC_zzx/results/physical_ga_main_v1/README.md)、[完成回执](../../ZAC_zzx/results/physical_ga_main_v1/execution_completion.json)、[GAMain 数值与来源](../../ZAC_zzx/results/physical_ga_main_v1/paper_exports) |
| 2026-09-12 已验收总体结果 | [default_initial_v1/paper_exports/](../../ZAC_zzx/results/default_initial_v1/paper_exports)；历史 `Default` 宏保留，不改标为新 GA |
| 原接受包与动态研究 | [paper_zh_v2/final_manifest.json](../../ZAC_zzx/results/paper_zh_v2/final_manifest.json)、[results_values_zh.tex](../../IEEE_conference_template/results_values_zh.tex)；保存对照、预算及历史串行计时 |
| GA 初始化内部比较 | [先导说明](../../ZAC_zzx/results/physical_ga_initial_v1/corrected_driver_v1/README.md)、[数值宏](../../IEEE_conference_template/physical_ga_initial_values.tex)、[派生来源](metadata/paper_physical_ga_initial_provenance.json) |
| 五档动态前瞻 | [horizon_extension_values.tex](../../IEEE_conference_template/horizon_extension_values.tex)、[独立来源](metadata/paper_horizon_extension_provenance.json) |
| 历史 SA 初始化扩展 | [initial_lookahead_v1/](../../ZAC_zzx/results/initial_lookahead_v1)；记录与失败保留，退出活动稿方法叙述 |
| 物理算例与显示宏 | [method_argument_values.tex](../../IEEE_conference_template/method_argument_values.tex)、[paper_argument_values.json](metadata/paper_argument_values.json) |
| 参数、样本与证据关系 | [实验协议](notes/experiment_protocol.md)、[论证架构](notes/03_argument_map.md)、[章节契约](notes/04_section_contracts.md) |
| 引文与术语 | [references.bib](../../IEEE_conference_template/references.bib)、[术语表](notes/06_terminology_ledger.md) |

初始化内部比较只读筛选原先导的 GA-H2、GA-H0、Random-H2 三组：81项、75成功、6次600 s超时，共同8电路。三臂资格按规范化身份重新核对，其共同集合与原四臂相同；原108项、101成功、7超时和 SA 参照均在完整导出中保留。新主实验复用全部27项先导 GA-H2，包括3次超时，不只选成功项，也不重试替换。

动态 H0/H8、遗传/贪心和五档 H 研究的保存 trace 已核对起始映射一致，正文因此把初始映射作为固定输入；这些局部对照没有使用新 GA 初始化。历史端到端172倍计时比退出活动稿，阶段计时按逐次剥离初始化后再聚合。新主实验并行执行，先导串行/并行混合调度的墙钟时间仅描述，不解释为串行速度比。

原接受包、历史结果、失败记录和冻结图数据不因正文改写而覆盖。旧26.24%/25.85%属于 SA 四候选配置，其 QMAP 共同集合为120电路；新集合因 `ising_model_13` 的一个种子超时减为119，故新旧增幅变化不能直接解释为初始化单因素效应。所有保真度是既定物理模型的估计值，物理时延与主机编译耗时分开。

## 构建与验收

在仓库根目录执行：

```bash
make paper PYTHON=ZAC/.venv/bin/python
make paper-en PYTHON=ZAC/.venv/bin/python
make paper-test PYTHON=ZAC/.venv/bin/python
```

中文自然分页，英文目标为六页正文加一页参考文献。两语言分别重建独立框架图及正文，默认清洁文字；图构建失败不能使用旧 PDF。检查源文件与输出身份、冻结数值、双语公式与引用、物理实例、摘要关键词不变量、字号、溢出及逐页排版。

输出统一位于 `IEEE_conference_template/build/`：

- [中文 PDF](../../IEEE_conference_template/build/paper_zh/paper_zh.pdf)与[验收报告](../../IEEE_conference_template/build/paper_zh/final_paper_qa.json)
- [英文 PDF](../../IEEE_conference_template/build/paper_en/paper_en.pdf)与[验收报告](../../IEEE_conference_template/build/paper_en/final_paper_qa.json)
- [实施前轻量快照](../../IEEE_conference_template/build/paper_zh/compact-20260914/source-before.tar.gz)

脚本和测试在 `scripts/paper/`，来源元数据在 `docs/paper/metadata/`，正文源码目录保持精简。历史构建、实验、失败记录和环境不作清理。

## 在线版本

Overleaf 的 Git checkout 为 `IEEE_conference_template/build/overleaf-sync/`。上次已核验同步为 `da33838`，见[同步记录](notes/20260918_intro_citation_layout.md)。本轮未访问、导出或更新Overleaf，当前本地合并稿与在线稿不同。后续同步仍须先核对云端新增内容，仅导出必要排版依赖并正常推送，禁止覆盖未合并的在线修改。

主仓库 [AGENTS.md](../../AGENTS.md) 与[仓库导航](../REPOSITORY_MAP.md)给出路径约定；日期化交付包仅证明对应历史版本。
