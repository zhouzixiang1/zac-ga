"""Matplotlib rendering of the neutral-atom array for the forward simulator.

``draw_scene`` is a pure drawing routine shared by the live canvas and the
offline MP4 renderer.  ``CanvasView`` is an embeddable Tk widget with scroll
zoom, drag pan and a *Fit View* reset.  The forward simulator canvas is
read-only (no atom editing) — it visualizes a computed :class:`~simulator.SceneSpec`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from . import theme
from . import hardware as hw
from .hardware import (
    ENTANGLE_ARRAY,
    ENTANGLE_ARRAY,
    STORAGE_ARRAY,
    STORAGE_COLS,
    STORAGE_ROWS,
    Site,
    bounds,
    site_xy,
)


@dataclass
class SceneSpec:
    """Everything needed to draw one animation frame."""

    positions: dict[int, Site]
    highlight: set[int] = field(default_factory=set)
    # (atom, src, dst, progress in [0,1]) — atoms currently in transit
    moves: list[tuple[int, Site, Site, float]] = field(default_factory=list)
    # active two-qubit pairs (drawn as entangling links)
    entangle_pairs: list[tuple[int, int]] = field(default_factory=list)
    # atoms receiving an active single-qubit gate (purple pulse ring)
    gate_atoms: set[int] = field(default_factory=set)
    measured: set[int] = field(default_factory=set)
    # validation conflicts
    conflict_atoms: set[int] = field(default_factory=set)
    conflict_pairs: list[tuple[int, int]] = field(default_factory=list)
    # ghost tweezers created by the crossed-AOD tone product, as ((sx,sy),(dx,dy))
    # grid-coordinate pairs; ``ghost_bad`` are those disturbing a static atom.
    ghost_traps: list[tuple[tuple[float, float], tuple[float, float]]] = field(
        default_factory=list
    )
    ghost_bad: list[tuple[tuple[float, float], tuple[float, float]]] = field(
        default_factory=list
    )
    ghost_progress: float = 1.0
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

    ent_rows, ent_cols = hw.ENTANGLE_ROWS, hw.ENTANGLE_COLS
    sp = hw.COL_SPACING

    # --- zone backgrounds -------------------------------------------------
    # Storage block sits below; entanglement block floats above, sharing cols.
    sx0, sy_bot = -0.5 * sp, site_xy(Site(STORAGE_ARRAY, STORAGE_ROWS - 1, 0))[1] - 0.5
    storage_rect = mpatches.FancyBboxPatch(
        (sx0, sy_bot), STORAGE_COLS * sp, STORAGE_ROWS,
        boxstyle="round,pad=0.02,rounding_size=0.4",
        facecolor=theme.STORAGE_ZONE_BG, edgecolor=theme.STORAGE_ZONE_EDGE,
        linewidth=1.2, zorder=0,
    )
    ax.add_patch(storage_rect)
    ey_bot = site_xy(Site(ENTANGLE_ARRAY, ent_rows - 1, 0))[1] - 0.5
    ent_rect = mpatches.FancyBboxPatch(
        (-0.5 * sp, ey_bot), ent_cols * sp, ent_rows,
        boxstyle="round,pad=0.02,rounding_size=0.4",
        facecolor=theme.ENTANGLE_ZONE_BG, edgecolor=theme.ENTANGLE_ZONE_EDGE,
        linewidth=1.2, zorder=0,
    )
    ax.add_patch(ent_rect)

    mid_x = (STORAGE_COLS - 1) / 2.0 * sp
    ent_top = site_xy(Site(ENTANGLE_ARRAY, 0, 0))[1]
    ax.text(mid_x, ent_top + 1.05,
            f"ENTANGLEMENT  ·  {ent_rows} × {ent_cols}   (CZ pairs: same column, rows 0/1)",
            ha="center", va="bottom", fontsize=10, fontweight="bold",
            color=theme.ENTANGLE_ATOM)
    ax.text(mid_x, site_xy(Site(STORAGE_ARRAY, 0, 0))[1] + 0.7,
            f"STORAGE ZONE  ·  {STORAGE_ROWS} × {STORAGE_COLS}",
            ha="center", va="bottom", fontsize=10, fontweight="bold",
            color=theme.STORAGE_ATOM)

    # --- empty traps ------------------------------------------------------
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

    moving_ids = {m[0] for m in spec.moves}

    def _pos_xy(atom: int) -> Optional[tuple[float, float]]:
        for a, src, dst, prog in spec.moves:
            if a == atom:
                return _interp(src, dst, prog)
        if atom in spec.positions:
            return site_xy(spec.positions[atom])
        return None

    # --- active two-qubit pairs ------------------------------------------
    for a0, a1 in spec.entangle_pairs:
        p0 = _pos_xy(a0)
        p1 = _pos_xy(a1)
        if p0 is None or p1 is None:
            continue
        ax.plot([p0[0], p1[0]], [p0[1], p1[1]], color=theme.ENTANGLE_LINK,
                linewidth=2.4, zorder=3)
        for (xh, yh) in (p0, p1):
            ax.scatter(xh, yh, s=atom_size * 3.4, facecolors="none",
                       edgecolors=theme.ENTANGLE_LINK, linewidths=1.8, alpha=0.6, zorder=3)

    # --- conflict links ---------------------------------------------------
    for a0, a1 in spec.conflict_pairs:
        p0 = _pos_xy(a0)
        p1 = _pos_xy(a1)
        if p0 is None or p1 is None:
            continue
        ax.plot([p0[0], p1[0]], [p0[1], p1[1]], color=theme.CONFLICT,
                linewidth=2.2, linestyle=(0, (2, 2)), zorder=6)

    # --- movement trajectories -------------------------------------------
    for atom, src, dst, progress in spec.moves:
        x0, y0 = site_xy(src)
        x1, y1 = site_xy(dst)
        ax.plot([x0, x1], [y0, y1], color=theme.MOVE_PATH, linewidth=1.6,
                linestyle=(0, (4, 3)), alpha=0.9, zorder=2)
        ax.scatter(x0, y0, s=atom_size * 0.9, facecolors="none",
                   edgecolors=theme.MOVE_PATH, linewidths=1.4, zorder=4)
        ax.scatter(x1, y1, s=atom_size * 1.3, marker="X", c=theme.MOVE_PATH,
                   alpha=0.8, zorder=4)

    # --- active single-qubit gate rings ----------------------------------
    for atom in spec.gate_atoms:
        p = _pos_xy(atom)
        if p is None:
            continue
        ax.scatter(p[0], p[1], s=atom_size * 3.2, facecolors="none",
                   edgecolors=theme.GATE_1Q, linewidths=2.2, alpha=0.85, zorder=4)

    # --- ghost tweezers (non-target AOD tone intersections) --------------
    #  The crossed AOD forms a tweezer at every X-tone x Y-tone intersection.
    #  Ones with no intended atom are drawn as dashed, semi-transparent rings so
    #  the user can see the "ghost" traps the tone product creates; any that
    #  sweep over a static atom are flagged in the conflict color.
    _sp = hw.COL_SPACING
    _bad = {(tuple(s), tuple(d)) for s, d in spec.ghost_bad}
    for src, dst in spec.ghost_traps:
        gx = (src[0] + (dst[0] - src[0]) * spec.ghost_progress) * _sp
        gy = src[1] + (dst[1] - src[1]) * spec.ghost_progress
        is_bad = (tuple(src), tuple(dst)) in _bad
        gcolor = theme.CONFLICT if is_bad else theme.MOVE_PATH
        galpha = 0.85 if is_bad else 0.4
        ax.scatter(gx, gy, s=atom_size * 1.1, facecolors="none",
                   edgecolors=gcolor, linewidths=1.2,
                   linestyle=(0, (2, 2)), alpha=galpha, zorder=3)

    # --- atoms ------------------------------------------------------------
    for atom, site in sorted(spec.positions.items()):
        p = _pos_xy(atom)
        if p is None:
            continue
        x, y = p
        if atom in moving_ids:
            color = theme.MOVING_ATOM
        elif atom in spec.measured:
            color = theme.MEASURED_ATOM
        elif site.array == ENTANGLE_ARRAY:
            color = theme.ENTANGLE_ATOM
        else:
            color = theme.STORAGE_ATOM

        conflict = atom in spec.conflict_atoms
        selected = atom in spec.highlight
        if conflict:
            edge, lw = theme.CONFLICT, 2.6
            ax.scatter(x, y, s=atom_size * 3.0, facecolors="none",
                       edgecolors=theme.CONFLICT, linewidths=1.8, alpha=0.7, zorder=5)
        elif selected:
            edge, lw = theme.SELECT_EDGE, 2.4
            ax.scatter(x, y, s=atom_size * 2.6, facecolors="none",
                       edgecolors=theme.SELECT_EDGE, linewidths=1.4, alpha=0.5, zorder=4)
        else:
            edge, lw = "#0f172a", 0.8
        ax.scatter(x, y, s=atom_size, c=color, edgecolors=edge, linewidths=lw, zorder=6)
        if show_ids:
            ax.text(x + 0.34, y + 0.30, str(atom), fontsize=7.5,
                    color=theme.TEXT, zorder=7)

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
        ax.text(x_min + 0.2, y_min + 0.5, spec.subtitle, fontsize=9, color=theme.TEXT_MUTED)


class CanvasView:
    """An embeddable, interactive atom-array canvas (zoom / pan / fit)."""

    def __init__(self, master) -> None:
        import matplotlib
        matplotlib.use("TkAgg", force=False)
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

        self.figure = Figure(figsize=(7.6, 4.6), dpi=100, facecolor=theme.BG_CARD)
        self.ax = self.figure.add_subplot(111)
        self.figure.subplots_adjust(left=0.02, right=0.98, top=0.90, bottom=0.04)
        self._canvas = FigureCanvasTkAgg(self.figure, master=master)
        self._spec: Optional[SceneSpec] = None

        self._canvas.mpl_connect("scroll_event", self._on_scroll)
        self._canvas.mpl_connect("button_press_event", self._on_press)
        self._canvas.mpl_connect("button_release_event", self._on_release)
        self._canvas.mpl_connect("motion_notify_event", self._on_motion)
        self._pan_start: Optional[tuple[float, float]] = None
        self._on_atom_pick = None

    @property
    def widget(self):
        return self._canvas.get_tk_widget()

    def set_atom_pick(self, cb) -> None:
        self._on_atom_pick = cb

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
            atom = self._nearest_atom(event.xdata, event.ydata)
            if atom is not None and self._on_atom_pick:
                self._on_atom_pick(atom)
        elif event.button in (2, 3):
            self._pan_start = (event.xdata, event.ydata)

    def _on_motion(self, event) -> None:
        if self._pan_start is None or event.inaxes is not self.ax or event.xdata is None:
            return
        dx = event.xdata - self._pan_start[0]
        dy = event.ydata - self._pan_start[1]
        if abs(dx) >= 0.4 or abs(dy) >= 0.4:
            x0, x1 = self.ax.get_xlim()
            y0, y1 = self.ax.get_ylim()
            self.ax.set_xlim(x0 - dx, x1 - dx)
            self.ax.set_ylim(y0 - dy, y1 - dy)
            self._canvas.draw_idle()

    def _on_release(self, event) -> None:
        self._pan_start = None
