"""Main application window for the Neutral-Atom Reverse Compiler.

Modern three-pane layout built on CustomTkinter:

    ┌───────────────── toolbar ─────────────────┐
    │ New  Load  Undo  Redo │ Reverse  Validate │ Export
    ├────────────┬───────────────┬──────────────┤
    │  Timeline  │  Atom canvas  │  Properties   │
    ├────────────┴───────────────┴──────────────┤
    │  Output tabs: HW ops / Circuit / Valid /Log│
    ├────────────────────────────────────────────┤
    │  status bar · errors · current step        │
    └────────────────────────────────────────────┘

The heavy lifting lives in sibling modules (``state``, ``renderer``,
``video_export``, ``outputs``); this file wires them to widgets.
"""

from __future__ import annotations

import logging
import os
from tkinter import filedialog, messagebox
from typing import Optional

import customtkinter as ctk

from . import theme
from .outputs import ReverseResult, run_reverse
from .renderer import CanvasView, SceneSpec
from .state import (
    ENTANGLE_ARRAY,
    STORAGE_ARRAY,
    GuiState,
    Site,
    StateError,
    configure_entanglement,
    entangle_dims,
    entangle_pair_ok,
    instruction_atoms,
    op_kind,
    op_summary,
)
from .video_export import (
    ExportSettings,
    FFmpegNotFoundError,
    VideoExportError,
    export_animation,
    find_ffmpeg,
)

logger = logging.getLogger(__name__)


