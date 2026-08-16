# ZAC_zzx 实验全程：按顺序的构建与实验记录（含代码差异对照）

> 阅读约定：所有关键改动都用【ZAC】与【ZAC_zzx】两块代码对照给出，
> **⚠ 标出的行就是两者的实质差异**。三个改进目标（按 2q 拆批次 /
> 可选不放回 / 图着色引导放置）在过程中依次落地，也依次暴露问题。
> 总成绩：18 电路 geomean = ZAC 的 **0.867**（ZAC_new 此前最佳 0.959），
> 验证器 8 查全绿，编译全套 237s。

---

## 第 0 步：搭骨架，先立回归基线（M0）

**做了什么**：拷贝 ZAC_new 为同级文件夹 `ZAC_zzx/`（自带 `zac/` 字节副本、
benchmark、hardware_spec），包改名 `zzx`，配置键白名单化。

**验收**：`placer="zac"` 回归模式与 ZAC_new 现场重跑的指令流完全一致
（toy 电路 duration/保真度逐位相同；`diff -r` 证 `zac/` 源码零差异）。

```
改动的代价为零，收益是后面对照实验的控制变量：任何结果差异都只能来自
新代码，不来自环境或副本漂移。
```

---

## 第 1 步：驻留核心 resident.py（M1）——"不放回"的大脑

### 1.1 下次使用表（按 2q 门拆批次的底座）

轮次体系 = ZAC 的 ASAP 2q 门层（每层互不共用比特、每轮一次 rydberg）。
ZAC_zzx 在其上加一张**下次使用表**——ASAP 调度静态可知，零运行时开销：

```python
class NextUse:
    def __init__(self, gate_scheduling: list):
        self.rounds: dict[int, list[int]] = {}
        self.partner: dict[tuple[int, int], int] = {}
        for layer, gates in enumerate(gate_scheduling):
            for q0, q1 in gates:
                self.rounds.setdefault(q0, []).append(layer)
                self.rounds.setdefault(q1, []).append(layer)
                self.partner[(q0, layer)] = q1
                self.partner[(q1, layer)] = q0

    def next_round(self, q: int, after: int):
        """r(q)：严格晚于 after 的首个参与轮次；无则 None（死驻留者）。"""
```

**⚠ 与 ZAC 的差异**：ZAC 的复用判定（`zac.py:256-305`）只在**相邻两层**
之间做二分图匹配——"下一层马上用"才留；ZAC_zzx 对每个原子知道**任意
远的下次使用轮次与搭档**，这是跨轮驻留决策的情报来源。

### 1.2 驻留登记簿

```python
class ResidentRegistry:
    def __init__(self, architecture, initial_mapping, theta_capacity=0.9):
        self.zone_seat: dict[int, tuple] = {}     # q -> (slm, r, c) 当前激发区座位
        self.storage_site: dict[int, tuple] = {}  # q -> 当前存储位
        self.zone_sites = sum(slm.n_r * slm.n_c        # 区内总座位（容量）
                              for slm in ... if slm.entanglement_id != -1)

    def anchor(self, q, after, next_use):
        """下次使用锚点：搭档在区内→其最近存储投影；在存储→原位。"""
```

### 1.3 惰性决策：默认全留，两类强制回

```python
def decide_lazy(registry, next_use, layer, next_gates, next_gate_seats, ...):
    participants = {q for gate in next_gates for q in gate}
    needed = {seat for pair in next_gate_seats for seat in pair}

    # E2 挡路逐出：非参与者的座位被下一轮门位需要
    forced = [q for q, seat in registry.zone_seat.items()
              if q not in participants and seat in needed]

    # 容量阀：保留座位 + 2×门数 > θ·容量时，按下次使用轮次降序逐出（死驻留者最先）
    evict_order = sorted(
        (q for q in registry.zone_seat if q not in participants and q not in forced_set),
        key=lambda q: (next_use.next_round(q, layer) is None,
                       next_use.next_round(q, layer) or 0), reverse=True)
    ...
```

**⚠ 与 ZAC 的差异（本项目的第一核心）**——ZAC 的回位是**无条件全员**：

