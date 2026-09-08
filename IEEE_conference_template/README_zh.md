# 中文 IEEE 论文

本目录是后续唯一的论文编辑入口，随 ZAC 主仓库的 main 分支管理。主文件为 [paper_zh.tex](paper_zh.tex)，使用 IEEE conference A4 双栏版式和 XeLaTeX。

对应英文稿为 [paper_en.tex](paper_en.tex)，按同名章节保存在 [sections_en/](sections_en/)；构建与逐项对照方法见 [英文稿说明](README_en.md)。中文稿作为内容对照依据；英文稿目前暂停同步，不能视为最新中文稿的对应版本。

2026-09-08：完整初始化前瞻质量实验已经结束。经核验的派生数据已更新中文标红稿的摘要、总体表 II、逐电路表 III 和图 7(a)；原接受结果包保持不变，受控比较仍使用各自的独立证据。

桌面的 IEEE_conference_template_upgrade 与原始 ZIP 保持原样，作为保留副本；不再向它们同步修改或编译。本目录不含嵌套 .git。现有 Overleaf 项目继续使用，其同步副本位于本论文目录 build/overleaf-sync/，不依赖桌面目录。

## 编辑与审阅

2026-09-08 起，本轮新增或改写的正文、图注与图内文字使用红色修订标记；不对作者此前的改动整体着色。请先阅读 [本轮审核说明](writing/20260908_review_notes.md)，再检查下表中的 PDF。修订显示由 [revision_marks.tex](revision_marks.tex) 统一控制，审核后再关闭。

| 内容 | 位置 |
|---|---|
| 标题、作者、摘要与章节入口 | [paper_zh.tex](paper_zh.tex) |
| 正文及四张编号表 | [sections/](sections/) |
| 七张 TikZ/PGFPlots 源图（含 ZAIR 输出） | [figures/](figures/) |
| 冻结结果宏 | [results_values_zh.tex](results_values_zh.tex) |
| 当前总体与逐电路结果 | [default_initial_v1/paper_exports/](../ZAC_zzx/results/default_initial_v1/paper_exports/) 中的六个派生文件 |
| 初始化扩展实验独立宏 | [initial_lookahead_extension_values.tex](initial_lookahead_extension_values.tex) |
| 方法算例与机制分解派生宏 | [method_argument_values.tex](method_argument_values.tex)、[来源记录](paper_argument_values.json) |
| 实验协议与贡献对应 | [实验协议](writing/experiment_protocol.md)、[方法论证修订](writing/2026-09-07_method_argument_alignment.md) |
| 引文与术语约定 | [references.bib](references.bib)、[术语表](writing/06_terminology_ledger.md) |
| 当前审核范围 | [2026-09-08审核说明](writing/20260908_review_notes.md)；较早带日期清单仅记录当时状态 |
| 当前 PDF | [paper_zh.pdf](build/paper_zh/paper_zh.pdf) |
| 当前验收记录 | [final_paper_qa.json](build/paper_zh/final_paper_qa.json) |
| 全文总览及单页预览 | [page_overview.png](build/paper_zh/page_overview.png)、[preview/](build/paper_zh/preview/) |

build/ 是本地生成目录，不入 Git。上表中的 PDF 和预览链接在构建后可用；GitHub 保存可编辑源文件及冻结数据。

