"""人工核验工作表生成器：把编译产物 code JSON 排成"可以拿笔核对"的表。

核验分批次的正确性 = 核以下四件事（对应 verify_batches.py 的 ①-⑧ 检查，
本工具只做"打印"，不做"判定"，判定留给人；--strict 例外，见下）：

  1. 时间线：每条指令干了什么、何时开始何时结束（核依赖与时序用）
  2. 每批物理腿表：一条 rearrangeJob = 一个搬送批；批内每个原子一条
     "搬运腿"（物理起点→物理终点）。注意：逻辑 loc=(座位区,行,列) 里
     成对工位共享 (行,列)，必须换算成物理 (x,y) 才能套 compatible_2d
     四规则（同起点必同终点；起点不同则顺序严格保持，禁止交叉/汇合/追平；
     行、列各查一遍）
  3. 单原子行踪（--atom）：一个原子从 init 到末轮的完整旅程
  4. 座位账本（--seats）：每个座位上"谁在何时放下、谁在何时拿起"

用法：
  python manual_check.py results/main/code/bv_n14_transpiled_code.json
  python manual_check.py xxx_code.json --atom 13 --seats
  python manual_check.py xxx_code.json --batch 6      # 只看指定批
  python manual_check.py xxx_code.json --strict       # 机器代算四规则逐对判定
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_batches import load_arch  # noqa: E402  复用硬件规格加载


def compatible_2d(a, b):
    """ZAC router.py:231 原样四规则；a/b=(x0,x1,y0,y1) 物理坐标。"""
    if a[0] == b[0] and a[1] != b[1]:
        return False
    if a[1] == b[1] and a[0] != b[0]:
        return False
    if a[0] < b[0] and a[1] >= b[1]:
        return False
    if a[0] > b[0] and a[1] <= b[1]:
        return False
    if a[2] == b[2] and a[3] != b[3]:
        return False
    if a[3] == b[3] and a[2] != b[2]:
        return False
    if a[2] < b[2] and a[3] >= b[3]:
        return False
    if a[2] > b[2] and a[3] <= b[3]:
        return False
    return True


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a[2:] for a in sys.argv[1:] if a.startswith("--")}
    batch_filter = None
    if "batch" in flags:                      # --batch N 只看批 N
        batch_filter = int(sys.argv[sys.argv.index("--batch") + 1])
        flags.discard("batch")
    atom = int(sys.argv[sys.argv.index("--atom") + 1]) if "atom" in flags else None
    flags.discard("atom")

    cp = Path(args[0])
    code = json.load(open(cp))
    arch = load_arch(code, cp)
    exact = lambda l: arch.exact_SLM_location_tuple(tuple(l[1:]))
    insts = code["instructions"]
    time_of = {i["id"]: (i["begin_time"], i["end_time"]) for i in insts
               if "begin_time" in i}

    # ---------- 1. 时间线 ----------
    print(f"=== 时间线 {cp.name}（行99=存储区；逻辑格=(座位区,行,列)）===")
    for i in insts:
        if i["type"] == "rearrangeJob":
            legs = [f"q{q}:s{b[1]}({b[2]},{b[3]})→s{e[1]}({e[2]},{e[3]})"
                    for q, b, e in zip(i["aod_qubits"], i["begin_locs"], i["end_locs"])
                    if b != e]
            print(f"#{i['id']:>3} 搬送 {i['begin_time']:8.2f}→{i['end_time']:8.2f} "
                  f"依赖qubit={i.get('dependency',{}).get('qubit',[])} | " +
                  (" ".join(legs) if legs else "(原地)"))
        elif i["type"] == "rydberg":
            gs = [(g["q0"], g["q1"]) for g in i["gates"]]
            print(f"#{i['id']:>3} 门批 {i['begin_time']:8.2f}→{i['end_time']:8.2f} | {gs}")
        elif i["type"] == "1qGate":
            gs = [f"{g['name']}:q{g['q']}" for g in i["gates"]]
            print(f"#{i['id']:>3} 1q批 {i['begin_time']:8.2f}→{i['end_time']:8.2f} | {gs[:8]}"
                  + ("..." if len(gs) > 8 else ""))

    # ---------- 2. 每批物理腿表 ----------
    where = {}          # 原子 → 当前逻辑位（顺带核行踪连续性）
    for inst in insts:
        if "init_locs" in inst:
            for loc in inst["init_locs"]:
                where[loc[0]] = tuple(loc[1:])
        if inst["type"] != "rearrangeJob":
            continue
        if batch_filter is not None and inst["id"] != batch_filter:
            continue
        legs = []
        gap = []
        for q, b, e in zip(inst["aod_qubits"], inst["begin_locs"], inst["end_locs"]):
            bK, eK = tuple(b[1:]), tuple(e[1:])
            if q in where and where[q] != bK:
                gap.append(f"q{q}:账本在{where[q]} 本批起点{bK}")
            where[q] = eK
            (x0, y0), (x1, y1) = exact(b), exact(e)
            if abs(x1 - x0) + abs(y1 - y0) > 1e-9:
                legs.append((q, (x0, x1, y0, y1)))
        if not legs:
            continue
        print(f"\n--- 批#{inst['id']} {inst['begin_time']:.2f}→{inst['end_time']:.2f} "
              f"共{len(legs)}条腿（物理坐标，y=行 x=列）---")
        print(f"{'原子':>5} {'起点(x,y)':>16} {'终点(x,y)':>16}  {'y行 起→终':>14}  {'x列 起→终':>14}")
        for q, v in sorted(legs, key=lambda t: (t[1][2], t[1][0])):
            x0, x1, y0, y1 = v
            print(f"q{q:>4} ({x0:7.1f},{y0:7.1f}) ({x1:7.1f},{y1:7.1f})"
                  f"  {y0:6.1f}→{y1:<6.1f}  {x0:6.1f}→{x1:<6.1f}")
        for g in gap:
            print(f"  ⚠ 行踪断裂: {g}")
        if "strict" in flags:
            bad = [f"q{legs[i][0]}×q{legs[k][0]}"
                   for i in range(len(legs)) for k in range(i + 1, len(legs))
                   if not compatible_2d(legs[i][1], legs[k][1])]
            n = len(legs) * (len(legs) - 1) // 2
            print(f"  四规则逐对 {n} 对: {'全部兼容 ✓' if not bad else '冲突→ ' + ' '.join(bad)}")

    # ---------- 3. 单原子行踪 ----------
    if atom is not None:
        print(f"\n=== 原子 q{atom} 全程行踪 ===")
        pos = None
        for inst in insts:
            if "init_locs" in inst:
                for loc in inst["init_locs"]:
                    if loc[0] == atom:
                        pos = tuple(loc[1:])
                        print(f"  init: {pos}")
            if inst["type"] == "rearrangeJob" and atom in inst["aod_qubits"]:
                k = inst["aod_qubits"].index(atom)
                b, e = tuple(inst["begin_locs"][k][1:]), tuple(inst["end_locs"][k][1:])
                print(f"  #{inst['id']:>3} {inst['begin_time']:8.2f}→{inst['end_time']:8.2f}"
                      f"  {b} → {e}" + ("（未动）" if b == e else ""))
                pos = e
            elif inst["type"] in ("rydberg", "1qGate"):
                for g in inst.get("gates", []):
                    if atom in (g.get("q0"), g.get("q1"), g.get("q")):
                        print(f"  #{inst['id']:>3} {inst['begin_time']:8.2f}→{inst['end_time']:8.2f}"
                              f"  做门 {g} @ {pos}")

    # ---------- 4. 座位账本 ----------
    if "seats" in flags:
        events = {}
        for inst in insts:
            if "init_locs" in inst:
                for loc in inst["init_locs"]:
                    events.setdefault(tuple(loc[1:]), []).append((0.0, f"+q{loc[0]} init"))
            if inst["type"] != "rearrangeJob":
                continue
            for q, b, e in zip(inst["aod_qubits"], inst["begin_locs"], inst["end_locs"]):
                if b == e:
                    continue
                events.setdefault(tuple(b[1:]), []).append(
                    (inst["begin_time"], f"-q{q} 拿起(#{inst['id']})"))
                events.setdefault(tuple(e[1:]), []).append(
                    (inst["end_time"], f"+q{q} 放下(#{inst['id']})"))
        print("\n=== 座位账本（同刻批结算：t 相同的 -/+ 视为同时交棒）===")
        for site in sorted(events, key=lambda s: min(t for t, _ in events[s])):
            ev = sorted(events[site])
            print(f"座位{site}: " + " | ".join(f"t={t:.1f} {m}" for t, m in ev))


if __name__ == "__main__":
    main()
