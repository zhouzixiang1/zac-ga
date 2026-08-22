"""Formal bounded-memory Large compiler orchestration for QMAP 3.2 (M2).

The C++ streaming compiler owns the routing-aware A* search.  This module is
the evidence boundary around it: it audits the frozen SQLite schedule, derives
the QMAP architecture from the same converter used by the ordinary experiment
runner, times only the CLI process, and independently normalizes, validates,
scores, and archives the resulting NA program.

An attempt is always private until every ledger and physical check passes.
Successful attempts are published with one atomic directory rename.  Timeout,
compiler, and verification failures retain a uniquely named ``.inprogress``
directory with logs and a failure manifest, but never create the requested
final directory.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import resource
import signal
import subprocess
import time
from typing import Any, Iterator, Mapping
import uuid

from evaluation import FidelityModel, TraceValidationError

from .na_instruction_stream import normalize_na_incrementally
from .qasm_sqlite import LogicalLedgerHasher
from .qmap_schedule_sqlite import QMAP_SCHEDULE_FORMAT, QmapScheduleStore
from .trace_pipeline import (
    IncrementalTracePipeline,
    IncrementalTraceScorer,
    IncrementalTraceValidator,
)


_QMAP_FREEZE_DIRECTORY = (
    Path(__file__).resolve().parents[1] / "third_party" / "qmap32_streaming"
)

# This is the JSON form consumed by RoutingAwareCompiler::Config in the
# v3.2.0 streaming CLI.  Equality is deliberate: accepting extra or omitted
# fields would make the formal M2 configuration differ from the paper path.
QMAP_32_FROZEN_STREAM_CONFIG: Mapping[str, Any] = {
    "logLevel": 4,
    "schedulerConfig": None,
    "reuseAnalyzerConfig": None,
    "routerConfig": None,
    "placerConfig": {
        "useWindow": True,
        "windowMinWidth": 8,
        "windowRatio": 1.0,
        "windowShare": 0.6,
        "deepeningFactor": 0.6,
        "deepeningValue": 0.2,
        "lookaheadFactor": 0.2,
        "reuseLevel": 5.0,
        "maxNodes": 50_000_000,
    },
    "codeGeneratorConfig": {
        "parkingOffset": 1,
        "warnUnsupportedGates": True,
    },
}

_ZERO_HASH = "0" * 64


@dataclass(frozen=True)
class QmapScheduleAudit:
    qubits: int
    events: int
    gates_1q: int
    gates_2q: int
    two_qubit_layers: int
    scheduled_items: int
    schedule_sha256: str
    logical_ledger_sha256: str
    source_statement_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FormalLargeQmapResult:
    method: str
    status: str
    two_qubit_layers_completed: int
    two_qubit_layers_total: int
    full_circuit: bool
    provenance_ok: bool
    support_claim_eligible: bool
    compiler_core_ns: int
    compiler_process_wall_ns: int
    compiler_statistics: Mapping[str, int]
    compiler_cpu_ns: int | None
    end_to_end_ns: int
    peak_rss_bytes: int | None
    rss_limit_bytes: int
    rss_limit_exceeded: bool
    native_chunks: int
    native_operations: int
    canonical_events: int
    native_sha256: str
    metadata_sha256: str
    canonical_gzip_sha256: str
    canonical_chain_sha256: str
    fidelity: Mapping[str, Any]
    validation: Mapping[str, Any]
    output_directory: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class FormalLargeQmapAttemptError(RuntimeError):
    """A launched formal M2 attempt failed without publishing final output."""

    def __init__(self, status: str, attempt_directory: Path, message: str):
        super().__init__(message)
        self.status = str(status)
        self.attempt_directory = Path(attempt_directory)


def _load_json(path: Path) -> Mapping[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, Mapping):
        raise ValueError(f"JSON root must be an object: {path}")
    return dict(value)


def _sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_hash(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True, ensure_ascii=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _write_json_canonical(path: Path, value: Mapping[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(
            value, handle, sort_keys=True, separators=(",", ":"),
            ensure_ascii=True,
        )
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _convert_architecture(zac_spec: Mapping[str, Any]) -> Mapping[str, Any]:
    """Invoke the repository's canonical ``experiments/spec_convert.py``."""

    converter_path = Path(__file__).resolve().parents[2] / "experiments" / "spec_convert.py"
    if not converter_path.is_file():
        raise FileNotFoundError(f"QMAP architecture converter is missing: {converter_path}")
    module_spec = importlib.util.spec_from_file_location(
        "_formal_large_qmap_spec_convert", converter_path,
    )
    if module_spec is None or module_spec.loader is None:
        raise RuntimeError(f"cannot load QMAP architecture converter: {converter_path}")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    converted = module.convert(dict(zac_spec))
    if not isinstance(converted, Mapping):
        raise TypeError("spec_convert.convert did not return an object")
    return dict(converted)


