"""Pytest configuration for the ZAC reverse compiler.

The package is fully independent of ZAC; we only add the package root to
``sys.path`` so that ``import reverse_compiler`` works regardless of where
pytest is invoked from.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
