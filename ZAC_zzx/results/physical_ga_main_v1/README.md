# GA 初始化主实验：独立结果入口

本目录保存 `physical_prefix_ga`（初始化 H=2、最多 32 次不同映射物理评价）与原动态 H=8 组合的完整主矩阵。旧 `default_initial_v1` 与已接受历史数据保持原样；本目录没有覆盖它们。

## 完整性与来源

- 169 个 canonical 输入、172 个文件标签、种子 0/1/2，共 507 项。
- 全量复用先导中同配置的 27 项（24 成功、3 超时），其余 480 项新跑；没有选优复用或失败重试。
- 最终 480 成功、18 超时、6 程序错误、3 内存限制。成功记录中 64 项为完成但保真度模型 OOD，单独列示，不计为程序错误。
- 每项限制 600 秒 / 3 GiB；新跑采用 14 worker 并行，于北京时间 2026-09-13 10:50:29–11:39:06 完成。墙钟仅描述实际开销，不作为串行运行时间比较。复用记录的计时保留先导原调度身份。
- 344 项 ZAC / ICCAD 基线保留原输入、模型、版本、输出验证与 ghost/OOD 口径。新协议、冻结源（含工作区状态）、输入、native 身份和回执均可追溯。

协议：[protocol.json](protocol.json)；SHA-256 `9bb0ae97d2f3f1536a5c496a65fe2e21e2192ce20830979dd75b30b0a3148fc7`。

## 主结果

先对每个文件的每个字段取三个种子的中位数，再在三方法共同完成且保真度有效的文件集合中合并同 canonical 输入的别名字段。保真度采用跨 canonical 输入的几何均值，MOVE 批次与时延采用算术均值。

| 数据集 | 共同 canonical / 文件数 | 相对基线 | 保真度增幅 | MOVE 批次降低 | 逐电路胜 / 平 / 负 |
|---|---:|---|---:|---:|---:|
| ZAC18 | 16 / 16 | ZAC | 5.95% | 15.69% | 12 / 0 / 4 |
| ZAC18 | 16 / 16 | ICCAD | 8.43% | 5.99% | 13 / 0 / 3 |
| QMAP154 | 119 / 121 | ZAC | 26.42% | 25.86% | 48 / 0 / 71 |
| QMAP154 | 119 / 121 | ICCAD | 26.28% | 25.22% | 46 / 0 / 73 |

QMAP 的均值提升与多数电路未获益同时成立。新共同集合较旧版本少 `ising_model_13`（seed 0 超时）；因此新旧数据集均值的变化同时涉及初始化配置与共同集合变化，不能直接当作初始化单因素效应。

## 失败与模型适用范围

| 原始状态 | 电路及种子 |
|---|---|
| timeout | `urf3_155`、`urf4_187`、`hwb9_119`、`ising_n42`、`ising_n98_transpiled`：0/1/2；`plus63mod8192_164`：0/2；`ising_model_13`：0 |
| program_error | `qft_10`、`qft_16`：0/1/2；输入为空门序列，编译快速路径后初始化报告字段缺失，不归因为 GA 搜索失败 |
| memory_limit | `urf1_149`：0/1/2；初始化映射已保存，后续触及单任务内存限制 |

保留的 [失败表](paper_exports/failures.csv)、[完成但 OOD 表](paper_exports/completed_ood.csv) 与 [全部 canonical 记录](paper_exports/canonical_runs.csv) 分别记录原始状态、来源文件和哈希。

## 论文数据与复核

- [数值 JSON](paper_exports/ga_main_values.json)、[GAMain 宏](paper_exports/ga_main_values.tex)
- [逐电路分析单位](paper_exports/analysis_units.csv)、[文件级结果](paper_exports/main_rows.csv)
- [案例与有符号分量](paper_exports/representative_cases.csv)、[案例表宏](paper_exports/representative_cases.tex)
- [全部来源与输出哈希](paper_exports/provenance.json)

历史固定起始映射动态控制与阶段计时在导出中有独立来源字段；21.31 秒为历史串行固定布局组扣除完整初始化后的剩余编译阶段。预算时间比 0.7572 / 0.7517 来自另一历史四路并行组，不是本轮完整 GA 方法的串行速度结论。

独立导出已通过一次只读复核（`status=pass`、`read_only=true`、无差异）。复核绑定 worker 已完成的逻辑/物理验证与原始文件哈希，重算保存分数、集合和统计，不重新编译或重放轨迹：

```bash
PYTHONPATH=ZAC_zzx ZAC/.venv/bin/python -B \
  -m experiments_v2.export_physical_ga_main \
  --protocol ZAC_zzx/results/physical_ga_main_v1/protocol.json \
  --output-root ZAC_zzx/results/physical_ga_main_v1/paper_exports --check
```

数值 JSON SHA-256：`1ced21730cf505003e0b1ae1bfc571d8c308d74a2ce1d7e0479efe348930fad0`。

来源清单 SHA-256：`69fd33a7d97157f7b4e7b39a12450ff785f3bf95b578fb4f013f3826123ab112`。
