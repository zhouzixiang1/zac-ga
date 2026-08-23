"""Stable DTOs shared by the Python and native resident-search backends.

The first native ABI deliberately starts *after* placement decoding.  Python owns
the compiler state and constructs immutable candidate movement plans; a backend
scores many candidates in one call and returns the lexicographic winner.  This is
already a coarse-grained boundary (there is no per-leg Python callback) and gives
us an exact differential oracle before moving mutation and rollout into C++.
"""
from __future__ import annotations

from array import array
from dataclasses import dataclass
from math import dist, isfinite
from typing import Iterable, Mapping, Sequence


NATIVE_ABI_VERSION = 3
RNG_VERSION = "python-random-mt19937-v1"


def _finite(value: float, name: str) -> float:
    result = float(value)
    if not isfinite(result):
        raise ValueError(f"{name} must be finite, got {value!r}")
    return result


@dataclass(frozen=True, slots=True)
class Point:
    x: float
    y: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "x", _finite(self.x, "point.x"))
        object.__setattr__(self, "y", _finite(self.y, "point.y"))

    def to_wire(self) -> list[float]:
        return [self.x, self.y]


@dataclass(frozen=True, slots=True)
class Leg:
    distance_um: float
    source: Point
    target: Point

    def __post_init__(self) -> None:
        distance_um = _finite(self.distance_um, "leg.distance_um")
        if distance_um < 0:
            raise ValueError("leg.distance_um must be non-negative")
        object.__setattr__(self, "distance_um", distance_um)

    @classmethod
    def between(cls, source: Point | Sequence[float],
                target: Point | Sequence[float]) -> "Leg":
        source = source if isinstance(source, Point) else Point(*source)
        target = target if isinstance(target, Point) else Point(*target)
        return cls(dist((source.x, source.y), (target.x, target.y)),
                   source, target)

    def to_wire(self) -> list[float]:
        return [self.distance_um, self.source.x, self.source.y,
                self.target.x, self.target.y]


@dataclass(frozen=True, slots=True)
class Ghost:
    atom: int
    position: Point

    def __post_init__(self) -> None:
        if not isinstance(self.atom, int) or isinstance(self.atom, bool):
            raise TypeError("ghost.atom must be an integer")

    def to_wire(self) -> list[float | int]:
        return [self.atom, self.position.x, self.position.y]


@dataclass(frozen=True, slots=True)
class MovementPhase:
    legs: tuple[Leg, ...] = ()
    ghosts: tuple[Ghost, ...] = ()
    owners: tuple[int, ...] = ()
    batching: str = "phase"

    def __post_init__(self) -> None:
        object.__setattr__(self, "legs", tuple(self.legs))
        object.__setattr__(self, "ghosts", tuple(self.ghosts))
        object.__setattr__(self, "owners", tuple(int(v) for v in self.owners))
        if self.owners and len(self.owners) != len(self.legs):
            raise ValueError("phase owners must be empty or match the leg count")
        if self.batching not in {"phase", "greedy"}:
            raise ValueError("phase batching must be 'phase' or 'greedy'")

    def to_wire(self) -> dict:
        return {
            "legs": [leg.to_wire() for leg in self.legs],
            "ghosts": [ghost.to_wire() for ghost in self.ghosts],
            "owners": list(self.owners),
            "batching": self.batching,
        }


@dataclass(frozen=True, slots=True)
class CandidatePlan:
    chromosome: tuple[int, ...]
    phases: tuple[MovementPhase, ...]
    idle_exposures: int = 0

    def __post_init__(self) -> None:
        chromosome = tuple(int(v) for v in self.chromosome)
        phases = tuple(self.phases)
        if self.idle_exposures < 0:
            raise ValueError("idle_exposures must be non-negative")
        object.__setattr__(self, "chromosome", chromosome)
        object.__setattr__(self, "phases", phases)

    def to_wire(self) -> dict:
        return {
            "chromosome": list(self.chromosome),
            "phases": [phase.to_wire() for phase in self.phases],
            "idle_exposures": int(self.idle_exposures),
        }


