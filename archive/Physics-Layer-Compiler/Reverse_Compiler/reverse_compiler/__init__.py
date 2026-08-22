"""Reverse compiler for ZAC.

Reconstructs a physical-qubit quantum circuit from a ZAC hardware schedule
(ZAIR ``*_code.json``).  See :mod:`reverse_compiler.reverse_compiler` for the
entry point :class:`ReverseCompiler`.
"""

from .circuit_ir import CircuitIR, CircuitOperation
from .reverse_compiler import ReverseCompiler, reverse_compile
from .exporters import to_qiskit, to_stim, StimExportError
from .reference_encoder import encode_circuit
from .visualization import extract_atom_events, plot_atom_operations, plot_circuit_structure

__all__ = [
    "CircuitIR",
    "CircuitOperation",
    "ReverseCompiler",
    "reverse_compile",
    "to_qiskit",
    "to_stim",
    "StimExportError",
    "encode_circuit",
    "extract_atom_events",
    "plot_atom_operations",
    "plot_circuit_structure",
]
