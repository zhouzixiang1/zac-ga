"""Matplotlib rendering of the neutral-atom array.

Two responsibilities:
* :func:`draw_scene` — a pure drawing routine used both by the live canvas and
  by the offline video/GIF frame renderer.
* :class:`CanvasView` — an embeddable widget (matplotlib on Tk) with mouse zoom,
  pan and a *Fit View* reset.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from . import theme
from . import state as st
from .state import (
    ENTANGLE_ARRAY,
    STORAGE_ARRAY,
    STORAGE_COLS,
    STORAGE_ROWS,
    Site,
)

# Layout: entanglement zone sits to the right of storage, sharing the row axis.
ZONE_GAP = 4
ENT_X0 = STORAGE_COLS + ZONE_GAP
X_MIN = -1.5


def _top() -> int:
    """Y of the top-most row (row 0); zones are top-aligned."""
    return max(STORAGE_ROWS, st.ENTANGLE_ROWS) - 1


def bounds() -> tuple[float, float, float, float]:
    """Full-extent (x_min, x_max, y_min, y_max) for the current hardware."""
    x_max = ENT_X0 + st.ENTANGLE_COLS + 0.5
    y_max = _top() + 1.6
    return X_MIN, x_max, -1.5, y_max


def site_xy(site: Site) -> tuple[float, float]:
    """Map a hardware site to canvas (x, y) with row 0 at the top."""
    top = _top()
    if site.array == STORAGE_ARRAY:
        return float(site.col), float(top - site.row)
    return float(ENT_X0 + site.col), float(top - site.row)


@dataclass
class SceneSpec:
    """Everything needed to draw one frame of the array."""

    positions: dict[int, Site]
    highlight: set[int] = field(default_factory=set)
    # (atom, src, dst, progress in [0,1])
    moves: list[tuple[int, Site, Site, float]] = field(default_factory=list)
    entangle_pairs: list[tuple[int, int]] = field(default_factory=list)
    title: str = ""
    subtitle: str = ""


def _interp(src: Site, dst: Site, t: float) -> tuple[float, float]:
    x0, y0 = site_xy(src)
    x1, y1 = site_xy(dst)
    return x0 + (x1 - x0) * t, y0 + (y1 - y0) * t


def draw_scene(ax, spec: SceneSpec, *, atom_size: float = 70.0, show_ids: bool = True) -> None:
    """Render ``spec`` onto a matplotlib Axes."""
    import matplotlib.patches as mpatches

    ax.clear()
    ax.set_facecolor(theme.CANVAS_BG)

    ent_rows, ent_cols = st.ENTANGLE_ROWS, st.ENTANGLE_COLS
    top = _top()

    # --- zone backgrounds -------------------------------------------------
    storage_rect = mpatches.FancyBboxPatch(
        (-0.5, top - STORAGE_ROWS + 0.5),
        STORAGE_COLS,
        STORAGE_ROWS,
        boxstyle="round,pad=0.02,rounding_size=0.4",
        facecolor=theme.STORAGE_ZONE_BG,
        edgecolor=theme.STORAGE_ZONE_EDGE,
        linewidth=1.2,
        zorder=0,
    )
    ax.add_patch(storage_rect)
    ent_rect = mpatches.FancyBboxPatch(
        (ENT_X0 - 0.5, top - ent_rows + 0.5),
        ent_cols,
        ent_rows,
        boxstyle="round,pad=0.02,rounding_size=0.4",
        facecolor=theme.ENTANGLE_ZONE_BG,
        edgecolor=theme.ENTANGLE_ZONE_EDGE,
        linewidth=1.2,
        zorder=0,
    )
    ax.add_patch(ent_rect)

    ax.text(
        (STORAGE_COLS - 1) / 2.0,
        top + 0.55,
        f"STORAGE ZONE  ·  {STORAGE_COLS} × {STORAGE_ROWS}",
        ha="center", va="bottom", fontsize=10, fontweight="bold",
        color=theme.STORAGE_ATOM,
    )
    ax.text(
        ENT_X0 + (ent_cols - 1) / 2.0,
        top + 0.55,
        f"ENTANGLEMENT  ·  {ent_cols} × {ent_rows}",
        ha="center", va="bottom", fontsize=10, fontweight="bold",
        color=theme.ENTANGLE_ATOM,
    )

    # --- empty traps (low-alpha dots) ------------------------------------
    occupied = {p.key for p in spec.positions.values()}
    trap_x: list[float] = []
    trap_y: list[float] = []
    for r in range(STORAGE_ROWS):
        for c in range(STORAGE_COLS):
            if (STORAGE_ARRAY, r, c) in occupied:
                continue
            x, y = site_xy(Site(STORAGE_ARRAY, r, c))
            trap_x.append(x)
            trap_y.append(y)
    for r in range(ent_rows):
        for c in range(ent_cols):
            if (ENTANGLE_ARRAY, r, c) in occupied:
                continue
            x, y = site_xy(Site(ENTANGLE_ARRAY, r, c))
            trap_x.append(x)
            trap_y.append(y)
    if trap_x:
        ax.scatter(trap_x, trap_y, s=6, c=theme.TRAP_DOT, alpha=0.28, zorder=1, linewidths=0)

    # --- entanglement halos + links --------------------------------------
    for a0, a1 in spec.entangle_pairs:
        if a0 not in spec.positions or a1 not in spec.positions:
            continue
        x0, y0 = site_xy(spec.positions[a0])
        x1, y1 = site_xy(spec.positions[a1])
        ax.plot([x0, x1], [y0, y1], color=theme.ENTANGLE_LINK, linewidth=2.0, zorder=3)
        for (xh, yh) in ((x0, y0), (x1, y1)):
            ax.scatter(xh, yh, s=atom_size * 3.2, facecolors="none",
                       edgecolors=theme.ENTANGLE_LINK, linewidths=1.6, alpha=0.55, zorder=3)

    # --- movement trajectories -------------------------------------------
    for atom, src, dst, progress in spec.moves:
        x0, y0 = site_xy(src)
        x1, y1 = site_xy(dst)
        ax.plot([x0, x1], [y0, y1], color=theme.MOVE_PATH, linewidth=1.6,
                linestyle=(0, (4, 3)), alpha=0.9, zorder=2)
        # start (hollow) and end (target) markers
        ax.scatter(x0, y0, s=atom_size * 0.9, facecolors="none",
                   edgecolors=theme.MOVE_PATH, linewidths=1.6, zorder=4)
        ax.scatter(x1, y1, s=atom_size * 1.4, marker="X", c=theme.MOVE_PATH, zorder=4)
        mx, my = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        ax.text(mx, my + 0.35, f"a{atom}", color=theme.MOVE_PATH,
                fontsize=8, ha="center", va="bottom", fontweight="bold")

    # --- atoms ------------------------------------------------------------
    for atom, site in sorted(spec.positions.items()):
        # If the atom is mid-move, draw it along the interpolated path.
        moving = next((m for m in spec.moves if m[0] == atom), None)
        if moving is not None:
            _a, src, dst, progress = moving
            x, y = _interp(src, dst, progress)
            zone_array = dst.array
        else:
            x, y = site_xy(site)
            zone_array = site.array
        color = theme.STORAGE_ATOM if zone_array == STORAGE_ARRAY else theme.ENTANGLE_ATOM
        selected = atom in spec.highlight
        edge = theme.SELECT_EDGE if selected else "#0f172a"
        lw = 2.4 if selected else 0.8
        if selected:
            ax.scatter(x, y, s=atom_size * 2.6, facecolors="none",
                       edgecolors=theme.SELECT_EDGE, linewidths=1.4, alpha=0.5, zorder=4)
        ax.scatter(x, y, s=atom_size, c=color, edgecolors=edge, linewidths=lw, zorder=5)
        if show_ids:
            ax.text(x + 0.34, y + 0.30, str(atom), fontsize=7.5,
                    color=theme.TEXT, zorder=6)

    # --- axes cosmetics ---------------------------------------------------
    x_min, x_max, y_min, y_max = bounds()
    ax.set_xlim(x_min, x_max)
    ax.set_ylim(y_min, y_max)
    ax.set_aspect("equal", adjustable="box")
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)
    if spec.title:
        ax.set_title(spec.title, fontsize=12, fontweight="bold", color=theme.TEXT, loc="left")
    if spec.subtitle:
        ax.text(x_min + 0.2, y_min + 0.4, spec.subtitle, fontsize=9, color=theme.TEXT_MUTED)


class CanvasView:
    """An embeddable, interactive atom-array canvas (zoom / pan / fit)."""

    def __init__(self, master) -> None:
        import matplotlib
        matplotlib.use("TkAgg", force=False)
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

        self.figure = Figure(figsize=(7.5, 4.4), dpi=100, facecolor=theme.BG_CARD)
        self.ax = self.figure.add_subplot(111)
        self.figure.subplots_adjust(left=0.02, right=0.98, top=0.92, bottom=0.03)
        self._canvas = FigureCanvasTkAgg(self.figure, master=master)
        self._spec: Optional[SceneSpec] = None

        self._canvas.mpl_connect("scroll_event", self._on_scroll)
        self._canvas.mpl_connect("button_press_event", self._on_press)
        self._canvas.mpl_connect("button_release_event", self._on_release)
        self._canvas.mpl_connect("motion_notify_event", self._on_motion)
        self._pan_start: Optional[tuple[float, float]] = None
        self._press: Optional[dict] = None
        self._marquee = None
        # interaction callbacks (set by the app)
        self._on_atom_pick = None
        self._on_site_pick = None
        self._on_marquee = None
        self._on_drag_preview = None
        self._on_drag_commit = None
        self._on_clear = None

    @property
    def widget(self):
        return self._canvas.get_tk_widget()

    def set_pick_handlers(self, on_atom, on_site) -> None:
        self._on_atom_pick = on_atom
        self._on_site_pick = on_site

    def set_interaction_handlers(self, *, on_atom=None, on_site=None, on_marquee=None,
                                 on_drag_preview=None, on_drag_commit=None, on_clear=None) -> None:
        if on_atom is not None:
            self._on_atom_pick = on_atom
        if on_site is not None:
            self._on_site_pick = on_site
        self._on_marquee = on_marquee
        self._on_drag_preview = on_drag_preview
        self._on_drag_commit = on_drag_commit
        self._on_clear = on_clear

    def render(self, spec: SceneSpec, *, keep_view: bool = True) -> None:
        prev = None
        if keep_view and self._spec is not None:
            prev = (self.ax.get_xlim(), self.ax.get_ylim())
        self._spec = spec
        draw_scene(self.ax, spec)
        if prev is not None:
            self.ax.set_xlim(prev[0])
            self.ax.set_ylim(prev[1])
        self._canvas.draw_idle()

    def fit_view(self) -> None:
        x_min, x_max, y_min, y_max = bounds()
        self.ax.set_xlim(x_min, x_max)
        self.ax.set_ylim(y_min, y_max)
        self._canvas.draw_idle()

    # ------------------------------------------------------------- interaction
    #
    # Left button:
    #   * click an atom            -> select it (Shift-click toggles multi-select)
    #   * click empty              -> move the single selected atom there
    #   * drag an atom             -> move the whole selection by an integer shift
    #   * drag on empty background -> rubber-band (marquee) multi-select
    # Middle / right button drag   -> pan.  Scroll -> zoom.
    @staticmethod
    def _is_shift(event) -> bool:
        ge = getattr(event, "guiEvent", None)
        try:
            return bool(ge is not None and (ge.state & 0x0001))
        except Exception:
            return False

    def _nearest_atom(self, x: float, y: float) -> Optional[int]:
        if self._spec is None:
            return None
        best: Optional[int] = None
        best_d = 0.5 ** 2
        for a, p in self._spec.positions.items():
            sx, sy = site_xy(p)
            d = (sx - x) ** 2 + (sy - y) ** 2
            if d < best_d:
                best_d = d
                best = a
        return best

    def _on_scroll(self, event) -> None:
        if event.inaxes is not self.ax or event.xdata is None:
            return
        scale = 0.85 if event.button == "up" else 1.0 / 0.85
        x0, x1 = self.ax.get_xlim()
        y0, y1 = self.ax.get_ylim()
        xd, yd = event.xdata, event.ydata
        self.ax.set_xlim(xd + (x0 - xd) * scale, xd + (x1 - xd) * scale)
        self.ax.set_ylim(yd + (y0 - yd) * scale, yd + (y1 - yd) * scale)
        self._canvas.draw_idle()

    def _on_press(self, event) -> None:
        if event.inaxes is not self.ax or event.xdata is None:
            return
        if event.button == 1:
            self._press = {
                "x": event.xdata,
                "y": event.ydata,
                "atom": self._nearest_atom(event.xdata, event.ydata),
                "shift": self._is_shift(event),
                "moved": False,
            }
        elif event.button in (2, 3):
            self._pan_start = (event.xdata, event.ydata)

    def _on_motion(self, event) -> None:
        # Pan (middle/right).
        if self._pan_start is not None and event.inaxes is self.ax and event.xdata is not None:
            dx = event.xdata - self._pan_start[0]
            dy = event.ydata - self._pan_start[1]
            if abs(dx) >= 0.4 or abs(dy) >= 0.4:
                x0, x1 = self.ax.get_xlim()
                y0, y1 = self.ax.get_ylim()
                self.ax.set_xlim(x0 - dx, x1 - dx)
                self.ax.set_ylim(y0 - dy, y1 - dy)
                self._canvas.draw_idle()
            return
        # Left-button gestures.
        if self._press is None or event.xdata is None:
            return
        dx = event.xdata - self._press["x"]
        dy = event.ydata - self._press["y"]
        if not self._press["moved"] and (abs(dx) > 0.3 or abs(dy) > 0.3):
            self._press["moved"] = True
        if not self._press["moved"]:
            return
        if self._press["atom"] is not None and not self._press["shift"]:
            target = self._anchor_target(self._press["atom"], event.xdata, event.ydata,
                                         self._press["x"], self._press["y"])
            if target is not None and self._on_drag_preview:
                self._on_drag_preview(self._press["atom"], target)
        else:
            # Shift-drag (or drag from empty space) draws a selection box, so a
            # dense row/column can be boxed even when starting on an atom.
            self._draw_marquee(self._press["x"], self._press["y"], event.xdata, event.ydata)

    def _on_release(self, event) -> None:
        if self._pan_start is not None:
            self._pan_start = None
            return
        p = self._press
        self._press = None
        if p is None:
            return
        x = event.xdata if event.xdata is not None else p["x"]
        y = event.ydata if event.ydata is not None else p["y"]
        if not p["moved"]:
            if p["atom"] is not None and self._on_atom_pick:
                self._on_atom_pick(p["atom"], p["shift"])
            elif p["atom"] is None:
                site = self._nearest_site(x, y)
                if site is not None and self._on_site_pick:
                    self._on_site_pick(site)
                elif site is None and self._on_clear:
                    self._on_clear()
            return
        # A real drag.
        if p["atom"] is not None and not p["shift"]:
            target = self._anchor_target(p["atom"], x, y, p["x"], p["y"])
            if self._on_drag_commit:
                self._on_drag_commit(p["atom"], target)
        else:
            atoms = self._atoms_in_rect(p["x"], p["y"], x, y)
            self._clear_marquee()
            if self._on_marquee:
                self._on_marquee(atoms, p["shift"])

    def _anchor_target(self, anchor: int, x: float, y: float,
                       px: float, py: float) -> Optional[Site]:
        """Snap the anchor atom's dropped position to the nearest hardware site.

        Uses the pointer displacement (not raw coordinates) so that a drop over the
        entanglement zone resolves to that zone even though it sits at a different
        x-offset from storage.
        """
        if self._spec is None or anchor not in self._spec.positions:
            return None
        src = self._spec.positions[anchor]
        sx, sy = site_xy(src)
        return self._nearest_site(sx + (x - px), sy + (y - py))

    # ---- marquee helpers ----
    def _draw_marquee(self, x0: float, y0: float, x1: float, y1: float) -> None:
        import matplotlib.patches as mpatches

        lo_x, hi_x = sorted((x0, x1))
        lo_y, hi_y = sorted((y0, y1))
        if self._marquee is None:
            self._marquee = mpatches.Rectangle(
                (lo_x, lo_y), hi_x - lo_x, hi_y - lo_y,
                facecolor=theme.ACCENT, alpha=0.12,
                edgecolor=theme.ACCENT, linewidth=1.2, zorder=8,
            )
            self.ax.add_patch(self._marquee)
        else:
            self._marquee.set_bounds(lo_x, lo_y, hi_x - lo_x, hi_y - lo_y)
        self._canvas.draw_idle()

    def _clear_marquee(self) -> None:
        if self._marquee is not None:
            try:
                self._marquee.remove()
            except Exception:
                pass
            self._marquee = None
            self._canvas.draw_idle()

    def _atoms_in_rect(self, x0: float, y0: float, x1: float, y1: float) -> set[int]:
        if self._spec is None:
            return set()
        lo_x, hi_x = sorted((x0, x1))
        lo_y, hi_y = sorted((y0, y1))
        found: set[int] = set()
        for a, p in self._spec.positions.items():
            sx, sy = site_xy(p)
            if lo_x - 0.4 <= sx <= hi_x + 0.4 and lo_y - 0.4 <= sy <= hi_y + 0.4:
                found.add(a)
        return found

    @staticmethod
    def _nearest_site(x: float, y: float) -> Optional[Site]:
        # storage
        best: Optional[Site] = None
        best_d = 0.6 ** 2
        for array, rows, cols in (
            (STORAGE_ARRAY, STORAGE_ROWS, STORAGE_COLS),
            (ENTANGLE_ARRAY, st.ENTANGLE_ROWS, st.ENTANGLE_COLS),
        ):
            for r in range(rows):
                for c in range(cols):
                    site = Site(array, r, c)
                    sx, sy = site_xy(site)
                    d = (sx - x) ** 2 + (sy - y) ** 2
                    if d < best_d:
                        best_d = d
                        best = site
        return best
