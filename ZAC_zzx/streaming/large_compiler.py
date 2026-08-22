"""Executable bounded-memory compiler for the frozen Large experiment track.

The module deliberately sits below the experiment runner.  It consumes the
SQLite dependency-layer store one layer at a time, emits canonical physical
events directly into the incremental verifier/scorer and a concatenable gzip
JSONL stream, and checkpoints only at layer boundaries.  No trace-sized Python
object is ever built.

The four methods share the exact same physical scheduler and differ only in
their registered residency policy:

``M1``
    Development proxy with eager return after every CZ pulse.
``M2``
    Development proxy with adjacent-layer routing awareness.
``M3``
    Physical-cost resident decision with the frozen NL horizon ``H=0``.  The
    oracle exposes the target CZ layer but no extra forecast layer.
``M4``
    The identical physical-cost resident decision with ``H=2``.  It may keep an
    atom across non-adjacent CZ layers when the bounded forecast proves that
    avoiding RETURN plus re-entry is cheaper than idle Rydberg exposure.

Movement is intentionally conservative for the first executable Large core:
one atom is carried by the single AOD at a time.  A direct leg is used when the
shared ghost predicate proves it safe; otherwise the router selects one or two
currently unoccupied *real SLM sites* as waypoints.  Every emitted leg is then
replayed by :class:`IncrementalTraceValidator`, so success implies zero ghost
hits rather than trusting compiler metadata.

This module is an executable integration scaffold, not yet a formal M1/M2/M3/M4
replacement.  In particular, it does not claim byte-equivalence to stock ZAC
or ``mqt.qmap==3.2.0`` and its resident decision kernel is not yet the frozen
Schema-2 GA.  Every result therefore carries ``support_claim_eligible=false``;
the public ``run-large`` command remains fail-closed until the exact adapters
pass their differential gates.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import random
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from evaluation import CanonicalTraceEvent, EventType, FidelityModel
from zac.ds.architecture import Architecture
from zzx.algorithm_v2 import PhysicalIncrementalCost
from zzx.ghost import ghost_hits

from .checkpoint import Checkpoint, EventStreamWriter, load_checkpoint
from .controller import CheckpointController
from .qasm_sqlite import GateEvent, LayerStore
from .trace_pipeline import (
    IncrementalTracePipeline,
    IncrementalTraceScorer,
    IncrementalTraceValidator,
)


_FORMAT = "zac-large-streaming-compiler-v1"
_TRACE_NAME = "canonical_trace.jsonl.gz"
_CHECKPOINT_NAME = "checkpoint.json"
_RESULT_NAME = "result.json"
_STATS_NAME = "compiler_stats.json"
_PROGRESS_NAME = "progress.json"
_FORBIDDEN_SCHEMA2_KEYS = frozenset(("w_ghost", "w_ord", "gamma0", "gamma_batch"))
_METHOD_ALIASES = {
    "M1": "M1",
    "ZAC": "M1",
    "M2": "M2",
    "ICCAD": "M2",
    "QMAP": "M2",
    "M3": "M3",
    "OURS_NL": "M3",
    "OURS-NL": "M3",
    "M4": "M4",
    "OURS_LK": "M4",
    "OURS-LK": "M4",
}
_METHOD_IDS = {
    "M1": frozenset(("M1", "zac", "zac_m1")),
    "M2": frozenset(("M2", "iccad_qmap_3_2_astar", "qmap", "iccad")),
    "M3": frozenset(("M3", "ours_nl")),
    "M4": frozenset(("M4", "ours_lk")),
}
_ZERO_HASH = "0" * 64
_IMPLEMENTATION_STATUS = "development_streaming_proxy_v1"


@dataclass(frozen=True, order=True)
class _Site:
    slm: int
    row: int
    column: int

    def to_list(self) -> list[int]:
        return [self.slm, self.row, self.column]

    @classmethod
    def from_value(cls, value: Sequence[int]) -> "_Site":
        if len(value) != 3:
            raise ValueError(f"invalid SLM site: {value!r}")
        return cls(int(value[0]), int(value[1]), int(value[2]))


@dataclass(frozen=True)
class _EntanglementPair:
    left: _Site
    right: _Site


@dataclass
class LargeCompilerStats:
    format: str = "zac-large-compiler-stats-v1"
    dependency_layers: int = 0
    dependency_events: int = 0
    one_qubit_gates: int = 0
    two_qubit_gates: int = 0
    rydberg_pulses: int = 0
    move_batches_generated: int = 0
    movement_legs: int = 0
    movement_distance_um: float = 0.0
    direct_paths: int = 0
    one_waypoint_paths: int = 0
    two_waypoint_paths: int = 0
    stay_decisions: int = 0
    return_decisions: int = 0
    resident_evictions: int = 0
    physical_decision_evaluations: int = 0
    window_peak_events: int = 0
    resident_peak: int = 0
    checkpoints: int = 0
    checkpoint_reasons: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "LargeCompilerStats":
        allowed = {item.name for item in cls.__dataclass_fields__.values()}
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(f"unknown Large compiler stats fields: {sorted(unknown)}")
        return cls(**dict(value))


def _load_json(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _stable_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("ascii")).hexdigest()


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _resolved_setting(config: Mapping[str, Any]) -> dict[str, Any]:
    settings = config.get("zac_setting")
    if settings is None:
        return {key: value for key, value in config.items() if key != "large_runtime"}
    if not isinstance(settings, list) or len(settings) != 1 or not isinstance(settings[0], dict):
        raise ValueError("Large compiler requires exactly one zac_setting object")
    return dict(settings[0])


def _normalise_method(method: str) -> str:
    key = str(method).strip().upper()
    try:
        return _METHOD_ALIASES[key]
    except KeyError as error:
        raise ValueError(f"unknown Large method: {method!r}") from error


def _validate_config(
    config: Mapping[str, Any], setting: Mapping[str, Any], method: str,
) -> tuple[int, int, Optional[int]]:
    if int(setting.get("experiment_schema", config.get("experiment_schema", -1))) != 2:
        raise ValueError("Large compiler requires experiment_schema=2")
    method_id = str(setting.get("method_id", ""))
    if method_id not in _METHOD_IDS[method]:
        raise ValueError(
            f"config method_id {method_id!r} is incompatible with {method}")
    forbidden = sorted(_FORBIDDEN_SCHEMA2_KEYS.intersection(setting))
    if forbidden:
        raise ValueError(f"Schema 2 Large config contains forbidden keys: {forbidden}")

    expected_horizon = {"M1": 0, "M2": 0, "M3": 0, "M4": 2}[method]
    horizon = int(setting.get("lookahead_horizon", expected_horizon))
    if horizon != expected_horizon:
        raise ValueError(
            f"{method} requires lookahead_horizon={expected_horizon}, got {horizon}")

    if method in {"M3", "M4"}:
        frozen = {
            "objective": "physical_log_fidelity",
            "population_size": 6,
            "iterations": 8,
            "neighbors_per_solution": 2,
            "neighbor_sample_size": 24,
        }
        differences = {
            key: (setting.get(key), expected)
            for key, expected in frozen.items()
            if setting.get(key) != expected
        }
        if differences:
            raise ValueError(f"{method} differs from frozen resident config: {differences}")
        expected_name = "ours_nl_v2.json" if method == "M3" else "ours_lk_v2.json"
        expected_path = Path(__file__).resolve().parents[1] / "exp_setting" / expected_name
        expected = _resolved_setting(_load_json(expected_path))
        allowed_differences = {"method_id", "lookahead_horizon", "dir", "seed"}
        actual_core = {
            key: value for key, value in setting.items()
            if key not in allowed_differences
        }
        expected_core = {
            key: value for key, value in expected.items()
            if key not in allowed_differences
        }
        if actual_core != expected_core:
            changed = sorted({
                key for key in set(actual_core) | set(expected_core)
                if actual_core.get(key) != expected_core.get(key)
            })
            raise ValueError(
                f"{method} semantic config is not frozen; changed keys: {changed}")

    seed = int(setting.get("seed", 0))
    runtime = config.get("large_runtime", {})
    if runtime is None:
        runtime = {}
    if not isinstance(runtime, Mapping):
        raise ValueError("large_runtime must be an object")
    raw_limit = runtime.get("max_layers_per_invocation")
    invocation_limit = None if raw_limit is None else int(raw_limit)
    if invocation_limit is not None and invocation_limit <= 0:
        raise ValueError("max_layers_per_invocation must be positive")
    return horizon, seed, invocation_limit


def _stream_hash(path: Path) -> tuple[int, str]:
    count = 0
    rolling = _ZERO_HASH
    try:
        with gzip.open(path, "rb") as handle:
            for line in handle:
                if not line.strip():
                    continue
                rolling = hashlib.sha256(bytes.fromhex(rolling) + line).hexdigest()
                count += 1
    except (OSError, EOFError) as error:
        raise ValueError(f"checkpoint trace prefix is not a complete gzip stream: {path}") from error
    return count, rolling


class _LargeCompiler:
    def __init__(
        self,
        *,
        layer_store_path: Path,
        architecture_path: Path,
        config_path: Path,
        method: str,
        output_dir: Path,
        resume_from: Path | None,
    ):
        self.layer_store_path = layer_store_path
        self.architecture_path = architecture_path
        self.config_path = config_path
        self.output_dir = output_dir
        self.resume_from = resume_from
        self.method = _normalise_method(method)

        self.config = _load_json(config_path)
        self.setting = _resolved_setting(self.config)
        self.horizon, self.seed, self.invocation_limit = _validate_config(
            self.config, self.setting, self.method)
        self.architecture_spec = _load_json(architecture_path)
        self.architecture = Architecture(self.architecture_spec)
        self.model = FidelityModel()
        self.physical_cost: PhysicalIncrementalCost | None = None
        self.rng = random.Random(self.seed)

        self.input_sha256 = ""
        self.raw_config_sha256 = _sha256_file(config_path)
        self.architecture_sha256 = _sha256_file(architecture_path)
        self.identity_sha256 = _stable_hash({
            "format": _FORMAT,
            "config_sha256": self.raw_config_sha256,
            "architecture_sha256": self.architecture_sha256,
            "method": self.method,
        })

        self.trace_path = output_dir / _TRACE_NAME
        self.checkpoint_path = output_dir / _CHECKPOINT_NAME
        self.result_path = output_dir / _RESULT_NAME
        self.stats_path = output_dir / _STATS_NAME
        self.progress_path = output_dir / _PROGRESS_NAME

        self.store: LayerStore | None = None
        self.metadata: dict[str, Any] = {}
        self.n_qubits = 0
        self.max_layer = -1
        self.max_two_qubit_layer = -1
        self.storage_sites: list[_Site] = []
        self.entanglement_pairs: list[_EntanglementPair] = []
        self.pair_by_site: dict[_Site, int] = {}
        self.all_waypoint_sites: list[_Site] = []
        self.homes: list[_Site] = []
        self.mapping: list[_Site] = []
        self.positions: list[tuple[float, float]] = []
        self.regions: list[str] = []
        self.residents: set[int] = set()
        self.site_owner: dict[_Site, int] = {}
        self.clock_us = 0.0
        self.next_layer = 0
        self.stats = LargeCompilerStats()
        self.writer: EventStreamWriter | None = None
        self.pipeline: IncrementalTracePipeline | None = None
        self.controller: CheckpointController | None = None

    def run(self) -> dict[str, Any]:
        end_to_end_begin = time.perf_counter_ns()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        with LayerStore(self.layer_store_path) as store:
            self.store = store
            self.metadata = store.metadata
            self.n_qubits = int(self.metadata["qubits"])
            self.max_layer = int(self.metadata.get("max_layer", -1))
            self.max_two_qubit_layer = int(
                self.metadata.get("max_two_qubit_layer", -1))
            statement_hash = str(self.metadata.get("statement_sha256", ""))
            self.input_sha256 = (
                statement_hash if len(statement_hash) == 64
                else _sha256_file(self.layer_store_path)
            )
            if getattr(store, "has_logical_ledger", False):
                store.verify_logical_ledger()
            self._build_architecture_catalogs()
            self.physical_cost = PhysicalIncrementalCost(self.n_qubits)

            prepare_done = time.perf_counter_ns()
            if self.resume_from is None:
                self._start_fresh()
            else:
                self._resume()
            compile_begin = time.perf_counter_ns()

            processed_this_invocation = 0
            assert self.store is not None
            for batch in self.store.iter_dependency_layers(start_layer=self.next_layer):
                layer = int(batch.metadata.layer)
                if layer != self.next_layer:
                    raise ValueError(
                        f"dependency layer stream skipped {self.next_layer}: found {layer}")
                self._compile_dependency_layer(tuple(batch.events))
                self.next_layer = layer + 1
                self.stats.dependency_layers += 1
                self.stats.dependency_events += int(batch.metadata.event_count)
                processed_this_invocation += 1

                self._maybe_checkpoint(force=False, reopen=True)
                if (
                    self.invocation_limit is not None
                    and processed_this_invocation >= self.invocation_limit
                    and self.next_layer <= self.max_layer
                ):
                    self._maybe_checkpoint(force=True, reopen=False)
                    compile_end = time.perf_counter_ns()
                    progress = self._progress_payload(
                        status="checkpointed",
                        compiler_core_seconds=(compile_end - compile_begin) / 1e9,
                        end_to_end_seconds=(compile_end - end_to_end_begin) / 1e9,
                    )
                    _atomic_json(self.progress_path, progress)
                    return progress

            # A final pre-finalisation checkpoint is deliberately retained.  It
            # lets a preempted runner restore the exact verifier/scorer state
            # even if writing the small result JSON is interrupted.
            self._maybe_checkpoint(force=True, reopen=False)
            assert self.pipeline is not None
            result = self.pipeline.finalize()
            compile_end = time.perf_counter_ns()
            self._verify_final_result(result)
            payload = self._success_payload(
                result,
                preparation_seconds=(prepare_done - end_to_end_begin) / 1e9,
                compiler_core_seconds=(compile_end - compile_begin) / 1e9,
                end_to_end_seconds=(time.perf_counter_ns() - end_to_end_begin) / 1e9,
            )
            _atomic_json(self.stats_path, payload["compiler_stats"])
            _atomic_json(self.result_path, payload)
            if self.progress_path.exists():
                self.progress_path.unlink()
            return payload

    def close(self) -> None:
        if self.writer is not None:
            self.writer.close()
            self.writer = None

    # ------------------------------------------------------------------ setup
    def _build_architecture_catalogs(self) -> None:
        for slm_id in sorted(self.architecture.storage_zone):
            slm = self.architecture.dict_SLM[slm_id]
            for row in range(slm.n_r):
                for column in range(slm.n_c):
                    self.storage_sites.append(_Site(slm_id, row, column))
        if self.n_qubits > len(self.storage_sites):
            raise ValueError(
                f"architecture has {len(self.storage_sites)} storage sites for "
                f"{self.n_qubits} atoms")

        for group in self.architecture.entanglement_zone:
            ids = sorted(int(value) for value in group)
            if len(ids) >= 2:
                for offset in range(0, len(ids) - 1, 2):
                    left, right = (
                        self.architecture.dict_SLM[ids[offset]],
                        self.architecture.dict_SLM[ids[offset + 1]],
                    )
                    for row in range(min(left.n_r, right.n_r)):
                        for column in range(min(left.n_c, right.n_c)):
                            self.entanglement_pairs.append(_EntanglementPair(
                                _Site(left.idx, row, column),
                                _Site(right.idx, row, column),
                            ))
            elif ids:
                slm = self.architecture.dict_SLM[ids[0]]
                for row in range(slm.n_r):
                    for column in range(0, slm.n_c - 1, 2):
                        self.entanglement_pairs.append(_EntanglementPair(
                            _Site(slm.idx, row, column),
                            _Site(slm.idx, row, column + 1),
                        ))
        if not self.entanglement_pairs:
            raise ValueError("architecture has no usable entanglement-site pairs")
        for index, pair in enumerate(self.entanglement_pairs):
            if pair.left in self.pair_by_site or pair.right in self.pair_by_site:
                raise ValueError("entanglement site occurs in more than one gate pair")
            self.pair_by_site[pair.left] = index
            self.pair_by_site[pair.right] = index

        ent_sites = sorted(self.pair_by_site)
        self.all_waypoint_sites = list(self.storage_sites) + ent_sites
        self.homes = self._interaction_aware_homes(ent_sites)

    def _interaction_aware_homes(self, ent_sites: Sequence[_Site]) -> list[_Site]:
        assert self.store is not None
        degrees = [0] * self.n_qubits
        for q0, q1, weight in self.store.interaction_matrix():
            degrees[q0] += weight
            degrees[q1] += weight
        ent_positions = [self._position(site) for site in ent_sites]
        center = (
            math.fsum(point[0] for point in ent_positions) / len(ent_positions),
            math.fsum(point[1] for point in ent_positions) / len(ent_positions),
        )
        nearby = sorted(
            self.storage_sites,
            key=lambda site: (math.dist(self._position(site), center), site),
        )[:self.n_qubits]
        qubits = sorted(range(self.n_qubits), key=lambda q: (-degrees[q], q))
        homes: list[_Site | None] = [None] * self.n_qubits
        for qubit, site in zip(qubits, nearby):
            homes[qubit] = site
        return [site for site in homes if site is not None]

    def _start_fresh(self) -> None:
        stale = [
            path for path in (
                self.trace_path, self.checkpoint_path, self.result_path,
                self.stats_path, self.progress_path,
            ) if path.exists()
        ]
        if stale:
            raise FileExistsError(
                "fresh Large output directory contains prior artifacts: "
                + ", ".join(str(path) for path in stale))
        self.mapping = list(self.homes)
        self.positions = [self._position(site) for site in self.mapping]
        self.regions = ["storage"] * self.n_qubits
        self.site_owner = {site: q for q, site in enumerate(self.mapping)}
        self.residents = set()
        self.writer = EventStreamWriter(self.trace_path)
        self.pipeline = IncrementalTracePipeline(
            IncrementalTraceValidator(
                self.n_qubits,
                expected_one_qubit_gates=int(self.metadata.get("gates_1q", 0)),
                expected_two_qubit_gates=int(self.metadata.get("gates_2q", 0)),
                expected_logical_ledger_sha256=str(
                    self.metadata.get("logical_ledger_sha256", "")) or None,
            ),
            IncrementalTraceScorer(self.n_qubits, self.model),
            self.writer,
        )
        self.pipeline.consume(CanonicalTraceEvent(
            EventType.INIT,
            0.0,
            0.0,
            atoms=tuple(range(self.n_qubits)),
            end_positions=tuple(self.positions),
            end_regions=tuple(self.regions),
            metadata={
                "compiler": _FORMAT,
                "method": self.method,
                "architecture_sha256": self.architecture_sha256,
            },
        ))
        self.next_layer = 0
        self._create_controller()

    def _resume(self) -> None:
        checkpoint_path = self.resume_from or self.checkpoint_path
        checkpoint = load_checkpoint(
            checkpoint_path,
            input_sha256=self.input_sha256,
            config_sha256=self.identity_sha256,
        )
        dependencies = dict(checkpoint.dependencies)
        if dependencies.get("format") != "zac-large-compiler-state-v1":
            raise ValueError("unsupported Large compiler checkpoint state")
        if str(dependencies.get("method")) != self.method:
            raise ValueError("checkpoint method mismatch")
        stream_size = int(dependencies["stream_size_bytes"])
        if not self.trace_path.exists():
            raise FileNotFoundError(f"checkpoint trace is missing: {self.trace_path}")
        actual_size = self.trace_path.stat().st_size
        if actual_size < stream_size:
            raise ValueError("checkpoint trace is shorter than its committed prefix")
        if actual_size > stream_size:
            with open(self.trace_path, "r+b") as handle:
                handle.truncate(stream_size)
                handle.flush()
                os.fsync(handle.fileno())
        count, digest = _stream_hash(self.trace_path)
        if count != checkpoint.event_count or digest != checkpoint.event_hash:
            raise ValueError("checkpoint event count/hash does not match trace prefix")

        self.mapping = [
            _Site.from_value(checkpoint.mapping[str(q)])
            for q in range(self.n_qubits)
        ]
        self.positions = [self._position(site) for site in self.mapping]
        raw_regions = dependencies.get("regions")
        if not isinstance(raw_regions, list) or len(raw_regions) != self.n_qubits:
            raise ValueError("checkpoint region mapping is incomplete")
        self.regions = [str(value) for value in raw_regions]
        self.residents = {int(q) for q in checkpoint.residents.get("atoms", [])}
        expected_residents = {
            q for q, region in enumerate(self.regions) if region == "entanglement"
        }
        if self.residents != expected_residents:
            raise ValueError("checkpoint resident registry disagrees with mapping")
        if len(set(self.mapping)) != self.n_qubits:
            raise ValueError("checkpoint maps two atoms to one SLM site")
        self.site_owner = {site: q for q, site in enumerate(self.mapping)}
        self.clock_us = float(dependencies["clock_us"])
        self.next_layer = int(checkpoint.current_layer)
        if self.next_layer != int(dependencies.get("next_dependency_layer", -1)):
            raise ValueError("checkpoint dependency layer index mismatch")
        self.stats = LargeCompilerStats.from_mapping(dependencies["compiler_stats"])
        checkpoint.restore_rng(self.rng)

        self.writer = EventStreamWriter(
            self.trace_path,
            append=True,
            event_count=checkpoint.event_count,
            event_hash=checkpoint.event_hash,
        )
        self.pipeline = IncrementalTracePipeline.from_state(
            dependencies["pipeline"], self.writer)
        self._create_controller()
        assert self.controller is not None
        self.controller.resume_from(checkpoint)
        self._assert_pipeline_mapping()

    def _create_controller(self) -> None:
        self.controller = CheckpointController(
            self.checkpoint_path,
            input_sha256=self.input_sha256,
            config_sha256=self.identity_sha256,
            start_layer=self.next_layer,
        )

    # --------------------------------------------------------------- scheduling
    def _compile_dependency_layer(self, events: tuple[GateEvent, ...]) -> None:
        if not events:
            return
        self.stats.window_peak_events = max(self.stats.window_peak_events, len(events))
        one_qubit = sorted(
            (event for event in events if len(event.qubits) == 1),
            key=lambda event: event.seq,
        )
        two_qubit = sorted(
            (event for event in events if len(event.qubits) == 2),
            key=lambda event: (int(event.two_qubit_layer or 0), event.seq),
        )
        for event in one_qubit:
            self._emit_one_qubit(event)
        groups: dict[int, list[GateEvent]] = {}
        for event in two_qubit:
            if event.operation != "cz" or event.two_qubit_layer is None:
                raise ValueError(f"Large store contains invalid 2Q event: {event}")
            groups.setdefault(int(event.two_qubit_layer), []).append(event)
        for two_qubit_layer in sorted(groups):
            self._compile_cz_group(two_qubit_layer, groups[two_qubit_layer])

    def _emit_one_qubit(self, event: GateEvent) -> None:
        q = event.qubits[0]
        start = self.clock_us
        self.clock_us += self.model.one_qubit_duration_us
        assert self.pipeline is not None
        self.pipeline.consume(CanonicalTraceEvent(
            EventType.ONE_QUBIT_GATE,
            start,
            self.clock_us,
            atoms=(q,),
            start_positions=(self.positions[q],),
            end_positions=(self.positions[q],),
            start_regions=(self.regions[q],),
            end_regions=(self.regions[q],),
            gate_names=(event.operation,),
            source_index=event.seq,
        ))
        self.stats.one_qubit_gates += 1

    def _compile_cz_group(self, boundary: int, gates: list[GateEvent]) -> None:
        assert self.store is not None
        visible = tuple(self.store.two_qubit_window(boundary, self.horizon))
        self.stats.window_peak_events = max(
            self.stats.window_peak_events, len(gates) + len(visible))
        participants = {q for event in gates for q in event.qubits}
        self._prune_idle_residents(boundary, participants, visible)

        pending = list(sorted(gates, key=lambda event: event.seq))
        while pending:
            chunk: list[tuple[GateEvent, int]] = []
            used_pairs: set[int] = set()
            remaining: list[GateEvent] = []
            for event in pending:
                assignment = self._assign_gate_pair(event, used_pairs, participants)
                if assignment is None:
                    remaining.append(event)
                    continue
                pair_index = assignment
                self._place_gate(event, pair_index)
                used_pairs.add(pair_index)
                chunk.append((event, pair_index))
            if not chunk:
                protected = {q for event in pending for q in event.qubits}
                if not self._evict_resident(protected):
                    # Two participants resident in incompatible pairs are
                    # resolved explicitly before declaring architectural failure.
                    first = pending[0]
                    ent = [q for q in first.qubits if q in self.residents]
                    if len(ent) >= 2:
                        self._return_home(max(ent), eviction=True)
                        continue
                    raise RuntimeError(
                        "no free entanglement pair and no evictable resident")
                continue

            self._emit_cz_chunk(chunk)
            for event, _ in chunk:
                for q in event.qubits:
                    if self._should_stay_after_gate(q, boundary, visible):
                        self.stats.stay_decisions += 1
                    else:
                        self.stats.return_decisions += 1
                        self._return_home(q)
            pending = remaining
        self.stats.resident_peak = max(self.stats.resident_peak, len(self.residents))

    def _assign_gate_pair(
        self, event: GateEvent, used_pairs: set[int], protected: set[int],
    ) -> int | None:
        q0, q1 = event.qubits
        pair0 = self.pair_by_site.get(self.mapping[q0]) if q0 in self.residents else None
        pair1 = self.pair_by_site.get(self.mapping[q1]) if q1 in self.residents else None
        if pair0 is not None and pair0 == pair1 and pair0 not in used_pairs:
            return pair0
        if pair0 is not None and pair1 is not None and pair0 != pair1:
            self._return_home(max(q0, q1), eviction=True)
            return self._assign_gate_pair(event, used_pairs, protected)
        resident_q = q0 if pair0 is not None else (q1 if pair1 is not None else None)
        resident_pair = pair0 if pair0 is not None else pair1
        if resident_q is not None and resident_pair is not None and resident_pair not in used_pairs:
            pair = self.entanglement_pairs[resident_pair]
            other_site = pair.right if self.mapping[resident_q] == pair.left else pair.left
            owner = self.site_owner.get(other_site)
            if owner is not None and owner not in event.qubits:
                self._return_home(owner, eviction=True)
                owner = self.site_owner.get(other_site)
            if owner is None or owner in event.qubits:
                return resident_pair
        for index, pair in enumerate(self.entanglement_pairs):
            if index in used_pairs:
                continue
            if pair.left not in self.site_owner and pair.right not in self.site_owner:
                return index
        return None

    def _place_gate(self, event: GateEvent, pair_index: int) -> None:
        pair = self.entanglement_pairs[pair_index]
        q0, q1 = event.qubits
        if self.mapping[q0] == pair.left:
            targets = ((q0, pair.left), (q1, pair.right))
        elif self.mapping[q0] == pair.right:
            targets = ((q0, pair.right), (q1, pair.left))
        elif self.mapping[q1] == pair.left:
            targets = ((q1, pair.left), (q0, pair.right))
        elif self.mapping[q1] == pair.right:
            targets = ((q1, pair.right), (q0, pair.left))
        else:
            targets = ((q0, pair.left), (q1, pair.right))
        for q, target in targets:
            if self.mapping[q] != target:
                self._move_atom(q, target)

    def _emit_cz_chunk(self, chunk: list[tuple[GateEvent, int]]) -> None:
        events = [event for event, _ in chunk]
        pairs = tuple(tuple(event.qubits) for event in events)
        atoms = tuple(q for pair in pairs for q in pair)
        start = self.clock_us
        self.clock_us += self.model.rydberg_duration_us
        region_atoms = tuple(sorted(self.residents))
        assert set(atoms).issubset(region_atoms)
        assert self.pipeline is not None
        self.pipeline.consume(CanonicalTraceEvent(
            EventType.TWO_QUBIT_GATE,
            start,
            self.clock_us,
            atoms=atoms,
            region="entanglement",
            gate_pairs=pairs,
            region_atoms=region_atoms,
            gate_names=("cz",) * len(pairs),
            source_index=min(event.seq for event in events),
            metadata={
                "source_indices": [event.seq for event in events],
                "two_qubit_layers": [event.two_qubit_layer for event in events],
                "ghost_hits": 0,
                "capacity": len(self.entanglement_pairs),
            },
        ))
        self.stats.two_qubit_gates += len(pairs)
        self.stats.rydberg_pulses += 1

    # --------------------------------------------------------- residency policy
    def _prune_idle_residents(
        self,
        boundary: int,
        participants: set[int],
        visible: Sequence[GateEvent],
    ) -> None:
        for q in sorted(self.residents - participants):
            if self._should_idle_resident_stay(q, boundary, visible):
                self.stats.stay_decisions += 1
            else:
                self.stats.return_decisions += 1
                self._return_home(q)

    def _should_stay_after_gate(
        self, q: int, boundary: int, visible: Sequence[GateEvent],
    ) -> bool:
        if self.method == "M1":
            return False
        next_layer = self._next_visible_layer(q, boundary, visible, include_current=False)
        if self.method == "M2":
            return next_layer == boundary + 1
        if boundary >= self.max_two_qubit_layer:
            # Frozen M3/M4 policy does not manufacture a final RETURN solely to
            # make the output end in storage.
            return True
        return self._physical_stay_decision(q, boundary, next_layer, visible)

    def _should_idle_resident_stay(
        self, q: int, boundary: int, visible: Sequence[GateEvent],
    ) -> bool:
        if self.method == "M1":
            return False
        next_layer = self._next_visible_layer(q, boundary, visible, include_current=True)
        if self.method == "M2":
            # A general dependency layer may split otherwise independent CZs
            # carrying the same independent-CZ-layer number.  Retain the atom
            # until its registered adjacent-layer gate is actually consumed.
            return next_layer == boundary
        return self._physical_stay_decision(q, boundary, next_layer, visible)

    @staticmethod
    def _next_visible_layer(
        q: int,
        boundary: int,
        visible: Sequence[GateEvent],
        *,
        include_current: bool,
    ) -> int | None:
        lower = boundary if include_current else boundary + 1
        layers = [
            int(event.two_qubit_layer)
            for event in visible
            if event.two_qubit_layer is not None
            and int(event.two_qubit_layer) >= lower
            and q in event.qubits
        ]
        return min(layers) if layers else None

    def _physical_stay_decision(
        self,
        q: int,
        boundary: int,
        next_layer: int | None,
        visible: Sequence[GateEvent],
    ) -> bool:
        self.stats.physical_decision_evaluations += 1
        # No visible reuse means the bounded oracle has no evidence with which
        # to justify occupying scarce entanglement capacity.
        if next_layer is None:
            return False
        idle_pulses = 0
        for layer in range(boundary + 1, next_layer):
            gate_count = sum(
                1 for event in visible if event.two_qubit_layer == layer)
            idle_pulses += math.ceil(gate_count / len(self.entanglement_pairs))
        current = self.positions[q]
        home = self._position(self.homes[q])
        distance = math.dist(current, home)
        leg = (distance, current[0], current[1], home[0], home[1])
        assert self.physical_cost is not None
        phase = self.physical_cost.movement_phase((leg,))
        _, return_cost = self.physical_cost.score((phase, phase), 0, chromosome=(1,))
        _, stay_cost = self.physical_cost.score((), idle_pulses, chromosome=(0,))
        return stay_cost.objective((0,)) < return_cost.objective((1,))

    def _evict_resident(self, protected: set[int]) -> bool:
        candidates = sorted(self.residents - protected, reverse=True)
        if not candidates:
            return False
        self._return_home(candidates[0], eviction=True)
        return True

    # --------------------------------------------------------------- movement
    def _return_home(self, q: int, *, eviction: bool = False) -> None:
        if self.mapping[q] != self.homes[q]:
            self._move_atom(q, self.homes[q])
        if eviction:
            self.stats.resident_evictions += 1

    def _move_atom(self, q: int, target: _Site) -> None:
        if self.mapping[q] == target:
            return
        owner = self.site_owner.get(target)
        if owner is not None and owner != q:
            raise RuntimeError(f"target SLM site {target} is occupied by atom {owner}")
        start_site = self.mapping[q]
        start_position = self.positions[q]
        target_position = self._position(target)
        start_region = self.regions[q]
        target_region = self._region(target)
        path = self._ghost_safe_path(q, start_position, target_position)

        batch_id = f"large-{self.stats.move_batches_generated:012d}-q{q}"
        assert self.pipeline is not None
        begin = self.clock_us
        self.clock_us += self.model.transfer_duration_us
        self.pipeline.consume(CanonicalTraceEvent(
            EventType.LOAD,
            begin,
            self.clock_us,
            atoms=(q,),
            batch_id=batch_id,
            start_positions=(start_position,),
            end_positions=(start_position,),
            start_regions=(start_region,),
            end_regions=("aod",),
        ))

        current = start_position
        for waypoint_index, endpoint in enumerate(path):
            distance = math.dist(current, endpoint)
            if distance <= 1e-12:
                continue
            begin = self.clock_us
            self.clock_us += math.sqrt(distance / PhysicalIncrementalCost.ACCEL_UM_PER_US2)
            self.pipeline.consume(CanonicalTraceEvent(
                EventType.MOVE,
                begin,
                self.clock_us,
                atoms=(q,),
                batch_id=batch_id,
                start_positions=(current,),
                end_positions=(endpoint,),
                start_regions=("aod",),
                end_regions=("aod",),
                metadata={
                    "ghost_hits": 0,
                    "leg_index": waypoint_index,
                    "waypoint": endpoint != target_position,
                },
            ))
            self.stats.movement_legs += 1
            self.stats.movement_distance_um += distance
            current = endpoint

        begin = self.clock_us
        self.clock_us += self.model.transfer_duration_us
        self.pipeline.consume(CanonicalTraceEvent(
            EventType.STORE,
            begin,
            self.clock_us,
            atoms=(q,),
            batch_id=batch_id,
            start_positions=(target_position,),
            end_positions=(target_position,),
            start_regions=("aod",),
            end_regions=(target_region,),
        ))
        del self.site_owner[start_site]
        self.site_owner[target] = q
        self.mapping[q] = target
        self.positions[q] = target_position
        self.regions[q] = target_region
        if target_region == "entanglement":
            self.residents.add(q)
        else:
            self.residents.discard(q)
        self.stats.move_batches_generated += 1
        self.stats.resident_peak = max(self.stats.resident_peak, len(self.residents))

    def _ghost_safe_path(
        self,
        q: int,
        start: tuple[float, float],
        target: tuple[float, float],
    ) -> list[tuple[float, float]]:
        ghosts = [
            (other, *position)
            for other, position in enumerate(self.positions)
            if other != q
        ]
        if self._safe_segment(start, target, ghosts):
            self.stats.direct_paths += 1
            return [target]

        occupied = set(self.mapping)
        candidates = [
            site for site in self.all_waypoint_sites
            if site not in occupied
            and self._position(site) not in {start, target}
        ]
        candidates.sort(key=lambda site: (
            math.dist(start, self._position(site))
            + math.dist(self._position(site), target),
            site,
        ))
        safe_from: list[tuple[float, float]] = []
        safe_to: list[tuple[float, float]] = []
        for site in candidates:
            waypoint = self._position(site)
            from_safe = self._safe_segment(start, waypoint, ghosts)
            to_safe = self._safe_segment(waypoint, target, ghosts)
            if from_safe:
                safe_from.append(waypoint)
            if to_safe:
                safe_to.append(waypoint)
            if from_safe and to_safe:
                self.stats.one_waypoint_paths += 1
                return [waypoint, target]

        # Two real-SLM waypoints are an intentionally rare fallback.  Bound
        # the Cartesian search while considering candidates in globally best
        # distance order; a dense architecture supplies many safe choices.
        for first in safe_from[:128]:
            for second in safe_to[:128]:
                if first == second:
                    continue
                if self._safe_segment(first, second, ghosts):
                    self.stats.two_waypoint_paths += 1
                    return [first, second, target]
        raise RuntimeError(
            f"no ghost-safe direct or legal-SLM-waypoint route for atom {q}: "
            f"{start} -> {target}")

    @staticmethod
    def _safe_segment(
        start: tuple[float, float],
        target: tuple[float, float],
        ghosts: Sequence[tuple[int, float, float]],
    ) -> bool:
        distance = math.dist(start, target)
        if distance <= 1e-12:
            return True
        return not ghost_hits(
            [(distance, start[0], start[1], target[0], target[1])], ghosts)

    # ------------------------------------------------------------- checkpoint
    def _maybe_checkpoint(self, *, force: bool, reopen: bool) -> bool:
        assert self.controller is not None

        reasons = self.controller.due_reasons(self.next_layer)
        if force and "forced" not in reasons:
            reasons = (*reasons, "forced")
        if not reasons:
            return False
        # The counters are part of the checkpoint itself, so a resumed run does
        # not forget checkpoints committed by an earlier invocation.
        self.stats.checkpoints += 1
        for reason in reasons:
            self.stats.checkpoint_reasons[reason] = (
                self.stats.checkpoint_reasons.get(reason, 0) + 1)

        def factory() -> Checkpoint:
            assert self.writer is not None and self.pipeline is not None
            writer = self.writer
            writer.close()
            self.pipeline.writer = None
            self.writer = None
            stream_size = self.trace_path.stat().st_size
            checkpoint = Checkpoint(
                current_layer=self.next_layer,
                mapping={str(q): site.to_list() for q, site in enumerate(self.mapping)},
                residents={
                    "atoms": sorted(self.residents),
                    "homes": {str(q): site.to_list() for q, site in enumerate(self.homes)},
                },
                dependencies={
                    "format": "zac-large-compiler-state-v1",
                    "method": self.method,
                    "lookahead_horizon": self.horizon,
                    "next_dependency_layer": self.next_layer,
                    "clock_us": self.clock_us,
                    "regions": list(self.regions),
                    "pipeline": self.pipeline.state_dict(),
                    "compiler_stats": asdict(self.stats),
                    "stream_size_bytes": stream_size,
                    "logical_ledger_sha256": self.metadata.get("logical_ledger_sha256"),
                },
                rolling_metrics=self.pipeline.scorer.rolling_metrics(),
                event_count=writer.event_count,
                event_hash=writer.event_hash,
                input_sha256=self.input_sha256,
                config_sha256=self.identity_sha256,
            )
            checkpoint.capture_rng(self.rng)
            return checkpoint

        decision = self.controller.maybe_checkpoint(
            self.next_layer, factory, force=force)
        if not decision.saved:
            raise RuntimeError("checkpoint was due but controller did not save it")
        if reopen:
            checkpoint = load_checkpoint(self.checkpoint_path)
            self.writer = EventStreamWriter(
                self.trace_path,
                append=True,
                event_count=checkpoint.event_count,
                event_hash=checkpoint.event_hash,
            )
            assert self.pipeline is not None
            self.pipeline.writer = self.writer
        return True

    # --------------------------------------------------------------- auditing
    def _verify_final_result(self, result: Mapping[str, Any]) -> None:
        validation = result["validation"]
        fidelity = result["fidelity"]
        expected_1q = int(self.metadata.get("gates_1q", 0))
        expected_2q = int(self.metadata.get("gates_2q", 0))
        if not validation.get("ok") or int(validation.get("ghost_hits", -1)) != 0:
            raise RuntimeError("strict final Large validation did not prove zero ghost hits")
        if int(validation.get("one_qubit_gates", -1)) != expected_1q:
            raise RuntimeError("final Large 1Q ledger mismatch")
        if int(validation.get("two_qubit_gates", -1)) != expected_2q:
            raise RuntimeError("final Large 2Q ledger mismatch")
        expected_ledger = self.metadata.get("logical_ledger_sha256")
        if (expected_ledger is not None and
                validation.get("logical_ledger_sha256") != expected_ledger):
            raise RuntimeError("final Large per-atom logical ledger mismatch")
        if int(fidelity.get("move_batches", -1)) != self.stats.move_batches_generated:
            raise RuntimeError("generated/replayed Move batch counts disagree")
        count, digest = _stream_hash(self.trace_path)
        if int(validation.get("event_count", -1)) != count:
            raise RuntimeError("trace stream/validator event counts disagree")
        assert self.pipeline is not None
        # The writer is closed at the final checkpoint, so the checkpoint hash
        # is the authoritative event-chain hash for the completed stream.
        checkpoint = load_checkpoint(self.checkpoint_path)
        if count != checkpoint.event_count or digest != checkpoint.event_hash:
            raise RuntimeError("completed trace differs from final checkpoint hash")

    def _assert_pipeline_mapping(self) -> None:
        assert self.pipeline is not None
        state = self.pipeline.validator.state_dict()
        validator_positions = state["positions"]
        validator_regions = state["regions"]
        for q in range(self.n_qubits):
            expected_position = list(self.positions[q])
            if validator_positions[q] != expected_position:
                raise ValueError(f"checkpoint validator position mismatch for atom {q}")
            if validator_regions[q] != self.regions[q]:
                raise ValueError(f"checkpoint validator region mismatch for atom {q}")

    def _progress_payload(
        self, *, status: str, compiler_core_seconds: float,
        end_to_end_seconds: float,
    ) -> dict[str, Any]:
        checkpoint = load_checkpoint(self.checkpoint_path)
        return {
            "format": _FORMAT,
            "status": status,
            "implementation_status": _IMPLEMENTATION_STATUS,
            "support_claim_eligible": False,
            "method": self.method,
            "lookahead_horizon": self.horizon,
            "next_dependency_layer": self.next_layer,
            "total_dependency_layers": self.max_layer + 1,
            "trace_path": str(self.trace_path),
            "checkpoint_path": str(self.checkpoint_path),
            "event_count": checkpoint.event_count,
            "event_hash": checkpoint.event_hash,
            "compiler_stats": asdict(self.stats),
            "timing": {
                "compiler_core_seconds": compiler_core_seconds,
                "end_to_end_seconds": end_to_end_seconds,
            },
            "result": None,
        }

    def _success_payload(
        self,
        result: Mapping[str, Any],
        *,
        preparation_seconds: float,
        compiler_core_seconds: float,
        end_to_end_seconds: float,
    ) -> dict[str, Any]:
        checkpoint = load_checkpoint(self.checkpoint_path)
        return {
            "format": _FORMAT,
            "status": "success",
            "implementation_status": _IMPLEMENTATION_STATUS,
            "support_claim_eligible": False,
            "method": self.method,
            "lookahead_horizon": self.horizon,
            "input_sha256": self.input_sha256,
            "config_sha256": self.raw_config_sha256,
            "architecture_sha256": self.architecture_sha256,
            "compilation_identity_sha256": self.identity_sha256,
            "logical_ledger_sha256": self.metadata.get("logical_ledger_sha256"),
            "trace_path": str(self.trace_path),
            "checkpoint_path": str(self.checkpoint_path),
            "event_count": checkpoint.event_count,
            "event_hash": checkpoint.event_hash,
            "compiler_stats": asdict(self.stats),
            "timing": {
                "preparation_seconds": preparation_seconds,
                "compiler_core_seconds": compiler_core_seconds,
                "end_to_end_seconds": end_to_end_seconds,
            },
            "result": dict(result),
        }

    # --------------------------------------------------------------- geometry
    def _position(self, site: _Site) -> tuple[float, float]:
        if not self.architecture.is_valid_SLM(site.slm):
            raise ValueError(f"unknown SLM in mapping: {site.slm}")
        if not self.architecture.is_valid_SLM_position(
                site.slm, site.row, site.column):
            raise ValueError(f"invalid SLM site in mapping: {site}")
        value = self.architecture.exact_SLM_location(
            site.slm, site.row, site.column)
        return float(value[0]), float(value[1])

    def _region(self, site: _Site) -> str:
        if site.slm in self.architecture.storage_zone:
            return "storage"
        if site in self.pair_by_site:
            return "entanglement"
        raise ValueError(f"SLM site is outside registered physical regions: {site}")


def compile_large_streaming(
    layer_store_path: str | Path,
    architecture_path: str | Path,
    config_path: str | Path,
    method: str,
    output_dir: str | Path,
    resume_from: str | Path | None = None,
) -> dict[str, Any]:
    """Compile one canonical layer store into a verified compressed trace.

    ``resume_from`` must point to a checkpoint created in the same
    ``output_dir`` with the same canonical input, method, architecture and
    config.  If the stream contains bytes past that checkpoint (for example a
    process was killed after a later event), the uncommitted suffix is removed
    before the event hash is verified and generation resumes.
    """

    compiler = _LargeCompiler(
        layer_store_path=Path(layer_store_path).resolve(),
        architecture_path=Path(architecture_path).resolve(),
        config_path=Path(config_path).resolve(),
        method=method,
        output_dir=Path(output_dir).resolve(),
        resume_from=(None if resume_from is None else Path(resume_from).resolve()),
    )
    try:
        return compiler.run()
    finally:
        compiler.close()


__all__ = ["LargeCompilerStats", "compile_large_streaming"]
