# GA-LK 论文 / Manuscript

- 中文：[paper_zh.tex](paper_zh.tex)，章节在 [sections/](sections/)。
- English: [paper_en.tex](paper_en.tex), with matching files in [sections_en/](sections_en/).
- [figures/](figures/)：四张正文图的 TikZ/PGFPlots 源码、共享样式及必要数据。
- `references.bib`、`IEEEtran.cls`、数值宏和 `revision_marks.tex` 是编译依赖。

在仓库根目录执行 / Run from the repository root:

```bash
make paper PYTHON=ZAC/.venv/bin/python
make paper-en PYTHON=ZAC/.venv/bin/python
make paper-test PYTHON=ZAC/.venv/bin/python
```

PDF：[中文](build/paper_zh/paper_zh.pdf) · [English](build/paper_en/paper_en.pdf)。
框架图（图2）在两语言各自正文之前独立构建。默认使用清洁文字，编译器为 XeLaTeX。
图2保留六阶段主链，以重基电路、布局候选和当前/预测配置说明联合优化；图4在单栏内左右并排展示保真度和批次趋势。
英文目标为六页正文加一页参考文献；中文采用相同内容结构，自然分页。
当前本地稿采用引言、方法、实验评估、结论四章。引言保留原引言与背景两章的信息量，包含架构、AOD约束、保真度模型、驻留算例与相关工作；算法1恢复在方法总述后引入。英文方法从第2页右栏开始，图2与算法在第3页，图3在第4页。
最新调整见[引言衔接与图4留白](../docs/paper/notes/20260919_intro_story_layout.md)：英文引言结束在第2页左栏，图4收紧标签和图例间距。此前[引言与背景完整合并](../docs/paper/notes/20260918_intro_bg_restored.md)及[英文排版记录](../docs/paper/notes/20260918_english_layout.md)描述对应历史版本。

脚本与测试位于 [scripts/paper/](../scripts/paper/)；来源记录、改动说明和退役图表位于 [docs/paper/](../docs/paper/README_zh.md)。
论文源目录不放 Python、JSON、测试或历史模板；PDF、日志和报告统一进入 `build/`。
现有实验环境和冻结构建保留原位。编译论文不会重跑实验。

2026-09-20已完成24条文献核对并移除个人仓库脚注；当前为匿名稿。已同步至Overleaf，远端提交 `5cd7151` 的41个导出文件全部回读核验一致，见[文献与匿名修订记录](../docs/paper/notes/20260920_reference_anonymity_audit.md)。导出源码已独立本地编译，未运行浏览器端编译。此前[本地论证与图文调整](../docs/paper/notes/20260918_local_story_merge.md)、[引言引用与版面优化](../docs/paper/notes/20260918_intro_citation_layout.md)、[论证修订](../docs/paper/notes/20260917_story_fixes.md)及[引用恢复记录](../docs/paper/notes/20260917_citation_restore.md)仅描述对应历史版本。

正文为四图、一表和一个两阶段、14行算法块，`algorithm_style.tex`统一两语言格式。历史修订及退役图表均保留在 `docs/paper/`。行文关系见[论证脉络](../docs/paper/notes/20260914_narrative_map.md)，详细比较条件与来源见[实验协议](../docs/paper/notes/experiment_protocol.md)。
