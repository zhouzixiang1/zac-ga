"""natam_compiler -- a hardware-aware compiler for a zoned neutral-atom QPU.

Quick start::

    from natam_compiler import compile_circuit, verify
    from natam_compiler.benchmarks import ghz

    qc = ghz(5)
    result = compile_circuit(qc)
    print(result.program.to_text())
    print(result.metrics.as_dict())
    assert verify(qc, result.program)
"""

from .hardware import HardwareSpec, Zone, default_hardware, STORAGE, ENTANGLEMENT
from .ir import LogicalCircuit, from_qasm, from_qiskit
from .compiler import Compiler, CompileResult, compile_circuit
from .movement import (
    aod_ghost_traps,
    aod_tone_layout,
    batch_feasible,
    diagnose_batch,
    moves_compatible,
    plan_batches,
    validate_move,
)
from .animation import animate_compilation
from .zair import to_zair
from .reverse import reverse_to_ir, to_qiskit, to_stim
from .metrics import Metrics, summarize
from .verify import verify, VerificationResult
from . import operations, benchmarks

__all__ = [
    "HardwareSpec",
    "Zone",
    "default_hardware",
    "STORAGE",
    "ENTANGLEMENT",
    "LogicalCircuit",
    "from_qasm",
    "from_qiskit",
    "Compiler",
    "CompileResult",
    "compile_circuit",
    "plan_batches",
    "diagnose_batch",
    "moves_compatible",
    "batch_feasible",
    "aod_tone_layout",
    "aod_ghost_traps",
    "validate_move",
    "animate_compilation",
    "to_zair",
    "reverse_to_ir",
    "to_qiskit",
    "to_stim",
    "Metrics",
    "summarize",
    "verify",
    "VerificationResult",
    "operations",
    "benchmarks",
]

__version__ = "0.1.0"
