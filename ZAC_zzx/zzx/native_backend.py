"""Fail-closed Python adapter for the C++ resident boundary backend."""
from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from dataclasses import replace
from pathlib import Path
from zipfile import ZipFile
from importlib import import_module
from time import perf_counter_ns
from typing import Iterable, Sequence

from .boundary_problem import (
    NATIVE_ABI_VERSION,
    RNG_VERSION,
    BoundaryConfig,
    BoundaryProblem,
    BoundaryResult,
    FitnessResult,
    RichH0Problem,
    RichH0Result,
    RichSearchConfig,
    flatten_candidates,
)


class NativeBackendUnavailable(RuntimeError):
    pass


class NativeBackendError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _registration_path(module) -> Path:
    extension = Path(module.__file__).resolve()
    return extension.with_name(extension.name + ".wheel-registration.json")


def _runtime_metadata(module, *, require_registered_wheel: bool = False,
                      expected_wheel_sha256: str | None = None) -> dict:
    extension = Path(module.__file__).resolve()
    extension_sha = _sha256(extension)
    marker = _registration_path(module)
    registered = None
    if marker.is_file():
        try:
            registered = json.loads(marker.read_text(encoding="utf-8"))
        except Exception as exc:
            raise NativeBackendUnavailable(
                f"invalid native wheel registration {marker}") from exc
        if registered.get("extension_sha256") != extension_sha:
            raise NativeBackendUnavailable(
                "registered native wheel does not match the loaded extension")
    if require_registered_wheel and registered is None:
        raise NativeBackendUnavailable(
            "formal native run requires a verified wheel registration")
    actual_wheel = None if registered is None else registered.get("wheel_sha256")
    if expected_wheel_sha256 is not None:
        expected = str(expected_wheel_sha256).lower()
        if len(expected) != 64 or any(ch not in "0123456789abcdef"
                                      for ch in expected):
            raise NativeBackendUnavailable("expected wheel SHA256 is malformed")
        if actual_wheel != expected:
            raise NativeBackendUnavailable(
                "loaded native wheel SHA256 differs from the frozen config")
    return {
        "extension_path": str(extension),
        "extension_sha256": extension_sha,
        "wheel_registration_path": str(marker) if registered else None,
        "native_wheel_sha256": actual_wheel,
        "wheel_registered": registered is not None,
    }


def _load_native(*, require_registered_wheel: bool = False,
                 expected_wheel_sha256: str | None = None):
    try:
        module = import_module("zac_native_core")
    except (ImportError, OSError) as exc:
        raise NativeBackendUnavailable(
            "zac_native_core is required; formal runs never fall back to Python") from exc
    abi = int(getattr(module, "NATIVE_ABI_VERSION", -1))
    if abi != NATIVE_ABI_VERSION:
        raise NativeBackendUnavailable(
            f"native ABI mismatch: Python={NATIVE_ABI_VERSION}, extension={abi}")
    rng_version = str(getattr(module, "RNG_VERSION", ""))
    if rng_version != RNG_VERSION:
        raise NativeBackendUnavailable(
            f"native RNG mismatch: Python={RNG_VERSION}, extension={rng_version}")
    _runtime_metadata(
        module,
        require_registered_wheel=require_registered_wheel,
        expected_wheel_sha256=expected_wheel_sha256,
    )
    return module


def native_available() -> bool:
    try:
        _load_native()
    except NativeBackendUnavailable:
        return False
    return True


def build_info(*, require_registered_wheel: bool = False,
               expected_wheel_sha256: str | None = None) -> dict:
    """Return auditable compiler/build flags from the loaded extension."""
    module = _load_native(
        require_registered_wheel=require_registered_wheel,
        expected_wheel_sha256=expected_wheel_sha256,
    )
    value = dict(module.build_info())
    value.update(_runtime_metadata(
        module,
        require_registered_wheel=require_registered_wheel,
        expected_wheel_sha256=expected_wheel_sha256,
    ))
    return value