class _Log:
    """Tiny in-memory log buffer mirrored into the Logs tab."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self._sink = None

    def bind(self, sink) -> None:
        self._sink = sink

    def __call__(self, msg: str, level: str = "info") -> None:
        import datetime

        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        line = f"[{stamp}] {level.upper():5} {msg}"
        self.lines.append(line)
        logger.info(msg)
        if self._sink:
            self._sink(line)


class ReverseCompilerApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        theme.apply_appearance()
        self.title("Neutral-Atom Reverse Compiler")
        self.geometry("1560x960")
        self.minsize(1280, 800)
        self.configure(fg_color=theme.BG_APP)

        self.state_model = GuiState()
        self.log = _Log()

        # selection + view state
        self._primary_atom: Optional[int] = None
        self.selected_atoms: set[int] = set()
        self.selected_op: Optional[int] = None  # None or index into operations
        self.preview_step: int = 0
        self._drag_delta: Optional[tuple[int, int]] = None

        # replay
        self._replaying = False
        self._replay_job: Optional[str] = None
        self.replay_delay = ctk.IntVar(value=500)

        # add-operation composer state
        self.compose_mode = ctk.StringVar(value="Move")

        self._build_toolbar()
        self._build_body()
        self._build_statusbar()

        self.state_model.reset_atoms(6)
        self.preview_step = 0
        self.refresh_all()
        self.log("Ready. 6 atoms initialised in storage zone.")

        self.bind_all("<Control-z>", lambda _e: self._undo())
        self.bind_all("<Command-z>", lambda _e: self._undo())
        self.bind_all("<Control-y>", lambda _e: self._redo())
        self.bind_all("<Command-Shift-Z>", lambda _e: self._redo())

    # ============================================================== selection
    @property
    def selected_atom(self) -> Optional[int]:
        """The primary (single) selected atom, or None."""
        if self._primary_atom is not None and self._primary_atom in self.selected_atoms:
            return self._primary_atom
        if len(self.selected_atoms) == 1:
            return next(iter(self.selected_atoms))
        return None

    @selected_atom.setter
    def selected_atom(self, value: Optional[int]) -> None:
        if value is None:
            self.selected_atoms = set()
            self._primary_atom = None
        else:
            self.selected_atoms = {value}
            self._primary_atom = value

    def _set_selection(self, atoms: set[int], primary: Optional[int] = None) -> None:
        self.selected_atoms = set(atoms)
        if primary is not None and primary in self.selected_atoms:
            self._primary_atom = primary
        elif len(self.selected_atoms) == 1:
            self._primary_atom = next(iter(self.selected_atoms))
        else:
            self._primary_atom = None

    # ================================================================= toolbar
    def _build_toolbar(self) -> None:
        bar = ctk.CTkFrame(self, fg_color=theme.BG_CARD, corner_radius=0, height=58)
        bar.pack(side="top", fill="x")
        bar.pack_propagate(False)

        def tb(parent, text, cmd, primary=False, color=None):
            return ctk.CTkButton(
                parent, text=text, command=cmd, height=34, corner_radius=theme.RADIUS_SM,
                fg_color=(color or (theme.ACCENT if primary else theme.BG_CARD_ALT)),
                hover_color=(theme.ACCENT_HOVER if primary else theme.BORDER),
                text_color=(theme.TEXT_ON_ACCENT if primary else theme.TEXT),
                font=theme.FONT_BODY, border_width=0 if primary else 1,
                border_color=theme.BORDER,
            )

        title = ctk.CTkLabel(bar, text="⬡  Reverse Compiler", font=theme.FONT_TITLE,
                             text_color=theme.TEXT)
        title.pack(side="left", padx=(16, 20))

        left = ctk.CTkFrame(bar, fg_color="transparent")
        left.pack(side="left")
        tb(left, "New", self._new_project).pack(side="left", padx=4)
        tb(left, "Load", self._load_zair).pack(side="left", padx=4)
        self.btn_undo = tb(left, "↶ Undo", self._undo)
        self.btn_undo.pack(side="left", padx=4)
        self.btn_redo = tb(left, "↷ Redo", self._redo)
        self.btn_redo.pack(side="left", padx=4)

        right = ctk.CTkFrame(bar, fg_color="transparent")
        right.pack(side="right", padx=12)
        tb(right, "⤓ Export", self._open_export_dialog).pack(side="right", padx=4)
        tb(right, "Validate", self._validate).pack(side="right", padx=4)
        tb(right, "⚙ Reverse Compile", self._reverse_compile, primary=True).pack(side="right", padx=4)

    # ==================================================================== body
    def _build_body(self) -> None:
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(side="top", fill="both", expand=True, padx=10, pady=(8, 4))
        body.grid_columnconfigure(0, weight=0, minsize=290)
        body.grid_columnconfigure(1, weight=1)
        body.grid_columnconfigure(2, weight=0, minsize=330)
        body.grid_rowconfigure(0, weight=1)

        self._build_timeline(body)
        self._build_center(body)
        self._build_properties(body)

    # ------------------------------------------------------------- timeline
    def _build_timeline(self, parent) -> None:
        card = ctk.CTkFrame(parent, fg_color=theme.BG_CARD, corner_radius=theme.RADIUS)
        card.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        card.grid_rowconfigure(1, weight=1)
        card.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(card, text="Hardware Operation Timeline", font=theme.FONT_H2,
                     text_color=theme.TEXT).grid(row=0, column=0, sticky="w", padx=14, pady=(12, 6))
        self.timeline_frame = ctk.CTkScrollableFrame(card, fg_color="transparent")
        self.timeline_frame.grid(row=1, column=0, sticky="nsew", padx=6, pady=(0, 10))
        self.timeline_frame.grid_columnconfigure(0, weight=1)

    def _refresh_timeline(self) -> None:
        for w in self.timeline_frame.winfo_children():
            w.destroy()

        self._timeline_row(
            0, "INIT", op_summary({"type": "init", "init_locs": [1] * len(self.state_model.init_positions)}),
            selected=(self.selected_op == -1), op_index=-1,
        )
        for i, inst in enumerate(self.state_model.operations):
            self._timeline_row(
                i + 1, op_kind(inst), op_summary(inst),
                selected=(self.selected_op == i), op_index=i,
            )
        if not self.state_model.operations:
            ctk.CTkLabel(self.timeline_frame, text="No operations yet.\nAdd from the Properties panel →",
                         font=theme.FONT_SMALL, text_color=theme.TEXT_MUTED,
                         justify="left").grid(sticky="w", padx=12, pady=8)

    def _timeline_row(self, num: int, kind: str, summary: str, *, selected: bool, op_index: int) -> None:
        color = theme.OP_COLORS.get(kind, theme.OP_COLORS["OTHER"])
        row = ctk.CTkFrame(
            self.timeline_frame,
            fg_color=theme.ACCENT_SOFT if selected else theme.BG_CARD_ALT,
            corner_radius=theme.RADIUS_SM, border_width=1,
            border_color=theme.ACCENT if selected else theme.BORDER,
        )
        row.grid(sticky="ew", padx=4, pady=3)
        row.grid_columnconfigure(2, weight=1)

        ctk.CTkLabel(row, text=str(num), font=theme.FONT_TINY, width=22,
                     text_color=theme.TEXT_MUTED).grid(row=0, column=0, padx=(8, 2), pady=6)
        chip = ctk.CTkLabel(row, text=kind, font=(theme.FONT_FAMILY, 10, "bold"), width=52,
                            fg_color=color, text_color="#ffffff", corner_radius=6)
        chip.grid(row=0, column=1, padx=4, pady=6)
        lbl = ctk.CTkLabel(row, text=summary, font=theme.FONT_SMALL, text_color=theme.TEXT,
                           anchor="w", justify="left")
        lbl.grid(row=0, column=2, sticky="w", padx=6)

        for widget in (row, chip, lbl):
            widget.bind("<Button-1>", lambda _e, idx=op_index: self._select_op(idx))

    # --------------------------------------------------------------- center
    def _build_center(self, parent) -> None:
        wrap = ctk.CTkFrame(parent, fg_color="transparent")
        wrap.grid(row=0, column=1, sticky="nsew")
        wrap.grid_rowconfigure(0, weight=3)
        wrap.grid_rowconfigure(1, weight=2)
        wrap.grid_columnconfigure(0, weight=1)

        canvas_card = ctk.CTkFrame(wrap, fg_color=theme.BG_CARD, corner_radius=theme.RADIUS)
        canvas_card.grid(row=0, column=0, sticky="nsew", pady=(0, 8))
        canvas_card.grid_rowconfigure(1, weight=1)
        canvas_card.grid_columnconfigure(0, weight=1)

        header = ctk.CTkFrame(canvas_card, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 2))
        ctk.CTkLabel(header, text="Atom Array Canvas", font=theme.FONT_H2,
                     text_color=theme.TEXT).pack(side="left")

        ctrl = ctk.CTkFrame(header, fg_color="transparent")
        ctrl.pack(side="right")
        for text, cmd in (("⤢ Fit", self._fit_view), ("⏮", self._replay_reset),
                          ("⏭ Step", self._replay_step), ("▶ Play", self._replay_toggle)):
            ctk.CTkButton(ctrl, text=text, command=cmd, width=54, height=28,
                          corner_radius=theme.RADIUS_SM, fg_color=theme.BG_CARD_ALT,
                          hover_color=theme.BORDER, text_color=theme.TEXT,
                          border_width=1, border_color=theme.BORDER,
                          font=theme.FONT_SMALL).pack(side="left", padx=3)
        self.play_btn = ctrl.winfo_children()[-1]

        canvas_host = ctk.CTkFrame(canvas_card, fg_color=theme.BG_CARD, corner_radius=8)
        canvas_host.grid(row=1, column=0, sticky="nsew", padx=8, pady=(4, 10))
        self.canvas = CanvasView(canvas_host)
        self.canvas.widget.pack(fill="both", expand=True)
        self.canvas.set_interaction_handlers(
            on_atom=self._on_pick_atom,
            on_site=self._on_pick_site,
            on_marquee=self._on_marquee,
            on_drag_preview=self._on_drag_preview,
            on_drag_commit=self._on_drag_commit,
            on_clear=self._on_clear_selection,
        )

        self._build_output_tabs(wrap)

    def _build_output_tabs(self, parent) -> None:
        tabs = ctk.CTkTabview(parent, fg_color=theme.BG_CARD, corner_radius=theme.RADIUS,
                              segmented_button_fg_color=theme.BG_CARD_ALT,
                              segmented_button_selected_color=theme.ACCENT,
                              segmented_button_selected_hover_color=theme.ACCENT_HOVER)
        tabs.grid(row=1, column=0, sticky="nsew")
        for name in ("Hardware Operations", "Circuit Output", "Validation", "Logs"):
            tabs.add(name)

        # Hardware Operations: movement schedule + mapping history
        hw = tabs.tab("Hardware Operations")
        hw.grid_rowconfigure(0, weight=1)
        hw.grid_columnconfigure(0, weight=1)
        hw.grid_columnconfigure(1, weight=1)
        self.txt_movement = self._textbox(hw, 0, 0)
        self.txt_mapping = self._textbox(hw, 0, 1)

        # Circuit Output: nested tabs
        co = tabs.tab("Circuit Output")
        co.grid_rowconfigure(0, weight=1)
        co.grid_columnconfigure(0, weight=1)
        inner = ctk.CTkTabview(co, fg_color=theme.BG_CARD_ALT, corner_radius=8,
                               segmented_button_selected_color=theme.ACCENT,
                               segmented_button_selected_hover_color=theme.ACCENT_HOVER)
        inner.grid(row=0, column=0, sticky="nsew", padx=2, pady=2)
        for name in ("OpenQASM", "Qiskit", "Stim", "Diagram"):
            inner.add(name)
        self.txt_qasm = self._textbox(inner.tab("OpenQASM"), 0, 0, mono=True)
        self.txt_qiskit = self._textbox(inner.tab("Qiskit"), 0, 0, mono=True)
        self.txt_stim = self._textbox(inner.tab("Stim"), 0, 0, mono=True)
        diag = inner.tab("Diagram")
        diag.grid_rowconfigure(0, weight=1)
        diag.grid_columnconfigure(0, weight=1)
        self.diagram_label = ctk.CTkLabel(diag, text="Run Reverse Compile to render the circuit diagram.",
                                          text_color=theme.TEXT_MUTED, font=theme.FONT_SMALL)
        self.diagram_label.grid(row=0, column=0, sticky="nsew")
        self._diagram_image = None

        # Validation
        va = tabs.tab("Validation")
        va.grid_rowconfigure(0, weight=1)
        va.grid_columnconfigure(0, weight=1)
        self.txt_validation = self._textbox(va, 0, 0, mono=True)

        # Logs
        lo = tabs.tab("Logs")
        lo.grid_rowconfigure(0, weight=1)
        lo.grid_columnconfigure(0, weight=1)
        self.txt_logs = self._textbox(lo, 0, 0, mono=True)
        self.log.bind(lambda line: self._append_text(self.txt_logs, line + "\n"))
        self.output_tabs = tabs

    def _textbox(self, parent, r, c, *, mono: bool = False):
        parent.grid_rowconfigure(r, weight=1)
        parent.grid_columnconfigure(c, weight=1)
        box = ctk.CTkTextbox(parent, font=theme.FONT_CODE if mono else theme.FONT_SMALL,
                             fg_color=theme.BG_CARD, text_color=theme.TEXT, wrap="none",
                             corner_radius=8, border_width=1, border_color=theme.BORDER)
        box.grid(row=r, column=c, sticky="nsew", padx=4, pady=4)
        box.configure(state="disabled")
        return box

    def _set_text(self, box, content: str) -> None:
        box.configure(state="normal")
        box.delete("1.0", "end")
        box.insert("1.0", content)
        box.configure(state="disabled")

    def _append_text(self, box, content: str) -> None:
        box.configure(state="normal")
        box.insert("end", content)
        box.see("end")
        box.configure(state="disabled")

    # ------------------------------------------------------------ properties
    def _build_properties(self, parent) -> None:
        card = ctk.CTkFrame(parent, fg_color=theme.BG_CARD, corner_radius=theme.RADIUS)
        card.grid(row=0, column=2, sticky="nsew", padx=(8, 0))
        card.grid_rowconfigure(1, weight=1)
        card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(card, text="Properties", font=theme.FONT_H2,
                     text_color=theme.TEXT).grid(row=0, column=0, sticky="w", padx=14, pady=(12, 6))
        self.props = ctk.CTkScrollableFrame(card, fg_color="transparent")
        self.props.grid(row=1, column=0, sticky="nsew", padx=6, pady=(0, 10))
        self.props.grid_columnconfigure(0, weight=1)

    def _refresh_properties(self) -> None:
        for w in self.props.winfo_children():
            w.destroy()
        if len(self.selected_atoms) > 1:
            self._props_selection(sorted(self.selected_atoms))
        elif self.selected_atom is not None:
            self._props_atom(self.selected_atom)
        elif self.selected_op is not None and self.selected_op >= 0:
            self._props_op(self.selected_op)
        else:
            self._props_init()
        self._props_add_operation()

    # ---- property section helpers ----
    def _section(self, title: str) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(self.props, fg_color=theme.BG_CARD_ALT, corner_radius=theme.RADIUS_SM)
        frame.grid(sticky="ew", padx=4, pady=(4, 8))
        frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(frame, text=title, font=theme.FONT_H2, text_color=theme.TEXT,
                     anchor="w").grid(sticky="ew", padx=10, pady=(8, 2))
        return frame

    def _field_row(self, parent, label: str, var, values: Optional[list[str]] = None):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.grid(sticky="ew", padx=10, pady=3)
        row.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(row, text=label, font=theme.FONT_SMALL, text_color=theme.TEXT_MUTED,
                     width=64, anchor="w").grid(row=0, column=0, sticky="w")
        if values is not None:
            w = ctk.CTkOptionMenu(row, values=values, variable=var, height=30,
                                  fg_color=theme.BG_CARD, button_color=theme.ACCENT,
                                  button_hover_color=theme.ACCENT_HOVER, text_color=theme.TEXT,
                                  font=theme.FONT_SMALL)
        else:
            w = ctk.CTkEntry(row, textvariable=var, height=30, fg_color=theme.BG_CARD,
                             text_color=theme.TEXT, font=theme.FONT_SMALL,
                             border_color=theme.BORDER)
        w.grid(row=0, column=1, sticky="ew", padx=(8, 0))
        return w

    def _action(self, parent, text, cmd, *, color=None, primary=False, danger=False):
        fg = theme.ACCENT if primary else (theme.DANGER if danger else (color or theme.BG_CARD))
        hover = theme.ACCENT_HOVER if primary else (theme.DANGER_SOFT if danger else theme.BORDER)
        tc = theme.TEXT_ON_ACCENT if (primary or danger) else theme.TEXT
        return ctk.CTkButton(parent, text=text, command=cmd, height=32,
                             corner_radius=theme.RADIUS_SM, fg_color=fg, hover_color=hover,
                             text_color=tc, font=theme.FONT_SMALL, border_width=0 if (primary or danger) else 1,
                             border_color=theme.BORDER)

    def _props_init(self) -> None:
        sec = self._section("Initial Layout")
        ctk.CTkLabel(sec, text=f"{len(self.state_model.init_positions)} atoms placed in storage",
                     font=theme.FONT_SMALL, text_color=theme.TEXT_MUTED,
                     anchor="w").grid(sticky="ew", padx=10, pady=2)
        self.var_atom_count = ctk.StringVar(value=str(self.state_model.atom_count or 6))
        self._field_row(sec, "Atoms", self.var_atom_count)
        btns = ctk.CTkFrame(sec, fg_color="transparent")
        btns.grid(sticky="ew", padx=6, pady=(4, 10))
        btns.grid_columnconfigure((0, 1), weight=1)
        self._action(btns, "Reset Layout", self._reset_layout, primary=True).grid(row=0, column=0, sticky="ew", padx=4)
        self._action(btns, "Clear Ops", self._clear_ops).grid(row=0, column=1, sticky="ew", padx=4)
        ctk.CTkLabel(sec, text="Tip: click an empty trap on the canvas to place the\nselected atom there.",
                     font=theme.FONT_TINY, text_color=theme.TEXT_MUTED,
                     justify="left").grid(sticky="w", padx=10, pady=(0, 8))

        # Entanglement-zone tweezer array (customizable).
        ent_rows, ent_cols = entangle_dims()
        esec = self._section("Entanglement Zone (tweezer array)")
        self.var_ent_rows = ctk.StringVar(value=str(ent_rows))
        self.var_ent_cols = ctk.StringVar(value=str(ent_cols))
        self._field_row(esec, "Rows", self.var_ent_rows)
        self._field_row(esec, "Cols", self.var_ent_cols)
        self._action(esec, "Apply Zone Size", self._apply_entangle_zone, primary=True).grid(
            sticky="ew", padx=10, pady=(4, 6))
        ctk.CTkLabel(esec, text="Storage stays 40 × 20. Columns must be even so atoms\n"
                               "pair as adjacent columns {2k, 2k+1} for CZ/CX.",
                     font=theme.FONT_TINY, text_color=theme.TEXT_MUTED,
                     justify="left").grid(sticky="w", padx=10, pady=(0, 8))

    def _apply_entangle_zone(self) -> None:
        try:
            rows = int(self.var_ent_rows.get())
            cols = int(self.var_ent_cols.get())
        except ValueError:
            self._status("Entanglement rows/cols must be integers.", error=True)
            return
        # Changing hardware geometry can invalidate operations that reference the
        # entanglement zone, so require a clean operation list.
        if self.state_model.operations and not messagebox.askyesno(
            "Resize entanglement zone",
            "Changing the entanglement zone clears all operations. Continue?"):
            return
        try:
            configure_entanglement(rows, cols)
        except StateError as exc:
            self._status(str(exc), error=True)
            return
        self.state_model.clear_operations()
        # Drop any init atoms that no longer fit the (unchanged) storage zone — none
        # normally — and refresh the view to the new geometry.
        self.selected_atom = None
        self.selected_op = None
        self.preview_step = 0
        self.refresh_all()
        self.canvas.fit_view()
        self._status(f"Entanglement zone set to {cols} × {rows}.")


    def _props_atom(self, atom: int) -> None:
        positions = self.state_model.positions_at(self.preview_step)
        site = positions.get(atom)
        sec = self._section(f"Atom {atom}")
        loc = f"(a{site.array}, r{site.row}, c{site.col})" if site else "—"
        zone = "Storage" if (site and site.array == STORAGE_ARRAY) else "Entanglement"
        ctk.CTkLabel(sec, text=f"Zone: {zone}\nPosition: {loc}", font=theme.FONT_SMALL,
                     text_color=theme.TEXT_MUTED, justify="left",
                     anchor="w").grid(sticky="ew", padx=10, pady=2)
        pick = ctk.CTkFrame(sec, fg_color="transparent")
        pick.grid(sticky="ew", padx=6, pady=(2, 8))
        pick.grid_columnconfigure((0, 1), weight=1)
        self._action(pick, "Select Row", lambda: self._select_line(atom, "row")).grid(
            row=0, column=0, sticky="ew", padx=4)
        self._action(pick, "Select Column", lambda: self._select_line(atom, "col")).grid(
            row=0, column=1, sticky="ew", padx=4)

        move_sec = self._section("Move Atom")
        self.var_mv_zone = ctk.StringVar(value=str(site.array if site else 0))
        self.var_mv_row = ctk.StringVar(value=str(site.row if site else 0))
        self.var_mv_col = ctk.StringVar(value=str(site.col if site else 0))
        self._field_row(move_sec, "Zone", self.var_mv_zone, values=["0", "1"])
        self._field_row(move_sec, "Row", self.var_mv_row)
        self._field_row(move_sec, "Col", self.var_mv_col)
        self._action(move_sec, "Apply Move", lambda: self._apply_move(atom), primary=True).grid(
            sticky="ew", padx=10, pady=(4, 6))

        gate_sec = self._section("Add Gate on this Atom")
        self.var_g1 = ctk.StringVar(value="h")
        self.var_g1p = ctk.StringVar(value="")
        self._field_row(gate_sec, "Gate", self.var_g1,
                        values=["h", "x", "y", "z", "s", "sdg", "t", "tdg", "rx", "ry", "rz"])
        self._field_row(gate_sec, "Param", self.var_g1p)
        self._action(gate_sec, "Add 1Q Gate", lambda: self._add_1q(atom)).grid(sticky="ew", padx=10, pady=(4, 6))

        if not self.state_model.operations:
            self._action(self._section("Danger"), "Remove Atom",
                         lambda: self._remove_atom(atom), danger=True).grid(sticky="ew", padx=10, pady=(2, 8))

    def _props_selection(self, atoms: list[int]) -> None:
        sec = self._section(f"Selection · {len(atoms)} atoms")
        preview = ", ".join(str(a) for a in atoms[:12]) + (" …" if len(atoms) > 12 else "")
        ctk.CTkLabel(sec, text=f"Atoms: {preview}", font=theme.FONT_SMALL,
                     text_color=theme.TEXT_MUTED, anchor="w", justify="left",
                     wraplength=280).grid(sticky="ew", padx=10, pady=2)
        ctk.CTkLabel(sec, text="Tip: drag any selected atom on the canvas to move the\n"
                               "whole selection. Shift-drag a box to select a row/column;\n"
                               "Shift-click toggles individual atoms.",
                     font=theme.FONT_TINY, text_color=theme.TEXT_MUTED,
                     justify="left").grid(sticky="w", padx=10, pady=(0, 6))

        # Parallel move
        msec = self._section("Parallel Move (AOD)")
        self.var_sel_zone = ctk.StringVar(value="(same)")
        self.var_sel_drow = ctk.StringVar(value="0")
        self.var_sel_dcol = ctk.StringVar(value="0")
        self._field_row(msec, "To Zone", self.var_sel_zone, values=["(same)", "0", "1"])
        self._field_row(msec, "Δ Row", self.var_sel_drow)
        self._field_row(msec, "Δ Col", self.var_sel_dcol)
        self._action(msec, "Move Selection", lambda: self._selection_parallel_move(atoms),
                     primary=True).grid(sticky="ew", padx=10, pady=(4, 6))

        # Parallel 1Q on all
        gsec = self._section("1Q Gate on All")
        self.var_sel_gate = ctk.StringVar(value="h")
        self.var_sel_param = ctk.StringVar(value="")
        self._field_row(gsec, "Gate", self.var_sel_gate,
                        values=["h", "x", "y", "z", "s", "sdg", "t", "tdg", "rx", "ry", "rz"])
        self._field_row(gsec, "Param", self.var_sel_param)
        self._action(gsec, "Add 1Q to All (parallel)",
                     lambda: self._selection_add_1q(atoms), primary=True).grid(
            sticky="ew", padx=10, pady=(4, 6))

        # 2Q when exactly two
        if len(atoms) == 2:
            qsec = self._section("2Q Gate on Pair")
            self.var_sel_2q = ctk.StringVar(value="cz")
            self._field_row(qsec, "Gate", self.var_sel_2q, values=["cz", "cx", "swap"])
            self._action(qsec, f"Add 2Q on {atoms[0]},{atoms[1]}",
                         lambda: self._selection_add_2q(atoms), primary=True).grid(
                sticky="ew", padx=10, pady=(4, 6))

        # Entangle all valid row pairs among the selected atoms (any count)
        positions = self.state_model.positions_at(self.preview_step)
        if any(positions.get(a) and positions[a].array == ENTANGLE_ARRAY for a in atoms):
            esec = self._section("Entangle Row Pairs")
            self.var_sel_ent = ctk.StringVar(value="cz")
            self._field_row(esec, "Gate", self.var_sel_ent, values=["cz", "cx"])
            self._action(esec, "⚡ Entangle Selected Row Pairs",
                         lambda: self._entangle_all(self.var_sel_ent.get(), set(atoms)),
                         primary=True).grid(sticky="ew", padx=10, pady=(4, 6))

        # Measure all + clear
        asec = self._section("More")
        grid = ctk.CTkFrame(asec, fg_color="transparent")
        grid.grid(sticky="ew", padx=6, pady=(2, 8))
        grid.grid_columnconfigure((0, 1), weight=1)
        self._action(grid, "Measure All", lambda: self._selection_measure(atoms)).grid(
            row=0, column=0, sticky="ew", padx=4)
        self._action(grid, "Clear Selection", self._on_clear_selection).grid(
            row=0, column=1, sticky="ew", padx=4)

    def _selection_parallel_move(self, atoms: list[int]) -> None:
        try:
            drow = int(self.var_sel_drow.get())
            dcol = int(self.var_sel_dcol.get())
        except ValueError:
            self._status("Δrow/Δcol must be integers.", error=True)
            return
        zone = self.var_sel_zone.get()
        target_array = None if zone == "(same)" else int(zone)
        try:
            self.state_model.add_parallel_move(atoms, drow, dcol, target_array=target_array)
        except StateError as exc:
            self._status(str(exc), error=True)
            return
        dest = "" if target_array is None else f" → zone {target_array}"
        self._after_add(f"Parallel move of {len(atoms)} atoms by (Δr={drow}, Δc={dcol}){dest}.")

    def _selection_add_1q(self, atoms: list[int]) -> None:
        gate = self.var_sel_gate.get()
        param = self._parse_param(self.var_sel_param.get())
        try:
            for i, a in enumerate(atoms):
                self.state_model.add_1q(a, gate, param, parallel=(i > 0))
        except StateError as exc:
            self._status(str(exc), error=True)
            self.refresh_all()
            return
        self._after_add(f"Added parallel 1Q {gate.upper()} on {len(atoms)} atoms.")

    def _selection_add_2q(self, atoms: list[int]) -> None:
        gate = self.var_sel_2q.get()
        try:
            self.state_model.add_2q(gate, atoms[0], atoms[1])
        except StateError as exc:
            self._status(str(exc), error=True)
            return
        self._after_add(f"Added 2Q {gate.upper()} on atoms {atoms[0]}, {atoms[1]}.")

    def _selection_measure(self, atoms: list[int]) -> None:
        try:
            self.state_model.add_measure(atoms)
        except StateError as exc:
            self._status(str(exc), error=True)
            return
        self._after_add(f"Measured {len(atoms)} atoms.")

    def _props_op(self, index: int) -> None:
        inst = self.state_model.operations[index]
        kind = op_kind(inst)
        sec = self._section(f"Operation #{index + 1} · {kind}")
        ctk.CTkLabel(sec, text=op_summary(inst), font=theme.FONT_SMALL, text_color=theme.TEXT_MUTED,
                     anchor="w", justify="left", wraplength=280).grid(sticky="ew", padx=10, pady=2)

        # Editable fields per kind
        if kind == "1Q":
            g = inst.get("gates", [{}])[0]
            self.var_e_gate = ctk.StringVar(value=str(g.get("name", "h")))
            self.var_e_param = ctk.StringVar(value=str(g.get("params", [""])[0]) if g.get("params") else "")
            self.var_e_atom = ctk.StringVar(value=str(g.get("q", 0)))
            self._field_row(sec, "Atom", self.var_e_atom)
            self._field_row(sec, "Gate", self.var_e_gate,
                            values=["h", "x", "y", "z", "s", "sdg", "t", "tdg", "rx", "ry", "rz"])
            self._field_row(sec, "Param", self.var_e_param)
            self._action(sec, "Apply Edit", lambda: self._edit_1q(index), primary=True).grid(
                sticky="ew", padx=10, pady=(4, 6))
        elif kind == "2Q":
            g = inst.get("gates", [{}])[0]
            self.var_e_gate = ctk.StringVar(value=str(g.get("name", "cz")))
            self.var_e_a0 = ctk.StringVar(value=str(g.get("q0", 0)))
            self.var_e_a1 = ctk.StringVar(value=str(g.get("q1", 1)))
            self._field_row(sec, "Gate", self.var_e_gate, values=["cz", "cx"])
            self._field_row(sec, "Atom A", self.var_e_a0)
            self._field_row(sec, "Atom B", self.var_e_a1)
            self._action(sec, "Apply Edit", lambda: self._edit_2q(index), primary=True).grid(
                sticky="ew", padx=10, pady=(4, 6))
        elif kind == "MOVE":
            eps = self.state_model.move_endpoints(index)
            if eps:
                atom, src, dst = eps[0]
                self.var_e_atom = ctk.StringVar(value=str(atom))
                self.var_e_zone = ctk.StringVar(value=str(dst.array))
                self.var_e_row = ctk.StringVar(value=str(dst.row))
                self.var_e_col = ctk.StringVar(value=str(dst.col))
                self._field_row(sec, "Atom", self.var_e_atom)
                self._field_row(sec, "Zone", self.var_e_zone, values=["0", "1"])
                self._field_row(sec, "Row", self.var_e_row)
                self._field_row(sec, "Col", self.var_e_col)
                self._action(sec, "Apply Edit", lambda: self._edit_move(index), primary=True).grid(
                    sticky="ew", padx=10, pady=(4, 6))

        arrange = self._section("Arrange")
        grid = ctk.CTkFrame(arrange, fg_color="transparent")
        grid.grid(sticky="ew", padx=6, pady=(2, 8))
        grid.grid_columnconfigure((0, 1), weight=1)
        self._action(grid, "▲ Up", lambda: self._reorder(index, -1)).grid(row=0, column=0, sticky="ew", padx=4, pady=3)
        self._action(grid, "▼ Down", lambda: self._reorder(index, 1)).grid(row=0, column=1, sticky="ew", padx=4, pady=3)
        self._action(grid, "⧉ Duplicate", lambda: self._duplicate(index)).grid(row=1, column=0, sticky="ew", padx=4, pady=3)
        self._action(grid, "▷ Play to here", lambda: self._play_to(index)).grid(row=1, column=1, sticky="ew", padx=4, pady=3)
        self._action(arrange, "🗑 Delete", lambda: self._delete(index), danger=True).grid(
            sticky="ew", padx=10, pady=(2, 8))

    def _props_add_operation(self) -> None:
        sec = self._section("Add Operation")
        seg = ctk.CTkSegmentedButton(sec, values=["Move", "1Q", "2Q", "Measure"],
                                     variable=self.compose_mode, command=lambda _v: self._refresh_compose(),
                                     selected_color=theme.ACCENT, selected_hover_color=theme.ACCENT_HOVER,
                                     fg_color=theme.BG_CARD, unselected_color=theme.BG_CARD,
                                     text_color=theme.TEXT, font=theme.FONT_SMALL)
        seg.grid(sticky="ew", padx=10, pady=(2, 6))
        self.compose_body = ctk.CTkFrame(sec, fg_color="transparent")
        self.compose_body.grid(sticky="ew", padx=0, pady=(0, 8))
        self.compose_body.grid_columnconfigure(0, weight=1)
        self._refresh_compose()

    def _refresh_compose(self) -> None:
        for w in self.compose_body.winfo_children():
            w.destroy()
        mode = self.compose_mode.get()
        body = self.compose_body
        atoms = [str(a) for a in sorted(self.state_model.positions_at(self.preview_step))]
        default = atoms[0] if atoms else "0"
        self.c_parallel = ctk.BooleanVar(value=False)

        if mode == "Move":
            if not hasattr(self, "move_submode"):
                self.move_submode = ctk.StringVar(value="Single")
            seg = ctk.CTkSegmentedButton(
                body, values=["Single", "Parallel (AOD)"],
                command=lambda v: (self.move_submode.set(v), self._refresh_move_form()),
                selected_color=theme.ACCENT, selected_hover_color=theme.ACCENT_HOVER,
                fg_color=theme.BG_CARD, unselected_color=theme.BG_CARD,
                text_color=theme.TEXT, font=theme.FONT_SMALL)
            val = "Parallel (AOD)" if self.move_submode.get().startswith("Parallel") else "Single"
            seg.set(val)
            seg.grid(sticky="ew", padx=10, pady=(2, 6))
            self.move_form = ctk.CTkFrame(body, fg_color="transparent")
            self.move_form.grid(sticky="ew")
            self.move_form.grid_columnconfigure(0, weight=1)
            self._refresh_move_form()
        elif mode == "1Q":
            self.c_atom = ctk.StringVar(value=default)
            self.c_gate = ctk.StringVar(value="h")
            self.c_param = ctk.StringVar(value="")
            self._field_row(body, "Atom", self.c_atom, values=atoms or ["0"])
            self._field_row(body, "Gate", self.c_gate,
                            values=["h", "x", "y", "z", "s", "sdg", "t", "tdg", "rx", "ry", "rz"])
            self._field_row(body, "Param", self.c_param)
            self._parallel_checkbox(body, "1Q")
            self._action(body, "Add 1Q Gate", self._compose_1q, primary=True).grid(sticky="ew", padx=10, pady=6)
        elif mode == "2Q":
            self.c_gate = ctk.StringVar(value="cz")
            self.c_a0 = ctk.StringVar(value=atoms[0] if atoms else "0")
            self.c_a1 = ctk.StringVar(value=atoms[1] if len(atoms) > 1 else default)
            self._field_row(body, "Gate", self.c_gate, values=["cz", "cx", "swap"])
            self._field_row(body, "Atom A", self.c_a0, values=atoms or ["0"])
            self._field_row(body, "Atom B", self.c_a1, values=atoms or ["0"])
            self._parallel_checkbox(body, "2Q")
            self._action(body, "Add 2Q Gate", self._compose_2q, primary=True).grid(sticky="ew", padx=10, pady=6)
            self._action(body, "⚡ Entangle All Row Pairs",
                         lambda: self._entangle_all(self.c_gate.get())).grid(
                sticky="ew", padx=10, pady=(0, 6))
            ctk.CTkLabel(body, text="CZ/CX need both atoms in the entanglement zone,\n"
                                    "same row, adjacent columns {2k, 2k+1}.\n"
                                    "“Entangle All Row Pairs” applies one parallel layer.",
                         font=theme.FONT_TINY, text_color=theme.TEXT_MUTED,
                         justify="left").grid(sticky="w", padx=10, pady=(0, 4))
        elif mode == "Measure":
            self.c_atom = ctk.StringVar(value=default)
            self._field_row(body, "Atom", self.c_atom, values=atoms or ["0"])
            self._action(body, "Add Measure", self._compose_measure, primary=True).grid(sticky="ew", padx=10, pady=6)

    def _parallel_checkbox(self, parent, kind: str) -> None:
        last = self.state_model.operations[-1] if self.state_model.operations else None
        can_merge = last is not None and op_kind(last) == kind
        hint = "merges into the previous op" if can_merge else "previous op differs → new layer"
        cb = ctk.CTkCheckBox(parent, text="⇉ Parallel with previous", variable=self.c_parallel,
                             font=theme.FONT_SMALL, text_color=theme.TEXT,
                             fg_color=theme.ACCENT, hover_color=theme.ACCENT_HOVER,
                             checkbox_width=18, checkbox_height=18)
        cb.grid(sticky="w", padx=10, pady=(6, 0))
        ctk.CTkLabel(parent, text=hint, font=theme.FONT_TINY,
                     text_color=theme.TEXT_MUTED).grid(sticky="w", padx=34, pady=(0, 2))

    def _refresh_move_form(self) -> None:
        for w in self.move_form.winfo_children():
            w.destroy()
        body = self.move_form
        atoms = [str(a) for a in sorted(self.state_model.positions_at(self.preview_step))]
        default = atoms[0] if atoms else "0"
        if self.move_submode.get().startswith("Parallel"):
            preset = str(self.selected_atom) if self.selected_atom is not None else ""
            self.c_atoms = ctk.StringVar(value=preset)
            self.c_pzone = ctk.StringVar(value="(same)")
            self.c_drow = ctk.StringVar(value="0")
            self.c_dcol = ctk.StringVar(value="0")
            self._field_row(body, "Atoms", self.c_atoms)
            self._field_row(body, "To Zone", self.c_pzone, values=["(same)", "0", "1"])
            self._field_row(body, "Δ Row", self.c_drow)
            self._field_row(body, "Δ Col", self.c_dcol)
            self._action(body, "Add Parallel Move", self._compose_parallel_move,
                         primary=True).grid(sticky="ew", padx=10, pady=6)
            ctk.CTkLabel(body,
                         text="AOD moves any m rows × n columns of atoms together\n"
                              "(rows/columns need NOT be contiguous). Comma-separated\n"
                              "atoms + a shared (Δrow, Δcol); set To Zone to cross zones.",
                         font=theme.FONT_TINY, text_color=theme.TEXT_MUTED,
                         justify="left").grid(sticky="w", padx=10, pady=(0, 4))
        else:
            self.c_atom = ctk.StringVar(value=str(self.selected_atom) if self.selected_atom is not None else default)
            self.c_zone = ctk.StringVar(value="0")
            self.c_row = ctk.StringVar(value="0")
            self.c_col = ctk.StringVar(value="0")
            self._field_row(body, "Atom", self.c_atom, values=atoms or ["0"])
            self._field_row(body, "Zone", self.c_zone, values=["0", "1"])
            self._field_row(body, "Row", self.c_row)
            self._field_row(body, "Col", self.c_col)
            self._action(body, "Add Move", self._compose_move, primary=True).grid(sticky="ew", padx=10, pady=6)

    def _compose_parallel_move(self) -> None:
        raw = self.c_atoms.get().replace(" ", "")
        if not raw:
            self._status("Enter one or more atom IDs (comma-separated).", error=True)
            return
        try:
            atoms = [int(tok) for tok in raw.split(",") if tok]
            drow = int(self.c_drow.get())
            dcol = int(self.c_dcol.get())
        except ValueError:
            self._status("Atoms and Δrow/Δcol must be integers.", error=True)
            return
        zone = self.c_pzone.get()
        target_array = None if zone == "(same)" else int(zone)
        try:
            self.state_model.add_parallel_move(atoms, drow, dcol, target_array=target_array)
        except StateError as exc:
            self._status(str(exc), error=True)
            return
        dest = "" if target_array is None else f" → zone {target_array}"
        self._after_add(f"Parallel move of {len(atoms)} atoms by (Δr={drow}, Δc={dcol}){dest}.")

    # ================================================================ statusbar
    def _build_statusbar(self) -> None:
        bar = ctk.CTkFrame(self, fg_color=theme.BG_CARD, corner_radius=0, height=34)
        bar.pack(side="bottom", fill="x")
        bar.pack_propagate(False)
        self.status_var = ctk.StringVar(value="Ready")
        self.error_var = ctk.StringVar(value="")
        self.step_var = ctk.StringVar(value="Step 0 / 0")
        ctk.CTkLabel(bar, textvariable=self.status_var, font=theme.FONT_SMALL,
                     text_color=theme.TEXT).pack(side="left", padx=14)
        self.error_label = ctk.CTkLabel(bar, textvariable=self.error_var, font=theme.FONT_SMALL,
                                        text_color=theme.DANGER)
        self.error_label.pack(side="left", padx=14)
        ctk.CTkLabel(bar, textvariable=self.step_var, font=theme.FONT_SMALL,
                     text_color=theme.TEXT_MUTED).pack(side="right", padx=14)

    def _status(self, msg: str, *, error: bool = False) -> None:
        if error:
            self.error_var.set(msg)
            self.status_var.set("Error")
            self.log(msg, level="error")
        else:
            self.error_var.set("")
            self.status_var.set(msg)
            self.log(msg)

    # ================================================================== render
    def refresh_all(self) -> None:
        self.preview_step = max(0, min(self.preview_step, len(self.state_model.operations)))
        self._refresh_timeline()
        self._refresh_properties()
        self._render_canvas()
        self._refresh_hw_tabs()
        self.step_var.set(f"Step {self.preview_step} / {len(self.state_model.operations)}")
        self.btn_undo.configure(state="normal" if self.state_model.can_undo() else "disabled")
        self.btn_redo.configure(state="normal" if self.state_model.can_redo() else "disabled")

    def _render_canvas(self, *, moves=None, progress: float = 1.0) -> None:
        step = self.preview_step
        positions = self.state_model.positions_at(step)
        highlight: set[int] = set(self.selected_atoms)
        entangle_pairs: list[tuple[int, int]] = []
        title = "Initial layout" if step == 0 else f"After step {step}"
        subtitle = ""
        move_specs = moves or []

        applied = step - 1
        if 0 <= applied < len(self.state_model.operations):
            inst = self.state_model.operations[applied]
            kind = op_kind(inst)
            highlight |= instruction_atoms(inst)
            title = f"Step {step}: {kind}"
            if kind == "MOVE" and not move_specs:
                for atom, src, dst in self.state_model.move_endpoints(applied):
                    move_specs.append((atom, src, dst, 1.0))
                subtitle = "Atom movement (hardware schedule, not a gate)"
            elif kind == "2Q":
                for g in inst.get("gates", []):
                    entangle_pairs.append((int(g["q0"]), int(g["q1"])))
                subtitle = "Two-qubit entangling gate"
            elif kind == "SWAP":
                qs = inst.get("qubits", [])
                if len(qs) >= 2:
                    entangle_pairs.append((int(qs[0]), int(qs[1])))

        spec = SceneSpec(positions=positions, highlight=highlight, moves=move_specs,
                         entangle_pairs=entangle_pairs, title=title, subtitle=subtitle)
        self.canvas.render(spec)

    def _refresh_hw_tabs(self) -> None:
        from .outputs import _mapping_history_text, _movement_schedule_text

        self._set_text(self.txt_movement, _movement_schedule_text(self.state_model))
        self._set_text(self.txt_mapping, _mapping_history_text(self.state_model))

    # ================================================================ selection
    def _select_op(self, index: int) -> None:
        self._stop_replay()
        self.selected_atom = None
        self.selected_op = index
        if index == -1:
            self.preview_step = 0
        else:
            self.preview_step = index + 1
        self.refresh_all()

    def _on_pick_atom(self, atom: int, additive: bool = False) -> None:
        self._stop_replay()
        self.selected_op = None
        if additive:
            sel = set(self.selected_atoms)
            if atom in sel:
                sel.discard(atom)
            else:
                sel.add(atom)
            self._set_selection(sel, primary=atom if atom in sel else None)
        else:
            self._set_selection({atom}, primary=atom)
        self.refresh_all()
        n = len(self.selected_atoms)
        self._status(f"Selected atom {atom}" if n <= 1 else f"{n} atoms selected")

    def _on_marquee(self, atoms: set[int], additive: bool = False) -> None:
        self._stop_replay()
        self.selected_op = None
        sel = set(self.selected_atoms) if additive else set()
        sel |= set(atoms)
        self._set_selection(sel)
        self.refresh_all()
        self._status(f"{len(sel)} atom(s) selected" if sel else "Selection cleared")

    def _on_clear_selection(self) -> None:
        if not self.selected_atoms and self.selected_op is None:
            return
        self._set_selection(set())
        self.selected_op = None
        self._drag_delta = None
        self.refresh_all()

    def _select_line(self, atom: int, axis: str) -> None:
        """Select every atom sharing ``atom``'s AOD row (or column)."""
        self._stop_replay()
        positions = self.state_model.positions_at(self.preview_step)
        ref = positions.get(atom)
        if ref is None:
            return
        if axis == "row":
            sel = {a for a, p in positions.items() if p.array == ref.array and p.row == ref.row}
        else:
            sel = {a for a, p in positions.items() if p.array == ref.array and p.col == ref.col}
        self.selected_op = None
        self._set_selection(sel, primary=atom)
        self.refresh_all()
        self._status(f"Selected {len(sel)} atoms in {axis} ({'r' if axis=='row' else 'c'}"
                     f"{ref.row if axis=='row' else ref.col}).")

    def _on_pick_site(self, site: Site) -> None:
        # Clicking an empty trap moves the single selected atom there.
        if len(self.selected_atoms) == 1:
            self._apply_move_to(next(iter(self.selected_atoms)), site)
        elif not self.selected_atoms:
            self._on_clear_selection()

    def _on_drag_preview(self, anchor: int, target: Site) -> None:
        # Live preview of dragging the selection so the anchor lands on ``target``
        # (which may be in a different zone).
        if target is None:
            return
        positions = self.state_model.positions_at(self.preview_step)
        src0 = positions.get(anchor)
        if src0 is None:
            return
        drow = target.row - src0.row
        dcol = target.col - src0.col
        arr = target.array
        if drow == 0 and dcol == 0 and arr == src0.array:
            self._drag_delta = None
            self.refresh_all()
            return
        atoms = self.selected_atoms if anchor in self.selected_atoms else {anchor}
        self._drag_delta = (drow, dcol)
        moves = []
        for a in atoms:
            src = positions.get(a)
            if src is None:
                continue
            dst = Site(arr, src.row + drow, src.col + dcol)
            moves.append((a, src, dst, 1.0))
        zone = "storage" if arr == STORAGE_ARRAY else "entanglement"
        spec = SceneSpec(positions=positions, highlight=set(atoms), moves=moves,
                         title="Drag to move", subtitle=f"→ {zone} (Δr={drow}, Δc={dcol})")
        self.canvas.render(spec)

    def _on_drag_commit(self, anchor: int, target: Site) -> None:
        self._drag_delta = None
        if target is None:
            self.refresh_all()
            return
        positions = self.state_model.positions_at(len(self.state_model.operations))
        src0 = positions.get(anchor)
        if src0 is None:
            return
        drow = target.row - src0.row
        dcol = target.col - src0.col
        arr = target.array
        if drow == 0 and dcol == 0 and arr == src0.array:
            self._on_pick_atom(anchor, additive=False)
            return
        in_sel = anchor in self.selected_atoms
        atoms = sorted(self.selected_atoms) if in_sel else [anchor]
        if not in_sel:
            self._set_selection({anchor}, primary=anchor)
        try:
            if len(atoms) == 1:
                self.state_model.add_move(atoms[0], Site(arr, src0.row + drow, src0.col + dcol))
            else:
                self.state_model.add_parallel_move(atoms, drow, dcol, target_array=arr)
        except StateError as exc:
            self._status(str(exc), error=True)
            self.refresh_all()
            return
        self.selected_op = len(self.state_model.operations) - 1
        self.preview_step = len(self.state_model.operations)
        self.refresh_all()
        zone = "storage" if arr == STORAGE_ARRAY else "entanglement"
        label = "atom" if len(atoms) == 1 else f"{len(atoms)} atoms (parallel)"
        self._status(f"Moved {label} to {zone} (Δr={drow}, Δc={dcol}).")

    # ================================================================ actions
    def _reset_layout(self) -> None:
        try:
            count = int(self.var_atom_count.get())
        except ValueError:
            self._status("Atom count must be an integer.", error=True)
            return
        self._stop_replay()
        self.state_model.reset_atoms(count)
        self.selected_atom = None
        self.selected_op = None
        self.preview_step = 0
        self.refresh_all()
        self._status(f"Reset layout to {len(self.state_model.init_positions)} atoms.")

    def _clear_ops(self) -> None:
        if self.state_model.operations and not messagebox.askyesno(
            "Clear operations", "Remove all operations and return atoms to init positions?"):
            return
        self.state_model.clear_operations()
        self.selected_op = None
        self.preview_step = 0
        self.refresh_all()
        self._status("Cleared all operations.")

    def _remove_atom(self, atom: int) -> None:
        try:
            self.state_model.remove_atom(atom)
        except StateError as exc:
            self._status(str(exc), error=True)
            return
        self.selected_atom = None
        self.refresh_all()
        self._status(f"Removed atom {atom}.")

    def _apply_move(self, atom: int) -> None:
        try:
            site = Site(int(self.var_mv_zone.get()), int(self.var_mv_row.get()), int(self.var_mv_col.get()))
        except ValueError:
            self._status("Zone/row/col must be integers.", error=True)
            return
        self._apply_move_to(atom, site)

    def _apply_move_to(self, atom: int, dst: Site, *, parallel: bool = False) -> None:
        try:
            self.state_model.add_move(atom, dst, parallel=parallel)
        except StateError as exc:
            self._status(str(exc), error=True)
            return
        self.selected_op = len(self.state_model.operations) - 1
        self.selected_atom = None
        self.preview_step = len(self.state_model.operations)
        self.refresh_all()
        self._status(f"Moved atom {atom} → (a{dst.array}, r{dst.row}, c{dst.col}).")

    def _add_1q(self, atom: int) -> None:
        gate = self.var_g1.get()
        param = self._parse_param(self.var_g1p.get())
        try:
            self.state_model.add_1q(atom, gate, param)
        except StateError as exc:
            self._status(str(exc), error=True)
            return
        self._after_add(f"Added 1Q {gate.upper()} on atom {atom}.")

    def _compose_move(self) -> None:
        try:
            atom = int(self.c_atom.get())
            site = Site(int(self.c_zone.get()), int(self.c_row.get()), int(self.c_col.get()))
        except ValueError:
            self._status("Atom/zone/row/col must be integers.", error=True)
            return
        self._apply_move_to(atom, site, parallel=self.c_parallel.get())

    def _compose_1q(self) -> None:
        try:
            atom = int(self.c_atom.get())
        except ValueError:
            self._status("Atom must be an integer.", error=True)
            return
        gate = self.c_gate.get()
        param = self._parse_param(self.c_param.get())
        try:
            self.state_model.add_1q(atom, gate, param, parallel=self.c_parallel.get())
        except StateError as exc:
            self._status(str(exc), error=True)
            return
        self._after_add(f"Added 1Q {gate.upper()} on atom {atom}.")

    def _compose_2q(self) -> None:
        try:
            a0, a1 = int(self.c_a0.get()), int(self.c_a1.get())
        except ValueError:
            self._status("Atoms must be integers.", error=True)
            return
        gate = self.c_gate.get()
        try:
            self.state_model.add_2q(gate, a0, a1, parallel=self.c_parallel.get())
        except StateError as exc:
            self._status(str(exc), error=True)
            return
        self._after_add(f"Added 2Q {gate.upper()} on atoms {a0}, {a1}.")

    def _entangle_all(self, gate: str, atoms: Optional[set[int]] = None) -> None:
        gate = (gate or "cz").lower()
        if gate not in {"cz", "cx"}:
            gate = "cz"
        try:
            count = self.state_model.add_all_entangle_pairs(gate, atoms=atoms)
        except StateError as exc:
            self._status(str(exc), error=True)
            return
        scope = "selected " if atoms else ""
        self._after_add(f"Entangled {count} {scope}row pair(s) with {gate.upper()} (parallel).")

    def _compose_measure(self) -> None:
        try:
            atom = int(self.c_atom.get())
        except ValueError:
            self._status("Atom must be an integer.", error=True)
            return
        try:
            self.state_model.add_measure([atom])
        except StateError as exc:
            self._status(str(exc), error=True)
            return
        self._after_add(f"Added MEASURE on atom {atom}.")

    def _after_add(self, msg: str) -> None:
        self.selected_op = len(self.state_model.operations) - 1
        self.selected_atom = None
        self.preview_step = len(self.state_model.operations)
        self.refresh_all()
        self._status(msg)

    @staticmethod
    def _parse_param(text: str) -> Optional[float]:
        text = text.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            import math
            return {"pi": math.pi, "pi/2": math.pi / 2, "pi/4": math.pi / 4}.get(text)

    # ---- edits ----
    def _edit_1q(self, index: int) -> None:
        try:
            atom = int(self.var_e_atom.get())
        except ValueError:
            self._status("Atom must be an integer.", error=True)
            return
        gate = self.var_e_gate.get().lower()
        g = {"name": gate, "q": atom}
        if gate in {"rx", "ry", "rz"}:
            p = self._parse_param(self.var_e_param.get())
            if p is None:
                self._status(f"{gate.upper()} needs an angle.", error=True)
                return
            g["params"] = [p]
        self.state_model.replace_op(index, {"type": "1qGate", "unitary": gate, "gates": [g]})
        self.refresh_all()
        self._status(f"Edited op #{index + 1}.")

    def _edit_2q(self, index: int) -> None:
        try:
            a0, a1 = int(self.var_e_a0.get()), int(self.var_e_a1.get())
        except ValueError:
            self._status("Atoms must be integers.", error=True)
            return
        gate = self.var_e_gate.get().lower()
        # validate against positions before this op
        before = self.state_model.positions_at(index)
        p0, p1 = before.get(a0), before.get(a1)
        if not (p0 and p1 and entangle_pair_ok(p0, p1)):
            self._status("CZ/CX requires both atoms in the entanglement zone, "
                         "same row, adjacent column pair {2k, 2k+1}.", error=True)
            return
        self.state_model.replace_op(index, {"type": "rydberg", "zone_id": 0,
                                            "gates": [{"q0": a0, "q1": a1, "name": gate}]})
        self.refresh_all()
        self._status(f"Edited op #{index + 1}.")

    def _edit_move(self, index: int) -> None:
        try:
            atom = int(self.var_e_atom.get())
            dst = Site(int(self.var_e_zone.get()), int(self.var_e_row.get()), int(self.var_e_col.get()))
        except ValueError:
            self._status("Atom/zone/row/col must be integers.", error=True)
            return
        before = self.state_model.positions_at(index)
        src = before.get(atom)
        if src is None:
            self._status(f"Atom {atom} does not exist at that step.", error=True)
            return
        self.state_model.replace_op(index, {
            "type": "rearrangeJob", "aod_qubits": [atom],
            "begin_locs": [src.as_loc(atom)], "end_locs": [dst.as_loc(atom)],
        })
        self.refresh_all()
        self._status(f"Edited move op #{index + 1}.")

    def _delete(self, index: int) -> None:
        self.state_model.delete_op(index)
        self.selected_op = None
        self.preview_step = min(self.preview_step, len(self.state_model.operations))
        self.refresh_all()
        self._status(f"Deleted op #{index + 1}.")

    def _duplicate(self, index: int) -> None:
        self.state_model.duplicate_op(index)
        self.refresh_all()
        self._status(f"Duplicated op #{index + 1}.")

    def _reorder(self, index: int, delta: int) -> None:
        new_index = self.state_model.move_op(index, delta)
        self.selected_op = new_index
        self.preview_step = new_index + 1
        self.refresh_all()

    def _play_to(self, index: int) -> None:
        self._select_op(index)
        self._status(f"Previewing state after op #{index + 1}.")

    # ---- project ----
    def _new_project(self) -> None:
        if not messagebox.askyesno("New project", "Discard current schedule and start fresh?"):
            return
        self._stop_replay()
        self.state_model.reset_atoms(6)
        self.selected_atom = None
        self.selected_op = None
        self.preview_step = 0
        self.refresh_all()
        self._status("New project (6 atoms).")

    def _load_zair(self) -> None:
        path = filedialog.askopenfilename(title="Load ZAIR JSON",
                                          filetypes=[("JSON", "*.json"), ("All", "*.*")])
        if not path:
            return
        import json
        try:
            with open(path, "r", encoding="utf-8") as f:
                zair = json.load(f)
            self.state_model.load_zair(zair)
        except (StateError, ValueError, OSError) as exc:
            self._status(f"Load failed: {exc}", error=True)
            return
        self.selected_atom = None
        self.selected_op = None
        self.preview_step = len(self.state_model.operations)
        self.refresh_all()
        self._status(f"Loaded {os.path.basename(path)}.")

    def _undo(self) -> None:
        if self.state_model.undo():
            self._post_history("Undo")

    def _redo(self) -> None:
        if self.state_model.redo():
            self._post_history("Redo")

    def _post_history(self, what: str) -> None:
        self.selected_atom = None
        self.selected_op = None
        self.preview_step = min(self.preview_step, len(self.state_model.operations))
        self.refresh_all()
        self._status(f"{what}.")

    # ================================================================ compile
    def _reverse_compile(self) -> ReverseResult:
        result = run_reverse(self.state_model)
        if not result.ok:
            self._set_text(self.txt_qasm, "")
            self._set_text(self.txt_qiskit, "")
            self._set_text(self.txt_stim, "")
            self._set_text(self.txt_validation, result.validation.as_text())
            self._status(f"Reverse compile failed: {result.error}", error=True)
            self.output_tabs.set("Validation")
            return result
        self._set_text(self.txt_qasm, result.qasm)
        self._set_text(self.txt_qiskit, result.qiskit_text)
        self._set_text(self.txt_stim, result.stim_text)
        self._set_text(self.txt_validation, result.validation.as_text())
        self._show_diagram(result.diagram_path)
        self._last_result = result
        verdict = "PASS" if result.validation.passed else "FAIL"
        self._status(f"Reverse compiled: {result.validation.gate_1q_count} 1Q, "
                     f"{result.validation.gate_2q_count} 2Q gates · validation {verdict}.")
        self.output_tabs.set("Circuit Output")
        return result

    def _validate(self) -> None:
        result = run_reverse(self.state_model)
        self._set_text(self.txt_validation, result.validation.as_text())
        self.output_tabs.set("Validation")
        if result.validation.passed:
            self._status("Validation PASS.")
        else:
            self._status("Validation FAIL — see Validation tab.", error=True)

    def _show_diagram(self, path: Optional[str]) -> None:
        if not path or not os.path.exists(path):
            self.diagram_label.configure(image=None, text="No diagram available.")
            return
        try:
            from PIL import Image
            img = Image.open(path)
            w, h = img.size
            scale = min(900 / w, 340 / h, 1.0)
            size = (int(w * scale), int(h * scale))
            self._diagram_image = ctk.CTkImage(light_image=img, size=size)
            self.diagram_label.configure(image=self._diagram_image, text="")
        except Exception as exc:  # pragma: no cover
            self.diagram_label.configure(image=None, text=f"Diagram render failed: {exc}")

    # ================================================================== replay
    def _fit_view(self) -> None:
        self.canvas.fit_view()

    def _replay_reset(self) -> None:
        self._stop_replay()
        self.preview_step = 0
        self.selected_op = -1
        self.selected_atom = None
        self.refresh_all()

    def _replay_step(self) -> None:
        self._stop_replay()
        if self.preview_step < len(self.state_model.operations):
            self.preview_step += 1
            self.selected_op = self.preview_step - 1
            self.refresh_all()

    def _replay_toggle(self) -> None:
        if self._replaying:
            self._stop_replay()
        else:
            self._start_replay()

    def _start_replay(self) -> None:
        if not self.state_model.operations:
            self._status("No operations to replay.", error=True)
            return
        if self.preview_step >= len(self.state_model.operations):
            self.preview_step = 0
        self._replaying = True
        self.play_btn.configure(text="⏸ Pause")
        self._replay_tick()

    def _replay_tick(self) -> None:
        if not self._replaying:
            return
        if self.preview_step >= len(self.state_model.operations):
            self._stop_replay()
            self._status("Replay complete.")
            return
        self.preview_step += 1
        self.selected_op = self.preview_step - 1
        self.selected_atom = None
        self.refresh_all()
        self._replay_job = self.after(max(150, self.replay_delay.get()), self._replay_tick)

    def _stop_replay(self) -> None:
        self._replaying = False
        if self._replay_job is not None:
            try:
                self.after_cancel(self._replay_job)
            except Exception:
                pass
            self._replay_job = None
        if hasattr(self, "play_btn"):
            self.play_btn.configure(text="▶ Play")

    # ================================================================== export
    def _open_export_dialog(self) -> None:
        ExportDialog(self)


