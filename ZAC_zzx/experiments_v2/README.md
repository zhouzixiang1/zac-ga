# ZAC 四方法 Schema 2 实验

本目录是 ZAC、ICCAD/QMAP 3.2、Ours-NL 和 Ours-LK 的正式实验轨道。它与历史工作簿和 `*_fidelity.json` 完全隔离；汇总器只接受带冻结 `experiment_id` 的 Schema 2 `RunManifest`。

历史表格、QMAP 3.2 捕获和遗传结果已在 `legacy_pilot_inventory.json` 中按 SHA256 标记为 `legacy/pilot`，只允许做回归和故障诊断，不能进入论文主表。

## 方法边界

- M1：从外层冻结源码树直接运行未修改的原版 ZAC，使用论文的 `maximalis_sort` 路由及其余原始编译设置；正式主实验读取同一 canonical QASM，`resyn=false`，不做论文之外的拆批、waypoint 或 ghost 修复。
- M2：直接运行 `mqt.qmap==3.2.0` 的 routing-aware A*，固定论文参数，禁止 routing-agnostic fallback；官方编译器产生的 NA 文本按字节原样进入评分器，不做后处理。
- M3：跨层驻留 GA，使用正式衰减 spec 的 `max_horizon=0`；只能看到当前边界和目标 2Q 层，任何 future-layer 访问都会失败。
- M4：与 M3 共用 `physical_terminal_decay_v1`，仅把 `max_horizon` 改为 8。未来物理启发项按 `alpha_lookahead * rho^(offset-1)` 加权，默认 `rho=0.6`；当裸衰减因子低于 `epsilon=0.05` 时在读取该层前截断，因此默认有效深度为 6。当前边界的真实物理 NLL 始终系数为 1、不折扣。

创新声明只能落在跨越非相邻层的驻留、真实物理代价、滚动多层前瞻、分相位 ghost 硬保证和有界内存编译上，不能声称首次 routing-aware placement 或首次 look-ahead。

复现轨道只用论文数值校准实现和参数；正式表中的每个数都来自在相应数据集上重新实际运行，绝不从论文抄入、插值或补齐。论文重合电路用于报告复现误差，论文未覆盖的电路则由同一冻结实现直接外推编译。

ghost 约束按方法原始能力执行。M1/M2 的原生轨迹始终审计并记录 `ghost_hits`，但不修复、也不因 stationary ghost 判失败；这两列不能宣称满足我们新增的 ghost-safe 硬保证。M3/M4 在生成期和独立重放期都要求 `ghost_hits=0`，否则失败。四方法仍共同强制 canonical 门账本、轨迹可解析性、连续性、占位、held 状态及各自声明的 AOD 规则，并用同一 ZAC 物理模型评分。Manifest 固定记录 `trace_protocol`、`ghost_policy` 和 `physicalization_policy`，旧的 physicalized-baseline 结果缺少这些契约，不能进入新汇总。

## 正式执行顺序

本轮只生成 ZAC18、QMAP154 和 Runtime 三张结果表，不运行 Large、消融、
可视化或旧 `aggregate`。从仓库内 `ZAC_zzx` 目录按以下顺序执行；每个
“提交”节点都必须 commit、push，并确认工作树 clean 后才能进入下一阶段。