@dataclass(frozen=True, slots=True)
class ArchitectureSnapshot:
    """Persistent, immutable native-side architecture input.

    Coordinates are indexed by the compiler's canonical site id.  Candidate plans
    currently carry exact coordinates as well, but preloading this array now avoids
    an ABI break when native decoding starts consuming site indices.
    """

    n_atoms: int
    site_coordinates: tuple[Point, ...] = ()
    storage_site_ids: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if (not isinstance(self.n_atoms, int) or isinstance(self.n_atoms, bool)
                or self.n_atoms <= 0):
            raise ValueError("n_atoms must be a positive integer")
        object.__setattr__(self, "site_coordinates", tuple(self.site_coordinates))
        storage_ids = tuple(int(value) for value in self.storage_site_ids)
        if len(set(storage_ids)) != len(storage_ids):
            raise ValueError("storage_site_ids must be unique")
        if any(value < 0 or value >= len(self.site_coordinates)
               for value in storage_ids):
            raise ValueError("storage site id is outside site_coordinates")
        object.__setattr__(self, "storage_site_ids", storage_ids)

    @classmethod
    def from_coordinates(cls, n_atoms: int,
                         coordinates: Iterable[Sequence[float]],
                         storage_site_ids: Iterable[int] = ()) -> "ArchitectureSnapshot":
        return cls(n_atoms, tuple(Point(*value) for value in coordinates),
                   tuple(storage_site_ids))

    def to_wire(self) -> dict:
        return {
            "n_atoms": self.n_atoms,
            "site_coordinates": [point.to_wire() for point in self.site_coordinates],
            "storage_site_ids": list(self.storage_site_ids),
        }


@dataclass(frozen=True, slots=True)
class BoundaryProblem:
    architecture: ArchitectureSnapshot
    candidates: tuple[CandidatePlan, ...]
    boundary_id: str = ""
    effective_horizon: int = 0
    selected_horizon: int | None = None

    def __post_init__(self) -> None:
        candidates = tuple(self.candidates)
        if not candidates:
            raise ValueError("boundary problem needs at least one candidate")
        chosen_horizon = (self.effective_horizon if self.selected_horizon is None
                          else self.selected_horizon)
        if (not isinstance(chosen_horizon, int)
                or isinstance(chosen_horizon, bool) or chosen_horizon < 0):
            raise ValueError("selected_horizon must be a non-negative integer")
        if self.selected_horizon is not None and self.effective_horizon not in {
                0, chosen_horizon}:
            raise ValueError("effective_horizon and selected_horizon disagree")
        keys = [candidate.chromosome for candidate in candidates]
        if len(set(keys)) != len(keys):
            raise ValueError("candidate chromosomes must be unique")
        object.__setattr__(self, "candidates", candidates)
        object.__setattr__(self, "effective_horizon", chosen_horizon)
        object.__setattr__(self, "selected_horizon", chosen_horizon)

    def select(self, chromosomes: Iterable[Sequence[int]] | None = None
               ) -> tuple[CandidatePlan, ...]:
        if chromosomes is None:
            return self.candidates
        by_key = {candidate.chromosome: candidate for candidate in self.candidates}
        selected = []
        seen = set()
        for raw in chromosomes:
            key = tuple(int(value) for value in raw)
            if key in seen:
                continue
            try:
                selected.append(by_key[key])
            except KeyError as exc:
                raise KeyError(f"unknown chromosome {key!r}") from exc
            seen.add(key)
        return tuple(selected)


@dataclass(frozen=True, slots=True)
class FlatBoundaryBatch:
    """Contiguous ABI payload for a batch of decoded candidates.

    All integer buffers use signed 64-bit storage, floating buffers use IEEE-754
    doubles, and batching uses one unsigned byte per phase.  Offset arrays follow
    the usual CSR convention and always start at zero.  The object retains buffer
    ownership for the complete native call, so pybind11 can read without copying
    or constructing thousands of nested Python dictionaries.
    """

    candidates: tuple[CandidatePlan, ...]
    chromosome_offsets: array
    chromosome_values: array
    candidate_phase_offsets: array
    idle_exposures: array
    phase_leg_offsets: array
    phase_ghost_offsets: array
    phase_owner_offsets: array
    batching: array
    leg_values: array
    ghost_atoms: array
    ghost_xy: array
    owners: array

    def buffers(self) -> dict[str, array]:
        return {
            "chromosome_offsets": self.chromosome_offsets,
            "chromosome_values": self.chromosome_values,
            "candidate_phase_offsets": self.candidate_phase_offsets,
            "idle_exposures": self.idle_exposures,
            "phase_leg_offsets": self.phase_leg_offsets,
            "phase_ghost_offsets": self.phase_ghost_offsets,
            "phase_owner_offsets": self.phase_owner_offsets,
            "batching": self.batching,
            "leg_values": self.leg_values,
            "ghost_atoms": self.ghost_atoms,
            "ghost_xy": self.ghost_xy,
            "owners": self.owners,
        }