```python
# 【ZAC】vmplacer.py:343-348 —— 激发区原子一律放回存储
for q, mapping in enumerate(last_gate_mapping):
    array_id = mapping[0]
    if array_id in is_empty_storage_site:
        is_empty_storage_site[array_id][mapping[1]][mapping[2]] = False
    elif (not test_reuse) or (q not in self.list_reuse_qubit[layer]):
        qubit_to_place.append(q)          # ⚠ 不在"下一层复用"名单 → 强制回宿舍
```

```python
# 【ZAC_zzx】decide_lazy —— 默认 STAY（0 条搬运腿），只有 E2/容量才 RETURN
decisions = {}
for q, seat in list(registry.zone_seat.items()):
    if q in sites:
        decisions[q] = ("RETURN", sites[q])   # 少数被逐出者
        registry.return_to_storage(q, sites[q])
    else:
        decisions[q] = ("STAY", seat)          # ⚠ 默认原地闲放
```

### 1.4 RETURN 落位：三方案箱式匹配（笔记 :123-131）

回存储不是"回原位"那么简单——原位可能被占。三族候选箱（原位/就近/伙伴）
∪ 自由位过滤 → 最小权完美匹配：

```python
def match_return_sites(registry, returners, next_use, after, ...):
    # C1 原位族 / C2 就近族 / C3 伙伴族 —— 每族扩成 (2·ratio+1)² 自由位箱
    #（防塌缩：ZAC 的 nearest_storage_site 每半行每列只回一个位，同列驻留者会撞）
    families = [registry.homes[q], near_current, anchor_loc]
    ...
    cost = sqrt(d(激发区座位→候选位)) + alpha * sqrt(d(候选位→锚点))  # 省 now + 省 future
    # scipy 最小权完美匹配；失败贪心兜底
```

**单测**：30/30 全绿（下次使用表/登记簿/E2/容量阀/三方案/边界腿格式）。

---

## 第 2 步：接进 ZAC 流水线（M2）——第一次端到端

### 2.1 放置器轮循环：整个覆写父类 run()

```python
# 【ZAC_zzx】ResidentPlacer.run —— 顺序与原生编译器同构：先定门位，闲人让路
placement = self._plan_round(0)
self._commit_round(0, placement)
for layer in range(n):
    if layer + 1 < n:
        placement = self._plan_round(layer + 1)   # 门赢：先放下一轮门位
        next_seats = [p["seats"] for p in placement]
    decisions, stats = decide_lazy(...)            # ⚠ 边界决策（默认全留）
    self._append_boundary(decisions)               # mapping[2L+2]：留座者不动
    self._commit_round(layer + 1, placement)       # mapping[2L+3]：参与者落门位
self._assert_contract()   # ⚠ 流契约断言：长度 2n+1 / 拷贝不变式 / 每张映射单射
```

**⚠ 与 ZAC 的差异**：ZAC 的 `VertexMatchingPlacer.run`（vmplacer.py:19-62）
每轮先调 `place_qubit` 生成**回存储**的边界映射，再放下轮门位——复用两世界
`filter_mapping` 只看一个边界。ZAC_zzx 的边界映射里**留座者逐位不动**，
下游路由见 2.2 自然不搬他们。

**首战**：toy 电路 **0.596**（比 ZAC 快 40%，保真度 0.887→0.916），
决策账本符合设计：第 1 层门只搬 3 个原子（其余全在车上），末边界零回撤。

### 2.2 路由四改（zzx/zac_zzx.py `_route_resident`）

```python
# 【ZAC】router.py:56-61 —— 只看本轮门原子；断言一端必在存储
remain_graph = []
for gate in self.gate_scheduling[layer]:
    for q in gate:
        if initial_mapping[q] != gate_mapping[q]:
            assert(initial_mapping[q][0] == 0 or gate_mapping[q][0] == 0)  # ⚠
            remain_graph.append(q)
```

