# ZAC 四方法 Schema 2 实验

本目录比较四种实际编译方法：M1 原始 ZAC、M2 ICCAD/QMAP 3.2
routing-aware A*、M3 无前瞻遗传驻留、M4 几何衰减多层前瞻遗传驻留。
本轮只处理 ZAC18、QMAP154，不运行 Large、不做图。

历史工作簿和旧 688 次结果已在 `legacy_pilot_inventory.json` 中标记为
`legacy/pilot`，只用于回归和故障定位，不能进入新主表。

## 方法边界

- M1/M2 实际运行论文原始方法；统一评分器会审计 ghost，但不会给基线增加论文
  中没有的 ghost repair。
- M3/M4 的完整决策内核统一为 C++17 resident engine；Python 只保留 QASM、
  编译状态、原生调用、最终路由和独立验证。M3 的 `max_horizon=0`，不能读取未来层；
  M4 最多看 8 层，未来物理增量按
  `alpha_lookahead * rho ** (depth - 1)` 衰减，裸衰减低于 0.05 时停止。
- 每个候选在排序前完成实际 RETURN 子集的 K-best 位置匹配、确定性 RESEAT、
  `back -> out` 两相重放、ghost 硬检查、分批和物理重新计分。winner 之后禁止
  再发生改变结果的修复。
- 直接搜索空间不超过 512 时精确枚举；更大空间使用唯一评价预算受控的 GA，
  包含分区交叉、冲突簇联合变异、多样性保持和最多两轮局部精修。

## 当前调参协议

正式协议是 `resident-ga-quality-racing-v3`，入口为
`experiments_v2.quality_racing_cli`。旧的 18→6→固定 270 次 validation 代码只
保留为历史 ledger 读取器，不再生成本轮配置。

质量调参串行执行：

1. 先实际运行 30 个开发/验证电路的 M1/M2，得到每电路最强基线。
2. M3、M4 分别比较 5 个搜索预算档位；每完成 5 个开发电路，质量中位数比
   领先者差超过 0.005 且 Move 也无优势的配置立即淘汰。
3. 每方法前 2 个搜索档位做单因素决策扩展：容量、RETURN 候选数、K-best
   匹配数、交叉率和局部精修轮数。
4. M4 的前 2 个决策配置再测试 `alpha={0.10,0.20,0.35}` 与
   `rho={0.50,0.70}` 的 6 个组合。
5. 每方法前 2 个配置在独立 15 电路验证集跑 seeds 0/1/2。质量差距不超过
   0.002 时优先逐层放置时间更短者。
6. 主表使用 M3/M4 各自最优配置；另由 M4 最优配置构造只差 horizon 的共享
   参数对，在全部 30 个开发/验证电路上检查前瞻贡献。共享检查不进入主表。

固定电路和搜索空间见
`exp_setting/native_ga_v1/split_manifest.json` 与
`exp_setting/native_ga_v1/tuning_space.json`。

在仓库内 `ZAC_zzx` 目录的执行顺序：

```bash
PLAN=experiments_v2/experiment_plan_v2.json
ROOT=../../artifacts/native-ga-v1/tuning-quality-v1

python -m experiments_v2.quality_racing_cli --plan "$PLAN" --root "$ROOT" prepare
python -m experiments_v2.quality_racing_cli --plan "$PLAN" --root "$ROOT" baselines
python -m experiments_v2.quality_racing_cli --plan "$PLAN" --root "$ROOT" profiles
python -m experiments_v2.quality_racing_cli --plan "$PLAN" --root "$ROOT" decisions
python -m experiments_v2.quality_racing_cli --plan "$PLAN" --root "$ROOT" lookahead
python -m experiments_v2.quality_racing_cli --plan "$PLAN" --root "$ROOT" validation
python -m experiments_v2.quality_racing_cli --plan "$PLAN" --root "$ROOT" shared-forward-check
python -m experiments_v2.quality_racing_cli --plan "$PLAN" --root "$ROOT" freeze-selection
```

runner 没有 worker 或并发模式。每次尝试写入确定性 receipt，resume 只接受该
receipt 绑定的 manifest 和 SHA256，不扫描旧目录推断成功。

## 正式实验

选择配置写回并形成 clean commit 后：

- M1/M2 在 ZAC18、QMAP154 各实际运行一次质量结果；
- M3/M4 各跑 seeds 0/1/2，逐电路取中位数；
- 四方法逐层放置时间 warm-up 后各重复 3 次，报告中位数和 IQR；
- 所有 success 必须账本一致；M3/M4 还必须 `ghost_hits=0` 且无 fallback。

最终工作簿只交付两张表：`ZAC18`、`QMAP154`。每行一个电路，方法按
M1/M2/M3/M4 横向分组；逐层放置时间直接保留在各方法列中。同步输出
`zac18.csv`、`qmap154.csv`、`experiment_summary.md` 和
`final_manifest.json`。

统一评分器在 log 域计算 ZAC 物理保真度，并由事件重放器统一计算 Move 批次
和 Move 时间。线性退相干模型 OOD 时不伪造数值，统一切换到指数敏感性结果
进行同一调参 cohort 的比较。