def _validate_architecture_model(
    converted: Mapping[str, Any], model: FidelityModel,
) -> None:
    durations = converted.get("operation_duration")
    if not isinstance(durations, Mapping):
        raise ValueError("converted QMAP architecture has no operation_duration")
    expected_durations = {
        "single_qubit_gate": model.one_qubit_duration_us,
        "rydberg_gate": model.rydberg_duration_us,
        "atom_transfer": model.transfer_duration_us,
    }
    observed_durations = {
        key: float(durations.get(key, -1.0)) for key in expected_durations
    }
    if observed_durations != expected_durations:
        raise ValueError(
            "QMAP architecture durations differ from the frozen fidelity model: "
            f"{observed_durations} != {expected_durations}"
        )
    fidelities = converted.get("operation_fidelity")
    if not isinstance(fidelities, Mapping):
        raise ValueError("converted QMAP architecture has no operation_fidelity")
    expected_fidelities = {
        "single_qubit_gate": model.one_qubit_fidelity,
        "rydberg_gate": model.two_qubit_fidelity,
        "atom_transfer": model.transfer_fidelity,
    }
    observed_fidelities = {
        key: float(fidelities.get(key, -1.0)) for key in expected_fidelities
    }
    if observed_fidelities != expected_fidelities:
        raise ValueError(
            "QMAP architecture fidelities differ from the frozen model: "
            f"{observed_fidelities} != {expected_fidelities}"
        )
    aods = converted.get("aods")
    if not isinstance(aods, list) or len(aods) != 1:
        raise ValueError("formal M2 requires exactly one AOD")


def _validate_frozen_config(value: Mapping[str, Any]) -> Mapping[str, Any]:
    resolved = json.loads(json.dumps(value))
    if resolved != QMAP_32_FROZEN_STREAM_CONFIG:
        raise ValueError("M2 streaming configuration differs from frozen QMAP 3.2 A* settings")
    return resolved


