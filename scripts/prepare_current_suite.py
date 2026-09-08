"""Prepare a portable, immutable current-default suite configuration.

Preparation never executes benchmarks and is not an accepted-result promotion.
The regular run.py frontend stops on errors; it is not a fault-tolerant formal
experiment coordinator. Use a separately reviewed protocol for paper claims.
"""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import re

from acquire_benchmarks import MANIFEST, read_manifest
from portable_reproduce import ROOT, BUILD, check_runtime, local_file, sha, write


def prepare(root, runtime, inputs, name, dataset="all", seed=0):
    for label in (inputs, name):
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", label):
            raise ValueError("simple input/run names are required")
    if dataset not in {"all", "zac18", "qmap154"} or type(seed) is not int or seed < 0:
        raise ValueError("invalid dataset or seed")
    rt, build = check_runtime(root, runtime)
    input_root = local_file(root, Path("IEEE_conference_template/build/benchmark-inputs") / inputs)
    report = json.loads((input_root / "inputs.json").read_text())
    manifest = read_manifest(root)
    if report.get("status") != "verified" or report.get("manifest_sha256") != sha(root / MANIFEST):
        raise ValueError("input bundle is not verified against the distributed manifest")
    expected = {(row["dataset"], row["filename"]): row for row in manifest["files"]}
    observed = {(row["dataset"], row["filename"]): row for row in report["files"]}
    if set(expected) != set(observed) or len(report["files"]) != 172:
        raise ValueError("input bundle is incomplete or duplicated")
    selected = []
    for key, original in expected.items():
        row = observed[key]
        canonical = local_file(input_root, row["canonical"])
        if row["canonical_sha256"] != original["canonical_sha256"] or sha(canonical) != original["canonical_sha256"]:
            raise ValueError(f"canonical input changed: {key}")
        if dataset == "all" or key[0] == dataset:
            selected.append({"dataset": key[0], "filename": key[1],
                             "canonical": str(canonical.relative_to(root)),
                             "canonical_sha256": original["canonical_sha256"]})
    output = local_file(root, Path("IEEE_conference_template/build/current-suite") / name)
    output.mkdir(parents=True, exist_ok=False)
    config = json.loads((rt / "current_config.json").read_text())
    config["qasm_list"] = ["../" + row["canonical"] for row in selected]
    config["zac_setting"][0].update(seed=seed, dir="../" + str(output.relative_to(root)) + "/outputs/")
    write(output / "config.json", config)
    protocol = {"schema": "zac-current-suite-preparation-v1", "status": "prepared-not-executed",
                "dataset": dataset, "seed": seed, "jobs": selected,
                "runtime": str(BUILD / runtime), "bootstrap_sha256": sha(rt / "bootstrap.json"),
                "inputs_manifest_sha256": sha(input_root / "inputs.json"),
                "source_hashes": build["source_hashes"], "config_sha256": sha(output / "config.json"),
                "current_default": True, "historical_reproduction": False,
                "formal_paper_result": False, "automatic_promotion": False,
                "execution_policy": "run.py is a fail-fast frontend; it does not resume failures or provide a full statistical acceptance pipeline"}
    write(output / "protocol.json", protocol)
    python = (BUILD / runtime / "venv/bin/python").as_posix()
    return {"status": "prepared-not-executed", "jobs": len(selected), "output": str(output),
            "posix_command": f"{python} -B ZAC_zzx/run.py {output.relative_to(root)}/config.json",
            "requirement": "Verify each emitted ZAIR against its corresponding canonical QASM; do not call preparation or raw compilation a paper-result acceptance."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--inputs", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--dataset", choices=("all", "zac18", "qmap154"), default="all")
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    print(json.dumps(prepare(args.root.resolve(), args.runtime, args.inputs, args.name, args.dataset, args.seed), indent=2))
