# 2026-09-17 引用恢复

已同步Overleaf至 `1f890ec3cae17184ade749cb1d847c9539548f98`；推送后回读41个文件，逐项哈希一致，同步工作区干净。

压缩提交 `23fd2ee` 删除部分论述时一并删除了引用命令，使正文实际引用由24篇降至13篇；`references.bib`中的24项始终保留。此次对照作者版 `d8715da`、压缩前 `d8bdcea` 和本轮前 `0cbb346` 恢复11项引用，没有使用 `\nocite` 补数量。

| 位置 | 恢复文献及对应论述 |
|---|---|
| 引言 | Preskill：噪声和相干限制；Geyser、GRAPHINE、Tan 2022、Hybrid：原生门、交互布局及移动路由研究脉络 |
| 第二章 | PARALLAX：移动替代SWAP；Q-Pilot：移动辅助原子；Weaver：可重定向编译；PowerMove：调度与布局转换；Mantra：减少操作交替和跨区移动 |
| 第三章 | Holland：遗传搜索的通用方法来源，不用于归因本工作的物理目标、OX或预算 |

中英文引用集合逐章一致，最终各24篇。摘要、关键词、实验数值、公式、图源及书目数据库保持原样；正文引用编号随首次出现顺序更新。引言仍为四段，背景与相关工作在英文第二页结束。删除英文过时的强制换栏，并压缩结论首尾的重复措辞，保留全部研究判断及“相近质量”边界。中文保留其原有换栏方式。

核实了恢复文献的正式出版信息及作者原文。Hybrid正式DOI的Crossref作者表与作者公开稿存在差异，本轮保持原有正式DOI元数据，未擅改作者。主要核验来源：[Preskill](https://quantum-journal.org/papers/q-2018-08-06-79/)、[GRAPHINE](https://sc23.supercomputing.org/proceedings/tech_paper/tech_paper_pages/pap117.html)、[Hybrid](https://arxiv.org/abs/2311.14164)、[PARALLAX](https://arxiv.org/abs/2409.04578)、[Q-Pilot](https://www.hanruiwang.com/projects/q-pilot)、[Weaver](https://arxiv.org/abs/2409.07870)、[PowerMove](https://arxiv.org/abs/2411.12263)、[Mantra正式全文](https://yipenghuang.com/wp-content/uploads/2025/03/3696443.3708937.pdf)。Holland保留1975年初版信息。

`make paper-en`通过，包含两语言框架图和正文分别构建；英文六页正文加一页参考文献，中文六页。429项论文测试、39项同步测试通过。页面检查涵盖引言衔接、相关工作结束位置、最后正文页及完整书目，无未定义引文或overfull box。

本轮快照、最终差异、保护内容核对和构建日志保存在 `IEEE_conference_template/build/paper_en/citations-20260917/`。Overleaf导出与同步回读结果见该目录的 `sync-receipt.json`；同步在独立导出编译和远端复核后完成，未运行浏览器端编译。主仓库作者修改保留，不提交主仓库。
