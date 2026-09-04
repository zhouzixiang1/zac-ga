"""Formal end-to-end bounded-memory compiler for Large ZAC methods.

This module is the executable composition layer for M1, M3, and M4.  It does
not introduce a proxy placement or routing rule: the exact stock/Schema-2
placement streams feed the production ZAC router, whose native instructions
are normalized, strictly replayed, scored, and archived as they are emitted.

Canonical events are written in native dependency order.  That order is
explicit in the manifest and is accepted only by the dependency-aware strict
pipeline, which additionally proves per-atom, global-1Q, and single-AOD time
monotonicity.  The physical result is numerically identical to replaying the
same completed native trace after chronological sorting, without retaining a
trace-sized Python list.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import gzip
import hashlib
import json
import os
from pathlib import Path
import resource
import shutil
import subprocess
import time
from typing import Any, Iterable, Mapping

from evaluation import FidelityModel
from zac.ds.architecture import Architecture

from .formal_zac_placement import FormalZacPlacementStream
from .formal_zac_checkpoint import (
    CheckpointCadence,
    FormalZacCheckpointIdentity,
    FormalZacCheckpointManager,
    RollingInstructionHash,
)
from .qasm_sqlite import LayerStore
from .trace_pipeline import (
    IncrementalTracePipeline,
    IncrementalTraceScorer,
    IncrementalTraceValidator,
)
from .zac_initial_placement import place_stock_sa_from_layer_store
from .zac_route_transition import ZACRouteTransitionDriver
from .zair_instruction_stream import IncrementalZairNormalizer


_ZERO_HASH = "0" * 64
_DECISION_TIMING_KEYS = (
    "horizon_selection_ns",
    "problem_preparation_ns",
    "search_kernel_ns",
    "result_commit_ns",
)
_BACKEND_TIMING_KEYS = (
    "python_marshal_ns",
    "native_call_wall_ns",
    "native_search_wall_ns",
    "fitness_ns",
    "normalize_ns",
    "decode_ns",
    "return_match_ns",
    "forecast_ns",
    "selection_ns",
    "native_parse_ns",
    "native_serialize_ns",
)


def _empty_timing_breakdown(initial_placement_ns: int = 0) -> dict[str, int]:
    return {
        "initial_placement_ns": int(initial_placement_ns),
        "placement_transition_ns": 0,
        "routing_ns": 0,
        **{key: 0 for key in _DECISION_TIMING_KEYS},
        **{key: 0 for key in _BACKEND_TIMING_KEYS},
    }


def _accumulate_timing(
    destination: dict[str, int], source: Mapping[str, Any] | None,
    keys: Iterable[str],
) -> None:
    if source is None:
        return
    for key in keys:
        raw = source.get(key, 0)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError(f"invalid formal Large timing {key}: {raw!r}")
        value = int(raw)
        if value < 0:
            raise ValueError(f"negative formal Large timing {key}: {value}")
        destination[key] += value


def _sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_hash(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _load_json(path: Path) -> Mapping[str, Any]:
    with open(path, encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, Mapping):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def _resolved_setting(
    method: str, setting: Mapping[str, Any] | str | Path | None,
) -> Mapping[str, Any] | None:
    method = str(method).upper()
    if method == "M1":
        if setting is not None:
            raise ValueError("formal M1 does not accept a resident setting")
        return None
    if method not in {"M3", "M4"}:
        raise ValueError("formal ZAC method must be M1, M3, or M4")
    if setting is None:
        raise ValueError(f"formal {method} requires a Schema-2 setting")
    value: Mapping[str, Any]
    if isinstance(setting, (str, Path)):
        value = _load_json(Path(setting).resolve())
    elif isinstance(setting, Mapping):
        value = setting
    else:
        raise TypeError("setting must be a mapping or JSON path")
    if "zac_setting" in value:
        rows = value["zac_setting"]
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(
            rows[0], Mapping
        ):
            raise ValueError("setting file must contain one zac_setting object")
        value = rows[0]
    return dict(value)


def _stock_stage_capacity(architecture: Architecture) -> int:
    """Return the exact capacity used by stock ``Scheduler_mixin``."""

    capacity = 0
    for zone in architecture.entanglement_zone:
        if not zone:
            raise ValueError("architecture contains an empty entanglement zone")
        slm = architecture.dict_SLM[zone[0]]
        capacity += int(slm.n_r) * int(slm.n_c)
    if capacity <= 0:
        raise ValueError("architecture has no physical Rydberg stage capacity")
    return capacity


def _peak_rss_bytes() -> int:
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    # Darwin reports bytes; Linux reports KiB.  This project runs on both in CI.
    return value if os.uname().sysname == "Darwin" else value * 1024


def _git_state() -> tuple[str, bool]:
    root = Path(__file__).resolve().parents[2]
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    commit = revision.stdout.strip()
    if len(commit) != 40:
        raise RuntimeError(f"formal Large run requires a full Git commit: {commit!r}")
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return commit, bool(status.stdout.strip())


class _GzipJsonlWriter:
    """Deterministic JSONL gzip member plus a content-chain hash."""

    def __init__(
        self,
        path: Path,
        *,
        append: bool = False,
        count: int = 0,
        chain_sha256: str = _ZERO_HASH,
    ):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if int(count) < 0 or len(str(chain_sha256)) != 64:
            raise ValueError("invalid JSONL writer resume state")
        try:
            bytes.fromhex(str(chain_sha256))
        except ValueError as error:
            raise ValueError("invalid JSONL writer chain hash") from error
        self.count = int(count)
        self.chain_sha256 = str(chain_sha256)
        self._raw = open(path, "ab" if append else "wb")
        self._gzip = gzip.GzipFile(fileobj=self._raw, mode="wb", mtime=0)

    def write(self, value: Mapping[str, Any]) -> None:
        line = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        ).encode("ascii") + b"\n"
        self.chain_sha256 = hashlib.sha256(
            bytes.fromhex(self.chain_sha256) + line
        ).hexdigest()
        self.count += 1
        self._gzip.write(line)

    def flush(self) -> None:
        self._gzip.flush()
        self._raw.flush()
        os.fsync(self._raw.fileno())

    def close(self) -> None:
        if self._gzip is not None:
            try:
                self._gzip.close()
            finally:
                self._gzip = None
                self._raw.close()

    def state_dict(self) -> dict[str, Any]:
        if self._gzip is not None:
            raise RuntimeError("close JSONL writer before capturing a checkpoint")
        return {
            "format": "formal-large-jsonl-writer-v1",
            "count": self.count,
            "chain_sha256": self.chain_sha256,
            "file_size": self.path.stat().st_size,
        }

    @classmethod
    def resume(cls, path: Path, state: Mapping[str, Any]) -> "_GzipJsonlWriter":
        if state.get("format") != "formal-large-jsonl-writer-v1":
            raise ValueError("unsupported formal Large JSONL writer state")
        expected_size = int(state["file_size"])
        if expected_size < 0 or not path.exists():
            raise ValueError("formal Large checkpoint output file is missing")
        actual_size = path.stat().st_size
        if actual_size < expected_size:
            raise ValueError(
                f"formal Large output is shorter than checkpoint: "
                f"{actual_size} < {expected_size}"
            )
        if actual_size != expected_size:
            # Discard only the uncheckpointed suffix inside this attempt-owned
            # in-progress directory.  The saved boundary ends at a complete
            # gzip member, so the next member remains independently readable.
            with open(path, "r+b") as handle:
                handle.truncate(expected_size)
                handle.flush()
                os.fsync(handle.fileno())
        return cls(
            path,
            append=True,
            count=int(state["count"]),
            chain_sha256=str(state["chain_sha256"]),
        )

    def __enter__(self) -> "_GzipJsonlWriter":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def __del__(self) -> None:
        # Checkpoint rotation deliberately replaces writer generations.  Keep a
        # final ownership guard so an exception between those assignments never
        # leaves the underlying file descriptor open.
        try:
            self.close()
        except Exception:
            pass


@dataclass(frozen=True)
class FormalLargeZacResult:
    method: str
    status: str
    stages_completed: int
    stages_total: int
    full_circuit: bool
    support_claim_eligible: bool
    compiler_core_ns: int
    timing_breakdown_ns: Mapping[str, int]
    end_to_end_ns: int
    peak_rss_bytes: int
    native_instructions: int
    canonical_events: int
    native_chain_sha256: str
    canonical_chain_sha256: str
    fidelity: Mapping[str, Any]
    validation: Mapping[str, Any]
    output_directory: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True, ensure_ascii=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def compile_formal_large_zac(
    *,
    method: str,
    layer_store_path: str | Path,
    architecture_path: str | Path,
    output_directory: str | Path,
    setting: Mapping[str, Any] | str | Path | None = None,
    model: FidelityModel | None = None,
    max_stages: int | None = None,
    progress_every: int = 1000,
    resume: bool = False,
    checkpoint_every_stages: int = 1000,
    checkpoint_every_seconds: float = 300.0,
    interrupt_after_stages: int | None = None,
) -> FormalLargeZacResult:
    """Compile one formal M1/M3/M4 Large attempt and publish it atomically.

    ``max_stages`` is a development-only prefix bound.  Prefix results are
    strictly verified but always carry ``support_claim_eligible=false``.
    Formal support eligibility requires every physical stage to be compiled.
    """

    method = str(method).upper()
    resolved = _resolved_setting(method, setting)
    physical_model = model or FidelityModel()
    store_path = Path(layer_store_path).resolve()
    architecture_file = Path(architecture_path).resolve()
    destination = Path(output_directory).resolve()
    if progress_every <= 0:
        raise ValueError("progress_every must be positive")
    if max_stages is not None and int(max_stages) <= 0:
        raise ValueError("max_stages must be positive when supplied")
    if interrupt_after_stages is not None and int(interrupt_after_stages) <= 0:
        raise ValueError("interrupt_after_stages must be positive when supplied")
    actual_git_commit, git_dirty = _git_state()
    if destination.exists():
        raise FileExistsError(f"formal output directory already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / f".{destination.name}.inprogress"
    checkpoint_path = temporary / "checkpoint.json"
    if resume:
        if not temporary.is_dir() or not checkpoint_path.is_file():
            raise FileNotFoundError(
                f"formal Large resume checkpoint is missing: {checkpoint_path}")
    else:
        if temporary.exists():
            raise FileExistsError(
                f"formal Large in-progress directory already exists: {temporary}")
        temporary.mkdir()

    end_to_end_start = time.perf_counter_ns()
    core_ns = 0
    timing_breakdown = _empty_timing_breakdown()
    prior_end_to_end_ns = 0
    native_writer: _GzipJsonlWriter | None = None
    canonical_writer: _GzipJsonlWriter | None = None
    decision_writer: _GzipJsonlWriter | None = None
    route_writer: _GzipJsonlWriter | None = None
    try:
        architecture_spec = _load_json(architecture_file)
        architecture = Architecture(dict(architecture_spec))
        architecture.preprocessing()
        capacity = _stock_stage_capacity(architecture)
        duration_contract = {
            "rydberg": physical_model.rydberg_duration_us,
            "1qGate": physical_model.one_qubit_duration_us,
            "atom_transfer": physical_model.transfer_duration_us,
        }
        observed_durations = {
            key: float(architecture.operation_duration.get(key, -1.0))
            for key in duration_contract
        }
        if observed_durations != duration_contract:
            raise ValueError(
                "architecture durations differ from frozen fidelity model: "
                f"{observed_durations} != {duration_contract}"
            )

        with LayerStore(store_path) as store:
            metadata = store.metadata
            store.verify_zac_view()
            logical_ledger_sha256 = store.verify_logical_ledger()
            n_qubits = int(metadata["qubits"])

            architecture_sha256 = _sha256_file(architecture_file)
            config_identity = {
                "method": method,
                "setting": resolved,
                "model": physical_model.to_dict(),
                "max_stages": max_stages,
            }
            config_sha256 = _canonical_hash(config_identity)
            identity = FormalZacCheckpointIdentity(
                git_commit=actual_git_commit,
                input_sha256=str(metadata["statement_sha256"]),
                config_sha256=config_sha256,
                architecture_sha256=architecture_sha256,
            )

            core_start = time.perf_counter_ns()
            initial_mapping = place_stock_sa_from_layer_store(
                architecture,
                store,
                max_gates_per_stage=capacity,
            )
            initial_placement_ns = time.perf_counter_ns() - core_start

            requested_stage_limit: int | None = None
            if resume:
                restored = FormalZacCheckpointManager.restore(
                    checkpoint_path,
                    expected_identity=identity,
                    architecture=architecture,
                    initial_mapping=initial_mapping,
                    store=store,
                    max_gates_per_stage=capacity,
                    setting=resolved,
                )
                placement = restored.placement
                router = restored.route
                output_hash = restored.output_hash
                counters = dict(restored.counters)
                if counters.get("format") != "formal-large-zac-counters-v1":
                    raise ValueError("unsupported formal Large compiler counters")
                stages_total = placement.stage_count
                requested_stage_limit = (
                    stages_total if max_stages is None
                    else min(stages_total, int(max_stages)))
                stage_limit = int(counters["stage_limit"])
                if stage_limit != requested_stage_limit:
                    raise ValueError("resume stage limit differs from checkpoint")
                full_circuit = bool(counters["full_circuit"])
                if full_circuit != (stage_limit == stages_total):
                    raise ValueError("resume full-circuit flag is inconsistent")
                stages_completed = restored.next_layer
                core_ns = int(counters["compiler_core_ns"])
                raw_timing = counters.get("timing_breakdown_ns")
                if not isinstance(raw_timing, Mapping):
                    raise ValueError(
                        "formal Large checkpoint lacks timing_breakdown_ns")
                timing_breakdown = _empty_timing_breakdown()
                if set(raw_timing) != set(timing_breakdown):
                    raise ValueError(
                        "formal Large checkpoint timing key mismatch")
                for key, raw in raw_timing.items():
                    value = int(raw)
                    if value < 0:
                        raise ValueError(
                            f"negative checkpoint timing {key}: {value}")
                    timing_breakdown[key] = value
                prior_end_to_end_ns = int(counters["end_to_end_active_ns"])
                normalizer = IncrementalZairNormalizer.from_state(
                    counters["normalizer"], architecture=architecture_spec)
                pipeline = IncrementalTracePipeline.from_state(
                    counters["pipeline"])
                writer_states = counters["writers"]
                native_writer = _GzipJsonlWriter.resume(
                    temporary / "native.jsonl.gz", writer_states["native"])
                canonical_writer = _GzipJsonlWriter.resume(
                    temporary / "trace.jsonl.gz", writer_states["canonical"])
                decision_writer = _GzipJsonlWriter.resume(
                    temporary / "decisions.jsonl.gz", writer_states["decision"])
                route_writer = _GzipJsonlWriter.resume(
                    temporary / "route.jsonl.gz", writer_states["route"])
            else:
                core_ns = initial_placement_ns
                timing_breakdown = _empty_timing_breakdown(
                    initial_placement_ns)
                placement = FormalZacPlacementStream(
                    method=method,
                    architecture=architecture,
                    initial_mapping=initial_mapping,
                    store=store,
                    max_gates_per_stage=capacity,
                    setting=resolved,
                )
                stages_total = placement.stage_count
                stage_limit = stages_total if max_stages is None else min(
                    stages_total, int(max_stages)
                )
                full_circuit = stage_limit == stages_total
                router = ZACRouteTransitionDriver(
                    architecture,
                    initial_mapping,
                    initial_one_qubit_gates=placement.leading_one_qubit_gates,
                    placer_kind="zac" if method == "M1" else "resident",
                    coloring_exact_threshold=int(
                        24 if resolved is None else
                        resolved.get("coloring_exact_threshold", 24)),
                )
                normalizer = IncrementalZairNormalizer(
                    architecture_spec, model=physical_model)
                validator = IncrementalTraceValidator(
                    n_qubits,
                    expected_one_qubit_gates=(
                        int(metadata["gates_1q"]) if full_circuit else None),
                    expected_two_qubit_gates=(
                        int(metadata["gates_2q"]) if full_circuit else None),
                    expected_logical_ledger_sha256=(
                        logical_ledger_sha256 if full_circuit else None),
                    require_zero_ghost=True,
                    event_order="dependency",
                )
                scorer = IncrementalTraceScorer(
                    n_qubits, physical_model, event_order="dependency")
                pipeline = IncrementalTracePipeline(validator, scorer)
                output_hash = RollingInstructionHash()
                native_writer = _GzipJsonlWriter(
                    temporary / "native.jsonl.gz")
                canonical_writer = _GzipJsonlWriter(
                    temporary / "trace.jsonl.gz")
                decision_writer = _GzipJsonlWriter(
                    temporary / "decisions.jsonl.gz")
                route_writer = _GzipJsonlWriter(
                    temporary / "route.jsonl.gz")
                stages_completed = 0

            if full_circuit and git_dirty:
                raise RuntimeError(
                    "formal full-circuit Large run requires a clean Git worktree")

            def consume_native(instructions: Iterable[Mapping[str, Any]]) -> None:
                assert native_writer is not None and canonical_writer is not None
                for instruction in instructions:
                    output_hash.update(instruction)
                    native_writer.write(instruction)
                    for event in normalizer.consume(instruction):
                        pipeline.consume(event)
                        canonical_writer.write(event.to_dict())

            if not resume:
                consume_native(router.initial_instructions)

            cadence = CheckpointCadence(
                every_stages=checkpoint_every_stages,
                every_seconds=checkpoint_every_seconds,
                completed_stages=stages_completed,
            )

            def close_writers() -> Mapping[str, Mapping[str, Any]]:
                assert native_writer is not None
                assert canonical_writer is not None
                assert decision_writer is not None
                assert route_writer is not None
                native_writer.close()
                canonical_writer.close()
                decision_writer.close()
                route_writer.close()
                return {
                    "native": native_writer.state_dict(),
                    "canonical": canonical_writer.state_dict(),
                    "decision": decision_writer.state_dict(),
                    "route": route_writer.state_dict(),
                }

            def save_boundary(*, reopen: bool) -> None:
                nonlocal native_writer, canonical_writer
                nonlocal decision_writer, route_writer
                writer_states = close_writers()
                active_end_to_end_ns = (
                    prior_end_to_end_ns
                    + time.perf_counter_ns() - end_to_end_start
                )
                counters = {
                    "format": "formal-large-zac-counters-v1",
                    "stage_limit": stage_limit,
                    "full_circuit": full_circuit,
                    "compiler_core_ns": core_ns,
                    "timing_breakdown_ns": dict(timing_breakdown),
                    "end_to_end_active_ns": active_end_to_end_ns,
                    "normalizer": normalizer.state_dict(),
                    "pipeline": pipeline.state_dict(),
                    "writers": writer_states,
                }
                FormalZacCheckpointManager.save(
                    checkpoint_path,
                    identity=identity,
                    placement=placement,
                    route=router,
                    output_hash=output_hash,
                    counters=counters,
                )
                cadence.mark_saved(stages_completed)
                if reopen:
                    native_writer = _GzipJsonlWriter.resume(
                        temporary / "native.jsonl.gz", writer_states["native"])
                    canonical_writer = _GzipJsonlWriter.resume(
                        temporary / "trace.jsonl.gz", writer_states["canonical"])
                    decision_writer = _GzipJsonlWriter.resume(
                        temporary / "decisions.jsonl.gz", writer_states["decision"])
                    route_writer = _GzipJsonlWriter.resume(
                        temporary / "route.jsonl.gz", writer_states["route"])

            iterator = iter(placement)
            for expected_layer in range(stages_completed, stage_limit):
                placement_start = time.perf_counter_ns()
                row = next(iterator)
                placement_ns = time.perf_counter_ns() - placement_start
                timing_breakdown["placement_transition_ns"] += placement_ns
                _accumulate_timing(
                    timing_breakdown, row.decision_log,
                    _DECISION_TIMING_KEYS)
                _accumulate_timing(
                    timing_breakdown, row.backend_timing,
                    _BACKEND_TIMING_KEYS)
                if row.layer != expected_layer:
                    raise AssertionError(
                        f"placement emitted stage {row.layer}, expected {expected_layer}")
                route_start = time.perf_counter_ns()
                route = router.route_layer(
                    row.layer,
                    row.b_l,
                    row.g_l,
                    row.b_l_plus_1,
                    row.gates,
                    row.gate_ids,
                    row.parent_one_qubit_gates,
                )
                routing_ns = time.perf_counter_ns() - route_start
                timing_breakdown["routing_ns"] += routing_ns
                core_ns += placement_ns + routing_ns

                consume_native(route.instructions)
                if row.decision_log is not None:
                    # Wall-clock samples are aggregated in the manifest timing
                    # ledger.  They are deliberately excluded from the
                    # deterministic decision stream so checkpoint/resume remains
                    # byte-identical to an uninterrupted compile.
                    decision_writer.write({
                        key: value for key, value in row.decision_log.items()
                        if not key.endswith("_ns")
                    })
                for entry in route.route_log:
                    route_writer.write(entry)
                stages_completed += 1
                if stages_completed % progress_every == 0:
                    for writer in (
                        native_writer, canonical_writer, decision_writer,
                        route_writer,
                    ):
                        writer.flush()
                    print(
                        f"[LARGE] {method} completed {stages_completed}/"
                        f"{stage_limit} stages; core={core_ns / 1e9:.3f}s",
                        flush=True,
                    )
                if cadence.due(stages_completed):
                    save_boundary(reopen=stages_completed < stage_limit)
                if (interrupt_after_stages is not None and
                        stages_completed == int(interrupt_after_stages)):
                    if cadence.last_completed_stages != stages_completed:
                        save_boundary(reopen=True)
                    raise RuntimeError(
                        f"simulated interruption after stage {stages_completed}")

            if full_circuit:
                try:
                    next(iterator)
                except StopIteration:
                    pass
                else:
                    raise AssertionError("placement stream exceeds declared stage count")

            # Always leave an exact final recovery boundary before finalization.
            # When the cadence fired on the last stage, its writers are already
            # closed and its checkpoint is exactly this boundary; do not pickle
            # the same potentially large cache state twice.
            if cadence.last_completed_stages != stages_completed:
                save_boundary(reopen=False)
            report = pipeline.finalize()
            final_git_commit, final_git_dirty = _git_state()
            git_unchanged = bool(
                final_git_commit == actual_git_commit
                and final_git_dirty == git_dirty
                and not final_git_dirty
            )
            if full_circuit and not git_unchanged:
                raise RuntimeError(
                    "formal full-circuit Large Git state changed during compilation")

            end_to_end_ns = (
                prior_end_to_end_ns + time.perf_counter_ns() - end_to_end_start
            )
            support_claim_eligible = bool(
                full_circuit
                and report["validation"]["ok"]
                and report["validation"]["ghost_hits"] == 0
                and report["validation"]["one_qubit_gates"]
                == int(metadata["gates_1q"])
                and report["validation"]["two_qubit_gates"]
                == int(metadata["gates_2q"])
                and report["validation"]["logical_ledger_sha256"]
                == logical_ledger_sha256
                and git_unchanged
            )
            result = FormalLargeZacResult(
                method=method,
                status="success",
                stages_completed=stages_completed,
                stages_total=stages_total,
                full_circuit=full_circuit,
                support_claim_eligible=support_claim_eligible,
                compiler_core_ns=core_ns,
                timing_breakdown_ns=dict(timing_breakdown),
                end_to_end_ns=end_to_end_ns,
                peak_rss_bytes=_peak_rss_bytes(),
                native_instructions=native_writer.count,
                canonical_events=canonical_writer.count,
                native_chain_sha256=native_writer.chain_sha256,
                canonical_chain_sha256=canonical_writer.chain_sha256,
                fidelity=report["fidelity"],
                validation=report["validation"],
                output_directory=str(destination),
            )
            manifest = {
                "format": "formal-large-zac-attempt-v1",
                "method": method,
                "event_order": "dependency",
                "resumed": bool(resume),
                "git": {
                    "start_commit": actual_git_commit,
                    "start_dirty": git_dirty,
                    "end_commit": final_git_commit,
                    "end_dirty": final_git_dirty,
                    "unchanged_clean": git_unchanged,
                },
                "checkpoint": {
                    "path": "checkpoint.json",
                    "every_stages": int(checkpoint_every_stages),
                    "every_seconds": float(checkpoint_every_seconds),
                    "identity": identity.as_dict(),
                },
                "input": {
                    "layer_store": str(store_path),
                    "layer_store_sha256": _sha256_file(store_path),
                    "statement_sha256": metadata["statement_sha256"],
                    "logical_ledger_sha256": logical_ledger_sha256,
                    "qubits": n_qubits,
                    "gates_1q": int(metadata["gates_1q"]),
                    "gates_2q": int(metadata["gates_2q"]),
                },
                "architecture": {
                    "path": str(architecture_file),
                    "sha256": architecture_sha256,
                    "stage_capacity": capacity,
                },
                "model": physical_model.to_dict(),
                "runtime_definition": {
                    "compiler_core_ns": (
                        "initial placement plus each exact placement transition "
                        "and production routing call; input preparation, event "
                        "normalization, validation, scoring and file I/O excluded"
                    ),
                    "timing_breakdown_ns": (
                        "requested M3/M4 component ledger; native sub-timers may "
                        "overlap placement_transition_ns and are not additive"
                    ),
                },
                "setting": resolved,
                "setting_sha256": (
                    None if resolved is None else _canonical_hash(resolved)),
                "compiler_config_sha256": config_sha256,
                "artifacts": {
                    "native": "native.jsonl.gz",
                    "canonical_trace": "trace.jsonl.gz",
                    "decisions": "decisions.jsonl.gz",
                    "route": "route.jsonl.gz",
                },
                "result": result.to_dict(),
            }
            _write_json_atomic(temporary / "manifest.json", manifest)

        os.replace(temporary, destination)
        return result
    except BaseException:
        for writer in (
            native_writer, canonical_writer, decision_writer, route_writer,
        ):
            if writer is not None:
                try:
                    writer.close()
                except Exception:
                    pass
        # A published checkpoint owns a resumable, complete gzip-member prefix.
        # Before the first checkpoint there is no safe state to resume, so the
        # function removes only the private directory it just created.
        if not checkpoint_path.exists():
            shutil.rmtree(temporary, ignore_errors=True)
        raise


__all__ = ["FormalLargeZacResult", "compile_formal_large_zac"]