```python
# 【ZAC_zzx】_route_resident —— 三处差异
# ① out 相：断言放宽为"两端均为合法 SLM 位"（区内换座 zone→zone 合法化）
assert self.architecture.is_valid_SLM_position(*initial_mapping[q])
assert self.architecture.is_valid_SLM_position(*gate_mapping[q])

# ② back 相 remain_graph 扩为"全部映射增量者"（闲住驻留者的回撤腿也要发车）
remain_back = [q for q in range(len(gate_mapping))
               if gate_mapping[q] != final_mapping[q]]        # ⚠ 不再只扫门原子

# ③ 依赖账本补丁：非参与者的 qubit_dependency 可能停在数轮之前——
#    不补会与 rydberg/1q 并行执行（审计实证的洞）
last_gate_inst = len(self.result_json["instructions"]) - 1
for q in remain_back:
    if q not in participants:
        self.qubit_dependency[q] = last_gate_inst             # ⚠ 压到本轮门指令后
```

---

## 第 3 步：真电路第一课——E2 逐出风暴

**现象**：ghz_n23 ratio 1.232，决策账本显示 22 个边界**逐出 21 次**。

**机理**：链式电路里新门锚点天然落在上一对座位附近，匹配选中被占座位 →
E2 逐出 → 每轮"逐出一个 + 换座一个 + 接一个"。**先撞再逐**等于每轮多付
逐出+回接两腿。

**修复**：菜单生成时就硬排除非参与者驻留者的座位（门绕开闲人）：

```python
# 【ZAC_zzx】_plan_round —— 硬排除（ZAC 的 place_gate 候选窗无任何占用过滤 ⚠）
blocked_g = {seat for q, seat in reg.zone_seat.items()
             if q != q1 and q != q2}
opts = self._build_opts(set_sites, q1, q2, blocked_g, pin_base)
#  _build_opts 内：if blocked and (s1 in blocked or s2 in blocked): continue
```

**结果**：E2 21→0，ghz 批数 64→43（优于 ZAC 的 44），但 ratio 仍 1.10——
引出第 4 步。

---

## 第 4 步：漂移与钉扎的拉锯——解析规则无解的实证

逐轮解剖 ghz（每轮作业时长）发现：**结对座位逐轮漂移**——匹配把门位放在
"驻留者与新来者"的折中点，驻留者每轮区内长走 ~150μs。

**尝试三档解析策略**（4 电路冒烟 geomean）：

| 策略 | ghz | bv | ising | qft | geomean | 病根 |
|---|---|---|---|---|---|---|
| 不钉扎 | 1.118 | 1.137 | 0.921 | 1.341 | 1.115 | 门位漂移，驻留者反复长走 |
| K2 钉扎窗 | 1.533 | 1.372 | 0.815 | 0.763 | 1.095 | 座位锚死，新原子家门逐轮走远：入区腿 157→250μs/轮 线性上涨 |
| 钉扎+偏移罚 | 1.533 | 1.372 | 0.815 | 1.198 | 1.197 | qft 的多驻留门需要融合 |

```python
# 【ZAC_zzx】K2 钉扎窗（ZAC place_gate 无此概念 ⚠）：有驻留者参与的门，
# 菜单塌缩到其座位对 ± pin_radius 列——驻留者一步不走，新来者跑全程
resident_seats = [reg.zone_seat[q] for q in (q1, q2) if reg.is_resident(q)]
if resident_seats:
    for seat in resident_seats:
        base = self._norm_left(seat)
        for dc in range(-self.pin_radius, self.pin_radius + 1):
            ...  # (base[0], base[1], base[2]+dc)
```

**结论（本步最重要的产出）**：链式与并行电路要的**相反**，解析规则
顾此失彼——"钉不钉"本质是逐门决策，必须交给搜索。这就是第 6 步 GA 层
存在的直接证据。

---

## 第 5 步：座位双订连环案——验证器驱动的三轮追凶

验证器（第 7 步详述）在宽层电路连续抓出真冲突，剥了四层才到根：

### 5.1 scipy 匹配的"full"只保小侧全覆盖

钉扎窗让 sites < gates 时有门未被匹配，`chosen[col]` 直接 KeyError：

```python
# 【ZAC_zzx】_match_gates —— 验证全覆盖，否则贪心兜底（ZAC 菜单恒大，无此坑 ⚠）
if len(chosen) == n_cols:
    return [...]
# 落入 greedy()
```

### 5.2 食堂顺延在耗尽的菜单上原地打转