def flatten_candidates(candidates: Iterable[CandidatePlan]) -> FlatBoundaryBatch:
    """Build the version-1 typed contiguous candidate payload."""
    candidates = tuple(candidates)
    chromosome_offsets = array("q", [0])
    chromosome_values = array("q")
    candidate_phase_offsets = array("q", [0])
    idle_exposures = array("q")
    phase_leg_offsets = array("q", [0])
    phase_ghost_offsets = array("q", [0])
    phase_owner_offsets = array("q", [0])
    batching = array("B")
    leg_values = array("d")
    ghost_atoms = array("q")
    ghost_xy = array("d")
    owners = array("q")

    phase_count = 0
    for candidate in candidates:
        chromosome_values.extend(candidate.chromosome)
        chromosome_offsets.append(len(chromosome_values))
        idle_exposures.append(candidate.idle_exposures)
        for phase in candidate.phases:
            for leg in phase.legs:
                leg_values.extend(leg.to_wire())
            phase_leg_offsets.append(len(leg_values) // 5)
            for ghost in phase.ghosts:
                ghost_atoms.append(ghost.atom)
                ghost_xy.extend((ghost.position.x, ghost.position.y))
            phase_ghost_offsets.append(len(ghost_atoms))
            owners.extend(phase.owners)
            phase_owner_offsets.append(len(owners))
            batching.append(0 if phase.batching == "phase" else 1)
            phase_count += 1
        candidate_phase_offsets.append(phase_count)
    return FlatBoundaryBatch(
        candidates=candidates,
        chromosome_offsets=chromosome_offsets,
        chromosome_values=chromosome_values,
        candidate_phase_offsets=candidate_phase_offsets,
        idle_exposures=idle_exposures,
        phase_leg_offsets=phase_leg_offsets,
        phase_ghost_offsets=phase_ghost_offsets,
        phase_owner_offsets=phase_owner_offsets,
        batching=batching,
        leg_values=leg_values,
        ghost_atoms=ghost_atoms,
        ghost_xy=ghost_xy,
        owners=owners,
    )


@dataclass(frozen=True, slots=True)
class BoundaryConfig:
    seed: int = 0
    max_unique_evaluations: int = 0
    exact_coloring_threshold: int = 0
    horizon_policy: str = "fixed"
    max_horizon: int = 0
    # The resident placer already performs the executable ghost repair after
    # selecting a chromosome.  During exact migration we must therefore retain
    # its historical scoring semantics: pairwise ghost edges affect batching,
    # while a single-leg hit is rejected only by the shared hard-repair layer.
    # Standalone kernel callers keep the stricter default.
    enforce_single_leg_ghost: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.seed, int) or isinstance(self.seed, bool):
            raise TypeError("seed must be an integer")
        for name in ("max_unique_evaluations", "exact_coloring_threshold",
                     "max_horizon"):
            value = getattr(self, name)
            if (not isinstance(value, int) or isinstance(value, bool) or value < 0):
                raise ValueError(f"{name} must be a non-negative integer")
        if self.horizon_policy not in {"fixed", "dynamic"}:
            raise ValueError("horizon_policy must be 'fixed' or 'dynamic'")
        if not isinstance(self.enforce_single_leg_ghost, bool):
            raise TypeError("enforce_single_leg_ghost must be a boolean")

    def to_wire(self) -> dict:
        return {
            "seed": self.seed,
            "max_unique_evaluations": self.max_unique_evaluations,
            "exact_coloring_threshold": self.exact_coloring_threshold,
            "horizon_policy": self.horizon_policy,
            "max_horizon": self.max_horizon,
            "enforce_single_leg_ghost": self.enforce_single_leg_ghost,
        }