def register_native_wheel(wheel_path: str | Path) -> dict:
    """Bind the actually loaded extension to a wheel archive SHA256.

    Registration succeeds only when the wheel contains byte-for-byte the loaded
    extension.  Formal runners can subsequently require and compare this marker
    instead of echoing an unverified hash from their JSON config.
    """
    # A force-reinstalled extension legitimately leaves the previous marker
    # stale.  Registration is the operation that replaces that marker, so it
    # must validate the newly imported ABI/RNG and wheel bytes directly rather
    # than calling _load_native(), whose normal fail-closed path correctly
    # rejects the stale marker.
    try:
        module = import_module("zac_native_core")
    except (ImportError, OSError) as exc:
        raise NativeBackendUnavailable(
            "zac_native_core is required for wheel registration") from exc
    abi = int(getattr(module, "NATIVE_ABI_VERSION", -1))
    if abi != NATIVE_ABI_VERSION:
        raise NativeBackendUnavailable(
            f"native ABI mismatch: Python={NATIVE_ABI_VERSION}, extension={abi}")
    rng_version = str(getattr(module, "RNG_VERSION", ""))
    if rng_version != RNG_VERSION:
        raise NativeBackendUnavailable(
            f"native RNG mismatch: Python={RNG_VERSION}, extension={rng_version}")
    wheel = Path(wheel_path).resolve()
    if not wheel.is_file() or wheel.suffix != ".whl":
        raise NativeBackendError("native wheel registration needs a .whl file")
    loaded_path = Path(module.__file__).resolve()
    loaded_sha = _sha256(loaded_path)
    with ZipFile(wheel) as archive:
        candidates = [name for name in archive.namelist()
                      if Path(name).name.startswith("zac_native_core")
                      and Path(name).suffix in {".so", ".pyd", ".dll"}]
        matches = []
        for name in candidates:
            digest = hashlib.sha256(archive.read(name)).hexdigest()
            if digest == loaded_sha:
                matches.append(name)
    if len(matches) != 1:
        raise NativeBackendError(
            "loaded extension is not the unique binary from the supplied wheel")
    value = {
        "schema": 1,
        "wheel_path": str(wheel),
        "wheel_sha256": _sha256(wheel),
        "extension_member": matches[0],
        "extension_sha256": loaded_sha,
    }
    marker = _registration_path(module)
    temporary = marker.with_name(marker.name + ".tmp")
    temporary.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    temporary.replace(marker)
    return build_info(
        require_registered_wheel=True,
        expected_wheel_sha256=value["wheel_sha256"],
    )


