"""Adapter that loads the standalone reverse compiler from the sibling
``Reverse_Compiler`` folder and exposes a small, stable API.

The reverse compiler is intentionally *not* re-implemented here: per the
project layout, all reverse-compilation logic lives in ``Reverse_Compiler``.
This module makes that package importable and wires it to our ZAIR exporter so
that callers can go ``HardwareProgram -> Qiskit/Stim circuit`` in one step.
"""

from __future__ import annotations

import importlib
import os
import sys
from functools import lru_cache
from pathlib import Path

from .operations import HardwareProgram
from .zair import to_zair


def _candidate_paths():
    env = os.environ.get("NATAM_REVERSE_COMPILER_PATH")
    if env:
        yield Path(env)
    # Sibling of the workspace folder: .../Compiler/Reverse_Compiler
    here = Path(__file__).resolve()
    yield here.parents[2] / "Reverse_Compiler"
    yield here.parents[3] / "Reverse_Compiler"


@lru_cache(maxsize=1)
def _load_package():
    last_err = None
    for path in _candidate_paths():
        pkg_dir = path / "reverse_compiler"
        if pkg_dir.is_dir():
            if str(path) not in sys.path:
                sys.path.insert(0, str(path))
            try:
                return importlib.import_module("reverse_compiler")
            except Exception as exc:  # pragma: no cover
                last_err = exc
    raise ImportError(
        "could not locate the 'Reverse_Compiler' package; set the "
        "NATAM_REVERSE_COMPILER_PATH environment variable to its folder. "
        f"last error: {last_err}"
    )


def reverse_to_ir(program: HardwareProgram, *, strict: bool = True):
    """Reverse-compile a program to the external package's ``CircuitIR``."""
    pkg = _load_package()
    return pkg.reverse_compile(to_zair(program), strict=strict)


def to_qiskit(program: HardwareProgram, *, strict: bool = True):
    """Reverse-compile a program straight to a Qiskit circuit."""
    pkg = _load_package()
    return pkg.to_qiskit(reverse_to_ir(program, strict=strict))


def to_stim(program: HardwareProgram, *, strict: bool = True):
    """Reverse-compile a program straight to a Stim circuit (Clifford only)."""
    pkg = _load_package()
    return pkg.to_stim(reverse_to_ir(program, strict=strict))


def stim_export_error():
    """Return the external package's ``StimExportError`` type."""
    return _load_package().StimExportError
