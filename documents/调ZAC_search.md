# 调ZAC\_search

## 这个 `ZAC` 类整体在做什么

`ZAC` 是一个量子电路编译器/调度器，目标是把输入的量子程序，转换成适合某种**中性原子计算\-存储架构**的执行方案。

它继承了几个 mixin：

- `Scheduler_mixin`：负责门调度

- `Placer_mixin`：负责量子比特放置

- `Router_mixin`：负责路由

- `Verifier_mixin`：负责验证结果

- `Animator`：负责可视化/动画

所以这个类本质上是一个**编译流水线总控器**。

---

## 初始化阶段 `init`

初始化里主要设置默认配置和结果容器：

- `dir`：输出目录，默认 `./result/`

- `n_q`：量子比特数

- `n_g`：两比特门数

- `architecture`：目标架构对象

- `result_json`：最终结果 JSON

- `runtime_analysis`：各阶段耗时统计

以及一堆策略开关：

- `to_verify`：是否做验证

- `trivial_placement`：是否使用简单放置

- `routing_strategy`：路由策略

- `scheduling_strategy`：调度策略

- `dynamic_placement`：是否动态中间放置

- `given_initial_mapping`：用户给定初始映射

- `has_dependency`：门之间是否考虑依赖

- `l2`：是否用 L2 距离模型

- `use_window`：是否使用窗口式路由

- `reuse`：是否做 qubit reuse

- `resyn`：是否重综合

- `common_1q`：特殊单比特门统计

---

## 配置相关方法

### `parse_setting(setting)`

从一个字典里读取配置，覆盖默认参数。

比如可以设置：

- 名称

- 输出目录

- 是否考虑依赖

- 路由策略

- 是否启用 verifier

- 窗口大小

- 是否 reuse

- 是否 resyn 等

它是“运行前配置入口”。

---

### `set_architecture_spec_path(path)`

把架构规格文件路径写进 `result_json`，方便最后输出。

---

### `set_initial_mapping(mapping)`

设置用户指定的初始映射。

注释里写了 `todo: check if the given mapping is valid`，说明这里**还没有校验映射合法性**。

---

### `print_setting()`

把当前配置打印出来，主要是调试和日志用途。

它会输出：

- 输出目录

- 调度策略

- 初始放置策略

- 中间放置策略

- reuse 情况

- 路由策略

- verifier 是否启用

---

### `set_architecture(arch)`

把目标架构对象保存下来。

---

## 输入程序解析 `set_program(benchmark)`

这是非常关键的一步：把输入电路解析成内部表示。

### 支持的输入格式

它根据文件后缀分三类：

#### 1）`.qasm` / `.qpy`

这是最完整的路径。

流程大概是：

1. 读入电路

    - `qasm`：`QuantumCircuit.from_qasm_str`

    - `qpy`：`qpy.load`

2. 如果 `resyn=True`，先用 Qiskit 重综合到基门集：

    - `cz`, `id`, `u2`, `u1`, `u3`

3. 遍历电路指令：

    - 遇到两比特门：记录成 `self.g_q`

    - 遇到单比特门：按“最近一个两比特门”建立依赖，记录到 `dict_g_1q_parent`

#### 关键内部结构

- `self.g_q`：两比特门列表，每个元素形如 `[q0, q1]`

- `self.dict_g_1q_parent`：记录某个两比特门之后挂着哪些单比特门

这意味着它不仅关心两比特门，也保留了单比特门的相对位置。

---

#### 2）`.txt`

看起来是一个“图实例”格式。

- 直接 `eval(f.read())` 得到 `g_q`

- 推断总 qubit 数

- 默认把每个 qubit 都挂一个 `h` 门作为初始单比特门

---

#### 3）`.json`

这是特殊格式，注释里说是给 `olsq-dpqa` 用的。

- 从 `layers` 里读出 gates

- 统一转成 `[min(q0,q1), max(q0,q1)]`

- 推断 qubit 数

---

### 解析结束后统一做的事

最后它会：

- `self.n_g = len(self.g_q)`：两比特门数量

- `self.g_s = tuple(['CRZ' for _ in range(self.n_g)])`

- 打印 qubit 数、两比特门数、单比特门数

---

## 核心入口 `solve(save_file=True)`

这是整个编译流程的总入口。

---

### 第一步：初始化中间结果容器

```Python
self.gate_scheduling = None
self.gate_scheduling_idx = None
self.gate_1q_scheduling = None
self.reuse_qubit = None
self.qubit_mapping = []
```

这些变量用于后续调度、放置、路由。

---

### 第二步：打印总信息和配置

输出：

- “这是一个适用于 neutral atom 架构的编译器”

- 当前配置

---

### 第三步：门调度 `self.scheduling()`

这是把原始两比特门安排到若干“时间层”里。

你可以理解成：

- 原始门序列：`g0, g1, g2, ...`

- 调度后：分成若干批次

    - 第 0 层能并行执行的门

    - 第 1 层能并行执行的门

    - \.\.\.

