"""Real-circuit Python-reference versus ABI4 exact-boundary benchmark.

This benchmark deliberately measures the existing :class:`ResidentPlacer`
boundary implementation instead of a synthetic collection of candidate legs.
It uses the immutable ZAC18 canonical ``ising_n42`` input, the formal full
architecture, the exact migration operator profile, and a bounded geometric
forecast.  QASM parsing, scheduling, and initial placement are outside the
timed region.

The primary timing wraps every complete ``_ga_step_v2`` call.  Consequently it
includes future preparation, BoundaryProblem construction, Python/C++
marshalling, search, result replay, ghost-safe repair, and mapping commit.  The
native inner-search and marshal counters are retained as a bottleneck
decomposition; they are not substituted for the primary stage time.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import random
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


# Keep the reproducible benchmark single-threaded before scipy/qiskit import.
for _name in (
        "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
        "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(_name, "1")

ZAC_ZZX_ROOT = Path(__file__).resolve().parents[3]
WORKTREE_ROOT = ZAC_ZZX_ROOT.parent
PROJECT_ROOT = WORKTREE_ROOT.parent
if str(ZAC_ZZX_ROOT) not in sys.path:
    sys.path.insert(0, str(ZAC_ZZX_ROOT))

from zac.ds.architecture import Architecture  # noqa: E402
from zzx.algorithm_v2 import decay_lookahead_spec  # noqa: E402
from zzx.native_backend import build_info, native_available  # noqa: E402
from zzx.zac_zzx import ZAC_zzx  # noqa: E402
from zzx.zplacer import ResidentPlacer  # noqa: E402


DEFAULT_QASM = (
    PROJECT_ROOT / "artifacts/canonical/zac18/ising_n42.qasm")
DEFAULT_ARCHITECTURE = (
    ZAC_ZZX_ROOT / "hardware_spec/full_architecture.json")
NLL_TOLERANCE = 1e-12


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _stable_sha256(value: Any) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"),
        allow_nan=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _inclusive_iqr(values: Iterable[int]) -> list[int]:
    ordered = sorted(int(value) for value in values)
    if len(ordered) == 1:
        return [ordered[0], ordered[0]]
    first, _median, third = statistics.quantiles(
        ordered, n=4, method="inclusive")
    return [int(first), int(third)]


def _median(values: Iterable[int]) -> int:
    return int(statistics.median(tuple(int(value) for value in values)))


class TimedResidentPlacer(ResidentPlacer):
    """Read-only timing shell around the unchanged boundary semantics."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.benchmark_boundary_ns: list[int] = []

    def _ga_step_v2(self, layer: int):
        started = time.perf_counter_ns()
        result = super()._ga_step_v2(layer)
        self.benchmark_boundary_ns.append(
            time.perf_counter_ns() - started)
        return result


@dataclass(frozen=True)
class RealCase:
    architecture: Architecture
    schedule: tuple[tuple[tuple[int, int], ...], ...]
    initial_mapping: tuple[tuple[int, int, int], ...]
    qasm_path: Path
    architecture_path: Path
    source_qubits: int
    source_two_qubit_gates: int
    full_layer_count: int
    gates_per_layer_limit: int