class NativeResidentBackend:
    """One persistent architecture object and one native call per boundary."""

    name = "cpp-native-v9"

    def __init__(self, architecture, *, flat_buffers: bool = True,
                 require_registered_wheel: bool = False,
                 expected_wheel_sha256: str | None = None):
        self._module = _load_native(
            require_registered_wheel=require_registered_wheel,
            expected_wheel_sha256=expected_wheel_sha256,
        )
        self.runtime_build_info = build_info(
            require_registered_wheel=require_registered_wheel,
            expected_wheel_sha256=expected_wheel_sha256,
        )
        self._formal_native = bool(require_registered_wheel)
        self._architecture_dto = architecture
        try:
            self._architecture = self._module.ArchitectureSnapshot(
                architecture.n_atoms,
                [point.to_wire() for point in architecture.site_coordinates],
                list(architecture.storage_site_ids),
                [list(pair) for pair in architecture.entangling_site_pairs],
            )
        except Exception as exc:
            raise NativeBackendError("failed to construct native architecture") from exc
        self.flat_buffers = bool(flat_buffers)
        self.last_evaluate_timing: dict[str, int] = {}
        # Cross-boundary reuse is safe only for the tuned solver's exhaustive
        # direct path: it is deterministic, consumes no RNG values, and its
        # winner depends solely on this complete physical problem/config key.
        # Keeping the cache here also skips repeated Python buffer construction
        # and the Python/C++ crossing on long, locally repetitive circuits.
        self._rich_exact_cache: OrderedDict[tuple, RichH0Result] = OrderedDict()
        self._rich_exact_cache_limit = 4096

    def _check_problem(self, problem: BoundaryProblem) -> None:
        if problem.architecture != self._architecture_dto:
            raise NativeBackendError(
                "boundary architecture differs from the backend snapshot")

    def evaluate_many(self, problem: BoundaryProblem,
                      chromosomes: Iterable[Sequence[int]] | None = None,
                      config: BoundaryConfig | None = None
                      ) -> tuple[FitnessResult, ...]:
        self._check_problem(problem)
        config = config or BoundaryConfig()
        selected = problem.select(chromosomes)
        try:
            if not self.flat_buffers:
                values = self._module.evaluate_many(
                    self._architecture,
                    [candidate.to_wire() for candidate in selected],
                    config.to_wire(),
                    problem.prior_idle_time_us,
                )
                self.last_evaluate_timing = {}
                return tuple(FitnessResult.from_wire(value) for value in values)
            marshal_started = perf_counter_ns()
            flat = flatten_candidates(selected)
            config_value = config.to_wire()
            marshal_in_stopped = perf_counter_ns()
            value = self._module.evaluate_many_flat(
                self._architecture, flat.buffers(), config_value,
                problem.prior_idle_time_us)
            marshal_out_started = perf_counter_ns()
            evaluated = tuple(
                FitnessResult.from_flat_row(candidate.chromosome, row)
                for candidate, row in zip(flat.candidates, value["rows"]))
            marshal_out_stopped = perf_counter_ns()
            if len(evaluated) != len(flat.candidates):
                raise NativeBackendError("native flat result count mismatch")
            self.last_evaluate_timing = {
                "marshal_ns": (
                    marshal_in_stopped - marshal_started
                    + marshal_out_stopped - marshal_out_started
                    + int(value["native_parse_ns"])
                    + int(value["native_serialize_ns"])),
                "fitness_ns": int(value["fitness_ns"]),
                "native_parse_ns": int(value["native_parse_ns"]),
                "native_serialize_ns": int(value["native_serialize_ns"]),
            }
            return evaluated
        except Exception as exc:
            raise NativeBackendError(
                f"native evaluation failed for boundary {problem.boundary_id!r}") from exc

    def solve_boundary(self, problem: BoundaryProblem,
                       config: BoundaryConfig | None = None) -> BoundaryResult:
        self._check_problem(problem)
        config = config or BoundaryConfig()
        try:
            if not self.flat_buffers:
                return self.solve_boundary_legacy(problem, config)
            marshal_started = perf_counter_ns()
            flat = flatten_candidates(problem.candidates)
            config_value = config.to_wire()
            marshal_in_stopped = perf_counter_ns()
            value = self._module.solve_boundary_flat(
                self._architecture,
                flat.buffers(),
                config_value,
                problem.prior_idle_time_us,
            )
            marshal_out_started = perf_counter_ns()
            evaluated = tuple(
                FitnessResult.from_flat_row(candidate.chromosome, row)
                for candidate, row in zip(flat.candidates, value["rows"]))
            winner_index = int(value["winner_index"])
            winner = evaluated[winner_index]
            marshal_out_stopped = perf_counter_ns()
            return BoundaryResult(
                winner=winner,
                evaluated=evaluated,
                evaluations=int(value["evaluations"]),
                unique_evaluations=int(value["unique_evaluations"]),
                search_kernel_ns=int(value["search_kernel_ns"]),
                backend=self.name,
                marshal_ns=(marshal_in_stopped - marshal_started
                            + marshal_out_stopped - marshal_out_started
                            + int(value["native_parse_ns"])
                            + int(value["native_serialize_ns"])),
                fitness_ns=int(value["fitness_ns"]),
                selection_ns=int(value["selection_ns"]),
                native_parse_ns=int(value["native_parse_ns"]),
                native_serialize_ns=int(value["native_serialize_ns"]),
                effective_horizon=problem.effective_horizon,
            )
        except NativeBackendError:
            raise
        except Exception as exc:
            raise NativeBackendError(
                f"native search failed for boundary {problem.boundary_id!r}") from exc

    def evaluate_many_legacy(self, problem: BoundaryProblem,
                             chromosomes: Iterable[Sequence[int]] | None = None,
                             config: BoundaryConfig | None = None
                             ) -> tuple[FitnessResult, ...]:
        """Compatibility oracle for ABI-v1 nested dictionaries."""
        self._check_problem(problem)
        config = config or BoundaryConfig()
        selected = problem.select(chromosomes)
        try:
            values = self._module.evaluate_many(
                self._architecture,
                [candidate.to_wire() for candidate in selected],
                config.to_wire(),
                problem.prior_idle_time_us,
            )
            return tuple(FitnessResult.from_wire(value) for value in values)
        except Exception as exc:
            raise NativeBackendError("legacy native evaluation failed") from exc

    def solve_boundary_legacy(self, problem: BoundaryProblem,
                              config: BoundaryConfig | None = None
                              ) -> BoundaryResult:
        """Compatibility oracle for the original nested solve ABI."""
        self._check_problem(problem)
        config = config or BoundaryConfig()
        marshal_started = perf_counter_ns()
        candidate_values = [candidate.to_wire()
                            for candidate in problem.candidates]
        config_value = config.to_wire()
        marshal_in_stopped = perf_counter_ns()
        try:
            value = self._module.solve_boundary(
                self._architecture, candidate_values, config_value,
                problem.prior_idle_time_us)
            marshal_out_started = perf_counter_ns()
            evaluated = tuple(FitnessResult.from_wire(item)
                              for item in value["evaluated"])
            winner = FitnessResult.from_wire(value["winner"])
            marshal_out_stopped = perf_counter_ns()
            return BoundaryResult(
                winner=winner,
                evaluated=evaluated,
                evaluations=int(value["evaluations"]),
                unique_evaluations=int(value["unique_evaluations"]),
                search_kernel_ns=int(value["search_kernel_ns"]),
                backend=f"{self.name}-legacy-wire",
                marshal_ns=(marshal_in_stopped - marshal_started
                            + marshal_out_stopped - marshal_out_started
                            + int(value["native_parse_ns"])
                            + int(value["native_serialize_ns"])),
                fitness_ns=int(value["fitness_ns"]),
                selection_ns=int(value["selection_ns"]),
                native_parse_ns=int(value["native_parse_ns"]),
                native_serialize_ns=int(value["native_serialize_ns"]),
                effective_horizon=problem.effective_horizon,
            )
        except Exception as exc:
            raise NativeBackendError("legacy native search failed") from exc

    def solve_rich_boundary(
            self,
            problem: RichH0Problem,
            config: RichSearchConfig,
            rng_state: tuple,
            *,
            cached_winner: Sequence[int] | None = None,
    ) -> RichH0Result:
        """Solve one complete current-layer residency boundary in one call.

        The extension receives Python's complete ``random.Random.getstate()``
        and returns the advanced state.  Any ABI, validation, matching, or
        native-search failure is surfaced as a compiler error; there is no
        reference fallback on this path.
        """
        if self._formal_native and not problem.exact_current_scheduler:
            raise NativeBackendError(
                "formal ABI8 rich boundary requires an exact scheduler snapshot")
        if self._formal_native and not problem.enforce_frozen_physical_model:
            raise NativeBackendError(
                "formal ABI8 rich boundary requires the frozen physical model")
        if problem.architecture != self._architecture_dto:
            raise NativeBackendError(
                "rich boundary architecture differs from backend snapshot")
        if problem.selected_horizon != config.max_horizon:
            raise NativeBackendError(
                "problem selected_horizon differs from decay config")
        if config.max_horizon == 0 and (
                problem.future_layers or any(
                    term.depth != 0 for term in problem.forecast_terms)):
            raise NativeBackendError(
                "strict M3 boundary can contain only depth-zero state terms")
        marshal_started = perf_counter_ns()
        exact_cache_key = None
        direct_space = 1
        for domain in problem.gate_domains:
            direct_space *= len(domain)
            if direct_space > config.direct_enumeration_limit:
                break
        if (direct_space <= config.direct_enumeration_limit
                and problem.decision_policy == "optimize"):
            direct_space *= 2 ** len(problem.eligible)
        can_reuse_exact = (
            config.fitness_cache
            and config.operator_profile == "tuned"
            and direct_space <= config.direct_enumeration_limit
            and direct_space <= config.resolved_unique_budget
        )
        if can_reuse_exact:
            exact_cache_key = (
                config,
                problem.current_points,
                problem.current_site_ids,
                problem.participants,
                problem.gate_domains,
                problem.static_ghosts,
                problem.eligible,
                problem.min_returns,
                problem.eviction_order_indices,
                problem.forced_return_mask,
                problem.recommended_return_mask,
                problem.recommended_stay_mask,
                problem.return_domains,
                problem.matched_gate_genes,
                problem.decision_policy,
                problem.occupied_storage_site_ids,
                problem.forecast_terms,
                problem.future_layers,
                problem.selected_horizon,
                problem.terminal_boundary,
                problem.prior_idle_time_us,
                problem.scheduler_trace_end_us,
                problem.scheduler_active_union_us,
                problem.scheduler_aod_end_us,
                problem.scheduler_one_qubit_end_us,
                problem.scheduler_rydberg_end_us,
                problem.scheduler_qubit_dependency_end_us,
                problem.scheduler_back_dependency_end_us,
                problem.scheduler_site_dependency_site_ids,
                problem.scheduler_site_dependency_activation_finish_us,
                problem.target_one_qubit_atoms,
                problem.scheduler_one_qubit_duration_us,
                problem.scheduler_rydberg_duration_us,
                problem.scheduler_one_qubit_common_us,
                problem.scheduler_transfer_duration_us,
                problem.scheduler_accel_um_per_us2,
                problem.coherence_t2_us,
                problem.enforce_frozen_physical_model,
            )
            cached_exact = self._rich_exact_cache.get(exact_cache_key)
            if cached_exact is not None:
                self._rich_exact_cache.move_to_end(exact_cache_key)
                elapsed = perf_counter_ns() - marshal_started
                operator_stats = dict(cached_exact.operator_stats)
                operator_stats["exact_result_cache_hits"] = 1
                timing = {
                    "normalize_ns": 0,
                    "decode_ns": 0,
                    "return_match_ns": 0,
                    "fitness_ns": 0,
                    "forecast_ns": 0,
                    "selection_ns": 0,
                    "search_ns": elapsed,
                    "search_kernel_ns": elapsed,
                    "marshal_ns": 0,
                    "python_marshal_ns": 0,
                    "native_call_wall_ns": 0,
                    "native_search_wall_ns": 0,
                    "native_parse_ns": 0,
                    "native_serialize_ns": 0,
                }
                return replace(
                    cached_exact,
                    rng_state=tuple(rng_state),
                    operator_stats=operator_stats,
                    timing=timing,
                )
        try:
            buffers = problem.flat_buffers()
            config_value = config.to_wire()
            cached_value = (None if cached_winner is None else
                            tuple(int(value) for value in cached_winner))
            marshal_in_stopped = perf_counter_ns()
            native_call_started = perf_counter_ns()
            value = self._module.solve_rich_boundary(
                self._architecture,
                buffers,
                config_value,
                rng_state,
                cached_value,
            )
            native_call_stopped = perf_counter_ns()
            marshal_out_started = perf_counter_ns()
            winner = FitnessResult.from_wire(dict(value["winner"]))
            if not winner.feasible:
                raise NativeBackendError(
                    f"native rich search found no feasible candidate for "
                    f"boundary {problem.boundary_id!r}: {winner.error}")
            stats = dict(value["stats"])
            native_timing = {
                key: int(item) for key, item in dict(value["timing"]).items()
            }
            marshal_out_stopped = perf_counter_ns()
            timing = dict(native_timing)
            python_marshal_ns = (
                marshal_in_stopped - marshal_started
                + marshal_out_stopped - marshal_out_started)
            native_call_wall_ns = native_call_stopped - native_call_started
            native_envelope_ns = (
                int(native_timing.get("native_parse_ns", 0))
                + int(native_timing.get("native_serialize_ns", 0)))
            # The wall interval around the pybind call is the authoritative
            # native-stage timer.  Several internal counters deliberately sum
            # nested fitness operations and therefore are useful profiles but
            # not mutually exclusive wall-clock components.
            native_search_wall_ns = max(
                0, native_call_wall_ns - native_envelope_ns)
            timing["native_internal_search_ns"] = int(
                native_timing["search_ns"])
            timing["search_kernel_ns"] = native_search_wall_ns
            timing["python_marshal_ns"] = python_marshal_ns
            timing["native_call_wall_ns"] = native_call_wall_ns
            timing["native_search_wall_ns"] = native_search_wall_ns
            timing["marshal_ns"] = (
                python_marshal_ns + native_envelope_ns
            )
            result = RichH0Result(
                winner=winner,
                gate_option_indices=tuple(
                    int(item) for item in value["gate_option_indices"]),
                return_assignments=tuple(
                    (int(atom), int(site))
                    for atom, site in value["return_assignments"]),
                reseat_assignments=tuple(
                    (int(atom), int(site))
                    for atom, site in value["reseat_assignments"]),
                participant_parking_assignments=tuple(
                    (int(atom), int(site))
                    for atom, site in
                    value["participant_parking_assignments"]),
                rng_state=tuple(value["rng_state"]),
                search_mode=str(value["search_mode"]),
                operator_profile=str(value["operator_profile"]),
                evaluations=int(stats["evaluations"]),
                unique_evaluations=int(stats["unique_evaluations"]),
                deterministic_unique_evaluations=int(
                    stats["deterministic_unique_evaluations"]),
                stochastic_unique_evaluations=int(
                    stats["stochastic_unique_evaluations"]),
                fitness_hits=int(stats["fitness_hits"]),
                decode_hits=int(stats["decode_hits"]),
                return_match_hits=int(stats["return_match_hits"]),
                generations=int(stats["generations"]),
                early_stopped=bool(stats["early_stopped"]),
                stochastic_budget=int(stats["stochastic_budget"]),
                early_stop_reason=str(stats["early_stop_reason"]),
                operator_stats={
                    str(key): int(item)
                    for key, item in dict(stats["operator_stats"]).items()
                } | {"exact_result_cache_hits": 0},
                forecast_terms_applied=int(stats["forecast_terms_applied"]),
                forecast_terms_skipped_cutoff=int(
                    stats["forecast_terms_skipped_cutoff"]),
                forecast_nll=float(value["forecast_nll"]),
                search_negative_log_fidelity=float(
                    value["search_negative_log_fidelity"]),
                forecast_by_depth=tuple(
                    float(item) for item in value["forecast_by_depth"]),
                forecast_breakdown={
                    str(key): float(item)
                    for key, item in dict(value["forecast_breakdown"]).items()
                },
                return_assignment_rank=int(value["return_assignment_rank"]),
                return_assignment_evaluated=int(
                    value["return_assignment_evaluated"]),
                current_ghost_rejections=int(
                    value["current_ghost_rejections"]),
                future_ghost_cost=float(value["future_ghost_cost"]),
                pre_score_reseats=int(value["pre_score_reseats"]),
                pre_score_participant_parkings=int(
                    value["pre_score_participant_parkings"]),
                current_gate_anchor=tuple(
                    int(item) for item in value["current_gate_anchor"]),
                current_gate_anchor_assignment_site_ids=tuple(
                    int(item) for item in
                    value["current_gate_anchor_assignment_site_ids"]),
                current_gate_final_assignment_site_ids=tuple(
                    int(item) for item in
                    value["current_gate_final_assignment_site_ids"]),
                current_gate_guard_branch=str(
                    value["current_gate_guard_branch"]),
                current_gate_guard_cohort_size=int(
                    value["current_gate_guard_cohort_size"]),
                current_gate_guard_admitted_size=int(
                    value["current_gate_guard_admitted_size"]),
                current_gate_projection_source=str(
                    value["current_gate_projection_source"]),
                current_gate_projection_evaluated=int(
                    value["current_gate_projection_evaluated"]),
                timing=timing,
            )
            if exact_cache_key is not None and result.search_mode in {
                    "direct", "enumerate"}:
                self._rich_exact_cache[exact_cache_key] = result
                self._rich_exact_cache.move_to_end(exact_cache_key)
                while len(self._rich_exact_cache) > \
                        self._rich_exact_cache_limit:
                    self._rich_exact_cache.popitem(last=False)
            return result
        except NativeBackendError:
            raise
        except Exception as exc:
            raise NativeBackendError(
                f"native rich search failed for boundary "
                f"{problem.boundary_id!r}: {exc}") from exc

    def solve_rich_h0(
            self,
            problem: RichH0Problem,
            config: RichSearchConfig,
            rng_state: tuple,
            *,
            cached_winner: Sequence[int] | None = None,
    ) -> RichH0Result:
        """Compatibility name for the strict no-future M3 contract."""
        if problem.selected_horizon != 0 or config.max_horizon != 0:
            raise NativeBackendError(
                "solve_rich_h0 is strict and cannot consume future information")
        return self.solve_rich_boundary(
            problem, config, rng_state, cached_winner=cached_winner)


