# ZAC 四方法 Schema 2 实验

本目录是 ZAC、ICCAD/QMAP 3.2、Ours-NL 和 Ours-LK 的正式实验轨道。它与历史工作簿和 `*_fidelity.json` 完全隔离；汇总器只接受带冻结 `experiment_id` 的 Schema 2 `RunManifest`。

历史表格、QMAP 3.2 捕获和遗传结果已在 `legacy_pilot_inventory.json` 中按 SHA256 标记为 `legacy/pilot`，只允许做回归和故障诊断，不能进入论文主表。

## 方法边界

- M1：原版 ZAC 的放置与 maximal-independent-set 端点决策，正式主实验读取同一 canonical QASM，`resyn=false`；之后只执行公共物理合法化层。
- M2：`mqt.qmap==3.2.0` 的 routing-aware A*，固定论文参数，禁止 routing-agnostic fallback；保留 QMAP 的端点和逐原子轨迹，再执行同一口径的公共物理合法化层。
- M3：跨层驻留 GA，`lookahead_horizon=0`；只能看到当前边界和目标 2Q 层。
- M4：与 M3 的有效配置只差 `method_id`、输出目录和 `lookahead_horizon=2`；可额外读取后续两个 2Q 层。

创新声明只能落在跨越非相邻层的驻留、真实物理代价、滚动多层前瞻、分相位 ghost 硬保证和有界内存编译上，不能声称首次 routing-aware placement 或首次 look-ahead。

四方法最终进入评分器的轨迹都必须满足同一 ghost 硬约束。原 ZAC 和 QMAP 3.2 的冲突图没有把静止原子纳入判定，因此原始 native 轨迹另存为证据，但不能直接冒充可执行结果：公共合法化层不改变门序或目标落位，只拆分实际非法的 batch；若单原子原路径仍撞 ghost，则显式加入空闲合法 SLM waypoint。增加的 Move batch、Move phase、transfer 和时间全部进入统一指标，repair/split 计数写入 `compiler_stats.json`。M3/M4 在生成期调用同一逐 phase 重放逻辑，repair 后必须再次验证；任何无法合法化的轨迹仍然 fail-closed。

## 正式执行顺序

从仓库内 `ZAC_zzx` 目录执行：

```bash
python -m experiments_v2 reproduce-baselines --plan experiments_v2/experiment_plan_v2.json
python -m experiments_v2 canonicalize-suite --plan experiments_v2/experiment_plan_v2.json --datasets zac18 qmap154
python -m experiments_v2 run-coverage --plan experiments_v2/experiment_plan_v2.json --datasets zac18 qmap154 --resume
python -m experiments_v2 run-main --plan experiments_v2/experiment_plan_v2.json --datasets zac18 qmap154 --seeds 0,1,2,3,4 --resume
python -m experiments_v2 run-timing --plan experiments_v2/experiment_plan_v2.json --datasets zac18 qmap154 --resume
python -m experiments_v2 run-ablation --plan experiments_v2/experiment_plan_v2.json --datasets zac18 qmap154 --resume
python -m experiments_v2 aggregate-ablation --plan experiments_v2/experiment_plan_v2.json --dataset zac18
python -m experiments_v2 aggregate-ablation --plan experiments_v2/experiment_plan_v2.json --dataset qmap154
python -m experiments_v2 aggregate --plan experiments_v2/experiment_plan_v2.json --dataset zac18
python -m experiments_v2 aggregate --plan experiments_v2/experiment_plan_v2.json --dataset qmap154
```

正式 attempt 只允许在 clean commit 上启动。每次运行使用唯一临时目录，完成后原子提升；终态固定为 `success/timeout/oom/compiler_error/verifier_fail/scorer_error`。600 秒超时会杀死整个进程组，旧同名产物不会被读取。

`canonicalize-suite` 会自动重新启动到计划中声明的 Qiskit 1.2.4 Python；因此即使当前 shell 的 `python` 是 Qiskit 2.x，也不会用错 canonicalizer。coverage、main、timing 和 ablation 都支持 `--resume`，且只跳过 Git commit、experiment ID、输入、配置、架构和模型身份完全一致的 attempt。

`reproduce-baselines` 同样只允许在 clean commit 上启动，并把 Git commit、机器/内存/系统、计划、两套架构、统一物理模型、论文 truth 表和两个环境锁的 SHA256 写入报告。后续每次正式 attempt 都会重新计算这些身份并重新验证 M1/M2 的 sealed native 证据；代码、锁文件或任一冻结输入变化后，旧复现报告不能解锁新实验。M1 原文复现使用 `experiments/zac_arch_repro.json` 中原 artifact 的调度参数；主实验四方法则统一使用 `full_architecture.json` 和 52 μs 的 ZAC 物理评分，二者不混表。

M1 的 fidelity/duration 与论文派生表比较；论文没有逐项公开的结构量则与 tracked ZAC artifact 在 Qiskit 1.2.4 下冻结的 reference 精确比较。正式门逐例要求 qubits、1Q/2Q gates、depth、2Q layer 数、最大层宽以及 emitted 1Q/2Q 数全部一致，并在报告中把它标为 `artifact_reference_exact`，不会冒充“论文公开结构”。