@dataclass(frozen=True, slots=True)
class FitnessResult:
    chromosome: tuple[int, ...]
    feasible: bool
    negative_log_fidelity: float
    transfer_nll: float
    idle_excitation_nll: float
    coherence_nll: float
    move_batches: int
    move_time_us: float
    total_distance_um: float
    idle_exposures: int
    transfers: int
    phase_batches: tuple[tuple[tuple[int, ...], ...], ...] = ()
    error: str | None = None

    @property
    def objective(self) -> tuple:
        return (
            self.negative_log_fidelity,
            self.move_batches,
            self.move_time_us,
            self.total_distance_um,
            self.chromosome,
        )

    @classmethod
    def from_wire(cls, value: Mapping) -> "FitnessResult":
        return cls(
            chromosome=tuple(int(v) for v in value["chromosome"]),
            feasible=bool(value["feasible"]),
            negative_log_fidelity=float(value["negative_log_fidelity"]),
            transfer_nll=float(value["transfer_nll"]),
            idle_excitation_nll=float(value["idle_excitation_nll"]),
            coherence_nll=float(value["coherence_nll"]),
            move_batches=int(value["move_batches"]),
            move_time_us=float(value["move_time_us"]),
            total_distance_um=float(value["total_distance_um"]),
            idle_exposures=int(value["idle_exposures"]),
            transfers=int(value["transfers"]),
            phase_batches=tuple(
                tuple(tuple(int(i) for i in batch) for batch in phase)
                for phase in value.get("phase_batches", ())),
            error=value.get("error"),
        )

    @classmethod
    def from_flat_row(cls, chromosome: Sequence[int], value: Sequence
                      ) -> "FitnessResult":
        """Decode the compact native row; chromosome stays in the Python DTO."""
        if len(value) != 12:
            raise ValueError(f"native fitness row needs 12 fields, got {len(value)}")
        return cls(
            chromosome=tuple(int(v) for v in chromosome),
            feasible=bool(value[0]),
            negative_log_fidelity=float(value[1]),
            transfer_nll=float(value[2]),
            idle_excitation_nll=float(value[3]),
            coherence_nll=float(value[4]),
            move_batches=int(value[5]),
            move_time_us=float(value[6]),
            total_distance_um=float(value[7]),
            idle_exposures=int(value[8]),
            transfers=int(value[9]),
            phase_batches=tuple(
                tuple(tuple(int(i) for i in batch) for batch in phase)
                for phase in value[10]),
            error=value[11],
        )


@dataclass(frozen=True, slots=True)
class BoundaryResult:
    winner: FitnessResult
    evaluated: tuple[FitnessResult, ...]
    evaluations: int
    unique_evaluations: int
    search_kernel_ns: int
    backend: str
    marshal_ns: int = 0
    fitness_ns: int = 0
    selection_ns: int = 0
    native_parse_ns: int = 0
    native_serialize_ns: int = 0
    native_abi_version: int = NATIVE_ABI_VERSION
    rng_version: str = RNG_VERSION
    effective_horizon: int = 0


@dataclass(frozen=True, slots=True)
class RichGateOption:
    """One decoded gate-site menu row for the one-call H=0 solver."""

    site_id: int
    q1: int
    q2: int
    target1: Point | None
    target2: Point | None
    target1_site_id: int | None = None
    target2_site_id: int | None = None
    legs: tuple[Leg, ...] = ()
    owners: tuple[int, ...] = ()
    seated_ghosts: tuple[Ghost, ...] = ()

    def __post_init__(self) -> None:
        for name in ("site_id", "q1", "q2"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(f"{name} must be an integer")
        indexed = (self.target1_site_id is not None
                   or self.target2_site_id is not None)
        if indexed:
            if self.target1_site_id is None or self.target2_site_id is None:
                raise ValueError("both indexed gate target ids are required")
            for name in ("target1_site_id", "target2_site_id"):
                value = getattr(self, name)
                if (not isinstance(value, int) or isinstance(value, bool)
                        or value < 0):
                    raise ValueError(f"{name} must be a non-negative integer")
        elif self.target1 is None or self.target2 is None:
            raise ValueError("legacy gate options require both target points")
        object.__setattr__(self, "legs", tuple(self.legs))
        object.__setattr__(self, "owners", tuple(int(v) for v in self.owners))
        object.__setattr__(self, "seated_ghosts", tuple(self.seated_ghosts))
        if self.owners and len(self.owners) != len(self.legs):
            raise ValueError("gate-option owners must be empty or match legs")


@dataclass(frozen=True, slots=True)
class RichReturnOption:
    """One already occupancy-filtered RETURN matching edge."""

    site_id: int
    point: Point | None
    cost: float
    site_location: tuple[int, int, int] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.site_id, int) or isinstance(self.site_id, bool):
            raise TypeError("return site_id must be an integer")
        object.__setattr__(self, "cost", _finite(self.cost, "return cost"))
        if self.cost < 0:
            raise ValueError("return cost must be non-negative")
        if self.site_location is not None:
            location = tuple(int(value) for value in self.site_location)
            if len(location) != 3:
                raise ValueError("site_location must contain array,row,column")
            object.__setattr__(self, "site_location", location)