```bash
# 0. 在已经通过全套测试的 clean implementation commit 上冻结输入。
python -m experiments_v2 canonicalize-suite --plan experiments_v2/experiment_plan_v2.json --datasets zac18 qmap154
# 同一 clean commit 上执行 native/README.md 的 attest -> benchmark -> freeze；
# build_manifest.json 必须为 artifact_status=frozen，dirty candidate 不可用。

# 1. 180 次共同初始布局门；apply 只允许同时修改 M3/M4 的 init_engine。
python -m experiments_v2.initial_placement_cli prepare --plan experiments_v2/experiment_plan_v2.json --root ../../artifacts/native-ga-v1/initial-placement --dataset qmap154
python -m experiments_v2.initial_placement_cli run --plan experiments_v2/experiment_plan_v2.json --root ../../artifacts/native-ga-v1/initial-placement --dataset qmap154 --resume
python -m experiments_v2.initial_placement_cli finalize --root ../../artifacts/native-ga-v1/initial-placement
python -m experiments_v2.initial_placement_cli apply --plan experiments_v2/experiment_plan_v2.json --root ../../artifacts/native-ga-v1/initial-placement

# 2. 提交并推送 init_engine 选择，然后在该 clean commit 上完成 1026 次调参。
python -m experiments_v2.tuning_cli prepare --plan experiments_v2/experiment_plan_v2.json --root ../../artifacts/native-ga-v1/tuning --dataset qmap154
python -m experiments_v2.tuning_cli run-phase --plan experiments_v2/experiment_plan_v2.json --root ../../artifacts/native-ga-v1/tuning --dataset qmap154 --phase screen --resume
python -m experiments_v2.tuning_cli promote --root ../../artifacts/native-ga-v1/tuning --phase screen
python -m experiments_v2.tuning_cli run-phase --plan experiments_v2/experiment_plan_v2.json --root ../../artifacts/native-ga-v1/tuning --dataset qmap154 --phase successive_halving --resume
python -m experiments_v2.tuning_cli promote --root ../../artifacts/native-ga-v1/tuning --phase successive_halving
python -m experiments_v2.tuning_cli run-phase --plan experiments_v2/experiment_plan_v2.json --root ../../artifacts/native-ga-v1/tuning --dataset qmap154 --phase validation --resume
python -m experiments_v2.tuning_cli finalize --plan experiments_v2/experiment_plan_v2.json --root ../../artifacts/native-ga-v1/tuning --config-output exp_setting/native_ga_v1

# 3. 提交并推送最终 shared configs；在最终 clean commit 上重跑论文原始基线复现门。
python -m experiments_v2 reproduce-baselines --plan experiments_v2/experiment_plan_v2.json

# 4. 正式 coverage、质量和同阶段计时；每一步都会重放 initial/tuning/reproduction 门。
python -m experiments_v2 run-coverage --plan experiments_v2/experiment_plan_v2.json --datasets zac18 qmap154 --resume
python -m experiments_v2 run-main --plan experiments_v2/experiment_plan_v2.json --datasets zac18 qmap154 --seeds 0,1,2,3,4 --resume
python -m experiments_v2 run-timing --plan experiments_v2/experiment_plan_v2.json --datasets zac18 qmap154 --resume

# 5. 直接从正式run report封存完整cohort，不扫描runs目录。
python -m experiments_v2.final_results_cli seal-report-index --plan experiments_v2/experiment_plan_v2.json --report ../../artifacts/native-ga-v1/reports/run-main.json --run-kind main --output ../../artifacts/native-ga-v1/reports/quality_index.json
python -m experiments_v2.final_results_cli seal-report-index --plan experiments_v2/experiment_plan_v2.json --report ../../artifacts/native-ga-v1/reports/run-timing.json --run-kind timing --output ../../artifacts/native-ga-v1/reports/timing_index.json
python -m experiments_v2.final_results_cli aggregate --plan experiments_v2/experiment_plan_v2.json --quality-index ../../artifacts/native-ga-v1/reports/quality_index.json --timing-index ../../artifacts/native-ga-v1/reports/timing_index.json --output-directory ../../artifacts/native-ga-v1/reports
python -m experiments_v2.final_results_cli render --contract ../../artifacts/native-ga-v1/reports/workbook_contract.json --aggregation-provenance ../../artifacts/native-ga-v1/reports/aggregation_provenance.json --output ../../artifacts/native-ga-v1/reports/four_methods_results.xlsx --qa-directory ../../artifacts/native-ga-v1/reports/workbook_qa --node /Users/zhouzixiang/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node --node-modules /Users/zhouzixiang/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules
```