`run-main` 强制 M3/M4 使用配对种子 0–4；M1/M2 质量运行一次。`run-timing` 先做不计入统计的 warm-up，再在 seed 0 上串行执行五次，统计同时报告成功运行时间和 PAR-2。

`run-ablation` 固定使用 0–4 五个种子。ZAC18 全量运行；QMAP154 按 canonical 2Q 门数 `<=300 / 301–1500 / >1500` 分层，每层确定性选 canonical SHA256 最小的 10 个电路。六个非重复实验臂为：`h0`、兼作 phase-coloring 参照的 `h2_phase_coloring`、容量安全的 `always_stay`、每层（包括末层）全回存储的 `always_return`、仅允许相邻 2Q 层直接复用的 `adjacent_only`，以及 lumped fitness + ghost-safe greedy batching 的 `lumped_greedy`。每次 attempt 使用独立 ablation wrapper 与 `RunManifest.ablation_variant`；主 M3/M4 配置不增加任何消融字段，非 ablation 轨道收到消融 wrapper 会直接失败。

`aggregate-ablation` 只读取同一 frozen ablation `experiment_id` 下的 `run_kind=ablation` manifest。它要求每个电路/变体恰有 seed 0–4、`repetition=0` 的五次成功运行，先在电路内取中位数，再汇总 log-fidelity、Move 批次、Move 时间和编译时间。以完整 `h2_phase_coloring` 为参照，逐电路给出 bootstrap 95% CI、双侧 paired Wilcoxon 和每个指标内的 Holm 校正；这些比较全部标记为 exploratory。缺种子、重复 attempt、失败或 OOD 都逐电路显式保留为 invalid；报告固定 `diagnostic_only=true` 和 `eligible_for_main_claim_gate=false`，不会进入主论文结论门。命令同时生成 Markdown/LaTeX/CSV、经逐 sheet QA 的 `ablation.xlsx` 以及四指标、ECDF 和配对效应的 SVG/PDF/PNG 图。

`aggregate` 会一次生成完整交付目录：可追溯 JSON/CSV、Markdown、LaTeX、四方法四指标主表、预注册分层表、经 artifact-tool 渲染和逐 sheet 预览检查的 `report.xlsx`，以及 coverage、四指标、ECDF、fidelity-B* 归一化 Pareto 和规模分层的 SVG/PDF/PNG 图。所有 ECDF/Pareto 点只来自严格 paired cohort；缺失值保持空白，不插补。若论文结论门未通过，每张图和文字报告都会明确标记为 diagnostic only。

## 统一计量

两种 native 输出先归一为 `CanonicalTraceEvent`，再由同一个严格重放器计算：

- log-domain ZAC 物理保真度及 1Q、2Q、idle excitation、transfer、coherence 五项分解；
- 完整 `load -> move+ -> store` 的 Move 批次；
- 每个 phase 最长轨迹累加得到的 Move 时间；
- 只包围编译核心的实现级 wall/CPU 时间。

任何 1Q/2Q 账本差异、重复占位、依赖错误、AOD/held 状态错误或 ghost hit 都使运行失败。线性退相干模型在任一原子 `t_q >= T2` 时标记 OOD，不产生伪造的线性保真度，只保留指数退相干敏感性结果。

## Large 轨道

Large 输入固定到 QASMBench commit `357b942396d5c2b7cbc1c229c585a6ef5ccaebac`，采用逐语句标准门展开，禁止跨门优化。QASMBench 中用于电路生命周期的 `reset/measure/barrier/creg` 会被删除并在 canonical manifest 中逐项记录；因此 Large 只验证路由/编译可扩展性，reset 的物理时间和误差不纳入统一模型。

流式基座提供 SQLite 层库、2Q 前瞻窗口、增量验证/计分、压缩 JSONL 事件、每 1000 层或 5 分钟的原子检查点，以及恢复一致性和 RSS 探针。当前这些组件尚未接入四方法的端到端编译器，冻结契约明确记录 `streaming_compiler_integrated=false`：`run-large --dry-run` 只输出不可执行的阻塞计划，正式 `run-large` 会 fail-closed，绝不回退到普通内存路径。只有某个 Large 电路完整经过流式编译、严格验证和统一计分后，报告才能写“支持该电路”；仅完成解析、canonicalize、RSS 探针或 timeout/OOM 尝试都不算支持。

## 论文门控

两个数据集分别统计，先按电路对五个随机种子取中位数，再做配对分析。M4 必须同时通过 M4-vs-M3 和 M4-vs-每电路最强保真度基线的 2% 几何均值、Holm 校正、同步 95% CI 下界和预注册分层一致性门；Move 至少一项显著更好或两项均在 2% 非劣界内。任何门未通过时，输出只能标记为诊断，不能包装成论文正结果。

环境快照见 [environment_zac_qiskit124.lock.txt](environment_zac_qiskit124.lock.txt) 和 [environment_iccad_qmap320.lock.txt](environment_iccad_qmap320.lock.txt)。
