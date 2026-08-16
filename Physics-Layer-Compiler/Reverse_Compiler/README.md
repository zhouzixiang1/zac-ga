# ZAC Reverse Compiler（ZAC 逆向编译器）

将一份**硬件调度**（hardware schedule，JSON 指令序列）逆向重建回对应的
**物理比特（physical-qubit）量子线路**，并可导出为 Qiskit `QuantumCircuit`
或 Stim `Circuit`。

本包**完全独立于 ZAC**：它**不导入、不调用 ZAC 的任何函数**，只
**借鉴 ZAC 的输出格式与思想**。我们在 ZAC 最终输出的基础上做了一处关键扩展——
**单比特门携带旋转角度信息（`params`）**——因此任意角度旋转都能被精确还原。
包里自带一个**自包含的参考前向编码器** `encode_circuit`（物理线路 → 硬件调度），
用于演示指令如何构建并支撑不依赖 ZAC 的 round-trip 测试。

```
Compiler/
├── ZAC-main/                      # 原始 ZAC 编译器（保持不变）
└── zac_reverse_compiler/          # 本包
    ├── reverse_compiler/
    │   ├── circuit_ir.py          # 内部 IR：CircuitIR / CircuitOperation
    │   ├── state.py               # 比特/原子/位置 记账 + 目标解析器
    │   ├── reverse_compiler.py    # 调度回放引擎（核心）
    │   ├── exporters.py           # to_qiskit / to_stim
    │   ├── reference_encoder.py   # 自包含前向编码器：物理线路 -> 硬件调度（不含 ZAC 代码）
    │   └── cli.py                 # 命令行入口
    ├── tests/
    │   └── test_reverse_compiler.py
    ├── reverse_compiler_test.ipynb # Jupyter 形式的演示/测试
    ├── conftest.py
    └── README.md
```

---

## 1. 这个逆向编译器在做什么

ZAC 是一个针对**中性原子分区架构（zoned neutral-atom architecture）**的编译器。
它把一条物理比特线路编译成底层硬件调度，调度里包含：

- 原子搬运（atom movement / rearrangement）；
- 单比特门；
- 两比特门（Rydberg CZ）；
- 测量与重置；
- 时间戳 / 并行层。

**逆向编译器做的是相反的事**：读入这份硬件调度，按时间顺序"回放"，
把每条硬件指令翻译回它原本代表的量子操作，从而重建出物理比特线路。

### 1.1 核心语义（最关键的设计原则）

每个**物理比特绑定一个原子**。这条不变量贯穿整个回放过程：

| 硬件事件 | 对量子线路的影响 |
|---|---|
| 原子搬运 `rearrangeJob` / `MOVE` | **只**更新原子位置/分区，**不产生任何门** |
| 两个原子交换空间位置 | **不产生 `SWAP`**（位置交换 ≠ 量子态交换） |
| 显式量子 `swap` 指令 | 产生 `SWAP` 门 |
| `rydberg` 指令 | 在所涉及的物理比特间产生 `CZ` |

> 例子：把 q0、q1 各自的原子移进纠缠区、做一次 CZ、再移回原位，
> 逆向重建出来应当是**一条** `CZ q0 q1`，而不是任何搬运/SWAP 门。

为什么"位置交换不等于 SWAP"如此重要？因为 AOD 重排会频繁地把原子搬来搬去以
满足布局约束，这些搬运纯粹是硬件层的物流操作；只有比特之间**量子态**的真正
交换才是 `SWAP` 门。混淆二者会凭空多出大量错误的门。

---

## 2. ZAIR 调度格式（被消费的输入）

逆向编译器直接消费 ZAC 原生的指令类型。位置统一采用 ZAC 的编码
`[id, a, r, c]`，其中 `(a, r, c)` 是位置 `(array/SLM 编号, 行, 列)`。

