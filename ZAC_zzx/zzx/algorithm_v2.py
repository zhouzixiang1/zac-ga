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


ADAPTIVE_HORIZON_V1 = {
    "mode": "adaptive",
    "max_horizon": 2,
    "policy": "reuse_pressure_v1",
}
# Legacy adaptive H=0/1/2 remains importable for regression only.  Formal M3
# and M4 use the same geometric-decay contract and differ solely in the maximum
# future depth made visible by ForecastOracle.
DECAY_LOOKAHEAD_POLICY_V1 = "physical_terminal_decay_v1"
DECAY_LOOKAHEAD_BASE_V1 = {
    "mode": "decay",
    "policy": DECAY_LOOKAHEAD_POLICY_V1,
    "decay": "geometric",
    "rho": 0.6,
    "epsilon": 0.05,
}


def decay_lookahead_spec(max_horizon: int, *, rho: float = 0.6) -> dict:
    return {
        **DECAY_LOOKAHEAD_BASE_V1,
        "rho": float(rho),
        "max_horizon": int(max_horizon),
    }


FORMAL_NL_LOOKAHEAD_V1 = decay_lookahead_spec(0)
FORMAL_LK_LOOKAHEAD_V1 = decay_lookahead_spec(8)
SCHEMA2_METHOD_HORIZON = {
    "ours_nl": FORMAL_NL_LOOKAHEAD_V1,
    "ours_lk": FORMAL_LK_LOOKAHEAD_V1,
}
SCHEMA2_FORBIDDEN_KEYS = {"w_ghost", "w_ord", "gamma0", "gamma_batch"}
SCHEMA2_PAIR_EXEMPT_KEYS = {"method_id", "dir"}

# A native run is an auditable experiment contract, not a best-effort backend
# preference.  Keeping the registered values here makes an ABI/RNG change an
# explicit experiment-revision event instead of a silent wheel substitution.
FORMAL_NATIVE_ALGORITHM_REVISION = "native-ga-v1"
FORMAL_NATIVE_TUNING_PROTOCOL_ID = "resident-ga-native-v1"
FORMAL_NATIVE_ABI_VERSION = 3
FORMAL_NATIVE_RNG_VERSION = "python-random-mt19937-v1"
SCHEMA2_NATIVE_REQUIRED_KEYS = {
    "algorithm_revision",
    "backend",
    "early_stop_patience",
    "elite_count",
    "formal_native",
    "native_abi_version",
    "native_fail_closed",
    "native_wheel_sha256",
    "operator_profile",
    "rng_version",
    "tuning_protocol_id",
}
SCHEMA2_NATIVE_MARKER_KEYS = (
    SCHEMA2_NATIVE_REQUIRED_KEYS
    - {"backend", "early_stop_patience", "elite_count", "operator_profile"}
)


def _is_integer(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_nonempty_string(setting: dict, key: str) -> str:
    value = setting[key]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} 必须是非空字符串")
    return value


