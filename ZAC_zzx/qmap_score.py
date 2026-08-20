"""ICCAD(qmap) 的同尺计分：NA 码 → 五项保真度 + 顺序时长（compare_zac 约定）。

约定（与 Physics-Layer-Compiler/examples/compare_zac.py 给 Fable 计时完全一致）：
  - 每个搬运作业（load…store 循环）= 2×15μs + √(dmax/0.00275)，dmax=批内最长位移
  - 每 1q 指令（u/rz 行或块）= 52μs；每 cz 层指令 = 0.36μs；顺序无重叠
  - 五项保真度 = 0.9997^1q × 0.995^2q × 1 × 0.999^(2×人次) × Π(1−idle/T)
    （idle_q = 总时长 − busy_q；busy_q = 52×该比特1q数 + 0.36×该比特2q参与数
      + 30×该比特被搬次数——全部精确可知，无需还原门-比特配对）

用法：.venv_qmap/bin/python qmap_score.py <qasm> <agnostic|astar> [timeout_s]
输出：单行 JSON。
"""
from __future__ import annotations

import io
import json
import re
import sys
from math import sqrt

sys.path.insert(0, "/Users/zhouzixiang/Desktop/zac/experiments")
from spec_convert import convert  # noqa: E402

import json as _json  # noqa: E402
from qiskit import QuantumCircuit, qasm2, transpile  # noqa: E402
from mqt.core import load as mqt_load  # noqa: E402
from mqt.qmap.na.zoned import (  # noqa: E402
    ZonedNeutralAtomArchitecture, RoutingAgnosticCompiler, RoutingAwareCompiler,
)

ACCEL, T_TR, T_1Q, T_RYD, T_COH = 0.00275, 15.0, 52.0, 0.36, 1.5e6
MOVE_LINE = re.compile(r"\((-?\d+\.\d+), (-?\d+\.\d+)\) (\w+)")


