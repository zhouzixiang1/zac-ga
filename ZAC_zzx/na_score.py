"""NA 码五项保真度计分器（无依赖纯 Python，可对任意版本的 qmap 输出计分）。

与 qmap_score.py 同一套约定（ZAC 作者 compare_zac 口径）：
  job = load…store 循环 = 2×15μs + √(dmax/0.00275)
  @+ cz = 0.36μs；@+ u/rz/ry/sh = 52μs（顺序时间戳）
  F = 0.9997^n1q × 0.995^n2q × 0.999^(2×人次) × Π(1−idle/T)
用法：python3 na_score.py <meta.json> <out.json>
  meta.json 每行 {circuit, config, na_file, n1q[], n2q[]}（门数由调用方
  在 qiskit 侧统计——3.2.0 的 mqt 对象没有统一 API）。
"""
from __future__ import annotations

import json
import re
import sys
from math import sqrt

ACCEL, T_TR, T_1Q, T_RYD, T_COH = 0.00275, 15.0, 52.0, 0.36, 1.5e6
MOVE = re.compile(r"\((-?\d+\.?\d*), (-?\d+\.?\d*)\) (\w+)")


def score_na(path, n1q, n2q):
    lines = [l for l in open(path).read().splitlines() if l.strip()]
    loc, i = {}, 0
    while True:
        m = re.match(r"atom \((-?\d+\.?\d*), (-?\d+\.?\d*)\) (\w+)", lines[i])
        if not m:
            break
        loc[m.group(3)] = (float(m.group(1)), float(m.group(2)))
        i += 1

    def block(j, pref):
        if lines[j].rstrip().endswith("["):
            toks, j = [], j + 1
            while lines[j].strip() != "]":
                toks.append(lines[j].strip())
                j += 1
            return toks, j + 1
        return [lines[j][len(pref):].strip()], j + 1

    held, job, jobs = set(), {}, []
    moves = [0] * (len(n1q) if n1q else max((int(a[4:]) for a in loc), default=0) + 1)
    dur = 0.0
    n1i = 0

    def flush():
        nonlocal dur, job
        if job:
            dmax = max(sqrt((loc[a][0] - p[0]) ** 2 + (loc[a][1] - p[1]) ** 2)
                       for a, p in job.items())
            jobs.append(len(job))
            dur += 2 * T_TR + sqrt(dmax / ACCEL)
            for a in job:
                moves[int(a[4:])] += 1
            job = {}

    while i < len(lines):
        L = lines[i]
        if L.startswith("@+ load"):
            toks, i = block(i, "@+ load")
            for a in toks:
                job.setdefault(a, loc[a])
                held.add(a)
        elif L.startswith("@+ move"):
            if L.rstrip().endswith("["):
                i += 1
                while lines[i].strip() != "]":
                    mm = MOVE.match(lines[i].strip())
                    if mm:
                        loc[mm.group(3)] = (float(mm.group(1)), float(mm.group(2)))
                    i += 1
                i += 1
            else:
                mm = MOVE.search(L)
                if mm:
                    loc[mm.group(3)] = (float(mm.group(1)), float(mm.group(2)))
                i += 1
        elif L.startswith("@+ store"):
            toks, i = block(i, "@+ store")
            for a in toks:
                held.discard(a)
            if not held:
                flush()
        elif L.startswith("@+ cz"):
            flush()
            dur += T_RYD
            i += 1
        elif L.startswith(("@+ u", "@+ rz", "@+ ry", "@+ sh")):
            flush()
            n1i += 1
            dur += T_1Q
            i += 1
            if lines[i - 1].rstrip().endswith("["):
                while lines[i].strip() != "]":
                    i += 1
                i += 1
        else:
            i += 1
    flush()
    tr = sum(jobs)
    f1q = 0.9997 ** sum(n1q)
    f2q = 0.995 ** (sum(n2q) // 2)
    ftr = 0.999 ** (2 * tr)
    coh = 1.0
    for q in range(len(n1q)):
        busy = T_1Q * n1q[q] + T_RYD * n2q[q] + 2 * T_TR * moves[q]
        coh *= max(0.0, 1 - max(0.0, dur - busy) / T_COH)
    return {"steps": sum(jobs), "transfers": tr, "duration": round(dur, 1),
            "n1q_total": sum(n1q), "n2q_total": sum(n2q) // 2,
            "fid": round(f1q * f2q * ftr * coh, 6),
            "f1q": round(f1q, 4), "f2q": round(f2q, 4),
            "f_trans": round(ftr, 4), "f_coh": round(coh, 4)}


if __name__ == "__main__":
    meta = json.load(open(sys.argv[1]))
    out = []
    for m in meta:
        r = score_na(m["na_file"], m["n1q"], m["n2q"])
        r["circuit"] = m["circuit"]
        r["config"] = m["config"]
        out.append(r)
        print(m["circuit"], m["config"], r["steps"], r["fid"], flush=True)
    json.dump(out, open(sys.argv[2], "w"), indent=1)
    print("DONE", len(out))