| 指令 `type` | 含义 | 关键字段 |
|---|---|---|
| `init` | 初始原子布局 | `init_locs = [[id, a, r, c], ...]` |
| `1qGate` | 一层单比特门 | `gates = [{name, q}, ...]`、`locs` |
| `rydberg` | 一层两比特纠缠门（CZ） | `gates = [{id, q0, q1}, ...]`、`zone_id` |
| `rearrangeJob` | 原子搬运（仅物流） | `begin_locs`、`end_locs`、`insts` |

> **与原始 ZAC 的差异**：原始 ZAC 的 `1qGate` 只记录门的 **名字**（`h`、`u3`…），
> **不保存旋转角度**（原因见文末附录）。**我们的硬件格式在此扩展**：单比特门额外携带
> `params`（角度），即 `{"name": "rz", "q": 3, "params": [0.37]}`。因此任意角度旋转
> 都能被精确逆向。`params` 缺失只会发生在格式不合法时，由 §3.4 的 `strict` 兜底。

### 2.1 向后兼容的扩展指令

为了覆盖任务要求但当前 ZAC 主流程尚未产出的操作，逆向编译器额外支持一组
**向后兼容**的指令（原始 ZAC 不会发出它们，因此不冲突）：

`global_1qGate`（全局脉冲）、`measure`、`reset`、显式 `swap`、
`barrier` / `delay`、`loss` / `activate`，以及**按坐标/陷阱（trap）寻址**的门。

### 2.2 硬件指令如何构建

下面给出**我们的硬件指令集**的完整构建方式。每条指令都是一个 JSON 对象，整份
调度形如：

```json
{
  "name": "my_program",
  "architecture_spec_path": null,
  "instructions": [ <init>, <inst>, <inst>, ... ]
}
```

约定：位置一律写成 `[id, a, r, c]`，其中 `(a, r, c)` = `(array/SLM 编号, 行, 列)`；
`a` 选定哪些是存储区、哪些是纠缠区由你的架构自行定义（示例里 `a=0` 存储、`a=1` 纠缠）。

**(1) `init` —— 初始布局（必须是第一条）**

```json
{"type": "init", "init_locs": [[0, 0, 0, 0], [1, 0, 0, 1], [2, 0, 0, 2]]}
```

> 把原子 0/1/2 放在存储区。`id` 同时作为原子号；默认 `qubit i == atom i`。
> 可选 `"inactive": [..]` / `"lost": [..]` 在初始时标记不在场的原子。
> 也可在顶层加 `"qubit_to_atom": {"5": 0, "6": 1}` 指定非恒等的比特↔原子绑定。

**(2) `1qGate` —— 一层单比特门（携带角度）**

```json
{"type": "1qGate", "gates": [
    {"name": "h",  "q": 0},
    {"name": "rz", "q": 1, "params": [0.37]},
    {"name": "u3", "q": 2, "params": [0.1, 0.2, 0.3]}
]}
```

> `q` 是原子号。**含参门必须给 `params`**。门也可改用坐标寻址：
> `{"name": "h", "position": [0, 0, 2]}`。

**(3) `rydberg` —— 一层两比特纠缠门（CZ）**

```json
{"type": "rydberg", "zone_id": 0, "gates": [{"q0": 0, "q1": 1}, {"q0": 2, "q1": 3}]}
```

> 同一条指令里的多个门处于**同一并行层**。默认是 `CZ`，可加 `"name": "cx"` 改写。

**(4) `rearrangeJob` / `MOVE` —— 原子搬运（只更新位置，不产门）**

```json
{"type": "rearrangeJob", "aod_qubits": [0, 1],
 "begin_locs": [[0, 0, 0, 0], [1, 0, 0, 1]],
 "end_locs":   [[0, 1, 0, 0], [1, 1, 0, 1]]}
```

> 把原子 0/1 从存储区搬到纠缠区。`end_locs` 决定新位置/分区，比特↔原子绑定不变。

**(5) 扩展指令**

