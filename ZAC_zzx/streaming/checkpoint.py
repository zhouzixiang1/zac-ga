"""Event-wise deterministic compressed output and atomic checkpoints."""

from __future__ import annotations

import base64
import dataclasses
import gzip
import hashlib
import json
import os
import pickle
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, Mapping, Optional


@dataclass
class Checkpoint:
    format: str = "zac-large-checkpoint-v1"
    current_layer: int = 0
    mapping: Dict[str, Any] = field(default_factory=dict)
    residents: Dict[str, Any] = field(default_factory=dict)
    dependencies: Dict[str, Any] = field(default_factory=dict)
    rng_state_b64: str = ""
    rolling_metrics: Dict[str, float] = field(default_factory=dict)
    event_count: int = 0
    event_hash: str = "0" * 64
    input_sha256: str = ""
    config_sha256: str = ""

    def capture_rng(self, rng: random.Random) -> None:
        self.rng_state_b64 = base64.b64encode(
            pickle.dumps(rng.getstate(), protocol=4)).decode("ascii")

    def restore_rng(self, rng: random.Random) -> None:
        if not self.rng_state_b64:
            raise ValueError("checkpoint has no RNG state")
        state = pickle.loads(base64.b64decode(self.rng_state_b64.encode("ascii")))
        rng.setstate(state)

    def validate(self) -> None:
        if self.format != "zac-large-checkpoint-v1":
            raise ValueError(f"unsupported checkpoint format: {self.format}")
        if self.current_layer < 0 or self.event_count < 0:
            raise ValueError("negative checkpoint index")
        if len(self.event_hash) != 64:
            raise ValueError("invalid rolling event hash")
        try:
            bytes.fromhex(self.event_hash)
        except ValueError as error:
            raise ValueError("invalid rolling event hash") from error


def save_checkpoint(path: str | Path, checkpoint: Checkpoint) -> None:
    checkpoint.validate()
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(dataclasses.asdict(checkpoint), handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, destination)


def load_checkpoint(path: str | Path, *, input_sha256: Optional[str] = None,
                    config_sha256: Optional[str] = None) -> Checkpoint:
    with open(path, encoding="utf-8") as handle:
        checkpoint = Checkpoint(**json.load(handle))
    checkpoint.validate()
    if input_sha256 is not None and checkpoint.input_sha256 != input_sha256:
        raise ValueError("checkpoint input hash mismatch")
    if config_sha256 is not None and checkpoint.config_sha256 != config_sha256:
        raise ValueError("checkpoint config hash mismatch")
    return checkpoint


class EventStreamWriter:
    """Write canonical JSONL into concatenable gzip members.

    The rolling chain hash is independent of gzip member boundaries, so a
    resumed stream and an uninterrupted stream are event-wise identical even
    though their compressed bytes need not match.
    """

    def __init__(self, path: str | Path, *, append: bool = False,
                 event_count: int = 0, event_hash: str = "0" * 64):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if event_count < 0:
            raise ValueError("event_count must be non-negative")
        if len(event_hash) != 64:
            raise ValueError("invalid rolling event hash")
        try:
            bytes.fromhex(event_hash)
        except ValueError as error:
            raise ValueError("invalid rolling event hash") from error
        self.event_count = event_count
        self.event_hash = event_hash
        mode = "ab" if append else "wb"
        self._raw = open(self.path, mode)
        self._gzip = gzip.GzipFile(fileobj=self._raw, mode="wb", mtime=0)

    def write(self, event: Mapping[str, Any]) -> None:
        line = json.dumps(event, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True).encode("utf-8") + b"\n"
        previous = bytes.fromhex(self.event_hash)
        self.event_hash = hashlib.sha256(previous + line).hexdigest()
        self.event_count += 1
        self._gzip.write(line)

    def flush(self) -> None:
        self._gzip.flush()
        self._raw.flush()
        os.fsync(self._raw.fileno())

    def close(self) -> None:
        if self._gzip is not None:
            self._gzip.close()
            self._gzip = None
            self._raw.close()

    def __enter__(self) -> "EventStreamWriter":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @staticmethod
    def read(path: str | Path) -> Iterator[Dict[str, Any]]:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    yield json.loads(line)


__all__ = ["Checkpoint", "EventStreamWriter", "load_checkpoint", "save_checkpoint"]