调度结果会影响后面的：

- reuse 计算

- 初始放置

- 中间放置

- 路由

---

### 第四步：计算 qubit reuse

如果 `self.reuse=True`：

```Python
self.collect_reuse_qubit()
```

否则：

```Python
self.reuse_qubit = [set() for i in range(len(self.gate_scheduling))]
```

意思是：如果不考虑 reuse，就每层都没有可复用 qubit。

---

### 第五步：初始放置 `self.place_qubit_initial()`

根据调度结果和架构，把逻辑 qubit 放到物理位置上。

然后记录耗时：

```Python
self.runtime_analysis["initial placement"]
```

---

### 第六步：中间放置 `self.place_qubit_intermedeiate()`

这里是“层与层之间”的动态调整。

如果 `dynamic_placement=True`，通常意味着会在执行过程中重新做某种匹配/调整，以减少后续路由开销。

然后记录：

```Python
self.runtime_analysis["intermediate placement"]
```

---

### 第七步：路由 `self.route_qubit()`

把需要相互作用但不在一起的 qubit 移动/交换到可执行位置，完成真正的执行路径构造。

然后记录：

```Python
self.runtime_analysis["routing"]
```

---

### 第八步：统计总耗时

```Python
self.runtime_analysis["total"] = time.time() - t_s
```

并打印总时间。

---

### 第九步：保存结果

如果 `save_file=True`：

- 写出代码级 JSON：

    - `./result/code/<name>_code.json`

- 写出时间统计 JSON：

    - `./result/time/<name>_time.json`

保存的是：

- `self.result_json`

- `self.runtime_analysis`

---

### 第十步：验证

如果 `self.to_verify=True`：

1. `verify_scheduling(self.gate_scheduling_idx)`

2. `verify_qubit_mapping(0)`

即检查：

- 调度是否合法

- 映射是否合法

---

### 第十一部：返回结果

最终返回：

```Python
return self.result_json
```

---

## 6\. `collect_reuse_qubit()` 在做什么

这个函数专门计算：**哪些 qubit 可以在两个 Rydberg stage 之间保持不动，复用位置**。

它的思路比较像“跨层匹配 \+ 复用优化”。

---

### 6\.1 初始化

- `self.reuse_qubit = []`

- 如果没有调度结果，直接返回

然后创建：

```Python
qubit_is_used = [[-1 for i in range(self.n_q)] for j in range(len(self.gate_scheduling))]
```

它表示：

- 每层里，每个 qubit 被哪个 gate 使用

---

### 6\.2 先处理第 0 层

把第一层每个 gate 里用到的 qubit 标记进去。

---

### 6\.3 逐层比较前一层和当前层

对于每一层 `i`：

#### 情况 A：当前 gate 的两个 qubit 在上一层属于同一个 gate

说明这对 qubit 已经天然成组，可以直接复用：

```Python
self.reuse_qubit[-1].add(gate[0])
self.reuse_qubit[-1].add(gate[1])
```

#### 情况 B：不在同一个旧 gate 中

就建立一个二分图匹配矩阵：

- 行：当前层 gate

- 列：上一层 gate

- 如果当前 gate 的某个 qubit 曾被上一层某个 gate 使用，就连边

然后用：

```Python
maximum_bipartite_matching
```

做最大匹配，尽量复用更多 qubit。

---

### 6\.4 更新 `extra_reuse_qubit`

这是一个统计“还需要额外处理多少复用”的计数器。

最后断言：

```Python
assert(extra_reuse_qubit >= 0)
```

说明算法预期复用统计不能变成负数。

---

### 6\.5 结果形式

最后：

- `self.reuse_qubit`：每层可复用 qubit 的集合

- `self.extra_reuse_qubit`：额外的复用开销计数

- 并补一个最后的空集合：

```Python
self.reuse_qubit.append(set())
```

---

## 你可以把整个流程理解成这条流水线

```Plain Text
读取配置
  ↓
解析电路
  ↓
门调度
  ↓
计算 reuse
  ↓
初始放置
  ↓
中间放置
  ↓
路由
  ↓
保存结果
  ↓
验证
```

---

## 这段代码的核心数据流

最重要的几个内部变量是：

- `self.g_q`：两比特门序列

- `self.g_s`：对应门的类型标签

- `self.gate_scheduling`：调度后的分层门序列

- `self.gate_scheduling_idx`：调度索引

- `self.reuse_qubit`：每层可复用 qubit

- `self.result_json`：最终编译结果

- `self.runtime_analysis`：阶段耗时

---

## 一句话总结

这个 `ZAC` 类做的事情就是：

> **把量子电路读进来，调度成多层，计算 qubit 复用，做初始放置和中间放置，再完成路由，最后输出可验证的编译结果。**
> 
> 

如果你愿意，我下一步可以继续帮你把这份代码画成一张**流程图**，或者逐个解释 `scheduling / place / route / verify` 这些 mixin 可能各自负责什么

