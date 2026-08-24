"""Atomic, identity-bound checkpoints for the formal Large ZAC pipeline.

Checkpoints are legal only after placement and native routing have both
completed the same prefix ``[0, next_layer)``.  The single-file JSON envelope
contains a versioned trusted-local pickle payload, its SHA256, the immutable
experiment identity, and an independently recomputed explicit state summary.
No SQLite connection, architecture instance, or output file handle is pickled.
"""

from __future__ import annotations

import base64
from collections import OrderedDict
from collections.abc import Mapping as MappingABC
from copy import deepcopy
from dataclasses import dataclass, fields, is_dataclass
import hashlib
import json
import os
from pathlib import Path
import pickle
import re
import time
from typing import Any, Callable, Iterable, Mapping
import uuid

from .formal_zac_placement import FormalZacPlacementStream
from .qasm_sqlite import LayerStore
from .zac_route_transition import ZACRouteTransitionDriver


CHECKPOINT_ENVELOPE_FORMAT_V1 = "formal-zac-checkpoint-envelope-v1"
CHECKPOINT_PAYLOAD_FORMAT_V1 = "formal-zac-checkpoint-payload-v1"
CHECKPOINT_ENVELOPE_FORMAT = "formal-zac-checkpoint-envelope-v2"
CHECKPOINT_PAYLOAD_FORMAT = "formal-zac-checkpoint-payload-v2"
ROLLING_OUTPUT_FORMAT = "formal-zac-output-chain-v1"
_PAYLOAD_CODEC = "pickle-protocol-5-base64"
_PICKLE_PROTOCOL = 5
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class FormalZacCheckpointIdentity:
    """Immutable evidence identity that must match exactly on restore."""

    git_commit: str
    input_sha256: str
    config_sha256: str
    architecture_sha256: str

    def __post_init__(self) -> None:
        if not self.git_commit or any(ch.isspace() for ch in self.git_commit):
            raise ValueError("checkpoint git commit must be a non-empty token")
        for name in ("input_sha256", "config_sha256", "architecture_sha256"):
            value = getattr(self, name)
            if _SHA256.fullmatch(value) is None:
                raise ValueError(f"checkpoint {name} must be lowercase SHA256")

    def as_dict(self) -> dict[str, str]:
        return {
            "git_commit": self.git_commit,
            "input_sha256": self.input_sha256,
            "config_sha256": self.config_sha256,
            "architecture_sha256": self.architecture_sha256,
        }

    @classmethod
    def coerce(
        cls, value: "FormalZacCheckpointIdentity | Mapping[str, str]",
    ) -> "FormalZacCheckpointIdentity":
        if isinstance(value, cls):
            return value
        return cls(
            git_commit=str(value["git_commit"]),
            input_sha256=str(value["input_sha256"]),
            config_sha256=str(value["config_sha256"]),
            architecture_sha256=str(value["architecture_sha256"]),
        )