初始布局门使用 `sa-vs-ga-initial-v2`。每个电路/方法只要任一 SA/GA
seed 超出论文线性退相干模型的适用域，该电路/方法的全部十次运行就统一用
manifest 中的指数退相干敏感性 logF 比较；否则全部使用线性 logF。这样不会
把两种不同的 coherence 模型直接相减，原始线性 OOD 仍保持空值。

调参不得只跑 screen 后直接进入主实验。`successive_halving` 和 `validation`
任一 ledger 不完整、promotion 重放不一致或共享配置未形成 clean commit，
`run-main`、`run-timing` 和最终汇总都会 fail closed。最终汇总只接受显式、
哈希封存的 `main`/`timing` manifest index，不扫描旧结果目录。

正式 attempt 只允许在 clean commit 上启动。每次运行使用唯一临时目录，完成后原子提升；终态固定为 `success/timeout/oom/compiler_error/verifier_fail/scorer_error`。600 秒超时会杀死整个进程组，旧同名产物不会被读取。

`canonicalize-suite` 会自动重新启动到计划中声明的 Qiskit 1.2.4 Python；因此即使当前 shell 的 `python` 是 Qiskit 2.x，也不会用错 canonicalizer。coverage、main、timing 和 ablation 都支持 `--resume`，且只跳过 Git commit、experiment ID、输入、配置、架构和模型身份完全一致的 attempt。

`reproduce-baselines` 同样只允许在 clean commit 上启动，并把 Git commit、机器/内存/系统、计划、两套架构、统一物理模型、论文 truth 表和两个环境锁的 SHA256 写入报告。后续每次正式 attempt 都会重新计算这些身份并重新验证 M1/M2 的 sealed native 证据；代码、锁文件或任一冻结输入变化后，旧复现报告不能解锁新实验。M1 原文复现使用 `experiments/zac_arch_repro.json` 中原 artifact 的调度参数；主实验四方法则统一使用 `full_architecture.json` 和 52 μs 的 ZAC 物理评分，二者不混表。

M1 的 fidelity/duration 与论文派生表比较；论文没有逐项公开的结构量则与 tracked ZAC artifact 在 Qiskit 1.2.4 下冻结的 reference 精确比较。正式门逐例要求 qubits、1Q/2Q gates、depth、2Q layer 数、最大层宽以及 emitted 1Q/2Q 数全部一致，并在报告中把它标为 `artifact_reference_exact`，不会冒充“论文公开结构”。

`run-main` 强制 M3/M4 使用配对种子 0–4；M1/M2 质量运行一次。`run-timing` 先做不计入统计的 warm-up，再在 seed 0 上串行执行五次，统计同时报告成功运行时间和 PAR-2。

`run-ablation` 固定使用 0–4 五个种子。ZAC18 全量运行；QMAP154 按 canonical 2Q 门数 `<=300 / 301–1500 / >1500` 分层，每层确定性选 canonical SHA256 最小的 10 个电路。六个非重复实验臂为：`h0`、完整衰减前瞻和 phase-coloring 参照 `decay_phase_coloring`、容量安全的 `always_stay`、每层（包括末层）全回存储的 `always_return`、仅允许相邻 2Q 层直接复用的 `adjacent_only`，以及 lumped fitness + ghost-safe greedy batching 的 `lumped_greedy`。每次 attempt 使用独立 ablation wrapper 与 `RunManifest.ablation_variant`；主 M3/M4 配置不增加任何消融字段，非 ablation 轨道收到消融 wrapper 会直接失败。

