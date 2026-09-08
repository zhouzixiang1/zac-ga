"""Count frozen mapping choices on the strictly complete linked cohort only."""
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent
LINKED_SHA = "1fe755f84c98fc6ed00f5313c7eb8aba934550d87edf092c8b1e2c12186b4ed3"


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    source = ROOT / "linked39_analysis.json"
    if digest(source) != LINKED_SHA:
        raise ValueError("linked analysis changed")
    summary = json.loads(source.read_text())
    complete = [row for row in summary["per_circuit"] if row["complete"]]
    if len(complete) != 36:
        raise ValueError("complete cohort changed")
    rows, input_hashes = [], {}
    for circuit in complete:
        for seed in (0, 1, 2):
            job = Path(circuit["source_root"]) / "jobs" / f"{circuit['circuit']}-s{seed}"
            results = {}
            for arm in ("sa_reference", "h0", "h2"):
                path = job / f"{arm}_result.json"
                input_hashes[str(path)] = digest(path)
                results[arm] = json.loads(path.read_text())
            maps = {arm: row["selected_mapping_sha256"] for arm, row in results.items()}
            rows.append({"circuit": circuit["circuit"], "seed": seed,
                "source_root": circuit["source_root"],
                "h0_h2_same_mapping": maps["h0"] == maps["h2"],
                "h0_retains_sa": maps["h0"] == maps["sa_reference"],
                "h2_retains_sa": maps["h2"] == maps["sa_reference"],
                "mapping_sha256": maps})
    if len(rows) != 108:
        raise ValueError("complete-pair count changed")
    report = {"analysis_unit": "mapping choice per seed within the 36 complete circuits; not 108 independent circuits",
        "complete_circuits": 36, "paired_seeds": 108,
        "h0_h2_same_mapping": sum(row["h0_h2_same_mapping"] for row in rows),
        "h0_retains_sa": sum(row["h0_retains_sa"] for row in rows),
        "h2_retains_sa": sum(row["h2_retains_sa"] for row in rows),
        "both_retain_sa": sum(row["h0_retains_sa"] and row["h2_retains_sa"] for row in rows),
        "per_pair": rows, "result_file_sha256": input_hashes,
        "linked39_sha256": LINKED_SHA, "analysis_source_sha256": digest(__file__),
        "scope": "Descriptive mechanism audit after results were frozen. Does not change cohort, choices or performance summaries."}
    with (ROOT / "mapping_choices_analysis.json").open("x", encoding="utf-8") as stream:
        json.dump(report, stream, sort_keys=True, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps({k: v for k, v in report.items() if k not in ("per_pair", "result_file_sha256")}, indent=2))


if __name__ == "__main__":
    main()
