"""Launcher for the Neutral-Atom Reverse Compiler GUI.

The GUI has been refactored into the :mod:`reverse_compiler.gui` package
(``theme``, ``state``, ``renderer``, ``video_export``, ``outputs``, ``app``).
This module remains a stable entry point:

    python -m reverse_compiler.gui_app

``HardwareGuiApp`` is kept as a backwards-compatible alias for the new
``ReverseCompilerApp``.
"""

from __future__ import annotations

import os
import sys

try:
    from .gui.app import ReverseCompilerApp, main
except ImportError:  # Script mode: python /path/to/reverse_compiler/gui_app.py
    pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if pkg_root not in sys.path:
        sys.path.insert(0, pkg_root)
    from reverse_compiler.gui.app import ReverseCompilerApp, main

# Backwards-compatible alias.
HardwareGuiApp = ReverseCompilerApp

__all__ = ["ReverseCompilerApp", "HardwareGuiApp", "main"]


if __name__ == "__main__":
    main()
