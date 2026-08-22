"""Schema-2 contracts and physical objective for the resident GA.

This module deliberately contains only deterministic, side-effect-free helpers.  The
compiler and the experiment runner can therefore share the same validation rules
without importing the full ZAC pipeline.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import dist, log, log1p, sqrt
from typing import Callable, Iterable, Sequence

from zzx.zcost import compatible_2d, greedy_phase_batches, phase_batches


SCHEMA2_METHOD_HORIZON = {"ours_nl": 0, "ours_lk": 2}
SCHEMA2_FORBIDDEN_KEYS = {"w_ghost", "w_ord", "gamma0", "gamma_batch"}
SCHEMA2_PAIR_EXEMPT_KEYS = {"method_id", "dir", "lookahead_horizon"}


def validate_schema2_setting(setting: dict) -> None:
    """Fail closed when a formal M3/M4 setting is not the registered experiment.

    Schema-1 settings remain a compatibility path and are intentionally not handled
    here.  A Schema-2 run is strict: legacy proxy weights cannot silently turn into
    the method difference, and the method id fixes the only permitted horizon.
    """
    if setting.get("experiment_schema") != 2:
        raise ValueError("Schema 2 设置必须显式包含 experiment_schema=2")
    required = {
        "method_id", "objective", "lookahead_horizon", "population_size",
        "iterations", "neighbors_per_solution", "neighbor_sample_size",
        "seed", "placer", "engine", "routing_strategy", "resyn",
        "fitness_cache",
    }
    missing = sorted(required - set(setting))
    if missing:
        raise ValueError(f"Schema 2 缺少必需设置键: {missing}")
    present_forbidden = sorted(SCHEMA2_FORBIDDEN_KEYS & set(setting))
    if present_forbidden:
        raise ValueError(
            "Schema 2 禁止用旧代理权重区分 NL/LK: " + str(present_forbidden))
    method = setting["method_id"]
    if method not in SCHEMA2_METHOD_HORIZON:
        raise ValueError(f"未知 Schema 2 method_id: {method!r}")
    expected_horizon = SCHEMA2_METHOD_HORIZON[method]
    if setting["lookahead_horizon"] != expected_horizon:
        raise ValueError(
            f"{method} 必须使用 lookahead_horizon={expected_horizon}，"
            f"实际为 {setting['lookahead_horizon']!r}")
    if setting["objective"] != "physical_log_fidelity":
        raise ValueError("Schema 2 objective 必须为 physical_log_fidelity")
    if setting["placer"] != "resident" or setting["engine"] != "ga":
        raise ValueError("Schema 2 M3/M4 必须使用 resident + ga")
    if setting["routing_strategy"] != "coloring":
        raise ValueError("Schema 2 M3/M4 必须使用分相位 coloring 路由")
    if setting["resyn"] is not False:
        raise ValueError("Schema 2 正式输入已经 canonicalize，必须设置 resyn=false")
    if setting.get("fitness_mode", "phase") != "phase":
        raise ValueError("Schema 2 只允许 phase fitness")
    for key in ("population_size", "iterations", "neighbors_per_solution",
                "neighbor_sample_size"):
        value = setting[key]
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"{key} 必须是正整数，实际为 {value!r}")
    if not isinstance(setting["seed"], int) or isinstance(setting["seed"], bool):
        raise ValueError("seed 必须是整数")
    if not isinstance(setting["fitness_cache"], bool):
        raise ValueError("fitness_cache 必须是布尔值")


def validate_schema2_pair(first: dict, second: dict) -> None:
    """Verify that the formal M3/M4 pair differs only in registered identity."""
    validate_schema2_setting(first)
    validate_schema2_setting(second)
    if {first["method_id"], second["method_id"]} != set(SCHEMA2_METHOD_HORIZON):
        raise ValueError("Schema 2 公平对必须恰好包含 ours_nl 与 ours_lk")
    keys = (set(first) | set(second)) - SCHEMA2_PAIR_EXEMPT_KEYS
    missing = object()
    differences = {}
    for key in sorted(keys):
        left, right = first.get(key, missing), second.get(key, missing)
        if left != right:
            differences[key] = (
                "<MISSING>" if left is missing else left,
                "<MISSING>" if right is missing else right,
            )
    if differences:
        raise ValueError(f"NL/LK 除 horizon/身份/输出目录外存在配置差异: {differences}")


class ForecastBoundaryError(IndexError):
    """Raised when an algorithm tries to observe a layer outside its horizon."""


class ForecastLayerProvider:
    """Bounded backing store for :class:`ForecastOracle` layers.

    Batch compilation passes an ordinary sequence and keeps its historic frozen
    copy. Large compilation supplies a provider whose implementation may retain
    only a small SQLite-backed ring. The oracle remains the sole horizon gate in
    both cases.
    """

    @property
    def layer_count(self) -> int:
        raise NotImplementedError

    def read_layer(self, layer: int) -> Sequence[Sequence[int]]:
        raise NotImplementedError


class _FrozenForecastLayerProvider(ForecastLayerProvider):
    """Exact compatibility adapter for the existing in-memory schedule."""

    def __init__(self, gate_scheduling: Sequence[Sequence[Sequence[int]]]):
        self._schedule = tuple(
            tuple((int(g[0]), int(g[1])) for g in layer)
            for layer in gate_scheduling)

    @property
    def layer_count(self) -> int:
        return len(self._schedule)

    def read_layer(self, layer: int) -> tuple[tuple[int, int], ...]:
        return self._schedule[layer]


class ForecastOracle:
    """Read-only, horizon-limited view of layers after the current transition.

    At boundary ``L`` the target layer ``L+1`` is current work and is always visible.
    A horizon of two additionally exposes ``L+2`` and ``L+3``.  H=0 therefore has
    no API path to future partners, which makes the no-lookahead ablation auditable.
    """

    def __init__(self, gate_scheduling: Sequence[Sequence[Sequence[int]]] |
                 ForecastLayerProvider,
                 horizon: int):
        if not isinstance(horizon, int) or isinstance(horizon, bool) or horizon < 0:
            raise ValueError("lookahead_horizon 必须是非负整数")
        self._provider = (
            gate_scheduling if isinstance(gate_scheduling, ForecastLayerProvider)
            else _FrozenForecastLayerProvider(gate_scheduling))
        self.horizon = horizon

    @property
    def layer_count(self) -> int:
        return self._provider.layer_count

    def target_layer(self, boundary_layer: int) -> tuple[tuple[int, int], ...]:
        return self._read(boundary_layer, boundary_layer + 1, allow_target=True)

    def future_layer(self, boundary_layer: int,
                     offset: int) -> tuple[tuple[int, int], ...]:
        """Return future offset 1..H (1 means L+2), rejecting all other reads."""
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 1:
            raise ForecastBoundaryError("future offset 必须从 1 开始")
        return self._read(boundary_layer, boundary_layer + 1 + offset,
                          allow_target=False)

    def visible_future(self, boundary_layer: int):
        for offset in range(1, self.horizon + 1):
            layer = boundary_layer + 1 + offset
            if layer >= self.layer_count:
                break
            yield layer, self.future_layer(boundary_layer, offset)

    def next_use(self, q: int, boundary_layer: int):
        """First visible *future* use, never the current target layer."""
        for layer, gates in self.visible_future(boundary_layer):
            for q0, q1 in gates:
                if q == q0:
                    return layer, q1
                if q == q1:
                    return layer, q0
        return None

    def _read(self, boundary_layer: int, layer: int, allow_target: bool):
        lower = boundary_layer + 1 if allow_target else boundary_layer + 2
        upper = boundary_layer + 1 + self.horizon
        if layer < lower or layer > upper:
            raise ForecastBoundaryError(
                f"layer {layer} 超出 boundary={boundary_layer}, H={self.horizon} 的可见范围")
        if layer < 0 or layer >= self.layer_count:
            return ()
        return tuple(
            (int(gate[0]), int(gate[1]))
            for gate in self._provider.read_layer(layer))


@dataclass(frozen=True)
class MovementPhaseCost:
    batches: int
    move_time_us: float
    total_distance_um: float
    movers: int


@dataclass(frozen=True)
class PhysicalCostBreakdown:
    negative_log_fidelity: float
    transfer_nll: float
    idle_excitation_nll: float
    coherence_nll: float
    move_batches: int
    move_time_us: float
    total_distance_um: float
    idle_exposures: int
    transfers: int

    def objective(self, chromosome: Sequence[int]):
        """Registered lexicographic fitness including deterministic tie-break."""
        return (
            self.negative_log_fidelity,
            self.move_batches,
            self.move_time_us,
            self.total_distance_um,
            tuple(int(v) for v in chromosome),
        )


class PhysicalIncrementalCost:
    """Candidate-dependent physical -log(F) used inside the resident GA.

    Gate fidelities are common to all candidates in one transition and are restored
    by the independent final scorer.  This incremental objective includes every
    candidate-dependent term: zone-idle Rydberg exposure, load/store fidelity, and
    coherence loss accumulated during physical movement time.
    """

    F_EXC = 0.9975
    F_TRANSFER = 0.999
    T_TRANSFER_US = 15.0
    T_RYDBERG_US = 0.36
    ACCEL_UM_PER_US2 = 0.00275
    T2_US = 1.5e6

    def __init__(self, n_atoms: int):
        if n_atoms <= 0:
            raise ValueError("n_atoms 必须为正")
        self.n_atoms = n_atoms

    def movement_phase(self, legs: Sequence[tuple], ghosts=None, owners=None,
                       exact_threshold: int = 0,
                       batching: str = "phase") -> MovementPhaseCost:
        legs = tuple(legs)
        if not legs:
            return MovementPhaseCost(0, 0.0, 0.0, 0)
        if batching not in {"phase", "greedy"}:
            raise ValueError(f"unknown movement batching policy: {batching!r}")
        # A one-leg conflict graph has exactly one color under both registered
        # batchers.  Pairwise ghost edges cannot exist, so constructing a graph
        # and running DSATUR is pure overhead on serial circuits.
        if len(legs) == 1:
            batches = ((0,),)
        elif len(legs) == 2:
            first = (legs[0][1], legs[0][3], legs[0][2], legs[0][4])
            second = (legs[1][1], legs[1][3], legs[1][2], legs[1][4])
            conflict = not compatible_2d(first, second)
            if not conflict and ghosts:
                from zzx.ghost import pair_edges
                conflict = bool(pair_edges(legs, ghosts, owners=owners))
            order = tuple(sorted(
                range(2), key=lambda index: legs[index][0], reverse=True))
            batches = ((order[0],), (order[1],)) if conflict else (order,)
        elif batching == "phase":
            _, batches, _ = phase_batches(
                legs, ghosts=ghosts, owners=owners,
                exact_threshold=exact_threshold)
        else:
            _, batches, _ = greedy_phase_batches(
                legs, ghosts=ghosts, owners=owners)
        move_time = sum(
            self._expanded_batch_time(legs, members) for members in batches)
        return MovementPhaseCost(
            batches=len(batches),
            move_time_us=move_time,
            total_distance_um=sum(float(leg[0]) for leg in legs),
            movers=len(legs),
        )

    @classmethod
    def _expanded_batch_time(cls, legs: Sequence[tuple], members) -> float:
        """Exact ZAC expanded-AOD duration for one compatible leg batch.

        ``Router_mixin.expand_arrangement`` loads source rows sequentially.  A
        non-final row is parked by one micrometre in both axes before the big
        move, and every row activation costs a transfer interval.  The former
        fitness used only the longest direct leg plus two transfer intervals;
        it therefore underpriced multi-row batches by every intermediate load
        and parking segment.  This coordinate-only form mirrors the router's
        expansion and is shared by NL/LK candidate evaluation.
        """
        selected = [legs[int(i)] for i in members]
        if not selected:
            return 0.0
        if len(selected) == 1:
            # The general expanded-AOD construction collapses exactly to one
            # load, one store, and the direct leg duration for a singleton.
            leg = selected[0]
            longest = dist((float(leg[1]), float(leg[2])),
                           (float(leg[3]), float(leg[4])))
            return (2 * cls.T_TRANSFER_US
                    + sqrt(longest / cls.ACCEL_UM_PER_US2))
        rows: dict[float, list[tuple]] = {}
        for leg in selected:
            rows.setdefault(float(leg[2]), []).append(leg)
        ordered_rows = sorted(rows.items())
        row_count = len(ordered_rows)

        # Each source row is activated separately; all held atoms are released
        # together by one final deactivation.
        duration = (row_count + 1) * cls.T_TRANSFER_US
        if row_count > 1:
            parking_distance = dist((0.0, 0.0), (1.0, 1.0))
            duration += (row_count - 1) * sqrt(
                parking_distance / cls.ACCEL_UM_PER_US2)

        # At the big move, every non-final source row is parked at y+1.  A
        # source column remains parked at x+1 iff its final occurrence was in a
        # non-final row.  ZAC computes the phase makespan over the Cartesian
        # product of active row and column displacements.
        row_moves = []
        last_row_for_x: dict[float, int] = {}
        target_x_for_source: dict[float, float] = {}
        for row_index, (source_y, row_legs) in enumerate(ordered_rows):
            target_y = float(row_legs[0][4])
            row_moves.append((source_y + (1.0 if row_index < row_count - 1 else 0.0),
                              target_y))
            for leg in row_legs:
                source_x, target_x = float(leg[1]), float(leg[3])
                last_row_for_x[source_x] = row_index
                target_x_for_source.setdefault(source_x, target_x)
        column_moves = [
            (source_x + (1.0 if last_row_for_x[source_x] < row_count - 1 else 0.0),
             target_x_for_source[source_x])
            for source_x in sorted(last_row_for_x)
        ]
        longest = max(
            dist((column_begin, row_begin), (column_end, row_end))
            for row_begin, row_end in row_moves
            for column_begin, column_end in column_moves)
        duration += sqrt(longest / cls.ACCEL_UM_PER_US2)
        return duration

    def score(self, phases: Iterable[MovementPhaseCost], idle_exposures: int,
              chromosome: Sequence[int] = ()) -> tuple[tuple, PhysicalCostBreakdown]:
        phases = tuple(phases)
        if idle_exposures < 0:
            raise ValueError("idle_exposures 不得为负")
        movers = 0
        move_batches = 0
        move_time_us = 0.0
        total_distance_um = 0.0
        for phase in phases:
            movers += phase.movers
            move_batches += phase.batches
            move_time_us += phase.move_time_us
            total_distance_um += phase.total_distance_um
        transfers = 2 * movers                 # load and store are separate errors
        transfer_nll = -transfers * log(self.F_TRANSFER)
        excitation_nll = -idle_exposures * log(self.F_EXC)
        # An illuminated resident that is not a CZ participant remains idle
        # for the 0.36-us pulse.  RETURN can remove that exposure, so this is a
        # candidate-dependent coherence term as well as an f_exc term.
        coherence_nll = -idle_exposures * log1p(
            -self.T_RYDBERG_US / self.T2_US)
        for phase in phases:
            if phase.move_time_us <= 0:
                continue
            stationary_idle = phase.move_time_us
            mover_idle = max(0.0, phase.move_time_us - 2 * self.T_TRANSFER_US)
            if stationary_idle >= self.T2_US or mover_idle >= self.T2_US:
                return self._ood(chromosome, phases, idle_exposures, transfers)
            coherence_nll -= (self.n_atoms - phase.movers) * log1p(
                -stationary_idle / self.T2_US)
            coherence_nll -= phase.movers * log1p(-mover_idle / self.T2_US)
        total_nll = transfer_nll + excitation_nll + coherence_nll
        breakdown = PhysicalCostBreakdown(
            negative_log_fidelity=total_nll,
            transfer_nll=transfer_nll,
            idle_excitation_nll=excitation_nll,
            coherence_nll=coherence_nll,
            move_batches=move_batches,
            move_time_us=move_time_us,
            total_distance_um=total_distance_um,
            idle_exposures=idle_exposures,
            transfers=transfers,
        )
        return breakdown.objective(chromosome), breakdown

    def residency_break_even(
            self, round_trip_phases: Iterable[MovementPhaseCost],
            idle_exposures: int) -> tuple[bool, float, float]:
        """Conservative physical admission test for a cross-layer ``STAY``.

        A resident is admitted only when the Rydberg-idle loss incurred before
        its visible reuse is smaller than the *atom-local* loss of a dedicated
        RETURN and re-entry.  Transfer errors and the moving atom's coherence
        are unavoidable local costs.  Coherence accumulated by other atoms is
        deliberately not credited here because phase batching can amortise it;
        the complete candidate objective still accounts for that term exactly.

        The returned tuple is ``(admit_stay, idle_nll, avoided_move_nll)``.
        Ties choose RETURN, giving the guard a deterministic safety boundary.
        """
        phases = tuple(round_trip_phases)
        if idle_exposures < 0:
            raise ValueError("idle_exposures 不得为负")
        idle_nll = -idle_exposures * (
            log(self.F_EXC) + log1p(-self.T_RYDBERG_US / self.T2_US))
        movers = sum(phase.movers for phase in phases)
        avoided_move_nll = -2 * movers * log(self.F_TRANSFER)
        for phase in phases:
            if phase.move_time_us <= 0 or phase.movers <= 0:
                continue
            mover_idle = max(0.0, phase.move_time_us - 2 * self.T_TRANSFER_US)
            if mover_idle >= self.T2_US:
                return True, idle_nll, float("inf")
            avoided_move_nll -= phase.movers * log1p(
                -mover_idle / self.T2_US)
        return idle_nll < avoided_move_nll, idle_nll, avoided_move_nll

    @staticmethod
    def _ood(chromosome, phases, idle_exposures, transfers):
        breakdown = PhysicalCostBreakdown(
            negative_log_fidelity=float("inf"),
            transfer_nll=float("inf"),
            idle_excitation_nll=float("inf"),
            coherence_nll=float("inf"),
            move_batches=sum(p.batches for p in phases),
            move_time_us=sum(p.move_time_us for p in phases),
            total_distance_um=sum(p.total_distance_um for p in phases),
            idle_exposures=idle_exposures,
            transfers=transfers,
        )
        return breakdown.objective(chromosome), breakdown


@dataclass
class CacheStats:
    evaluations: int = 0
    unique_evaluations: int = 0
    fitness_hits: int = 0
    decode_hits: int = 0
    return_match_hits: int = 0

    def as_dict(self) -> dict:
        return {
            "evaluations": self.evaluations,
            "unique_evaluations": self.unique_evaluations,
            "fitness_hits": self.fitness_hits,
            "decode_hits": self.decode_hits,
            "return_match_hits": self.return_match_hits,
        }


def resident_decision_candidates(resident_ids: Iterable[int],
                                 participants: Iterable[int]) -> list[int]:
    """All non-participating residents, deliberately including no-future-use atoms."""
    participant_set = set(participants)
    return sorted(int(q) for q in resident_ids if q not in participant_set)


def build_seed_population(gate_domains: Sequence[int], n_decisions: int,
                          greedy_decisions: Sequence[int], population_size: int,
                          rng, normalize: Callable[[Sequence[int]], Sequence[int]] | None = None,
                          greedy_gate_genes: Sequence[int] | None = None):
    """Build deterministic all-STAY/all-RETURN/physical-greedy GA seeds."""
    if len(greedy_decisions) != n_decisions:
        raise ValueError("greedy_decisions 长度与决策基因数不一致")
    if (greedy_gate_genes is not None and
            len(greedy_gate_genes) != len(gate_domains)):
        raise ValueError("greedy_gate_genes 长度与门位基因数不一致")
    if population_size <= 0:
        raise ValueError("population_size 必须为正")
    gate_zero = [0] * len(gate_domains)
    greedy_gates = (gate_zero if greedy_gate_genes is None else
                    [int(v) for v in greedy_gate_genes])
    raw = [
        gate_zero + [0] * n_decisions,
        gate_zero + [1] * n_decisions,
        greedy_gates + [int(v) for v in greedy_decisions],
    ]
    while len(raw) < population_size * 3:
        raw.append(
            [rng.randrange(max(1, int(domain))) for domain in gate_domains]
            + [rng.randrange(2) for _ in range(n_decisions)])
    unique, seen = [], set()
    for chrom in raw:
        normalized = list(normalize(chrom) if normalize else chrom)
        key = tuple(normalized)
        if key in seen:
            continue
        seen.add(key)
        unique.append(normalized)
        if len(unique) == population_size:
            break
    # Degenerate no-decision/single-domain layers may have fewer unique solutions.
    while len(unique) < population_size:
        unique.append(list(unique[-1] if unique else gate_zero))
    return unique
