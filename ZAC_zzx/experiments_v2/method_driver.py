"""One-attempt compiler adapters used by the Schema-2 process runner.

This module writes only beneath ``ZAC_RUN_DIR``.  It does not score fidelity;
the parent runner invokes the independent event normaliser and scorer after the
compiler exits.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict

from .ablation import validate_ablation_config


ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(REPO / "experiments"))


M2_FROZEN_CONFIG: Dict[str, Any] = {
    "compiler": "RoutingAwareCompiler",
    "deepening_factor": 0.6,
    "deepening_value": 0.2,
    "experiment_schema": 2,
    "fallback": False,
    "lookahead_factor": 0.2,
    "max_nodes": 50_000_000,
    "method_id": "iccad_qmap_3_2_astar",
    "mqt_core_version": "3.1.0",
    "mqt_qmap_version": "3.2.0",
    "reuse_level": 5.0,
}


def _load(path: str | Path) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _run_dir() -> Path:
    value = os.environ.get("ZAC_RUN_DIR")
    if not value:
        raise RuntimeError("ZAC_RUN_DIR is required")
    path = Path(value).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_json(path: Path, payload: object) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def compile_zac(input_path: Path, config_path: Path, architecture_path: Path,
                method: str, *, run_kind: str = "", ablation_variant: str = "") -> None:
    import qiskit
    from zac.ds.architecture import Architecture
    from zac.zac import ZAC
    from zzx.zac_zzx import ZAC_zzx

    if method not in ("M1", "M3", "M4"):
        raise ValueError(f"ZAC driver cannot run method {method}")
    if qiskit.__version__ != "1.2.4":
        raise RuntimeError(
            f"M1/M3/M4 require qiskit==1.2.4, found {qiskit.__version__}")
    output = _run_dir()
    spec = _load(architecture_path)
    architecture = Architecture(spec)
    architecture.preprocessing()
    # Formal M1 keeps ZAC's placement and greedy maximal-independent-set
    # routing decisions, then passes each proposed batch through the same
    # strict expanded-phase legality repair used by M3/M4.  Raw ZAC does not
    # model stationary-atom ghost intersections; scoring an invalid raw trace
    # would make the baseline undefined on most of HPCA18.
    compiler = ZAC_zzx() if method in ("M1", "M3", "M4") else ZAC()
    user_config = _load(config_path)
    ablation_controls: Dict[str, Any] | None = None
    if run_kind == "ablation":
        if method not in ("M3", "M4") or not ablation_variant:
            raise ValueError("ablation attempts require M3/M4 and a registered variant")
        user_config, variant = validate_ablation_config(
            user_config, expected_variant=ablation_variant,
            expected_method=method)
        ablation_controls = variant.controls()
    elif ablation_variant or user_config.get("run_kind") == "ablation":
        raise ValueError("ablation controls are forbidden outside run_kind=ablation")
    if "zac_setting" in user_config:
        settings = user_config["zac_setting"]
        if not isinstance(settings, list) or len(settings) != 1:
            raise ValueError("Schema-2 method config must contain exactly one zac_setting")
        user_config = dict(settings[0])
    if user_config.get("experiment_schema", 2) != 2:
        raise ValueError("only Schema-2 configuration is accepted")

    setting: Dict[str, Any] = {
        "name": input_path.stem,
        "dir": str(output) + os.sep,
        "arch_spec": str(architecture_path.resolve()),
        "dependency": True,
        "scheduling": "asap",
        "trivial_placement": False,
        "dynamic_placement": True,
        "use_window": True,
        "window_size": 1000,
        "reuse": True,
        # The independent Schema-2 event verifier runs outside the timed core.
        "use_verifier": False,
        "resyn": False,
    }
    if method == "M1":
        setting.update(placer="zac", routing_strategy="greedy")
        if user_config != {"experiment_schema": 2, "method_id": "M1"}:
            raise ValueError(
                "M1 config must contain only experiment_schema=2 and method_id=M1")
    else:
        setting.update(placer="resident", routing_strategy="coloring")
        setting.update(user_config)
        expected = 0 if method == "M3" else 2
        if setting.get("lookahead_horizon") != expected:
            raise ValueError(f"{method} requires lookahead_horizon={expected}")
        expected_id = "ours_nl" if method == "M3" else "ours_lk"
        if setting.get("method_id") != expected_id:
            raise ValueError(f"method_id/config mismatch for {method}")

    # Provenance fields in the frozen config are validated above, but an
    # attempt may never escape its unique runner directory or substitute a
    # different architecture via those fields.
    setting["name"] = input_path.stem
    setting["dir"] = str(output) + os.sep
    setting["arch_spec"] = str(architecture_path.resolve())

    compiler.parse_setting(setting)
    if ablation_controls is not None:
        # Inject only after the immutable main setting passed Schema-2 validation.
        # Main runs therefore have no code path that can consume these controls.
        compiler.zzx_params["ablation_policy"] = ablation_controls["decision_policy"]
        compiler.zzx_params["ablation_fitness_mode"] = \
            ablation_controls["fitness_phase_mode"]
        compiler.routing_strategy = ablation_controls["routing_batcher"]
    compiler.set_architecture_spec_path(str(architecture_path.resolve()))
    compiler.set_architecture(architecture)
    compiler.set_program(str(input_path.resolve()))
    core_wall_start = time.perf_counter_ns()
    core_cpu_start = time.process_time_ns()
    native_payload = compiler.solve(save_file=False)
    core_cpu_ns = time.process_time_ns() - core_cpu_start
    core_wall_ns = time.perf_counter_ns() - core_wall_start
    _write_json(output / "compiler_timing.json", {
        "compiler_time_ns": core_wall_ns,
        "cpu_time_ns": core_cpu_ns,
        "definition": "compiler scheduling through native routing; excludes input/arch parse and scoring",
    })
    _write_json(output / "trace.zair.json", native_payload)
    _write_json(output / "compiler_stats.json", {
        "runtime_analysis": compiler.runtime_analysis,
        "qiskit_version": qiskit.__version__,
        "python_version": sys.version.split()[0],
        "qubits": compiler.n_q,
        "gates_2q": compiler.n_g,
        "decision_log": getattr(compiler, "zzx_decision_log", []),
        "route_log": getattr(compiler, "zzx_route_log", []),
        "ghost_splits": getattr(compiler, "zzx_ghost_splits", 0),
        "run_kind": run_kind or "unregistered",
        "ablation_variant": ablation_variant or None,
        "ablation_controls": ablation_controls,
        "physicalization": (
            "common_expanded_phase_ghost_safe_repair"
            if method in ("M1", "M3", "M4") else None
        ),
    })


def _validated_m2_config(config_path: Path) -> Dict[str, Any]:
    config = _load(config_path)
    if config != M2_FROZEN_CONFIG:
        differences = {
            key: (config.get(key), M2_FROZEN_CONFIG.get(key))
            for key in sorted(set(config) | set(M2_FROZEN_CONFIG))
            if config.get(key) != M2_FROZEN_CONFIG.get(key)
        }
        raise ValueError(f"M2 frozen routing-aware configuration mismatch: {differences}")
    return config


def compile_qmap(input_path: Path, config_path: Path,
                 architecture_path: Path) -> None:
    """Run only QMAP 3.2 routing-aware A* with strict routing."""
    import mqt.core
    import mqt.qmap
    from mqt.core import load
    from mqt.qmap.na.zoned import RoutingAwareCompiler, ZonedNeutralAtomArchitecture
    from qiskit import QuantumCircuit
    from spec_convert import convert
    from evaluation import normalize_na, validate_trace_physics
    from .na_physicalizer import physicalize_na

    config = _validated_m2_config(config_path)
    qmap_version = importlib.metadata.version("mqt.qmap")
    core_version = importlib.metadata.version("mqt.core")
    if (qmap_version, core_version) != (
            config["mqt_qmap_version"], config["mqt_core_version"]):
        raise RuntimeError(
            "M2 requires mqt.qmap==3.2.0 and mqt-core==3.1.0; "
            f"found {qmap_version} and {core_version}")
    output = _run_dir()
    zac_spec = _load(architecture_path)
    converted = convert(zac_spec)
    architecture = ZonedNeutralAtomArchitecture.from_json_string(json.dumps(converted))
    # The QASM is already canonical.  Loading it must not invoke another transpile.
    qiskit_circuit = QuantumCircuit.from_qasm_file(str(input_path.resolve()))
    circuit = load(qiskit_circuit)
    compiler = RoutingAwareCompiler(
        architecture, log_level="ERROR",
        deepening_factor=config["deepening_factor"],
        deepening_value=config["deepening_value"],
        lookahead_factor=config["lookahead_factor"],
        reuse_level=config["reuse_level"],
        max_nodes=config["max_nodes"],
    )
    start = time.perf_counter_ns()
    cpu_start = time.process_time_ns()
    native = compiler.compile(circuit)
    cpu_time_ns = time.process_time_ns() - cpu_start
    compiler_time_ns = time.perf_counter_ns() - start
    if not isinstance(native, str) or not native.strip():
        raise RuntimeError("QMAP returned an empty native program")
    # Preserve every @+ u instruction.  Removing it invalidates gate and idle
    # ledgers.  Keep the unmodified compiler output as evidence, while the
    # formal trace receives only deterministic legality splits/waypoints that
    # preserve QMAP's endpoints and per-atom authored trajectory.
    (output / "trace.na.raw").write_text(native, encoding="utf-8")
    repaired_native, repair_stats = physicalize_na(native, zac_spec)
    repaired_path = output / "trace.na"
    repaired_path.write_text(repaired_native, encoding="utf-8")
    physical_validation = validate_trace_physics(
        normalize_na(repaired_path, architecture=architecture_path),
        n_qubits=len(qiskit_circuit.qubits),
    )
    stats = compiler.stats()
    _write_json(output / "compiler_timing.json", {
        "compiler_time_ns": compiler_time_ns,
        "cpu_time_ns": cpu_time_ns,
        "definition": "RoutingAwareCompiler.compile only; canonical input and architecture preloaded",
    })
    _write_json(output / "compiler_stats.json", {
        "mqt_qmap_version": qmap_version,
        "mqt_core_version": core_version,
        "qiskit_version": importlib.metadata.version("qiskit"),
        "python_version": sys.version.split()[0],
        "compiler_class": type(compiler).__name__,
        "fallback": False,
        "stats": stats,
        "physicalization": "common_ghost_safe_split_preserving_qmap_endpoints",
        "physicalization_stats": repair_stats,
        "physical_validation": physical_validation,
        "ghost_splits": repair_stats["ghost_splits"],
        "ghost_repairs": repair_stats["waypoint_atoms"],
    })


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--method", required=True, choices=("M1", "M2", "M3", "M4"))
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--architecture", required=True, type=Path)
    parser.add_argument("--run-kind", default="")
    parser.add_argument("--ablation-variant", default="")
    args = parser.parse_args(argv)
    environment_kind = os.environ.get("ZAC_RUN_KIND", "")
    environment_variant = os.environ.get("ZAC_ABLATION_VARIANT", "")
    if environment_kind and args.run_kind != environment_kind:
        raise ValueError(
            f"run-kind argument/environment mismatch: {args.run_kind!r} != "
            f"{environment_kind!r}")
    if environment_variant and args.ablation_variant != environment_variant:
        raise ValueError(
            "ablation variant argument/environment mismatch: "
            f"{args.ablation_variant!r} != {environment_variant!r}")
    if args.method == "M2":
        if args.run_kind == "ablation" or args.ablation_variant:
            raise ValueError("M2 is not an ablation implementation")
        compile_qmap(args.input, args.config, args.architecture)
    else:
        compile_zac(
            args.input, args.config, args.architecture, args.method,
            run_kind=args.run_kind, ablation_variant=args.ablation_variant)


if __name__ == "__main__":
    main()


__all__ = ["M2_FROZEN_CONFIG", "compile_qmap", "compile_zac",
           "_validated_m2_config"]
