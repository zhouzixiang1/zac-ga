# 中文 IEEE 论文

本目录是后续唯一的论文编辑入口，随 ZAC 主仓库的 main 分支管理。主文件为 [paper_zh.tex](paper_zh.tex)，使用 IEEE conference A4 双栏版式和 XeLaTeX。

桌面的 IEEE_conference_template_upgrade 与原始 ZIP 保持原样，作为保留副本；不再向它们同步修改或编译。本目录不含嵌套 .git。现有 Overleaf 项目继续使用，其同步副本位于仓库根 build/overleaf-sync/，不依赖桌面目录。

## 编辑与审阅

| 内容 | 位置 |
|---|---|
| 标题、作者、摘要与章节入口 | [paper_zh.tex](paper_zh.tex) |
| 正文及四张编号表 | [sections/](sections/) |
| 六张 TikZ/PGFPlots 源图 | [figures/](figures/) |
| 冻结结果宏 | [results_values_zh.tex](results_values_zh.tex) |
| 引文与术语约定 | [references.bib](references.bib)、[术语表](writing/06_terminology_ledger.md) |
| 当前 PDF | [paper_zh.pdf](../build/paper_zh/paper_zh.pdf) |
| 当前验收记录 | [final_paper_qa.json](../build/paper_zh/final_paper_qa.json) |
| 全文总览及单页预览 | [page_overview.png](../build/paper_zh/page_overview.png)、[preview/](../build/paper_zh/preview/) |

build/ 是本地生成目录，不入 Git。上表中的 PDF 和预览链接在构建后可用；GitHub 保存可编辑源文件及冻结数据。

[Overleaf 项目](https://www.overleaf.com/project/6a866ce86ea64496e2ae01a5) 作为本目录的在线镜像。同步前检查远端人工修改，按仓库 [同步说明](../docs/REPOSITORY_MAP.md#overleaf-同步) 准备并提交；图3 PDF 的引用在导出时适配为项目内路径。在线主文档为 paper_zh.tex，编译器使用 XeLaTeX。下述 make 与审计命令用于完整 ZAC 本地仓库，不在 Overleaf 云端运行。

## 统一构建

从 ZAC 仓库根目录执行：

~~~bash
make paper
make paper-check
make paper-test PYTHON=/path/to/environment/bin/python
make paper-preview PYTHON=/path/to/environment/bin/python
~~~

- paper 强制重建图3和主论文，执行九页验收及数值一致性检查。
- paper-check 检查已有输出，不重新编译。
- paper-test 执行构建路径、数值、图形数据与同步保护测试，需要 pytest。
- paper-preview 构建后用 Poppler 和 Pillow 渲染单页及九页总览。
- 默认 Python 为 python3。XeLaTeX、latexmk、BibTeX、Poppler 需已安装；本机完整环境可用 ZAC/.venv/bin/python。

从论文目录调用完整验收也可：

~~~bash
python3 -B verify_paper_zh.py --compile --expected-pages 9
~~~

验收器自动发现同仓库的冻结结果。所有输出均在仓库根 build/paper_zh/：主文 PDF、辅助文件、日志、QA、图3独立 PDF 和预览。.latexmkrc 也将直接调用 latexmk 的输出定向到该目录；完整构建仍应使用 make paper，以保证先生成图3。

图3是唯一外部化的核心图，源为 figures/overall_framework.tex，独立入口为 figures/overall_framework_standalone.tex；生成物为 ../build/paper_zh/figures/overall_framework.pdf。其余五张图直接插入 TikZ 源。图构建失败会停止主文构建，不使用旧 PDF 回退。

单独进行只读数值审计：

~~~bash
python3 -B writing/audit_numerical_presentation.py \
  --paper-root . --project-root .. \
  --pdf-path ../build/paper_zh/paper_zh.pdf
~~~

## 证据与修改边界

当前权威结果为 [ZAC_zzx/results/paper_zh_v2/](../ZAC_zzx/results/paper_zh_v2/)，包括 final_manifest.json、paper_values.json、逐电路结果与仅含 ZAC18/QMAP154 的最终工作簿。

- 摘要、正文和图表数值由冻结的178个结果宏及8个图形派生文件提供；不手工回填，不因排版或润色重新运行实验。
- 主文比较 ZAC、ICCAD/QMAP 与 GA-LK；GA-NL 是内部结构配置，不等同于共享参数的零视界对照。
- 总体比较使用共同有效电路；逐电路表保留原有选例与筛选条件。固定12电路计时与总体质量比较采用不同统计范围。
- 本文为中文原创研究稿，保持既有模型、术语、方法归属及可复现条件；作者已确认无经费资助。
- 源文件允许作者并行编辑。修改前检查 Git 差异，不覆盖其他人的未提交修改。

当前验收版为9页：正文1–8页，参考文献第9页。表I位于第6页，表II–IV位于第7页，图6位于第8页。修改后必须重新构建并复核位置；旧验收记录不代表新稿状态。

## 迁移与历史材料

2026-09-06从桌面副本的当前工作树接入，包含未提交的可读性修改和新增表III、IV源文件，未用旧提交覆盖当前稿件。迁移仅修改构建路径、审计入口与文档，正文内容及冻结数据保持一致。

[writing/](writing/) 中带日期的审校记录描述当时版本，出现桌面路径或旧 PDF 位置时按历史记录理解；当前路径以本 README 和仓库根 [AGENTS.md](../AGENTS.md) 为准。IEEEtran_HOWTO.pdf、template-A4.pdf、fig1.png 是原模板材料，不属于本轮生成的编译产物。