def load_real_case(qasm_path: Path = DEFAULT_QASM,
                   architecture_path: Path = DEFAULT_ARCHITECTURE,
                   *, layer_limit: int = 9,
                   gates_per_layer_limit: int = 1) -> RealCase:
    """Schedule one real canonical circuit with the production ZAC scheduler."""
    qasm_path = qasm_path.resolve()
    architecture_path = architecture_path.resolve()
    if not qasm_path.is_file():
        raise FileNotFoundError(f"canonical QASM is unavailable: {qasm_path}")
    if not architecture_path.is_file():
        raise FileNotFoundError(
            f"architecture is unavailable: {architecture_path}")
    architecture = Architecture(json.loads(
        architecture_path.read_text(encoding="utf-8")))
    architecture.preprocessing()
    scheduler = ZAC_zzx()
    scheduler.resyn = False
    scheduler.set_architecture(architecture)
    # The upstream parser prints informational messages.  Keep stdout reserved
    # for the machine-readable benchmark artifact.
    with contextlib.redirect_stdout(io.StringIO()):
        scheduler.set_program(str(qasm_path))
        scheduler.scheduling()
    full_schedule = tuple(
        tuple(tuple(int(q) for q in gate) for gate in layer)
        for layer in scheduler.gate_scheduling)
    selected_layers = (full_schedule[:layer_limit]
                       if layer_limit else full_schedule)
    if gates_per_layer_limit <= 0:
        raise ValueError("gates_per_layer_limit must be positive")
    schedule = tuple(
        layer[:gates_per_layer_limit] for layer in selected_layers)
    if len(schedule) < 2:
        raise ValueError("real boundary benchmark needs at least two 2Q layers")
    storage_id = int(architecture.storage_zone[0])
    storage = architecture.dict_SLM[storage_id]
    if scheduler.n_q > storage.n_r * storage.n_c:
        raise ValueError("benchmark circuit does not fit the storage SLM")
    initial = tuple(
        (storage_id, atom // storage.n_c, atom % storage.n_c)
        for atom in range(scheduler.n_q))
    return RealCase(
        architecture=architecture,
        schedule=schedule,
        initial_mapping=initial,
        qasm_path=qasm_path,
        architecture_path=architecture_path,
        source_qubits=int(scheduler.n_q),
        source_two_qubit_gates=int(scheduler.n_g),
        full_layer_count=len(full_schedule),
        gates_per_layer_limit=int(gates_per_layer_limit),
    )


def _resident_parameters(backend: str, seed: int) -> dict[str, Any]:
    return {
        "seed": int(seed),
        "experiment_schema": 2,
        "method_id": "ours_lk",
        "objective": "physical_log_fidelity",
        "lookahead_horizon": decay_lookahead_spec(8),
        "alpha_lookahead": 0.1,
        "engine": "ga",
        "fitness_cache": True,
        "population_size": 6,
        "iterations": 8,
        "neighbors_per_solution": 2,
        "neighbor_sample_size": 24,
        "elite_count": 1,
        "early_stop_patience": 0,
        "operator_profile": "exact",
        "direct_enumeration_limit": 16384,
        "max_unique_evaluations": 16384,
        "theta_capacity": 0.9,
        "box_ratio": 3,
        "pin_radius": 2,
        "w_resident": 0.3,
        "backend": backend,
        # This is a migration differential benchmark, not a formal result run.
        # Actual loaded-wheel provenance is checked separately in this process.
        "formal_native": False,
    }


def _transition_rows(placer: TimedResidentPlacer,
                     schedule: tuple[tuple[tuple[int, int], ...], ...]
                     ) -> list[dict[str, Any]]:
    nonterminal = placer.decision_log[:-1]
    cached = list(placer.transition_cache.items())
    if len(nonterminal) != len(schedule) - 1 or len(cached) != len(nonterminal):
        raise AssertionError("real boundary count differs from transition cache")
    result = []
    for index, (row, (state_key, chromosome)) in enumerate(
            zip(nonterminal, cached)):
        winner = tuple(int(value) for value in chromosome)
        gate_count = len(schedule[index + 1])
        eligible_atoms = tuple(int(value) for value in state_key[7])
        residency_bits = winner[gate_count:]
        if len(residency_bits) != len(eligible_atoms):
            raise AssertionError("winner decision bits do not align with eligible atoms")
        stay_atoms = tuple(atom for atom, bit in zip(
            eligible_atoms, residency_bits) if not bit)
        return_atoms = tuple(atom for atom, bit in zip(
            eligible_atoms, residency_bits) if bit)
        if (len(stay_atoms), len(return_atoms)) != (
                int(row["stay"]), int(row["return"])):
            raise AssertionError(
                "winner STAY/RETURN bits differ from the executable log")
        physical = dict(row["physical"])
        forecast = dict(row["forecast_objective"])
        current_nll = float(physical["negative_log_fidelity"])
        forecast_nll = float(forecast["weighted_negative_log_fidelity"])
        search_nll = float(forecast["search_negative_log_fidelity"])
        if abs(search_nll - (current_nll + forecast_nll)) >= NLL_TOLERANCE:
            raise AssertionError(
                "search NLL is not current physical NLL plus forecast NLL")
        result.append({
            "layer": int(row["layer"]),
            "winner": list(winner),
            "gate_option_genes": list(winner[:gate_count]),
            "eligible_atoms": list(eligible_atoms),
            "residency_bits": list(residency_bits),
            "stay_atoms": list(stay_atoms),
            "return_atoms": list(return_atoms),
            "reseat": int(row["reseat"]),
            "current_physical_nll": current_nll,
            "forecast_nll": forecast_nll,
            "search_nll": search_nll,
            "forecast_by_depth": [
                float(value) for value in forecast["forecast_by_depth"]],
            "forecast_breakdown": {
                str(key): float(value)
                for key, value in dict(forecast["forecast_breakdown"]).items()
            },
            "move_batches": int(physical["move_batches"]),
            "move_time_us": float(physical["move_time_us"]),
            "transfers": int(physical["transfers"]),
            "search_mode": str(row["search_mode"]),
            "evaluations": int(row["cache"]["evaluations"]),
            "unique_evaluations": int(row["cache"]["unique_evaluations"]),
        })
    return result


def _snapshot(placer: TimedResidentPlacer, case: RealCase) -> dict[str, Any]:
    transitions = _transition_rows(placer, case.schedule)
    mapping = [
        [list(int(value) for value in location) for location in layer]
        for layer in placer.mapping]
    rng_state = placer.rng.getstate()
    safety_rng_state = placer.safety_rng.getstate()
    return {
        "winner_chromosomes": [row["winner"] for row in transitions],
        "winner_sha256": _stable_sha256(
            [row["winner"] for row in transitions]),
        "stay_return": [{
            "layer": row["layer"],
            "stay_atoms": row["stay_atoms"],
            "return_atoms": row["return_atoms"],
        } for row in transitions],
        "mapping": mapping,
        "mapping_sha256": _stable_sha256(mapping),
        "rng_state": rng_state,
        "rng_state_sha256": _stable_sha256(rng_state),
        "safety_rng_state": safety_rng_state,
        "safety_rng_state_sha256": _stable_sha256(safety_rng_state),
        "transitions": transitions,
    }


def _timing(placer: TimedResidentPlacer, wall_ns: int) -> dict[str, int]:
    nonterminal = placer.decision_log[:-1]
    backend_rows = placer.backend_timing_log[:-1]
    return {
        "resident_placement_wall_ns": int(wall_ns),
        "complete_boundary_solve_ns": int(sum(
            placer.benchmark_boundary_ns)),
        "search_stage_ns": int(sum(
            row.get("search_kernel_ns", 0) for row in nonterminal)),
        "horizon_selection_ns": int(sum(
            row.get("horizon_selection_ns", 0) for row in nonterminal)),
        "backend_inner_search_ns": int(sum(
            row.get("search_kernel_ns", 0) for row in backend_rows)),
        "marshal_ns": int(sum(
            row.get("marshal_ns", 0) for row in backend_rows)),
        "fitness_ns": int(sum(
            row.get("fitness_ns", 0) for row in backend_rows)),
        "selection_ns": int(sum(
            row.get("selection_ns", 0) for row in backend_rows)),
        "native_parse_ns": int(sum(
            row.get("native_parse_ns", 0) for row in backend_rows)),
        "native_serialize_ns": int(sum(
            row.get("native_serialize_ns", 0) for row in backend_rows)),
        "evaluations": int(sum(
            row["cache"]["evaluations"] for row in nonterminal)),
        "unique_evaluations": int(sum(
            row["cache"]["unique_evaluations"] for row in nonterminal)),
    }


def run_trial(case: RealCase, *, backend: str, seed: int = 7
              ) -> tuple[dict[str, Any], dict[str, int]]:
    initial = [tuple(location) for location in case.initial_mapping]
    schedule = [[list(gate) for gate in layer] for layer in case.schedule]
    placer = TimedResidentPlacer(
        initial, **_resident_parameters(backend, seed))
    started = time.perf_counter_ns()
    placer.run(
        case.architecture, [initial], schedule, True,
        [set() for _ in schedule])
    wall_ns = time.perf_counter_ns() - started
    return _snapshot(placer, case), _timing(placer, wall_ns)


def assert_parity(reference: dict[str, Any], native: dict[str, Any]
                  ) -> dict[str, Any]:
    checks = {
        "winner": (reference["winner_chromosomes"]
                   == native["winner_chromosomes"]),
        "stay_return": reference["stay_return"] == native["stay_return"],
        "mapping": reference["mapping"] == native["mapping"],
        "rng": reference["rng_state"] == native["rng_state"],
        "safety_rng": (reference["safety_rng_state"]
                       == native["safety_rng_state"]),
    }
    if not all(checks.values()):
        failures = sorted(key for key, value in checks.items() if not value)
        raise AssertionError("real boundary parity failed: " + ", ".join(failures))
    if len(reference["transitions"]) != len(native["transitions"]):
        raise AssertionError("transition counts differ")
    max_current = max_forecast = max_search = 0.0
    requested_evaluation_delta = []
    for first, second in zip(
            reference["transitions"], native["transitions"]):
        for structural in (
                "winner", "stay_atoms", "return_atoms", "reseat",
                "move_batches", "transfers", "search_mode",
                "unique_evaluations"):
            if first[structural] != second[structural]:
                raise AssertionError(
                    f"transition {first['layer']} differs at {structural}")
        requested_evaluation_delta.append(
            int(first["evaluations"]) - int(second["evaluations"]))
        current = abs(first["current_physical_nll"]
                      - second["current_physical_nll"])
        forecast = abs(first["forecast_nll"] - second["forecast_nll"])
        search = abs(first["search_nll"] - second["search_nll"])
        max_current = max(max_current, current)
        max_forecast = max(max_forecast, forecast)
        max_search = max(max_search, search)
        for left, right in zip(
                first["forecast_by_depth"], second["forecast_by_depth"]):
            max_forecast = max(max_forecast, abs(left - right))
    if max(max_current, max_forecast, max_search) >= NLL_TOLERANCE:
        raise AssertionError("real boundary NLL error reached the 1e-12 limit")
    return {
        **checks,
        "current_nll_max_abs_error": max_current,
        "forecast_nll_max_abs_error": max_forecast,
        "search_nll_max_abs_error": max_search,
        "tolerance_exclusive": NLL_TOLERANCE,
        "unique_evaluations": True,
        # Python re-requests the already cached incumbent once after search on
        # boundaries without residency genes.  This advances no RNG and adds no
        # unique fitness work; ABI4 returns the incumbent directly.
        "requested_evaluation_delta_reference_minus_native_by_layer":
            requested_evaluation_delta,
    }


def _timing_summary(rows: list[dict[str, int]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in rows[0]:
        values = [row[key] for row in rows]
        result[key] = {
            "median": _median(values),
            "iqr": _inclusive_iqr(values),
            "values": values,
        }
    return result


def _native_bottleneck(rows: list[dict[str, int]]) -> dict[str, Any]:
    components = {
        "fitness_kernel": [],
        "native_search_other": [],
        "native_selection": [],
        "marshal_total": [],
        "python_search_orchestration": [],
        "boundary_prepare_replay_commit": [],
    }
    for row in rows:
        complete = row["complete_boundary_solve_ns"]
        outer_search = row["search_stage_ns"]
        inner = row["backend_inner_search_ns"]
        marshal = row["marshal_ns"]
        fitness = row["fitness_ns"]
        selection = row["selection_ns"]
        components["fitness_kernel"].append(fitness)
        components["native_selection"].append(selection)
        components["native_search_other"].append(max(
            0, inner - fitness - selection))
        components["marshal_total"].append(marshal)
        components["python_search_orchestration"].append(max(
            0, outer_search - inner - marshal))
        components["boundary_prepare_replay_commit"].append(max(
            0, complete - outer_search))
    complete_median = _median(
        row["complete_boundary_solve_ns"] for row in rows)
    medians = {key: _median(value) for key, value in components.items()}
    fractions = {
        key: value / complete_median for key, value in medians.items()}
    dominant = max(medians, key=medians.get)
    return {
        "exclusive_component_median_ns": medians,
        "fraction_of_complete_boundary_median": fractions,
        "dominant_component": dominant,
        "definition": (
            "fitness and selection are subsets of native inner search; "
            "all listed exclusive components partition the measured complete "
            "boundary stage up to cross-repeat median rounding"),
    }


def benchmark(*, qasm_path: Path = DEFAULT_QASM,
              architecture_path: Path = DEFAULT_ARCHITECTURE,
              repeats: int = 5, seed: int = 7, timing_seed: int = 20260823,
              layer_limit: int = 9,
              gates_per_layer_limit: int = 1,
              expected_wheel_sha256: str | None = None,
              require_registered_wheel: bool = True) -> dict[str, Any]:
    if repeats <= 0:
        raise ValueError("repeats must be positive")
    if not native_available():
        raise RuntimeError("ABI4 zac_native_core is not installed")
    native_build = build_info(
        require_registered_wheel=require_registered_wheel,
        expected_wheel_sha256=expected_wheel_sha256)
    case = load_real_case(
        qasm_path, architecture_path, layer_limit=layer_limit,
        gates_per_layer_limit=gates_per_layer_limit)

    # One unmeasured warm-up per backend populates Python/architecture caches
    # and faults in the extension before randomized paired timings begin.
    warm_reference, _ = run_trial(case, backend="reference", seed=seed)
    warm_native, _ = run_trial(case, backend="native", seed=seed)
    warm_parity = assert_parity(warm_reference, warm_native)

    schedule_rng = random.Random(timing_seed)
    schedule_rows = []
    timing: dict[str, list[dict[str, int]]] = {
        "reference": [], "native": []}
    canonical_snapshot = None
    parity_rows = []
    for repetition in range(repeats):
        order = ["reference", "native"]
        schedule_rng.shuffle(order)
        schedule_rows.append({
            "repetition": repetition,
            "order": list(order),
        })
        snapshots: dict[str, dict[str, Any]] = {}
        for backend in order:
            snapshots[backend], measured = run_trial(
                case, backend=backend, seed=seed)
            timing[backend].append(measured)
        parity = assert_parity(snapshots["reference"], snapshots["native"])
        parity_rows.append({"repetition": repetition, **parity})
        if canonical_snapshot is None:
            canonical_snapshot = snapshots["reference"]
        elif any(canonical_snapshot[key] != snapshots["reference"][key]
                 for key in ("winner_chromosomes", "stay_return", "mapping",
                             "rng_state", "safety_rng_state", "transitions")):
            raise AssertionError("reference result changed across repetitions")

    summaries = {
        backend: _timing_summary(rows) for backend, rows in timing.items()
    }
    speedups = {}
    for metric in (
            "complete_boundary_solve_ns", "search_stage_ns",
            "resident_placement_wall_ns"):
        reference_median = summaries["reference"][metric]["median"]
        native_median = summaries["native"][metric]["median"]
        speedups[metric] = reference_median / native_median
    primary_speedup = speedups["complete_boundary_solve_ns"]
    transitions = canonical_snapshot["transitions"]
    result_snapshot = {
        "winner_sha256": canonical_snapshot["winner_sha256"],
        "winner_chromosomes": canonical_snapshot["winner_chromosomes"],
        "stay_return": canonical_snapshot["stay_return"],
        "mapping_sha256": canonical_snapshot["mapping_sha256"],
        "rng_state_sha256": canonical_snapshot["rng_state_sha256"],
        "safety_rng_state_sha256": canonical_snapshot[
            "safety_rng_state_sha256"],
        "per_boundary_nll": [{
            key: row[key] for key in (
                "layer", "current_physical_nll", "forecast_nll",
                "search_nll", "forecast_by_depth")
        } for row in transitions],
    }
    return {
        "schema": 1,
        "benchmark_id": "abi4-exact-real-boundary-ising-n42-v2",
        "claim_scope": "migration benchmark; not a formal quality result",
        "case": {
            "dataset": "zac18",
            "circuit": case.qasm_path.stem,
            "canonical_qasm_path": str(case.qasm_path),
            "canonical_qasm_sha256": _sha256(case.qasm_path),
            "architecture_path": str(case.architecture_path),
            "architecture_sha256": _sha256(case.architecture_path),
            "qubits": case.source_qubits,
            "two_qubit_gates": case.source_two_qubit_gates,
            "full_two_qubit_layers": case.full_layer_count,
            "benchmarked_two_qubit_layers": len(case.schedule),
            "gate_widths": [len(layer) for layer in case.schedule],
            "gates_per_layer_limit": case.gates_per_layer_limit,
            "slice_scope": (
                "first real scheduled gates per layer; migration evidence only"),
            "boundary_calls": len(case.schedule) - 1,
            "schedule_sha256": _stable_sha256(case.schedule),
            "initial_mapping_sha256": _stable_sha256(case.initial_mapping),
        },
        "algorithm": {
            "operator_profile": "exact",
            "seed": seed,
            "population_size": 6,
            "iterations": 8,
            "neighbors_per_solution": 2,
            "neighbor_sample_size": 24,
            "direct_enumeration_limit": 16384,
            "max_unique_evaluations": 16384,
            "forecast": decay_lookahead_spec(8),
            "alpha_lookahead": 0.1,
        },
        "native_build": native_build,
        "protocol": {
            "repeats": repeats,
            "warmups_per_backend": 1,
            "timing_schedule_seed": timing_seed,
            "randomized_schedule": schedule_rows,
            "primary_metric": "complete_boundary_solve_ns",
            "primary_definition": (
                "sum of outer _ga_step_v2 wall times: forecast/problem "
                "construction, backend call, winner replay, repair, and commit"),
            "excluded": (
                "QASM parse, scheduling, initial placement, routing, scoring, "
                "and output serialization"),
        },
        "parity": {
            "warmup": warm_parity,
            "repetitions": parity_rows,
            "all_passed": True,
            "evaluation_accounting_note": (
                "unique fitness evaluations match at every boundary; the "
                "Python path counts two cached incumbent re-requests that the "
                "one-call native result returns directly"),
            "result_snapshot": result_snapshot,
        },
        "timing": {
            "raw": timing,
            "summary": summaries,
            "speedup_reference_over_native": speedups,
            "primary_speedup": primary_speedup,
            "meets_5x_complete_boundary_gate": primary_speedup >= 5.0,
            "native_bottleneck": _native_bottleneck(timing["native"]),
        },
    }


def _markdown(payload: dict[str, Any]) -> str:
    case = payload["case"]
    timing = payload["timing"]
    speedup = timing["speedup_reference_over_native"]
    bottleneck = timing["native_bottleneck"]
    reference = timing["summary"]["reference"]
    native = timing["summary"]["native"]
    primary = "complete_boundary_solve_ns"
    return "\n".join((
        "# ABI4 exact real-boundary benchmark",
        "",
        f"- Case: ZAC18 `{case['circuit']}`; {case['qubits']} qubits, "
        f"{case['two_qubit_gates']} two-qubit gates, "
        f"{case['boundary_calls']} nonterminal boundaries.",
        "- Parity: winner, atom-level STAY/RETURN, mapping, Python RNG state, "
        "current NLL, and decay-forecast NLL all match; maximum NLL error is 0.",
        "- Unique fitness evaluations also match. Python records two extra "
        "cached incumbent requests; neither changes search work or RNG state.",
        f"- Complete boundary median: Python "
        f"{reference[primary]['median'] / 1e6:.3f} ms, ABI4 C++ "
        f"{native[primary]['median'] / 1e6:.3f} ms.",
        f"- Complete boundary speedup: {speedup[primary]:.3f}x "
        f"(5x gate: {'PASS' if timing['meets_5x_complete_boundary_gate'] else 'FAIL'}).",
        f"- Inner search speedup: {speedup['search_stage_ns']:.3f}x; "
        f"whole ResidentPlacer boundary-stream speedup: "
        f"{speedup['resident_placement_wall_ns']:.3f}x.",
        f"- Largest remaining native component: "
        f"`{bottleneck['dominant_component']}` "
        f"({bottleneck['fraction_of_complete_boundary_median'][bottleneck['dominant_component']] * 100:.1f}% "
        "of complete-boundary median).",
        "",
        "This is a language-migration benchmark under `operator_profile=exact`, "
        "not a formal circuit-quality result.",
        "",
    ))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--qasm", type=Path, default=DEFAULT_QASM)
    parser.add_argument("--architecture", type=Path,
                        default=DEFAULT_ARCHITECTURE)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--timing-seed", type=int, default=20260823)
    parser.add_argument("--layer-limit", type=int, default=9)
    parser.add_argument("--gates-per-layer-limit", type=int, default=1)
    parser.add_argument("--expected-wheel-sha256")
    parser.add_argument("--allow-unregistered-wheel", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    args = parser.parse_args(argv)
    payload = benchmark(
        qasm_path=args.qasm,
        architecture_path=args.architecture,
        repeats=args.repeats,
        seed=args.seed,
        timing_seed=args.timing_seed,
        layer_limit=args.layer_limit,
        gates_per_layer_limit=args.gates_per_layer_limit,
        expected_wheel_sha256=args.expected_wheel_sha256,
        require_registered_wheel=not args.allow_unregistered_wheel,
    )
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    if args.markdown_output:
        args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_output.write_text(_markdown(payload), encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()
