# Chinese-to-English manuscript ledger

The English manuscript is a full translation of the maintained Chinese draft,
not a shortened or independently argued paper. Section order, equation labels,
citations, numerical macros, selected circuits, and evidence qualifications are
preserved. English is written for an IEEE computer-architecture/EDA audience.

| Chinese | Canonical English |
|---|---|
| 分区中性原子量子架构 | zoned neutral-atom quantum architecture |
| 层边界联合优化 | joint layer-boundary optimization |
| 衰减多层物理前瞻 | decayed multilayer physical look-ahead |
| 跨层驻留 | inter-layer residency |
| 门位 | gate site |
| 门位对 | gate-site pair, defined as a pair of traps for one two-qubit gate |
| 存储位选择/分配 | storage-site selection/assignment |
| 回迁至存储区 | return to the storage zone |
| 候选可行性检查 | candidate feasibility check |
| 物理状态转移评价 | physical state-transition evaluation |
| 非目标交点约束 | unintended-intersection constraint |
| 阱转移 | trap transfer |
| 并行重排批次 | parallel rearrangement batch |
| 重排时延 | rearrangement time |
| 模型保真度 | model fidelity |
| 空闲原子受激 | idle-atom excitation |
| 最大视界 | maximum horizon |
| 评价预算 | evaluation budget |
| 有限视界未来代价 | finite-horizon future cost |

Keep GA-LK, GA-NL, ZAC, ICCAD/QMAP, ZAC18, QMAP154, SLM, AOD, and ZAIR unchanged.
Use nonadjacent and benchmark sets consistently.
Define spatial light modulator (SLM), acousto-optic deflector (AOD), and zoned
architecture intermediate representation (ZAIR) at first relevant use. Use
look-ahead consistently, including the title. Uppercase internal action names
and experiment IDs are not manuscript terminology; Apple M4 Pro is a processor
name and is retained. The English version retains the Chinese text's measured
compiler-runtime comparison and the stated circuit-selection conditions.

Core propositions: joint gate-site/residency/storage-site decisions are evaluated
under physical routing constraints; conflict-graph coloring groups compatible
trajectories; decayed future-layer costs start from each candidate's resulting
state. Existing ZAC/QMAP methods retain their original attribution. All fidelity
results describe the specified model, not measurements on quantum hardware.
