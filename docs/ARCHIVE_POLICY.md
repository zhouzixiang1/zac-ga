# 一个主项目，两类本地材料

日常开发、论文维护及 Git 提交均以仓库根 `zac/` 的 `main` 为唯一入口。
不建立另一份总项目，不把本地归档作为新的开发分支或发布内容。

## Git 展示范围

- `IEEE_conference_template/`：当前论文、TikZ/PGFPlots 图、数值展示与校验。
- `ZAC_zzx/`：当前 GA-LK 实现、原生内核、运行器、配置及测试。
- `ZAC/`、`experiments/`：当前两项基线所需的源码与辅助复现材料。
- `ZAC_zzx/results/paper_zh_v2/`：已接受主结果、逐电路表、汇总及来源清单。
- `ZAC_zzx/results/default_initial_v1/paper_exports/`：历史 SA 四候选稿的总体质量与逐电路展示数据；不覆盖原接受包。
- `ZAC_zzx/results/physical_ga_main_v1/` 和 `physical_ga_initial_v1/`：当前稿的 GA 主实验、初始化先导、协议、逐任务评分、失败回执及展示数据。
- 当前补实验的轻量协议、汇总、逐任务评分与回执：与主结果分开，不将未完成研究写成已接受改进。
- `docs/`、`scripts/` 和 `documents/`：维护、复现、审计及论文参考材料。

`archive/` 全部排除主仓库 Git。已跟踪历史文件需要从索引移除，但本地
归档文件保留，旧提交历史也保留；这不是历史重写或数据删除。

原生轨迹、大日志、已安装环境和机器本地的原始运行不随日常提交。新研究冻结的第三方完整输入及作者另行提供的模板也只在本地保存；Git 中保留输入身份、哈希及获取方式，不把本地授权自动视为公开再分发授权。
初始化补实验不能整个排除 `jobs/`：论文数值校验仍需各任务的协议、汇总和
各配置的评分 JSON。`.gitignore` 仅排除该层其他原始产物及日志。

## 当前实验保留范围

保留 `paper_zh_v2/`、`default_initial_v1/`、`initial_lookahead_v1/`、
`physical_ga_initial_v1/`、`physical_ga_main_v1/`、
`horizon_extension_v1/`、`horizon_extension_v2/`、`refinement_v1/` 和
`supplementary_20260907/` 的全部本地数据，包括失败、超时和中断记录。
旧层数研究 v1 已由 v2 质量阶段替代，不恢复旧队列，也不因此删除旧记录。

`fidelity-lookahead-v2/artifacts/` 保留当前论文的规范化输入、基线复现证明、
正式运行、初始化与参数选择依据，以及冻结 wheel、环境和源码。
其中部分被主结果复用的目录虽然名为 `tuning-quality-v1-51583a5` 或
`analysis-v18-zac18-seed0`，仍属于当前证据，不按目录名称清理。

主结果所引用的有效路径维持原位。历史报告或完整性快照中记录的旧路径
不改写，由归档索引提供旧路径到新路径的对应关系；原文件内容与哈希保留。

## 归档分区

| 本地目录 | 内容 |
|---|---|
| `archive/fidelity-lookahead-v2/` | 旧调参、被替代运行、诊断及非当前论文数据集；保持原相对层级 |
| `archive/zac-zzx-legacy/` | 早期比较脚本、配置及原 `ZAC_zzx/archive/` |
| `archive/baseline-pilots/` | 非当前基线范围的试验材料 |
| `archive/manuscript-reviews/` | 历轮论文审阅截图、辅助脚本及中间物 |
| `archive/FABLE/`、`GA/`、`ZAC_new/` 等 | 已有历史实现，保持本地内容不变 |

归档中的独立 `.git` 仅随历史材料保留，不是当前主项目的子模块或同步目标。
历史压缩包默认保留；有解压目录并不代表压缩包没有独有文件。作者授权清理后，须先核对独有内容，再决定是否可用小型保全副本替代包含重复缓存的整包。2026-09-30 的具体处理见 [仓库整理记录](repository-maintenance-20260930.md)；该次清理不包含历史实验归档。

## 迁移与恢复

迁移由 `scripts/archive_repository_material.py` 按明确清单执行，默认只读。
执行前封存每个文件的内容哈希，执行时使用不覆盖目标的原地重命名，
完成后复核所有文件。失败时保留回执并停止，不进行破坏式回滚。

轻量索引见 `docs/archive_relocation_index.json`；完整封存清单及执行回执
位于索引所列的本地审计目录。查找历史文件时按最长匹配的旧路径前缀替换，
不要自动修改冻结 manifest。恢复时也需先检查目标不存在并复核哈希。
根构建目录后来迁入论文目录；查找旧 `build/...` 路径时另见
[构建迁移索引](build_relocation_index.json)，不改写本次历史归档索引。

本次迁移的持久审计副本位于 `archive/_manifests/archive-20260907-v1/`，
不依赖后续 `build/` 的保留。178 项路径迁移经过逐文件哈希核对；原封存的
20,676 个文件、45 个论文源文件与两份 PDF 保持一致，整理后 306 项回归测试通过。
审计仅验证归档与内容保全，不改变原实验的接受范围。

归档仍在本仓库所在磁盘，目录整理本身不会释放约同等大小的磁盘空间。
GitHub 上的源码、汇总与哈希不是原始数据备份；迁往其他磁盘属于另一次操作。