```json
{"type": "swap",    "qubits": [0, 1]}                          // 显式量子 SWAP
{"type": "measure", "qubits": [0, 1], "classical_bits": [0, 1]}
{"type": "reset",   "qubits": [0]}
{"type": "global_1qGate", "name": "h", "zone": 1}              // 全局脉冲（可按 zone/mask 过滤）
{"type": "loss",    "atoms": [2]}                              // 原子丢失/失活
{"type": "barrier", "qubits": [0, 1]}                          // 或 delay（含 "duration"）
```

**典型组合：带搬运的 CZ**（搬入 → 纠缠 → 搬出，逆向后只得到一条 `CZ`）：

```
rearrangeJob(存储 → 纠缠)
rydberg(CZ)
rearrangeJob(纠缠 → 存储)
```

> 这正是 `reverse_compiler/reference_encoder.py` 里 `encode_circuit()` 自动生成的
> 序列——它是一个**自包含**的"物理线路 → 硬件调度"参考编码器（借鉴 ZAC 思想、
> 不含 ZAC 代码），可直接拿来构造调度或做 round-trip。

---

## 3. 内部逻辑详解

### 3.1 状态记账（`state.py`）

回放期间维护任务要求的所有映射：

```python
qubit_to_atom:    dict[int, int]            # 物理比特 -> 原子
atom_to_qubit:    dict[int, int]            # 原子 -> 物理比特
atom_to_position: dict[int, Position]       # 原子 -> (a, r, c)
position_to_atom: dict[Position, int]       # (a, r, c) -> 原子
active_atoms:     set[int]                  # 仍然在场（未丢失/未屏蔽）的原子
trap_to_position: dict[int, Position]       # 可选：陷阱编号 -> 位置
```

`ReplayState` 同时提供一个**统一的目标解析器** `resolve_target()`，
把灵活的目标说明解析成"物理比特"：

| 目标写法 | 解析路径 |
|---|---|
| `42`（裸整数） | 原子 id → 比特 |
| `{"atom": 42}` | 原子 id → 比特 |
| `{"qubit": 7}` | 比特 id（经 `qubit_to_atom` 校验） |
| `{"position": [a,r,c]}` | 坐标 → 原子 → 比特 |
| `{"trap": 5}` | 陷阱 → 位置 → 原子 → 比特 |

解析失败（位置上没有原子、比特未绑定等）会抛出 `ResolutionError`，
**绝不静默猜测**。

### 3.2 回放引擎（`reverse_compiler.py`）

`ReverseCompiler.compile(zair)` 按时间顺序遍历 `instructions`，对每条指令
分派到对应的 handler：

- `init` → 建立原子位置、默认 `qubit==atom` 的恒等绑定（可由 `qubit_to_atom`
  字段覆盖），把所有原子标记为 active。
- `1qGate` → 逐个门解析目标、规范化门名、产出 `CircuitOperation`；
  同一层内同一比特出现两次会报冲突。
- `rydberg` → 每个 `{q0, q1}` 产出一条 `CZ`（可被 `name` 改写为 cx/swap）。
- `rearrangeJob` / `MOVE` → **只**调用 `apply_end_locs()` 更新位置；
  位置先整体清空再写入，使"两原子交换位置"不会在 `position_to_atom` 里瞬时撞车；
  **不调用** `advance_layer`，因此搬运在 IR 中完全不留痕迹。
- `swap` → 产出显式 `SWAP`。
- `measure` / `reset` / `barrier` / `delay` / `loss` / `activate` → 对应处理。
- `global_1qGate` → 见 §3.3。

每条会产生门的指令对应一个**并行层（layer）**，层号写入
`CircuitOperation.layer`，`begin_time` 写入 `timestamp`，供导出器还原并行性。

### 3.3 全局门的"作用掩码"

全局脉冲不是无脑地作用到所有比特，而是经过 `_global_atom_mask()` 计算
**真正被影响的原子**集合：

1. 起点：显式 `atoms`/`mask`，否则全体原子；
2. 若给了 `zone`，按位置的 array 编号过滤到该分区；
3. 扣除 `exclude` / `shelved` / `lost`（屏蔽、搁置、丢失）；
4. 默认 `active_only=True`，再与 `active_atoms` 求交。

