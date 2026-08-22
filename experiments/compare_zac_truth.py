"""Compare ZAC repro run (result/zac/repro/qasm_sa_1000_reuse) against HPCA'25 paper truth."""
import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULT = ROOT / "ZAC" / "result" / "zac" / "repro_fixed"
TRUTH = ROOT / "experiments" / "paper_truth" / "zac.csv"

truth = {r["circuit"]: (float(r["fidelity"]), float(r["duration_us"]))
         for r in csv.DictReader(open(TRUTH))}

print(f"{'circuit':22s} {'fid(ours)':>10s} {'fid(paper)':>10s} {'Δfid':>9s}   "
      f"{'dur(ours)':>10s} {'dur(paper)':>10s} {'Δdur%':>7s}")
n_bad = 0
result_files = {p.name.replace("_fidelity.json", ""): p for p in (RESULT / "fidelity").glob("*_fidelity.json")}
for circuit in sorted(truth, key=lambda c: (c.split("_n")[0], int(c.split("_n")[1].split("_")[0]))):
    key = circuit if circuit in result_files else circuit + "_transpiled"
    fj = result_files.get(key)
    if not fj.exists():
        print(f"{circuit:22s} MISSING")
        n_bad += 1
        continue
    r = json.load(open(fj))
    fid, dur = r["cir_fidelity"], r["cir_duration"]
    tf, td = truth[circuit]
    dfid = fid - tf
    ddur = (dur - td) / td * 100
    flag = "" if abs(dfid) < 5e-3 and abs(ddur) < 0.5 else "  <-- MISMATCH"
    if flag:
        n_bad += 1
    print(f"{circuit:22s} {fid:10.6f} {tf:10.6f} {dfid:+9.5f}   {dur:10.1f} {td:10.1f} {ddur:+7.2f}%{flag}")
print(f"\n{'ALL MATCH' if n_bad == 0 else f'{n_bad} mismatches'}")
