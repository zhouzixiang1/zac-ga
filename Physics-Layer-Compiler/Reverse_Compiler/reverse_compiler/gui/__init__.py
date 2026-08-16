"""Modular GUI for the Neutral-Atom Reverse Compiler.

Public entry point:
    from reverse_compiler.gui import main
    main()
"""

from __future__ import annotations


def main() -> None:
    """Launch the GUI application."""
    from .app import main as _main

    _main()


__all__ = ["main"]
