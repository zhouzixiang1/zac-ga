"""Parse ZAIR code JSON (ZAC / GA outputs) into unified metrics.

Steps semantics aligned with the qmap side (naviz_eval): one rearrangeJob
instruction = one rearrangement step. Duration of a job = end_time - begin_time.
"""
from __future__ import annotations

import json
from pathlib import Path


def parse_zair(code_path: str | Path) -> dict:
    d = json.load(open(code_path))
    insts = d["instructions"]
    jobs = [i for i in insts if i["type"] == "rearrangeJob"]
    ryd = [i for i in insts if i["type"] == "rydberg"]
    one_q = [i for i in insts if i["type"] == "1qGate"]
    # serial rearrangement length: sum of job spans (single AOD => serial)
    duration = sum(i.get("end_time", 0) - i.get("begin_time", 0) for i in jobs)
    n_transfers = 0
    for i in jobs:
        for detail in i.get("insts", []):
            t = detail.get("type", "").split(":")[0]
            if t in ("activate", "deactivate"):
                n_transfers += 1
    return {
        "steps": len(jobs),
        "rearrangement_jobs_duration": round(duration, 1),
        "n_transfer_instructions": n_transfers,
        "runtime": d.get("runtime"),
        "rydberg_layers": len(ryd),
        "two_qubit_gates": sum(len(i.get("gates", [])) for i in ryd),
        "one_qubit_instructions": len(one_q),
    }


def load_run(result_dir: str | Path) -> dict[str, dict]:
    """{circuit_name(no _transpiled): {code metrics + fidelity + times}}"""
    result_dir = Path(result_dir)
    out = {}
    for f in sorted((result_dir / "code").glob("*_code.json")):
        name = f.name.replace("_code.json", "").replace("_transpiled", "")
        entry = parse_zair(f)
        fj = result_dir / "fidelity" / f"{f.name.replace('_code.json', '')}_fidelity.json"
        if fj.exists():
            fid = json.load(open(fj))
            entry["fidelity"] = fid["cir_fidelity"]
            entry["duration_us"] = fid["cir_duration"]
        tj = result_dir / "time" / f"{f.name.replace('_code.json', '')}_time.json"
        if tj.exists():
            entry["compile_times"] = json.load(open(tj))
        out[name] = entry
    return out
