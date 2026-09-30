# Chinese-to-English manuscript ledger

The current English manuscript translates the clean Chinese version uploaded to
Overleaf as `2ebed8b1bf9bceeca318705d24fcc8844e6a40e0` on 2026-09-13. Section order,
claims, equation labels, citations, result macros, selection rules, and evidence
boundaries are preserved. English sentence boundaries and pagination may differ.

The fixed title is **GA-LK: A Multi-Layer Look-Ahead Compiler for Joint Placement
on Zoned Neutral-Atom Architectures**. Both manuscripts retain an empty author
block and the implementation repository link.

| Chinese | Canonical English |
|---|---|
| 分区中性原子架构 | zoned neutral-atom architecture |
| 多层前瞻 | multi-layer look-ahead |
| 衰减未来损失 | discounted future loss |
| 前瞻层数上限 $H$ | look-ahead upper bound $H$ |
| 实际预测层数 | actual prediction depth |
| 层内联合优化 | joint optimization within each layer |
| 门位 / 门位对 | gate site / gate-site pair |
| 跨层驻留 | inter-layer residency |
| 回迁存储位 | return storage site |
| 回迁至存储区 | return to the storage zone |
| 纠缠区 / 存储区 | entangling zone / storage zone |
| 完整候选 | complete candidate |
| 候选执行后配置 | candidate's post-execution configuration |
| 阱转移 | trap transfer |
| AOD输运 | AOD transport |
| 非目标交点约束 | unintended-intersection constraint |
| 重排批次 | rearrangement batches |
| 重排时延 | rearrangement latency (time in compact table labels) |
| 模型保真度 | model-estimated fidelity (fidelity after definition) |
| 空闲受激损失 | idle-excitation loss |
| 相干损失 | decoherence loss |
| 物理GA初始化 | physical GA initialization |
| 固定初始映射 | fixed initial mapping |
| QMAP电路集 | QMAP circuit set |

Define spatial light modulator (SLM) and acousto-optic deflector (AOD) at first
relevant use. Preserve GA-LK, ZAC, ICCAD/QMAP, ZAC18, and ZAIR; do not invent an
expansion for LK. QMAP154 remains only where required by an existing data alias.
Use model-estimated fidelity for headline results: these are not hardware
measurements. Preserve the geometric mean for fidelity and the arithmetic mean
for main-table batches and latency. The separate horizon figure uses geometric
means of per-circuit ratios for both metrics.

The method uses physical GA initialization. Historical SA initialization and
its results remain in the evidence archive but are not restored in translation.
The main comparison, dynamic controls with fixed initial mappings, initialization
controls, and independent timing study retain their respective configurations.
Nominal H and actual prediction depth remain distinct. The quarter-gain admission
rule, truncation and terminal-return boundaries, and the current-layer-only
execution rule are unchanged. The source's two contributions remain two.