这保证全局门尊重 active mask、分区、搁置、丢失等硬件信息。

### 3.4 门名规范化与角度处理

- 无参单比特门：`h→H, x→X, y→Y, z→Z, s→S, sdg→SDG, t→T, tdg→TDG`；
- 固定旋转：`sx→RX(π/2)`、`sxdg→RX(-π/2)`；
- 含参旋转：`rx/ry/rz/p/u1 → RX/RY/RZ`（取角度参数）；
- `u2/u3/u → U`（保留全部角度参数，供 Qiskit）；
- `id/i/delay`（无参）→ 不产门。

**缺角度的兜底**：我们的硬件格式要求含参门携带 `params`，正常情况下角度都在。
万一遇到 `rz`/`u3` 等需要角度却缺 `params` 的**非法输入**：
- `strict=True`（默认）→ 抛 `ReverseCompileError`，明确指出该门缺角度；
- `strict=False` → 记一条 warning 并丢弃该门。

### 3.5 内部 IR（`circuit_ir.py`）

```python
@dataclass
class CircuitOperation:
    name: str
    qubits: tuple[int, ...]
    params: tuple[float, ...] = ()
    classical_bits: tuple[int, ...] = ()
    timestamp: float | None = None
    layer: int | None = None
    metadata: dict = field(default_factory=dict)
```

`metadata` 保留可审计的硬件细节：原子 id、坐标、分区、来源指令、时间戳、并行层等。
`CircuitIR` 还提供：
- `without_timing()`：去掉 `BARRIER`/`DELAY` 等纯硬件操作（用于线路比较）；
- `signature()`：忽略元数据/时间的可比较签名（用于 round-trip 等价判断）。

### 3.6 导出器（`exporters.py`）

- **`to_qiskit`**：支持任意角度旋转；按时间顺序追加门，不引入多余依赖。
- **`to_stim`**：只接受 Clifford 操作；对精确 Clifford 旋转做化简
  （`RX(π)→X`、`RY(π)→Y`、`RZ(π)→Z`、`RZ(π/2)→S`、`RZ(-π/2)→S_DAG`、
  `RX(π/2)→SQRT_X` …）；遇到**非 Clifford** 旋转或 `T`/`U` 抛
  `StimExportError`，**绝不近似**。并行层在 Stim 中以 `TICK` 分隔，
  同层多个门处于同一 moment；同层同比特冲突会报错。

---

## 4. 使用方式

```python
import json
from reverse_compiler import (
  reverse_compile,
  to_qiskit,
  to_stim,
  plot_atom_operations,
  plot_circuit_structure,
)

zair = json.load(open("ZAC-main/result/zac/logic/code/iqp_code.json"))
ir  = reverse_compile(zair)        # CircuitIR（核心产物）
qc  = to_qiskit(ir)                # qiskit.QuantumCircuit（支持任意角度）
sc  = to_stim(ir)                  # stim.Circuit（仅 Clifford，否则抛异常）

# 可视化：硬件层（原子移动+门）和线路层（量子电路结构）
plot_atom_operations(zair, "atom_ops.png")
plot_circuit_structure(ir, "circuit_structure.png")
```

命令行：

```bash
python -m reverse_compiler.cli path/to/schedule_code.json --to qiskit
python -m reverse_compiler.cli path/to/schedule_code.json --to stim
python -m reverse_compiler.cli path/to/schedule_code.json --to ir --no-strict
python -m reverse_compiler.cli path/to/schedule_code.json --to ir \
  --viz-atom-ops atom_ops.png --viz-circuit circuit_structure.png
```

> 注：可视化依赖 `matplotlib`，仅在调用绘图函数或传入 `--viz-*` 参数时需要安装。

---

## 5. 测试套件及其意义

运行：

```bash
cd zac_reverse_compiler
python -m pytest tests/ -q          # 24 passed
```

也可逐格运行 `reverse_compiler_test.ipynb`（含线路绘制与 round-trip）。

下表逐项解释**每个测试在验证什么、为什么重要**：

### 基础语义（任务要求 1–9）