def _valid_sha256(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        bytes.fromhex(value)
    except ValueError:
        return False
    return True


def audit_qmap_schedule(path: str | Path) -> QmapScheduleAudit:
    """Audit counts, schema, layer legality, digest, and per-atom gate order."""

    schedule_path = Path(path).resolve()
    with QmapScheduleStore(schedule_path) as store:
        verified = dict(store.verify())
        metadata = dict(store.metadata)
        connection = store.connection
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise ValueError(f"QMAP schedule SQLite integrity check failed: {integrity}")

        n_qubits = int(metadata["qubits"])
        if n_qubits <= 0:
            raise ValueError("formal M2 schedule must contain at least one qubit")
        two_layers = int(verified["two_qubit_layers"])
        scheduled_items = int(verified["scheduled_items"])
        expected_items = two_layers + 1
        if scheduled_items != expected_items:
            raise ValueError(
                f"QMAP schedule must contain one final 1Q item: "
                f"{scheduled_items} != {expected_items}"
            )

        source_statement_sha256 = str(metadata.get("source_statement_sha256", ""))
        if not _valid_sha256(source_statement_sha256):
            raise ValueError("QMAP schedule has no valid frozen source statement SHA256")

        logical = LogicalLedgerHasher(n_qubits)
        expected_seq = 0
        rows = connection.execute(
            "SELECT seq,layer,kind,operation,q0,q1,statement "
            "FROM qmap_operations ORDER BY seq"
        )
        for row in rows:
            seq = int(row["seq"])
            if seq != expected_seq:
                raise ValueError(
                    f"QMAP operation seq ledger is not contiguous: {seq} != {expected_seq}"
                )
            expected_seq += 1
            layer = int(row["layer"])
            kind = int(row["kind"])
            operation = str(row["operation"])
            q0 = int(row["q0"])
            q1 = None if row["q1"] is None else int(row["q1"])
            statement = str(row["statement"])
            if not 0 <= layer < scheduled_items:
                raise ValueError(f"QMAP operation {seq} has invalid scheduled layer {layer}")
            if not 0 <= q0 < n_qubits:
                raise ValueError(f"QMAP operation {seq} has invalid q0={q0}")
            if not statement.strip():
                raise ValueError(f"QMAP operation {seq} has an empty source statement")
            if kind == 1:
                if q1 is not None or operation not in {"u1", "u2", "u3"}:
                    raise ValueError(f"QMAP operation {seq} is not a frozen 1Q operation")
                logical.record_one_qubit(q0)
            elif kind == 2:
                if (
                    q1 is None or not 0 <= q1 < n_qubits or q1 == q0
                    or operation != "cz" or layer >= two_layers
                ):
                    raise ValueError(f"QMAP operation {seq} is not a valid frozen CZ")
                logical.record_cz(q0, q1)
            else:
                raise ValueError(f"QMAP operation {seq} has unsupported kind {kind}")
        if expected_seq != int(verified["events"]):
            raise ValueError("QMAP operation seq ledger disagrees with event count")

        layer_rows = connection.execute(
            "SELECT layer,gate_count FROM two_qubit_counts ORDER BY layer"
        )
        for expected_layer, row in enumerate(layer_rows):
            layer = int(row["layer"])
            count = int(row["gate_count"])
            if layer != expected_layer or count <= 0:
                raise ValueError("QMAP two-qubit-count layers must be contiguous and nonempty")
        count_rows = connection.execute(
            "SELECT COUNT(*) FROM two_qubit_counts"
        ).fetchone()[0]
        if int(count_rows) != two_layers:
            raise ValueError("QMAP two-qubit-count ledger has missing layers")

        active_layer: int | None = None
        used: set[int] = set()
        for row in connection.execute(
            "SELECT layer,q0,q1 FROM qmap_operations WHERE kind=2 "
            "ORDER BY layer,seq"
        ):
            layer = int(row["layer"])
            if active_layer != layer:
                active_layer = layer
                used = set()
            q0, q1 = int(row["q0"]), int(row["q1"])
            if q0 in used or q1 in used:
                raise ValueError(
                    f"qubit occurs in multiple CZ gates in QMAP layer {layer}"
                )
            used.update((q0, q1))

        return QmapScheduleAudit(
            qubits=n_qubits,
            events=int(verified["events"]),
            gates_1q=int(verified["gates_1q"]),
            gates_2q=int(verified["gates_2q"]),
            two_qubit_layers=two_layers,
            scheduled_items=scheduled_items,
            schedule_sha256=str(verified["schedule_sha256"]),
            logical_ledger_sha256=logical.hexdigest(),
            source_statement_sha256=source_statement_sha256,
        )


def _iter_gate_pairs(
    path: Path, expected_layers: int,
) -> Iterator[tuple[tuple[int, int], ...]]:
    """Yield one bounded CZ-pair ledger entry per native CZ pulse."""

    with QmapScheduleStore(path) as store:
        for layer in range(expected_layers):
            scheduled = store.layer(layer)
            pairs = tuple(
                tuple(sorted((int(operation.q0), int(operation.q1))))
                for operation in scheduled.two_qubit
                if operation.q1 is not None
            )
            if not pairs:
                raise TraceValidationError(
                    f"QMAP schedule layer {layer} has no CZ gate pairs"
                )
            yield pairs


class _GzipJsonlWriter:
    """Deterministic gzip JSONL writer with an uncompressed chain hash."""

    def __init__(self, path: Path):
        self.path = path
        self.count = 0
        self.chain_sha256 = _ZERO_HASH
        self._raw = path.open("wb")
        self._gzip = gzip.GzipFile(
            filename="", fileobj=self._raw, mode="wb", mtime=0,
        )

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

    def close(self) -> None:
        if self._gzip is None:
            return
        try:
            self._gzip.close()
        finally:
            self._gzip = None
            self._raw.close()


def _metadata_schedule_record(audit: QmapScheduleAudit) -> Mapping[str, Any]:
    return {
        "format": QMAP_SCHEDULE_FORMAT,
        "qubits": audit.qubits,
        "events": audit.events,
        "gates_1q": audit.gates_1q,
        "gates_2q": audit.gates_2q,
        "two_qubit_layers": audit.two_qubit_layers,
        "scheduled_items": audit.scheduled_items,
    }


def _validate_placement(value: Any, n_qubits: int, label: str) -> None:
    if not isinstance(value, list) or len(value) != n_qubits:
        raise TraceValidationError(f"{label} is not a complete placement")
    observed: set[int] = set()
    locations: set[tuple[int, int, int]] = set()
    for item in value:
        if not isinstance(item, Mapping):
            raise TraceValidationError(f"{label} contains a non-object record")
        try:
            q = int(item["qubit"])
            location = (int(item["slm"]), int(item["row"]), int(item["column"]))
        except (KeyError, TypeError, ValueError) as error:
            raise TraceValidationError(f"{label} contains an invalid record") from error
        if not 0 <= q < n_qubits or q in observed:
            raise TraceValidationError(f"{label} has an invalid qubit ledger")
        if location in locations:
            raise TraceValidationError(f"{label} contains duplicate occupancy")
        observed.add(q)
        locations.add(location)


def _audit_metadata_jsonl(
    path: Path, audit: QmapScheduleAudit,
) -> Mapping[str, int]:
    chunks = 0
    operations = 0
    peak_operations = 0
    with path.open(encoding="utf-8") as handle:
        for raw in handle:
            if not raw.strip():
                raise TraceValidationError("QMAP metadata JSONL contains an empty line")
            try:
                record = json.loads(raw)
            except json.JSONDecodeError as error:
                raise TraceValidationError("QMAP metadata JSONL is malformed") from error
            if not isinstance(record, Mapping):
                raise TraceValidationError("QMAP metadata JSONL record is not an object")
            if record.get("schema") != "qmap-stream-metadata-v1":
                raise TraceValidationError("unsupported QMAP metadata JSONL schema")
            try:
                native_operations = int(record["native_operations"])
            except (KeyError, TypeError, ValueError) as error:
                raise TraceValidationError("invalid native operation count") from error
            if native_operations < 0:
                raise TraceValidationError("native operation count cannot be negative")
            operations += native_operations
            peak_operations = max(peak_operations, native_operations)
            if chunks == 0:
                if record.get("kind") != "initial":
                    raise TraceValidationError("first QMAP metadata record is not initial")
                if record.get("schedule") != _metadata_schedule_record(audit):
                    raise TraceValidationError("QMAP CLI schedule metadata disagrees with SQLite")
                _validate_placement(record.get("placement"), audit.qubits, "initial placement")
            else:
                expected_layer = chunks - 1
                if record.get("kind") != "transition" or int(
                    record.get("layer", -1)
                ) != expected_layer:
                    raise TraceValidationError(
                        f"QMAP transition metadata is not contiguous at layer {expected_layer}"
                    )
                for key in (
                    "previous_placement", "gate_placement", "storage_placement",
                ):
                    _validate_placement(record.get(key), audit.qubits, key)
                for key in ("previous_reuse", "next_reuse"):
                    values = record.get(key)
                    if (
                        not isinstance(values, list)
                        or len(set(values)) != len(values)
                        or any(not isinstance(q, int) or not 0 <= q < audit.qubits for q in values)
                    ):
                        raise TraceValidationError(f"invalid {key} metadata ledger")
            chunks += 1
    expected_chunks = audit.two_qubit_layers + 1
    if chunks != expected_chunks:
        raise TraceValidationError(
            f"QMAP metadata chunk count mismatch: {chunks} != {expected_chunks}"
        )
    return {
        "chunks": chunks,
        "native_operations": operations,
        "peak_native_operations_per_chunk": peak_operations,
    }


def _rusage_snapshot() -> tuple[float, float, int]:
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return float(usage.ru_utime), float(usage.ru_stime), int(usage.ru_maxrss)


def _terminate_process_group(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=2.0)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    process.wait()


def _git_state() -> Mapping[str, Any]:
    root = Path(__file__).resolve().parents[2]
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    dirty = bool(subprocess.run(
        ["git", "status", "--porcelain"], cwd=root, check=True,
        capture_output=True, text=True,
    ).stdout.strip())
    return {"commit": commit, "dirty": dirty}


def _verify_qmap_provenance(
    source_tree: Path, cli_file: Path, config_file: Path,
) -> Mapping[str, Any]:
    """Close M2 provenance over the frozen bundle, source result, and CLI."""

    verifier_path = _QMAP_FREEZE_DIRECTORY / "verify_frozen_patch.py"
    module_spec = importlib.util.spec_from_file_location(
        "_formal_large_qmap_freeze_verifier", verifier_path,
    )
    if module_spec is None or module_spec.loader is None:
        raise RuntimeError(f"cannot load QMAP freeze verifier: {verifier_path}")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    try:
        bundle = dict(module.verify_frozen_bundle())
        source = dict(module.verify_patched_source_tree(source_tree, bundle))
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        raise ValueError(f"QMAP frozen source provenance failed: {error}") from error
    if not bundle.get("ok") or not source.get("ok"):
        raise ValueError("QMAP frozen provenance verifier did not return ok")
    if _sha256_file(config_file) != bundle["config_sha256"]:
        raise ValueError("formal M2 config does not match the frozen bundle")
    resolved_source = Path(source["source_tree"]).resolve()
    try:
        relative_cli = cli_file.resolve().relative_to(resolved_source)
    except ValueError as error:
        raise ValueError(
            "formal M2 CLI must be located inside the verified QMAP source tree"
        ) from error
    if cli_file.name != "mqt-qmap-na-zoned-stream":
        raise ValueError("formal M2 CLI has an unexpected executable name")
    frozen_cli = dict(bundle["formal_cli"])
    if relative_cli.as_posix() != frozen_cli["relative_path"]:
        raise ValueError(
            "formal M2 CLI relative path differs from the frozen binary"
        )
    cli_size = cli_file.stat().st_size
    if cli_size != int(frozen_cli["size_bytes"]):
        raise ValueError("formal M2 CLI size differs from the frozen binary")
    cli_sha256 = _sha256_file(cli_file)
    if cli_sha256 != frozen_cli["sha256"]:
        raise ValueError("formal M2 CLI SHA256 differs from the frozen binary")
    return {
        "ok": True,
        "bundle": bundle,
        "patched_source": source,
        "cli_relative_path": relative_cli.as_posix(),
        "cli_sha256": cli_sha256,
        "cli_size_bytes": cli_size,
        "frozen_cli": frozen_cli,
    }


def _process_group_rss_bytes(process_group: int) -> int | None:
    """Sample aggregate RSS for one POSIX process group using ``ps``."""

    try:
        completed = subprocess.run(
            ["/bin/ps", "-o", "rss=", "-g", str(int(process_group))],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except OSError:
        return None
    values: list[int] = []
    for raw in completed.stdout.splitlines():
        try:
            values.append(int(raw.strip()))
        except ValueError:
            continue
    return None if not values else sum(values) * 1024


def _address_space_limiter(limit_bytes: int):
    """Return a child-only hard address-space cap as an RSS overshoot guard."""

    # Darwin exposes RLIMIT_AS but rejects lowering its sentinel hard limit.
    # The process-group RSS monitor remains the authoritative cross-platform
    # limit there. Linux can additionally fail allocation before RSS overshoots.
    if os.uname().sysname == "Darwin":
        return None

    def apply_limit() -> None:
        resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, limit_bytes))

    return apply_limit