def _validate_native_contract(setting: dict) -> None:
    """Validate the frozen native-GA provenance and fail-closed controls."""
    missing = sorted(SCHEMA2_NATIVE_REQUIRED_KEYS - set(setting))
    if missing:
        raise ValueError(f"正式 native Schema 2 缺少必需设置键: {missing}")
    if setting["backend"] != "native":
        raise ValueError("正式 native Schema 2 必须设置 backend='native'")
    if setting["native_fail_closed"] is not True:
        raise ValueError("正式 native Schema 2 必须设置 native_fail_closed=true")
    if setting["formal_native"] is not True:
        raise ValueError("正式 native Schema 2 必须设置 formal_native=true")
    if setting["operator_profile"] != "tuned":
        raise ValueError(
            "正式 native Schema 2 必须显式设置 operator_profile='tuned'")

    revision = _require_nonempty_string(setting, "algorithm_revision")
    if revision != FORMAL_NATIVE_ALGORITHM_REVISION:
        raise ValueError(
            "正式 native algorithm_revision 必须为 "
            f"{FORMAL_NATIVE_ALGORITHM_REVISION!r}")
    protocol = _require_nonempty_string(setting, "tuning_protocol_id")
    if protocol != FORMAL_NATIVE_TUNING_PROTOCOL_ID:
        raise ValueError(
            "正式 native tuning_protocol_id 必须为 "
            f"{FORMAL_NATIVE_TUNING_PROTOCOL_ID!r}")

    abi = setting["native_abi_version"]
    if not _is_integer(abi) or abi != FORMAL_NATIVE_ABI_VERSION:
        raise ValueError(
            "正式 native native_abi_version 必须为 "
            f"{FORMAL_NATIVE_ABI_VERSION}")
    wheel_sha256 = _require_nonempty_string(setting, "native_wheel_sha256")
    if (len(wheel_sha256) != 64
            or any(character not in "0123456789abcdefABCDEF"
                   for character in wheel_sha256)):
        raise ValueError("native_wheel_sha256 必须是64位十六进制SHA256")
    rng_version = _require_nonempty_string(setting, "rng_version")
    if rng_version != FORMAL_NATIVE_RNG_VERSION:
        raise ValueError(
            "正式 native rng_version 必须为 "
            f"{FORMAL_NATIVE_RNG_VERSION!r}")

    elite_count = setting["elite_count"]
    if not _is_integer(elite_count) or elite_count <= 0:
        raise ValueError("elite_count 必须是正整数")
    if elite_count > setting["population_size"]:
        raise ValueError("elite_count 不能超过 population_size")
    patience = setting["early_stop_patience"]
    if not _is_integer(patience) or patience < 0:
        raise ValueError("early_stop_patience 必须是非负整数")
    max_unique = setting.get("max_unique_evaluations")
    if max_unique is not None:
        if not _is_integer(max_unique) or max_unique <= 0:
            raise ValueError("max_unique_evaluations 必须是正整数")
        if max_unique < setting["population_size"]:
            raise ValueError(
                "max_unique_evaluations 不能小于 population_size")


def resolved_max_unique_evaluations(setting: dict) -> int:
    """Return the effective unique-fitness budget for a resolved config."""
    explicit = setting.get("max_unique_evaluations")
    if explicit is not None:
        if not _is_integer(explicit) or explicit <= 0:
            raise ValueError("max_unique_evaluations 必须是正整数")
        return explicit
    return (int(setting["population_size"]) * int(setting["iterations"])
            * int(setting["neighbor_sample_size"]))


