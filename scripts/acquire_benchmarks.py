"""Acquire and normalize hash-pinned ZAC18/QMAP154 inputs; default is read-only.

The old manifests did not record upstream commits. Locator refs are therefore
not presented as historical version identity: both original and canonical bytes
must match the published content hashes, or the attempt fails without overwrite.
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import re
import shutil
import sys
from urllib.parse import quote
from urllib.request import Request, urlopen

from portable_reproduce import ROOT, local_file, sha, write


MANIFEST = Path("docs/benchmark_acquisition_manifest.json")
HOSTS = {"zac18": "UCLA-VAST/ZAC", "qmap154": "munich-quantum-toolkit/qmap"}


def read_manifest(root):
    value = json.loads(local_file(root, MANIFEST).read_text())
    rows = value["files"]
    if Counter(row["dataset"] for row in rows) != {"zac18": 18, "qmap154": 154}:
        raise ValueError("the published 18/154 input inventory changed")
    if len({(row["dataset"], row["filename"]) for row in rows}) != 172:
        raise ValueError("duplicate input identities")
    for row in rows:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+\.qasm", row["filename"]):
            raise ValueError("unsafe input filename")
        for key in ("source_sha256", "canonical_sha256"):
            if not re.fullmatch(r"[a-f0-9]{64}", row[key]):
                raise ValueError("invalid content digest")
    if value["normalization"] != {
        "qiskit_version": "1.2.4", "basis_gates": ["cz", "u1", "u2", "u3"],
        "optimization_level": 3, "seed_transpiler": 0,
        "canonical_profile": "main_qiskit_1_2_4_opt3", "canonicalizer_version": "qiskit-transpile-v1"}:
        raise ValueError("unregistered canonicalization settings")
    return value


def upstream_url(dataset, ref, relative):
    if dataset not in HOSTS or not re.fullmatch(r"[A-Za-z0-9_./-]+", ref) or ".." in ref.split("/"):
        raise ValueError("invalid official upstream locator")
    if relative.startswith("/") or ".." in relative.split("/"):
        raise ValueError("invalid upstream relative path")
    return f"https://raw.githubusercontent.com/{HOSTS[dataset]}/{quote(ref, safe='')}/{quote(relative, safe='/')}"


def fetch(url, destination):
    request = Request(url, headers={"User-Agent": "GA-LK-benchmark-acquisition/1"})
    with urlopen(request, timeout=60) as response, destination.open("xb") as target:
        if not response.url.startswith("https://raw.githubusercontent.com/"):
            raise ValueError("upstream redirected outside the expected content host")
        total = 0
        while block := response.read(1024 * 1024):
            total += len(block)
            if total > 64 * 1024**2:
                raise ValueError("unexpectedly large benchmark download")
            target.write(block)


def acquire(root, name, *, download=False, refs=None, source_dirs=None):
    manifest = read_manifest(root)
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", name):
        raise ValueError("a simple new acquisition name is required")
    if bool(download) == bool(source_dirs):
        raise ValueError("choose official download OR both original source directories")
    if not download and set(source_dirs or {}) != set(HOSTS):
        raise ValueError("both original source directories are required")
    refs = refs or {ds: "main" for ds in HOSTS}
    for ds in HOSTS:
        upstream_url(ds, refs[ds], "LICENSE")
    import qiskit
    if qiskit.__version__ != "1.2.4":
        raise ValueError("invoke this script with the bootstrap environment's Qiskit 1.2.4")
    sys.path.insert(0, str(root / "ZAC_zzx"))
    from experiments_v2.canonicalize import canonicalize_circuit
    from experiments_v2 import canonicalize
    if root / "ZAC_zzx" not in Path(canonicalize.__file__).resolve().parents:
        raise ValueError("canonicalizer came from outside this source root")
    output = local_file(root, Path("IEEE_conference_template/build/benchmark-inputs") / name)
    output.mkdir(parents=True, exist_ok=False)
    write(output / "started.json", {"schema": "zac-benchmark-acquisition-v1",
          "manifest_sha256": sha(root / MANIFEST), "download": download,
          "locator_refs": refs if download else None, "upstream_commit_recorded": False,
          "canonicalizer_sha256": sha(Path(canonicalize.__file__))})
    records = []
    for ds in HOSTS:
        (output / "raw" / ds).mkdir(parents=True)
        (output / "canonical" / ds).mkdir(parents=True)
        if download:
            fetch(upstream_url(ds, refs[ds], "LICENSE"), output / "raw" / ds / "UPSTREAM_LICENSE")
    for index, row in enumerate(manifest["files"]):
        ds, filename = row["dataset"], row["filename"]
        raw = output / "raw" / ds / filename
        if download:
            fetch(upstream_url(ds, refs[ds], manifest["datasets"][ds]["subdirectory"] + "/" + filename), raw)
        else:
            original = local_file(Path(source_dirs[ds]).resolve(), filename)
            if not original.is_file() or sha(original) != row["source_sha256"]:
                raise ValueError(f"source missing or different: {ds}/{filename}")
            shutil.copyfile(original, raw)
        if sha(raw) != row["source_sha256"]:
            raise ValueError(f"upstream bytes differ from accepted input: {ds}/{filename}; retain failed attempt")
        canonical = output / "canonical" / ds / filename
        value = canonicalize_circuit(raw, canonical, require_qiskit_version="1.2.4",
                                      optimization_level=3, seed_transpiler=0)
        if value.canonical_sha256 != row["canonical_sha256"] or sha(canonical) != row["canonical_sha256"]:
            raise ValueError(f"normalization differs from accepted input: {ds}/{filename}")
        record = {**row, "raw": str(raw.relative_to(output)),
                  "canonical": str(canonical.relative_to(output)), "verified": True}
        records.append(record)
        write(canonical.with_suffix(".receipt.json"), record)
        print(f"verified {index + 1}/172: {ds}/{filename}", flush=True)
    result = {"schema": "zac-benchmark-acquisition-v1", "status": "verified",
              "manifest_sha256": sha(root / MANIFEST), "files": records,
              "counts": dict(Counter(row["dataset"] for row in records)),
              "historical_compiler_runs_reproduced": False,
              "upstream_commit_recorded": False,
              "license_notices": {ds: sha(output / "raw" / ds / "UPSTREAM_LICENSE") for ds in HOSTS} if download else {},
              "notice": "Local source mode does not grant third-party redistribution rights."}
    write(output / "inputs.json", result)
    return {"status": "verified", "output": str(output), "counts": result["counts"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--name")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--zac-ref", default="main")
    parser.add_argument("--qmap-ref", default="main")
    parser.add_argument("--source-zac", type=Path)
    parser.add_argument("--source-qmap", type=Path)
    args = parser.parse_args(); root = args.root.resolve()
    if not args.execute:
        data = read_manifest(root)
        print(json.dumps({"status": "preview", "writes": False,
                          "counts": dict(Counter(r["dataset"] for r in data["files"])),
                          "identity": data["identity_policy"]}, indent=2))
    else:
        if not args.name:
            parser.error("--execute requires a new --name")
        dirs = None if not (args.source_zac or args.source_qmap) else {"zac18": args.source_zac, "qmap154": args.source_qmap}
        if dirs and any(v is None for v in dirs.values()):
            parser.error("both --source-zac and --source-qmap are required")
        print(json.dumps(acquire(root, args.name, download=args.download,
                                refs={"zac18": args.zac_ref, "qmap154": args.qmap_ref},
                                source_dirs=dirs), indent=2))