def _parse_cli_summary(
    stdout_path: Path,
    audit: QmapScheduleAudit,
    metadata_audit: Mapping[str, int],
) -> Mapping[str, Any]:
    lines = [line for line in stdout_path.read_text().splitlines() if line.strip()]
    if len(lines) != 1:
        raise TraceValidationError("QMAP CLI must emit exactly one JSON result line")
    try:
        value = json.loads(lines[0])
    except json.JSONDecodeError as error:
        raise TraceValidationError("QMAP CLI result is not valid JSON") from error
    if not isinstance(value, Mapping) or value.get("schema") != "qmap-stream-cli-result-v1":
        raise TraceValidationError("unsupported QMAP CLI result schema")
    expected = {
        "events": audit.events,
        "two_qubit_layers": audit.two_qubit_layers,
        "chunks": int(metadata_audit["chunks"]),
        "peak_native_operations_per_chunk": int(
            metadata_audit["peak_native_operations_per_chunk"]
        ),
    }
    for key, expected_value in expected.items():
        if value.get(key) != expected_value:
            raise TraceValidationError(
                f"QMAP CLI result mismatch for {key}: {value.get(key)} != {expected_value}"
            )
    raw_statistics = value.get("statistics")
    keys = (
        "schedulingTime", "reuseAnalysisTime", "placementTime",
        "routingTime", "codeGenerationTime", "totalTime",
    )
    if not isinstance(raw_statistics, Mapping) or set(raw_statistics) != set(keys):
        raise TraceValidationError("QMAP CLI statistics ledger is incomplete")
    statistics: dict[str, int] = {}
    for key in keys:
        raw = raw_statistics[key]
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise TraceValidationError(f"invalid QMAP CLI statistic {key}: {raw!r}")
        statistics[key] = raw
    if statistics["totalTime"] != sum(statistics[key] for key in keys[:-1]):
        raise TraceValidationError("QMAP CLI totalTime does not equal its five components")
    return {**dict(value), "statistics": statistics}


