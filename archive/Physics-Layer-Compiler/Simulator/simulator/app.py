"""Forward neutral-atom simulator — three-pane CustomTkinter GUI.

    ┌──────────────────────── toolbar ───────────────────────┐
    │ ⬡ Simulator   Load · Demo · Demo(invalid)      Export  │
    ├───────────────┬────────────────────────┬───────────────┤
    │  Instruction  │   Animated storage +   │   Selected    │
    │   timeline    │   entanglement zones   │   step detail │
    │  (parallel    │  ┌───playback bar───┐  │  atoms, gate, │
    │   grouped)    │  │ ◀ ▶ ⏵ ⏸ ⟳ speed  │  │  coords,      │
    │               │  │ fit · jump-to     │  │  validation   │
    ├───────────────┴────────────────────────┴───────────────┤
    │  status bar · validation summary · current step         │
    └─────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import logging
import os
from tkinter import filedialog, messagebox
from typing import Optional

import customtkinter as ctk

from . import theme
from .engine import SimulationEngine, StepInfo
from .frames import ExportSettings, step_scene
from .renderer import CanvasView
from .sample import from_compiler, sample_program
from .schema import load_instructions
from .video_export import (
    FFmpegNotFoundError,
    VideoExportError,
    export_animation,
    find_ffmpeg,
)

logger = logging.getLogger(__name__)


class SimulatorApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        theme.apply_appearance()
        self.title("Neutral-Atom Forward Simulator")
        self.geometry("1580x960")
        self.minsize(1300, 820)
        self.configure(fg_color=theme.BG_APP)

        self.engine: Optional[SimulationEngine] = None
        self.step_index = 0          # 0 == initial layout
        self.selected_step = 0
        self.move_progress = 1.0
        self.selected_atom: Optional[int] = None

        # playback
        self._playing = False
        self._play_job: Optional[str] = None
        self.speed = ctk.DoubleVar(value=1.0)

        self._build_toolbar()
        self._build_body()
        self._build_statusbar()

        self.load_program(sample_program(), source_label="built-in demo")

    # ================================================================ toolbar
    def _build_toolbar(self) -> None:
        bar = ctk.CTkFrame(self, fg_color=theme.BG_CARD, corner_radius=0, height=58)
        bar.pack(side="top", fill="x")
        bar.pack_propagate(False)

        def tb(parent, text, cmd, primary=False):
            return ctk.CTkButton(
                parent, text=text, command=cmd, height=34,
                corner_radius=theme.RADIUS_SM,
                fg_color=(theme.ACCENT if primary else theme.BG_CARD_ALT),
                hover_color=(theme.ACCENT_HOVER if primary else theme.BORDER),
                text_color=(theme.TEXT_ON_ACCENT if primary else theme.TEXT),
                font=theme.FONT_BODY, border_width=0 if primary else 1,
                border_color=theme.BORDER,
            )

        ctk.CTkLabel(bar, text="⬡  Forward Simulator", font=theme.FONT_TITLE,
                     text_color=theme.TEXT).pack(side="left", padx=(16, 20))

        left = ctk.CTkFrame(bar, fg_color="transparent")
        left.pack(side="left")
        tb(left, "Load Compiler Output", self._load_file).pack(side="left", padx=4)
        tb(left, "Demo", lambda: self.load_program(sample_program(),
           source_label="built-in demo")).pack(side="left", padx=4)
        tb(left, "Demo (invalid)", lambda: self.load_program(
           sample_program(invalid=True),
           source_label="built-in demo (invalid)")).pack(side="left", padx=4)
        tb(left, "From Compiler", self._load_from_compiler).pack(side="left", padx=4)

        right = ctk.CTkFrame(bar, fg_color="transparent")
        right.pack(side="right", padx=12)
        tb(right, "⤓ Export MP4", self._export_mp4, primary=True).pack(side="right", padx=4)
        tb(right, "⤓ Export GIF", self._export_gif).pack(side="right", padx=4)

    # =================================================================== body
    def _build_body(self) -> None:
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(side="top", fill="both", expand=True, padx=10, pady=(8, 4))
        body.grid_columnconfigure(0, weight=0, minsize=300)
        body.grid_columnconfigure(1, weight=1)
        body.grid_columnconfigure(2, weight=0, minsize=340)
        body.grid_rowconfigure(0, weight=1)

        self._build_timeline(body)
        self._build_center(body)
        self._build_details(body)

    # ------------------------------------------------------------- timeline
    def _build_timeline(self, parent) -> None:
        card = ctk.CTkFrame(parent, fg_color=theme.BG_CARD, corner_radius=theme.RADIUS)
        card.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        card.grid_rowconfigure(1, weight=1)
        card.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(card, text="Instruction Timeline", font=theme.FONT_H2,
                     text_color=theme.TEXT).grid(row=0, column=0, sticky="w",
                                                 padx=14, pady=(12, 6))
        self.timeline = ctk.CTkScrollableFrame(card, fg_color=theme.BG_CARD_ALT,
                                               corner_radius=theme.RADIUS_SM)
        self.timeline.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))
        self.timeline.grid_columnconfigure(0, weight=1)
        self._timeline_rows: list[ctk.CTkButton] = []

    # --------------------------------------------------------------- center
    def _build_center(self, parent) -> None:
        card = ctk.CTkFrame(parent, fg_color=theme.BG_CARD, corner_radius=theme.RADIUS)
        card.grid(row=0, column=1, sticky="nsew", padx=8)
        card.grid_rowconfigure(0, weight=1)
        card.grid_columnconfigure(0, weight=1)

        self.canvas = CanvasView(card)
        self.canvas.widget.grid(row=0, column=0, sticky="nsew", padx=8, pady=(10, 4))
        self.canvas.set_atom_pick(self._on_atom_pick)

        self._build_playbar(card)

    def _build_playbar(self, parent) -> None:
        bar = ctk.CTkFrame(parent, fg_color=theme.BG_CARD_ALT, corner_radius=theme.RADIUS_SM)
        bar.grid(row=1, column=0, sticky="ew", padx=8, pady=(2, 10))
        for i in range(12):
            bar.grid_columnconfigure(i, weight=0)
        bar.grid_columnconfigure(7, weight=1)

        def cb(text, cmd, w=44):
            return ctk.CTkButton(bar, text=text, command=cmd, width=w, height=32,
                                 corner_radius=theme.RADIUS_SM, font=theme.FONT_BODY,
                                 fg_color=theme.BG_CARD, hover_color=theme.BORDER,
                                 text_color=theme.TEXT, border_width=1,
                                 border_color=theme.BORDER)

        cb("⟲ Reset", self._reset, 74).grid(row=0, column=0, padx=(10, 4), pady=8)
        cb("◀ Prev", self._prev, 74).grid(row=0, column=1, padx=4, pady=8)
        self.btn_play = cb("⏵ Play", self._toggle_play, 84)
        self.btn_play.grid(row=0, column=2, padx=4, pady=8)
        cb("Next ▶", self._next, 74).grid(row=0, column=3, padx=4, pady=8)
        cb("⤢ Fit", lambda: self.canvas.fit_view(), 64).grid(row=0, column=4, padx=4, pady=8)

        ctk.CTkLabel(bar, text="Speed", font=theme.FONT_SMALL,
                     text_color=theme.TEXT_MUTED).grid(row=0, column=5, padx=(14, 4))
        ctk.CTkSlider(bar, from_=0.25, to=3.0, number_of_steps=11,
                      variable=self.speed, width=120).grid(row=0, column=6, padx=(0, 8))

        self.progress_label = ctk.CTkLabel(bar, text="", font=theme.FONT_SMALL,
                                           text_color=theme.TEXT_MUTED)
        self.progress_label.grid(row=0, column=7, sticky="e", padx=8)

        ctk.CTkLabel(bar, text="Jump", font=theme.FONT_SMALL,
                     text_color=theme.TEXT_MUTED).grid(row=0, column=8, padx=(10, 4))
        self.jump_entry = ctk.CTkEntry(bar, width=54, height=30, font=theme.FONT_SMALL)
        self.jump_entry.grid(row=0, column=9, padx=(0, 4))
        self.jump_entry.bind("<Return>", lambda _e: self._jump())
        cb("Go", self._jump, 44).grid(row=0, column=10, padx=(0, 10), pady=8)

    # -------------------------------------------------------------- details
    def _build_details(self, parent) -> None:
        card = ctk.CTkFrame(parent, fg_color=theme.BG_CARD, corner_radius=theme.RADIUS)
        card.grid(row=0, column=2, sticky="nsew", padx=(8, 0))
        card.grid_rowconfigure(1, weight=1)
        card.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(card, text="Step Details", font=theme.FONT_H2,
                     text_color=theme.TEXT).grid(row=0, column=0, sticky="w",
                                                 padx=14, pady=(12, 6))
        self.details = ctk.CTkTextbox(card, fg_color=theme.BG_CARD_ALT,
                                      corner_radius=theme.RADIUS_SM,
                                      font=theme.FONT_CODE, text_color=theme.TEXT,
                                      wrap="word")
        self.details.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))
        self.details.configure(state="disabled")

    # ------------------------------------------------------------ statusbar
    def _build_statusbar(self) -> None:
        bar = ctk.CTkFrame(self, fg_color=theme.BG_CARD, corner_radius=0, height=34)
        bar.pack(side="bottom", fill="x")
        bar.pack_propagate(False)
        self.status = ctk.CTkLabel(bar, text="Ready.", font=theme.FONT_SMALL,
                                   text_color=theme.TEXT_MUTED, anchor="w")
        self.status.pack(side="left", padx=14)
        self.valid_badge = ctk.CTkLabel(bar, text="", font=theme.FONT_SMALL, anchor="e")
        self.valid_badge.pack(side="right", padx=14)

    # =============================================================== loading
    def load_program(self, source, source_label: str = "") -> None:
        self._stop_play()
        try:
            instructions = load_instructions(source)
            engine = SimulationEngine(instructions)
        except Exception as exc:  # pragma: no cover - user input
            messagebox.showerror("Load failed", str(exc))
            self._set_status(f"Load failed: {exc}")
            return
        self.engine = engine
        self.step_index = 0
        self.selected_step = 0
        self.move_progress = 1.0
        self.selected_atom = None
        self._rebuild_timeline()
        self.canvas.fit_view()
        self.refresh()
        n_invalid = sum(1 for s in engine.steps if not s.validation.ok)
        extra = f" · {n_invalid} invalid step(s)" if n_invalid else ""
        self._set_status(
            f"Loaded {source_label or 'program'}: {len(engine.init_positions)} atoms, "
            f"{engine.num_steps} steps{extra}."
        )

    def _load_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Load compiler output (ZAIR JSON)",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        self.load_program(path, source_label=os.path.basename(path))

    def _load_from_compiler(self) -> None:
        try:
            zair = from_compiler()
        except Exception as exc:
            messagebox.showwarning(
                "Compiler unavailable",
                "Could not build a schedule from natam_compiler:\n\n"
                f"{exc}\n\nFalling back is available via the Demo button.",
            )
            return
        self.load_program(zair, source_label="natam_compiler output")

    # ============================================================== timeline
    def _rebuild_timeline(self) -> None:
        for w in self._timeline_rows:
            w.destroy()
        self._timeline_rows = []
        if self.engine is None:
            return

        self._add_timeline_row(0, "INIT", "Initial layout", True)
        for i, s in enumerate(self.engine.steps, start=1):
            self._add_timeline_row(i, s.kind, s.summary, s.validation.ok)

    def _add_timeline_row(self, index: int, kind: str, summary: str, ok: bool) -> None:
        color = theme.OP_COLORS.get(kind, theme.OP_COLORS["OTHER"])
        prefix = "●" if ok else "⚠"
        label = f"{prefix}  {index:>2}  {summary}"
        btn = ctk.CTkButton(
            self.timeline, text=label, anchor="w", height=34,
            corner_radius=theme.RADIUS_SM, font=theme.FONT_SMALL,
            fg_color=theme.BG_CARD, hover_color=theme.ACCENT_SOFT,
            text_color=(theme.TEXT if ok else theme.DANGER),
            border_width=1, border_color=(theme.BORDER if ok else theme.DANGER),
            command=lambda i=index: self._select_step(i),
        )
        # small colour chip via left border-ish: use a thin coloured label
        btn.grid(row=index, column=0, sticky="ew", padx=6, pady=3)
        self._timeline_rows.append(btn)

    def _highlight_timeline(self) -> None:
        for i, btn in enumerate(self._timeline_rows):
            if i == self.selected_step:
                btn.configure(fg_color=theme.ACCENT_SOFT, border_color=theme.ACCENT)
            else:
                ok = "⚠" not in btn.cget("text")
                btn.configure(fg_color=theme.BG_CARD,
                              border_color=(theme.BORDER if ok else theme.DANGER))

    # ============================================================== playback
    def _select_step(self, index: int) -> None:
        self._stop_play()
        self.step_index = index
        self.selected_step = index
        self.move_progress = 1.0
        self.refresh()

    def _prev(self) -> None:
        self._stop_play()
        if self.step_index > 0:
            self.step_index -= 1
            self.selected_step = self.step_index
            self.move_progress = 1.0
            self.refresh()

    def _next(self) -> None:
        self._stop_play()
        if self.engine and self.step_index < self.engine.num_steps:
            self.step_index += 1
            self.selected_step = self.step_index
            self.move_progress = 1.0
            self.refresh()

    def _reset(self) -> None:
        self._stop_play()
        self.step_index = 0
        self.selected_step = 0
        self.move_progress = 1.0
        self.refresh()

    def _jump(self) -> None:
        if self.engine is None:
            return
        try:
            target = int(self.jump_entry.get())
        except ValueError:
            self._set_status("Jump: enter a step number.")
            return
        target = max(0, min(target, self.engine.num_steps))
        self._select_step(target)

    def _toggle_play(self) -> None:
        if self._playing:
            self._stop_play()
        else:
            self._start_play()

    def _start_play(self) -> None:
        if self.engine is None or self.engine.num_steps == 0:
            return
        if self.step_index >= self.engine.num_steps:
            self.step_index = 0
            self.move_progress = 1.0
        self._playing = True
        self.btn_play.configure(text="⏸ Pause")
        self._tick()

    def _stop_play(self) -> None:
        self._playing = False
        if self._play_job is not None:
            self.after_cancel(self._play_job)
            self._play_job = None
        if hasattr(self, "btn_play"):
            self.btn_play.configure(text="⏵ Play")

    def _tick(self) -> None:
        if not self._playing or self.engine is None:
            return
        # Are we mid-way animating a MOVE step, or at a step boundary?
        next_idx = self.step_index + 1
        if next_idx > self.engine.num_steps:
            self._stop_play()
            return

        step = self.engine.steps[next_idx - 1]
        delay = int(60 / max(0.25, self.speed.get()))
        if step.kind == "MOVE":
            # advance progress towards the next step
            if self.step_index != next_idx:
                # begin animating into next_idx
                self.step_index = next_idx
                self.selected_step = next_idx
                self.move_progress = 0.0
            self.move_progress += 0.08 * max(0.25, self.speed.get())
            if self.move_progress >= 1.0:
                self.move_progress = 1.0
                self.refresh()
                self._play_job = self.after(max(200, delay * 3), self._tick)
                return
            self.refresh()
            self._play_job = self.after(delay, self._tick)
        else:
            self.step_index = next_idx
            self.selected_step = next_idx
            self.move_progress = 1.0
            self.refresh()
            self._play_job = self.after(max(250, delay * 6), self._tick)

    # ================================================================ render
    def refresh(self) -> None:
        if self.engine is None:
            return
        scene = step_scene(self.engine, self.step_index, self.move_progress)
        if self.selected_atom is not None and self.selected_atom in scene.positions:
            scene.highlight = set(scene.highlight) | {self.selected_atom}
        self.canvas.render(scene, keep_view=True)
        self._highlight_timeline()
        self._update_progress_label()
        self._update_details()
        self._update_validity_badge()

    def _update_progress_label(self) -> None:
        if self.engine is None:
            return
        self.progress_label.configure(
            text=f"Step {self.step_index} / {self.engine.num_steps}"
        )

    def _update_validity_badge(self) -> None:
        if self.engine is None:
            return
        if self.engine.is_valid():
            self.valid_badge.configure(text="✓ schedule valid", text_color=theme.SUCCESS)
        else:
            n = sum(1 for s in self.engine.steps if not s.validation.ok)
            n += 1 if self.engine.global_errors else 0
            self.valid_badge.configure(text=f"⚠ {n} issue(s)", text_color=theme.DANGER)

    def _update_details(self) -> None:
        if self.engine is None:
            return
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.insert("end", self._details_text())
        self.details.configure(state="disabled")

    def _details_text(self) -> str:
        eng = self.engine
        assert eng is not None
        idx = self.selected_step
        lines: list[str] = []
        if idx == 0:
            pos = eng.positions_at(0)
            lines.append("INITIAL LAYOUT")
            lines.append("=" * 34)
            lines.append(f"atoms: {len(pos)}")
            if eng.global_errors:
                lines.append("")
                lines.append("GLOBAL ISSUES")
                for e in eng.global_errors:
                    lines.append(f"  ⚠ {e}")
            lines.append("")
            for a in sorted(pos)[:60]:
                lines.append(f"  atom {a:<3} @ {pos[a].label()}")
            return "\n".join(lines)

        s: StepInfo = eng.steps[idx - 1]
        rep = s.validation
        lines.append(f"STEP {idx} / {eng.num_steps}   [{s.kind}]")
        lines.append("=" * 34)
        lines.append(f"summary       : {s.summary}")
        lines.append(f"parallel group: {s.parallel_size}")
        lines.append(f"atom IDs      : {sorted(s.atoms) if s.atoms else '—'}")
        lines.append(f"validation    : {rep.status}")
        lines.append("")

        if s.kind == "MOVE":
            lines.append("MOVEMENTS (parallel, simultaneous)")
            lines.append("-" * 34)
            for m in s.moves:
                lines.append(f"  atom {m.atom:<3} {m.src.label()}  →  {m.dst.label()}")
        elif s.kind in {"1Q"}:
            lines.append("GATES")
            lines.append("-" * 34)
            for g in s.gates:
                p = g.get("params") or []
                ptxt = "(" + ", ".join(f"{float(x):.4g}" for x in p) + ")" if p else ""
                lines.append(f"  {str(g['name']).upper()}{ptxt}  on atom {g['q']}")
        elif s.kind == "2Q":
            lines.append("ENTANGLING PAIRS")
            lines.append("-" * 34)
            for g in s.gates:
                lines.append(f"  {str(g['name']).upper()}  atoms {g['q0']}, {g['q1']}")
        elif s.kind == "SWAP":
            lines.append(f"SWAP atoms {s.entangle_pairs}")
        elif s.kind == "MEASURE":
            lines.append(f"MEASURE atoms {s.measured}")

        if rep.errors:
            lines.append("")
            lines.append("VALIDATION ERRORS")
            lines.append("-" * 34)
            for e in rep.errors:
                lines.append(f"  ⚠ {e}")
        if rep.warnings:
            lines.append("")
            for w in rep.warnings:
                lines.append(f"  · {w}")
        return "\n".join(lines)

    def _on_atom_pick(self, atom: int) -> None:
        self.selected_atom = atom
        self._set_status(f"Selected atom {atom}.")
        self.refresh()

    # ================================================================ export
    def _export_mp4(self) -> None:
        self._export("mp4")

    def _export_gif(self) -> None:
        self._export("gif")

    def _export(self, fmt: str) -> None:
        if self.engine is None or self.engine.num_steps == 0:
            messagebox.showinfo("Nothing to export", "Load a program first.")
            return
        if fmt == "mp4" and find_ffmpeg() is None:
            messagebox.showerror(
                "FFmpeg not found",
                "MP4 export needs FFmpeg. Install with:\n"
                "  pip install imageio-ffmpeg\n"
                "or export a GIF instead.",
            )
            return
        default = f"simulation.{fmt}"
        path = filedialog.asksaveasfilename(
            title=f"Export {fmt.upper()}", defaultextension=f".{fmt}",
            initialfile=default,
            filetypes=[(fmt.upper(), f"*.{fmt}")],
        )
        if not path:
            return
        self._stop_play()
        settings = ExportSettings()
        self._set_status(f"Exporting {fmt.upper()} … rendering frames.")
        self.update_idletasks()

        def prog(done: int, total: int, phase: str) -> None:
            self._set_status(f"{phase}: {done}/{total}")
            self.update_idletasks()

        try:
            export_animation(self.engine, path, fmt, settings, progress=prog)
        except (FFmpegNotFoundError, VideoExportError) as exc:
            messagebox.showerror("Export failed", str(exc))
            self._set_status(f"Export failed: {exc}")
            return
        except Exception as exc:  # pragma: no cover
            messagebox.showerror("Export failed", str(exc))
            self._set_status(f"Export failed: {exc}")
            return
        self._set_status(f"Exported → {path}")
        messagebox.showinfo("Export complete", f"Saved animation to:\n{path}")

    # ================================================================ helpers
    def _set_status(self, text: str) -> None:
        self.status.configure(text=text)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    app = SimulatorApp()
    app.mainloop()


if __name__ == "__main__":
    main()
