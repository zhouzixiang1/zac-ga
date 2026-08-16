"""Pytest configuration: make the package importable when tests run from
anywhere and ensure the sibling ``Reverse_Compiler`` package can be located.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)  # the 100%_VIbe_Coding_Compiler folder
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