class RollingInstructionHash:
    """Checkpointable chain hash over published native instruction dicts.

    A bare SHA256 digest cannot resume incremental hashing without serialising
    implementation-private hash state.  This explicit chain instead computes
    ``H_i = SHA256(H_(i-1) || length || canonical_json(record_i))``.  Its 32-byte
    digest and count are sufficient to resume on every supported Python build.
    """

    def __init__(self) -> None:
        self._digest = hashlib.sha256(ROLLING_OUTPUT_FORMAT.encode()).digest()
        self._count = 0

    @property
    def instruction_count(self) -> int:
        return self._count

    @property
    def hexdigest(self) -> str:
        return self._digest.hex()

    def update(self, instruction: Mapping[str, Any]) -> None:
        encoded = json.dumps(
            instruction,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        self._digest = hashlib.sha256(
            self._digest + len(encoded).to_bytes(8, "big") + encoded).digest()
        self._count += 1

    def update_many(self, instructions: Iterable[Mapping[str, Any]]) -> None:
        for instruction in instructions:
            self.update(instruction)

    def state_dict(self) -> dict[str, Any]:
        return {
            "format": ROLLING_OUTPUT_FORMAT,
            "instruction_count": self._count,
            "sha256": self.hexdigest,
        }

    @classmethod
    def from_state(cls, state: Mapping[str, Any]) -> "RollingInstructionHash":
        if state.get("format") != ROLLING_OUTPUT_FORMAT:
            raise ValueError("unsupported formal ZAC rolling-output state")
        count = int(state["instruction_count"])
        digest = str(state["sha256"])
        if count < 0 or _SHA256.fullmatch(digest) is None:
            raise ValueError("invalid formal ZAC rolling-output state")
        value = cls()
        value._count = count
        value._digest = bytes.fromhex(digest)
        return value


class CheckpointCadence:
    """Trigger an atomic checkpoint every 1000 stages or five minutes."""

    def __init__(
        self,
        *,
        every_stages: int = 1000,
        every_seconds: float = 300.0,
        completed_stages: int = 0,
        monotonic_ns: int | None = None,
    ):
        if int(every_stages) <= 0 or float(every_seconds) <= 0:
            raise ValueError("checkpoint cadence intervals must be positive")
        self.every_stages = int(every_stages)
        self.every_ns = int(float(every_seconds) * 1_000_000_000)
        self.last_completed_stages = int(completed_stages)
        self.last_monotonic_ns = (
            time.monotonic_ns() if monotonic_ns is None else int(monotonic_ns))

    def due(self, completed_stages: int, *, monotonic_ns: int | None = None) -> bool:
        completed = int(completed_stages)
        now = time.monotonic_ns() if monotonic_ns is None else int(monotonic_ns)
        if completed < self.last_completed_stages:
            raise ValueError("checkpoint cadence cannot move backwards")
        return (
            completed - self.last_completed_stages >= self.every_stages or
            now - self.last_monotonic_ns >= self.every_ns
        )

    def mark_saved(
        self, completed_stages: int, *, monotonic_ns: int | None = None,
    ) -> None:
        completed = int(completed_stages)
        now = time.monotonic_ns() if monotonic_ns is None else int(monotonic_ns)
        if completed < self.last_completed_stages:
            raise ValueError("checkpoint cadence cannot move backwards")
        self.last_completed_stages = completed
        self.last_monotonic_ns = now


@dataclass(frozen=True)
class LoadedFormalZacCheckpoint:
    identity: FormalZacCheckpointIdentity
    method: str
    next_layer: int
    placement_state: Mapping[str, Any]
    route_state: Mapping[str, Any]
    output_state: Mapping[str, Any]
    counters: Mapping[str, Any]
    summary: Mapping[str, Any]


@dataclass(frozen=True)
class FormalZacResume:
    placement: FormalZacPlacementStream
    route: ZACRouteTransitionDriver
    output_hash: RollingInstructionHash
    counters: Mapping[str, Any]
    summary: Mapping[str, Any]

    @property
    def next_layer(self) -> int:
        return self.placement.next_layer


def _canonical_state(value: Any) -> Any:
    """Make state content hashable independent of pickle memo/alias layout."""
    if value is None:
        return ["none"]
    if isinstance(value, bool):
        return ["bool", value]
    if isinstance(value, int):
        return ["int", str(value)]
    if isinstance(value, float):
        return ["float", value.hex()]
    if isinstance(value, str):
        return ["str", value]
    if isinstance(value, bytes):
        return ["bytes", base64.b64encode(value).decode("ascii")]
    if is_dataclass(value) and not isinstance(value, type):
        return [
            "dataclass",
            f"{type(value).__module__}.{type(value).__qualname__}",
            [[field.name, _canonical_state(getattr(value, field.name))]
             for field in fields(value)],
        ]
    if isinstance(value, OrderedDict):
        return ["ordered_dict", [
            [_canonical_state(key), _canonical_state(item)]
            for key, item in value.items()
        ]]
    if isinstance(value, MappingABC):
        items = [
            (_canonical_state(key), _canonical_state(item))
            for key, item in value.items()
        ]
        items.sort(key=lambda pair: json.dumps(
            pair[0], sort_keys=True, separators=(",", ":")))
        return ["dict", [[key, item] for key, item in items]]
    if isinstance(value, tuple):
        return ["tuple", [_canonical_state(item) for item in value]]
    if isinstance(value, list):
        return ["list", [_canonical_state(item) for item in value]]
    if isinstance(value, (set, frozenset)):
        items = [_canonical_state(item) for item in value]
        items.sort(key=lambda item: json.dumps(
            item, sort_keys=True, separators=(",", ":")))
        return ["set", items]
    raise TypeError(
        f"unsupported checkpoint summary state type: {type(value)!r}")


class _CanonicalHashWriter:
    """Hash the legacy canonical JSON without materialising its object tree.

    Version-1 checkpoints stored hashes of ``_canonical_state(value)``.  The
    tagged tree can be close to a gigabyte for the bounded resident LRUs even
    when its pickle is only a few megabytes.  This writer emits exactly the
    same compact JSON byte stream incrementally.  It is retained solely so old
    checkpoints remain fail-closed and low-memory readable.
    """

    _FLUSH_BYTES = 256 * 1024

    def __init__(self) -> None:
        self._digest = hashlib.sha256()
        self._buffer = bytearray()

    def write(self, value: str | bytes) -> None:
        encoded = value.encode("utf-8") if isinstance(value, str) else value
        self._buffer.extend(encoded)
        if len(self._buffer) >= self._FLUSH_BYTES:
            self._flush()

    def _flush(self) -> None:
        if self._buffer:
            self._digest.update(self._buffer)
            self._buffer.clear()

    def json_scalar(self, value: Any) -> None:
        self.write(json.dumps(
            value, ensure_ascii=True, separators=(",", ":"), allow_nan=False,
        ))

    def array(self, emitters: Iterable[Callable[[], None]]) -> None:
        self.write("[")
        for index, emit in enumerate(emitters):
            if index:
                self.write(",")
            emit()
        self.write("]")

    @staticmethod
    def _sort_key(value: Any) -> str:
        # Only mapping keys and set members are materialised.  They are tiny in
        # the formal state; the large cache values remain fully streaming.
        return json.dumps(
            _canonical_state(value), sort_keys=True, separators=(",", ":"),
        )

    def emit(self, value: Any) -> None:
        if value is None:
            self.array((lambda: self.json_scalar("none"),))
            return
        if isinstance(value, bool):
            self.array((
                lambda: self.json_scalar("bool"),
                lambda: self.json_scalar(value),
            ))
            return
        if isinstance(value, int):
            self.array((
                lambda: self.json_scalar("int"),
                lambda: self.json_scalar(str(value)),
            ))
            return
        if isinstance(value, float):
            self.array((
                lambda: self.json_scalar("float"),
                lambda: self.json_scalar(value.hex()),
            ))
            return
        if isinstance(value, str):
            self.array((
                lambda: self.json_scalar("str"),
                lambda: self.json_scalar(value),
            ))
            return
        if isinstance(value, bytes):
            encoded = base64.b64encode(value).decode("ascii")
            self.array((
                lambda: self.json_scalar("bytes"),
                lambda: self.json_scalar(encoded),
            ))
            return
        if is_dataclass(value) and not isinstance(value, type):
            def emit_fields() -> None:
                self.write("[")
                for index, field in enumerate(fields(value)):
                    if index:
                        self.write(",")
                    self.array((
                        lambda field=field: self.json_scalar(field.name),
                        lambda field=field: self.emit(
                            getattr(value, field.name)),
                    ))
                self.write("]")

            self.array((
                lambda: self.json_scalar("dataclass"),
                lambda: self.json_scalar(
                    f"{type(value).__module__}.{type(value).__qualname__}"),
                emit_fields,
            ))
            return
        if isinstance(value, OrderedDict):
            def emit_items() -> None:
                self.write("[")
                for index, (key, item) in enumerate(value.items()):
                    if index:
                        self.write(",")
                    self.array((
                        lambda key=key: self.emit(key),
                        lambda item=item: self.emit(item),
                    ))
                self.write("]")

            self.array((
                lambda: self.json_scalar("ordered_dict"), emit_items,
            ))
            return
        if isinstance(value, MappingABC):
            ordered = sorted(
                value.items(), key=lambda item: self._sort_key(item[0]))

            def emit_items() -> None:
                self.write("[")
                for index, (key, item) in enumerate(ordered):
                    if index:
                        self.write(",")
                    self.array((
                        lambda key=key: self.emit(key),
                        lambda item=item: self.emit(item),
                    ))
                self.write("]")

            self.array((lambda: self.json_scalar("dict"), emit_items))
            return
        if isinstance(value, tuple):
            self.array((
                lambda: self.json_scalar("tuple"),
                lambda: self._emit_sequence(value),
            ))
            return
        if isinstance(value, list):
            self.array((
                lambda: self.json_scalar("list"),
                lambda: self._emit_sequence(value),
            ))
            return
        if isinstance(value, (set, frozenset)):
            ordered = sorted(value, key=self._sort_key)
            self.array((
                lambda: self.json_scalar("set"),
                lambda: self._emit_sequence(ordered),
            ))
            return
        raise TypeError(
            f"unsupported checkpoint summary state type: {type(value)!r}")

    def _emit_sequence(self, values: Iterable[Any]) -> None:
        self.write("[")
        for index, value in enumerate(values):
            if index:
                self.write(",")
            self.emit(value)
        self.write("]")

    def hexdigest(self) -> str:
        self._flush()
        return self._digest.hexdigest()


def _content_hash(value: Any) -> str:
    writer = _CanonicalHashWriter()
    writer.emit(value)
    return writer.hexdigest()


def _json_hash(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _cache_summary(resident_state: Mapping[str, Any] | None) -> tuple[dict, dict]:
    names = (
        "transition_cache", "phase_cost_cache", "return_candidate_cache",
        "rollout_pair_cache", "rollout_site_cache",
    )
    if resident_state is None:
        return ({name: 0 for name in names}, {})
    caches = resident_state["caches"]
    return (
        {name: len(caches[name]) for name in names},
        {str(k): int(v) for k, v in resident_state["cache_stats"].items()},
    )


def _summarize_payload(
    payload: Mapping[str, Any],
    *,
    state_hash: Callable[[Any], str],
    state_hash_codec: str | None = None,
    include_large_state_hashes: bool = True,
) -> dict[str, Any]:
    placement = payload["placement_state"]
    route = payload["route_state"]
    output = RollingInstructionHash.from_state(payload["output_state"])
    next_layer = int(placement["next_layer"])
    if next_layer != int(route["next_layer"]):
        raise ValueError("placement/router checkpoint layers differ")
    placement_boundary = tuple(tuple(v) for v in placement["resident_boundary"])
    route_boundary = tuple(tuple(v) for v in route["current_boundary"])
    if placement_boundary != route_boundary:
        raise ValueError("placement/router checkpoint boundaries differ")

    instruction_state = route["instructions"]
    next_instruction_id = int(instruction_state["next_id"])
    if output.instruction_count != next_instruction_id:
        raise ValueError(
            "rolling output count differs from global native instruction id")
    scheduler = route.get("scheduler")
    if not isinstance(scheduler, MappingABC):
        raise ValueError("route checkpoint lacks scheduler-ledger snapshot")
    scheduler_count = int(scheduler["timing_count"])
    scheduler_next_id = int(scheduler["next_instruction_id"])
    scheduler_sha256 = str(scheduler["timing_sha256"])
    if (scheduler_count != scheduler_next_id
            or scheduler_count != next_instruction_id):
        raise ValueError(
            "scheduler/native/output instruction counts differ")
    if _SHA256.fullmatch(scheduler_sha256) is None:
        raise ValueError("route checkpoint scheduler timing hash is invalid")
    resident = placement.get("resident_state")
    cache_sizes, cache_stats = _cache_summary(resident)
    if resident is None:
        resident_atoms = None
        storage_atoms = None
        rng_sha256 = None
        safety_rng_sha256 = None
        commitment_count = 0
        placement_scheduler_sha256 = None
    else:
        registry = resident["registry"]
        resident_atoms = len(registry["zone_seat"])
        storage_atoms = len(registry["storage_site"])
        rng_sha256 = state_hash(resident["rng_state"])
        safety_rng_sha256 = state_hash(resident["safety_rng_state"])
        commitment_count = len(resident["residency_commitments"])
        placement_scheduler = resident["scheduler_reference"]["driver"][
            "scheduler"]
        placement_scheduler_sha256 = str(
            placement_scheduler["timing_sha256"])
        if (placement_scheduler_sha256 != scheduler_sha256
                or int(placement_scheduler["timing_count"])
                != scheduler_count):
            raise ValueError(
                "placement/router scheduler checkpoints differ")

    dependencies = route["dependencies"]
    global_1q = dependencies.get("global_1q")
    provider = placement["provider"]
    summary = {
        "method": str(payload["method"]),
        "next_physical_stage": next_layer,
        "physical_stage_count": int(placement["stage_count"]),
        "current_boundary_sha256": _json_hash(placement_boundary),
        "m1_state_sha256": (
            None if placement.get("m1_state") is None
            else state_hash(placement["m1_state"])),
        "resident_atoms": resident_atoms,
        "storage_atoms": storage_atoms,
        "rng_state_sha256": rng_sha256,
        "safety_rng_state_sha256": safety_rng_sha256,
        "residency_commitment_count": commitment_count,
        "cache_sizes": cache_sizes,
        "cache_stats": cache_stats,
        "provider_resume_stage": int(provider["resume_stage"]),
        "provider_cache_limit": int(provider["max_cached_stages"]),
        "provider_cached_stages": [int(v) for v in provider["cached_stages"]],
        "next_native_instruction_id": next_instruction_id,
        "active_instruction_count": len(instruction_state["records"]),
        "peak_active_instruction_count": int(
            instruction_state["peak_active_count"]),
        "dependency_sizes": {
            "qubit": len(dependencies["qubit"]),
            "site": len(dependencies["site"]),
            "aod": len(dependencies["aod"]),
            "aod_end_time": len(dependencies["aod_end_time"]),
            "rydberg": len(dependencies["rydberg"]),
            "global_1q": int(global_1q is not None),
        },
        "dependency_state_sha256": state_hash(dependencies),
        "route_runtime_us": float(route["runtime_us"]),
        "ghost_splits": int(route["ghost_splits"]),
        "output_instruction_count": output.instruction_count,
        "rolling_output_sha256": output.hexdigest,
        "scheduler_timing_count": scheduler_count,
        "scheduler_timing_sha256": scheduler_sha256,
        "placement_scheduler_timing_sha256": placement_scheduler_sha256,
        "scheduler_trace_end_us": float(scheduler["trace_end_us"]),
        "scheduler_active_union_sha256": _json_hash(
            scheduler["active_union_us"]),
        "counters_sha256": state_hash(payload["counters"]),
    }
    if include_large_state_hashes:
        summary["placement_state_sha256"] = state_hash(placement)
        summary["resident_state_sha256"] = (
            None if resident is None else state_hash(resident))
    else:
        # The outer payload SHA256 covers every byte of both states.  Repeating
        # their hashes through the legacy tagged-tree representation was the
        # source of the checkpoint's gigabyte-scale transient allocation.
        summary["state_integrity"] = "envelope-payload-sha256"
    if state_hash_codec is not None:
        summary["state_hash_codec"] = state_hash_codec
    if summary["provider_resume_stage"] != next_layer:
        raise ValueError("provider checkpoint boundary differs from compiler layer")
    if len(summary["provider_cached_stages"]) > summary["provider_cache_limit"]:
        raise ValueError("provider checkpoint cache exceeds its bound")
    return summary


def _summarize_payload_v1(payload: Mapping[str, Any]) -> dict[str, Any]:
    return _summarize_payload(payload, state_hash=_content_hash)


def _summarize_payload_v2(payload: Mapping[str, Any]) -> dict[str, Any]:
    # The envelope payload SHA256 is the authoritative all-state integrity
    # check.  Only bounded metadata keeps the legacy canonical diagnostics.
    return _summarize_payload(
        payload,
        state_hash=_content_hash,
        state_hash_codec="canonical-tagged-json-v1-small-state",
        include_large_state_hashes=False,
    )


def _payload(
    *,
    identity: FormalZacCheckpointIdentity,
    placement: FormalZacPlacementStream,
    route: ZACRouteTransitionDriver,
    output_hash: RollingInstructionHash,
    counters: Mapping[str, Any] | None,
    payload_format: str = CHECKPOINT_PAYLOAD_FORMAT,
) -> dict[str, Any]:
    method = placement.method
    expected_placer = "zac" if method == "M1" else "resident"
    if route.placer_kind != expected_placer:
        raise ValueError("placement method/router kind mismatch")
    if payload_format not in {
        CHECKPOINT_PAYLOAD_FORMAT_V1, CHECKPOINT_PAYLOAD_FORMAT,
    }:
        raise ValueError("unsupported formal ZAC checkpoint payload format")
    is_v2 = payload_format == CHECKPOINT_PAYLOAD_FORMAT
    value: dict[str, Any] = {
        "format": payload_format,
        "identity": identity.as_dict(),
        "method": method,
        "placement_state": placement.state_dict(borrow_caches=is_v2),
        "route_state": route.state_dict(),
        "output_state": output_hash.state_dict(),
        "counters": (
            dict(counters or {}) if is_v2
            else deepcopy(dict(counters or {}))
        ),
    }
    value["summary"] = (
        _summarize_payload_v2(value) if is_v2
        else _summarize_payload_v1(value)
    )
    return value


class FormalZacCheckpointManager:
    """Save, validate, and restore trusted-local formal ZAC checkpoints."""

    @staticmethod
    def save(
        path: str | Path,
        *,
        identity: FormalZacCheckpointIdentity | Mapping[str, str],
        placement: FormalZacPlacementStream,
        route: ZACRouteTransitionDriver,
        output_hash: RollingInstructionHash,
        counters: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        destination = Path(path).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        frozen_identity = FormalZacCheckpointIdentity.coerce(identity)
        payload = _payload(
            identity=frozen_identity,
            placement=placement,
            route=route,
            output_hash=output_hash,
            counters=counters,
        )
        raw_payload = pickle.dumps(payload, protocol=_PICKLE_PROTOCOL)
        envelope = {
            "format": CHECKPOINT_ENVELOPE_FORMAT,
            "payload_codec": _PAYLOAD_CODEC,
            "payload_size": len(raw_payload),
            "payload_sha256": hashlib.sha256(raw_payload).hexdigest(),
            "identity": frozen_identity.as_dict(),
            "summary": payload["summary"],
            "payload_b64": base64.b64encode(raw_payload).decode("ascii"),
        }
        temporary = destination.with_name(
            f".{destination.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            # json.dump avoids allocating a second checkpoint-sized encoded
            # byte string on top of the raw pickle and base64 payload.
            with open(temporary, "x", encoding="utf-8", newline="\n") as handle:
                json.dump(
                    envelope, handle, sort_keys=True, separators=(",", ":"),
                    ensure_ascii=True, allow_nan=False,
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            try:
                directory_fd = os.open(destination.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                # Some filesystems do not permit directory fsync; the atomic
                # same-directory replace remains the core safety guarantee.
                pass
        finally:
            if temporary.exists():
                temporary.unlink()
        return deepcopy(payload["summary"])

    @staticmethod
    def load(
        path: str | Path,
        *,
        expected_identity: FormalZacCheckpointIdentity | Mapping[str, str],
    ) -> LoadedFormalZacCheckpoint:
        # Pickle is intentionally restricted to locally produced trusted files.
        source = Path(path).resolve()
        try:
            with open(source, encoding="utf-8") as handle:
                envelope = json.load(handle)
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError("invalid formal ZAC checkpoint envelope") from error
        if not isinstance(envelope, dict):
            raise ValueError("invalid formal ZAC checkpoint envelope")
        required = {
            "format", "payload_codec", "payload_size", "payload_sha256",
            "identity", "summary", "payload_b64",
        }
        if set(envelope) != required:
            raise ValueError("formal ZAC checkpoint envelope fields differ")
        envelope_format = envelope["format"]
        if (envelope_format not in {
                CHECKPOINT_ENVELOPE_FORMAT_V1, CHECKPOINT_ENVELOPE_FORMAT} or
                envelope["payload_codec"] != _PAYLOAD_CODEC):
            raise ValueError("unsupported formal ZAC checkpoint envelope")
        payload_b64 = envelope.pop("payload_b64")
        try:
            raw_payload = base64.b64decode(payload_b64, validate=True)
        except (ValueError, TypeError) as error:
            raise ValueError("invalid formal ZAC checkpoint payload encoding") from error
        del payload_b64
        if len(raw_payload) != int(envelope["payload_size"]):
            raise ValueError("formal ZAC checkpoint payload size mismatch")
        if hashlib.sha256(raw_payload).hexdigest() != envelope["payload_sha256"]:
            raise ValueError("formal ZAC checkpoint payload SHA256 mismatch")
        try:
            payload = pickle.loads(raw_payload)
        except Exception as error:
            raise ValueError("invalid trusted-local formal ZAC payload") from error
        del raw_payload
        expected_payload_format = (
            CHECKPOINT_PAYLOAD_FORMAT_V1
            if envelope_format == CHECKPOINT_ENVELOPE_FORMAT_V1
            else CHECKPOINT_PAYLOAD_FORMAT
        )
        if (not isinstance(payload, dict) or
                payload.get("format") != expected_payload_format):
            raise ValueError("unsupported formal ZAC checkpoint payload")
        identity = FormalZacCheckpointIdentity.coerce(payload["identity"])
        expected = FormalZacCheckpointIdentity.coerce(expected_identity)
        if identity != expected:
            raise ValueError("checkpoint commit/input/config/architecture mismatch")
        if envelope["identity"] != identity.as_dict():
            raise ValueError("checkpoint envelope/payload identity mismatch")
        actual_summary = (
            _summarize_payload_v1(payload)
            if expected_payload_format == CHECKPOINT_PAYLOAD_FORMAT_V1
            else _summarize_payload_v2(payload)
        )
        if payload.get("summary") != actual_summary:
            raise ValueError("checkpoint payload state summary mismatch")
        if envelope["summary"] != actual_summary:
            raise ValueError("checkpoint envelope state summary mismatch")
        method = str(payload["method"])
        if method not in {"M1", "M3", "M4"}:
            raise ValueError("checkpoint formal method is invalid")
        return LoadedFormalZacCheckpoint(
            identity=identity,
            method=method,
            next_layer=int(actual_summary["next_physical_stage"]),
            # ``payload`` is a fresh unpickled graph with no external owner.
            # Returning its sections directly avoids a second resident-cache
            # graph; restore may transfer those OrderedDicts into the placer.
            placement_state=payload["placement_state"],
            route_state=payload["route_state"],
            output_state=payload["output_state"],
            counters=payload["counters"],
            summary=actual_summary,
        )

    @staticmethod
    def restore(
        path: str | Path,
        *,
        expected_identity: FormalZacCheckpointIdentity | Mapping[str, str],
        architecture: Any,
        initial_mapping,
        store: LayerStore,
        max_gates_per_stage: int,
        setting: Mapping[str, Any] | None = None,
    ) -> FormalZacResume:
        loaded = FormalZacCheckpointManager.load(
            path, expected_identity=expected_identity)
        placement = FormalZacPlacementStream.from_state(
            architecture=architecture,
            initial_mapping=initial_mapping,
            store=store,
            max_gates_per_stage=max_gates_per_stage,
            setting=setting,
            state=loaded.placement_state,
            take_cache_ownership=True,
        )
        route = ZACRouteTransitionDriver.from_state(
            architecture, initial_mapping, dict(loaded.route_state))
        if placement.next_layer != route.next_layer:
            raise ValueError("restored placement/router layers differ")
        if placement._resident_boundary != route.current_boundary:
            raise ValueError("restored placement/router boundaries differ")
        output_hash = RollingInstructionHash.from_state(loaded.output_state)
        if output_hash.instruction_count != len(route.instructions):
            raise ValueError("restored output/global instruction counts differ")
        return FormalZacResume(
            placement=placement,
            route=route,
            output_hash=output_hash,
            counters=loaded.counters,
            summary=loaded.summary,
        )


save_formal_zac_checkpoint = FormalZacCheckpointManager.save
load_formal_zac_checkpoint = FormalZacCheckpointManager.load
restore_formal_zac_checkpoint = FormalZacCheckpointManager.restore


__all__ = [
    "CheckpointCadence",
    "FormalZacCheckpointIdentity",
    "FormalZacCheckpointManager",
    "FormalZacResume",
    "LoadedFormalZacCheckpoint",
    "RollingInstructionHash",
    "load_formal_zac_checkpoint",
    "restore_formal_zac_checkpoint",
    "save_formal_zac_checkpoint",
]