def _failure_manifest(
    base: Mapping[str, Any], *, status: str, message: str,
    compiler_core_ns: int | None = None, exit_code: int | None = None,
    compiler_cpu_ns: int | None = None, peak_rss_bytes: int | None = None,
    end_to_end_ns: int | None = None,
    compiler_process_wall_ns: int | None = None,
    rss_limit_exceeded: bool = False,
) -> Mapping[str, Any]:
    return {
        **dict(base),
        "status": status,
        "result": {
            "method": "M2",
            "status": status,
            "support_claim_eligible": False,
            "compiler_core_ns": compiler_core_ns,
            "compiler_process_wall_ns": compiler_process_wall_ns,
            "compiler_cpu_ns": compiler_cpu_ns,
            "peak_rss_bytes": peak_rss_bytes,
            "rss_limit_exceeded": bool(rss_limit_exceeded),
            "end_to_end_ns": end_to_end_ns,
            "exit_code": exit_code,
            "error": message,
        },
    }


def compile_formal_large_qmap(
    *,
    schedule_path: str | Path,
    architecture_path: str | Path,
    config_path: str | Path,
    cli_path: str | Path,
    qmap_source_tree: str | Path,
    output_directory: str | Path,
    model: FidelityModel | None = None,
    timeout_seconds: float = 86_400.0,
    rss_limit_bytes: int = 22 * 1024**3,
    development_prefix: bool = False,
) -> FormalLargeQmapResult:
    """Compile, strictly verify, score, and atomically publish formal M2.

    The C++ v3.2 streaming CLI currently has no resumable checkpoint protocol.
    Every manifest therefore states ``resume_supported=false``.  A small or
    truncated schedule may be used as a development fixture only by setting
    ``development_prefix=True``; such a result can never support a Large claim.
    """

    end_to_end_start = time.perf_counter_ns()
    schedule_file = Path(schedule_path).resolve()
    architecture_file = Path(architecture_path).resolve()
    config_file = Path(config_path).resolve()
    cli_file = Path(cli_path).resolve()
    qmap_source = Path(qmap_source_tree).resolve()
    destination = Path(output_directory).resolve()
    physical_model = model or FidelityModel()

    for label, path in (
        ("schedule", schedule_file),
        ("architecture", architecture_file),
        ("config", config_file),
        ("CLI", cli_file),
    ):
        if not path.is_file():
            raise FileNotFoundError(f"formal M2 {label} file is missing: {path}")
    if not os.access(cli_file, os.X_OK):
        raise PermissionError(f"formal M2 CLI is not executable: {cli_file}")
    if not qmap_source.is_dir():
        raise FileNotFoundError(
            f"formal M2 QMAP source tree is missing: {qmap_source}"
        )
    if destination.exists():
        raise FileExistsError(f"formal M2 output already exists: {destination}")
    if not float(timeout_seconds) > 0:
        raise ValueError("formal M2 timeout_seconds must be positive")
    if int(rss_limit_bytes) <= 0:
        raise ValueError("formal M2 rss_limit_bytes must be positive")

    resolved_config = _validate_frozen_config(_load_json(config_file))
    zac_architecture = _load_json(architecture_file)
    converted_architecture = _convert_architecture(zac_architecture)
    _validate_architecture_model(converted_architecture, physical_model)
    schedule_audit = audit_qmap_schedule(schedule_file)
    provenance = dict(
        _verify_qmap_provenance(qmap_source, cli_file, config_file)
    )
    orchestrator_git = dict(_git_state())
    if not development_prefix and orchestrator_git["dirty"]:
        raise RuntimeError(
            "formal full M2 requires the feature worktree HEAD to be clean"
        )

    input_hashes = {
        "schedule_file_sha256": _sha256_file(schedule_file),
        "architecture_source_sha256": _sha256_file(architecture_file),
        "config_file_sha256": _sha256_file(config_file),
        "config_canonical_sha256": _canonical_hash(resolved_config),
        "cli_sha256": _sha256_file(cli_file),
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    attempt = destination.parent / (
        f".{destination.name}.{uuid.uuid4().hex}.inprogress"
    )
    attempt.mkdir()

    converted_path = attempt / "qmap_architecture.json"
    native_path = attempt / "native.na"
    metadata_path = attempt / "metadata.jsonl"
    canonical_path = attempt / "trace.jsonl.gz"
    stdout_path = attempt / "compiler.stdout.log"
    stderr_path = attempt / "compiler.stderr.log"
    _write_json_canonical(converted_path, converted_architecture)
    converter_path = Path(__file__).resolve().parents[2] / "experiments" / "spec_convert.py"

    base_manifest: dict[str, Any] = {
        "format": "formal-large-qmap-attempt-v1",
        "method": "M2",
        "status": "inprogress",
        "event_order": "chronological",
        "development_prefix": bool(development_prefix),
        "resume_supported": False,
        "input": {
            "schedule_path": str(schedule_file),
            **schedule_audit.to_dict(),
            **input_hashes,
        },
        "architecture": {
            "source_path": str(architecture_file),
            "converted_artifact": "qmap_architecture.json",
            "converted_sha256": _sha256_file(converted_path),
            "converter_path": str(converter_path),
            "converter_sha256": _sha256_file(converter_path),
        },
        "qmap": {
            "base_commit": provenance["bundle"]["base_commit"],
            "mqt_qmap_version": "3.2.0",
            "mqt_core_version": "3.1.0",
            "source_tree": str(qmap_source),
            "cli_path": str(cli_file),
            "cli_relative_path": provenance["cli_relative_path"],
            "cli_sha256": input_hashes["cli_sha256"],
            "cli_size_bytes": provenance["cli_size_bytes"],
            "config_path": str(config_file),
            "config_sha256": input_hashes["config_canonical_sha256"],
            "provenance": provenance,
        },
        "orchestrator_git": orchestrator_git,
        "model": physical_model.to_dict(),
        "runtime_definition": (
            "compiler_core_ns is QMAP Statistics.totalTime in microseconds, "
            "converted to nanoseconds; its five components exclude synchronous "
            "stream consumer I/O. compiler_process_wall_ns is outer perf_counter_ns "
            "around the complete CLI process. Python pre/post work is excluded"
        ),
        "rss_definition": (
            "periodic aggregate RSS sampling of the CLI process group, with a "
            "child RLIMIT_AS overshoot guard where supported"
        ),
        "rss_limit_bytes": int(rss_limit_bytes),
        "artifacts": {
            "native": "native.na",
            "metadata": "metadata.jsonl",
            "canonical_trace": "trace.jsonl.gz",
            "stdout": "compiler.stdout.log",
            "stderr": "compiler.stderr.log",
        },
    }

    before_usage = _rusage_snapshot()
    compiler_process_start = time.perf_counter_ns()
    exit_code: int | None = None
    termination_status: str | None = None
    peak_rss_bytes: int | None = None
    try:
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            process = subprocess.Popen(
                [
                    str(cli_file), str(schedule_file), str(converted_path),
                    str(config_file), str(native_path), str(metadata_path),
                ],
                cwd=attempt,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
                preexec_fn=_address_space_limiter(int(rss_limit_bytes)),
            )
            deadline = (
                compiler_process_start
                + int(float(timeout_seconds) * 1_000_000_000)
            )
            while True:
                sampled = _process_group_rss_bytes(process.pid)
                if sampled is not None:
                    peak_rss_bytes = max(peak_rss_bytes or 0, sampled)
                    if sampled > int(rss_limit_bytes):
                        termination_status = "oom"
                        _terminate_process_group(process)
                        break
                exit_code = process.poll()
                if exit_code is not None:
                    break
                if time.perf_counter_ns() >= deadline:
                    termination_status = "timeout"
                    _terminate_process_group(process)
                    break
                time.sleep(0.25)
            exit_code = process.returncode
    except (OSError, subprocess.SubprocessError) as error:
        compiler_process_wall_ns = time.perf_counter_ns() - compiler_process_start
        after_usage = _rusage_snapshot()
        compiler_cpu_ns = int(
            ((after_usage[0] - before_usage[0])
             + (after_usage[1] - before_usage[1])) * 1_000_000_000
        )
        message = f"formal M2 CLI launch failed: {type(error).__name__}: {error}"
        _write_json_atomic(
            attempt / "manifest.json",
            _failure_manifest(
                base_manifest, status="compiler_error", message=message,
                exit_code=None, compiler_cpu_ns=compiler_cpu_ns,
                peak_rss_bytes=peak_rss_bytes,
                compiler_process_wall_ns=compiler_process_wall_ns,
                end_to_end_ns=time.perf_counter_ns() - end_to_end_start,
            ),
        )
        raise FormalLargeQmapAttemptError(
            "compiler_error", attempt, message
        ) from error
    compiler_process_wall_ns = time.perf_counter_ns() - compiler_process_start
    after_usage = _rusage_snapshot()
    compiler_cpu_ns = int(
        ((after_usage[0] - before_usage[0]) + (after_usage[1] - before_usage[1]))
        * 1_000_000_000
    )

    if termination_status == "timeout":
        message = f"formal M2 CLI timed out after {timeout_seconds:g} seconds"
        _write_json_atomic(
            attempt / "manifest.json",
            _failure_manifest(
                base_manifest, status="timeout", message=message,
                compiler_core_ns=None, exit_code=exit_code,
                compiler_cpu_ns=compiler_cpu_ns, peak_rss_bytes=peak_rss_bytes,
                compiler_process_wall_ns=compiler_process_wall_ns,
                end_to_end_ns=time.perf_counter_ns() - end_to_end_start,
            ),
        )
        raise FormalLargeQmapAttemptError("timeout", attempt, message)
    if termination_status == "oom":
        message = (
            f"formal M2 CLI exceeded RSS limit {int(rss_limit_bytes)} bytes"
        )
        _write_json_atomic(
            attempt / "manifest.json",
            _failure_manifest(
                base_manifest, status="oom", message=message,
                compiler_core_ns=None, exit_code=exit_code,
                compiler_cpu_ns=compiler_cpu_ns, peak_rss_bytes=peak_rss_bytes,
                compiler_process_wall_ns=compiler_process_wall_ns,
                rss_limit_exceeded=True,
                end_to_end_ns=time.perf_counter_ns() - end_to_end_start,
            ),
        )
        raise FormalLargeQmapAttemptError("oom", attempt, message)
    if exit_code != 0:
        stderr_text = stderr_path.read_text(errors="replace").lower()
        oom_markers = (
            "bad_alloc", "cannot allocate memory", "out of memory",
            "memoryerror", "failed to map segment",
        )
        killed_for_memory = exit_code == -int(signal.SIGKILL)
        status = (
            "oom"
            if killed_for_memory
            or any(marker in stderr_text for marker in oom_markers)
            else "compiler_error"
        )
        message = f"formal M2 CLI exited with status {exit_code}"
        _write_json_atomic(
            attempt / "manifest.json",
            _failure_manifest(
                base_manifest, status=status, message=message,
                compiler_core_ns=None, exit_code=exit_code,
                compiler_cpu_ns=compiler_cpu_ns, peak_rss_bytes=peak_rss_bytes,
                compiler_process_wall_ns=compiler_process_wall_ns,
                rss_limit_exceeded=(status == "oom"),
                end_to_end_ns=time.perf_counter_ns() - end_to_end_start,
            ),
        )
        raise FormalLargeQmapAttemptError(status, attempt, message)

    compiler_core_ns: int | None = None
    compiler_statistics: Mapping[str, int] = {}
    try:
        if not native_path.is_file() or native_path.stat().st_size == 0:
            raise TraceValidationError("QMAP CLI produced no native NA program")
        if not metadata_path.is_file() or metadata_path.stat().st_size == 0:
            raise TraceValidationError("QMAP CLI produced no metadata JSONL")
        metadata_audit = dict(_audit_metadata_jsonl(metadata_path, schedule_audit))
        cli_summary = dict(
            _parse_cli_summary(stdout_path, schedule_audit, metadata_audit)
        )
        compiler_statistics = dict(cli_summary["statistics"])
        compiler_core_ns = int(compiler_statistics["totalTime"]) * 1000
        writer = _GzipJsonlWriter(canonical_path)
        try:
            pipeline = IncrementalTracePipeline(
                IncrementalTraceValidator(
                    schedule_audit.qubits,
                    expected_one_qubit_gates=schedule_audit.gates_1q,
                    expected_two_qubit_gates=schedule_audit.gates_2q,
                    expected_logical_ledger_sha256=(
                        schedule_audit.logical_ledger_sha256
                    ),
                    require_zero_ghost=True,
                    event_order="chronological",
                ),
                IncrementalTraceScorer(
                    schedule_audit.qubits, physical_model,
                    event_order="chronological",
                ),
                writer,
            )
            events = normalize_na_incrementally(
                native_path,
                architecture=converted_path,
                model=physical_model,
                gate_pairs=_iter_gate_pairs(
                    schedule_file, schedule_audit.two_qubit_layers,
                ),
            )
            for event in events:
                pipeline.consume(event)
            report = pipeline.finalize()
        finally:
            writer.close()

        validation = report["validation"]
        if (
            not validation["ok"]
            or int(validation["ghost_hits"]) != 0
            or int(validation["one_qubit_gates"]) != schedule_audit.gates_1q
            or int(validation["two_qubit_gates"]) != schedule_audit.gates_2q
            or validation["logical_ledger_sha256"]
            != schedule_audit.logical_ledger_sha256
        ):
            raise TraceValidationError("formal M2 strict validation summary is inconsistent")
        # Refuse any mutation while the CLI or independent scorer consumed the
        # inputs.  The final pass also covers the schedule-backed CZ iterator.
        post_hashes = {
            "schedule_file_sha256": _sha256_file(schedule_file),
            "architecture_source_sha256": _sha256_file(architecture_file),
            "config_file_sha256": _sha256_file(config_file),
            "cli_sha256": _sha256_file(cli_file),
        }
        for key, value in post_hashes.items():
            if value != input_hashes[key]:
                raise TraceValidationError(
                    f"formal M2 input changed during compilation/scoring: {key}"
                )
        post_provenance = dict(
            _verify_qmap_provenance(qmap_source, cli_file, config_file)
        )
        if post_provenance != provenance:
            raise TraceValidationError(
                "QMAP bundle/source/CLI provenance changed during formal M2"
            )
        post_orchestrator_git = dict(_git_state())
        if post_orchestrator_git != orchestrator_git:
            raise TraceValidationError(
                "feature worktree HEAD/dirty state changed during formal M2"
            )
        if not development_prefix and post_orchestrator_git["dirty"]:
            raise TraceValidationError(
                "formal full M2 ended with a dirty feature worktree"
            )
    except BaseException as error:
        message = f"{type(error).__name__}: {error}"
        _write_json_atomic(
            attempt / "manifest.json",
            _failure_manifest(
                base_manifest, status="verifier_fail", message=message,
                compiler_core_ns=compiler_core_ns, exit_code=exit_code,
                compiler_cpu_ns=compiler_cpu_ns, peak_rss_bytes=peak_rss_bytes,
                compiler_process_wall_ns=compiler_process_wall_ns,
                end_to_end_ns=time.perf_counter_ns() - end_to_end_start,
            ),
        )
        raise FormalLargeQmapAttemptError("verifier_fail", attempt, message) from error

    full_circuit = not bool(development_prefix)
    provenance_ok = bool(provenance["ok"] and post_provenance["ok"])
    support_claim_eligible = bool(
        full_circuit
        and provenance_ok
        and not post_orchestrator_git["dirty"]
        and peak_rss_bytes is not None
        and peak_rss_bytes <= int(rss_limit_bytes)
        and validation["ok"]
        and int(validation["ghost_hits"]) == 0
        and int(validation["one_qubit_gates"]) == schedule_audit.gates_1q
        and int(validation["two_qubit_gates"]) == schedule_audit.gates_2q
        and validation["logical_ledger_sha256"]
        == schedule_audit.logical_ledger_sha256
    )
    end_to_end_ns = time.perf_counter_ns() - end_to_end_start
    result = FormalLargeQmapResult(
        method="M2",
        status="success",
        two_qubit_layers_completed=schedule_audit.two_qubit_layers,
        two_qubit_layers_total=schedule_audit.two_qubit_layers,
        full_circuit=full_circuit,
        provenance_ok=provenance_ok,
        support_claim_eligible=support_claim_eligible,
        compiler_core_ns=int(compiler_core_ns),
        compiler_process_wall_ns=compiler_process_wall_ns,
        compiler_statistics=compiler_statistics,
        compiler_cpu_ns=compiler_cpu_ns,
        end_to_end_ns=end_to_end_ns,
        peak_rss_bytes=peak_rss_bytes,
        rss_limit_bytes=int(rss_limit_bytes),
        rss_limit_exceeded=False,
        native_chunks=int(metadata_audit["chunks"]),
        native_operations=int(metadata_audit["native_operations"]),
        canonical_events=writer.count,
        native_sha256=_sha256_file(native_path),
        metadata_sha256=_sha256_file(metadata_path),
        canonical_gzip_sha256=_sha256_file(canonical_path),
        canonical_chain_sha256=writer.chain_sha256,
        fidelity=report["fidelity"],
        validation=validation,
        output_directory=str(destination),
    )
    manifest = {
        **base_manifest,
        "status": "success",
        "provenance_ok": provenance_ok,
        "orchestrator_git_end": post_orchestrator_git,
        "qmap_provenance_end": post_provenance,
        "cli_summary": cli_summary,
        "metadata_audit": metadata_audit,
        "artifact_hashes": {
            "native_sha256": result.native_sha256,
            "metadata_sha256": result.metadata_sha256,
            "canonical_gzip_sha256": result.canonical_gzip_sha256,
            "canonical_chain_sha256": result.canonical_chain_sha256,
        },
        "result": result.to_dict(),
    }
    _write_json_atomic(attempt / "manifest.json", manifest)
    os.replace(attempt, destination)
    return result


__all__ = [
    "FormalLargeQmapAttemptError",
    "FormalLargeQmapResult",
    "QMAP_32_FROZEN_STREAM_CONFIG",
    "QmapScheduleAudit",
    "audit_qmap_schedule",
    "compile_formal_large_qmap",
]
