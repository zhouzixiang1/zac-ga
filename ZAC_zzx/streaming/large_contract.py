"""Frozen provenance and resource contract for the seven-circuit Large suite."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping


QASMBENCH_REPOSITORY = "https://github.com/pnnl/QASMBench.git"
QASMBENCH_COMMIT = "357b942396d5c2b7cbc1c229c585a6ef5ccaebac"
BWT_LADDER = ("bwt_n37", "bwt_n57", "bwt_n97", "bwt_n177")
STRUCTURAL_REPRESENTATIVES = ("ising_n420", "qft_n320", "multiplier_n400")
LARGE_CIRCUITS = BWT_LADDER + STRUCTURAL_REPRESENTATIVES
LARGE_METHODS = ("M1", "M2", "M3", "M4")
LARGE_RSS_LIMIT_BYTES = 22 * (1 << 30)
LARGE_TIMEOUT_SECONDS = 24 * 60 * 60
CHECKPOINT_LAYER_INTERVAL = 1_000
CHECKPOINT_TIME_INTERVAL_SECONDS = 300.0
STREAMING_COMPILER_INTEGRATED = False
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


# The gate counts below are the exact values published in the QASMBench README
# Large-Circuits table at the frozen commit.  The depth came from the user plan,
# not that upstream table, so it is deliberately non-gating and cannot be cited
# as official QASMBench metadata.
OFFICIAL_METADATA: Mapping[str, Mapping[str, Any]] = {
    "bwt_n177": {
        "qubits": 177,
        "total_gates": 12_049_201,
        "cnot_gates": 4_641_200,
        "source": "QASMBench README Large-Circuits table at the frozen commit",
        "source_url": (
            "https://github.com/pnnl/QASMBench/blob/"
            "357b942396d5c2b7cbc1c229c585a6ef5ccaebac/README.md"
        ),
        "depth_user_plan_approx": 1_360_000,
        "depth_provenance": "user_plan_approx",
        "depth_is_acceptance_gate": False,
    }
}


def _stable_hash(value: Any) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class LargeExperimentContract:
    experiment_schema: int = 2
    format: str = "zac-large-contract-v1"
    upstream_repository: str = QASMBENCH_REPOSITORY
    upstream_commit: str = QASMBENCH_COMMIT
    circuits: tuple[str, ...] = LARGE_CIRCUITS
    methods: tuple[str, ...] = LARGE_METHODS
    canonical_policy: str = (
        "deterministic_standard_gate_expansion;no_cross_gate_optimization;shared_hash"
    )
    rss_limit_bytes: int = LARGE_RSS_LIMIT_BYTES
    timeout_seconds: int = LARGE_TIMEOUT_SECONDS
    checkpoint_layer_interval: int = CHECKPOINT_LAYER_INTERVAL
    checkpoint_time_interval_seconds: float = CHECKPOINT_TIME_INTERVAL_SECONDS
    event_format: str = "canonical-trace-jsonl-gzip-v1"
    # This field describes the executable end-to-end compiler adapter, not the
    # standalone SQLite/checkpoint/incremental-scoring primitives.  It must stay
    # false until all four methods generate and consume events incrementally.
    streaming_compiler_integrated: bool = STREAMING_COMPILER_INTEGRATED

    def validate(self, *, require_frozen: bool = True) -> None:
        if self.experiment_schema != 2 or self.format != "zac-large-contract-v1":
            raise ValueError("invalid Large experiment contract schema/format")
        if not re.fullmatch(r"[0-9a-f]{40}", self.upstream_commit):
            raise ValueError("Large upstream commit must be a full Git SHA")
        if len(set(self.circuits)) != len(self.circuits):
            raise ValueError("Large circuit ids must be unique")
        if len(set(self.methods)) != len(self.methods):
            raise ValueError("Large methods must be unique")
        if self.rss_limit_bytes <= 0 or self.timeout_seconds <= 0:
            raise ValueError("Large resource limits must be positive")
        if self.checkpoint_layer_interval <= 0 or self.checkpoint_time_interval_seconds <= 0:
            raise ValueError("Large checkpoint cadence must be positive")
        if not isinstance(self.streaming_compiler_integrated, bool):
            raise ValueError("streaming_compiler_integrated must be boolean")
        if require_frozen:
            expected = LargeExperimentContract()
            if self != expected:
                differences = {
                    item.name: (getattr(self, item.name), getattr(expected, item.name))
                    for item in dataclasses.fields(self)
                    if getattr(self, item.name) != getattr(expected, item.name)
                }
                raise ValueError(f"Large contract differs from frozen plan: {differences}")

    def validate_attempt_limits(self, *, rss_limit_bytes: int,
                                timeout_seconds: float) -> None:
        if int(rss_limit_bytes) != self.rss_limit_bytes:
            raise ValueError(
                f"Large RSS limit mismatch: {rss_limit_bytes} != {self.rss_limit_bytes}"
            )
        if float(timeout_seconds) != float(self.timeout_seconds):
            raise ValueError(
                f"Large timeout mismatch: {timeout_seconds} != {self.timeout_seconds}"
            )

    def require_streaming_execution(self) -> None:
        """Reject formal Large execution until the real adapter is connected."""

        self.validate()
        if not self.streaming_compiler_integrated:
            raise RuntimeError(
                "formal run-large is disabled: streaming_compiler_integrated=false; "
                "the bounded-memory compiler/event/scorer adapter is not implemented"
            )

    def to_dict(self) -> dict[str, Any]:
        value = dataclasses.asdict(self)
        value["circuits"] = list(self.circuits)
        value["methods"] = list(self.methods)
        value["official_metadata"] = {
            key: dict(metadata) for key, metadata in OFFICIAL_METADATA.items()
        }
        return value

    @property
    def sha256(self) -> str:
        # Official metadata is part of the frozen, auditable contract payload.
        return _stable_hash(self.to_dict())


@dataclass(frozen=True)
class LargeCircuitMetadata:
    circuit: str
    source_path: str
    source_sha256: str
    canonical_path: str
    canonical_sha256: str
    qubits: int
    gates_1q: int
    gates_2q: int
    layers: int
    statement_sha256: str = ""

    def validate(self) -> None:
        if self.circuit not in LARGE_CIRCUITS:
            raise ValueError(f"circuit is not in frozen Large suite: {self.circuit}")
        for name in ("source_sha256", "canonical_sha256"):
            if not _SHA256.fullmatch(getattr(self, name)):
                raise ValueError(f"invalid {name} for {self.circuit}")
        if self.statement_sha256 and not _SHA256.fullmatch(self.statement_sha256):
            raise ValueError(f"invalid statement_sha256 for {self.circuit}")
        if self.qubits <= 0 or min(self.gates_1q, self.gates_2q, self.layers) < 0:
            raise ValueError(f"invalid structural counts for {self.circuit}")
        if self.circuit == "bwt_n177" and self.qubits != 177:
            raise ValueError("bwt_n177 must contain exactly 177 qubits")

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class LargeSuiteMetadata:
    experiment_schema: int = 2
    format: str = "zac-large-suite-v1"
    contract_sha256: str = ""
    upstream_commit: str = QASMBENCH_COMMIT
    streaming_compiler_integrated: bool = STREAMING_COMPILER_INTEGRATED
    complete: bool = False
    circuits: tuple[LargeCircuitMetadata, ...] = field(default_factory=tuple)

    def validate(self, contract: LargeExperimentContract | None = None) -> None:
        contract = contract or LargeExperimentContract()
        contract.validate()
        if self.experiment_schema != 2 or self.format != "zac-large-suite-v1":
            raise ValueError("invalid Large suite metadata schema/format")
        if self.contract_sha256 != contract.sha256:
            raise ValueError("Large suite contract hash mismatch")
        if self.upstream_commit != contract.upstream_commit:
            raise ValueError("Large suite upstream commit mismatch")
        if self.streaming_compiler_integrated != contract.streaming_compiler_integrated:
            raise ValueError("Large suite streaming integration status mismatch")
        for record in self.circuits:
            record.validate()
        names = tuple(record.circuit for record in self.circuits)
        if len(set(names)) != len(names):
            raise ValueError("duplicate circuit in Large suite metadata")
        if any(name not in contract.circuits for name in names):
            raise ValueError("Large suite contains an unregistered circuit")
        if self.complete and set(names) != set(contract.circuits):
            missing = sorted(set(contract.circuits) - set(names))
            extra = sorted(set(names) - set(contract.circuits))
            raise ValueError(f"complete Large suite mismatch; missing={missing}, extra={extra}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "experiment_schema": self.experiment_schema,
            "format": self.format,
            "contract_sha256": self.contract_sha256,
            "upstream_commit": self.upstream_commit,
            "streaming_compiler_integrated": self.streaming_compiler_integrated,
            "complete": self.complete,
            "circuits": [record.to_dict() for record in self.circuits],
        }


def create_large_suite_metadata(
    records: Iterable[LargeCircuitMetadata], *, complete: bool = False,
    contract: LargeExperimentContract | None = None,
) -> LargeSuiteMetadata:
    contract = contract or LargeExperimentContract()
    contract.validate()
    metadata = LargeSuiteMetadata(
        contract_sha256=contract.sha256,
        upstream_commit=contract.upstream_commit,
        streaming_compiler_integrated=contract.streaming_compiler_integrated,
        complete=bool(complete),
        circuits=tuple(records),
    )
    metadata.validate(contract)
    return metadata


def write_large_suite_metadata(path: str | Path, metadata: LargeSuiteMetadata) -> None:
    metadata.validate()
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(metadata.to_dict(), handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, destination)


def load_large_suite_metadata(path: str | Path) -> LargeSuiteMetadata:
    with open(path, encoding="utf-8") as handle:
        value = json.load(handle)
    records = tuple(LargeCircuitMetadata(**row) for row in value.pop("circuits", []))
    metadata = LargeSuiteMetadata(circuits=records, **value)
    metadata.validate()
    return metadata


__all__ = [
    "BWT_LADDER",
    "CHECKPOINT_LAYER_INTERVAL",
    "CHECKPOINT_TIME_INTERVAL_SECONDS",
    "LARGE_CIRCUITS",
    "LARGE_METHODS",
    "LARGE_RSS_LIMIT_BYTES",
    "LARGE_TIMEOUT_SECONDS",
    "LargeCircuitMetadata",
    "LargeExperimentContract",
    "LargeSuiteMetadata",
    "OFFICIAL_METADATA",
    "QASMBENCH_COMMIT",
    "QASMBENCH_REPOSITORY",
    "STRUCTURAL_REPRESENTATIVES",
    "STREAMING_COMPILER_INTEGRATED",
    "create_large_suite_metadata",
    "load_large_suite_metadata",
    "write_large_suite_metadata",
]
