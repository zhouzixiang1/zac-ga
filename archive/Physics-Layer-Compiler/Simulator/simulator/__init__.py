"""Forward neutral-atom quantum-circuit simulator.

This package parses a *compiler-generated* hardware instruction list (the same
ZAIR schedule format produced by ``natam_compiler`` / consumed by the sibling
``Reverse_Compiler``) and *forward-simulates* it: it tracks every atom's
position through parallel movements and parallel gate layers, validates each
time step (destination conflicts, path collisions, atom overlap, unsupported
simultaneous moves) and animates the result.

Public API::

    from simulator import SimulationEngine, load_instructions, sample_program

The interactive GUI lives in :mod:`simulator.app` (run ``python run.py``).
"""

from __future__ import annotations

from .hardware import (
    ENTANGLE_ARRAY,
    ENTANGLE_COLS,
    ENTANGLE_ROWS,
    STORAGE_ARRAY,
    STORAGE_COLS,
    STORAGE_ROWS,
    Site,
    entangle_pair_ok,
    storage_index_to_site,
    valid_site,
)
from .engine import (
    Move,
    SimulationEngine,
    StepInfo,
    ValidationReport,
)
from .schema import instruction_atoms, load_instructions, op_kind, op_summary
from .sample import sample_program

__all__ = [
    "ENTANGLE_ARRAY",
    "ENTANGLE_COLS",
    "ENTANGLE_ROWS",
    "STORAGE_ARRAY",
    "STORAGE_COLS",
    "STORAGE_ROWS",
    "Site",
    "Move",
    "SimulationEngine",
    "StepInfo",
    "ValidationReport",
    "entangle_pair_ok",
    "storage_index_to_site",
    "valid_site",
    "instruction_atoms",
    "load_instructions",
    "op_kind",
    "op_summary",
    "sample_program",
]
