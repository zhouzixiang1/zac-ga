# 2026-09-20 编译器文献核对（10–19）

范围：当前 `references.bib` 中指定的十条编译器文献，以及中英文引言的对应引用。只读检查，未修改 BibTeX 或正文。

方法：逐项读取出版社登记到 Crossref 的 DOI 元数据，核对作者顺序、题名、年、会刊、卷期页码和 DOI；正文描述再与作者原文、作者实验室或会议官方网站核对。元数据原始记录保存在 `IEEE_conference_template/build/refs-audit-20260920/compilers-crossref.json`。ACM 网页直接打开返回 403，因此正式书目信息以其 Crossref 登记为准，不能声称直接读取了全部 ACM 正式 PDF。

## 需要修正

1. **`tan2022mapping` 第一作者署名**：当前 `Daniel Bochen Tan`，该篇正式 ACM/Crossref 登记为 **Bochen Tan**。推荐改为 `Bochen Tan and Dolev Bluvstein and Mikhail D. Lukin and Jason Cong`。该作者后续论文确实使用 Daniel Bochen Tan，但不应反向统一本篇署名。题名、2022 年、ICCAD、1–9 页、DOI 全部匹配。Article 107 可以补充；当前页码不是错误。
   - 正式元数据：https://api.crossref.org/works/10.1145/3508352.3549331
   - 作者单位公开书目（列 Article 107）：https://cdsc.ucla.edu/publications/
2. **`ruan2025powermove` 作者署名细节**：当前第五作者 `Travis S. Humble`，Crossref 和作者 arXiv 正文均署 **Travis Humble**。这是同一人的扩展写法，未导致认错作者，但若按正式论文署名逐字一致，推荐移除 `S.`。其余作者顺序、题名、2025 年、ASPLOS Volume 3、163–178 页及 DOI 匹配。
   - 正式元数据：https://api.crossref.org/works/10.1145/3676642.3736128
   - 作者原文首页：https://arxiv.org/html/2411.12263v1
   - 作者所属研究机构书目：https://impact.ornl.gov/en/publications/powermove-optimizing-compilation-for-neutral-atom-quantum-compute/

## 特别注意：不要把预印本作者列表混进正式版本

**`schmid2024hybrid` 当前三作者写法与正式 DAC 登记一致**：Ludwig Schmid、Sunghye Park、Robert Wille。arXiv 2311.14164 和研究组链接的旧 PDF 列出第四位 Seokhyeong Kang（位于 Park 与 Wille 之间），但正式 DOI 元数据不列此人，研究组的正式论文清单与后续引用也使用三作者。此处应保留当前正式版写法，不因预印本而自动加人。

- 正式元数据：https://api.crossref.org/works/10.1145/3649329.3655959
- 研究组正式书目：https://www.cda.cit.tum.de/research/quantum_qec/
- 显示四作者的旧预印本：https://arxiv.org/abs/2311.14164
- 作者 PDF（首页有 arXiv v1 日期）：https://www.cda.cit.tum.de/files/eda/2024_dac_hybrid_circuit_mapping.pdf

## 逐条结果

| Key | 核对结果 | 正文引用匹配 | 主要来源 |
|---|---|---|---|
| `tan2022mapping` | 第一作者改 Bochen Tan；其他字段匹配。ICCAD 2022，1–9 页。 | 布局和移动路由的概括受原文支持。 | https://api.crossref.org/works/10.1145/3508352.3549331 ; https://vast.cs.ucla.edu/projects/applications-architecture-and-compilation-quantum-computing |
| `schmid2024hybrid` | 三作者与正式登记匹配；题名、DAC 2024、1–6 页、DOI 匹配。 | 混合门作用与移动路由的概括受作者原文支持。 | https://api.crossref.org/works/10.1145/3649329.3655959 ; https://arxiv.org/abs/2311.14164 |
| `tan2024olsqdpqa` | 四作者、题名、Quantum 8 (2024), 1281、DOI 全部匹配。1281 为文章号，作为 BibTeX pages 常见且可保留。 | 联合安排布局、路由与门调度的概括受摘要直接支持。 | https://api.crossref.org/works/10.22331/q-2024-03-14-1281 ; https://quantum-journal.org/papers/q-2024-03-14-1281/ |
| `tan2025enola` | 三作者、题名、ASP-DAC 2025、921–929 页、DOI 匹配。booktitle 添加缩写不构成错误。 | 调度/放置/路由及高效调度概括受作者原文支持。 | https://api.crossref.org/works/10.1145/3658617.3697778 ; https://arxiv.org/abs/2405.15095 |
| `wang2024atomique` | 九作者顺序、题名、ISCA 2024、293–309 页、DOI 全部匹配。 | 多阵列映射与并行执行的概括受作者实验室摘要支持。 | https://api.crossref.org/works/10.1109/ISCA59077.2024.00030 ; https://hanlab.mit.edu/projects/atomique |
| `ludmir2024parallax` | 两作者、题名、SC24、1–17 页、DOI 全部匹配。 | SWAP-free/zero-SWAP 的描述由作者摘要直接支持。 | https://api.crossref.org/works/10.1109/SC41406.2024.00079 ; https://arxiv.org/abs/2409.04578 |
| `wang2024qpilot` | 七作者顺序、题名、DAC 2024、1–6 页、DOI 全部匹配。 | movable/flying ancillas 的描述由作者原文直接支持。 | https://api.crossref.org/works/10.1145/3649329.3658470 ; https://hanlab.mit.edu/projects/q-pilot ; https://arxiv.org/abs/2311.16190 |
| `kirmemis2025weaver` | 四作者顺序及重音符、题名、CGO 2025、299–316 页、DOI 全部匹配。 | retargetable compiler 的描述由作者原文直接支持。 | https://api.crossref.org/works/10.1145/3696443.3708965 ; https://arxiv.org/abs/2409.07870 ; https://portal.fis.tum.de/en/publications/weaver-a-retargetable-compiler-framework-for-fpqa-quantum-archite/ |
| `ruan2025powermove` | 建议按论文署名改 Travis Humble；其他字段匹配。 | 门层调度和布局过渡的概括由原文 Stage Scheduler / Continuous Router 支持。 | https://api.crossref.org/works/10.1145/3676642.3736128 ; https://arxiv.org/html/2411.12263v1 |
| `jang2025mantra` | 六作者順序、题名、CGO 2025、459–475 页、DOI 全部匹配。 | 重写程序减少跨区输运由会议官方摘要直接支持。 | https://api.crossref.org/works/10.1145/3696443.3708937 ; https://2025.cgo.org/details/cgo-2025-papers/22/Qubit-Movement-Optimized-Program-Generation-on-Zoned-Neutral-Atom-Processors |

## 结论及边界

这十条均是真实且对应正文所述方向的文献，未发现错 DOI、错年、错会议或明显不支持的引用。只建议修正两个作者署名细节。正文无需因本组文献核对而重写；当前引言使用的概括足够有界，未把这些方法误写成已做 GA-LK 的完整联合候选和跨层物理评估。本检查没有重新审计这些论文所有实验数字，也未把预印本数字转写进本稿。
