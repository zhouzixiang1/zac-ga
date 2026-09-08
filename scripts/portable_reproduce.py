"""Portable current-source demonstration and checked-in artifact audit.

No command installs into an existing interpreter or overwrites a run. All
generated material lives below IEEE_conference_template/build/. A new native
wheel is a current-source build, never an attestation of the accepted paper runs.
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import random
import re
import shutil
import subprocess
import sys
import time
import venv


BUILD = Path("IEEE_conference_template/build/portable")
CONFIG = Path("ZAC_zzx/exp_setting/ga_lk_default.json")
REQUIREMENTS = Path("scripts/requirements-current-source.txt")
RESULTS = Path("ZAC_zzx/results/paper_zh_v2")
ROOT = Path(__file__).resolve().parents[1]
SCOPES = (
    "scripts", "docs", "IEEE_conference_template", "ZAC_zzx/zzx",
    "ZAC_zzx/zac", "ZAC_zzx/evaluation", "ZAC_zzx/streaming",
    "ZAC_zzx/experiments_v2", "ZAC_zzx/native", "ZAC_zzx/hardware_spec",
    "ZAC_zzx/benchmark/toy_example", "ZAC_zzx/results/paper_zh_v2",
)
SUFFIXES = {".py", ".cpp", ".hpp", ".h", ".json", ".toml", ".txt", ".qasm",
            ".csv", ".xlsx", ".tex", ".bib", ".cls", ".sty", ".bst", ".dat", ".md"}
EXCLUDED_PARTS = {".git", "build", "__pycache__", ".pytest_cache", ".venv", "node_modules"}
PUBLICATION_BUNDLE = Path("ZAC_zzx/results/default_initial_v1/paper_exports")
PUBLICATION_FILES = ("default_initial_values.json", "default_initial_values.tex", "main_rows.csv",
                     "analysis_units.csv", "mechanism.csv", "representative_cases.tex")
PUBLICATION_INPUTS = {
    "../" + (PUBLICATION_BUNDLE / name).as_posix(): "publication/default_initial_v1/" + name
    for name in ("default_initial_values.tex", "representative_cases.tex")
}


def sha(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")


def safe_relative(value):
    path = Path(value)
    if path.is_absolute() or not path.parts or any(p in {".", ".."} for p in path.parts):
        raise ValueError("expected a nonempty relative path without traversal")
    return path


def local_file(root, relative):
    path = root / safe_relative(relative)
    current = root
    for part in path.relative_to(root).parts:
        current /= part
        if current.is_symlink():
            raise ValueError(f"symbolic links are not accepted: {relative}")
    return path


def new_output(root, name):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", name):
        raise ValueError("run name must contain lowercase letters, digits, hyphens or underscores")
    path = local_file(root, BUILD / name)
    path.mkdir(parents=True, exist_ok=False)
    return path


def publication_files(root):
    """Return the exact companion bundle when the Chinese manuscript uses it.

    This is a source-packaging contract, not a waiver of the independent
    historical-evidence audit. Older manuscripts and toy fixtures without these
    inputs need no companion bundle. Any other escaping input is rejected.
    """
    main = local_file(root, "IEEE_conference_template/paper_zh.tex")
    if not main.exists():
        return []
    source = re.sub(r"(?<!\\)%[^\n]*", "", main.read_text(encoding="utf-8"))
    inputs = re.findall(r"\\(?:input|include)\s*\{([^}]+)\}", source)
    for name in inputs:
        if name in PUBLICATION_INPUTS:
            continue
        # Do not resolve arbitrary parent paths against the filesystem.
        safe_relative(name)
        if "\\" in name or name != Path(name).as_posix():
            raise ValueError("noncanonical manuscript input path")
    referenced = [name for name in inputs if name in PUBLICATION_INPUTS]
    if not referenced:
        return []
    if sorted(referenced) != sorted(PUBLICATION_INPUTS):
        raise ValueError("the publication bundle requires both exact TeX inputs once")
    files = [(PUBLICATION_BUNDLE / name).as_posix() for name in PUBLICATION_FILES]
    for name in files:
        if not local_file(root, name).is_file():
            raise ValueError(f"required publication source file is missing: {name}")
    return files


def source_files(root):
    selected = {"README.md", "Makefile", "ZAC/LICENSE", "ZAC_zzx/README.md",
                "ZAC_zzx/run.py", "ZAC_zzx/verify_batches.py", str(CONFIG), str(REQUIREMENTS)}
    for scope in SCOPES:
        base = local_file(root, scope)
        if not base.is_dir():
            raise ValueError(f"source directory is missing: {scope}")
        for directory, names, filenames in os.walk(base, followlinks=False):
            names[:] = sorted(n for n in names if n not in EXCLUDED_PARTS)
            for name in [*names, *filenames]:
                path = Path(directory) / name
                if path.is_symlink():
                    raise ValueError(f"unexpected source symlink: {path.relative_to(root)}")
            for name in filenames:
                path = Path(directory) / name
                if path.suffix in SUFFIXES or path.name == "CMakeLists.txt":
                    selected.add(path.relative_to(root).as_posix())
    selected.update(publication_files(root))
    for name in sorted(selected):
        if not local_file(root, name).is_file():
            raise ValueError(f"required source file is missing: {name}")
    return sorted(selected)


def export_source(root, name):
    files = source_files(root)
    before = {path: sha(root / path) for path in files}
    output = new_output(root, name)
    destination = output / "source"
    destination.mkdir()
    for relative in files:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(local_file(root, relative), target)
        if sha(target) != before[relative] or sha(root / relative) != before[relative]:
            raise RuntimeError(f"source changed during export: {relative}; keep this failed export")
    if {path: sha(local_file(root, path)) for path in files} != before:
        raise RuntimeError("source changed during export; keep this failed export")
    manifest = {"schema": "zac-portable-source-v1", "files": before,
                "scope": "current-source-demo-and-checked-in-accepted-artifacts",
                "historical_raw_runs_included": False, "third_party_pdfs_included": False,
                "new_code_license": "not-selected", "upstream_license": "ZAC/LICENSE"}
    write(destination / "portable_source_manifest.json", manifest)
    return {"status": "exported", "source": str(destination), "files": len(files),
            "manifest_sha256": sha(destination / "portable_source_manifest.json")}


def execution_files(root):
    return {path: sha(root / path) for path in source_files(root)
            if (path.startswith("ZAC_zzx/") and "/results/" not in path)
            or path in {"scripts/portable_reproduce.py", str(REQUIREMENTS)}}


def runtime_env(root, output):
    env = {k: v for k, v in os.environ.items()
           if k not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}}
    env.update(PYTHONPATH=str(root / "ZAC_zzx"), PYTHONDONTWRITEBYTECODE="1",
               PYTHONHASHSEED="0", MPLBACKEND="Agg", MPLCONFIGDIR=str(output / "mpl"),
               OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1",
               TMPDIR=str(output / "tmp"), TEMP=str(output / "tmp"), TMP=str(output / "tmp"),
               PIP_CACHE_DIR=str(output / "pip-cache"), CMAKE_BUILD_PARALLEL_LEVEL="2")
    (output / "tmp").mkdir(exist_ok=True)
    return env


def logged(command, output, label, *, cwd, env):
    print(f"[{label}] {' '.join(map(str, command[:4]))}", flush=True)
    with (output / (label + ".log")).open("x") as stream:
        result = subprocess.run(list(map(str, command)), cwd=cwd, env=env,
                                stdout=stream, stderr=subprocess.STDOUT)
    if result.returncode:
        raise RuntimeError(f"{label} failed; inspect {output / (label + '.log')}; no automatic retry")


def environment_python(output):
    return output / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def bootstrap(root, name):
    if sys.version_info[:2] not in {(3, 10), (3, 11), (3, 12)}:
        raise ValueError("use CPython 3.10, 3.11 or 3.12 for the pinned Qiskit stack")
    if platform.python_implementation() != "CPython":
        raise ValueError("the current-source build is tested with CPython")
    if not shutil.which("cmake"):
        raise ValueError("CMake >=3.20 and a C++17 toolchain must be installed first")
    original = execution_files(root)
    output = new_output(root, name)
    write(output / "started.json", {"schema": "zac-current-bootstrap-v1",
          "source_hashes": original, "requirements_sha256": sha(root / REQUIREMENTS),
          "python": platform.python_version(), "platform": platform.platform(),
          "historical_experiment_reproduction": False})
    venv.EnvBuilder(with_pip=True, system_site_packages=False).create(output / "venv")
    python = environment_python(output)
    env = runtime_env(root, output)
    logged([python, "-B", "-m", "pip", "install", "--disable-pip-version-check", "-r", root / REQUIREMENTS],
           output, "install-dependencies", cwd=root, env=env)
    wheels = output / "wheels"
    wheels.mkdir()
    logged([python, "-B", "-m", "build", "--wheel", "--no-isolation", "--outdir", wheels,
            "-Cbuild-dir=" + str(output / "native"), root / "ZAC_zzx/native"],
           output, "build-native", cwd=root, env=env)
    found = list(wheels.glob("zac_native-*.whl"))
    if len(found) != 1:
        raise RuntimeError("expected exactly one newly built native wheel")
    wheel = found[0]
    logged([python, "-B", "-m", "pip", "install", "--no-deps", wheel],
           output, "install-native", cwd=root, env=env)
    probe = subprocess.check_output([str(python), "-B", "-c",
        "import json,sys; from zzx.native_backend import register_native_wheel,build_info; "
        "register_native_wheel(sys.argv[1]); print(json.dumps(build_info(require_registered_wheel=True)))",
        str(wheel)], cwd=root, env=env, text=True)
    native = json.loads(probe)
    if native["native_abi_version"] != 9 or native["native_wheel_sha256"] != sha(wheel):
        raise RuntimeError("native ABI or byte registration mismatch")
    config = load(root / CONFIG)
    config["zac_setting"][0]["native_wheel_sha256"] = sha(wheel)
    config["zac_setting"][0]["dir"] = "../" + (BUILD / name / "manual-run").as_posix() + "/"
    write(output / "current_config.json", config)
    resolved = subprocess.check_output([str(python), "-B", "-m", "pip", "freeze"],
                                       env=env, cwd=root, text=True)
    with (output / "resolved-packages.txt").open("x") as stream:
        stream.write(resolved)
    if execution_files(root) != original:
        raise RuntimeError("source changed during bootstrap; this build cannot be accepted")
    receipt = {"schema": "zac-current-bootstrap-v1", "status": "ready",
        "runtime": str(output.relative_to(root)), "source_hashes": original,
        "wheel": str(wheel.relative_to(root)), "wheel_sha256": sha(wheel),
        "config_sha256": sha(output / "current_config.json"), "native": native,
        "requirements_sha256": sha(root / REQUIREMENTS),
        "resolved_packages_sha256": sha(output / "resolved-packages.txt"),
        "historical_experiment_reproduction": False}
    write(output / "bootstrap.json", receipt)
    return {"status": "ready", "runtime": str(output), "wheel_sha256": sha(wheel),
            "next": f"python scripts/portable_reproduce.py smoke --runtime {name} --name smoke-v1"}


def check_runtime(root, name):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", name):
        raise ValueError("invalid runtime name")
    output = local_file(root, BUILD / name)
    receipt = load(output / "bootstrap.json")
    if receipt.get("status") != "ready" or receipt.get("runtime") != str(BUILD / name):
        raise ValueError("runtime receipt mismatch")
    if receipt["source_hashes"] != execution_files(root):
        raise ValueError("current source differs from the built source; use a new bootstrap name")
    for path, expected in [(root / REQUIREMENTS, receipt["requirements_sha256"]),
                           (output / "current_config.json", receipt["config_sha256"]),
                           (output / "resolved-packages.txt", receipt["resolved_packages_sha256"]),
                           (local_file(root, receipt["wheel"]), receipt["wheel_sha256"])]:
        if sha(path) != expected:
            raise ValueError("runtime artifact hash mismatch")
    return output, receipt


def gate_order_check(qasm, events):
    from qiskit import QuantumCircuit
    circuit = QuantumCircuit.from_qasm_file(str(qasm))
    expected = [[] for _ in range(circuit.num_qubits)]
    observed = [[] for _ in expected]
    for instruction in circuit.data:
        qubits = [circuit.find_bit(q).index for q in instruction.qubits]
        if instruction.operation.name == "cz" and len(qubits) == 2:
            a, b = qubits
            expected[a].append(f"cz:{b}"); expected[b].append(f"cz:{a}")
        elif len(qubits) == 1 and instruction.operation.name not in {"measure", "reset", "barrier"}:
            expected[qubits[0]].append("1q")
        else:
            raise ValueError("portable smoke accepts only one-qubit unitaries and CZ")
    for event in events:
        if event.kind == "one_qubit_gate":
            for atom in event.atoms:
                observed[atom].append("1q")
        elif event.kind == "two_qubit_gate":
            for a, b in event.gate_pairs:
                observed[a].append(f"cz:{b}"); observed[b].append(f"cz:{a}")
    if expected != observed:
        raise ValueError("independent QASM per-qubit gate order differs from ZAIR")
    return {"ok": True, "qubits": circuit.num_qubits,
            "scope": "per-qubit 1Q/CZ order and CZ partners; not a unitary-equivalence proof"}


def smoke_worker(root, runtime, output):
    sys.path.insert(0, str(root / "ZAC_zzx"))
    from evaluation import FidelityModel, normalize_zair, score_trace, validate_trace_physics
    from verify_batches import verify
    from zac.ds.architecture import Architecture
    from zzx.native_backend import build_info
    from zzx.zac_zzx import ZAC_zzx
    rt, receipt = check_runtime(root, runtime)
    for module in ("evaluation", "verify_batches", "zzx.zac_zzx", "zac.ds.architecture"):
        if root / "ZAC_zzx" not in Path(sys.modules[module].__file__).resolve().parents:
            raise RuntimeError("compiler/verifier imported outside this source export")
    native = build_info(require_registered_wheel=True, expected_wheel_sha256=receipt["wheel_sha256"])
    if native["extension_sha256"] != receipt["native"]["extension_sha256"]:
        raise ValueError("loaded extension differs from the built runtime")
    config = load(rt / "current_config.json")
    setting = deepcopy(config["zac_setting"][0])
    # Exercise the public default, not a private paper wrapper or an explicit
    # initializer override. All other settings remain the public example's.
    setting.pop("init_strategy", None); setting.pop("initial_lookahead", None)
    qasm = local_file(root, Path("ZAC_zzx") / safe_relative(config["qasm_list"][0]))
    architecture_path = local_file(root, Path("ZAC_zzx") / safe_relative(setting["arch_spec"]))
    spec = load(architecture_path)
    setting.update(dir=str(output) + "/", name="portable-smoke")
    random.seed(setting["seed"])
    compiler = ZAC_zzx()
    compiler.parse_setting(setting)
    architecture = Architecture(deepcopy(spec)); architecture.preprocessing()
    compiler.set_architecture(architecture)
    compiler.set_architecture_spec_path(str(architecture_path))
    compiler.set_program(str(qasm))
    started = time.perf_counter()
    trace = compiler.solve(save_file=False)
    elapsed = time.perf_counter() - started
    trace_path = output / "trace.json"
    write(trace_path, trace)
    zair = verify(trace_path, qasm)
    events = tuple(normalize_zair(trace, architecture=spec))
    physical = validate_trace_physics(events, n_qubits=compiler.n_q)
    logical = gate_order_check(qasm, events)
    model = FidelityModel()
    score = score_trace(events, model, n_qubits=compiler.n_q).to_dict()
    initialization = compiler.zzx_initial_lookahead_report
    if initialization["policy_id"] != "physical-prefix-initial-v1":
        raise ValueError("public initializer default was not used")
    for key, expected in {"horizon": 2, "candidates": 4, "rho": .7, "rollout_evaluations": 32}.items():
        if initialization["config"][key] != expected:
            raise ValueError("public initializer settings differ from the documented default")
    if any(zair["errors"].values()) or not physical["ok"] or physical["ghost_hits"] != 0:
        raise ValueError("independent physical/ZAIR verification failed")
    if score["ood"] or not math.isfinite(score["log_fidelity"]):
        raise ValueError("smoke score is outside the model domain")
    check_runtime(root, runtime)
    result = {"schema": "zac-current-source-smoke-v1", "status": "passed",
        "historical_experiment_reproduction": False, "performance_claim": False,
        "runtime": str(rt.relative_to(root)), "native": native,
        "qasm": str(qasm.relative_to(root)), "qasm_sha256": sha(qasm),
        "architecture": str(architecture_path.relative_to(root)), "architecture_sha256": sha(architecture_path),
        "trace_sha256": sha(trace_path), "bootstrap_sha256": sha(rt / "bootstrap.json"),
        "configuration": setting, "effective_configuration": compiler.zzx_params,
        "initialization": initialization, "zair_validation": zair,
        "physics_validation": physical, "logical_validation": logical,
        "model": model.to_dict(), "score": score, "compile_wall_s": elapsed}
    write(output / "smoke.json", result)
    return {"status": "passed", "report": str(output / "smoke.json"),
            "move_batches": score["move_batches"], "fidelity": score["fidelity"]}


def smoke(root, runtime, name):
    rt, receipt = check_runtime(root, runtime)
    output = new_output(root, name)
    write(output / "started.json", {"runtime": str(BUILD / runtime),
          "bootstrap_sha256": sha(rt / "bootstrap.json"), "timeout_seconds": 600})
    env = runtime_env(root, output)
    with (output / "compiler.log").open("x") as log:
        subprocess.run([str(environment_python(rt)), "-B", str(root / "scripts/portable_reproduce.py"),
                        "--root", str(root), "_worker", "--runtime", runtime, "--name", name],
                       cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=600)
    result = load(output / "smoke.json")
    if result.get("status") != "passed":
        raise RuntimeError("worker did not pass")
    return {"status": "passed", "report": str(output / "smoke.json"),
            "move_batches": result["score"]["move_batches"], "fidelity": result["score"]["fidelity"]}


def audit_artifacts(root):
    directory = local_file(root, RESULTS)
    manifest = load(directory / "final_manifest.json")
    checked = []
    for relative, expected in manifest["files"].items():
        path = local_file(directory, relative)
        if not path.is_file() or path.stat().st_size != expected["bytes"] or sha(path) != expected["sha256"]:
            raise ValueError(f"accepted artifact missing or changed: {relative}")
        checked.append(relative)
    if not checked:
        raise ValueError("accepted artifact manifest is empty")
    return {"status": "passed", "scope": "checked-in processed files only; no raw-run re-execution",
            "files": len(checked), "manifest_sha256": sha(directory / "final_manifest.json"),
            "historical_raw_runs_verified": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    subs = parser.add_subparsers(dest="command", required=True)
    for command in ("export", "bootstrap"):
        sub = subs.add_parser(command); sub.add_argument("--name", required=True)
    for command in ("smoke", "_worker"):
        sub = subs.add_parser(command); sub.add_argument("--runtime", required=True); sub.add_argument("--name", required=True)
    subs.add_parser("audit-artifacts")
    args = parser.parse_args(); root = args.root.resolve()
    if args.command == "export": result = export_source(root, args.name)
    elif args.command == "bootstrap": result = bootstrap(root, args.name)
    elif args.command == "smoke": result = smoke(root, args.runtime, args.name)
    elif args.command == "_worker": result = smoke_worker(root, args.runtime, local_file(root, BUILD / args.name))
    else: result = audit_artifacts(root)
    print(json.dumps(result, indent=2, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
