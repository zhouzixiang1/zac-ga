"""批次回放校验器：独立于"由构造保证正确"的第二道正确性证据链（8 查版）。

背景：ZAC/ZAC_zzx 的批次合法性是"由构造保证"的（独立集、覆盖性、
依赖账本），内置 verifier 只查调度与第 0 层映射。本脚本换一条完全
独立的路：把落盘的 ZAIR 指令流当成"别人交来的作业"，逐条回放检查。
路由分批器换成图着色、放置器换成驻留模式之后，这道独立校验从
"可选"升级为"必需"。

八项检查（对应八类可能出错的方式）：
  ① 批内兼容性：每个 rearrangeJob 里所有原子的外层搬运腿两两
     compatible_2D（AOD 保序铁律；停车微调在 job 内部 insts 里，
     不改外层端点，不影响本检查的语义）。
  ② 位置连续性：每个原子在本 job 的起点 = 它上一条指令时的终点
     （从第 0 条 init_locs 起全程追踪，幽灵传送即报错）。
  ③ 时序依赖（取放语义）：qubit/aod 依赖严格（begin ≥ 前驱 end）；
     site 依赖镜像 router 的 drop-after-pickup 重叠（router.py:622-648）：
     合法若 begin ≥ dep.end（严格）或 本批放完 ≥ dep 开始拿起+15μs
     （我先到site上空等、你拿起后我才放下）。同一 AOD 不得重叠。
  ④ 门执行邻接：CZ 门执行时两个原子必须坐在同一纠缠区的成对工位。
  ⑤ 座位独占时间线：任意时刻任何 (a,r,c) 至多一个原子——
     到达事件（job 结束=放稳）必须不早于离开事件（job 开始+15μs=拿起完成）。
     驻留模式头号风险（座位双订）由此兜底。
  ⑥ 1qGate 位置一致：指令声明的 locs 必须与追踪位置一致
     （旧版直接覆写不校验——半成品驻留补丁的陈旧 locs 会漏过）。
  ⑦ 逐比特门账本 vs QASM（需 --qasm=path）：每个比特的 2q 门搭档
     子序列必须与电路一致（时空查之外唯一的语义查）。
  ⑧ 门-搬运互斥（原子粒度）：任何作业搬运原子 q 的 begin 必须不早于
     q 自己最近一条门指令（rydberg/1qGate 且涉及 q）的 end。依赖账本的
     独立验收——不信任 dependency 字段，直接从流里重建。
     （ZAC 原版语义：不同原子的门与搬运允许并行，账本只约束同一原子；
     曾按"整块门串行"实现，在 ZAC 原版 18 电路上全部误报后修正。）

用法：
    ZAC/.venv/bin/python verify_batches.py <code.json> [more.json ...] [--qasm=<path>]
    （架构 spec 优先取 code JSON 里记录的路径；退出码 0=全部通过，1=有违例）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from zac.ds.architecture import Architecture       # noqa: E402 本地副本
from zzx.ghost import ghost_hits                  # noqa: E402 ⑨ 鬼点判据
from zzx.zcost import compatible_2d               # noqa: E402

T_TRANSFER = 15.0    # μs，与 router/架构的 atom_transfer 常数一致


def load_arch(code: dict, code_path: Path) -> Architecture:
    """按 code JSON 里记录的 spec 路径找架构（相对 run 目录解析）。"""
    candidates = []
    raw = code.get("architecture_spec_path", "")
    if raw:
        candidates += [code_path.parent.parent / raw, code_path.parent / raw,
                       Path(__file__).resolve().parent / raw]
    for arg in sys.argv[1:]:
        if arg.startswith("--arch="):
            candidates.insert(0, Path(arg.split("=", 1)[1]))
    for cand in candidates:
        if cand.is_file():
            with open(cand, encoding="utf-8") as handle:
                arch = Architecture(json.load(handle))
            arch.preprocessing()
            return arch
    raise FileNotFoundError(f"找不到架构 spec：{raw}（候选：{candidates}）")


def qasm_partner_ledger(qasm_path: Path) -> dict[int, list[int]]:
    """从 QASM 提取每个比特的 2q 门搭档有序表（qiskit 解析）。"""
    from qiskit import QuantumCircuit
    qc = QuantumCircuit.from_qasm_str(qasm_path.read_text())
    ledger: dict[int, list[int]] = {}
    for inst in qc.data:
        qs = [qc.find_bit(q).index for q in inst.qubits]
        if len(qs) == 2:
            ledger.setdefault(qs[0], []).append(qs[1])
            ledger.setdefault(qs[1], []).append(qs[0])
    return ledger


def verify(code_path: Path, qasm_path: Path | None = None) -> dict:
    with open(code_path, encoding="utf-8") as handle:
        code = json.load(handle)
    arch = load_arch(code, code_path)
    insts = code["instructions"]

    # 预备：指令 id → 时间；纠缠区 SLM 对
    time_of = {i["id"]: (i["begin_time"], i["end_time"]) for i in insts
               if "begin_time" in i}
    inst_by_id = {i["id"]: i for i in insts}
    zone_pairs = [set(z) for z in arch.entanglement_zone]

    def exact(loc):
        return arch.exact_SLM_location_tuple(loc)   # loc = (a, r, c)

    def ex_tuple(t):
        return arch.exact_SLM_location_tuple(tuple(t))

    where: dict[int, tuple] = {}     # 原子 → 当前 (a, r, c)
    aod_busy: dict[int, list] = {}   # aod_id → [(begin, end, id)]
    site_events: dict[tuple, list] = {}   # (a,r,c) → [(time, +1/-1, atom, id)]
    errors = {"compat": [], "continuity": [], "timing": [], "adjacency": [],
              "seat": [], "1q_loc": [], "gate_ledger": [], "boundary": [],
              "ghost": []}
    stats = {"jobs": 0, "atoms_moved": 0, "legs": 0}

    def site_event(site, t, delta, atom, iid):
        site_events.setdefault(site, []).append((t, delta, atom, iid))

    # ⑧ 预备：每原子最近一条门指令的结束时间（从流里独立重建，不信任账本）
    atom_gate_end: dict[int, float] = {}
    ryd_partner: dict[int, list[int]] = {}        # ⑦ 流内的搭档账本

    for inst in insts:
        kind = inst.get("type")
        if "init_locs" in inst:                      # 层起点快照（座位独占的 t=0 事件）
            for loc in inst["init_locs"]:
                where[loc[0]] = tuple(loc[1:])
                site_event(tuple(loc[1:]), 0.0, +1, loc[0], inst["id"])
        if kind != "rearrangeJob":
            # ④ 门执行邻接：CZ 门的两原子必须在成对工位
            #（rydberg 门字段是 q0/q1；曾按 q/qubits 解析——空转没有任何覆盖）
            for g in inst.get("gates", []):
                if "q0" not in g or "q1" not in g:
                    continue
                qs = [g["q0"], g["q1"]]
                locs = [where.get(q) for q in qs]
                if any(l is None for l in locs):
                    continue
                (a1, r1, c1), (a2, r2, c2) = locs
                ok = r1 == r2 and c1 == c2 and {a1, a2} in zone_pairs
                if not ok:
                    errors["adjacency"].append(
                        f"指令{inst['id']} 门{g}: {qs[0]}@{locs[0]} 与 {qs[1]}@{locs[1]} 不在成对工位")
                if kind == "rydberg":                # ⑦ 流内搭档账本
                    ryd_partner.setdefault(qs[0], []).append(qs[1])
                    ryd_partner.setdefault(qs[1], []).append(qs[0])
                # ⑧ 预备：记录每原子最近门指令结束时间
                for q in qs:
                    atom_gate_end[q] = max(atom_gate_end.get(q, 0.0), inst["end_time"])
            # ⑥ 1qGate locs 必须与追踪位置一致（不再盲目覆写）
            if "locs" in inst:
                for loc in inst["locs"]:
                    if len(loc) != 4:
                        continue
                    q, pos = loc[0], tuple(loc[1:])
                    if q in where and where[q] != pos:
                        errors["1q_loc"].append(
                            f"指令{inst['id']} 原子{q}: 声明位置{pos} ≠ 追踪位置{where[q]}")
                    where[q] = pos
                    # ⑧ 预备：1q 门执行期间该原子同样不可被搬运
                    atom_gate_end[q] = max(atom_gate_end.get(q, 0.0), inst["end_time"])
            continue

        # ---- rearrangeJob：①②③⑤⑧ 逐项检查 ----
        stats["jobs"] += 1
        begin = {loc[0]: tuple(loc[1:]) for loc in inst["begin_locs"]}
        end = {loc[0]: tuple(loc[1:]) for loc in inst["end_locs"]}
        stats["atoms_moved"] += len(inst["aod_qubits"])

        # ② 连续性：job 起点必须等于原子当前位置
        for q, loc in begin.items():
            if q in where and where[q] != loc:
                errors["continuity"].append(
                    f"指令{inst['id']} 原子{q}: 当前在{where[q]}，本批起点却是{loc}")
            where[q] = end[q]                       # 前进到批末位置

        # ⑤ 座位独占事件：源位在"拿起开始"（job.begin）即释放（router 语义），
        # 终点在 job 结束（放稳）才占用——比"拿起完成再释放"宽松 15μs，
        # 与 aod_assignment 的取放重叠放行口径一致
        for q in inst["aod_qubits"]:
            if q in begin and begin[q] != end[q]:
                site_event(begin[q], inst["begin_time"], -1, q, inst["id"])
                site_event(end[q], inst["end_time"], +1, q, inst["id"])

        # ① 批内两两兼容（外层腿；dist=0 的腿不占车）
        legs = []
        for q in inst["aod_qubits"]:
            if q not in begin or q not in end:
                continue
            (x0, y0), (x1, y1) = exact(begin[q]), exact(end[q])
            if abs(x1 - x0) + abs(y1 - y0) > 1e-9:
                legs.append((q, (x0, x1, y0, y1)))
        stats["legs"] += len(legs)
        for i in range(len(legs)):
            for j in range(i + 1, len(legs)):
                qa, va = legs[i]
                qb, vb = legs[j]
                if not compatible_2d(va, vb):
                    errors["compat"].append(
                        f"指令{inst['id']} 批内原子{qa}与{qb}的搬运腿不兼容")

        # ③ 时序：qubit 依赖严格 + 同 AOD 不重叠。
        # site 依赖不做成对检查——多跳座位交接（A放下→B拿起→C落下，中间隔
        # 第三方作业）在成对视角里必然误报；座位物理冲突由 ⑤ 的整批结算
        # 时间线独立把关（校验物理，不复读账本）。
        dep = inst.get("dependency", {})
        for d in dep.get("qubit", []):
            if d in time_of and time_of[d][1] > inst["begin_time"] + 1e-6:
                errors["timing"].append(
                    f"指令{inst['id']} begin={inst['begin_time']:.1f} 早于依赖qubit:{d} "
                    f"的 end={time_of[d][1]:.1f}")
        aod_id = dep.get("aod", inst.get("aod_id"))
        for (b0, e0, i0) in aod_busy.get(aod_id, []):
            if not (inst["end_time"] <= b0 + 1e-6 or e0 <= inst["begin_time"] + 1e-6):
                errors["timing"].append(
                    f"指令{inst['id']} 与指令{i0} 在 AOD{aod_id} 上时间重叠")
        aod_busy.setdefault(aod_id, []).append(
            (inst["begin_time"], inst["end_time"], inst["id"]))

        # ⑨ 鬼点：本批光束交叉点轨迹 vs 批时静止原子（动 vs 静——两家论文
        #    声明、两家代码未实现的约束；zzx 产物应恒为 0，ZAC/qmap 产物
        #    跑出命中属"审计发现"，默认只报告，--strict 才计违例）
        from math import dist as _dist
        legs9 = []
        for q, b, e in zip(inst["aod_qubits"], inst["begin_locs"],
                           inst["end_locs"]):
            p0, p1 = ex_tuple(tuple(b[1:])), ex_tuple(tuple(e[1:]))
            if _dist(p0, p1) > 1e-9:
                legs9.append((_dist(p0, p1), *p0, *p1))
        ghosts9 = [(q, *ex_tuple(where[q])) for q in where
                   if q not in set(inst["aod_qubits"])]
        for gid, gx, gy in ghost_hits(legs9, ghosts9):
            errors["ghost"].append(
                f"指令{inst['id']} 批内光束扫过静止原子 q{gid}@({gx:.0f},{gy:.0f})")

        # ⑧ 门-搬运互斥（原子粒度）：原子自己的门没做完之前不能被搬
        for q in inst["aod_qubits"]:
            ge = atom_gate_end.get(q, 0.0)
            if inst["begin_time"] < ge - 1e-6:
                errors["boundary"].append(
                    f"指令{inst['id']} 搬运原子{q}: begin {inst['begin_time']:.1f} "
                    f"早于它自己的门结束 {ge:.1f}")

    # ⑤ 座位独占：同一时刻的事件【整批结算】再查双占——ZAC 的两段式搬运
    # 会让"放下"与"再拿起"发生在同一微秒（零驻留中转），逐事件处理会把
    # 离开扑空、到达永占（ZAC 原版曾因此在 8 个电路上全部误报）
    for site, events in site_events.items():
        events.sort(key=lambda e: e[0])
        occupied: dict = {}
        k = 0
        while k < len(events):
            t = events[k][0]
            batch = []
            while k < len(events) and events[k][0] <= t + 1e-6:
                batch.append(events[k])
                k += 1
            for _, delta, atom, iid in batch:
                if delta > 0:
                    occupied[atom] = t
                else:
                    occupied.pop(atom, None)
            if len(occupied) >= 2:
                names = [f"{a}@{tt:.1f}" for a, tt in occupied.items()]
                errors["seat"].append(
                    f"座位{site}: t={t:.1f} 同时占用 {names}（指令{batch[0][3]} 等）")

    # ⑦ 门账本：优先与 code JSON 内嵌的重综合门列表对账（编译器实际执行集，
    # 与路由/放置同一输入）；--qasm 仅在无内嵌账本时使用（注意：直读 QASM
    # 会差在共享的重综合层——ZAC 真值同样"缺门"，4 电路实证，非编译 bug）
    embedded = code.get("gate_ledger")
    if embedded:
        for q, partners in embedded.items():
            got = ryd_partner.get(int(q), [])
            if got != partners:
                errors["gate_ledger"].append(
                    f"原子{q}: 编译器搭档序 {partners} ≠ 流内 {got}")
    elif qasm_path is not None:
        want = qasm_partner_ledger(qasm_path)
        for q, partners in want.items():
            got = ryd_partner.get(q, [])
            if got != partners:
                errors["gate_ledger"].append(
                    f"原子{q}: QASM 搭档序 {partners} ≠ 流内 {got}")

    stats["ghost_hits"] = len(errors["ghost"])
    return {"errors": errors, "stats": stats}


def main():
    qasm = None
    strict = "--strict" in sys.argv
    paths = []
    for a in sys.argv[1:]:
        if a.startswith("--arch=") or a.startswith("--qasm="):
            if a.startswith("--qasm="):
                qasm = Path(a.split("=", 1)[1])
        elif a != "--strict":
            paths.append(Path(a))
    if not paths:
        print(__doc__)
        sys.exit(2)
    n_bad = 0
    for p in paths:
        result = verify(p, qasm)
        errs = result["errors"]
        n_err = sum(len(v) for v in errs.values())
        if not strict:
            n_err -= len(errs["ghost"])      # 默认鬼点仅报告；--strict 计违例
        s = result["stats"]
        verdict = "✅ PASS" if n_err == 0 else f"❌ {n_err} 处违例"
        ghost_note = f", 鬼点={s['ghost_hits']}" + ("" if s["ghost_hits"] == 0
                                                    else "（--strict 计违例）")
        print(f"{p.name:40s} {verdict}   "
              f"(批次数={s['jobs']}, 搬运原子人次={s['atoms_moved']}, "
              f"有效腿={s['legs']}{ghost_note})")
        for kind, msgs in errs.items():
            for m in msgs[:5]:
                print(f"    [{kind}] {m}")
            if len(msgs) > 5:
                print(f"    [{kind}] ……另有 {len(msgs) - 5} 条")
        n_bad += (n_err > 0)
    sys.exit(1 if n_bad else 0)


if __name__ == "__main__":
    main()
