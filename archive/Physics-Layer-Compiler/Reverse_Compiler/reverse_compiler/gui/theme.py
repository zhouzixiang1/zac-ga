"""Central theme / design tokens for the Neutral-Atom Reverse Compiler GUI.

Everything visual (colours, fonts, spacing, radii) lives here so the rest of the
UI stays consistent and easy to restyle.  The palette is a modern, professional
light theme.
"""

from __future__ import annotations

# --------------------------------------------------------------------- palette
# Surfaces
BG_APP = "#eef1f6"          # app background (light grey)
BG_CARD = "#ffffff"         # content cards
BG_CARD_ALT = "#f6f8fb"     # subtle alternate card / hover
BORDER = "#dbe2ea"          # hairline borders

# Text
TEXT = "#1f2933"            # primary text
TEXT_MUTED = "#69707d"      # secondary text
TEXT_ON_ACCENT = "#ffffff"

# Brand / actions
ACCENT = "#2563eb"          # primary blue
ACCENT_HOVER = "#1d4ed8"
ACCENT_SOFT = "#dbeafe"

# Status
SUCCESS = "#16a34a"
SUCCESS_SOFT = "#dcfce7"
DANGER = "#dc2626"
DANGER_SOFT = "#fee2e2"
WARNING = "#d97706"
WARNING_SOFT = "#fef3c7"

# Zones (canvas)
STORAGE_ZONE_BG = "#e6f0ff"     # light blue background
STORAGE_ZONE_EDGE = "#93c5fd"
STORAGE_ATOM = "#2563eb"        # solid storage atom
ENTANGLE_ZONE_BG = "#ffe9dc"    # light orange/red background
ENTANGLE_ZONE_EDGE = "#fca5a5"
ENTANGLE_ATOM = "#dc2626"       # solid entanglement atom

TRAP_DOT = "#9aa7b8"            # empty trap marker
SELECT_EDGE = "#16a34a"         # highlighted / selected atom outline
MOVE_PATH = "#f59e0b"           # movement trajectory
ENTANGLE_LINK = "#dc2626"       # 2Q connection / halo

CANVAS_BG = "#ffffff"

# Operation-type accents (timeline chips)
OP_COLORS = {
    "INIT": "#64748b",
    "MOVE": "#f59e0b",
    "1Q": "#2563eb",
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
FONT_FAMILY = "SF Pro Text"     # falls back gracefully on non-mac
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
