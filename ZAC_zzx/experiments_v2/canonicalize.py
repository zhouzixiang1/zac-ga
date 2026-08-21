"""Create immutable QASM inputs shared by all four methods."""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Iterable, List, Sequence

from .contracts import CanonicalCircuitManifest, SCHEMA_VERSION, sha256_file


BASIS_GATES = ("cz", "u1", "u2", "u3")
REMOVABLE = frozenset(("measure", "barrier", "id"))
FORBIDDEN = frozenset(("reset", "delay", "if_else", "while_loop", "for_loop",
                       "switch_case", "store"))
LARGE_PROFILE = "large_qasmbench_expand_only"
QASMBENCH_COMMIT = "357b942396d5c2b7cbc1c229c585a6ef5ccaebac"

_QREG = re.compile(r"^qreg\s+q\[(\d+)\]\s*;$", re.IGNORECASE)
_SINGLE = re.compile(r"^(x|h|s|sdg|t|tdg)\s+q\[(\d+)\]\s*;$", re.IGNORECASE)
_ROTATION = re.compile(r"^(rz|u1)\s*\((.+)\)\s+q\[(\d+)\]\s*;$", re.IGNORECASE)
_CX = re.compile(r"^cx\s+q\[(\d+)\]\s*,\s*q\[(\d+)\]\s*;$", re.IGNORECASE)
_CCX = re.compile(
    r"^ccx\s+q\[(\d+)\]\s*,\s*q\[(\d+)\]\s*,\s*q\[(\d+)\]\s*;$",
    re.IGNORECASE,
)


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _operation_name(item: object) -> str:
    operation = getattr(item, "operation", item[0])
    return str(operation.name)


def _condition(operation: object) -> object:
    return getattr(operation, "condition", None)