ising 宽层两个门分到同一 site（顺延循环走完没 break，停在占位上）：

```python
# 【ZAC_zzx】修复：菜单预扩容到至少"门数"个选项（鸽笼保证顺延必有空位）
if len(opts) < len(list_gate):
    extra = sorted(..._all_zone_sites() | ..._expanded_sites(...) - seen, key=权重)
    for site in extra: ...  # 保持硬排除地补足
```

### 5.3 根：ZAC 的 site 账本只记"离开"、从不记"到达"

```python
# 【ZAC】router.py process_movement_layer（节选）——账本的天生盲区
set_site_dependency.add(self.site_dependency[final_mapping[q]])  # 只"读"终点
self.site_dependency[initial_mapping[q]] = inst_idx              # ⚠ 只"写"起点
```

原版无害：每轮全员往返，任何"到达"都在同轮内配对"离开"，盲窗 ≤ 一轮。
**驻留一开**：原子到达后一直坐，账本里它的座位指向陈旧的"离开"——新原子
落到同座，账本查不到约束 → **真双占**（ising 修复前 8 处、qft 58 处，
两原子同座几百微秒）。更深一层：就算补记"到达"也没用——同相位里
"驻留者离开"的指令可能后于"新原子到达"发射，账本只能指向先发射者，
**原理上表达不了反向约束**。

**修复（构造性禁绝，不修补账本）**——三道闸：

```python
# 【ZAC_zzx】闸 1：菜单硬排除扩大到"本轮其他参与者"的座位（同相位交接从构造上消失）
blocked_g = {seat for q, seat in reg.zone_seat.items() if q != q1 and q != q2}

# 【ZAC_zzx】闸 2：_pair_seats 驻留者保座——配对含自己座位时不动、搭档去另一座
# （堵"同门对座交换"变体：列序朝向会把搭档安排到驻留者旧座上）
for q, other, mine_first in ((q1, q2, True), (q2, q1, False)):
    if reg.is_resident(q) and reg.zone_seat[q] in (a, b):
        mine = reg.zone_seat[q]
        return (mine, b if mine == a else a) if mine_first else (b if mine == a else a, mine)

# 【ZAC_zzx】闸 3：_repair_placements 终检安全网——选完后按不变式复查，
# 违例即改选全区过滤域最优工位（正确性不再依赖任何菜单层级的表现）
if s1 not in others and s2 not in others and p["site"] not in (used - {p["site"]}):
    out.append(p); continue          # 合法
... 否则全区挑最优未用工位改选
```

**结果**：A1 与 A3 全部输出通过 8 查（含座位独占时间线）。

---

## 第 6 步：GA 决策层（M3）——门位求解如何"看见"运送批次

回答一个关键问题：**先定下一轮门位时，考虑运送批次数量了吗？** 考虑了，
而且不是"先放好座位再数批数"——**批数本身长在搜索的目标函数里**。
GA 每试一套座位方案，当场把这套方案诱发的搬运腿建成冲突图着色一遍，
"要发几趟车"直接决定这套方案的生死。下面按求解闭环逐步展开。

### 6.1 求解闭环总览（每轮跑一遍）

```
① 建菜单（每步一次，常量缓存）
     每门候选 = 钉扎窗(驻留者座位±pin_radius列) ∪ 锚点展开窗(容量自动扩窗)
     硬排除其他原子座位；预扩容到≥门数个选项（食堂顺延的鸽笼保证）
② 构造联合染色体
     染色体 = [每门菜单下标基因…] ++ [有后续使用的非参与者 0=STAY/1=RETURN 基因…]
③ 解码（每评估一次都跑）
     门位基因 → 食堂顺延 → 每门具体座位对（被占就沿菜单找空位）
     决策基因 → 边界决策（STAY 原座 / RETURN 用三方案匹配的预定位）
④ 推腿集（查表，O(门数)）
     去程腿 O：每个门两原子"当前位置→指派座位"中距离>0 者
     回撤腿 B：选 RETURN 的原子"座位→匹配存储位"的腿
⑤ 两张冲突图、各自着色（每评估一次都跑）
     图 O 的节点=去程腿，图 B 的节点=回撤腿；边 = compatible_2d 不成立
     各自 DSATUR 启发式着色 → χ + 色类 → 每类最长腿
⑥ 适应度
     F = 1.57×(χB + χO) + Σ√dmax(B) + Σ√dmax(O) + γ0^轮距·Σ√d(位置→下次锚点)
⑦ GA 择优迭代
     种群6 × 迭代8 × 邻域采样24 ≈ 1158 次评估/轮，精英保留，zeros 热启动
     （zeros = 每门选菜单第一个 = 权重最优的解析解，GA 从不比解析差）
```

