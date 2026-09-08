# 构建输出位置

所有新编译、预览、临时校验和 Overleaf 导出均使用
`IEEE_conference_template/build/`。根目录不保留 `build/`，也不建立兼容链接。
在仓库根运行 `make`；主稿及图表源文件仍在原位。

| 内容 | 仓库相对路径 |
|---|---|
| 中文 PDF、独立图、QA 与预览 | `IEEE_conference_template/build/paper_zh/` |
| 英文 PDF、QA 与预览 | `IEEE_conference_template/build/paper_en/` |
| 新原生构建和 wheel | `IEEE_conference_template/build/native/` |
| Overleaf 本地同步工作区 | `IEEE_conference_template/build/overleaf-sync/` |
| 新临时文件 | `IEEE_conference_template/build/tmp/` |

`make paper` 重建中文稿；`make paper-en` 验收中文稿并构建英文稿；
`make paper-test` 检查生成数据与构建接入。新路径的输出不进入 Git。

## 本次迁移的保全

根目录原 `build/` 整体迁入新位置，移动前后逐文件核对哈希。目标目录已有的
较早稿件未覆盖，保存在 `archive/build-location-20260907/previous-manuscript-build/`。
迁移前的中英文 PDF 与独立图副本在该归档的 `pre-migration-pdfs/` 下。
完整清单与执行回执位于 `archive/_manifests/build-location-20260907/`。
可入 Git 的简明路径索引见 [build_relocation_index.json](build_relocation_index.json)。

查找历史记录中的 `build/...` 时，通常将前缀替换为
`IEEE_conference_template/build/...`。原 `build/native/ctest/` 和
`build/native/wheel/` 例外：旧 CMake 缓存包含旧绝对路径，完整保存在
`IEEE_conference_template/build/native/prior-root-cache/`；新构建使用空目录，
不把旧缓存的搬迁当作一次新构建。

旧归档执行记录与 seal 不改写；其 `run_directory` 仍描述迁移前位置，
直接用旧执行器验证新位置会拒绝。应结合上述新迁移清单与保全验证脚本检查，
而不是编辑旧 seal。此前归档审计的持久副本仍在 `archive/_manifests/`。

## 实验边界

正式结果 `ZAC_zzx/results/paper_zh_v2/`、初始化补实验及其冻结输入不变。
`fidelity-lookahead-v2/artifacts/` 内的历史冻结环境也不属于这次根构建目录迁移。

H 补实验的旧 `horizon_extension_v1/` 协议绑定迁移前的冻结路径与运行器哈希，
保留记录，不再续跑。后续使用独立冻结的 `horizon_extension_v2/` 完成质量阶段；
其串行计时阶段未启用，不属于待恢复任务。`native/mechanism-v1/` 中的旧二进制和
缓存仍只作开发对照，不能据搬迁结果宣称旧缓存可直接续编译。

初始化九电路先导与新增 32 电路扩展的已完成结果也原样保留；它们的协议绑定
旧运行器哈希。路径适配后的开发树若要新增运行，同样需要新版本封存，不能
修改原协议或将当前代码当作当时的执行环境。

`default_initial_v1/` 的完整质量实验已结束，派生数据通过核验后接入中文标红稿。
原接受包 `paper_zh_v2/` 保持不变；各实验的失败、超时和恢复回执继续保留。

## 2026-09-07 迁移验收记录

中文稿在新位置编译通过，仍为九页、参考文献在最后一页；九页渲染与迁移前
逐页像素一致。211 项论文相关测试及原生 CTest 的两项测试通过。
原封存的 20,676 个证据文件内容未变。

以上计数描述迁移时的版本；后续稿件以重新生成的
`paper_zh/final_paper_qa.json` 和 `paper_zh/source_build_manifest.json` 为准，
不以历史验收代替当前源码检查。这些文件位于本页约定的构建根目录内。

英文路径接入已更新，但现有英文稿未同步中文的章节输入及部分公式、引用和
结果宏，严格双语校验未通过；本轮未扩展为翻译修订。旧英文 PDF 已保存在
上述迁移前 PDF 归档，不能将其作为当前中文稿的最新对应版本。