def canonicalize_circuit(source: str | Path, output_qasm: str | Path,
                         *, require_qiskit_version: str = "1.2.4",
                         optimization_level: int = 3,
                         seed_transpiler: int = 0,
                         canonical_profile: str = "main_qiskit_1_2_4_opt3",
                         upstream_commit: str = "") -> CanonicalCircuitManifest:
    """Canonicalise one QASM file, fail closed, and return its frozen manifest.

    Measurements, barriers and physical no-op ``id`` instructions are removed.
    Reset, delay, control flow, conditional operations and any post-transpile
    operation outside the requested basis fail the attempt.
    """
    if canonical_profile == LARGE_PROFILE:
        return canonicalize_large_circuit_streaming(
            source, output_qasm, upstream_commit=upstream_commit)
    if canonical_profile != "main_qiskit_1_2_4_opt3":
        raise ValueError(f"unknown canonical profile: {canonical_profile}")

    import qiskit
    from qiskit import QuantumCircuit, qasm2, transpile

    if qiskit.__version__ != require_qiskit_version:
        raise RuntimeError(
            f"canonicalisation requires Qiskit {require_qiskit_version}; "
            f"found {qiskit.__version__}")

    source_path = Path(source).resolve()
    output_path = Path(output_qasm).resolve()
    circuit = QuantumCircuit.from_qasm_file(str(source_path))
    clean = QuantumCircuit(circuit.num_qubits, name=circuit.name)
    removed: Counter[str] = Counter()

    for instruction in circuit.data:
        operation = instruction.operation
        name = str(operation.name)
        if name in FORBIDDEN or _condition(operation) is not None:
            raise ValueError(f"unsupported operation in {source_path}: {name}")
        if name in REMOVABLE:
            removed[name] += 1
            continue
        if instruction.clbits:
            raise ValueError(f"classical operand is unsupported: {name}")
        indices = [circuit.find_bit(qubit).index for qubit in instruction.qubits]
        clean.append(operation, [clean.qubits[index] for index in indices])

    canonical = transpile(
        clean,
        basis_gates=list(BASIS_GATES),
        optimization_level=optimization_level,
        seed_transpiler=seed_transpiler,
    )
    bad = []
    gates_1q = gates_2q = 0
    for instruction in canonical.data:
        name = str(instruction.operation.name)
        arity = len(instruction.qubits)
        if name not in BASIS_GATES or arity not in (1, 2):
            bad.append((name, arity))
        elif arity == 1:
            gates_1q += 1
        else:
            gates_2q += 1
    if bad:
        raise ValueError(f"canonical circuit contains unsupported operations: {bad[:5]}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(output_path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        qasm2.dump(canonical, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, output_path)

    manifest = CanonicalCircuitManifest(
        experiment_schema=SCHEMA_VERSION,
        source_path=str(source_path),
        canonical_path=str(output_path),
        source_sha256=sha256_file(source_path),
        canonical_sha256=sha256_file(output_path),
        qiskit_version=qiskit.__version__,
        basis_gates=list(BASIS_GATES),
        optimization_level=optimization_level,
        seed_transpiler=seed_transpiler,
        qubits=canonical.num_qubits,
        gates_1q=gates_1q,
        gates_2q=gates_2q,
        depth=canonical.depth(),
        removed_operations=dict(sorted(removed.items())),
        canonical_profile=canonical_profile,
        upstream_commit=upstream_commit,
        canonicalizer_version="qiskit-transpile-v1",
    )
    manifest.validate()
    _atomic_json(output_path.with_suffix(output_path.suffix + ".manifest.json"),
                 manifest.to_dict())
    return manifest


def canonicalize_large_circuit_streaming(
    source: str | Path,
    output_qasm: str | Path,
    *,
    upstream_commit: str,
) -> CanonicalCircuitManifest:
    """Expand the frozen QASMBench Large subset without materialising a circuit.

    The seven registered files use only the standard gates handled below.  CX
    and CCX are deterministically decomposed to ``cz,u1,u2,u3`` with no
    cancellation or cross-statement optimisation.  QASMBench lifecycle resets,
    final measurements and barriers are dropped *and counted*: Large measures
    routing/compiler scalability, while the main 172-circuit profile continues
    to reject every reset.
    """
    if upstream_commit != QASMBENCH_COMMIT:
        raise ValueError(f"Large source must pin QASMBench commit {QASMBENCH_COMMIT}")
    source_path = Path(source).resolve()
    output_path = Path(output_qasm).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(output_path.name + ".tmp")
    removed: Counter[str] = Counter()
    gates_1q = gates_2q = 0
    n_qubits: int | None = None
    depths: list[int] = []

    def clean(raw: str) -> str:
        return raw.split("//", 1)[0].strip()

    with open(source_path, encoding="utf-8") as input_handle, open(
            temporary, "w", encoding="utf-8") as output_handle:
        output_handle.write('OPENQASM 2.0;\ninclude "qelib1.inc";\n')

        def check_qubit(q: int) -> None:
            if n_qubits is None or q < 0 or q >= n_qubits:
                raise ValueError(f"Large gate references invalid q[{q}]")

        def emit_1q(name: str, params: str, q: int) -> None:
            nonlocal gates_1q
            check_qubit(q)
            output_handle.write(f"{name}({params}) q[{q}];\n")
            gates_1q += 1
            depths[q] += 1

        def emit_cz(q0: int, q1: int) -> None:
            nonlocal gates_2q
            check_qubit(q0)
            check_qubit(q1)
            if q0 == q1:
                raise ValueError("Large two-qubit gate has identical operands")
            output_handle.write(f"cz q[{q0}],q[{q1}];\n")
            gates_2q += 1
            layer = max(depths[q0], depths[q1]) + 1
            depths[q0] = depths[q1] = layer

        def emit_h(q: int) -> None:
            emit_1q("u2", "0,pi", q)

        def emit_phase(expr: str, q: int) -> None:
            emit_1q("u1", expr, q)

        def emit_cx(control: int, target: int) -> None:
            emit_h(target)
            emit_cz(control, target)
            emit_h(target)

        def emit_ccx(q0: int, q1: int, target: int) -> None:
            # Textbook exact Toffoli decomposition (six CX); each CX is then
            # expanded independently, intentionally retaining adjacent H gates.
            emit_h(target)
            emit_cx(q1, target)
            emit_phase("-pi/4", target)
            emit_cx(q0, target)
            emit_phase("pi/4", target)
            emit_cx(q1, target)
            emit_phase("-pi/4", target)
            emit_cx(q0, target)
            emit_phase("pi/4", q1)
            emit_phase("pi/4", target)
            emit_h(target)
            emit_cx(q0, q1)
            emit_phase("pi/4", q0)
            emit_phase("-pi/4", q1)
            emit_cx(q0, q1)

        for line_number, raw in enumerate(input_handle, 1):
            line = clean(raw)
            if not line:
                continue
            lowered = line.lower()
            if lowered.startswith("openqasm") or lowered.startswith("include"):
                continue
            match = _QREG.fullmatch(line)
            if match:
                if n_qubits is not None:
                    raise ValueError("Large QASM must contain exactly one qreg q")
                n_qubits = int(match.group(1))
                if n_qubits <= 0:
                    raise ValueError("Large qreg must be non-empty")
                depths = [0] * n_qubits
                output_handle.write(f"qreg q[{n_qubits}];\n")
                continue
            if lowered.startswith("creg "):
                removed["classical_register"] += 1
                continue
            if lowered.startswith("reset "):
                removed["reset"] += 1
                continue
            if lowered.startswith("measure "):
                removed["measure"] += 1
                continue
            if lowered.startswith("barrier"):
                removed["barrier"] += 1
                continue
            if lowered.startswith("if") or "->" in line:
                raise ValueError(f"unsupported Large classical operation at line {line_number}")
            match = _SINGLE.fullmatch(line)
            if match:
                name, q = match.group(1).lower(), int(match.group(2))
                if name == "x":
                    emit_1q("u3", "pi,0,pi", q)
                elif name == "h":
                    emit_h(q)
                else:
                    phase = {"s": "pi/2", "sdg": "-pi/2", "t": "pi/4",
                             "tdg": "-pi/4"}[name]
                    emit_phase(phase, q)
                continue
            match = _ROTATION.fullmatch(line)
            if match:
                emit_phase(match.group(2).strip(), int(match.group(3)))
                continue
            match = _CX.fullmatch(line)
            if match:
                emit_cx(int(match.group(1)), int(match.group(2)))
                continue
            match = _CCX.fullmatch(line)
            if match:
                emit_ccx(*(int(match.group(i)) for i in (1, 2, 3)))
                continue
            raise ValueError(
                f"unsupported Large QASM statement at {source_path}:{line_number}: {line[:120]}")

        if n_qubits is None:
            raise ValueError("Large QASM has no qreg q declaration")
        output_handle.flush()
        os.fsync(output_handle.fileno())
    os.replace(temporary, output_path)

    manifest = CanonicalCircuitManifest(
        experiment_schema=SCHEMA_VERSION,
        source_path=str(source_path),
        canonical_path=str(output_path),
        source_sha256=sha256_file(source_path),
        canonical_sha256=sha256_file(output_path),
        qiskit_version="not-used",
        basis_gates=list(BASIS_GATES),
        optimization_level=0,
        seed_transpiler=0,
        qubits=n_qubits,
        gates_1q=gates_1q,
        gates_2q=gates_2q,
        depth=max(depths, default=0),
        removed_operations=dict(sorted(removed.items())),
        canonical_profile=LARGE_PROFILE,
        upstream_commit=upstream_commit,
        canonicalizer_version="qasm2-stream-expander-v1",
    )
    manifest.validate()
    _atomic_json(output_path.with_suffix(output_path.suffix + ".manifest.json"),
                 manifest.to_dict())
    return manifest


def canonicalize_suite(sources: Iterable[str | Path], output_directory: str | Path,
                       **kwargs: object) -> List[CanonicalCircuitManifest]:
    output = Path(output_directory).resolve()
    output.mkdir(parents=True, exist_ok=True)
    manifests: List[CanonicalCircuitManifest] = []
    used_names: set[str] = set()
    for source in sorted((Path(item).resolve() for item in sources), key=str):
        if source.stem in used_names:
            raise ValueError(f"duplicate circuit stem: {source.stem}")
        used_names.add(source.stem)
        manifests.append(canonicalize_circuit(source, output / f"{source.stem}.qasm",
                                              **kwargs))
    _atomic_json(output / "suite.manifest.json", [item.to_dict() for item in manifests])
    return manifests


__all__ = [
    "BASIS_GATES", "LARGE_PROFILE", "canonicalize_circuit",
    "canonicalize_large_circuit_streaming", "canonicalize_suite",
]