### 6.2 真实代码（摘自 `zzx/zplacer.py _ga_step`）

**解码器**——染色体到座位表，冲突靠顺延消解：

```python
def decode(chrom):
    used = set()
    placed = []
    for col, opts in enumerate(candidates):        # 每门一个候选菜单
        idx = chrom[col] % len(opts)
        for step in range(len(opts)):              # 食堂顺延：被占就沿菜单找空位
            cand = opts[(idx + step) % len(opts)]
            if cand[0] not in used:
                break
        used.add(cand[0])
        placed.append(cand)
    return placed
```

**适应度**——与 ZAC 纯距离匹配权重的本质差异 ⚠：

```python
# 【ZAC】vmplacer.py:267-270（对照）：w = sqrt(d1) + sqrt(d2)，批次不可见
# 【ZAC_zzx】fitness：批数通过着色直接进目标
def fitness(chrom):
    placed = decode(chrom)
    legs_out, extra = [], 0.0
    for col, cand in enumerate(placed):
        e = gate_cache[col][cand[0]]       # (门,工位) → 两原子的去程腿 + 锚点项（缓存）
        legs_out.extend(e["legs"]); extra += e["anchor"]
    legs_back = []
    for i, q in enumerate(eligible):
        e = dec_cache[q][chrom[n_gates + i]]   # (原子,STAY/RETURN) → 回撤腿 + 锚点项（缓存）
        legs_back.extend(e[0]); extra += e[1]
    if self.fitness_mode == "lumped":          # A3' 消融档：单图混合（跨相合色=物理不存在的省钱）
        return batch_cost(legs_back + legs_out, w_batch=self.w_batch)[0] + extra
    cost_b = batch_cost(legs_back, w_batch=self.w_batch)[0]   # ⚠ χ(回撤相)+Σ√dmax
    cost_o = batch_cost(legs_out,  w_batch=self.w_batch)[0]   # ⚠ χ(去程相)+Σ√dmax
    return cost_b + cost_o + extra
```

`batch_cost`（`zcost.py`）内部就是"建冲突图 → DSATUR → 色数与色类"：

```python
def batch_cost(legs, w_batch=1.0, ...):
    adj = conflict_graph(legs)                       # O(n²) 两两 compatible_2d
    n_batches, batches, _ = color_batches(legs)      # DSATUR：饱和度→度数→距离
    time_cost = sum(sqrt(max(腿长) for 批内))          # 每批时间 = √批内最长腿
    return w_batch * n_batches + time_cost, n_batches, n_conflicts
```

**类型感知邻域**——异质基因各用各的算子（原 swap 算子跨类型几乎全空转，
审计实测）：门位基因 ±1 菜单下标或随机重抽；决策基因 0↔1 翻转：

```python
def neighbor(chrom):
    m = list(chrom)
    if n_gates and (not eligible or self.rng.random() < 0.5):
        gi = self.rng.randrange(n_gates)
        m[gi] = self.rng.choice([m[gi] + 1, m[gi] - 1,
                                 self.rng.randrange(len(candidates[gi]))])
    else:
        di = self.rng.randrange(len(eligible))
        m[n_gates + di] ^= 1
    return m
```

**缓存机制**（编译时间的胜负手）：菜单、锚点、RETURN 落位**每步只算一次**
当常量；`gate_cache[(门,工位)]` 与 `dec_cache[(原子,选项)]` 预存每个基因取值
对应的腿与锚点贡献——fitness 只剩查表 + 两次小图 DSATUR。全套 18 电路
编译 237s，反而快于 ZAC_new-A 的 272s。

