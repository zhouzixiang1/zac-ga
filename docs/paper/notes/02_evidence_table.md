# 主张—证据表

当前论文最终实验只接受 `ZAC_zzx/results/paper_zh_v2/final_manifest.json` 所约束的证据链，数值入口为同目录 `paper_values.json`，独立分析单元为 `main_primary_analysis_units.csv`，逐电路交付为 `four_methods_results.xlsx`。Git历史中的 `ZAC_zzx/results/native_ga_v1` 是早期seed-0参照，已从当前 `main` 的工作树移除，不得替代当前论文最终源。

| 主张 | 证据/来源 | 强度 | 可用章节 | 风险 | 状态 |
|---|---|---|---|---|---|
| 分区架构通过存储区隔离空闲原子 | Bluvstein 等；ZAC；ICCAD 背景 | 强 | 背景、引言 | 不应扩展为硬件实测本文方法 | evidence-backed |
| ZAC只执行相邻层立即复用匹配 | ZAC 方法章节 | 强 | 背景、相关工作 | ZAC初始布局使用更长程信息，不能写“完全无前瞻” | evidence-backed |
| ICCAD已有routing-aware A*和下一层look-ahead | ICCAD 方法与实现章节 | 强 | 引言、相关工作 | 禁止“首次routing-aware/look-ahead” | evidence-backed |
| 本文联合搜索门位、跨层驻留和回迁存储位 | native ABI8实现与配置 | 强 | 方法 | 小空间是精确枚举，不应一律称GA | evidence-backed |
| 冲突图与DSATUR形成初始重排批次，候选在排序前完成物理可行性检验 | native backend说明、验证器协议 | 强 | 方法 | 单轨迹静态占用冲突应直接判为不可行；不得为基线附加原论文没有的后处理 | evidence-backed |
| GA-LK使用最多8层几何衰减物理前瞻 | M4配置与native说明 | 强 | 方法 | 当前层序滚动预测为有界近似，不是精确全局未来 | evidence-backed |
| GA-NL暴露局部短视问题 | 当前结果与代表性idle轨迹 | 中 | 动机、讨论 | 不能用单例替代总体统计 | plausible-inference |
| GA-LK提高两种基线上的总体Fidelity几何均值，并在个别QMAP电路上取得较大增益 | 三种子主实验、共同有效集、逐电路分析；冻结证据另保留bootstrap输出 | 中 | 实验、讨论、结论 | 正文只呈现总体几何均值、四个典型电路和完整结果数，不呈现置信区间或胜/平/负 | evidence-backed |
| 启用与关闭有限视界整体配置的差异 | 共享$(\alpha,\rho,\epsilon)=(0.5,0.7,0.05)$及其余设置的三种子比较：正文报告改善23.08%；冻结证据保留原始比1.2308、95% CI [1.0903, 1.4221]与Holm $p=0.0044$ | 中 | 实验、讨论 | $H_{\max}=0$令可见未来层集合及相应驻留保护集合为空，同时保留当前边界约束和候选构造；正文不呈现置信区间或显著性检验，且不能将整体差异归因于单个未来代价分量 | evidence-backed |
| 遗传搜索相对同目标逐坐标确定性贪心及同预算局部精修带来较小总体收益 | H8下GA/greedy受控消融 | 中 | 消融、讨论 | 仅统计出现GA适用大边界的电路；隔离的是种群、交叉和变异，不是GA对A*的理论优势 | evidence-backed |
| GA-LK运行时间高于局部基线 | 全量质量运行与固定12电路严格计时 | 强 | 实验、局限 | M4中位23.109 s、M2中位0.131 s；配对时间比172.346。M1在`hwb8_113`三次验证失败 | evidence-backed |