[Overleaf 项目](https://www.overleaf.com/project/6a866ce86ea64496e2ae01a5) 是独立维护的在线镜像，不随 GitHub 推送自动更新。下次同步须先适配新增六文件结果包，再检查远端人工修改，按仓库 [同步说明](../docs/REPOSITORY_MAP.md#overleaf-同步) 导出、验收并提交。在线主文档为 paper_zh.tex，编译器使用 XeLaTeX。下述 make 与审计命令用于完整 ZAC 本地仓库，不在 Overleaf 云端运行。

## 统一构建

从 ZAC 仓库根目录执行：

~~~bash
make paper
make paper-check
make paper-test PYTHON=/path/to/environment/bin/python
make paper-preview PYTHON=/path/to/environment/bin/python
~~~

- paper 先只读核对原接受证据、当前总体结果包、模型算例、QFT分解及初始化补充实验派生值，再强制重建图3和主论文，执行九页验收及数值一致性检查。
- paper-check 检查已有输出，不重新编译。
- paper-test 执行构建路径、数值、图形数据与同步保护测试，需要 pytest。
- paper-preview 构建后用 Poppler 和 Pillow 渲染单页及九页总览。
- 默认 Python 为 python3。XeLaTeX、latexmk、BibTeX、Poppler 需已安装；本机完整环境可用 ZAC/.venv/bin/python。

从论文目录调用完整验收也可：

~~~bash
python3 -B verify_paper_zh.py --compile --expected-pages 9
~~~

验收器自动发现同仓库的冻结结果。所有输出均在本论文目录 build/paper_zh/：主文 PDF、辅助文件、日志、QA、图3独立 PDF 和预览。.latexmkrc 也将直接调用 latexmk 的输出定向到该目录；完整构建仍应使用 make paper，以保证先生成图3。

图3是唯一外部化的核心图，源为 figures/overall_framework.tex，独立入口为 figures/overall_framework_standalone.tex；生成物为 build/paper_zh/figures/overall_framework.pdf。其余六张图直接插入 TikZ 源。新增 ZAIR 图编号为 Fig. 6，结果图顺延为 Fig. 7；冻结图形数据文件的 fig6 前缀保留。图构建失败会停止主文构建，不使用旧 PDF 回退。

图4对应门位与驻留的联合编码，图5用固定模型的两因子算例解释多层前瞻，图7分别展示总体收益、搜索与前瞻对照、QFT损失分解。图5不是完整候选评分，图7的分解不是新增实验。`writing/generate_argument_values.py` 从固定模型与既有逐电路文件生成独立显示宏，其 `--check` 不写入文件。为让 IEEE 双栏大图出现在实验正文同页，`sections/05_overview_floats.tex` 在方法末尾提前声明总体表与图7，不改变章节阅读顺序。

单独进行只读数值审计：

~~~bash
python3 -B writing/audit_numerical_presentation.py \
  --paper-root . --project-root .. \
  --pdf-path build/paper_zh/paper_zh.pdf
~~~

## 证据与修改边界

原接受证据为 [ZAC_zzx/results/paper_zh_v2/](../ZAC_zzx/results/paper_zh_v2/)，包括 final_manifest.json、paper_values.json、逐电路结果与仅含 ZAC18/QMAP154 的最终工作簿；该包及其旧数值宏均保持不变。

当前中文稿的总体质量数据来自 [default_initial_v1/paper_exports/](../ZAC_zzx/results/default_initial_v1/paper_exports/)：`main_rows.csv` 保存逐输入汇总，`analysis_units.csv` 保存共同有效的规范化比较单元，`mechanism.csv` 保存机制指标，JSON 记录统计口径与来源，两个 TeX 文件提供显示宏和逐电路表行。摘要、表 II、表 III、图 7(a) 使用这套数据。共同有效集合为 ZAC 的16个电路和 QMAP 的120个规范化单元；两项基线与 GA-LK 在同一集合上重新汇总，不能直接用新旧集合的均值之差解释初始化收益。

初始化扩展使用独立的 [initial_lookahead_v1/](../ZAC_zzx/results/initial_lookahead_v1/) 证据，不替换主结果。预定39电路中，7项链接先导实验，32项按三个种子新增运行；原始 SA、首层评价和两层前瞻组成每个种子的三配置配对。最终36电路具有完整三种子结果，5次配对任务超时，完整结果及未完成记录均保留，见 [逐电路汇总](../ZAC_zzx/results/initial_lookahead_v1/dynamic39-missing32-h0-h2-sa-k4-rho07-b32-seed012-p3-v1/linked39_analysis.md)。

当前中文补充值由 `writing/generate_initial_lookahead_extension_values.py` 核对固定协议、来源及结果后生成。旧九电路先导及 `initial_lookahead_values.tex` 保留为独立阶段记录；两套生成器的 `--check` 均只读核对，不重跑实验。新增32电路采用三路单线程并行；其计时不与旧串行先导合并为速度结论。

- 原接受宏与图形数据保留；当前总体结果通过独立的 `Default` 前缀宏接入，初始化扩展单独生成数值宏。不手工回填，不因排版或润色重新运行实验。
- 主文比较 ZAC、ICCAD/QMAP 与 GA-LK；GA-NL 是内部结构配置，不等同于共享参数的零视界对照。
- 总体比较使用共同有效电路；逐电路表按预定选例规则生成。初始化消融、层数比较、遗传搜索对照与固定12电路串行计时各自保持独立口径，不将并行质量运行当作串行计时。
- 本文为中文原创研究稿，保持既有模型、术语、方法归属及可复现条件；作者已确认无经费资助。
- 源文件允许作者并行编辑。修改前检查 Git 差异，不覆盖其他人的未提交修改。

目标版式为9页：正文1–8页，参考文献第9页。方法按编译时间顺序组织，讨论与结论合为一章。修改后必须重新构建并复核图表位置；旧验收记录不代表新稿状态。本轮修改与人工核对入口见 [章节重构记录](writing/2026-09-06_chronological_revision.md)。

## 迁移与历史材料

2026-09-06从桌面副本的当前工作树接入，包含未提交的可读性修改和新增表III、IV源文件，未用旧提交覆盖当前稿件。迁移仅修改构建路径、审计入口与文档，正文内容及冻结数据保持一致。

[writing/](writing/) 中带日期的审校记录描述当时版本，出现桌面路径或旧 PDF 位置时按历史记录理解；当前路径以本 README 和仓库根 [AGENTS.md](../AGENTS.md) 为准。IEEEtran_HOWTO.pdf、template-A4.pdf、fig1.png 是原模板材料，不属于本轮生成的编译产物。
