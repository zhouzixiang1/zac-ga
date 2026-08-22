"""批次回放校验器：独立于"由构造保证正确"的第二道正确性证据链。

背景：ZAC/ZAC_new 的批次合法性是"由构造保证"的（独立集、覆盖性、
依赖账本），内置 verifier 只查调度与第 0 层映射。本脚本换一条完全
独立的路：把落盘的 ZAIR 指令流当成"别人交来的作业"，逐条回放检查。
路由分批器换成图着色之后（routing_strategy="coloring"），这道独立
校验从"可选"升级为"必需"。

四项检查（对应四类可能出错的方式）：
  ① 批内兼容性：每个 rearrangeJob 里所有原子的外层搬运腿两两
     compatible_2D（AOD 保序铁律；停车微调在 job 内部 insts 里，
     不改外层端点，不影响本检查的语义）。
  ② 位置连续性：每个原子在本 job 的起点 = 它上一条指令时的终点
     （从第 0 条 init_locs 起全程追踪，幽灵传送即报错）。
  ③ 时序依赖：job 的 begin_time ≥ 其依赖（qubit/site 账本里所有
     前驱指令）的 end_time；同一 AOD 上的作业时间不得重叠。
  ④ 门执行邻接：CZ 门执行时两个原子必须坐在同一纠缠区的成对工位
     （同 r 同 c、分列左右两个 SLM）。

用法：
    ZAC/.venv/bin/python verify_batches.py <code.json> [more.json ...]
    （架构 spec 优先取 code JSON 里记录的路径，找不到可用环境变量
     ZNEW_ARCH 覆盖；退出码 0=全部通过，1=有违例）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from zac.ds.architecture import Architecture       # noqa: E402 本地副本
from znew.zcost import compatible_2d               # noqa: E402


def load_arch(code: dict, code_path: Path) -> Architecture:
    """按 code JSON 里记录的 spec 路径找架构（相对 run 目录解析）。"""
    candidates = []
    raw = code.get("architecture_spec_path", "")
    if raw:
        candidates += [code_path.parent.parent / raw, code_path.parent / raw,
                       Path(__file__).resolve().parent / raw]
    if len(sys.argv) > 1 and sys.argv[1].startswith("--arch="):
        candidates.insert(0, Path(sys.argv[1].split("=", 1)[1]))
    for cand in candidates:
        if cand.is_file():
            arch = Architecture(json.load(open(cand)))
            arch.preprocessing()
            return arch
    raise FileNotFoundError(f"找不到架构 spec：{raw}（候选：{candidates}）")


def verify(code_path: Path) -> dict:
    code = json.load(open(code_path))
    arch = load_arch(code, code_path)
    insts = code["instructions"]

    # 预备：指令 id → 时间；纠缠区 SLM 对
    time_of = {i["id"]: (i["begin_time"], i["end_time"]) for i in insts
               if "begin_time" in i}
    zone_pairs = [set(z) for z in arch.entanglement_zone]

    def exact(loc):
        return arch.exact_SLM_location_tuple(loc)   # loc = (a, r, c)

    where: dict[int, tuple] = {}     # 原子 → 当前 (a, r, c)
    aod_busy: dict[int, list] = {}   # aod_id → [(begin, end, id)]
    errors = {"compat": [], "continuity": [], "timing": [], "adjacency": []}
    stats = {"jobs": 0, "atoms_moved": 0, "legs": 0}

    for inst in insts:
        kind = inst.get("type")
        if "init_locs" in inst:                      # 层起点快照
            for loc in inst["init_locs"]:
                where[loc[0]] = tuple(loc[1:])
        if kind != "rearrangeJob":
            # ④ 门执行邻接：CZ 门的两原子必须在成对工位
            for g in inst.get("gates", []):
                qs = g.get("q", g.get("qubits", []))
                if not isinstance(qs, list) or len(qs) != 2:
                    continue
                locs = [where.get(q) for q in qs]
                if any(l is None for l in locs):
                    continue
                (a1, r1, c1), (a2, r2, c2) = locs
                ok = r1 == r2 and c1 == c2 and {a1, a2} in zone_pairs
                if not ok:
                    errors["adjacency"].append(
                        f"指令{inst['id']} 门{g}: {qs[0]}@{locs[0]} 与 {qs[1]}@{locs[1]} 不在成对工位")
            # 单比特门/测量等：更新原子位置（如果带 locs）
            if "locs" in inst:
                for loc in inst["locs"]:
                    if len(loc) == 4:
                        where[loc[0]] = tuple(loc[1:])
            continue

        # ---- rearrangeJob：①②③ 逐项检查 ----
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

        # ③ 时序：依赖前驱的 end_time ≤ 本 job 的 begin_time
        dep = inst.get("dependency", {})
        for key in ("qubit", "site"):
            for d in dep.get(key, []):
                if d in time_of and time_of[d][1] > inst["begin_time"] + 1e-6:
                    errors["timing"].append(
                        f"指令{inst['id']} begin={inst['begin_time']:.1f} 早于依赖{key}:{d} "
                        f"的 end={time_of[d][1]:.1f}")
        # 同一 AOD 上的作业时间不得重叠
        aod_id = dep.get("aod", inst.get("aod_id"))
        for (b0, e0, i0) in aod_busy.get(aod_id, []):
            if not (inst["end_time"] <= b0 + 1e-6 or e0 <= inst["begin_time"] + 1e-6):
                errors["timing"].append(
                    f"指令{inst['id']} 与指令{i0} 在 AOD{aod_id} 上时间重叠")
        aod_busy.setdefault(aod_id, []).append((inst["begin_time"], inst["end_time"], inst["id"]))

    return {"errors": errors, "stats": stats}


def main():
    paths = [Path(a) for a in sys.argv[1:] if not a.startswith("--arch=")]
    if not paths:
        print(__doc__)
        sys.exit(2)
    n_bad = 0
    for p in paths:
        result = verify(p)
        errs = result["errors"]
        n_err = sum(len(v) for v in errs.values())
        s = result["stats"]
        verdict = "✅ PASS" if n_err == 0 else f"❌ {n_err} 处违例"
        print(f"{p.name:40s} {verdict}   "
              f"(批次数={s['jobs']}, 搬运原子人次={s['atoms_moved']}, 有效腿={s['legs']})")
        for kind, msgs in errs.items():
            for m in msgs[:5]:
                print(f"    [{kind}] {m}")
            if len(msgs) > 5:
                print(f"    [{kind}] ……另有 {len(msgs) - 5} 条")
        n_bad += (n_err > 0)
    sys.exit(1 if n_bad else 0)


if __name__ == "__main__":
    main()
