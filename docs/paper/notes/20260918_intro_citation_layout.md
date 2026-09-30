# 引言引用与前两章篇幅优化（2026-09-18）

作者要求保留历史引言中的必要文献，并控制空间与排版。本轮仅修改中英文第一、二章的四个源文件。摘要、关键词、贡献段、第三至五章、全部实验数据、图源、字号和页边距均保留。

## 历史核对与组织方式

Overleaf作者版`d8715da`及压缩前`d8bdcea`的引言引用24项；`23fd2ee`压缩后为5项，`1f890ec`恢复至10项（全文24项）。本轮让引言覆盖20项直接支撑硬件背景、编译脉络与研究定位的文献；Holland、Murty和Brélaz在第三章对应算法处引用，QMAP在第四章对应输入来源处引用。全文24项均由正文真实引用，无`nocite`补数量，书目数据库不变。

| 引言论述 | 对应文献 |
|---|---|
| 硬件进展、相干与操作误差 | Bluvstein 2024、Evered 2023、Bluvstein 2022、Preskill |
| 分区存储保护与并行门 | Stade 2024、ZAC |
| 原生门、布局及移动路由 | Geyser、GRAPHINE、Tan 2022、Hybrid |
| 调度与移动协调 | OLSQ-DPQA、Enola |
| 多阵列、免SWAP、移动辅助原子、可重定向编译 | Atomique、PARALLAX、Q-Pilot、Weaver |
| 分区调度、程序改写及驻留权衡 | PowerMove、Mantra、ZAP |
| 复用、后续伙伴距离和路由代价 | ZAC、Stade 2025 |

引言按研究问题概括各方向，随后引出完整联合方案和候选后配置；第二章删并已在引言出现的泛编译概述，集中保留ZAC、路由感知放置、ZAP与本文的直接区别，避免两章重复逐篇介绍。

## 原文核对

ZAP正式论文第7页式(12)–(15)同时比较再次使用前的受激损失与回迁的转移、退相干损失。引言改为“权衡驻留受激损失与回迁代价”，第二章保留这一比较的具体分量，避免省略已有方法的能力。

核验来源：[ZAP正式DOI](https://doi.org/10.1109/TQE.2026.3696707)、[OLSQ-DPQA](https://par.nsf.gov/servlets/purl/10512783)、[Enola](https://arxiv.org/abs/2405.15095)、[Atomique](https://par.nsf.gov/servlets/purl/10521789)、[PARALLAX](https://arxiv.org/html/2409.04578v1)、[Q-Pilot](https://arxiv.org/abs/2311.16190)、[Weaver](https://arxiv.org/abs/2409.07870)、[PowerMove](https://arxiv.org/abs/2411.12263)、[Mantra](https://yipenghuang.com/wp-content/uploads/2025/03/3696443.3708937.pdf)。其余支撑沿用已核验原文及[引用恢复记录](20260917_citation_restore.md)。

## 验收与交付

中英文引用集合逐章对齐，引言各20项、全文各24项，贡献段逐字保留。修复行列关系符号集合的行内断行。通过合并重复说明节省空间，不缩小字体或图形。

`make paper-en`及两语言各自框架图构建通过。英文保持6页正文加1页参考文献，引言在第一页右栏结束，背景与相关工作在第二页结束；中文6页自然分页。逐页检查无文字出栏或未定义引用。429项论文测试、14项图数据测试、39项同步测试和14项修订审计测试通过，末轮28项方法结构测试通过。

同步前再次核验Overleaf仍为`e209ee8423d50e694dbb59166cab7fdafb0f5eb0`。独立编译导出中英文及框架图，13页两版PDF与1页框架图同本地逐页文本和像素一致。已推送至`da338382909bcfcb0e8e380c0441604ce0b6aafc`，回读41个远端文件全部哈希一致，检出工作区干净。未运行浏览器端编译；未提交或推送根仓库。快照、差异、引用核对与构建日志位于`IEEE_conference_template/build/paper_en/intro-citations-20260918/`。