def score(qasm_path: str, config: str = "agnostic") -> dict:
    spec = convert(_json.load(open(
        "/Users/zhouzixiang/Desktop/zac/ZAC_zzx/hardware_spec/zac_arch_repro.json")))
    spec["operation_duration"] = {"single_qubit_gate": 0.625, "two_qubit_gate": 0.36,
                                  "rydberg_gate": 0.36, "atom_transfer": 15.0,
                                  "transfer_time": 15.0}
    arch = ZonedNeutralAtomArchitecture.from_json_string(_json.dumps(spec))

    import time as _time
    _t0 = _time.time()
    qc = transpile(QuantumCircuit.from_qasm_file(qasm_path),
                   basis_gates=["cz", "u"], optimization_level=1)
    # qmap 不支持 measure/barrier/reset——终端操作，对搬运无影响，剥掉（表注说明）
    from qiskit.circuit import QuantumCircuit as _QC
    stripped = _QC(qc.num_qubits)
    for inst in qc.data:
        if inst.operation.name in ("measure", "barrier", "reset", "delay"):
            continue
        stripped.append(inst.operation, [qc.find_bit(q).index for q in inst.qubits], [])
    qc = stripped
    buf = io.StringIO()
    qasm2.dump(qc, buf)
    mq = mqt_load(buf.getvalue())

    if config == "agnostic":
        code = RoutingAgnosticCompiler(arch, log_level="ERROR").compile(mq)
    else:  # astar = ICCAD'25 的路由感知放置（论文方法）
        code = RoutingAwareCompiler(arch, log_level="ERROR", deepening_factor=0.6,
                                    deepening_value=0.2, lookahead_factor=0.2,
                                    reuse_level=5.0).compile(mq)

    # ---- 逐比特门计数（来自转译后电路，精确） ----
    n1q = [0] * qc.num_qubits
    n2q = [0] * qc.num_qubits
    for inst in qc.data:
        qs = [qc.find_bit(q).index for q in inst.qubits]
        if len(qs) == 1:
            n1q[qs[0]] += 1
        elif len(qs) == 2:
            n2q[qs[0]] += 1
            n2q[qs[1]] += 1

    # ---- 解析 NA 码（沿用 naviz_eval 的语法） ----
    lines = [l for l in code.splitlines() if l.strip()]
    loc: dict[str, tuple] = {}
    i = 0
    while i < len(lines):
        m = re.match(r"atom\s+\((-?\d+\.\d+),\s*(-?\d+\.\d+)\)\s+(\w+)", lines[i])
        if not m:
            break
        loc[m.group(3)] = (float(m.group(1)), float(m.group(2)))
        i += 1

    def block(j, prefix):
        if lines[j].rstrip().endswith("["):
            toks, j = [], j + 1
            while lines[j].strip() != "]":
                toks.append(lines[j].strip())
                j += 1
            return toks, j + 1
        return [lines[j][len(prefix):].strip()], j + 1

    held: set = set()
    job_start: dict = {}
    moves = [0] * qc.num_qubits          # 每比特被搬作业数
    jobs: list = []                      # (原子数, dmax)
    dur = 0.0
    move_dur = 0.0                       # 纯搬运执行时长（不含门/1q）
    n1q_instr = n_cz = 0

    def flush():
        nonlocal dur, job_start, move_dur
        if job_start:
            dmax = max(sqrt((loc[a][0] - p[0]) ** 2 + (loc[a][1] - p[1]) ** 2)
                       for a, p in job_start.items())
            jobs.append((len(job_start), dmax))
            dur += 2 * T_TR + sqrt(dmax / ACCEL)
            move_dur += 2 * T_TR + sqrt(dmax / ACCEL)
            for a in job_start:
                moves[int(a[4:])] += 1
            job_start = {}
    while i < len(lines):
        line = lines[i]
        if line.startswith("@+ load"):
            toks, i = block(i, "@+ load")
            for a in toks:
                job_start.setdefault(a, loc[a])
                held.add(a)
        elif line.startswith("@+ move"):
            if line.rstrip().endswith("["):
                i += 1
                while lines[i].strip() != "]":
                    mm = MOVE_LINE.match(lines[i].strip())
                    if mm:
                        loc[mm.group(3)] = (float(mm.group(1)), float(mm.group(2)))
                    i += 1
                i += 1
            else:
                mm = MOVE_LINE.search(line)
                if mm:
                    loc[mm.group(3)] = (float(mm.group(1)), float(mm.group(2)))
                i += 1
        elif line.startswith("@+ store"):
            toks, i = block(i, "@+ store")
            for a in toks:
                held.discard(a)
            if not held:
                flush()
        elif line.startswith("@+ cz"):
            flush()
            n_cz += 1
            dur += T_RYD
            i += 1
        elif line.startswith(("@+ u", "@+ rz", "@+ ry", "@+ sh")):
            flush()
            n1q_instr += 1
            dur += T_1Q
            if line.rstrip().endswith("["):
                i += 1
                while lines[i].strip() != "]":
                    i += 1
            i += 1
        else:
            raise ValueError(f"未识别操作: {line!r}")
    flush()

    # ---- 五项保真度（判分器同模型直算） ----
    transfers = sum(n for n, _ in jobs)
    f1q = 0.9997 ** sum(n1q)
    f2q = 0.995 ** (sum(n2q) // 2)
    f_tr = 0.999 ** (2 * transfers)
    coh = 1.0
    for q in range(qc.num_qubits):
        busy = T_1Q * n1q[q] + T_RYD * n2q[q] + 2 * T_TR * moves[q]
        coh *= max(0.0, 1 - max(0.0, dur - busy) / T_COH)
    return {"config": config, "duration": round(dur, 1),
            "move_time": round(move_dur, 1),
            "compile_ms": round((_time.time() - _t0) * 1000, 1),
            "fidelity": round(f1q * f2q * f_tr * coh, 6),
            "f1q": round(f1q, 4), "f2q": round(f2q, 4),
            "f_trans": round(f_tr, 4), "f_coh": round(coh, 4),
            "batches": len(jobs), "transfers": transfers,
            "rounds": n_cz, "qubits": qc.num_qubits,
            "gates2q": sum(n2q) // 2, "gates1q": sum(n1q)}


if __name__ == "__main__":
    qasm, cfg = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "agnostic")
    print(json.dumps(score(qasm, cfg)))