### 6.3 两分钟例子：一个"更近但要多开一趟车"的抉择

两个门，A 门要搬原子 a（存储第 2 列），B 门要搬原子 b（存储第 4 列）：

| 方案 | a 的腿 | b 的腿 | compatible_2d 判定 | χ | 适应度账 |
|---|---|---|---|---|---|
| 座位顺手选 | 2 列→5 列 | 4 列→3 列 | 2<4 但 5>3：**路线交叉** | 2 批 | 2×1.57 + 两批的 √dmax |
| GA 换 B 的座位 | 2 列→5 列 | 4 列→6 列 | 次序保持：兼容 | **1 批** | **1×1.57 + 一批的 √dmax** |

纯距离匹配（ZAC 的 place_gate）会选第一种——b 去第 3 列明明更近！但
"交叉要多开一趟车"是**两个门决策之间的二次交互**：匹配的边权一次只看
一条边，表达不了"门 A 选了这个工位、门 B 选了那个工位、合起来多一批"。
GA 每次评估**整套**方案，着色把批数后果算出来，第二种胜出。ZAC_new 的
罚单引擎（B 引擎）用"每门×每工位的冲突条数"做匹配边上的近似罚，zzx 的
GA 则是每轮 1158 次真实着色精确计价——从"罚单近似"升级到"GA+着色求解"。

### 6.4 搜索与执行的分工

| | 搜索信号（放置 GA 内） | 实际执行（路由内） |
|---|---|---|
| 问的问题 | "这套座位**会**要几批？" | "这批腿**实际**怎么分车？" |
| 着色精度 | 启发式 DSATUR（毫秒级，×1158 次/轮） | 小图（≤24 腿）升级精确分支限界 |
| 相位结构 | 去程/回撤两张图分开算 | 同样分相位发车 |

ZAC_new 的对账纪律（预演 χ == 实发批数，689/689 层）在 zzx 延续：放置
算的批数就是路由发的批数，搜索优化的目标与实际付的账单是同一本账。

### 6.5 诚实边界

- **χ 只在轮内起作用，不跨轮**：上一轮的座位选择影响下一轮的腿型，这
  部分只靠锚点前瞻（γ 项）软性引导——而实测 γ 项不赚反亏（第 8.2 节），
  所以"跨轮批次联动"目前是缺的。真正要做是 rollout（把下一个换位近似
  推演一遍再计价，菜单冻结以避开循环依赖），v1.1 实验项；
- GA 每轮独立求解（轮间只通过登记簿状态耦合），不是全局搜索——种子
  方差（qft_n29 极差 0.079）就是这一点的代价。

**结果**（冒烟 4 电路）：A1（解析）geomean 1.139 → **A3（GA）0.900**——
第 4 步预言的"必须搜索"兑现：ising 0.647、qft_n18 0.799、ghz 1.533→1.107。
批数塌缩是这套闭环的直接产出：ising_n42 22→11、qft_n29 401→217、
knn 161→88（全部相对 ZAC 原版）。

---

## 第 7 步：8 查验证器（M4）——正确性的独立证据链

`verify_batches.py` 把落盘的 ZAIR 指令流当"别人交来的作业"逐条回放：

| 查 | 内容 | 关键实现点（调试中踩出来的） |
|---|---|---|
| ① | 批内两两 compatible_2d | rydberg 门字段是 **q0/q1**（按 q/qubits 解析会整体空转） |
| ② | 位置连续性（反幽灵传送） | 从 init_locs 全程追踪 |
| ③ | 时序依赖 | site 依赖镜像 router 的 **drop-after-pickup**（座位在"拿起开始"即释放，非拿起完成） |
| ④ | 门执行邻接（成对工位） | 同 ① 的字段修复后才真正生效 |
| ⑤ | **座位独占时间线** | **同一时刻的事件整批结算**——ZAC 两段式搬运的零驻留中转（放下与再拿起同一微秒）曾使逐事件处理在 8 个原版电路上全部误报 |
| ⑥ | 1qGate locs 与追踪一致 | 旧版直接覆写不校验 |
| ⑦ | **门账本对账** | 与 code JSON 内嵌的**重综合后门列表**对账；直读 QASM 会"缺门"——ZAC 真值 4 电路同差异实证，差异在共享重综合层，非编译 bug |
| ⑧ | 门-搬运互斥（原子粒度） | 原子自己的门没做完不能被搬；曾按"整块串行"实现，ZAC 原版合法流被误报后修正为原子粒度 |