def validate_decay_lookahead_spec(spec, *, expected_max_horizon: int) -> dict:
    """Validate the registered bounded geometric forecast contract."""
    if not isinstance(spec, dict):
        raise ValueError("正式 lookahead_horizon 必须是 decay spec 对象")
    expected_keys = {
        "mode", "policy", "decay", "rho", "epsilon", "max_horizon"}
    if set(spec) != expected_keys:
        raise ValueError(
            "decay lookahead spec 键必须恰好为 " + str(sorted(expected_keys)))
    if spec["mode"] != "decay":
        raise ValueError("正式 lookahead mode 必须为 decay")
    if spec["policy"] != DECAY_LOOKAHEAD_POLICY_V1:
        raise ValueError(
            f"正式 lookahead policy 必须为 {DECAY_LOOKAHEAD_POLICY_V1}")
    if spec["decay"] != "geometric":
        raise ValueError("正式 lookahead decay 必须为 geometric")
    horizon = spec["max_horizon"]
    if (not _is_integer(horizon) or horizon != expected_max_horizon):
        raise ValueError(
            f"正式 max_horizon 必须为 {expected_max_horizon}")
    rho = spec["rho"]
    if (not isinstance(rho, (int, float)) or isinstance(rho, bool)
            or float(rho) not in {0.4, 0.6, 0.8}):
        raise ValueError("正式 rho 必须来自注册集合 {0.4,0.6,0.8}")
    epsilon = spec["epsilon"]
    if (not isinstance(epsilon, (int, float)) or isinstance(epsilon, bool)
            or float(epsilon) != 0.05):
        raise ValueError("正式 epsilon 必须固定为 0.05")
    return {
        **spec,
        "rho": float(rho),
        "epsilon": float(epsilon),
    }


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
        "fitness_cache", "alpha_lookahead",
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
    expected_horizon = SCHEMA2_METHOD_HORIZON[method]["max_horizon"]
    validate_decay_lookahead_spec(
        setting["lookahead_horizon"],
        expected_max_horizon=expected_horizon)
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
    alpha = setting["alpha_lookahead"]
    if (not isinstance(alpha, (int, float)) or isinstance(alpha, bool)
            or float(alpha) <= 0.0):
        raise ValueError("alpha_lookahead 必须是正数")

    # Legacy/reference Schema-2 fixtures remain a deliberate Python-oracle
    # compatibility path.  Requesting native execution, however, always opts
    # into the complete formal contract; a partial set of provenance keys is
    # rejected rather than downgraded to reference execution.
    native_contract_present = bool(
        SCHEMA2_NATIVE_MARKER_KEYS & set(setting))
    backend = setting.get("backend")
    if backend not in {None, "reference", "native"}:
        raise ValueError("Schema 2 backend 只允许 reference 或 native")
    if backend == "native" or native_contract_present:
        _validate_native_contract(setting)


