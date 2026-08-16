#!/usr/bin/env python3
"""Launch the forward neutral-atom simulator GUI.

Usage:
    python run.py                      # start the GUI with the built-in demo
    python run.py path/to/schedule.json
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from simulator.app import SimulatorApp  # noqa: E402
from simulator.schema import load_instructions  # noqa: E402


def main() -> None:
    app = SimulatorApp()
    if len(sys.argv) > 1:
        path = sys.argv[1]
        try:
            app.load_program(load_instructions(path), source_label=os.path.basename(path))
        except Exception as exc:  # pragma: no cover
            print(f"Failed to load {path}: {exc}", file=sys.stderr)
    app.mainloop()


if __name__ == "__main__":
    main()
