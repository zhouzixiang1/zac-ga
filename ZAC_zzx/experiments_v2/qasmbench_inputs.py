"""Pinned QASMBench inventory and expansion-only canonical inputs.

This module deliberately keeps source discovery separate from experiment
execution.  The inventory is derived from the pinned Git tree (not an
untracked working-tree walk), and every selected source is bound to both its
upstream Git blob and its byte-level SHA-256 digest.

The canonicalizer is a two-pass streaming OpenQASM 2 expander.  It emits only
``cz,u1,u2,u3`` and expands every input statement independently, so no
cross-gate cancellation or optimisation can change the logical gate ledger.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence, TextIO

from .contracts import CanonicalCircuitManifest, SCHEMA_VERSION, sha256_file


QASMBENCH_REPOSITORY = "https://github.com/pnnl/QASMBench.git"
QASMBENCH_COMMIT = "357b942396d5c2b7cbc1c229c585a6ef5ccaebac"
QASMBENCH_CANONICAL_PROFILE = "qasmbench_standard_expand_v1"
QASMBENCH_CANONICALIZER_VERSION = "qasmbench-standard-expander-v1"
QASMBENCH_MANIFEST_SCHEMA = "qasmbench-source-manifest-v1"
QASMBENCH_SCALES = ("small", "medium", "large")
EXPECTED_DIRECTORY_COUNTS = {"small": 42, "medium": 25, "large": 70}
EXPECTED_SELECTED_QASM = 131
EXPECTED_NO_QASM_DIRECTORIES = frozenset({
    "large/QAOA_3SAT_N100_p1000",
    "large/QV_n1000",
    "large/quantum_telecloning_N1_M1000_LNN",
    "large/quantum_telecloning_N1_M1000_all_to_all",
    "large/quantum_telecloning_N1_M100_LNN",
    "large/quantum_telecloning_N1_M100_all_to_all",
})

_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_]*"
_QREG_DECLARATION = re.compile(
    rf"^qreg\s+({_IDENTIFIER})\[(\d+)\]\s*;$", re.IGNORECASE)
_CREG_DECLARATION = re.compile(
    rf"^creg\s+({_IDENTIFIER})\[(\d+)\]\s*;$", re.IGNORECASE)
_INDEXED_OPERAND = re.compile(
    rf"^({_IDENTIFIER})\[(\d+)\]$", re.IGNORECASE)
_REGISTER_OPERAND = re.compile(rf"^({_IDENTIFIER})$", re.IGNORECASE)
_GATE_STATEMENT = re.compile(
    rf"^({_IDENTIFIER})(?:\s*\((.*)\))?\s+(.+)\s*;$",
    re.IGNORECASE,
)

_ONE_QUBIT_NO_PARAMETER = frozenset({
    "id", "x", "y", "z", "h", "s", "sdg", "t", "tdg", "sx", "sxdg",
})
_ONE_QUBIT_ONE_PARAMETER = frozenset({"p", "rx", "ry", "rz", "u1"})
_ONE_QUBIT_TWO_PARAMETERS = frozenset({"u2"})
_ONE_QUBIT_THREE_PARAMETERS = frozenset({"u", "u3"})
_TWO_QUBIT_NO_PARAMETER = frozenset({"cx", "cz", "swap"})
_TWO_QUBIT_ONE_PARAMETER = frozenset({"cp", "rzz"})
_THREE_QUBIT_NO_PARAMETER = frozenset({"ccx", "cswap"})


class QASMBenchCanonicalError(ValueError):
    """A deterministic, source-independent canonicalisation failure."""


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _git(checkout: Path, *arguments: str) -> str:
    try:
        return subprocess.check_output(
            ["git", *arguments], cwd=checkout, text=True,
            stderr=subprocess.PIPE,
        ).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = ""
        if isinstance(exc, subprocess.CalledProcessError):
            detail = (exc.stderr or "").strip()
        raise ValueError(
            f"QASMBench checkout Git query failed: {' '.join(arguments)}"
            + (f": {detail}" if detail else "")
        ) from exc


def _git_names(
    checkout: Path,
    treeish: str,
    *,
    directories_only: bool = False,
) -> list[str]:
    command = ["git", "ls-tree", "-z", "--name-only"]
    if directories_only:
        command.append("-d")
    command.append(treeish)
    output = subprocess.check_output(
        command,
        cwd=checkout,
    )
    return sorted(
        item.decode("utf-8") for item in output.split(b"\0") if item)


@dataclass(frozen=True)
class QASMBenchInputEntry:
    """One official top-level benchmark directory and its chosen input."""

    benchmark_scale: str
    benchmark_directory: str
    upstream_path: str
    upstream_git_blob: str
    source_sha256: str
    selection_reason: str
    canonical_profile: str = QASMBENCH_CANONICAL_PROFILE
    status: str = "selected"
    canonical_path: str = ""
    canonical_sha256: str = ""
    qubits: int | None = None
    gates_1q: int | None = None
    gates_2q: int | None = None
    error: str = ""

    def validate(self) -> None:
        if self.benchmark_scale not in QASMBENCH_SCALES:
            raise ValueError(f"invalid QASMBench scale: {self.benchmark_scale!r}")
        if not self.benchmark_directory or "/" in self.benchmark_directory:
            raise ValueError("invalid QASMBench benchmark directory")
        expected_prefix = f"{self.benchmark_scale}/{self.benchmark_directory}"
        if self.upstream_path != expected_prefix and not self.upstream_path.startswith(
                expected_prefix + "/"):
            raise ValueError("QASMBench upstream path escapes its benchmark directory")
        if self.canonical_profile != QASMBENCH_CANONICAL_PROFILE:
            raise ValueError("wrong QASMBench canonical profile")
        if self.status not in {
                "selected", "success", "canonical_error", "no_qasm_source"}:
            raise ValueError(f"unknown QASMBench input status: {self.status}")
        allowed_reasons = {
            "preferred_transpiled", "preferred_named",
            "unique_transpiled_fallback", "sole_qasm", "no_qasm_source",
        }
        if self.selection_reason not in allowed_reasons:
            raise ValueError(
                f"unknown QASMBench input selection reason: {self.selection_reason}")

        if self.status == "no_qasm_source":
            if self.selection_reason != "no_qasm_source":
                raise ValueError("no-source entry has the wrong selection reason")
            if (self.upstream_git_blob or self.source_sha256
                    or self.canonical_path or self.canonical_sha256):
                raise ValueError("no-source entry may not claim source/canonical hashes")
            if any(value is not None for value in (
                    self.qubits, self.gates_1q, self.gates_2q)):
                raise ValueError("no-source entry may not claim gate counts")
            if self.error != "no_qasm_source":
                raise ValueError("no-source entry requires error=no_qasm_source")
            return

        if not _HEX40.fullmatch(self.upstream_git_blob):
            raise ValueError("selected QASMBench source has an invalid Git blob")
        if not _HEX64.fullmatch(self.source_sha256):
            raise ValueError("selected QASMBench source has an invalid SHA256")
        if self.selection_reason == "no_qasm_source":
            raise ValueError("selected source has no_qasm_source reason")

        if self.status == "success":
            if (not self.canonical_path
                    or not _HEX64.fullmatch(self.canonical_sha256)):
                raise ValueError("successful canonical input lacks its path/hash")
            if any(isinstance(value, bool) or not isinstance(value, int)
                   or value < 0 for value in (
                       self.qubits, self.gates_1q, self.gates_2q)):
                raise ValueError("successful canonical input has invalid gate counts")
            if not isinstance(self.qubits, int) or self.qubits <= 0:
                raise ValueError("successful canonical input has no qubits")
            if self.error:
                raise ValueError("successful canonical input may not have an error")
        elif self.status == "canonical_error":
            if (self.canonical_path or self.canonical_sha256
                    or any(value is not None for value in (
                        self.qubits, self.gates_1q, self.gates_2q))):
                raise ValueError("canonical-error entry may not claim canonical output")
            if not self.error:
                raise ValueError("canonical-error entry lacks an error")
        elif any((self.canonical_path, self.canonical_sha256, self.error)):
            raise ValueError("unprepared selected entry has prepared fields")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class QASMBenchInventory:
    """Deterministic source or prepared-input manifest."""

    entries: tuple[QASMBenchInputEntry, ...]
    upstream_repository: str = QASMBENCH_REPOSITORY
    upstream_commit: str = QASMBENCH_COMMIT
    canonical_profile: str = QASMBENCH_CANONICAL_PROFILE
    trace_retained: bool = False
    manifest_schema: str = QASMBENCH_MANIFEST_SCHEMA
    expected_counts_enforced: bool = True

    def _counts(self) -> dict[str, Any]:
        directories = Counter(entry.benchmark_scale for entry in self.entries)
        statuses = Counter(entry.status for entry in self.entries)
        return {
            "directories": {
                scale: directories.get(scale, 0) for scale in QASMBENCH_SCALES
            },
            "selected_qasm": sum(
                entry.status != "no_qasm_source" for entry in self.entries),
            "no_qasm_source": statuses.get("no_qasm_source", 0),
            "success": statuses.get("success", 0),
            "canonical_error": statuses.get("canonical_error", 0),
            "selected": statuses.get("selected", 0),
        }

    def validate(self) -> None:
        if self.manifest_schema != QASMBENCH_MANIFEST_SCHEMA:
            raise ValueError("wrong QASMBench source-manifest schema")
        if self.upstream_repository != QASMBENCH_REPOSITORY:
            raise ValueError("wrong QASMBench upstream repository")
        if self.canonical_profile != QASMBENCH_CANONICAL_PROFILE:
            raise ValueError("wrong QASMBench canonical profile")
        if self.trace_retained:
            raise ValueError("QASMBench input manifests may not retain traces")
        if not _HEX40.fullmatch(self.upstream_commit):
            raise ValueError("invalid QASMBench upstream commit")
        ordered = sorted(
            self.entries,
            key=lambda item: (
                QASMBENCH_SCALES.index(item.benchmark_scale),
                item.benchmark_directory,
            ),
        )
        if list(self.entries) != ordered:
            raise ValueError("QASMBench entries are not deterministically ordered")
        identities = [
            (entry.benchmark_scale, entry.benchmark_directory)
            for entry in self.entries
        ]
        if len(identities) != len(set(identities)):
            raise ValueError("duplicate QASMBench benchmark directory")
        for entry in self.entries:
            entry.validate()

        if self.expected_counts_enforced:
            if self.upstream_commit != QASMBENCH_COMMIT:
                raise ValueError("official QASMBench inventory has the wrong commit")
            counts = self._counts()
            if counts["directories"] != EXPECTED_DIRECTORY_COUNTS:
                raise ValueError(
                    "official QASMBench directory-count mismatch: "
                    f"{counts['directories']}")
            if counts["selected_qasm"] != EXPECTED_SELECTED_QASM:
                raise ValueError(
                    "official QASMBench selected-QASM mismatch: "
                    f"{counts['selected_qasm']}")
            no_sources = {
                f"{entry.benchmark_scale}/{entry.benchmark_directory}"
                for entry in self.entries
                if entry.status == "no_qasm_source"
            }
            if no_sources != EXPECTED_NO_QASM_DIRECTORIES:
                raise ValueError(
                    "official QASMBench no-source set mismatch: "
                    f"{sorted(no_sources)}")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "manifest_schema": self.manifest_schema,
            "upstream_repository": self.upstream_repository,
            "upstream_commit": self.upstream_commit,
            "canonical_profile": self.canonical_profile,
            "trace_retained": self.trace_retained,
            "counts": self._counts(),
            "entries": [entry.to_dict() for entry in self.entries],
        }


def _choose_qasm(directory: str, qasm_names: Sequence[str]) -> tuple[str, str]:
    exact_transpiled = f"{directory}_transpiled.qasm"
    exact_named = f"{directory}.qasm"
    if exact_transpiled in qasm_names:
        return exact_transpiled, "preferred_transpiled"
    if exact_named in qasm_names:
        return exact_named, "preferred_named"
    transpiled = [name for name in qasm_names
                  if name.lower().endswith("_transpiled.qasm")]
    if len(transpiled) == 1:
        # Three pinned Large directories have historical file stems which do
        # not equal their directory names.  Selecting their unique transpiled
        # file is the only deterministic reading consistent with 131 inputs.
        return transpiled[0], "unique_transpiled_fallback"
    if len(qasm_names) == 1:
        return qasm_names[0], "sole_qasm"
    raise ValueError(
        f"QASMBench directory {directory!r} has ambiguous QASM inputs: "
        f"{list(qasm_names)!r}")


def enumerate_qasmbench_sources(
    checkout: str | Path,
    *,
    require_pinned_commit: bool = True,
    validate_official_counts: bool = True,
) -> QASMBenchInventory:
    """Enumerate one source per official top-level benchmark directory."""

    root = Path(checkout).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"QASMBench checkout does not exist: {root}")
    commit = _git(root, "rev-parse", "HEAD")
    if not _HEX40.fullmatch(commit):
        raise ValueError(f"invalid QASMBench checkout commit: {commit!r}")
    if require_pinned_commit and commit != QASMBENCH_COMMIT:
        raise ValueError(
            f"QASMBench checkout must pin {QASMBENCH_COMMIT}; found {commit}")

    entries: list[QASMBenchInputEntry] = []
    for scale in QASMBENCH_SCALES:
        directories = _git_names(
            root, f"{commit}:{scale}", directories_only=True)
        for directory in directories:
            relative_directory = f"{scale}/{directory}"
            names = _git_names(root, f"{commit}:{relative_directory}")
            qasm_names = sorted(
                name for name in names if name.lower().endswith(".qasm"))
            if not qasm_names:
                entries.append(QASMBenchInputEntry(
                    benchmark_scale=scale,
                    benchmark_directory=directory,
                    upstream_path=relative_directory,
                    upstream_git_blob="",
                    source_sha256="",
                    selection_reason="no_qasm_source",
                    status="no_qasm_source",
                    error="no_qasm_source",
                ))
                continue

            selected, reason = _choose_qasm(directory, qasm_names)
            relative = f"{relative_directory}/{selected}"
            path = root / relative
            if not path.is_file():
                raise FileNotFoundError(
                    f"selected QASMBench source is not materialised: {relative}")
            blob = _git(root, "rev-parse", f"{commit}:{relative}")
            if not _HEX40.fullmatch(blob):
                raise ValueError(f"invalid upstream Git blob for {relative}: {blob}")
            working_blob = _git(root, "hash-object", "--", relative)
            if working_blob != blob:
                raise ValueError(
                    f"selected QASMBench source differs from pinned blob: {relative}")
            entries.append(QASMBenchInputEntry(
                benchmark_scale=scale,
                benchmark_directory=directory,
                upstream_path=relative,
                upstream_git_blob=blob,
                source_sha256=sha256_file(path),
                selection_reason=reason,
            ))

    inventory = QASMBenchInventory(
        entries=tuple(entries),
        upstream_commit=commit,
        expected_counts_enforced=validate_official_counts,
    )
    inventory.validate()
    return inventory


def _clean_qasm_line(raw: str) -> str:
    return raw.split("//", 1)[0].strip()


def _statements(path: Path) -> Iterator[tuple[int, str]]:
    with open(path, encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, 1):
            statement = _clean_qasm_line(raw)
            if statement:
                yield line_number, statement


def _split_csv(text: str) -> list[str]:
    result: list[str] = []
    start = 0
    depth = 0
    for index, character in enumerate(text):
        if character in "([":
            depth += 1
        elif character in ")]":
            depth -= 1
            if depth < 0:
                raise QASMBenchCanonicalError("unbalanced expression")
        elif character == "," and depth == 0:
            result.append(text[start:index].strip())
            start = index + 1
    if depth:
        raise QASMBenchCanonicalError("unbalanced expression")
    result.append(text[start:].strip())
    if any(not item for item in result):
        raise QASMBenchCanonicalError("empty parameter or operand")
    return result


def _parse_gate(statement: str, line_number: int) -> tuple[str, list[str], list[str]]:
    match = _GATE_STATEMENT.fullmatch(statement)
    if not match:
        raise QASMBenchCanonicalError(
            f"unsupported statement at line {line_number}: {statement[:100]}")
    name = match.group(1).lower()
    parameters = [] if match.group(2) is None else _split_csv(match.group(2))
    operands = _split_csv(match.group(3))
    return name, parameters, operands


def _expected_shape(name: str) -> tuple[int, int] | None:
    if name in _ONE_QUBIT_NO_PARAMETER:
        return 0, 1
    if name in _ONE_QUBIT_ONE_PARAMETER:
        return 1, 1
    if name in _ONE_QUBIT_TWO_PARAMETERS:
        return 2, 1
    if name in _ONE_QUBIT_THREE_PARAMETERS:
        return 3, 1
    if name in _TWO_QUBIT_NO_PARAMETER:
        return 0, 2
    if name in _TWO_QUBIT_ONE_PARAMETER:
        return 1, 2
    if name in _THREE_QUBIT_NO_PARAMETER:
        return 0, 3
    return None


@dataclass(frozen=True)
class _RegisterTable:
    quantum_offsets: Mapping[str, int]
    quantum_sizes: Mapping[str, int]
    classical_sizes: Mapping[str, int]

    @property
    def qubits(self) -> int:
        return sum(self.quantum_sizes.values())

    def quantum_bit(self, operand: str, line_number: int) -> int:
        match = _INDEXED_OPERAND.fullmatch(operand.strip())
        if not match:
            raise QASMBenchCanonicalError(
                f"gate requires an indexed qubit at line {line_number}")
        register, index_text = match.groups()
        if register not in self.quantum_sizes:
            raise QASMBenchCanonicalError(
                f"undeclared quantum register at line {line_number}: {register}")
        index = int(index_text)
        if index >= self.quantum_sizes[register]:
            raise QASMBenchCanonicalError(
                f"quantum index out of range at line {line_number}: {operand}")
        return self.quantum_offsets[register] + index

    def quantum_operand(self, operand: str, line_number: int) -> list[int]:
        indexed = _INDEXED_OPERAND.fullmatch(operand.strip())
        if indexed:
            return [self.quantum_bit(operand, line_number)]
        register = _REGISTER_OPERAND.fullmatch(operand.strip())
        if register and register.group(1) in self.quantum_sizes:
            name = register.group(1)
            offset = self.quantum_offsets[name]
            return list(range(offset, offset + self.quantum_sizes[name]))
        raise QASMBenchCanonicalError(
            f"invalid quantum operand at line {line_number}: {operand}")

    def classical_operand(self, operand: str, line_number: int) -> list[int]:
        indexed = _INDEXED_OPERAND.fullmatch(operand.strip())
        if indexed:
            register, index_text = indexed.groups()
            if register not in self.classical_sizes:
                raise QASMBenchCanonicalError(
                    f"undeclared classical register at line {line_number}: {register}")
            index = int(index_text)
            if index >= self.classical_sizes[register]:
                raise QASMBenchCanonicalError(
                    f"classical index out of range at line {line_number}: {operand}")
            return [index]
        register = _REGISTER_OPERAND.fullmatch(operand.strip())
        if register and register.group(1) in self.classical_sizes:
            return list(range(self.classical_sizes[register.group(1)]))
        raise QASMBenchCanonicalError(
            f"invalid classical operand at line {line_number}: {operand}")


def _validate_measurement(
    statement: str,
    line_number: int,
) -> None:
    body = statement[len("measure"):].strip()
    if not body.endswith(";") or "->" not in body:
        raise QASMBenchCanonicalError(
            f"malformed measurement at line {line_number}")
    quantum, classical = body[:-1].split("->", 1)
    if not quantum.strip() or not classical.strip():
        raise QASMBenchCanonicalError(
            f"malformed measurement at line {line_number}")


def _validate_barrier(
    statement: str,
    registers: _RegisterTable,
    line_number: int,
) -> None:
    body = statement[len("barrier"):].strip()
    if not body.endswith(";"):
        raise QASMBenchCanonicalError(f"malformed barrier at line {line_number}")
    for operand in _split_csv(body[:-1]):
        registers.quantum_operand(operand, line_number)


def _preflight_qasm(path: Path) -> tuple[_RegisterTable, Counter[str]]:
    quantum_offsets: dict[str, int] = {}
    quantum_sizes: dict[str, int] = {}
    classical_sizes: dict[str, int] = {}
    removed: Counter[str] = Counter()
    current_offset = 0
    seen_gate = False
    seen_measurement = False

    for line_number, statement in _statements(path):
        lowered = statement.lower()
        if lowered.startswith("openqasm ") or lowered.startswith("include "):
            if seen_gate:
                raise QASMBenchCanonicalError(
                    f"header after operations at line {line_number}")
            continue
        qreg = _QREG_DECLARATION.fullmatch(statement)
        if qreg:
            if seen_gate:
                raise QASMBenchCanonicalError(
                    f"qreg after operations at line {line_number}")
            name, size_text = qreg.groups()
            size = int(size_text)
            if size <= 0 or name in quantum_sizes:
                raise QASMBenchCanonicalError(
                    f"invalid qreg declaration at line {line_number}")
            quantum_offsets[name] = current_offset
            quantum_sizes[name] = size
            current_offset += size
            continue
        creg = _CREG_DECLARATION.fullmatch(statement)
        if creg:
            if seen_gate:
                raise QASMBenchCanonicalError(
                    f"creg after operations at line {line_number}")
            name, size_text = creg.groups()
            size = int(size_text)
            if size <= 0 or name in classical_sizes:
                raise QASMBenchCanonicalError(
                    f"invalid creg declaration at line {line_number}")
            classical_sizes[name] = size
            removed["classical_register"] += 1
            continue

        if not quantum_sizes:
            raise QASMBenchCanonicalError(
                f"operation before qreg declaration at line {line_number}")
        registers = _RegisterTable(
            quantum_offsets, quantum_sizes, classical_sizes)
        if lowered.startswith("if"):
            raise QASMBenchCanonicalError(
                f"conditional operation at line {line_number}")
        if lowered.startswith("reset"):
            raise QASMBenchCanonicalError(f"reset at line {line_number}")
        if lowered.startswith("measure"):
            # The pinned repository contains several mechanically appended
            # terminal measurements whose register spelling does not match the
            # circuit qreg/creg.  They are outside the unitary routing workload
            # and are removed by contract, so only their statement shape and
            # terminal position are validated here.
            _validate_measurement(statement, line_number)
            removed["measure"] += 1
            seen_gate = True
            seen_measurement = True
            continue
        if lowered.startswith("barrier"):
            _validate_barrier(statement, registers, line_number)
            removed["barrier"] += 1
            seen_gate = True
            continue
        if lowered.startswith("gate ") or lowered.startswith("opaque ") \
                or statement in {"{", "}"}:
            raise QASMBenchCanonicalError(
                f"custom gate declaration at line {line_number}")

        name, parameters, operands = _parse_gate(statement, line_number)
        shape = _expected_shape(name)
        if shape is None:
            raise QASMBenchCanonicalError(
                f"unsupported gate at line {line_number}: {name}")
        if (len(parameters), len(operands)) != shape:
            raise QASMBenchCanonicalError(
                f"wrong {name} arity at line {line_number}: "
                f"params={len(parameters)}, operands={len(operands)}")
        for operand in operands:
            registers.quantum_bit(operand, line_number)
        if len(operands) > 1 and len(set(operands)) != len(operands):
            raise QASMBenchCanonicalError(
                f"repeated {name} operand at line {line_number}")
        if name == "id":
            removed["id"] += 1
            seen_gate = True
            continue
        if seen_measurement:
            raise QASMBenchCanonicalError(
                f"mid-circuit measurement before line {line_number}")
        seen_gate = True

    if not quantum_sizes:
        raise QASMBenchCanonicalError("QASM has no qreg declaration")
    return _RegisterTable(
        dict(quantum_offsets), dict(quantum_sizes), dict(classical_sizes)), removed


def _negative(expression: str) -> str:
    return f"-({expression})"


def _half(expression: str) -> str:
    return f"({expression})/2"


class _BasisEmitter:
    def __init__(self, handle: TextIO, qubits: int):
        self.handle = handle
        self.depths = [0] * qubits
        self.gates_1q = 0
        self.gates_2q = 0

    @property
    def depth(self) -> int:
        return max(self.depths, default=0)

    def one(self, name: str, parameters: Sequence[str], qubit: int) -> None:
        if name not in {"u1", "u2", "u3"}:
            raise AssertionError(f"non-canonical 1Q gate: {name}")
        expected = {"u1": 1, "u2": 2, "u3": 3}[name]
        if len(parameters) != expected:
            raise AssertionError(f"wrong canonical {name} parameter count")
        self.handle.write(f"{name}({','.join(parameters)}) q[{qubit}];\n")
        self.gates_1q += 1
        self.depths[qubit] += 1

    def cz(self, left: int, right: int) -> None:
        if left == right:
            raise AssertionError("canonical CZ operands must differ")
        self.handle.write(f"cz q[{left}],q[{right}];\n")
        self.gates_2q += 1
        layer = max(self.depths[left], self.depths[right]) + 1
        self.depths[left] = self.depths[right] = layer

    def h(self, qubit: int) -> None:
        self.one("u2", ("0", "pi"), qubit)

    def phase(self, expression: str, qubit: int) -> None:
        self.one("u1", (expression,), qubit)

    def cx(self, control: int, target: int) -> None:
        self.h(target)
        self.cz(control, target)
        self.h(target)

    def ccx(self, first: int, second: int, target: int) -> None:
        self.h(target)
        self.cx(second, target)
        self.phase("-pi/4", target)
        self.cx(first, target)
        self.phase("pi/4", target)
        self.cx(second, target)
        self.phase("-pi/4", target)
        self.cx(first, target)
        self.phase("pi/4", second)
        self.phase("pi/4", target)
        self.h(target)
        self.cx(first, second)
        self.phase("pi/4", first)
        self.phase("-pi/4", second)
        self.cx(first, second)

    def gate(
        self,
        name: str,
        parameters: Sequence[str],
        qubits: Sequence[int],
    ) -> None:
        if name == "x":
            self.one("u3", ("pi", "0", "pi"), qubits[0])
        elif name == "y":
            self.one("u3", ("pi", "pi/2", "pi/2"), qubits[0])
        elif name == "z":
            self.phase("pi", qubits[0])
        elif name == "h":
            self.h(qubits[0])
        elif name == "s":
            self.phase("pi/2", qubits[0])
        elif name == "sdg":
            self.phase("-pi/2", qubits[0])
        elif name == "t":
            self.phase("pi/4", qubits[0])
        elif name == "tdg":
            self.phase("-pi/4", qubits[0])
        elif name == "sx":
            self.one("u2", ("-pi/2", "pi/2"), qubits[0])
        elif name == "sxdg":
            self.one("u2", ("pi/2", "-pi/2"), qubits[0])
        elif name in {"p", "rz", "u1"}:
            self.phase(parameters[0], qubits[0])
        elif name == "rx":
            self.one("u3", (parameters[0], "-pi/2", "pi/2"), qubits[0])
        elif name == "ry":
            self.one("u3", (parameters[0], "0", "0"), qubits[0])
        elif name == "u2":
            self.one("u2", parameters, qubits[0])
        elif name in {"u", "u3"}:
            self.one("u3", parameters, qubits[0])
        elif name == "cz":
            self.cz(qubits[0], qubits[1])
        elif name == "cx":
            self.cx(qubits[0], qubits[1])
        elif name == "cp":
            theta = parameters[0]
            self.phase(_half(theta), qubits[0])
            self.cx(qubits[0], qubits[1])
            self.phase(_negative(_half(theta)), qubits[1])
            self.cx(qubits[0], qubits[1])
            self.phase(_half(theta), qubits[1])
        elif name == "rzz":
            self.cx(qubits[0], qubits[1])
            self.phase(parameters[0], qubits[1])
            self.cx(qubits[0], qubits[1])
        elif name == "swap":
            self.cx(qubits[0], qubits[1])
            self.cx(qubits[1], qubits[0])
            self.cx(qubits[0], qubits[1])
        elif name == "ccx":
            self.ccx(qubits[0], qubits[1], qubits[2])
        elif name == "cswap":
            self.cx(qubits[2], qubits[1])
            self.ccx(qubits[0], qubits[1], qubits[2])
            self.cx(qubits[2], qubits[1])
        else:  # preflight and this emitter intentionally share a closed set.
            raise AssertionError(f"unhandled QASMBench gate: {name}")


def canonicalize_qasmbench_source(
    source: str | Path,
    output_qasm: str | Path,
    *,
    upstream_commit: str = QASMBENCH_COMMIT,
) -> CanonicalCircuitManifest:
    """Stream one pinned QASMBench QASM into the frozen canonical basis.

    All barriers and identities are discarded.  Measurements are discarded
    only when terminal; reset, conditionals, custom gates and mid-circuit
    measurements fail closed.  Existing output and sidecar files are removed
    before work begins so a failed attempt can never expose stale success data.
    """

    if upstream_commit != QASMBENCH_COMMIT:
        raise ValueError(
            f"QASMBench source must pin commit {QASMBENCH_COMMIT}")
    source_path = Path(source).resolve()
    output_path = Path(output_qasm).resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"QASMBench source does not exist: {source_path}")
    sidecar = output_path.with_suffix(output_path.suffix + ".manifest.json")
    output_path.unlink(missing_ok=True)
    sidecar.unlink(missing_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(output_path.name + ".tmp")
    temporary.unlink(missing_ok=True)

    try:
        registers, removed = _preflight_qasm(source_path)
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write('OPENQASM 2.0;\ninclude "qelib1.inc";\n')
            handle.write(f"qreg q[{registers.qubits}];\n")
            emitter = _BasisEmitter(handle, registers.qubits)
            for line_number, statement in _statements(source_path):
                lowered = statement.lower()
                if (lowered.startswith("openqasm ")
                        or lowered.startswith("include ")
                        or _QREG_DECLARATION.fullmatch(statement)
                        or _CREG_DECLARATION.fullmatch(statement)
                        or lowered.startswith("measure")
                        or lowered.startswith("barrier")):
                    continue
                name, parameters, operands = _parse_gate(statement, line_number)
                if name == "id":
                    continue
                qubits = [registers.quantum_bit(item, line_number)
                          for item in operands]
                emitter.gate(name, parameters, qubits)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output_path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        output_path.unlink(missing_ok=True)
        sidecar.unlink(missing_ok=True)
        raise

    manifest = CanonicalCircuitManifest(
        experiment_schema=SCHEMA_VERSION,
        source_path=str(source_path),
        canonical_path=str(output_path),
        source_sha256=sha256_file(source_path),
        canonical_sha256=sha256_file(output_path),
        qiskit_version="not-used",
        basis_gates=["cz", "u1", "u2", "u3"],
        optimization_level=0,
        seed_transpiler=0,
        qubits=registers.qubits,
        gates_1q=emitter.gates_1q,
        gates_2q=emitter.gates_2q,
        depth=emitter.depth,
        removed_operations=dict(sorted(removed.items())),
        canonical_profile=QASMBENCH_CANONICAL_PROFILE,
        upstream_commit=upstream_commit,
        canonicalizer_version=QASMBENCH_CANONICALIZER_VERSION,
    )
    manifest.validate()
    _atomic_json(sidecar, manifest.to_dict())
    return manifest


def _stable_error(exc: BaseException, *roots: Path) -> str:
    message = str(exc)
    for root in roots:
        message = message.replace(str(root.resolve()), "<root>")
    return f"{type(exc).__name__}: {message}"


def prepare_qasmbench_inputs(
    checkout: str | Path,
    output_directory: str | Path,
    *,
    require_pinned_commit: bool = True,
    validate_official_counts: bool = True,
) -> QASMBenchInventory:
    """Canonicalise the inventory and atomically publish a source manifest."""

    source_root = Path(checkout).resolve()
    output = Path(output_directory).resolve()
    inventory = enumerate_qasmbench_sources(
        source_root,
        require_pinned_commit=require_pinned_commit,
        validate_official_counts=validate_official_counts,
    )
    if output.exists():
        if any(output.iterdir()):
            raise ValueError(f"QASMBench output destination is non-empty: {output}")
        output.rmdir()
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(
        prefix=f".{output.name}.staging-", dir=output.parent)).resolve()
    prepared: list[QASMBenchInputEntry] = []
    try:
        for entry in inventory.entries:
            if entry.status == "no_qasm_source":
                prepared.append(entry)
                continue
            relative = Path(entry.benchmark_scale) / (
                entry.benchmark_directory + ".qasm")
            staged_path = staging / relative
            final_path = output / relative
            try:
                manifest = canonicalize_qasmbench_source(
                    source_root / entry.upstream_path,
                    staged_path,
                    upstream_commit=QASMBENCH_COMMIT,
                )
                published = replace(manifest, canonical_path=str(final_path))
                published.validate()
                _atomic_json(
                    staged_path.with_suffix(staged_path.suffix + ".manifest.json"),
                    published.to_dict(),
                )
                prepared.append(replace(
                    entry,
                    status="success",
                    canonical_path=relative.as_posix(),
                    canonical_sha256=manifest.canonical_sha256,
                    qubits=manifest.qubits,
                    gates_1q=manifest.gates_1q,
                    gates_2q=manifest.gates_2q,
                ))
            except (QASMBenchCanonicalError, ValueError, OSError) as exc:
                staged_path.unlink(missing_ok=True)
                staged_path.with_suffix(
                    staged_path.suffix + ".manifest.json").unlink(missing_ok=True)
                prepared.append(replace(
                    entry,
                    status="canonical_error",
                    error=_stable_error(exc, source_root, staging, output),
                ))

        result = QASMBenchInventory(
            entries=tuple(prepared),
            upstream_commit=inventory.upstream_commit,
            expected_counts_enforced=validate_official_counts,
        )
        result.validate()
        _atomic_json(staging / "source_manifest.json", result.to_dict())
        os.replace(staging, output)
        return result
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def load_qasmbench_source_manifest(path: str | Path) -> QASMBenchInventory:
    """Load and validate a deterministic ``source_manifest.json``."""

    manifest_path = Path(path).resolve()
    with open(manifest_path, encoding="utf-8") as handle:
        payload = json.load(handle)
    required = {
        "manifest_schema", "upstream_repository", "upstream_commit",
        "canonical_profile", "trace_retained", "counts", "entries",
    }
    if set(payload) != required:
        raise ValueError(
            "QASMBench source manifest fields differ: "
            f"missing={sorted(required - set(payload))}, "
            f"extra={sorted(set(payload) - required)}")
    entries = tuple(QASMBenchInputEntry(**dict(item))
                    for item in payload["entries"])
    inventory = QASMBenchInventory(
        entries=entries,
        upstream_repository=payload["upstream_repository"],
        upstream_commit=payload["upstream_commit"],
        canonical_profile=payload["canonical_profile"],
        trace_retained=payload["trace_retained"],
        manifest_schema=payload["manifest_schema"],
        expected_counts_enforced=(payload["upstream_commit"] == QASMBENCH_COMMIT
                                  and len(entries) == sum(
                                      EXPECTED_DIRECTORY_COUNTS.values())),
    )
    inventory.validate()
    if payload["counts"] != inventory._counts():
        raise ValueError("QASMBench source manifest count ledger mismatch")
    return inventory


__all__ = [
    "EXPECTED_DIRECTORY_COUNTS",
    "EXPECTED_NO_QASM_DIRECTORIES",
    "EXPECTED_SELECTED_QASM",
    "QASMBENCH_CANONICAL_PROFILE",
    "QASMBENCH_CANONICALIZER_VERSION",
    "QASMBENCH_COMMIT",
    "QASMBENCH_MANIFEST_SCHEMA",
    "QASMBENCH_REPOSITORY",
    "QASMBenchCanonicalError",
    "QASMBenchInputEntry",
    "QASMBenchInventory",
    "canonicalize_qasmbench_source",
    "enumerate_qasmbench_sources",
    "load_qasmbench_source_manifest",
    "prepare_qasmbench_inputs",
]