| # | 测试 | 验证的语义 / 意义 |
|---|---|---|
| 1 | `test_local_single_qubit_gates` | 单比特门按原子 id 正确解析为比特，门名规范化正确（`h→H`、`x→X`）。 |
| 2 | `test_global_gate_applies_to_all_active_atoms` | 全局脉冲默认作用于**所有 active 原子**，并标记 `global` 元数据。 |
| 2b | `test_global_gate_respects_zone_and_mask` | 全局脉冲按**分区（zone）过滤**，只命中该分区内的原子——验证作用掩码逻辑。 |
| 3 | `test_cz_without_movement` | `rydberg` 正确翻译为 `CZ`。 |
| 4 | `test_cz_with_movement_produces_single_cz` | **关键**：搬入→CZ→搬出只得到一条 `CZ`；搬运不产门，且 `qubit_to_atom` 不变、原子最终回到原位。 |
| 5 | `test_coordinate_targeted_gate` | 按**坐标寻址**的门能正确解析 位置→原子→比特。 |
| 5b | `test_missing_coordinate_raises` | 坐标上没有原子时抛 `ResolutionError`——**不静默猜测**。 |
| 6 | `test_parallel_cz_gates_same_layer` | 同一 `rydberg` 层的多条 CZ 共享 `layer`，Stim 中处于同一 moment（`num_ticks==0`）——并行性被保留。 |
| 6b | `test_conflicting_parallel_gates_detected` | 同层两门作用于同一比特时报冲突——并行层一致性检查。 |
| 7 | `test_position_exchange_emits_no_swap` | **关键**：两原子交换空间位置 → **0 个门**；`position_to_atom` 正确更新而比特绑定不变。 |
| 8 | `test_explicit_quantum_swap_emits_swap` | 只有显式量子 `swap` 才产生 `SWAP` 门——与 #7 形成对照。 |
| 9 | `test_measurement_and_reset` | `measure`/`reset` 正确重建，测量带经典比特，并能导出到 Qiskit。 |

### 导出与角度（任务要求 10–11）

| # | 测试 | 验证的语义 / 意义 |
|---|---|---|
| 10 | `test_arbitrary_rotations_in_qiskit` | Qiskit 导出支持**任意角度**旋转（`rz(0.37)`、`u3(...)`），角度精确保留。 |
| 10b | `test_missing_rotation_angle_strict_raises` | ZAIR 未记录角度时：`strict=True` 报错、`strict=False` 丢弃——如实反映 ZAC 不存角度这一限制。 |
| 11 | `test_stim_rejects_unsupported_rotation` | 非 Clifford 旋转 `RZ(0.37)` 在 Stim 导出时抛 `StimExportError`（但 Qiskit 正常）——**拒绝近似**。 |
| 11b | `test_stim_rejects_t_gate` | `T` 门（非 Clifford）同样被 Stim 拒绝。 |
| 11c | `test_stim_clifford_rotation_simplification` | 精确 Clifford 旋转被化简：`RX(π)→X`、`RY(π)→Y`、`RZ(π/2)→S`、`RZ(-π/2)→S_DAG`。 |

### 原子在场状态（任务要求 12）

| # | 测试 | 验证的语义 / 意义 |
|---|---|---|
| 12 | `test_lost_atoms_excluded_from_global_gate` | 被 `loss` 标记丢失的原子被**排除**在全局脉冲之外。 |
| 12b | `test_inactive_atom_at_init` | `init` 时标记 inactive 的原子不参与全局门。 |

### 额外健壮性 & Round-trip