def validate_schema2_pair(first: dict, second: dict) -> None:
    """Verify that the formal M3/M4 pair differs only in registered identity."""
    validate_schema2_setting(first)
    validate_schema2_setting(second)
    if {first["method_id"], second["method_id"]} != set(SCHEMA2_METHOD_HORIZON):
        raise ValueError("Schema 2 公平对必须恰好包含 ours_nl 与 ours_lk")
    first_lookahead = dict(first["lookahead_horizon"])
    second_lookahead = dict(second["lookahead_horizon"])
    first_max = first_lookahead.pop("max_horizon")
    second_max = second_lookahead.pop("max_horizon")
    if {first_max, second_max} != {0, 8}:
        raise ValueError("NL/LK max_horizon 必须恰好为 0/8")
    if first_lookahead != second_lookahead:
        raise ValueError(
            "NL/LK decay spec 除 max_horizon 外必须逐项相同: "
            f"{first_lookahead!r} != {second_lookahead!r}")
    keys = ((set(first) | set(second)) - SCHEMA2_PAIR_EXEMPT_KEYS
            - {"lookahead_horizon"})
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
    """Read-only, contract-limited view after the current transition.

    The target layer ``L+1`` is current work and never discounted.  Formal
    future reads use ``alpha * rho**(offset-1)`` and stop *before reading* the
    first layer whose bare decay factor is below epsilon.  Alpha scales the
    heuristic magnitude but never changes visible depth.  Thus M3 (H=0)
    has no API path to any future provider read, while M4 remains bounded by 8.
    Integer and legacy-adaptive inputs remain available only for regression.
    """

    def __init__(self, gate_scheduling: Sequence[Sequence[Sequence[int]]] |
                 ForecastLayerProvider,
                 horizon,
                 alpha_lookahead: float = 1.0):
        resolved_horizon = maximum_lookahead_horizon(horizon)
        if (not isinstance(alpha_lookahead, (int, float))
                or isinstance(alpha_lookahead, bool)
                or float(alpha_lookahead) < 0.0):
            raise ValueError("alpha_lookahead 必须是非负数")
        self._provider = (
            gate_scheduling if isinstance(gate_scheduling, ForecastLayerProvider)
            else _FrozenForecastLayerProvider(gate_scheduling))
        self.lookahead_spec = (
            dict(horizon) if isinstance(horizon, dict) else horizon)
        self.horizon = resolved_horizon
        self.alpha_lookahead = float(alpha_lookahead)
        self.decay_mode = is_decay_lookahead(horizon)

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
            if self.future_decay_factor(offset) == 0.0:
                break
            layer = boundary_layer + 1 + offset
            if layer >= self.layer_count:
                break
            yield layer, self.future_layer(boundary_layer, offset)

    def future_decay_factor(self, offset: int) -> float:
        """Return the bare decay factor, or zero once the cutoff is reached.

        Visibility is deliberately independent of ``alpha_lookahead``.  In
        particular, changing alpha may scale every forecast term but can never
        change which provider layers the registered lookahead window exposes.
        """
        if (not isinstance(offset, int) or isinstance(offset, bool)
                or offset < 1):
            raise ForecastBoundaryError("future offset 必须从 1 开始")
        if offset > self.horizon:
            raise ForecastBoundaryError(
                f"future offset {offset} 超出 H={self.horizon}")
        if not self.decay_mode:
            return 1.0
        spec = self.lookahead_spec
        decay_factor = float(spec["rho"]) ** (offset - 1)
        if decay_factor < float(spec["epsilon"]):
            return 0.0
        return decay_factor

    def future_weight(self, offset: int) -> float:
        """Return ``alpha_lookahead * decay_factor`` after bare-factor cutoff."""
        decay_factor = self.future_decay_factor(offset)
        if not self.decay_mode:
            return decay_factor
        return self.alpha_lookahead * decay_factor

    def weighted_future(self, boundary_layer: int):
        """Yield ``(absolute layer, gates, offset, weight)`` without hidden reads."""
        for offset in range(1, self.horizon + 1):
            if self.future_decay_factor(offset) == 0.0:
                break
            weight = self.future_weight(offset)
            layer = boundary_layer + 1 + offset
            if layer >= self.layer_count:
                break
            yield (layer, self.future_layer(boundary_layer, offset),
                   offset, weight)

    @property
    def effective_horizon(self) -> int:
        result = 0
        for offset in range(1, self.horizon + 1):
            if self.future_decay_factor(offset) == 0.0:
                break
            result = offset
        return result

    def next_use(self, q: int, boundary_layer: int):
        """First visible *future* use, never the current target layer."""
        for layer, gates in self.visible_future(boundary_layer):
            for q0, q1 in gates:
                if q == q0:
                    return layer, q1
                if q == q1:
                    return layer, q0
        return None

    def bounded(self, horizon: int) -> "ForecastOracle":
        """Return a narrower view backed by the same provider without reading it.

        This is a legacy-regression helper and a useful strictness primitive.
        Formal decay M4 normally consumes its complete registered bounded window;
        refusing expansion is still important because an H=0 oracle can never be
        turned into a future-reading oracle through this helper.
        """
        if not isinstance(horizon, int) or isinstance(horizon, bool):
            raise ValueError("bounded horizon 必须是整数")
        if horizon < 0 or horizon > self.horizon:
            raise ForecastBoundaryError(
                f"不能将 H={self.horizon} 的 oracle 扩展为 H={horizon}")
        if self.decay_mode:
            narrowed = dict(self.lookahead_spec)
            narrowed["max_horizon"] = horizon
            return ForecastOracle(
                self._provider, narrowed, self.alpha_lookahead)
        return ForecastOracle(
            self._provider, horizon, self.alpha_lookahead)

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


