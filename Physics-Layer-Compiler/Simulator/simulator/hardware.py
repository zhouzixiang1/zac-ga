"""Hardware layout for the forward neutral-atom simulator.

Geometry follows the compiler (``natam_compiler``) ZAIR conventions exactly, so
schedules produced by the compiler simulate without any coordinate translation:

* **Storage zone** — ``40 rows × 20 columns`` tweezer array, SLM/array id ``0``.
* **Entanglement zone** — ``2 rows × 20 columns`` array, id ``1``, sharing the
  storage column axis and drawn directly **above** the storage zone.

A Rydberg (two-qubit) interaction pairs the two atoms occupying the **same
column** of the entanglement zone, one in row ``0`` and one in row ``1`` — giving
up to 20 parallel CZ pairs.

Storage-zone atom indices are assigned column by column, top-to-bottom within
each column (``atom k → col = k // 40, row = k % 40``).
"""

from __future__ import annotations

from dataclasses import dataclass

# --------------------------------------------------------------- array ids
STORAGE_ARRAY = 0
ENTANGLE_ARRAY = 1

# --------------------------------------------------------------- dimensions
STORAGE_ROWS = 40
STORAGE_COLS = 20
ENTANGLE_ROWS = 2
ENTANGLE_COLS = 20

# Vertical gap (in site units) between the storage zone (below) and the
# entanglement zone (above), which share the column (x) axis.
ZONE_GAP = 3
Y_MIN = -1.6
X_MIN = -1.6

# Horizontal spacing multiplier applied to columns **for rendering only** so the
# 20-wide zones are not visually cramped.  Physical distances (collision checks)
# use true grid units via :func:`grid_xy`, so this never affects validation.
COL_SPACING = 2.4

# Maximum atoms a single AOD transport batch may move simultaneously.
AOD_MAX_ATOMS = 40


@dataclass(frozen=True)
class Site:
    """A hardware trap position ``(array, row, col)``."""

    array: int
    row: int
    col: int

    @property
    def key(self) -> tuple[int, int, int]:
        return (self.array, self.row, self.col)

    def as_loc(self, atom: int) -> list[int]:
        """ZAIR location encoding ``[id, array, row, col]``."""
        return [int(atom), int(self.array), int(self.row), int(self.col)]

    @classmethod
    def from_loc(cls, loc) -> "Site":
        return cls(int(loc[1]), int(loc[2]), int(loc[3]))

    def label(self) -> str:
        zone = "S" if self.array == STORAGE_ARRAY else "E"
        return f"{zone}(r{self.row}, c{self.col})"


def valid_site(site: Site) -> bool:
    """Whether a site lies inside a defined zone."""
    if site.array == STORAGE_ARRAY:
        return 0 <= site.row < STORAGE_ROWS and 0 <= site.col < STORAGE_COLS
    if site.array == ENTANGLE_ARRAY:
        return 0 <= site.row < ENTANGLE_ROWS and 0 <= site.col < ENTANGLE_COLS
    return False


def is_entanglement(site: Site) -> bool:
    return site.array == ENTANGLE_ARRAY


def storage_index_to_site(index: int) -> Site:
    """Map a storage-zone linear index to its ``Site`` (column-major)."""
    if index < 0:
        raise ValueError("storage index must be non-negative")
    col = index // STORAGE_ROWS
    row = index % STORAGE_ROWS
    if col >= STORAGE_COLS:
        raise ValueError(f"storage index {index} exceeds zone capacity")
    return Site(STORAGE_ARRAY, row, col)


def site_to_storage_index(site: Site) -> int:
    """Inverse of :func:`storage_index_to_site` (storage zone only)."""
    if site.array != STORAGE_ARRAY:
        raise ValueError("site is not in the storage zone")
    return site.col * STORAGE_ROWS + site.row


def entangle_pair_ok(p0: Site, p1: Site) -> bool:
    """Whether two positions form a valid Rydberg interaction pair.

    Rule: both atoms in the entanglement zone, in the **same column**, occupying
    the two different rows ``{0, 1}``.
    """
    if p0.array != ENTANGLE_ARRAY or p1.array != ENTANGLE_ARRAY:
        return False
    if p0.col != p1.col:
        return False
    return {p0.row, p1.row} == {0, 1}


# ----------------------------------------------------------------- rendering
# The storage block occupies y ∈ [0, STORAGE_ROWS-1] with row 0 at the top.
# The entanglement block sits above it (larger y) sharing the column x-axis.
def _storage_top() -> float:
    return float(STORAGE_ROWS - 1)


def _entangle_base() -> float:
    return float(STORAGE_ROWS - 1 + ZONE_GAP + ENTANGLE_ROWS)


def grid_xy(site: Site) -> tuple[float, float]:
    """Unscaled physical grid coordinate ``(col, y)`` used for distance/collision
    computations (columns are 1 site apart, independent of render spacing)."""
    if site.array == STORAGE_ARRAY:
        return float(site.col), float(_storage_top() - site.row)
    base = _entangle_base()
    return float(site.col), float(base - site.row)


def site_xy(site: Site) -> tuple[float, float]:
    """Map a hardware site to canvas ``(x, y)`` for rendering.

    Row 0 is drawn at the top of each zone; the entanglement zone floats above
    the storage zone with ``ZONE_GAP`` of empty space between them.  Columns are
    spread out horizontally by ``COL_SPACING`` to avoid visual crowding.
    """
    gx, gy = grid_xy(site)
    return gx * COL_SPACING, gy


def bounds() -> tuple[float, float, float, float]:
    """Full-extent ``(x_min, x_max, y_min, y_max)`` for the hardware."""
    x_min = -0.7 * COL_SPACING
    x_max = (max(STORAGE_COLS, ENTANGLE_COLS) - 0.5) * COL_SPACING + 0.7 * COL_SPACING
    y_max = _entangle_base() + 1.8
    return x_min, x_max, Y_MIN, y_max
