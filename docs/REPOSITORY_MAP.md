# 仓库导航与维护约定

本仓库统一保存 GA-LK 代码、论文源文件和当前论文结果。日常修改在 `main`
分支进行；GitHub 远端为 [zhouzixiang1/zac-ga](https://github.com/zhouzixiang1/zac-ga)，
访问权限由仓库所有者管理。

## 先找哪个文件

| 任务 | 入口 |
|---|---|
| 修改论文 | [`../IEEE_conference_template/paper_zh.tex`](../IEEE_conference_template/paper_zh.tex) 及其 `sections/` |
| 对照审阅英文稿 | [`../IEEE_conference_template/paper_en.tex`](../IEEE_conference_template/paper_en.tex) 及同名 `sections_en/` 文件 |
| 修改论文图 | `../IEEE_conference_template/figures/` 中的 TikZ 源文件 |
| 阅读最新论文修订 | [参考文献核对与匿名修订](paper/notes/20260920_reference_anonymity_audit.md)；该次 Overleaf 同步已核验为 `5cd7151`，GitHub 同步另行记录 |
| 查看已完成 GA 主实验 | [`physical_ga_main_v1/protocol.json`](../ZAC_zzx/results/physical_ga_main_v1/protocol.json)；507项含27复用、480新增，活动稿接入 [`GAMain 导出包`](../ZAC_zzx/results/physical_ga_main_v1/paper_exports/) |
| 查看历史SA四候选总体结果 | [`default_initial_v1/paper_exports/`](../ZAC_zzx/results/default_initial_v1/paper_exports/) |
| 查看原接受结果包 | [`paper_zh_v2/final_manifest.json`](../ZAC_zzx/results/paper_zh_v2/final_manifest.json) |
| 逐电路人工审计 | 当前 GA 主实验 [`analysis_units.csv`](../ZAC_zzx/results/physical_ga_main_v1/paper_exports/analysis_units.csv)；历史已验收稿 [`analysis_units.csv`](../ZAC_zzx/results/default_initial_v1/paper_exports/analysis_units.csv)；原接受包 [`four_methods_results.xlsx`](../ZAC_zzx/results/paper_zh_v2/four_methods_results.xlsx) |
| 理解 Python 编译集成 | [`../ZAC_zzx/zzx/`](../ZAC_zzx/zzx/) |
| 理解联合搜索和物理评价 | [`../ZAC_zzx/native/`](../ZAC_zzx/native/) |
| 了解已走过的路线、效果及下一步 | [实验演进与后续开发起点](EXPERIMENT_HISTORY.md)；[2026-10-01 清理验收](repository-maintenance-20261001.md) |
| 查看正式论文实验入口 | [`../ZAC_zzx/experiments_v2/paper_cli.py`](../ZAC_zzx/experiments_v2/paper_cli.py) |
| 查看初始化前瞻 | [`../ZAC_zzx/zzx/initial_lookahead.py`](../ZAC_zzx/zzx/initial_lookahead.py)；标准 ABI9 GA-LK 编译路径的一部分 |
| 查看初始化独立补实验 | [`../ZAC_zzx/results/initial_lookahead_v1/`](../ZAC_zzx/results/initial_lookahead_v1/)；与已接受主结果分开 |
| 查看物理 GA 初始化先导 | [`physical_initial_ga.py`](../ZAC_zzx/zzx/physical_initial_ga.py)；[`先导结果说明`](../ZAC_zzx/results/physical_ga_initial_v1/corrected_driver_v1/README.md)，原矩阵108项、101成功、7超时；活动稿三新组视图81项、75成功、6超时，共同8电路 |
| 查看层数补实验 | [`horizon_extension_v2/`](../ZAC_zzx/results/horizon_extension_v2/)；质量阶段完成，串行计时阶段未启用 |
| 查看原版 ZAC | [`../ZAC/run.py`](../ZAC/run.py) 和 [`../ZAC/zac/`](../ZAC/zac/) |

论文目录的简明入口为 [README](../IEEE_conference_template/README.md)。Python工具集中在 `scripts/paper/`，JSON、写作记录及退役图表在 `docs/paper/`；[文件迁移清单](paper/relocation-20260913.json)保留旧路径和内容哈希。

论文数值由现有结果和生成脚本维护，不在正文中手工另填一份。
重新编译论文不会重新运行量子电路实验。

## 目录分工

```text
zac/
├── IEEE_conference_template/    论文源文件、四图及编译必需数据
│   └── build/                  全部新构建、预览与同步输出，不进入 Git
├── ZAC_zzx/                    当前方法、实验管线和回归测试
│   ├── zzx/                    Python 编译集成
│   ├── native/                 C++17 搜索内核
│   ├── experiments_v2/         运行、汇总、验证、来源记录
│   ├── exp_setting/            编译与实验配置
│   ├── results/paper_zh_v2/    原接受结果包，保持不变
│   ├── results/default_initial_v1/  历史SA四候选完整实验及已验收展示数据
│   ├── results/initial_lookahead_v1/  初始化前瞻独立补实验
│   ├── results/physical_ga_initial_v1/  物理GA初始化先导及保留的驱动错误记录
│   ├── results/physical_ga_main_v1/  已完成GA完整主实验及GAMain展示数据，不覆盖旧数据
│   ├── results/horizon_extension_v2/  前瞻层数补实验
│   └── third_party/            冻结 QMAP 补丁与验证材料
├── ZAC/                        原版 ZAC 基线与本地运行环境
├── experiments/                基线复现辅助脚本和小型证据
├── documents/                  baseline 论文
├── scripts/paper/              论文生成器、校验器和测试
├── docs/paper/                 写作记录、来源JSON、退役图表与模板参考
├── docs/                       仓库维护说明
├── archive/                    本地历史实验与实现；全部排除 Git
├── qmap-main/                  本地 QMAP 源码，不进入主仓库 Git
└── fidelity-lookahead-v2/       当前论文所需原始证据及冻结依赖，不进入 Git
```

`ZAC_zzx/run.py` 是配置驱动的常规编译入口；原接受包及历史控制实验通过
`experiments_v2.paper_cli` 管理。新完整 GA 初始化研究的专用驱动是
[`physical_ga_main_study.py`](../ZAC_zzx/experiments_v2/physical_ga_main_study.py)，冻结协议与输出在 `results/physical_ga_main_v1/`。`experiments_v2.cli` 还保留通用 Schema-v2
实验操作。早期 `fourway_*`、`compare*` 脚本已纳入本地
`archive/zac-zzx-legacy/scripts/`，不替代当前论文结果生成管线。
运行实验需要明确指定计划、配置、环境和输出目录，不能仅凭旧脚本文件名判断其
适用版本。

## 论文只维护这一份

后续编辑目录固定为仓库内的 `IEEE_conference_template/`。
桌面的 `IEEE_conference_template_upgrade/` 保持原样，作为迁回仓库前的副本；
其独立 Git/Overleaf 信息保留，但它不再是日常论文工作目录。
不要在两份副本间自动双向同步，以免覆盖人工修改。

当前双语稿包含 I 引言、II 方法、III 实验评估、IV 结论。引言整合原引言与背景，包含架构、AOD约束、保真度模型、驻留算例及相关工作。两项贡献由四张图、一张总体结果表和一个引领算法块支撑；两语言的 `03_method.tex` 引用同目录下的 `03_algorithm.tex`，标签为 `alg:solver`。框架为图2，完整候选为图3，前瞻趋势为图4；历史记录中的旧编号不代表当前稿。

活动稿采用 `physical_ga_main_v1/paper_exports/` 的 `GAMain` 数据。2026-09-20 修订已同步到 Overleaf，正文不含个人仓库入口，24条实际引用均已核对。当前修订颜色关闭。英文为六页正文加一页参考文献，中文自然分页六页。退役算法、图表、写作记录及模板参考保存在 `docs/paper/`，不放回论文源文件目录。

在仓库根目录运行：

```bash
make paper
make paper-check
make paper-test
make paper-preview
```

`paper` 重建独立图和正文并校验，`paper-check` 检查已有构建，
`paper-test` 运行数值和图数据回归，`paper-preview` 生成视觉审阅材料。
需要切换 Python 时，在命令后加 `PYTHON=/path/to/env/python`。

中英文同步维护。双语编辑使用
`make paper-en` 与 `make paper-en-preview`，输出位于
`IEEE_conference_template/build/paper_en/`。该构建先验收中文稿，再独立重建英文框架图并校验中英文引用、公式和
结果宏的对应关系。英文分页独立验收，不通过删减翻译内容压到中文稿页数。

统一输出位置（迁移与旧记录恢复说明见 [构建目录约定](BUILD_OUTPUTS.md)）：

| 产物 | 仓库内路径 |
|---|---|
| 论文 PDF | `IEEE_conference_template/build/paper_zh/paper_zh.pdf` |
| 独立总体框架图 | `IEEE_conference_template/build/paper_zh/figures/overall_framework.pdf` |
| LaTeX 中间文件、编译日志 | `IEEE_conference_template/build/paper_zh/` 下 |
| 校验报告、逐页渲染与总览图 | `IEEE_conference_template/build/paper_zh/` 下 |
| 新原生构建和 wheel | `IEEE_conference_template/build/native/` 下 |

源文件目录不存放新编译的 `.aux`、`.log`、`.bbl`、`.fls` 等中间产物。
构建目录中的 PDF 和校验报告需要重新生成后再用于交付或审稿。

## 代码构建与实验记录分开

在仓库根目录使用：

```bash
make native-build PYTHON=/path/to/env/python
make native-test PYTHON=/path/to/env/python
make native-wheel PYTHON=/path/to/env/python
```

这些目标只负责构建与测试，不自动安装 wheel，不替换现有环境中的原生模块，
也不为新构建签发正式实验冻结证明。正式构建登记和冻结仍遵循
[`native/README.md`](../ZAC_zzx/native/README.md) 与
`experiments_v2.native_build_freeze` 的契约。

Python 环境需具备相应依赖：论文测试使用 `pytest`，预览使用 `Pillow`；
原生构建使用 `pybind11`，wheel 打包另需 `build` 与 `scikit-build-core`。
wheel 目标使用系统 Make，不自动安装依赖。本机上述目标已用
`ZAC/.venv/bin/python` 验证。

以下目录保留其已有结构，不按普通缓存清理：

- `fidelity-lookahead-v2/artifacts/` 中当前论文依赖的 wheel、构建证明、原始运行记录与轨迹；
- `ZAC/.venv/`、根目录下 QMAP 虚拟环境及已安装的 `.so`；
- `qmap-main/` 和归档中的独立仓库、第三方源码；
- `ZAC_zzx/third_party/qmap32_streaming/` 中的冻结补丁与清单。

新编译统一放到 `IEEE_conference_template/build/`。本次原根构建目录的内容已保全迁移，
旧冻结声明不改写。2026-10-01 已另按授权清单删除部分旧实验产物；当前所需
证据与运行环境保持原位，实验数据仍不按普通缓存清理。

## 证据与 Git 边界

原接受证据以 `ZAC_zzx/results/paper_zh_v2/final_manifest.json` 为索引，保留完整
逐电路结果、汇总、对照和计时信息，不重写该包。

2026-09-08至09-12 已验收中文稿采用 `default_initial_v1/paper_exports/` 的六个派生文件，对应 SA 四候选初始化。该版本的26.24%保真度提升、25.85%移动批次减少及QMAP154共同120电路均保持历史身份；其共同集合、部分电路、图7与摘要主指标不能仅改方法名作为物理 GA 结果。

2026-09-13 完整主实验 `physical_ga_main_v1/` 已完成：169个规范化电路、三种子共507逻辑任务，完整复用先导 GA-H2 的27项（含3次超时），新增480项。507项终态为480次成功、18次超时、6次空门序列初始化报告字段缺失的 `program_error`、3次 `memory_limit`；成功中64次为保真度模型域外（F-OOD），不归为程序错误。两基线复用原接受记录，统一结果见 [`ga_main_values.json`](../ZAC_zzx/results/physical_ga_main_v1/paper_exports/ga_main_values.json)，旧包不覆盖。新增阶段最多14路并行，墙钟时间仅描述，不用于串行速度比较。

活动稿的三方法共同有效规范化电路为ZAC18的16个、QMAP154的119个；QMAP154相对ZAC的几何平均保真度提升26.42%、平均移动批次减少25.86%。这些值来自新共同集合，不与旧120电路集合混算。表I最后一列统计完成编译（含F-OOD），F/B/T三项均仅在三方法共同有效集合上汇总；完整失败及OOD记录分别保存在新导出包的 `failures.csv`、`completed_ood.csv`。

物理 GA 先导的 `corrected_driver_v1/` 保存完整108项矩阵，旧驱动错误也完整保留。活动稿仅使用 GA-H2、GA-H0、Random-H2 三新组内部比较：81项、75成功、6次600 s超时，共同8电路；独立重核的三臂共同身份与原四臂相同。SA组、全部7次超时和原四臂汇总未删除。论文生成器 `scripts/paper/generate_physical_ga_initial_values.py` 保存完整来源并增加三臂视图；原分析器的串行计划措辞由先导README与执行补充明确更正为实际混合调度。

历史动态前瞻、GA/贪心与五档H研究的起始映射已从匹配trace核对一致，正文用固定输入映射隔离动态决策，不声称这些运行使用新GA初始化。原端到端172倍计时比退出活动稿；新阶段计时从每次完整编译中先剥离完整初始化再聚合，不能冒称纯搜索内核耗时。旧SA初始化扩展仅保留为历史来源，不再占据活动稿方法说明。

先导源码快照 `IEEE_conference_template/build/physical_ga_initial_v1/` 与新主实验的冻结依赖均需保留，不能作为普通缓存清理。

论文比较 ZAC、路由感知放置和 GA-LK；原工作簿中的 GA-NL 是独立配置，不能等同于
共享其他参数的 H = 0 对照。历史诊断与未通过门槛的微调不替代论文结果。

主仓库跟踪当前代码、论文源文件、结果包、补实验轻量协议/评分和维护说明；
`IEEE_conference_template/build/`、虚拟环境、原始大轨迹与整个 `archive/` 不进入新提交。
2026-09-07 归档只改变历史材料的位置与 Git 展示范围；2026-10-01 的另行授权
清理才删除清单内旧产物，两次操作均未重写旧提交。旧全量封存记录不再代表
所有历史 raw 均在，删除范围见[清理记录](repository-maintenance-20261001.md)。
根 `zac/` 是唯一维护中的总项目。归档分类、恢复方式及内容核验索引见
[归档约定](ARCHIVE_POLICY.md)和[迁移索引](archive_relocation_index.json)。

日常检查可在仓库根目录运行：

```bash
git status --short --branch
git branch -vv
git remote -v
git diff --check
```

本地提交与远端同步是两个状态；需要确认远端时先获取远端引用，再检查分支差异。
不要根据旧文档中的提交号或已移除的 worktree 路径判断当前状态。

## Overleaf 同步

中文稿由 `make paper` 生成于 `IEEE_conference_template/build/paper_zh/`；
`make paper-clean` 在其 `clean/` 子目录独立构建清洁稿及框架图。
两版的源文件一致，仅修订颜色开关不同。最近一次已核验 Overleaf 同步记录见
[2026-09-20 修订说明](paper/notes/20260920_reference_anonymity_audit.md)：
提交 `5cd7151`，41个导出文件回读哈希一致，导出副本中英文与独立框架图本地编译通过。
该记录不代表浏览器端编译，也不代表后续 GitHub 同步。历史快照中的旧章号、图号与云端版本仅用于追溯。

[现有 Overleaf 项目](https://www.overleaf.com/project/6a866ce86ea64496e2ae01a5)
保留原来的 Git 历史。同步使用独立暂存目录 `IEEE_conference_template/build/overleaf-sync/`，
不把 Overleaf 设置成整个 ZAC 仓库的推送目标，也不修改桌面副本。

1. 获取 Overleaf 的当前 `main` 提交。若有未合并的人工修改，先停止导出，
   将这些修改合并回 ZAC 论文目录并重新验收，再使用该提交作为预期版本。
2. 执行 `make paper-en`，生成并验收中英文当前图与正文。
3. 执行 `python3 -B scripts/prepare_overleaf_sync.py --expected-remote <已核对的完整提交号>`。
   仅需同步中文时，使用 `--chinese-only` 保留远端英文入口、`sections_en/`、英文 README、
   英文验收器及其测试的现有内容。共享图源和图形仍按当前稿导出；此选项不跳过任何源文件的构建哈希核对。
   默认模式导出两种语言。
4. 审阅 `IEEE_conference_template/build/overleaf-sync/` 的差异，并从该目录独立编译 `paper_zh.tex` 与 `paper_en.tex`，
   检查对应语言的页数目标、引用及图表。编译输出放到该 checkout 以外的 `build/paper_zh/` 审查子目录，
   不把日志和编译缓存提交给 Overleaf。验收后在 checkout 中提交，再正常推送 `origin main`。
5. 用 `git ls-remote` 核实远端提交。远端有新修改时先合并到 ZAC 的论文目录，
   不使用强制推送，也不直接用本地文件覆盖。

准备脚本不自动提交或推送；远端提交与预期不符、暂存目录有未提交修改时会停止。
构建记录把源文件内容与输出 PDF 绑定；即使文件修改时间未变，源码与构建不一致
也不能导出。该记录存放在 `IEEE_conference_template/build/paper_zh/source_build_manifest.json`。
导出流程把框架图引用改为项目内的 `figures/overall_framework.pdf` 并附带已验收的
单页 PDF。历史 SA 四候选稿与当前 `GAMain` 稿的完整六文件来源包仍在本地核验，
包括 CSV 和 JSON 的构建哈希及导出期间并发修改检查。在线项目只复制实际被 TeX
读取的两份数值宏：`ga_main_values.tex`（历史稿对应 `default_initial_values.tex`）
与 `representative_cases.tex`，放在相应 `paper_exports/` 子目录。
其余 CSV/JSON、生成器、测试和来源记录保留在本地仓库，后续导出不再带入在线项目。
在线 README 由同步器生成简短的 XeLaTeX 与中英文主文件说明；本地 README 保持仓库构建说明。
缺文件、符号链接、未登记的外部来源、目标冲突或源码在导出时发生变化都会停止。
普通准备流程不自动删除远端额外文件；2026-09-13按作者明确清理要求，另行核对依赖、
备份云端版本并移除退役文件，记录在 `build/paper_zh/overleaf-cleanup-20260913/`。
除上述依赖路径适配外，正文、TikZ 源、公式、数值和参考文献不得在导出时改写。
本地 `.latexmkrc` 不上传，以免把本机输出目录带入 Overleaf。
Overleaf 的主文档按需选 `paper_zh.tex` 或 `paper_en.tex`，编译器为 XeLaTeX。

`--check-only` 不复制论文文件，但仍会获取远端并尝试快进同步 checkout；仅需只读检查远端
时应单独使用安全的 Git fetch/diff，不把该选项视为无 Git 状态变更的检查。

该流程采用 Overleaf 官方的 [Git 集成](https://docs.overleaf.com/integrations-and-add-ons/git-integration-and-github-synchronization/git)
与 [远端提交核对方式](https://docs.overleaf.com/integrations-and-add-ons/git-integration-and-github-synchronization/git-integration/advanced-git-operations)。

当前 `figures/experimental_summary.tex` 使用 `horizon_trends.dat` 展示十个固定起始映射电路的五档前瞻趋势，即正文图4。完整逐电路分布与物理损失分量图分别保存在 `docs/paper/supplementary/figures/fidelity_distribution_supplement.tex` 和 `loss_decomposition_supplement.tex`；冻结数据及来源记录保留其历史身份。