def maximum_lookahead_horizon(spec) -> int:
    """Resolve the storage horizon used to construct the sole ForecastOracle."""
    if isinstance(spec, int) and not isinstance(spec, bool) and spec >= 0:
        return spec
    if isinstance(spec, dict):
        mode = spec.get("mode")
        if mode == "decay":
            if spec.get("policy") != DECAY_LOOKAHEAD_POLICY_V1:
                raise ValueError(f"未知衰减前瞻策略: {spec!r}")
            if spec.get("decay") != "geometric":
                raise ValueError(f"未知衰减函数: {spec!r}")
            rho = spec.get("rho")
            epsilon = spec.get("epsilon")
            if (not isinstance(rho, (int, float)) or isinstance(rho, bool)
                    or not 0.0 < float(rho) < 1.0):
                raise ValueError("decay rho 必须在 (0,1) 内")
            if (not isinstance(epsilon, (int, float))
                    or isinstance(epsilon, bool)
                    or not 0.0 < float(epsilon) <= 1.0):
                raise ValueError("decay epsilon 必须在 (0,1] 内")
        elif mode == "adaptive":
            if spec.get("policy") != "reuse_pressure_v1":
                raise ValueError(f"未知动态前瞻策略: {spec!r}")
        else:
            raise ValueError(f"未知动态前瞻模式: {spec!r}")
        value = spec.get("max_horizon")
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError("max_horizon 必须是非负整数")
        return value
    raise ValueError(f"无效 lookahead_horizon: {spec!r}")


def is_adaptive_lookahead(spec) -> bool:
    return isinstance(spec, dict) and spec.get("mode") == "adaptive"


def is_decay_lookahead(spec) -> bool:
    return isinstance(spec, dict) and spec.get("mode") == "decay"