**验收**：ZAC 原版 18/18 全过（证校验器本身正确）；7 类损坏注入全部抓到
（搬到已占座/幽灵传送/无视依赖/错位/丢门/1q 造假/门未完先搬）；
ZAC_zzx 全部输出（主配置+消融+种子）全绿。

---

## 第 8 步：18 电路总实验（M5）

### 8.1 主表（分母 = 冻结 ZAC 真值）

| 系统 | geomean | 编译全套 |
|---|---|---|
| ZAC 真值 | 1.000 | — |
| ZAC_new-B（同会话重跑） | 0.959 | — |
| ZAC_new-A（同会话重跑） | 0.947 | 272s |
| A1（驻留+解析匹配） | 1.081 | — |
| **ZAC_zzx（驻留+GA）** | **0.867** | **237s** |

分电路代表（全表见 results/comparison_table.md）：swap **0.561**、seca
**0.570**、knn **0.631**（批 161→88）、ising_n42 **0.647**、multiply 0.650、
qft_n18 **0.799**、**qft_n29 0.777（根治 ZAC_new 的 1.077 回退，批 401→217）**；
ising_n98 0.870 vs B 0.661（超宽并行层 B 的着色放置仍占优，互补保留）；
bv/cat/ghz/wstate 链式族 1.0-1.15（几何下限，见 8.3）。

### 8.2 三个反直觉发现（诚实记录）

1. **GA 学到"永不回撤"**：决策分布 STAY 2630 / RETURN 0。回撤的 2 条腿
   在判分模型下永远买不回收益（与原生编译器 K5 同构）——驻留的全部价值
   兑现为门位自由度，RETURN 三方案成为"备而无用的保险"。
2. **γ0 前瞻项不赚反亏**：哨兵上 γ0=0 → 0.738/0.863/1.028 vs 默认 0.5 →
   0.777/0.870/1.025（幅度在种子噪声内）。机理：锚点=搭档当前位置是陈旧
   预测，而成本已由第一层精确计价——结构性部件起作用、权重项平坦，
   与 ZAC_new 的 w_batch 现象同款。
3. **种子方差超帽**：qft_n29 极差 0.079 > 0.03 验收线（seed1=0.698 反而
   优于默认 seed0）；ghz_n78 极差 0。多种子取优是 v1.1 的免费收益。

### 8.3 链式族的几何铁律（未达串行验收线 0.85 的机理说明）

新原子必须与驻留者**同排**落座（配对座位同行），触发 compatible_2d 的
"同终点 ⇒ 必须同起点"y 维冲突——驻留者只要挪一步，两腿必分两批。
破法是**预取**（下轮搭档提前入场占位），v1.1 首选。

---

## 附：与 ZAC 的差异总清单（速查）

| 位置 | ZAC | ZAC_zzx | 差异性质 |
|---|---|---|---|
| 回位放置 vmplacer.py:343 | 全员强制回存储 | decide_lazy 默认 STAY | **核心行为差异** |
| 复用 zac.py:256 | 只看相邻一层 | NextUse 全程 + 边界决策 | **核心行为差异** |
| 门放置 place_gate | 纯距离匹配 | GA + 分相位着色适应度 | **核心行为差异** |
| 回程 remain_graph router.py:98 | 只扫本轮门原子 | 全部映射增量者 | 配套 |
| 路由断言 router.py:60 | 一端必在存储 | 两端合法即可（zone→zone 合法） | 配套 |
| 依赖账本 | 原样 | 非参与者回撤腿压到门指令后 | 补丁 |
| 门位候选窗 | 无占用过滤 | 硬排除他人座位 + 保座朝向 + 终检修复 | 补丁 |
| 验证 | 4 查（内置 2 查） | 8 查独立回放 | 证据链 |
| 配置 | — | 白名单 + 消费断言（未认识键报错） | 工程保障 |
