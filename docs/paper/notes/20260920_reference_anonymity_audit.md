# 2026-09-20 全部参考文献核对与匿名修订

核对范围为正文实际引用的24条文献：23篇DOI文献逐条与出版社登记的Crossref元数据核验，1975年Holland原版由美国国会图书馆MARC记录核验。题名、作者顺序、年份、会刊、卷期页码和DOI均逐条检查；没有未找到的文献。ACM等个别出版社网页返回403时使用其注册元数据，并以作者稿、机构或会议网页补核正文引用语境。没有将预印本作者或年份混入正式条目。

第9条不是缺失作者：IEEEtran原来将与第8条完全相同的作者替换为长横线。本轮使用官方`IEEEtranBSTCTL`关闭该替代，保持所有文献作者显式可读；控制条目不编号，不增加真实文献数量。

| 编号 | 引用键 | 结果/修订 | 核对入口 |
|---|---|---|---|
| 1 | `bluvstein2024logicalprocessor` | 补期号7997，保留正式卷出版年2024 | [元数据](https://api.crossref.org/works/10.1038/s41586-023-06927-3) |
| 2 | `evered2023highfidelity` | 补期号7982 | [元数据](https://api.crossref.org/works/10.1038/s41586-023-06481-y) |
| 3 | `bluvstein2022coherenttransport` | 补期号7906 | [元数据](https://api.crossref.org/works/10.1038/s41586-022-04592-6) |
| 4 | `preskill2018quantum` | 核对一致，保留 | [元数据](https://api.crossref.org/works/10.22331/q-2018-08-06-79) |
| 5 | `stade2024abstractmodel` | 核对一致，保留 | [元数据](https://api.crossref.org/works/10.1109/QCE60285.2024.00098) |
| 6 | `lin2025zac` | 核对一致，保留 | [元数据](https://api.crossref.org/works/10.1109/HPCA61900.2025.00021) |
| 7 | `stade2025routingaware` | 核对一致，保留 | [元数据](https://api.crossref.org/works/10.1109/ICCAD66269.2025.11240721) |
| 8 | `patel2022geyser` | 核对一致，保留 | [元数据](https://api.crossref.org/works/10.1145/3470496.3527428) |
| 9 | `patel2023graphine` | 作者显式显示；保护Rydberg大小写；元数据正确 | [元数据](https://api.crossref.org/works/10.1145/3581784.3607032) |
| 10 | `tan2022mapping` | 第一作者署名按正式版改为Bochen Tan | [元数据](https://api.crossref.org/works/10.1145/3508352.3549331) |
| 11 | `schmid2024hybrid` | 核对一致，保留 | [元数据](https://api.crossref.org/works/10.1145/3649329.3655959) |
| 12 | `tan2024olsqdpqa` | 核对一致，保留 | [元数据](https://api.crossref.org/works/10.22331/q-2024-03-14-1281) |
| 13 | `tan2025enola` | 核对一致，保留 | [元数据](https://api.crossref.org/works/10.1145/3658617.3697778) |
| 14 | `wang2024atomique` | 核对一致，保留 | [元数据](https://api.crossref.org/works/10.1109/ISCA59077.2024.00030) |
| 15 | `ludmir2024parallax` | 核对一致，保留 | [元数据](https://api.crossref.org/works/10.1109/SC41406.2024.00079) |
| 16 | `wang2024qpilot` | 核对一致，保留 | [元数据](https://api.crossref.org/works/10.1145/3649329.3658470) |
| 17 | `kirmemis2025weaver` | 核对一致，保留 | [元数据](https://api.crossref.org/works/10.1145/3696443.3708965) |
| 18 | `ruan2025powermove` | 第五作者署名按正式版改为Travis Humble | [元数据](https://api.crossref.org/works/10.1145/3676642.3736128) |
| 19 | `jang2025mantra` | 核对一致，保留 | [元数据](https://api.crossref.org/works/10.1145/3696443.3708937) |
| 20 | `huang2026zap` | 仅使用正式文章号3103619，移除未核实的1–19页范围 | [元数据](https://api.crossref.org/works/10.1109/TQE.2026.3696707) |
| 21 | `murty1968assignment` | 补全正式题名前缀Letter to the Editor | [元数据](https://api.crossref.org/works/10.1287/opre.16.3.682) |
| 22 | `holland1975adaptation` | 核对一致，保留 | [元数据](https://lccn.loc.gov/74078988/marcxml) |
| 23 | `brelaz1979coloring` | 核对一致，保留 | [元数据](https://api.crossref.org/works/10.1145/359094.359101) |
| 24 | `wille2023qmap` | 核对一致，保留 | [元数据](https://api.crossref.org/works/10.1145/3569052.3578928) |

第8条Geyser的完整题名由Crossref主标题与副标题组成；第9条GRAPHINE的作者Tirthak Patel、Daniel Silver、Devesh Tiwari及其順序与正式登记一致。原始响应位于`IEEE_conference_template/build/reference-audit-20260920/{geyser,graphine}-crossref.json`。

详细语境和版本记录见[硬件与基线1–7](20260920_refs_hardware_audit.md)、[编译器10–19](20260920_refs_compilers_audit.md)、[算法与ZAP20–24](20260920_refs_algorithms_audit.md)。

匿名处理移除中英文引言中本人仓库的整个脚注；仓库本身保留。正文作者字段及PDF作者元数据为空，PDF URL注释列表无个人入口。摘要、关键词逐字不变，数据、图源、方法、算法、实验和结论不变。为厘清引用边界，将全程静止原子扫掠约束明确为本文执行模型的要求，不暗示baseline原文完整规定相同检查。

中英文独立框架图和正文构建通过。英文7页（6页正文、1页参考文献），中文自然分页6页。24条文献均由正文实际引用，所有引用均能解析；第9条作者显式显示，没有重复作者长横线。429项论文测试、14项图数据测试、39项导出测试和14项修订审计测试通过。逐页检查章节与图表位置、参考文献和匿名处理后的留白，没有文字溢出。

本轮构建、快照、逐页预览及保留内容核对保存在`IEEE_conference_template/build/reference-audit-20260920/`。

## Overleaf同步

同步前及提交前再次检查云端仍为`ffdf2372c5f6f2463fef39c7514945fac635b9e1`，无未合并在线修改。已推送至`5cd7151ce50ca1376b619babe5e6cf732a10f319`，回读41个导出文件SHA-256全部一致，检出工作区干净。导出版本另行独立编译中英文及框架图，两版13页与本地交付逐页文字及72 dpi像素完全一致。未运行浏览器端编译，未提交或推送根仓库。