@dataclass(frozen=True, slots=True)
class RichForecastTerm:
    """One bounded, precomputed future heuristic contribution.

    ``depth`` is one-based relative to the current boundary.  Terms are unary
    or pairwise predicates over the current normalized chromosome/decoded
    assignment; this keeps the C++ search deterministic and prevents a nested
    future GA.  ``nll`` is *not* part of the current physical fidelity result.
    """

    depth: int
    kind: str
    category: str
    nll: float
    index: int = -1
    second_index: int = -1
    selector: int = -1

    def __post_init__(self) -> None:
        if (not isinstance(self.depth, int) or isinstance(self.depth, bool)
                or not 1 <= self.depth <= 8):
            raise ValueError("forecast depth must be in [1, 8]")
        if self.kind not in {
                "constant", "stay", "return", "return_site", "gate_option",
                "stay_pair", "return_pair"}:
            raise ValueError("unknown forecast term kind")
        if self.category not in {"residency", "reentry", "terminal"}:
            raise ValueError("unknown forecast term category")
        value = _finite(self.nll, "forecast nll")
        if value < 0:
            raise ValueError("forecast nll must be non-negative")
        object.__setattr__(self, "nll", value)
        if self.kind != "constant" and self.index < 0:
            raise ValueError("conditional forecast term needs an index")
        if self.kind in {"stay_pair", "return_pair"} and self.second_index < 0:
            raise ValueError("pair forecast term needs second_index")
        if self.kind in {"return_site", "gate_option"} and self.selector < 0:
            raise ValueError("selected forecast term needs selector")


@dataclass(frozen=True, slots=True)
class RichSearchConfig:
    # Required on purpose: parity and formal tuned runs must never be confused.
    operator_profile: str
    forecast_mode: str = "decay"
    forecast_policy: str = "physical_terminal_decay_v1"
    decay_kind: str = "geometric"
    max_horizon: int = 0
    alpha_lookahead: float = 0.1
    decay_rho: float = 0.6
    decay_epsilon: float = 0.05
    population_size: int = 6
    iterations: int = 8
    neighbors_per_solution: int = 2
    neighbor_sample_size: int = 24
    elite_count: int = 1
    early_stop_patience: int = 0
    max_unique_evaluations: int = 0
    exact_coloring_threshold: int = 0
    enforce_single_leg_ghost: bool = False
    fitness_cache: bool = True

    def __post_init__(self) -> None:
        if self.operator_profile not in {"exact", "tuned"}:
            raise ValueError("operator_profile must be 'exact' or 'tuned'")
        if self.forecast_mode != "decay":
            raise ValueError("forecast_mode must be 'decay'")
        if self.forecast_policy != "physical_terminal_decay_v1":
            raise ValueError("unknown forecast_policy")
        if self.decay_kind != "geometric":
            raise ValueError("decay_kind must be 'geometric'")
        if (not isinstance(self.max_horizon, int)
                or isinstance(self.max_horizon, bool)
                or not 0 <= self.max_horizon <= 8):
            raise ValueError("max_horizon must be in [0, 8]")
        object.__setattr__(self, "decay_rho",
                           _finite(self.decay_rho, "decay_rho"))
        object.__setattr__(self, "decay_epsilon",
                           _finite(self.decay_epsilon, "decay_epsilon"))
        object.__setattr__(self, "alpha_lookahead",
                           _finite(self.alpha_lookahead, "alpha_lookahead"))
        if self.alpha_lookahead < 0:
            raise ValueError("alpha_lookahead must be non-negative")
        if not 0 < self.decay_rho <= 1:
            raise ValueError("decay_rho must be in (0, 1]")
        if not 0 <= self.decay_epsilon <= 1:
            raise ValueError("decay_epsilon must be in [0, 1]")
        for name in ("population_size", "iterations", "neighbors_per_solution",
                     "neighbor_sample_size", "elite_count"):
            value = getattr(self, name)
            if (not isinstance(value, int) or isinstance(value, bool)
                    or value <= 0):
                raise ValueError(f"{name} must be a positive integer")
        for name in ("early_stop_patience", "max_unique_evaluations",
                     "exact_coloring_threshold"):
            value = getattr(self, name)
            if (not isinstance(value, int) or isinstance(value, bool)
                    or value < 0):
                raise ValueError(f"{name} must be a non-negative integer")
        if self.elite_count > self.population_size:
            raise ValueError("elite_count cannot exceed population_size")
        if not isinstance(self.enforce_single_leg_ghost, bool):
            raise TypeError("enforce_single_leg_ghost must be boolean")
        if not isinstance(self.fitness_cache, bool):
            raise TypeError("fitness_cache must be boolean")

    @property
    def resolved_unique_budget(self) -> int:
        return self.max_unique_evaluations or (
            self.population_size * self.iterations * self.neighbor_sample_size)

    def to_wire(self) -> dict:
        return {
            "operator_profile": self.operator_profile,
            "forecast_mode": self.forecast_mode,
            "forecast_policy": self.forecast_policy,
            "decay_kind": self.decay_kind,
            "max_horizon": self.max_horizon,
            "alpha_lookahead": self.alpha_lookahead,
            "decay_rho": self.decay_rho,
            "decay_epsilon": self.decay_epsilon,
            "population_size": self.population_size,
            "iterations": self.iterations,
            "neighbors_per_solution": self.neighbors_per_solution,
            "neighbor_sample_size": self.neighbor_sample_size,
            "elite_count": self.elite_count,
            "early_stop_patience": self.early_stop_patience,
            "max_unique_evaluations": self.resolved_unique_budget,
            "exact_coloring_threshold": self.exact_coloring_threshold,
            "enforce_single_leg_ghost": self.enforce_single_leg_ghost,
            "fitness_cache": self.fitness_cache,
        }