| # | 测试 | 验证的语义 / 意义 |
|---|---|---|
| — | `test_explicit_qubit_to_atom_mapping` | 支持显式 `qubit_to_atom` 绑定（非恒等映射），门作用于正确的物理比特。 |
| — | `test_stim_parallel_layers_separated_by_tick` | 不同层之间在 Stim 中以 `TICK` 分隔（`num_ticks==1`）。 |
| ★ | `test_round_trip_self_contained` | **完整闭环（不依赖 ZAC）**：物理线路 → `encode_circuit` 参考编码 → 硬件调度 → 逆向编译 → 重建线路，去掉硬件层操作后与原线路**逐门一致，且任意角度旋转精确保留**。 |
| ★ | `test_round_trip_movement_emits_no_extra_gates` | 充满搬运的调度逆向后只剩逻辑门，绝不产生多余 `SWAP`。 |
| ★ | `test_round_trip_complex_circuit` | **更大规模闭环**：8 比特、20 个门，覆盖全部支持门（`h/x/y/z/rx/ry/rz/cz/cx/swap`），与搬运交织；重建后**逐门、逐比特、逐角度**精确一致，且搬运绝不泄漏出多余 `SWAP`。 |

### Round-trip 一致性如何判定（三级强度）

判断 round-trip 前后是否一致，**不是只比门数量**。本项目按强度分三级：

| 强度 | 方法 | 捕捉的差异 | 适用 |
|---|---|---|---|
| 语法（有序） | 逐门比较 `(name, qubits, params)` 列表 | 门名 / 比特 / 角度 / **顺序** | 参考编码器不重排门时（自包含 round-trip） |
| 集合 | 门**多重集**计数（含具体比特对） | 门丢失 / 多出 / 连接关系错误，**忽略顺序** | 经真实 ZAC 后门被重排/路由时 |
| 语义 | **酉矩阵等价** `Operator.equiv`（含全局相位）/ **Stim tableau 等价** | 整体酉变换，容忍任意对易重排 / 路由 | 酉矩阵 ≲ 12 比特；tableau 用于大规模纯 Clifford |

> 仅看“门数量”是最弱的判据（无法区分 `cz(0,1)` 与 `cz(0,5)`）。笔记本里的 ZAC
> round-trip 已升级到**语义级**：4 比特用酉矩阵 `Operator.equiv`，12 比特（纯
> Clifford H+CZ）用 **Stim stabilizer tableau** 等价——既精确又远比稠密酉矩阵省内存。

> 测试套件**完全不导入 ZAC**。round-trip 借助包内 `encode_circuit`（自包含参考编码器，
> 只借鉴 ZAC 思想、无 ZAC 代码），用 `without_timing()` 剔除时序填充后逐门比较，
> 体现"忽略硬件层操作"的要求。
>
> 此外，`reverse_compiler_test.ipynb` 笔记本里**额外保留**了经真实 ZAC 编译的
> round-trip 演示（仅笔记本演示用，会临时 import `zac`），与上面的自包含
> round-trip 互为对照。笔记本含两个 ZAC round-trip：
>
> - **4 比特**（H + CZ）：先比多重集，再用 `Operator.equiv` 做**酉矩阵等价**校验；
> - **12 比特、多层纠缠**（纯 Clifford）：先比多重集，再用 **Stim tableau 等价**校验。

---

## 6. 依赖

`python 3.11+`、`qiskit`、`stim`、`pytest`。

> 注：原仓库未安装 `stim`；运行 Stim 导出与相关测试前需 `pip install stim`。

---

## 附录：为什么原始 ZAC 不记录角度

ZAC 是一个**布局 / 路由 / 调度**编译器，它在解析 QASM 时就只保留了单比特门的
**名字和比特号**，丢弃了 `operation.params`（角度）。原因是对 ZAC 的优化目标而言
角度是冗余信息：

- **不影响布局/路由**：原子放置与搬运只取决于两比特门的连接关系；`rx(0.3)` 还是
  `rx(1.7)` 对搬运没有区别。
- **不影响调度**：单比特门只贡献固定执行时长，与角度无关。
- **保真度模型只用计数**：ZAC 的模拟器只按门的数量估算保真度，从不读角度。

也就是说，ZAIR 的定位是"硬件调度 + 保真度/动画产物"，不是可重新仿真的完整线路。
**本逆向编译器面向的是我们自己的硬件格式**——在 ZAC 输出基础上让单比特门携带
`params`——从而把任意角度旋转也纳入可精确逆向的范围。