def evaluate_many(problem: BoundaryProblem,
                  chromosomes: Iterable[Sequence[int]] | None = None,
                  config: BoundaryConfig | None = None) -> tuple[FitnessResult, ...]:
    """Convenience API matching the planned public interface."""
    return NativeResidentBackend(problem.architecture).evaluate_many(
        problem, chromosomes, config)


def solve_boundary(problem: BoundaryProblem,
                   config: BoundaryConfig | None = None) -> BoundaryResult:
    """Convenience API matching the planned public interface."""
    return NativeResidentBackend(problem.architecture).solve_boundary(problem, config)


def select_backend(
        architecture,
        *,
        backend: str = "native",
        formal: bool = True,
        require_registered_wheel: bool | None = None,
        expected_wheel_sha256: str | None = None,
):
    """Select a backend without any implicit fallback.

    Formal runs are native-only.  Tests and migration benchmarks may explicitly
    request the reference backend, but ``auto`` is intentionally unsupported.
    """
    if backend == "native":
        require_registration = (formal if require_registered_wheel is None
                                else bool(require_registered_wheel))
        return NativeResidentBackend(
            architecture,
            require_registered_wheel=require_registration,
            expected_wheel_sha256=expected_wheel_sha256,
        )
    if backend == "reference" and not formal:
        from .reference_backend import ReferenceResidentBackend
        return ReferenceResidentBackend()
    if backend == "reference":
        raise NativeBackendError("formal resident runs require backend='native'")
    raise NativeBackendError(f"unknown resident backend {backend!r}; no fallback is allowed")
