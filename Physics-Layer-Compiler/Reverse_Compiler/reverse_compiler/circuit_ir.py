"""Internal circuit representation produced by the reverse compiler.

The IR is deliberately small and hardware agnostic.  Each
:class:`CircuitOperation` is a single reconstructed physical-qubit operation.
Hardware-only details (atom ids, coordinates, zones, the originating ZAC
instruction, timing/layer information) are preserved in ``metadata`` so that the
reconstruction is auditable and round-trippable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


# Gates that the IR knows how to reason about.  Anything else is still allowed
# to flow through (the exporters decide whether they can represent it) but these
# are the names produced by this package and used by the tests.
SINGLE_QUBIT_GATES = frozenset({"X", "Y", "Z", "H", "S", "SDG", "T", "TDG"})
ROTATION_GATES = frozenset({"RX", "RY", "RZ"})
TWO_QUBIT_GATES = frozenset({"CZ", "CX", "SWAP"})
NON_UNITARY = frozenset({"RESET", "MEASURE"})
TIMING_OPS = frozenset({"BARRIER", "DELAY"})


@dataclass
class CircuitOperation:
    """A single reconstructed physical-qubit operation."""

    name: str
    qubits: tuple[int, ...]
    params: tuple[float, ...] = ()
    classical_bits: tuple[int, ...] = ()
    timestamp: float | None = None
    layer: int | None = None
    metadata: dict = field(default_factory=dict)

    def is_timing(self) -> bool:
        """Whether this op is a hardware-only timing/padding op."""
        return self.name in TIMING_OPS

    def __str__(self) -> str:  # pragma: no cover - convenience only
        params = ""
        if self.params:
            params = "(" + ", ".join(f"{p:g}" for p in self.params) + ")"
        qubits = " ".join(f"q{q}" for q in self.qubits)
        bits = ""
        if self.classical_bits:
            bits = " -> " + " ".join(f"c{c}" for c in self.classical_bits)
        return f"{self.name}{params} {qubits}{bits}".strip()


@dataclass
class CircuitIR:
    """An ordered collection of :class:`CircuitOperation` objects."""

    operations: list[CircuitOperation] = field(default_factory=list)
    num_qubits: int = 0
    num_clbits: int = 0
    name: str = "reconstructed"
    metadata: dict = field(default_factory=dict)

    def add(self, op: CircuitOperation) -> None:
        """Append an operation, growing the qubit/clbit counts as needed."""
        if op.qubits:
            self.num_qubits = max(self.num_qubits, max(op.qubits) + 1)
        if op.classical_bits:
            self.num_clbits = max(self.num_clbits, max(op.classical_bits) + 1)
        self.operations.append(op)

    def without_timing(self) -> "CircuitIR":
        """Return a copy with hardware-only timing ops (BARRIER/DELAY) removed.

        Atom movement never produces an operation in the IR, so the only
        hardware-only ops to strip for circuit comparison are timing/padding.
        """
        clone = CircuitIR(
            operations=[op for op in self.operations if not op.is_timing()],
            num_qubits=self.num_qubits,
            num_clbits=self.num_clbits,
            name=self.name,
            metadata=dict(self.metadata),
        )
        return clone

    def signature(self) -> list[tuple]:
        """A hashable, comparable representation ignoring metadata/timing.

        Useful for round-trip equality checks.
        """
        sig: list[tuple] = []
        for op in self.operations:
            if op.is_timing():
                continue
            sig.append(
                (
                    op.name,
                    tuple(op.qubits),
                    tuple(round(p, 9) for p in op.params),
                    tuple(op.classical_bits),
                )
            )
        return sig

    def __iter__(self) -> Iterable[CircuitOperation]:
        return iter(self.operations)

    def __len__(self) -> int:
        return len(self.operations)
