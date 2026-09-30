# 参考文献 1–7 核对记录

核对日期：2026-09-20。范围是当前英文 `paper_en.bbl` 的前七条，以及中英文引言、方法和实验中对应的引用语境。只读核对，未改正文或 BibTeX。

采用 Crossref DOI 注册元数据，结合 Nature 官方全文、作者公开论文及仓库内两个 baseline 原文。Crossref 通过 Python 标准库访问成功；部分出版社网页经浏览工具会超时或进入 cookie 重定向，改用 Python 标准库后已取得三篇 Nature 及 Quantum 的官方页面元数据。

## 元数据结论

| 编号 / key | 核验信息 | 判定与建议 |
|---|---|---|
| 1 `bluvstein2024logicalprocessor` | 前三位为 Dolev Bluvstein、Simon J. Evered、Alexandra A. Geim；题名 *Logical quantum processor based on reconfigurable atom arrays*；Nature **626**，58–65，2024；DOI `10.1038/s41586-023-06927-3`。 | 现有作者顺序、题名、卷页、年份、DOI 正确。线上先发于 2023-12-06，但卷期出版为 2024-02-01，不能因 DOI/online 年份而改成 2023。可补 `number={7997}`。 |
| 2 `evered2023highfidelity` | 前三位为 Simon J. Evered、Dolev Bluvstein、Marcin Kalinowski；*High-fidelity parallel entangling gates on a neutral-atom quantum computer*；Nature **622**，268–272，2023；DOI `10.1038/s41586-023-06481-y`。 | 现有字段正确。可补 `number={7982}`。 |
| 3 `bluvstein2022coherenttransport` | 前三位为 Dolev Bluvstein、Harry Levine、Giulia Semeghini；*A quantum processor based on coherent transport of entangled atom arrays*；Nature **604**，451–456，2022；DOI `10.1038/s41586-022-04592-6`。 | 现有字段正确。可补 `number={7906}`。 |
| 4 `preskill2018quantum` | John Preskill；*Quantum Computing in the NISQ era and beyond*；Quantum **2**，79，2018；DOI `10.22331/q-2018-08-06-79`。 | 作者、题名、年卷和文章号正确；79 是文章号，以 Quantum 惯用的 `pages={79}` 表达不构成错误。 |
| 5 `stade2024abstractmodel` | Yannick Stade、Ludwig Schmid、Lukas Burgholzer、Robert Wille；*An Abstract Model and Efficient Routing for Logical Entangling Gates on Zoned Neutral Atom Architectures*；IEEE QCE 2024，784–795；DOI `10.1109/QCE60285.2024.00098`。 | 全部现有字段正确；作者公开稿 arXiv:2405.08068v2 对应同一论文。 |
| 6 `lin2025zac` | Wan-Hsuan Lin、Daniel Bochen Tan、Jason Cong；*Reuse-Aware Compilation for Zoned Quantum Architectures Based on Neutral Atoms*；IEEE HPCA 2025，127–142；DOI `10.1109/HPCA61900.2025.00021`。 | 全部现有字段正确。 |
| 7 `stade2025routingaware` | Yannick Stade、Wan-Hsuan Lin、Jason Cong、Robert Wille；*Routing-Aware Placement for Zoned Neutral Atom-based Quantum Computing*；IEEE/ACM ICCAD 2025，1–9；DOI `10.1109/ICCAD66269.2025.11240721`。 | 全部实质字段正确。`Atom-Based` 与 `Atom-based` 大小写、会议名称 `Computer-Aided` 与 Crossref 的 `Computer Aided` 仅为排版差异，不是错引。 |

前三篇以前三作者加 `and others` 输出 et al.，列出的作者顺序与完整注册名单一致。期号补充属于格式完整性改进，不应报告为原先错刊或错误文献。

## 引用与论述对应

- 文献 1–3 支持原子阵列、纠缠门与相干输运推动逻辑处理器发展的开篇。Nature 文献 1 的“Logical processor based on atom arrays”及 Figure 1 还直接支持分区保护、并行门和可寻址 Raman 单比特旋转。现有引用没有明显不支持的问题；若精确调整单比特 Raman 句，可将文献 1 加入该处，现有 ZAC 也讨论 Raman 指令。
- 文献 4 是 NISQ 噪声与可执行电路深度的总体背景引用，现有一句没有声称其直接研究本工作的分区编译。
- 文献 5 的 III-A 和 III-C 支持分区结构、行列不交叉、同行同列保持以及 ghost spots。文献 7 的 II-A 延续这些约束，IV–V 支持 A*、相容轨迹分组、最长轨迹决定并行移动代价、后续伙伴距离及复用节省的转移。
- 文献 6 的 V、VI、VII-B、IX 分别支持复用及位置选择、依赖调度、保真度模型和 ZAIR。现有模型取其乘积的对数，分量与原式一致；未把继承的支持模块包装成新增贡献。
- 需要注意的出处边界：文献 7 对 ghost spots 的显式说明着重 SLM/AOD **transfer** 的交点。文献 5 讨论一般扰动和避开原子间路径，但没有给出本稿实现的逐轨迹全程扫掠判据。因此，本稿“拾取、输运和放下过程中均不得扫过静止原子”应作为本工作的执行模型规则陈述，避免暗示 baseline 原文已经定义完全相同的检查。可在该句前加“本文的执行模型要求” / “Our execution model requires”；约束、代码与数据无需改变。

## 原始来源

各 DOI 元数据当日均成功返回：

1. [Crossref 1](https://api.crossref.org/works/10.1038/s41586-023-06927-3)；[Nature 官方全文](https://www.nature.com/articles/s41586-023-06927-3)。
2. [Crossref 2](https://api.crossref.org/works/10.1038/s41586-023-06481-y)；[Nature 出版入口](https://www.nature.com/articles/s41586-023-06481-y)。
3. [Crossref 3](https://api.crossref.org/works/10.1038/s41586-022-04592-6)；[Nature 出版入口](https://www.nature.com/articles/s41586-022-04592-6)。
4. [Crossref 4](https://api.crossref.org/works/10.22331/q-2018-08-06-79)；[Quantum 官方页面](https://quantum-journal.org/papers/q-2018-08-06-79/)（浏览工具超时，但 Python 获取官方 citation 元数据成功）。
5. [Crossref 5](https://api.crossref.org/works/10.1109/QCE60285.2024.00098)；[作者原文](https://arxiv.org/html/2405.08068v2)。
6. [Crossref 6](https://api.crossref.org/works/10.1109/HPCA61900.2025.00021)；本地原文 `documents/Reuse-Aware_Compilation_for_Zoned_Quantum_Architectures_Based_on_Neutral_Atoms.pdf`。
7. [Crossref 7](https://api.crossref.org/works/10.1109/ICCAD66269.2025.11240721)；[作者官网 PDF](https://www.cda.cit.tum.de/files/eda/2025_iccad_routing-aware_placement_zoned_neutral_atom.pdf)；[作者版本](https://arxiv.org/abs/2505.22715)。

总体：7 条元数据核验通过，0 条实质错引；3 条可补期号。正文有 1 处来源层次可澄清，不影响既有物理约束和实验。