`aggregate-ablation` 只读取同一 frozen ablation `experiment_id` 下的 `run_kind=ablation` manifest。它要求每个电路/变体恰有 seed 0–4、`repetition=0` 的五次成功运行，先在电路内取中位数，再汇总 log-fidelity、Move 批次、Move 时间和编译时间。以完整 `decay_phase_coloring` 为参照，逐电路给出 bootstrap 95% CI、双侧 paired Wilcoxon 和每个指标内的 Holm 校正；这些比较全部标记为 exploratory。缺种子、重复 attempt、失败或 OOD 都逐电路显式保留为 invalid；报告固定 `diagnostic_only=true` 和 `eligible_for_main_claim_gate=false`，不会进入主论文结论门。命令同时生成 Markdown/LaTeX/CSV、经逐 sheet QA 的 `ablation.xlsx` 以及四指标、ECDF 和配对效应的 SVG/PDF/PNG 图。

`aggregate` 会一次生成完整交付目录：可追溯 JSON/CSV、Markdown、LaTeX、四方法四指标主表、预注册分层表、经 artifact-tool 渲染和逐 sheet 预览检查的 `report.xlsx`，以及 coverage、四指标、ECDF、fidelity-B* 归一化 Pareto 和规模分层的 SVG/PDF/PNG 图。所有 ECDF/Pareto 点只来自严格 paired cohort；缺失值保持空白，不插补。若论文结论门未通过，每张图和文字报告都会明确标记为 diagnostic only。

## 统一计量

两种 native 输出先归一为 `CanonicalTraceEvent`，再由同一个严格重放器计算：

- log-domain ZAC 物理保真度及 1Q、2Q、idle excitation、transfer、coherence 五项分解；
- 完整 `load -> move+ -> store` 的 Move 批次；
- 每个 phase 最长轨迹累加得到的 Move 时间；
- 只包围编译核心的实现级 wall/CPU 时间。

任何 1Q/2Q 账本差异、重复占位、依赖错误或 AOD/held 状态错误都使运行失败；ghost hit 仅在 M3/M4 中是硬失败，在 M1/M2 中是可追溯诊断量。线性退相干模型在任一原子 `t_q >= T2` 时标记 OOD，不产生伪造的线性保真度，只保留指数退相干敏感性结果。

## Large 轨道

Large 输入固定到 QASMBench commit `357b942396d5c2b7cbc1c229c585a6ef5ccaebac`，采用逐语句标准门展开，禁止跨门优化。QASMBench 中用于电路生命周期的 `reset/measure/barrier/creg` 会被删除并在 canonical manifest 中逐项记录；因此 Large 只验证路由/编译可扩展性，reset 的物理时间和误差不纳入统一模型。

流式基座提供 SQLite 层库、逐原子逻辑账本、2Q 前瞻窗口、增量验证/计分、压缩 JSONL 事件、每 1000 层或 5 分钟的原子检查点，以及恢复一致性和 RSS 探针。`streaming/large_compiler.py` 还提供一个只用于集成和性能验证的四策略开发代理；其产物固定标记 `support_claim_eligible=false`，不能进入正式 Large 表。当前缺少的是与原 ZAC、QMAP 3.2 A* 和 Schema-2 GA 逐项等价的正式流式适配器，因此冻结契约仍记录 `streaming_compiler_integrated=false`：`run-large --dry-run` 只输出不可执行的阻塞计划，正式 `run-large` 会 fail-closed，绝不回退到普通内存路径。只有某个 Large 电路完整经过正式方法流式编译、严格验证和统一计分后，报告才能写“支持该电路”；仅完成解析、canonicalize、开发代理、RSS 探针或 timeout/OOM 尝试都不算支持。

## 论文门控

两个数据集分别统计，先按电路对五个随机种子取中位数，再做配对分析。M4 必须同时通过 M4-vs-M3 和 M4-vs-每电路最强保真度基线的 2% 几何均值、Holm 校正、同步 95% CI 下界和预注册分层一致性门；Move 至少一项显著更好或两项均在 2% 非劣界内。任何门未通过时，输出只能标记为诊断，不能包装成论文正结果。

环境快照见 [environment_zac_qiskit124.lock.txt](environment_zac_qiskit124.lock.txt) 和 [environment_iccad_qmap320.lock.txt](environment_iccad_qmap320.lock.txt)。
