# 仓库导航与维护约定

本仓库统一保存 GA-LK 代码、论文源文件和当前论文结果。日常修改在 `main`
分支进行；GitHub 远端为 `https://github.com/zhouzixiang1/zac-ga`，目前为私有仓库。

## 先找哪个文件

| 任务 | 入口 |
|---|---|
| 修改论文 | [`../IEEE_conference_template/paper_zh.tex`](../IEEE_conference_template/paper_zh.tex) 及其 `sections/` |
| 对照审阅英文稿 | [`../IEEE_conference_template/paper_en.tex`](../IEEE_conference_template/paper_en.tex) 及同名 `sections_en/` 文件 |
| 修改论文图 | `../IEEE_conference_template/figures/` 中的 TikZ 源文件 |
| 阅读最新编译稿 | 仓库根目录下的 `build/paper_zh/paper_zh.pdf` |
| 查看最终结果 | [`../ZAC_zzx/results/paper_zh_v2/final_manifest.json`](../ZAC_zzx/results/paper_zh_v2/final_manifest.json) |
| 逐电路人工审计 | [`../ZAC_zzx/results/paper_zh_v2/four_methods_results.xlsx`](../ZAC_zzx/results/paper_zh_v2/four_methods_results.xlsx) |
| 理解 Python 编译集成 | [`../ZAC_zzx/zzx/`](../ZAC_zzx/zzx/) |
| 理解联合搜索和物理评价 | [`../ZAC_zzx/native/`](../ZAC_zzx/native/) |
| 查看正式论文实验入口 | [`../ZAC_zzx/experiments_v2/paper_cli.py`](../ZAC_zzx/experiments_v2/paper_cli.py) |
| 查看原版 ZAC | [`../ZAC/run.py`](../ZAC/run.py) 和 [`../ZAC/zac/`](../ZAC/zac/) |

论文数值由现有结果和生成脚本维护，不在正文中手工另填一份。
重新编译论文不会重新运行量子电路实验。

## 目录分工

```text
zac/
├── IEEE_conference_template/    论文源文件、TikZ 图体、数值与校验脚本
├── ZAC_zzx/                    当前方法、实验管线和回归测试
│   ├── zzx/                    Python 编译集成
│   ├── native/                 C++17 搜索内核
│   ├── experiments_v2/         运行、汇总、验证、来源记录
│   ├── exp_setting/            编译与实验配置
│   ├── results/paper_zh_v2/    当前权威结果包
│   └── third_party/            冻结 QMAP 补丁与验证材料
├── ZAC/                        原版 ZAC 基线与本地运行环境
├── experiments/                基线复现辅助脚本和小型证据
├── documents/                  baseline 论文
├── docs/                       仓库维护说明
├── archive/                    历史实现与参考工作区
├── build/                      新构建产物，不进入 Git
├── qmap-main/                  本地 QMAP 源码，不进入主仓库 Git
└── fidelity-lookahead-v2/       本地历史原始实验材料，不进入 Git
```

`ZAC_zzx/run.py` 是配置驱动的常规编译入口；当前论文实验通过
`experiments_v2.paper_cli` 管理。`experiments_v2.cli` 还保留通用 Schema-v2
实验操作，早期 `fourway_*`、`compare*` 脚本不替代当前论文结果生成管线。
运行实验需要明确指定计划、配置、环境和输出目录，不能仅凭旧脚本文件名判断其
适用版本。

## 论文只维护这一份

后续编辑目录固定为仓库内的 `IEEE_conference_template/`。
桌面的 `IEEE_conference_template_upgrade/` 保持原样，作为迁回仓库前的副本；
其独立 Git/Overleaf 信息保留，但它不再是日常论文工作目录。
不要在两份副本间自动双向同步，以免覆盖人工修改。

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