@dataclass(frozen=True)
class AdaptiveHorizonDecision:
    """Auditable deterministic 0/1/2 choice for one resident boundary.

    The selector values only reuse that can still be influenced by the current
    boundary: non-participating residents may STAY/RETURN, while target-layer
    participants may have their gate seats chosen for later reuse.  A bounded
    pressure term favours the second future layer only when the boundary has many
    decisions, forced capacity evictions, or already active commitments.  The
    fixed depth penalty makes H=0 win when no visible reuse exists and breaks ties
    toward the shallower (faster) horizon.  Policy v1 is deliberately integer/
    count based and fixed in code for reproducibility:

    ``S_h = 4R_h + 2.5T_h + 0.5G_h + 0.5C_h + pL_h - 1.25h``

    where R/T are unique reused resident/target atoms, G is the number of coupled
    future gates, C is repeated reuse, L is the number of useful future layers,
    and ``p=min(decision_pressure, 8)/8``.
    """

    selected_horizon: int
    reason: str
    scores: tuple[float | None, ...]
    available_horizon: int
    resident_reuse_by_offset: tuple[int, ...]
    target_reuse_by_offset: tuple[int, ...]
    decision_pressure: int
    capacity_deficit: int

    @classmethod
    def fixed(cls, horizon: int, *, reason: str | None = None):
        if (not isinstance(horizon, int) or isinstance(horizon, bool)
                or horizon < 0):
            raise ValueError("registered fixed horizon 必须为非负整数")
        scores: list[float | None] = [None] * (horizon + 1)
        scores[horizon] = 0.0
        return cls(
            selected_horizon=horizon,
            reason=reason or ("fixed_zero_no_future_access" if horizon == 0
                              else "fixed_horizon"),
            scores=tuple(scores),
            available_horizon=horizon,
            resident_reuse_by_offset=(0,) * horizon,
            target_reuse_by_offset=(0,) * horizon,
            decision_pressure=0,
            capacity_deficit=0,
        )

    @classmethod
    def select(cls, oracle: ForecastOracle, boundary_layer: int, *,
               residents: Iterable[int],
               target_participants: Iterable[int],
               zone_sites: int,
               theta_capacity: float,
               active_commitments: Iterable[int] = ()):
        """Choose the smallest useful horizon from a registered H<=2 oracle."""
        if oracle.horizon < 1 or oracle.horizon > 2:
            raise ValueError("adaptive reuse_pressure_v1 要求 max_horizon 为 1 或 2")
        if zone_sites <= 0:
            raise ValueError("zone_sites 必须为正整数")
        if not 0.0 < theta_capacity <= 1.0:
            raise ValueError("theta_capacity 必须在 (0, 1] 内")

        resident_set = {int(q) for q in residents}
        target_set = {int(q) for q in target_participants}
        eligible = resident_set - target_set
        visible = {}
        for absolute_layer, gates in oracle.visible_future(boundary_layer):
            offset = absolute_layer - boundary_layer - 1
            if offset <= 2:
                visible[offset] = tuple(
                    (int(gate[0]), int(gate[1])) for gate in gates)
        available = max(visible, default=0)

        resident_reuse = [0, 0]
        target_reuse = [0, 0]
        relevant_by_offset: list[set[int]] = [set(), set()]
        coupled_gates = [0, 0]
        for offset in (1, 2):
            gates = visible.get(offset, ())
            atoms = {q for gate in gates for q in gate}
            resident_atoms = atoms & eligible
            target_atoms = atoms & target_set
            relevant = resident_atoms | target_atoms
            resident_reuse[offset - 1] = len(resident_atoms)
            target_reuse[offset - 1] = len(target_atoms)
            relevant_by_offset[offset - 1] = relevant
            coupled_gates[offset - 1] = sum(
                1 for gate in gates if set(gate) & relevant)

        capacity = int(theta_capacity * zone_sites + 1e-12)
        capacity_deficit = max(0, len(eligible) + len(target_set) - capacity)
        commitment_count = len({int(q) for q in active_commitments})
        decision_pressure = (
            len(eligible) + 2 * capacity_deficit + commitment_count)
        pressure_scale = min(decision_pressure, 8) / 8.0

        scores: list[float | None] = [0.0, None, None]
        seen: set[int] = set()
        appearances: dict[int, int] = {}
        reuse_layers = 0
        coupled_total = 0
        for horizon in range(1, available + 1):
            relevant = relevant_by_offset[horizon - 1]
            if relevant:
                reuse_layers += 1
            seen.update(relevant)
            for q in relevant:
                appearances[q] = appearances.get(q, 0) + 1
            coupled_total += coupled_gates[horizon - 1]
            resident_seen = len(seen & eligible)
            target_seen = len(seen & target_set)
            continuity = sum(max(0, count - 1)
                             for count in appearances.values())
            scores[horizon] = round(
                4.0 * resident_seen
                + 2.5 * target_seen
                + 0.5 * coupled_total
                + 0.5 * continuity
                + pressure_scale * reuse_layers
                - 1.25 * horizon,
                12,
            )

        candidates = [(score, -horizon, horizon)
                      for horizon, score in enumerate(scores)
                      if score is not None]
        selected = max(candidates)[2]
        if selected == 0:
            reason = ("no_visible_future" if available == 0
                      else "no_actionable_visible_reuse")
        elif selected == 1:
            reason = ("adjacent_reuse_only"
                      if not relevant_by_offset[1]
                      else "adjacent_reuse_dominates")
        else:
            new_second = relevant_by_offset[1] - relevant_by_offset[0]
            if new_second:
                reason = "second_future_layer_adds_reuse"
            elif pressure_scale >= 0.25:
                reason = "multi_layer_reuse_under_pressure"
            else:
                reason = "multi_layer_reuse_continuity"
        return cls(
            selected_horizon=selected,
            reason=reason,
            scores=tuple(scores),
            available_horizon=available,
            resident_reuse_by_offset=tuple(resident_reuse),
            target_reuse_by_offset=tuple(target_reuse),
            decision_pressure=decision_pressure,
            capacity_deficit=capacity_deficit,
        )

    def as_log(self) -> dict:
        return {
            "selected_horizon": self.selected_horizon,
            "horizon_reason": self.reason,
            "horizon_scores": {
                str(index): score for index, score in enumerate(self.scores)
            },
            "horizon_features": {
                "available_horizon": self.available_horizon,
                "resident_reuse_by_offset": list(
                    self.resident_reuse_by_offset),
                "target_reuse_by_offset": list(self.target_reuse_by_offset),
                "decision_pressure": self.decision_pressure,
                "capacity_deficit": self.capacity_deficit,
            },
        }


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
