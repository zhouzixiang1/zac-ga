# 2026-09-20 参考文献 20–24 核对（ZAP 与算法来源）

范围：`huang2026zap`、`murty1968assignment`、`holland1975adaptation`、`brelaz1979coloring`、`wille2023qmap`。本文件只记录审核，不修改 BibTeX 或正文。以当前 `references.bib`、中英文正文、出版社注册到 Crossref 的元数据、出版社原文和作者官方页面为依据。

## 结果

| 条目 | 已核对字段 | 结论及推荐 |
|---|---|---|
| `huang2026zap` | 七名作者与顺序、完整题名、IEEE Transactions on Quantum Engineering、卷 7、2026、文章号 3103619、DOI | 核心元数据正确；当前可验证的官方页码信息是文章号 3103619（Crossref page 为 `3103619-3103619`），未找到出版社将 1–19 列为正式页范围的记录；19 可确认为 PDF 页数。建议采用仅文章号写法，删除 pages，保留 `note={Art. no. 3103619}`。这属于消除未被当前官方记录支持的冗余页范围，不是文献身份错误。魏峰姓名在 IEEE PDF 为 WEI-FENG，现有 Wei-Feng 写法可保留。不要改用 2024 arXiv 初版的 Parallelizable 题名。 |
| `murty1968assignment` | Katta G. Murty；Operations Research 16(3), 682–687；1968；DOI | 年卷期页码作者和 DOI 全部正确；出版社正式题名带 `Letter to the Editor—` 前缀，推荐恢复该前缀，其余题名无问题。 |
| `holland1975adaptation` | John H. Holland；1975；University of Michigan Press；Ann Arbor；原版 ISBN 0472084607；全题名 | 现有作者、年和出版社正确。建议按完整书名补副标题 `An Introductory Analysis with Applications to Biology, Control, and Artificial Intelligence`；出版地可补 `Ann Arbor, MI, USA`。没有证实 1975 原版 DOI，继续不填。`10.7551/mitpress/1090.001.0001` 明确属于 1992 MIT 再版，不可混填。 |
| `brelaz1979coloring` | Daniel Brélaz；Communications of the ACM 22(4), 251–256；1979；DOI；题名 | 全部正确，保留。Brélaz 的重音编码正确。 |
| `wille2023qmap` | Robert Wille、Lukas Burgholzer，顺序正确；完整主副题；ISPD 2023 proceedings；198–204；ACM；DOI | 全部正确，保留。Crossref 将 `MQT QMAP` 和 `Efficient Quantum Circuit Mapping` 分别注册在 title、subtitle；合写冒号题名正确，作者官网 PDF 和官方仓库推荐引用也如此。 |

## 对应引文是否支持正文

- **ZAP**：当前引言称其比较下次使用前的受激损失与回迁转移及退相干损失，再按距离和冲突数逐门选位。正式 IEEE PDF 第 7–8 个物理页，公式 (12)–(17) 与 Algorithm 2 直接支持；无需更改正文。这里是 2026 正式版本的内容，不能用 2024 初稿替代核对。
- **Murty**：出版社摘要明确按成本递增列出匹配并求第 k 个最优匹配，支持第三章“前 K 个最小权匹配”；不是仅有一般匹配背景的泛引。
- **Holland**：用在遗传搜索首次引入处，定位为 GA 的基础来源，合理；具体编码、种子、交叉、物理评价仍是本论文自己的实现，正文未将其归给 Holland。原版书目由美国国会图书馆 MARC 原始记录核实。
- **Brélaz**：DSATUR 着色的标准来源，当前引用位置合适；论文没有用此文献为额外的 AOD 物理检查背书。ACM 页面本次受 403 限制，作者和刊物元数据由其 Crossref 注册记录核实；官方 igraph 文档也直接将 DSATUR 归于此文。
- **MQT QMAP**：支持工具/示例集的出处，但 2023 工具论文不会证实本次选取 154 文件、去重后 151 电路这两个数量；它们应由本文已有实验清单和输入哈希支持。正文没有声称这些数量出自该论文，当前引用作为工具来源可保留，不必增加新的仓库脚注。

## 核对来源

### ZAP

- IEEE 正式页面（搜索索引可读，直接打开有时出现机器人验证）：https://ieeexplore.ieee.org/document/11535023/
- Crossref： https://api.crossref.org/works/10.1109/TQE.2026.3696707
- DOI： https://doi.org/10.1109/TQE.2026.3696707
- 本地出版社最终 PDF：`documents/ZAP_Zoned_Architecture_and_Performant_Compiler_for_Field-Programmable_Atom_Array.pdf`，19 个物理页；首页 DOI 与标题匹配，载明 online 25 May 2026、current version 26 June 2026。Crossref page 字段是 `3103619-3103619`，IEEE 页面为 Article Sequence Number 3103619。

### Murty

- 出版社全文信息和官方推荐引文：https://pubsonline.informs.org/doi/10.1287/opre.16.3.682
- Crossref：https://api.crossref.org/works/10.1287/opre.16.3.682

### Holland

- 美国国会图书馆原始 MARC XML（本次通过 urllib 可直接读取）：https://lccn.loc.gov/74078988/marcxml
  - 100：Holland, John H. (John Henry)
  - 245：完整主副书名
  - 260：Ann Arbor；University of Michigan Press；[1975]
  - 300：viii, 183 p.
- 1992 再版出版社 DOI 页面，明确显示 Publication date: 1992：https://doi.org/10.7551/mitpress/1090.001.0001
- 原版扫描书目的辅助交叉检查：https://books.google.com/books/about/Adaptation_in_Natural_and_Artificial_Sys.html?id=Qk5RAAAAMAAJ

### Brélaz

- ACM DOI：https://doi.org/10.1145/359094.359101
- ACM 注册的 Crossref 元数据：https://api.crossref.org/works/10.1145/359094.359101
- 官方 igraph 文档中的 DSATUR 源文献归属：https://igraph.org/c/pdf/master/igraph-docs.pdf

### MQT QMAP

- 作者机构正式 PDF：https://www.cda.cit.tum.de/files/eda/2023_ispd_mqt_qmap_efficient_quantum_circuit_mapping.pdf
- ACM DOI：https://doi.org/10.1145/3569052.3578928
- Crossref（同时核对 title 和 subtitle）：https://api.crossref.org/works/10.1145/3569052.3578928
- 作者维护仓库 Cite This：https://github.com/munich-quantum-toolkit/qmap
- 作者预印本：https://arxiv.org/abs/2301.11935

## 建议改动的最小集合

1. ZAP 可规范为只保留卷、文章号、年、DOI；现有官方记录没有核实 `pages={1--19}`，推荐移除它而不将本条判作不存在或 DOI 错误。
2. Murty 补全出版社题名的 `Letter to the Editor---` 前缀。
3. Holland 可补全副标题及出版地，保持 1975 Michigan 原版，不新增 1992 DOI。
4. 其余两条及上述正文引文不需修改。所有 DOI 的字母大小写差异均无意义，不作为问题。