英文稿使用 `make paper-en` 与 `make paper-en-preview`，输出位于
`build/paper_en/`。该构建先验收中文稿及共享图3，再校验中英文引用、公式和
结果宏的对应关系。英文分页独立验收，不通过删减翻译内容压到中文稿页数。

统一输出位置：

| 产物 | 仓库内路径 |
|---|---|
| 论文 PDF | `build/paper_zh/paper_zh.pdf` |
| 独立总体框架图 | `build/paper_zh/figures/overall_framework.pdf` |
| LaTeX 中间文件、编译日志 | `build/paper_zh/` 下 |
| 校验报告、逐页渲染与总览图 | `build/paper_zh/` 下 |
| 新原生构建和 wheel | `build/native/` 下 |

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

- `fidelity-lookahead-v2/artifacts/` 中的历史 wheel、构建证明、原始运行记录与轨迹；
- `ZAC/.venv/`、根目录下 QMAP 虚拟环境及已安装的 `.so`；
- `qmap-main/` 和归档中的独立仓库、第三方源码；
- `ZAC_zzx/third_party/qmap32_streaming/` 中的冻结补丁与清单。

“统一放到 `build/`”约束今后的编译输出。历史证明绑定的文件和路径保持原位，
实验数据也不放进可重建的临时构建目录。

## 证据与 Git 边界

当前论文证据以 `ZAC_zzx/results/paper_zh_v2/final_manifest.json` 为索引。
该目录保留完整逐电路结果、汇总、对照和计时信息；历史诊断结果不作为当前论文
数值的替代来源。论文比较 ZAC、ICCAD/QMAP 和 GA-LK；完整工作簿中的 GA-NL
是独立配置，不能等同于共享其他参数的 H = 0 对照。

主仓库跟踪代码、论文源文件、当前结果包和维护说明；`build/`、虚拟环境、
本地原始材料与指定独立仓库不进入主仓库提交。`archive/` 中的受跟踪历史实现
仍随主仓库保存，不能把整个归档目录都视为缓存。

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

[现有 Overleaf 项目](https://www.overleaf.com/project/6a866ce86ea64496e2ae01a5)
保留原来的 Git 历史。同步使用独立暂存目录 `build/overleaf-sync/`，
不把 Overleaf 设置成整个 ZAC 仓库的推送目标，也不修改桌面副本。

1. 获取 Overleaf 的当前 `main` 提交。若有未合并的人工修改，先停止导出，
   将这些修改合并回 ZAC 论文目录并重新验收，再使用该提交作为预期版本。
2. 执行 `make paper`，生成并验收当前图与正文。
3. 执行 `python3 -B scripts/prepare_overleaf_sync.py --expected-remote <已核对的完整提交号>`。
4. 审阅 `build/overleaf-sync/` 的差异，在该目录提交，再正常推送 `origin main`。
5. 用 `git ls-remote` 核实远端提交。远端有新修改时先合并到 ZAC 的论文目录，
   不使用强制推送，也不直接用本地文件覆盖。

准备脚本不自动提交或推送；远端提交与预期不符、暂存目录有未提交修改时会停止。
构建记录把源文件内容与输出 PDF 绑定；即使文件修改时间未变，源码与构建不一致
也不能导出。该记录存放在 `build/paper_zh/source_build_manifest.json`。
导出只把图3引用改为项目内的 `figures/overall_framework.pdf` 并附带已验收的
单页 PDF，正文、TikZ 源、公式、数值和参考文献保持不变。
本地 `.latexmkrc` 不上传，以免把本机输出目录带入 Overleaf。
Overleaf 的主文档应为 `paper_zh.tex`，编译器为 XeLaTeX。

该流程采用 Overleaf 官方的 [Git 集成](https://docs.overleaf.com/integrations-and-add-ons/git-integration-and-github-synchronization/git)
与 [远端提交核对方式](https://docs.overleaf.com/integrations-and-add-ons/git-integration-and-github-synchronization/git-integration/advanced-git-operations)。