class ExportDialog(ctk.CTkToplevel):
    def __init__(self, app: "ReverseCompilerApp") -> None:
        super().__init__(app)
        self.app = app
        self.title("Export Animation")
        self.geometry("420x520")
        self.configure(fg_color=theme.BG_APP)
        self.transient(app)
        self.after(50, self.lift)

        card = ctk.CTkFrame(self, fg_color=theme.BG_CARD, corner_radius=theme.RADIUS)
        card.pack(fill="both", expand=True, padx=14, pady=14)
        ctk.CTkLabel(card, text="Export Animation", font=theme.FONT_H1,
                     text_color=theme.TEXT).pack(anchor="w", padx=16, pady=(14, 8))

        self.fmt = ctk.StringVar(value="mp4")
        ctk.CTkSegmentedButton(card, values=["mp4", "gif"], variable=self.fmt,
                               selected_color=theme.ACCENT).pack(fill="x", padx=16, pady=6)

        self.fps = ctk.StringVar(value="30")
        self.width = ctk.StringVar(value="1280")
        self.height = ctk.StringVar(value="720")
        self.hold = ctk.StringVar(value="0.6")
        self.move = ctk.StringVar(value="0.7")
        for label, var in (("Frames per second", self.fps), ("Width (px)", self.width),
                           ("Height (px)", self.height), ("Step dwell (s)", self.hold),
                           ("Move duration (s)", self.move)):
            row = ctk.CTkFrame(card, fg_color="transparent")
            row.pack(fill="x", padx=16, pady=4)
            ctk.CTkLabel(row, text=label, font=theme.FONT_SMALL, text_color=theme.TEXT_MUTED,
                         width=150, anchor="w").pack(side="left")
            ctk.CTkEntry(row, textvariable=var, height=30, border_color=theme.BORDER).pack(
                side="right", fill="x", expand=True)

        ff = find_ffmpeg()
        ff_text = f"FFmpeg: {os.path.basename(ff)}" if ff else "FFmpeg: NOT FOUND (MP4 disabled)"
        ctk.CTkLabel(card, text=ff_text, font=theme.FONT_TINY,
                     text_color=theme.SUCCESS if ff else theme.DANGER).pack(anchor="w", padx=16, pady=(6, 2))

        self.progress = ctk.CTkProgressBar(card, progress_color=theme.ACCENT)
        self.progress.set(0)
        self.progress.pack(fill="x", padx=16, pady=(10, 4))
        self.prog_label = ctk.CTkLabel(card, text="", font=theme.FONT_TINY, text_color=theme.TEXT_MUTED)
        self.prog_label.pack(anchor="w", padx=16)

        btns = ctk.CTkFrame(card, fg_color="transparent")
        btns.pack(fill="x", padx=16, pady=14)
        ctk.CTkButton(btns, text="Cancel", command=self.destroy, fg_color=theme.BG_CARD_ALT,
                      hover_color=theme.BORDER, text_color=theme.TEXT, border_width=1,
                      border_color=theme.BORDER).pack(side="right", padx=6)
        self.export_btn = ctk.CTkButton(btns, text="Export", command=self._do_export,
                                        fg_color=theme.ACCENT, hover_color=theme.ACCENT_HOVER)
        self.export_btn.pack(side="right", padx=6)

    def _do_export(self) -> None:
        fmt = self.fmt.get()
        try:
            settings = ExportSettings(
                fps=int(self.fps.get()), width=int(self.width.get()), height=int(self.height.get()),
                hold_seconds=float(self.hold.get()), move_seconds=float(self.move.get()),
            )
        except ValueError:
            messagebox.showerror("Invalid settings", "FPS/size/durations must be numbers.", parent=self)
            return
        if not self.app.state_model.operations:
            messagebox.showinfo("Nothing to export", "Add operations before exporting.", parent=self)
            return
        path = filedialog.asksaveasfilename(
            title=f"Export {fmt.upper()}", defaultextension=f".{fmt}",
            filetypes=[(fmt.upper(), f"*.{fmt}"), ("All", "*.*")], parent=self)
        if not path:
            return

        self.export_btn.configure(state="disabled")

        def cb(done, total, msg):
            self.progress.set(done / total if total else 0)
            self.prog_label.configure(text=msg)
            self.update_idletasks()

        try:
            export_animation(self.app.state_model, path, fmt, settings, progress=cb)
        except FFmpegNotFoundError as exc:
            self.export_btn.configure(state="normal")
            messagebox.showerror("FFmpeg not found", str(exc), parent=self)
            return
        except VideoExportError as exc:
            self.export_btn.configure(state="normal")
            messagebox.showerror("Export failed", str(exc), parent=self)
            return
        except Exception as exc:  # pragma: no cover
            self.export_btn.configure(state="normal")
            messagebox.showerror("Export failed", str(exc), parent=self)
            return
        self.progress.set(1)
        self.prog_label.configure(text="Done.")
        self.app._status(f"Exported {fmt.upper()} → {os.path.basename(path)}.")
        messagebox.showinfo("Export complete", f"Saved:\n{path}", parent=self)
        self.destroy()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    app = ReverseCompilerApp()
    app.mainloop()


if __name__ == "__main__":
    main()
