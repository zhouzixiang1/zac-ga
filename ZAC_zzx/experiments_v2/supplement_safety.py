"""Read-only seals for accepted results used by supplementary studies.

New experiment directories are deliberately absent. This module never deletes,
chmods, rewrites, or promotes an experiment result; it only writes new receipts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
PROTECTED = (
    "ZAC_zzx/results/paper_zh_v2",
    "ZAC_zzx/results/initial_lookahead_v1",
    "fidelity-lookahead-v2/artifacts/native-ga-v1/paper-zh-v1",
    "IEEE_conference_template/figures/data",
    "IEEE_conference_template/results_values_zh.tex",
)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def inventory(repo=REPO, targets=PROTECTED):
    result = {}
    for target in targets:
        path = repo / target
        if not path.exists():
            raise FileNotFoundError(path)
        files = sorted(p for p in path.rglob("*") if p.is_file()) if path.is_dir() else [path]
        for item in files:
            result[str(item.relative_to(repo))] = {"sha256": digest(item), "bytes": item.stat().st_size}
    return result


def write_new(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")


def compare(expected, actual):
    return {
        "missing": sorted(expected.keys() - actual.keys()),
        "added": sorted(actual.keys() - expected.keys()),
        "changed": sorted(k for k in expected.keys() & actual.keys() if expected[k] != actual[k]),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("seal", "check"))
    parser.add_argument("--seal", required=True)
    parser.add_argument("--receipt")
    args = parser.parse_args(argv)
    seal_path = Path(args.seal).resolve()
    allowed = REPO / "ZAC_zzx/results/supplementary_20260907"
    if allowed not in seal_path.parents:
        raise ValueError("preservation receipts belong in results/supplementary_20260907")
    if args.action == "seal":
        files = inventory()
        write_new(seal_path, {"schema": 1, "purpose": "preserve accepted results before supplementary experiments",
                              "protected": list(PROTECTED), "files": files})
        print(json.dumps({"sealed_files": len(files), "seal_sha256": digest(seal_path)}))
        return 0
    seal = json.loads(seal_path.read_text())
    delta = compare(seal["files"], inventory(targets=seal["protected"]))
    ok = not any(delta.values())
    result = {"unchanged": ok, "seal_sha256": digest(seal_path), "checked_files": len(seal["files"]), **delta}
    if args.receipt:
        receipt = Path(args.receipt).resolve()
        if allowed not in receipt.parents:
            raise ValueError("check receipt must remain in supplementary_20260907")
        write_new(receipt, result)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