@dataclass(frozen=True, slots=True)
class RichH0Problem:
    """Frozen current-boundary state consumed by ``solve_rich_h0``.

    No future layer appears in this contract.  M4 may call the H=0 solver only
    after Python has selected horizon zero; H1/H2 use a separately versioned
    rollout table so the no-lookahead information boundary remains auditable.
    """

    architecture: ArchitectureSnapshot
    current_points: tuple[Point, ...]
    participants: tuple[int, ...]
    gate_domains: tuple[tuple[RichGateOption, ...], ...]
    static_ghosts: tuple[Ghost, ...]
    eligible: tuple[int, ...]
    min_returns: int
    eviction_order_indices: tuple[int, ...]
    forced_return_mask: tuple[bool, ...]
    return_domains: tuple[tuple[RichReturnOption, ...], ...]
    matched_gate_genes: tuple[int, ...]
    decision_policy: str = "optimize"
    occupied_storage_site_ids: tuple[int, ...] = ()
    # Optional compact geometry wire.  When present, all current/target/RETURN
    # points are resolved against ArchitectureSnapshot in C++ and no repeated
    # doubles or leg rows cross the Python boundary.
    current_site_ids: tuple[int, ...] = ()
    forecast_terms: tuple[RichForecastTerm, ...] = ()
    boundary_id: str = ""
    selected_horizon: int = 0

    def __post_init__(self) -> None:
        points = tuple(self.current_points)
        current_site_ids = tuple(int(value) for value in self.current_site_ids)
        forecast_terms = tuple(self.forecast_terms)
        participants = tuple(int(q) for q in self.participants)
        gate_domains = tuple(tuple(domain) for domain in self.gate_domains)
        ghosts = tuple(self.static_ghosts)
        eligible = tuple(int(q) for q in self.eligible)
        eviction = tuple(int(i) for i in self.eviction_order_indices)
        forced = tuple(bool(value) for value in self.forced_return_mask)
        return_domains = tuple(tuple(domain) for domain in self.return_domains)
        matched = tuple(int(value) for value in self.matched_gate_genes)
        occupied_storage = tuple(int(value)
                                 for value in self.occupied_storage_site_ids)
        indexed_geometry = bool(current_site_ids)
        if indexed_geometry:
            if len(current_site_ids) != self.architecture.n_atoms:
                raise ValueError("current_site_ids must contain every atom")
            if any(value < 0 or value >= len(self.architecture.site_coordinates)
                   for value in current_site_ids):
                raise ValueError("current site id is outside architecture")
            for domain in gate_domains:
                for option in domain:
                    if (option.target1_site_id is None
                            or option.target2_site_id is None):
                        raise ValueError(
                            "indexed geometry requires target site ids")
                    if max(option.target1_site_id, option.target2_site_id) >= \
                            len(self.architecture.site_coordinates):
                        raise ValueError("gate target site id is outside architecture")
            for domain in return_domains:
                for option in domain:
                    if option.site_id < 0 or option.site_id >= \
                            len(self.architecture.site_coordinates):
                        raise ValueError("RETURN site id is outside architecture")
        elif len(points) != self.architecture.n_atoms:
            raise ValueError("current_points must contain every atom")
        if len(set(participants)) != len(participants):
            raise ValueError("participants must be unique")
        if len(set(eligible)) != len(eligible):
            raise ValueError("eligible atoms must be unique")
        if set(participants) & set(eligible):
            raise ValueError("eligible atoms cannot be target participants")
        if any(q < 0 or q >= self.architecture.n_atoms
               for q in participants + eligible):
            raise ValueError("atom id is outside the architecture")
        if any(not domain for domain in gate_domains):
            raise ValueError("every gate domain must be non-empty")
        for domain in gate_domains:
            gate = (domain[0].q1, domain[0].q2)
            if any((option.q1, option.q2) != gate for option in domain):
                raise ValueError("all options in one gate domain need the same gate")
        if len(return_domains) != len(eligible):
            raise ValueError("return_domains must align with eligible")
        if len(forced) != len(eligible):
            raise ValueError("forced_return_mask must align with eligible")
        if len(eviction) != len(eligible) or set(eviction) != set(range(len(eligible))):
            raise ValueError("eviction_order_indices must be a permutation")
        if len(matched) != len(gate_domains):
            raise ValueError("matched_gate_genes must align with gate domains")
        if self.min_returns < 0 or self.min_returns > len(eligible):
            raise ValueError("min_returns is outside the eligible range")
        if (not isinstance(self.selected_horizon, int)
                or isinstance(self.selected_horizon, bool)
                or not 0 <= self.selected_horizon <= 8):
            raise ValueError("selected_horizon must be in [0, 8]")
        if any(term.depth > self.selected_horizon for term in forecast_terms):
            raise ValueError("forecast term exceeds selected_horizon")
        if self.selected_horizon == 0 and forecast_terms:
            raise ValueError("strict H=0 problem cannot contain forecast terms")
        if self.decision_policy not in {
                "optimize", "always_stay", "always_return", "adjacent_only"}:
            raise ValueError("unknown rich decision_policy")
        if len(set(occupied_storage)) != len(occupied_storage):
            raise ValueError("occupied storage site ids must be unique")
        if any(site not in set(self.architecture.storage_site_ids)
               for site in occupied_storage):
            raise ValueError("occupied storage id is not registered in architecture")
        object.__setattr__(self, "current_points", points)
        object.__setattr__(self, "participants", participants)
        object.__setattr__(self, "gate_domains", gate_domains)
        object.__setattr__(self, "static_ghosts", ghosts)
        object.__setattr__(self, "eligible", eligible)
        object.__setattr__(self, "eviction_order_indices", eviction)
        object.__setattr__(self, "forced_return_mask", forced)
        object.__setattr__(self, "return_domains", return_domains)
        object.__setattr__(self, "matched_gate_genes", matched)
        object.__setattr__(self, "occupied_storage_site_ids", occupied_storage)
        object.__setattr__(self, "current_site_ids", current_site_ids)
        object.__setattr__(self, "forecast_terms", forecast_terms)

    @property
    def indexed_geometry(self) -> bool:
        return bool(self.current_site_ids)

    def return_location(self, eligible_index: int, site_id: int):
        for option in self.return_domains[eligible_index]:
            if option.site_id == site_id:
                return option.site_location
        raise KeyError((eligible_index, site_id))

    def flat_buffers(self) -> dict[str, array]:
        current_xy = array("d")
        if not self.indexed_geometry:
            for point in self.current_points:
                current_xy.extend(point.to_wire())
        participant_values = array("q", self.participants)
        static_ghost_atoms = array("q")
        static_ghost_xy = array("d")
        for ghost in self.static_ghosts:
            static_ghost_atoms.append(ghost.atom)
            if not self.indexed_geometry:
                static_ghost_xy.extend(ghost.position.to_wire())
        gate_option_offsets = array("q", [0])
        gate_site_ids = array("q")
        gate_q1 = array("q")
        gate_q2 = array("q")
        gate_targets = array("d")
        gate_target_site_ids = array("q")
        gate_leg_offsets = array("q", [0])
        gate_leg_values = array("d")
        gate_owner_offsets = array("q", [0])
        gate_owners = array("q")
        gate_seated_offsets = array("q", [0])
        gate_seated_atoms = array("q")
        gate_seated_xy = array("d")
        option_count = 0
        for domain in self.gate_domains:
            for option in domain:
                gate_site_ids.append(option.site_id)
                gate_q1.append(option.q1)
                gate_q2.append(option.q2)
                if self.indexed_geometry:
                    gate_target_site_ids.extend(
                        (option.target1_site_id, option.target2_site_id))
                else:
                    gate_targets.extend(option.target1.to_wire())
                    gate_targets.extend(option.target2.to_wire())
                for leg in (() if self.indexed_geometry else option.legs):
                    gate_leg_values.extend(leg.to_wire())
                gate_leg_offsets.append(len(gate_leg_values) // 5)
                if not self.indexed_geometry:
                    gate_owners.extend(option.owners)
                gate_owner_offsets.append(len(gate_owners))
                for ghost in (() if self.indexed_geometry
                              else option.seated_ghosts):
                    gate_seated_atoms.append(ghost.atom)
                    gate_seated_xy.extend(ghost.position.to_wire())
                gate_seated_offsets.append(len(gate_seated_atoms))
                option_count += 1
            gate_option_offsets.append(option_count)
        return_option_offsets = array("q", [0])
        return_site_ids = array("q")
        return_costs = array("d")
        return_xy = array("d")
        return_count = 0
        for domain in self.return_domains:
            for option in domain:
                return_site_ids.append(option.site_id)
                return_costs.append(option.cost)
                if not self.indexed_geometry:
                    if option.point is None:
                        raise ValueError("legacy RETURN option needs point")
                    return_xy.extend(option.point.to_wire())
                return_count += 1
            return_option_offsets.append(return_count)
        policy_codes = {
            "optimize": 0,
            "always_stay": 1,
            "always_return": 2,
            "adjacent_only": 3,
        }
        forecast_kind_codes = {
            "constant": 0,
            "stay": 1,
            "return": 2,
            "return_site": 3,
            "gate_option": 4,
            "stay_pair": 5,
            "return_pair": 6,
        }
        forecast_category_codes = {
            "residency": 0,
            "reentry": 1,
            "terminal": 2,
        }
        return {
            "geometry_mode": array("B", [1 if self.indexed_geometry else 0]),
            "current_xy": current_xy,
            "current_site_ids": array("q", self.current_site_ids),
            "participants": participant_values,
            "static_ghost_atoms": static_ghost_atoms,
            "static_ghost_xy": static_ghost_xy,
            "eligible": array("q", self.eligible),
            "min_returns": array("q", [self.min_returns]),
            "eviction_order_indices": array("q", self.eviction_order_indices),
            "forced_return_mask": array("B", self.forced_return_mask),
            "decision_policy": array("B", [policy_codes[self.decision_policy]]),
            "matched_gate_genes": array("q", self.matched_gate_genes),
            "gate_option_offsets": gate_option_offsets,
            "gate_site_ids": gate_site_ids,
            "gate_q1": gate_q1,
            "gate_q2": gate_q2,
            "gate_targets": gate_targets,
            "gate_target_site_ids": gate_target_site_ids,
            "gate_leg_offsets": gate_leg_offsets,
            "gate_leg_values": gate_leg_values,
            "gate_owner_offsets": gate_owner_offsets,
            "gate_owners": gate_owners,
            "gate_seated_offsets": gate_seated_offsets,
            "gate_seated_atoms": gate_seated_atoms,
            "gate_seated_xy": gate_seated_xy,
            "return_option_offsets": return_option_offsets,
            "return_site_ids": return_site_ids,
            "return_costs": return_costs,
            "return_xy": return_xy,
            "occupied_storage_site_ids": array(
                "q", self.occupied_storage_site_ids),
            "forecast_depths": array(
                "q", (term.depth for term in self.forecast_terms)),
            "forecast_kinds": array(
                "B", (forecast_kind_codes[term.kind]
                      for term in self.forecast_terms)),
            "forecast_categories": array(
                "B", (forecast_category_codes[term.category]
                      for term in self.forecast_terms)),
            "forecast_indices": array(
                "q", (term.index for term in self.forecast_terms)),
            "forecast_second_indices": array(
                "q", (term.second_index for term in self.forecast_terms)),
            "forecast_selectors": array(
                "q", (term.selector for term in self.forecast_terms)),
            "forecast_nll": array(
                "d", (term.nll for term in self.forecast_terms)),
        }


@dataclass(frozen=True, slots=True)
class RichH0Result:
    winner: FitnessResult
    gate_option_indices: tuple[int, ...]
    return_assignments: tuple[tuple[int, int], ...]
    rng_state: tuple
    search_mode: str
    operator_profile: str
    evaluations: int
    unique_evaluations: int
    deterministic_unique_evaluations: int
    stochastic_unique_evaluations: int
    fitness_hits: int
    decode_hits: int
    return_match_hits: int
    generations: int
    early_stopped: bool
    stochastic_budget: int
    early_stop_reason: str
    operator_stats: Mapping[str, int]
    forecast_terms_applied: int
    forecast_terms_skipped_cutoff: int
    forecast_nll: float
    search_negative_log_fidelity: float
    forecast_by_depth: tuple[float, ...]
    forecast_breakdown: Mapping[str, float]
    timing: Mapping[str, int]

    @property
    def search_objective(self) -> tuple:
        return (
            self.search_negative_log_fidelity,
            self.winner.move_batches,
            self.winner.move_time_us,
            self.winner.total_distance_um,
            self.winner.chromosome,
        )


# The ABI3 generic name; old imports remain source-compatible for strict M3.
RichBoundaryProblem = RichH0Problem
RichBoundaryResult = RichH0Result
