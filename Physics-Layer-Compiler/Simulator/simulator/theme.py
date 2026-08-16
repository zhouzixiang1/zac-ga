"""Design tokens for the forward simulator GUI (modern light theme).

Kept visually consistent with the sibling Reverse Compiler interface.
"""

from __future__ import annotations

# --------------------------------------------------------------------- palette
BG_APP = "#eef1f6"
BG_CARD = "#ffffff"
BG_CARD_ALT = "#f6f8fb"
BORDER = "#dbe2ea"

TEXT = "#1f2933"
TEXT_MUTED = "#69707d"
TEXT_ON_ACCENT = "#ffffff"

ACCENT = "#2563eb"
ACCENT_HOVER = "#1d4ed8"
ACCENT_SOFT = "#dbeafe"

SUCCESS = "#16a34a"
SUCCESS_SOFT = "#dcfce7"
DANGER = "#dc2626"
DANGER_SOFT = "#fee2e2"
WARNING = "#d97706"
WARNING_SOFT = "#fef3c7"

# Zones (canvas)
STORAGE_ZONE_BG = "#e6f0ff"
STORAGE_ZONE_EDGE = "#93c5fd"
STORAGE_ATOM = "#2563eb"
ENTANGLE_ZONE_BG = "#ffe9dc"
ENTANGLE_ZONE_EDGE = "#fca5a5"
ENTANGLE_ATOM = "#dc2626"

TRAP_DOT = "#9aa7b8"
SELECT_EDGE = "#16a34a"
MOVE_PATH = "#f59e0b"        # movement trajectory
MOVING_ATOM = "#f59e0b"      # atom currently in transit
GATE_1Q = "#7c3aed"          # active single-qubit gate halo
ENTANGLE_LINK = "#dc2626"    # active two-qubit pair
CONFLICT = "#dc2626"         # validation conflict highlight
MEASURED_ATOM = "#0891b2"    # measured atom

CANVAS_BG = "#ffffff"

OP_COLORS = {
    "INIT": "#64748b",
    "MOVE": "#f59e0b",
    "1Q": "#7c3aed",
    "2Q": "#dc2626",
    "SWAP": "#9333ea",
    "MEASURE": "#0891b2",
    "OTHER": "#64748b",
}

# --------------------------------------------------------------------- metrics
RADIUS = 10
RADIUS_SM = 7
PAD = 12
PAD_SM = 6
GAP = 8

# --------------------------------------------------------------------- fonts
FONT_FAMILY = "SF Pro Text"
FONT_MONO = "Menlo"

FONT_TITLE = (FONT_FAMILY, 18, "bold")
FONT_H1 = (FONT_FAMILY, 15, "bold")
FONT_H2 = (FONT_FAMILY, 13, "bold")
FONT_BODY = (FONT_FAMILY, 12)
FONT_SMALL = (FONT_FAMILY, 11)
FONT_TINY = (FONT_FAMILY, 10)
FONT_CODE = (FONT_MONO, 11)


def apply_appearance() -> None:
    """Configure CustomTkinter global appearance (light, blue accent)."""
    import customtkinter as ctk

    ctk.set_appearance_mode("light")
    ctk.set_default_color_theme("blue")
