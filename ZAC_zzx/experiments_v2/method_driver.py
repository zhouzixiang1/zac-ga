"""One-attempt compiler adapters used by the Schema-2 process runner.

This module writes only beneath ``ZAC_RUN_DIR``.  It does not score fidelity;
the parent runner invokes the independent event normaliser and scorer after the
compiler exits.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Mapping

from .ablation import validate_ablation_config
from .protocol import (ghost_policy_for_method,
                       physicalization_policy_for_method,
                       trace_protocol_for_method)
from .qmap_timing_provenance import validate_qmap_timing_freeze
from .runtime_benchmark import ordered_two_qubit_layer_ledger


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
QMAP_TIMING_PATCH_SHA256 = (
    "459298a31560a2af0367bee522047da48ae33296071f951b4b55b3add86f02c0")
QMAP_TIMING_PACKAGE_VERSION = "3.2.1.dev0+g745d56b26.d20260823"


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
        # Native traces and per-layer decision ledgers reach hundreds of
        # megabytes on the upper QMAP cohort.  Pretty-printing tripled both
        # bytes and post-core write time while adding no experiment evidence.
        # Stable compact separators preserve byte determinism and leave the
        # runner's compiler-time definition unchanged.
        json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
        handle.write("\n")


def _seconds_to_ns(value: Any) -> int:
    return max(0, round(float(value or 0.0) * 1_000_000_000))


def _transition_decisions(compiler) -> list[dict]:
    """Return only real L->L+1 choices, excluding the terminal bookkeeping row."""
    return [
        row for row in list(getattr(compiler, "zzx_decision_log", []) or [])
        if row.get("horizon_reason") != "terminal_boundary"
    ]


def _observed_zac_transition_layer_ledger(compiler) -> Dict[str, Any]:
    """Read the realised ZAC schedule, never the canonical-input receipt.

    ``gate_scheduling`` is populated by the compiler's actual scheduling pass
    and is the layer sequence consumed by intermediate placement and routing.
    Keeping this extraction after ``solve`` makes the timing comparability
    evidence independent of the canonical ASAP ledger registered by the
    parent process.
    """
    raw_layers = getattr(compiler, "gate_scheduling", None)
    if not isinstance(raw_layers, (list, tuple)):
        raise ValueError("ZAC compiler did not expose its realised gate_scheduling")
    ledger = ordered_two_qubit_layer_ledger(
        int(getattr(compiler, "n_q")), raw_layers)
    expected_gates = int(getattr(compiler, "n_g"))
    if int(ledger["gates_2q"]) != expected_gates:
        raise ValueError(
            "realised ZAC layer ledger gate count differs from compiler state: "
            f"{ledger['gates_2q']} != {expected_gates}")
    return ledger


def _zac_stage_timing(compiler, method: str, full_compile_ns: int) -> Dict[str, int]:
    runtime = dict(getattr(compiler, "runtime_analysis", {}) or {})
    # ZAC_zzx instruments the actual method boundaries with perf_counter_ns.
    # Prefer those integer intervals to the historic time.time-derived floats;
    # retaining the latter only as a compatibility fallback keeps M1 and old
    # diagnostic fixtures readable without weakening the formal timer.
    precise = dict(getattr(compiler, "zzx_stage_timing_ns", {}) or {})
    decisions = _transition_decisions(compiler)
    backend_timings = list(
        getattr(compiler, "zzx_backend_timing_log", []) or [])
    result = {
        "transition_decision_ns": int(precise.get(
            "transition_decision_ns",
            _seconds_to_ns(runtime.get("intermediate placement", 0.0)))),
        "search_kernel_ns": sum(int(row.get("search_kernel_ns", 0) or 0)
                                for row in backend_timings),
        "marshal_ns": sum(int(row.get("marshal_ns", 0) or 0)
                          for row in backend_timings),
        "fitness_ns": sum(int(row.get("fitness_ns", 0) or 0)
                          for row in backend_timings),
        "native_parse_ns": sum(int(row.get("native_parse_ns", 0) or 0)
                               for row in backend_timings),
        "native_serialize_ns": sum(
            int(row.get("native_serialize_ns", 0) or 0)
            for row in backend_timings),
        "horizon_selection_ns": sum(
            int(row.get("horizon_selection_ns", 0) or 0)
            for row in decisions),
        "initial_placement_ns": int(precise.get(
            "initial_placement_ns",
            _seconds_to_ns(runtime.get("initial placement", 0.0)))),
        "routing_ns": int(precise.get(
            "routing_ns", _seconds_to_ns(runtime.get("routing", 0.0)))),
        "full_compile_ns": int(full_compile_ns),
    }
    if method == "M1":
        result.update(search_kernel_ns=0, marshal_ns=0,
                      fitness_ns=0, native_parse_ns=0,
                      native_serialize_ns=0, horizon_selection_ns=0)
    return result


def _selected_horizon_counts(decisions: list[dict]) -> Dict[str, int]:
    """Aggregate legacy discrete-horizon decisions without a fixed bucket set.

    Formal decay runs use :func:`_forecast_summary` instead.  Keeping this
    dynamic helper lets old H=0/1/2 regression manifests remain readable while
    ensuring a depth greater than two can never crash the method driver.
    """
    counts: Dict[str, int] = {}
    for row in decisions:
        raw = row.get("selected_horizon", row.get("lookahead_horizon"))
        if raw is None:
            continue
        key = str(int(raw))
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: int(item[0])))


def _forecast_summary(decisions: list[dict],
                      setting: Mapping[str, Any] | None = None) -> Dict[str, Any]:
    """Aggregate the auditable bounded-decay forecast evidence for one run."""
    spec = None
    if setting is not None:
        candidate = setting.get("lookahead_horizon")
        if isinstance(candidate, Mapping) and candidate.get("mode") == "decay":
            spec = dict(candidate)
    rows = []
    for row in decisions:
        audit = row.get("forecast_objective")
        configured = row.get("configured_lookahead_horizon")
        if spec is None and isinstance(configured, Mapping) and \
                configured.get("mode") == "decay":
            spec = dict(configured)
        if isinstance(audit, Mapping):
            rows.append(dict(audit))
    if spec is None:
        return {}
    if len(rows) != len(decisions):
        raise ValueError(
            "formal decay decision log is missing forecast_objective evidence")

    expected = {
        "configured_depth": int(spec["max_horizon"]),
        "rho": float(spec["rho"]),
        "epsilon": float(spec["epsilon"]),
    }
    alpha = (float(setting["alpha_lookahead"])
             if setting is not None and "alpha_lookahead" in setting
             else (float(rows[0]["alpha_lookahead"]) if rows else None))
    effective_depths: Dict[str, int] = {}
    visible_depths: Dict[str, int] = {}
    offset_weights = None
    weighted_nll = 0.0
    state_potential_nll = 0.0
    for row in rows:
        for key, value in expected.items():
            if float(row.get(key, float("nan"))) != float(value):
                raise ValueError(
                    f"forecast {key} drift: {row.get(key)!r} != {value!r}")
        if alpha is None or float(row.get("alpha_lookahead", float("nan"))) != alpha:
            raise ValueError("forecast alpha_lookahead drift")
        effective = str(int(row.get("effective_depth", -1)))
        visible = str(int(row.get("visible_depth", -1)))
        if int(effective) < 0 or int(visible) < 0:
            raise ValueError("forecast depth must be non-negative")
        effective_depths[effective] = effective_depths.get(effective, 0) + 1
        visible_depths[visible] = visible_depths.get(visible, 0) + 1
        weights = list(row.get("offset_weights", ()))
        if offset_weights is None:
            offset_weights = weights
        elif weights != offset_weights:
            raise ValueError("forecast offset weights drift between boundaries")
        weighted_nll += float(row.get("weighted_negative_log_fidelity", 0.0))
        state_potential_nll += float(
            row.get("state_potential_negative_log_fidelity", 0.0))
    if offset_weights is None:
        # Empty schedules have no boundary rows, but their registered window is
        # still reconstructible without reading a future layer.
        offset_weights = []
        if alpha is not None:
            for offset in range(1, int(spec["max_horizon"]) + 1):
                factor = float(spec["rho"]) ** (offset - 1)
                if factor < float(spec["epsilon"]):
                    break
                offset_weights.append({
                    "offset": offset,
                    "decay_factor": factor,
                    "weight": alpha * factor,
                })
    return {
        "mode": spec["mode"],
        "policy": spec["policy"],
        "decay": spec["decay"],
        "configured_depth": int(spec["max_horizon"]),
        "effective_depth_counts": effective_depths,
        "visible_depth_counts": visible_depths,
        "rho": float(spec["rho"]),
        "epsilon": float(spec["epsilon"]),
        "alpha_lookahead": alpha,
        "offset_weights": offset_weights,
        "weighted_negative_log_fidelity_total": weighted_nll,
        "state_potential_negative_log_fidelity_total": (
            state_potential_nll),
        "transition_count": len(rows),
    }


def _load_qmap_timing_freeze() -> Dict[str, Any]:
    # This validation is deliberately executed inside the timing-only QMAP
    # interpreter.  It binds the patch, parity report, environment lock and
    # wheel, then proves every installed native binary is byte-identical to the
    # frozen wheel.  Merely rereading the tracked JSON is not sufficient.
    return dict(validate_qmap_timing_freeze())


def _instrument_m1_transition_timing(compiler) -> None:
    """Time only M1's original intermediate placer without changing semantics.

    The original implementation is frozen, so instrumentation is attached to
    this compiler instance rather than patched into ``ZAC/``.  The wrapper
    delegates exactly once, preserves its return value/exception, and writes to
    an out-of-band timing dictionary that cannot affect the emitted trace.
    """
    original = compiler.place_qubit_intermedeiate
    compiler.zzx_stage_timing_ns = {}

    def timed_intermediate():
        started_ns = time.perf_counter_ns()
        try:
            return original()
        finally:
            compiler.zzx_stage_timing_ns["transition_decision_ns"] = (
                time.perf_counter_ns() - started_ns)

    compiler.place_qubit_intermedeiate = timed_intermediate


def compile_zac(input_path: Path, config_path: Path, architecture_path: Path,
                method: str, *, run_kind: str = "", ablation_variant: str = "") -> None:
    import qiskit

    if method not in ("M1", "M3", "M4"):
        raise ValueError(f"ZAC driver cannot run method {method}")
    if qiskit.__version__ != "1.2.4":
        raise RuntimeError(
            f"M1/M3/M4 require qiskit==1.2.4, found {qiskit.__version__}")
    output = _run_dir()
    spec = _load(architecture_path)

    if method == "M1":
        # M1 is the unmodified paper implementation, not the ``zac`` package
        # embedded beside our extensions.  Each attempt has a fresh process,
        # so putting the frozen original source tree first is deterministic and
        # cannot leak into an M3/M4 attempt.
        original_root = (REPO / "ZAC").resolve()
        sys.path.insert(0, str(original_root))
        from zac.ds.architecture import Architecture
        from zac.zac import ZAC

        implementation = Path(sys.modules[ZAC.__module__].__file__).resolve()
        if original_root not in implementation.parents:
            raise RuntimeError(
                f"M1 did not load the original ZAC implementation: {implementation}")
        compiler = ZAC()
        _instrument_m1_transition_timing(compiler)
    else:
        from zac.ds.architecture import Architecture
        from zzx.zac_zzx import ZAC_zzx

        compiler = ZAC_zzx()

    architecture = Architecture(spec)
    architecture.preprocessing()
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
        # These are the ZAC paper settings.  ``resyn`` stays false only because
        # all four methods consume the already-transpiled immutable canonical
        # QASM; running the same Qiskit pass a second time would change the
        # comparison input rather than reproduce the compiler algorithm.
        setting.update(routing_strategy="maximalis_sort")
        if user_config != {"experiment_schema": 2, "method_id": "M1"}:
            raise ValueError(
                "M1 config must contain only experiment_schema=2 and method_id=M1")
    else:
        from zzx.algorithm_v2 import (
            SCHEMA2_METHOD_HORIZON,
            validate_decay_lookahead_spec,
            validate_schema2_setting,
        )

        setting.update(placer="resident", routing_strategy="coloring")
        setting.update(user_config)
        expected_id = "ours_nl" if method == "M3" else "ours_lk"
        expected = SCHEMA2_METHOD_HORIZON[expected_id]["max_horizon"]
        validate_decay_lookahead_spec(
            setting.get("lookahead_horizon"),
            expected_max_horizon=expected)
        if setting.get("method_id") != expected_id:
            raise ValueError(f"method_id/config mismatch for {method}")
        validate_schema2_setting(setting)

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
    stage_timing = _zac_stage_timing(compiler, method, core_wall_ns)
    _write_json(output / "compiler_timing.json", {
        "compiler_time_ns": core_wall_ns,
        "cpu_time_ns": core_cpu_ns,
        "definition": "compiler scheduling through native routing; excludes input/arch parse and scoring",
        **stage_timing,
    })
    _write_json(output / "trace.zair.json", native_payload)
    decisions = list(getattr(compiler, "zzx_decision_log", []) or [])
    transition_decisions = _transition_decisions(compiler)
    observed_layer_ledger = _observed_zac_transition_layer_ledger(compiler)
    forecast_summary = (
        _forecast_summary(transition_decisions, setting)
        if method in {"M3", "M4"} else {})
    compiler_stats: Dict[str, Any] = {
        "runtime_analysis": compiler.runtime_analysis,
        "qiskit_version": qiskit.__version__,
        "python_version": sys.version.split()[0],
        "qubits": compiler.n_q,
        "gates_2q": compiler.n_g,
        "decision_log": decisions,
        "route_log": getattr(compiler, "zzx_route_log", []),
        "ghost_splits": getattr(compiler, "zzx_ghost_splits", 0),
        "run_kind": run_kind or "unregistered",
        "ablation_variant": ablation_variant or None,
        "ablation_controls": ablation_controls,
        "routing_strategy": compiler.routing_strategy,
        "compiler_module": type(compiler).__module__,
        "compiler_source_file": str(
            Path(sys.modules[type(compiler).__module__].__file__).resolve()),
        "trace_protocol": trace_protocol_for_method(method),
        "ghost_policy": ghost_policy_for_method(method),
        "physicalization": physicalization_policy_for_method(method),
        "algorithm_revision": (
            "zac-paper-native-v1" if method == "M1"
            else str(setting.get("algorithm_revision", "resident-ga-v2"))),
        "backend": (
            "python-paper-zac" if method == "M1"
            else str(setting.get("backend", "python-reference"))),
        "tuning_protocol_id": str(setting.get("tuning_protocol_id", "")),
        "rng_version": str(setting.get("rng_version", "")),
        "observed_transition_layer_ledger_sha256":
            observed_layer_ledger["layer_ledger_sha256"],
        "observed_transition_count": observed_layer_ledger["transitions"],
        "observed_transition_layer_ledger_source":
            "compiler.gate_scheduling",
        # Kept solely for legacy discrete-H regression.  Formal M3/M4 evidence
        # lives in forecast_summary and never masquerades as 0/1/2 buckets.
        "selected_horizon_counts": (
            {} if forecast_summary else
            _selected_horizon_counts(transition_decisions)),
        "forecast_summary": forecast_summary,
        "transition_count": len(transition_decisions),
        # A formal M3/M4 attempt is fail-closed: no Python/reference fallback
        # is permitted after the native backend was requested.
        "fallback": False,
    }
    if method in {"M3", "M4"} and compiler_stats["backend"] == "native":
        from zzx.boundary_problem import NATIVE_ABI_VERSION, RNG_VERSION
        from zzx.native_backend import build_info

        compiler_stats.update(
            native_abi_version=NATIVE_ABI_VERSION,
            native_wheel_sha256=str(setting.get("native_wheel_sha256", "")),
            compiler_and_flags=build_info(
                require_registered_wheel=True,
                expected_wheel_sha256=str(
                    setting.get("native_wheel_sha256", ""))),
            rng_version=str(setting.get("rng_version", RNG_VERSION)),
        )
    _write_json(output / "compiler_stats.json", compiler_stats)


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
                 architecture_path: Path, *, run_kind: str = "") -> None:
    """Run only the paper-native QMAP 3.2 routing-aware A* compiler."""
    import mqt.core
    import mqt.qmap
    from mqt.core import load
    from mqt.qmap.na.zoned import RoutingAwareCompiler, ZonedNeutralAtomArchitecture
    from qiskit import QuantumCircuit
    from spec_convert import convert

    config = _validated_m2_config(config_path)
    qmap_version = importlib.metadata.version("mqt.qmap")
    core_version = importlib.metadata.version("mqt.core")
    timing_instrumented = run_kind == "timing"
    allowed_qmap_version = (QMAP_TIMING_PACKAGE_VERSION if timing_instrumented
                            else config["mqt_qmap_version"])
    if (qmap_version, core_version) != (
            allowed_qmap_version, config["mqt_core_version"]):
        raise RuntimeError(
            f"M2 requires mqt.qmap=={allowed_qmap_version} and "
            "mqt-core==3.1.0; "
            f"found {qmap_version} and {core_version}")
    timing_freeze = _load_qmap_timing_freeze() if timing_instrumented else None
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
    # Keep every instruction exactly as emitted by the official compiler.  In
    # particular, do not split batches or add ghost-avoidance waypoints: those
    # operations are absent from the ICCAD method and measurably change its
    # Move counts and time.  The independent evaluator records any stationary
    # ghost hits without rejecting this baseline.
    (output / "trace.na").write_bytes(native.encode("utf-8"))
    stats = compiler.stats()
    stage_timing: Dict[str, int | None] = {
        "transition_decision_ns": None,
        "search_kernel_ns": None,
        "marshal_ns": 0,
        "horizon_selection_ns": 0,
        "initial_placement_ns": None,
        "routing_ns": int(stats.get("routingTime", 0)) * 1_000,
        "full_compile_ns": compiler_time_ns,
    }
    if timing_instrumented:
        required = {"initialPlacementTime", "layerPlacementTime",
                    "reuseAnalysisTime", "routingTime"}
        missing = sorted(required - set(stats))
        if missing:
            raise RuntimeError(
                f"timing-instrumented QMAP is missing counters: {missing}")
        stage_timing.update(
            transition_decision_ns=(
                int(stats["reuseAnalysisTime"])
                + int(stats["layerPlacementTime"])) * 1_000,
            initial_placement_ns=int(stats["initialPlacementTime"]) * 1_000,
        )
    _write_json(output / "compiler_timing.json", {
        "compiler_time_ns": compiler_time_ns,
        "cpu_time_ns": cpu_time_ns,
        "definition": "RoutingAwareCompiler.compile only; canonical input and architecture preloaded",
        **stage_timing,
    })
    _write_json(output / "compiler_stats.json", {
        # The semantic algorithm remains the frozen paper implementation.  The
        # timing build version is recorded separately and is accepted only for
        # run_kind=timing after its native-trace parity gate passed.
        "mqt_qmap_version": config["mqt_qmap_version"],
        "instrumented_mqt_qmap_version": (
            qmap_version if timing_instrumented else None),
        "mqt_core_version": core_version,
        "qiskit_version": importlib.metadata.version("qiskit"),
        "python_version": sys.version.split()[0],
        "compiler_class": type(compiler).__name__,
        "native_sha256": hashlib.sha256(native.encode("utf-8")).hexdigest(),
        "fallback": False,
        "stats": stats,
        "trace_protocol": trace_protocol_for_method("M2"),
        "ghost_policy": ghost_policy_for_method("M2"),
        "physicalization": physicalization_policy_for_method("M2"),
        "ghost_splits": 0,
        "ghost_repairs": 0,
        "algorithm_revision": "qmap-3.2-paper-native-v1",
        "backend": "qmap-cpp-astar",
        "tuning_protocol_id": "",
        "rng_version": "qmap-3.2-upstream",
        # The frozen timing patch exposes stage counters, not its private layer
        # container.  The parent evaluator derives the observed ledger from
        # the emitted placement/native trace.  Never echo the canonical input
        # hash here: absence of trace proof must remain visibly unproven.
        "observed_transition_layer_ledger_source":
            "normalized_qmap_placement_trace_required",
        "selected_horizon_counts": {},
        "forecast_summary": {},
        "timing_instrumentation": timing_freeze,
        **({
            "qmap_timing_protocol": timing_freeze["protocol"],
            "qmap_timing_patch_sha256": timing_freeze["patch_sha256"],
            "qmap_timing_parity_report_sha256":
                timing_freeze["parity_report_sha256"],
            "qmap_timing_wheel_sha256": timing_freeze["wheel_sha256"],
            "qmap_timing_environment_lock_sha256":
                timing_freeze["environment_lock_sha256"],
        } if timing_freeze is not None else {}),
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
        compile_qmap(
            args.input, args.config, args.architecture,
            run_kind=args.run_kind)
    else:
        compile_zac(
            args.input, args.config, args.architecture, args.method,
            run_kind=args.run_kind, ablation_variant=args.ablation_variant)


if __name__ == "__main__":
    main()


__all__ = ["M2_FROZEN_CONFIG", "compile_qmap", "compile_zac",
           "_validated_m2_config"]
